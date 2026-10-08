"""Docker sandbox lane for one task.

This pass covers method signatures, return values, and docker CLI argv
assembly from docs/sandbox_design.md. Index persistence, TaskManager
binding, EventBus, failure rollback, and startup reconciliation come next.

sandbox_id is the container name: sa-{task_id}-{name}.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import TypedDict


DEFAULT_IMAGE = "safeagent-sandbox:py310"
EXEC_TIMEOUT_SECONDS = 300
KILL_GRACE_SECONDS = 10
TASK_TTL_SECONDS = 30 * 60
KEEP_TTL_SECONDS = 24 * 60 * 60
OUTPUT_LIMIT = 50_000
TIMEOUT_MESSAGE = "Error: Timeout (300s)"

# Same charset as WorktreeManager._validate_name.
_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,40}")
_SANDBOX_ID_RE = re.compile(r"^sa-(\d+)-([A-Za-z0-9._-]{1,40})$")


class SandboxLimits(TypedDict):
    cpus: float
    memory: str
    pids: int


class SandboxEntry(TypedDict):
    name: str
    task_id: int
    status: str
    container_name: str
    container_id: str
    image: str
    workspace_volume: str
    output_volume: str
    created_at: float
    deadline_ts: float
    limits: SandboxLimits


class CreateCommands(TypedDict):
    volume_create: list[list[str]]
    seed_archive: list[str]
    seed_extract: list[str]
    run: list[str]


class CreateSandboxResult(TypedDict):
    entry: SandboxEntry
    commands: CreateCommands


class ExecResult(TypedDict):
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    message: str


class KeepSandboxResult(TypedDict):
    sandbox_id: str
    status: str
    workspace_volume: str
    output_volume: str
    deadline_ts: float
    volume_retain_until: float
    commands: list[list[str]]


class RemoveSandboxResult(TypedDict):
    sandbox_id: str
    status: str
    workspace_volume: str
    output_volume: str
    commands: list[list[str]]


def parse_sandbox_id(sandbox_id: str) -> tuple[int, str]:
    """Return (task_id, name) from a container name sa-{task_id}-{name}."""
    matched = _SANDBOX_ID_RE.fullmatch(sandbox_id or "")
    if not matched:
        raise ValueError("Invalid sandbox_id. Expected sa-{task_id}-{name}")
    return int(matched.group(1)), matched.group(2)


def _clip(text: str | bytes | None) -> str:
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    return text[:OUTPUT_LIMIT]


def _timeout_result(stdout: str | bytes | None = None, stderr: str | bytes | None = None) -> ExecResult:
    return {
        "stdout": _clip(stdout),
        "stderr": _clip(stderr),
        "exit_code": 124,
        "timed_out": True,
        "message": TIMEOUT_MESSAGE,
    }


class SandboxManager:
    """One task, one container. Command builders are the source of the docker CLI."""

    def __init__(self, repo_root: Path | None = None, *, image: str = DEFAULT_IMAGE):
        self.repo_root = Path(repo_root) if repo_root is not None else Path.cwd()
        self.image = image
        self.exec_timeout = EXEC_TIMEOUT_SECONDS
        self.kill_grace = KILL_GRACE_SECONDS
        self.task_ttl = TASK_TTL_SECONDS
        self.keep_ttl = KEEP_TTL_SECONDS
        # Confirmation aid: argv staged here, not executed and not written to the index.
        self._plans: dict[str, dict] = {}

    def create_sandbox(self, name: str, task_id: int, base_ref: str = "HEAD") -> CreateSandboxResult:
        """Stage a sandbox lane.

        Returns the index entry (status=creating) plus the docker argv that the
        next pass will run, in this order: create two volumes, git archive into
        sa-ws-{task_id}, docker run -d. Does not call Docker, write the index,
        or bind the task.
        """
        self._validate_name(name)
        now = time.time()
        container_name = f"sa-{task_id}-{name}"
        workspace_volume = f"sa-ws-{task_id}"
        output_volume = f"sa-out-{task_id}"
        entry: SandboxEntry = {
            "name": name,
            "task_id": task_id,
            "status": "creating",
            "container_name": container_name,
            "container_id": "",
            "image": self.image,
            "workspace_volume": workspace_volume,
            "output_volume": output_volume,
            "created_at": now,
            "deadline_ts": now + self.task_ttl,
            "limits": {"cpus": 1.0, "memory": "512m", "pids": 128},
        }
        commands: CreateCommands = {
            "volume_create": [
                self.build_volume_create_command(workspace_volume),
                self.build_volume_create_command(output_volume),
            ],
            "seed_archive": self.build_seed_archive_command(base_ref),
            "seed_extract": self.build_seed_extract_command(workspace_volume),
            "run": self.build_run_command(
                container_name=container_name,
                task_id=task_id,
                workspace_volume=workspace_volume,
                output_volume=output_volume,
            ),
        }
        self._plans[container_name] = dict(commands)
        return {"entry": entry, "commands": commands}

    def execute(self, sandbox_id: str, command: str) -> ExecResult:
        """docker exec in /workspace as uid 1000, with a 300s process-group timeout.

        GNU timeout puts `sh -c` in its own process group and sends SIGTERM,
        then SIGKILL after 10s. PID 1 (`sleep infinity`) is not in that group.
        A hung local docker CLI is killed by subprocess; the container is left up.
        """
        parse_sandbox_id(sandbox_id)
        argv = self.build_exec_command(sandbox_id, command)
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self.exec_timeout + self.kill_grace + 15,
            )
        except subprocess.TimeoutExpired as exc:
            return _timeout_result(exc.stdout, exc.stderr)

        timed_out = completed.returncode == 124
        return {
            "stdout": _clip(completed.stdout),
            "stderr": _clip(completed.stderr),
            "exit_code": completed.returncode,
            "timed_out": timed_out,
            "message": TIMEOUT_MESSAGE if timed_out else "",
        }

    def keep_sandbox(self, sandbox_id: str) -> KeepSandboxResult:
        """Stop the container and keep both volumes.

        Cancels the 30-minute destroy lease and sets a 24h volume retention.
        Does not docker commit, docker rm, or delete volumes.
        """
        task_id, _name = parse_sandbox_id(sandbox_id)
        commands = [self.build_stop_command(sandbox_id)]
        self._plans[sandbox_id] = {"keep": commands}
        return {
            "sandbox_id": sandbox_id,
            "status": "kept",
            "workspace_volume": f"sa-ws-{task_id}",
            "output_volume": f"sa-out-{task_id}",
            "deadline_ts": 0,
            "volume_retain_until": time.time() + self.keep_ttl,
            "commands": commands,
        }

    def remove_sandbox(self, sandbox_id: str) -> RemoveSandboxResult:
        """Stage stop, rm, and volume deletion.

        Order: docker stop -t 10, docker rm -f, delete sa-ws-{task_id} and
        sa-out-{task_id}. Does not call Docker or unbind the task.
        """
        task_id, _name = parse_sandbox_id(sandbox_id)
        workspace_volume = f"sa-ws-{task_id}"
        output_volume = f"sa-out-{task_id}"
        commands = [
            self.build_stop_command(sandbox_id, timeout=10),
            ["docker", "rm", "-f", sandbox_id],
            ["docker", "volume", "rm", workspace_volume],
            ["docker", "volume", "rm", output_volume],
        ]
        self._plans[sandbox_id] = {"remove": commands}
        return {
            "sandbox_id": sandbox_id,
            "status": "removed",
            "workspace_volume": workspace_volume,
            "output_volume": output_volume,
            "commands": commands,
        }

    def build_run_command(
        self,
        *,
        container_name: str,
        task_id: int,
        workspace_volume: str,
        output_volume: str,
    ) -> list[str]:
        """Assemble `docker run -d` for a long-lived lane.

        Host env is not passed. No bind mount of the repo, Docker socket, or home.
        Workspace quota (2GB sparse file) is not applied in this pass.
        """
        return [
            "docker", "run", "-d",
            "--name", container_name,
            "--cpus=1.0",
            "--memory=512m",
            "--memory-swap=512m",
            "--pids-limit=128",
            "--ulimit", "nofile=1024:1024",
            "--cap-drop=ALL",
            "--security-opt", "no-new-privileges:true",
            "--read-only",
            "--network=none",
            "--user", "1000:1000",
            "--restart=no",
            "--label", "safeagent.managed=true",
            "--label", f"safeagent.task_id={task_id}",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
            "-v", f"{workspace_volume}:/workspace",
            "-v", f"{output_volume}:/outputs",
            "--entrypoint", "sleep",
            self.image,
            "infinity",
        ]

    def build_exec_command(self, sandbox_id: str, command: str) -> list[str]:
        """docker exec as uid 1000. `timeout` signals the exec process group only."""
        return [
            "docker", "exec",
            "--user", "1000:1000",
            "-w", "/workspace",
            sandbox_id,
            "timeout",
            "--signal=TERM",
            "-k", str(self.kill_grace),
            str(self.exec_timeout),
            "sh", "-c", command,
        ]

    def build_kill_process_group_command(self, sandbox_id: str, pgid: int, signal: str) -> list[str]:
        """Kill one in-container process group. signal is TERM or KILL. Never targets PID 1."""
        if signal not in ("TERM", "KILL"):
            raise ValueError("signal must be TERM or KILL")
        if pgid <= 1:
            raise ValueError("refusing to signal PID 1 or an invalid process group")
        return [
            "docker", "exec",
            "--user", "1000:1000",
            sandbox_id,
            "kill", f"-{signal}", "--", f"-{pgid}",
        ]

    def build_volume_create_command(self, volume_name: str) -> list[str]:
        return ["docker", "volume", "create", volume_name]

    def build_seed_archive_command(self, base_ref: str) -> list[str]:
        """Host-side `git archive`. cwd is repo_root. base_ref is not resolved yet."""
        return ["git", "-C", str(self.repo_root), "archive", base_ref]

    def build_seed_extract_command(self, workspace_volume: str) -> list[str]:
        """Read archive bytes on stdin and unpack them into the named volume."""
        return [
            "docker", "run", "--rm", "-i",
            "-v", f"{workspace_volume}:/workspace",
            "alpine",
            "tar", "-x", "-C", "/workspace",
        ]

    def build_stop_command(self, sandbox_id: str, timeout: int | None = None) -> list[str]:
        if timeout is None:
            return ["docker", "stop", sandbox_id]
        return ["docker", "stop", "-t", str(timeout), sandbox_id]

    def _validate_name(self, name: str) -> None:
        if not _NAME_RE.fullmatch(name or ""):
            raise ValueError(
                "Invalid sandbox name. Use 1-40 chars: letters, numbers, ., _, -"
            )
