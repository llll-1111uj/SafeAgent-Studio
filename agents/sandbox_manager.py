"""Docker sandbox lane: one task, one container.

Host-side seeding streams `git archive` into `docker run tar` with
subprocess pipes. No shell is started, so Windows does not depend on a
POSIX `|` operator. The archive excludes `.env` so secrets are not copied
into /workspace. Host environment enters the lane only through docker run
-e, and only for names in SANDBOX_ENV_ALLOWLIST.

sandbox_id is the container name: sa-{task_id}-{name}.
Index file: {repo_root}/.worktrees/index.json, key "sandboxes".
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, TypeVar, TypedDict




DEFAULT_IMAGE = "safeagent-sandbox:py310"
# Host variable names that may be passed with docker run -e. Empty on purpose:
# the model client runs on the host, and `env` inside the lane is auto-approved.
SANDBOX_ENV_ALLOWLIST: tuple[str, ...] = ()
_ARCHIVE_EXCLUDES = (
    ":(exclude).env",
    ":(glob,exclude)**/.env",
    ":(exclude).env.*",
    ":(glob,exclude)**/.env.*",
)
HELPER_IMAGE = "alpine:latest"
# Docker Engine registry-mirrors. USTC and NetEase are the Docker Desktop example;
# neither host resolves here, so DaoCloud is tried first.
REGISTRY_MIRRORS = (
    "https://docker.m.daocloud.io",
    "https://docker.mirrors.ustc.edu.cn",
    "https://hub-mirror.c.163.com",
)
EXEC_TIMEOUT_SECONDS = 300
KILL_GRACE_SECONDS = 10
TASK_TTL_SECONDS = 30 * 60
KEEP_TTL_SECONDS = 24 * 60 * 60
OUTPUT_LIMIT = 50_000
MAX_ACTIVE = 4
TIMEOUT_MESSAGE = "Error: Timeout (300s)"
_T = TypeVar("_T")

_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,40}")
_SANDBOX_ID_RE = re.compile(r"^sa-(\d+)-([A-Za-z0-9._-]{1,40})$")
_LIVE_STATUSES = {"creating", "active", "kept", "removing"}
_TRANSITIONS = {
    "creating": {"active", "failed", "removing"},
    "active": {"kept", "removing", "failed", "timed_out"},
    "kept": {"removing", "failed"},
    "removing": {"removed", "failed", "active", "kept"},
    "failed": {"removing"},
    "removed": set(),
    "timed_out": {"removing", "failed"},
}


class SandboxError(RuntimeError):
    """Docker or index operation failed. Resources were rolled back when possible."""


class SandboxConsistencyError(SandboxError):
    """Docker and index.json do not describe the same sandbox."""


class ExecResult(TypedDict):
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    message: str


def parse_sandbox_id(sandbox_id: str) -> tuple[int, str]:
    """Return (task_id, name) from container name sa-{task_id}-{name}."""
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
    """Create, exec, keep, and remove a per-task Docker sandbox."""

    def __init__(self, repo_root: Path | str | None = None, *, image: str = DEFAULT_IMAGE):
        self.repo_root = Path(repo_root) if repo_root is not None else Path.cwd()
        self.repo_root = self.repo_root.resolve()
        self.image = image
        self.exec_timeout = EXEC_TIMEOUT_SECONDS
        self.kill_grace = KILL_GRACE_SECONDS
        self.task_ttl = TASK_TTL_SECONDS
        self.keep_ttl = KEEP_TTL_SECONDS
        self.max_active = MAX_ACTIVE
        self.index_path = self.repo_root / ".worktrees" / "index.json"
        self.events_path = self.repo_root / ".worktrees" / "events.jsonl"
        self.tasks_dir = self.repo_root / ".tasks"
        self._lock_path = self.repo_root / ".worktrees" / "index.lock"
        self.startup_reconciliation()

    def create_sandbox(self, name: str, task_id: int, base_ref: str = "HEAD") -> dict:
        """Create volumes, seed the workspace, start the container, and record it.

        Returns the index entry. status is active and container_id is set.
        On any failure after resources exist, the container and both volumes
        are removed and the index entry is marked failed. The task is not bound.
        """
        self._validate_name(name)
        self._validate_task_id(task_id)
        self._assert_task_exists(task_id)

        now = time.time()
        container_name = f"sa-{task_id}-{name}"
        workspace_volume = f"sa-ws-{task_id}"
        output_volume = f"sa-out-{task_id}"
        entry = {
            "name": name,
            "task_id": task_id,
            "status": "creating",
            "container_name": container_name,
            "container_id": "",
            "image": self.image,
            "workspace_volume": workspace_volume,
            "output_volume": output_volume,
            "base_commit": "",
            "created_at": now,
            "deadline_ts": now + self.task_ttl,
            "limits": {"cpus": 1.0, "memory": "512m", "pids": 128},
            "error": "",
        }
        self._emit("sandbox.create.before", entry)
        self._mutate(lambda data: self._reserve(data, entry))

        containers: list[str] = []
        volumes: list[str] = []
        try:
            commit = self._resolve_commit(base_ref)
            entry["base_commit"] = commit
            helper_image = self._ensure_helper_image()
            self._ensure_sandbox_image()
            for volume in (workspace_volume, output_volume):
                volumes.append(volume)
                self._create_fresh_volume(volume)
            seed_name = f"{container_name}-seed"
            containers.append(seed_name)
            self._seed_workspace(workspace_volume, commit, seed_name, helper_image)
            chown_name = f"{container_name}-chown"
            containers.append(chown_name)
            self._chown_volumes(workspace_volume, output_volume, chown_name, helper_image)
            # One-shot helpers use --rm. Drop them from the rollback list once they exit.
            containers.clear()
            containers.append(container_name)
            started = self._run(
                self.build_run_command(
                    container_name=container_name,
                    task_id=task_id,
                    workspace_volume=workspace_volume,
                    output_volume=output_volume,
                ),
                timeout=120,
            )
            container_id = started.stdout.strip()
            if not container_id:
                raise SandboxError("docker run returned an empty container id")
            entry["container_id"] = container_id
            entry["status"] = "active"
            entry["error"] = ""
            saved = self._mutate(lambda data: self._activate(data, entry))
            try:
                self._bind_task(task_id, name)
            except Exception as bind_exc:
                raise SandboxError("task bind failed after container start") from bind_exc
            self._emit("sandbox.create.after", saved)
            return saved
        except Exception as exc:
            self._fail_create(container_name, containers, volumes, exc)
            raise

    def execute(self, sandbox_id: str, command: str) -> ExecResult:
        """Run command in /workspace as uid 1000.

        The 300s limit applies to the exec'd process group. On expiry that
        group receives SIGTERM, then SIGKILL 10s later. The container PID 1
        is not signalled.
        """
        if "\x00" in command:
            raise ValueError("command must not contain NUL")
        parse_sandbox_id(sandbox_id)
        entry = self._load_entry(sandbox_id)
        if entry["status"] != "active":
            raise SandboxError(f"sandbox {sandbox_id} is {entry['status']}, exec requires active")
        if not self._container_running(sandbox_id):
            self._mark_failed(sandbox_id, "index says active but container is not running")
            raise SandboxConsistencyError(
                f"index says {sandbox_id} is active, but the container is not running"
            )

        token = uuid.uuid4().hex
        proc = subprocess.Popen(
            self._build_exec_argv(sandbox_id, command, token),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
        try:
            stdout, stderr = proc.communicate(timeout=self.exec_timeout)
        except subprocess.TimeoutExpired:
            self._terminate_exec_group(sandbox_id, token)
            try:
                stdout, stderr = proc.communicate(timeout=self.kill_grace + 20)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
            self._emit("sandbox.exec.timeout", entry)
            return _timeout_result(stdout, stderr)

        return {
            "stdout": _clip(stdout),
            "stderr": _clip(stderr),
            "exit_code": int(proc.returncode if proc.returncode is not None else 1),
            "timed_out": False,
            "message": "",
        }

    def keep_sandbox(self, sandbox_id: str) -> dict:
        """Stop the container, keep both volumes, and extend the lease by 24 hours.

        Transition: active -> kept. A second call on an already kept sandbox
        returns the current entry.
        """
        parse_sandbox_id(sandbox_id)
        entry = self._load_entry(sandbox_id)
        if entry["status"] == "kept":
            return entry
        if entry["status"] != "active":
            raise SandboxError(f"cannot keep sandbox from status {entry['status']}")

        try:
            self._run(["docker", "stop", sandbox_id], timeout=60)
        except SandboxError as exc:
            if self._container_exists(sandbox_id):
                raise SandboxError(f"docker stop failed, sandbox stays active: {exc}") from exc
            self._mark_failed(sandbox_id, f"keep failed, container missing: {exc}")
            raise SandboxConsistencyError(
                f"index said active but container {sandbox_id} was already gone"
            ) from exc

        retain_until = time.time() + self.keep_ttl
        try:
            saved = self._mutate(
                lambda data: self._transition(
                    data,
                    sandbox_id,
                    "kept",
                    extra={
                        "deadline_ts": retain_until,
                        "kept_at": time.time(),
                        "volume_retain_until": retain_until,
                        "error": "",
                    },
                )
            )
        except Exception as exc:
            raise SandboxConsistencyError(
                f"container {sandbox_id} is stopped but index.json could not be marked kept: {exc}"
            ) from exc
        self._emit("sandbox.keep", saved)
        return saved

    def _export_artifacts(self, container_name: str, task_id: int, workspace_volume: str) -> str:
        """Copy /workspace onto the host before the container is deleted.

        A running lane is packed with docker exec. A stopped lane is packed
        from its workspace volume. Either way the host receives the tree at
        .worktrees/artifacts/{task_id}/, without .git or __pycache__.
        Returns "" when there is nothing left to copy.
        """
        if self._container_running(container_name):
            pack_cmd = [
                "docker", "exec", "-u", "1000:1000", container_name,
                "tar", "--exclude=.git", "--exclude=__pycache__",
                "-C", "/workspace", "-cf", "-", ".",
            ]
        elif workspace_volume and self._volume_exists(workspace_volume):
            pack_cmd = [
                "docker", "run", "--rm", "--network=none",
                "-v", f"{workspace_volume}:/workspace:ro",
                "--entrypoint", "tar", self.image,
                "--exclude=.git", "--exclude=__pycache__",
                "-C", "/workspace", "-cf", "-", ".",
            ]
        else:
            return ""

        dest = self.repo_root / ".worktrees" / "artifacts" / str(task_id)
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        self._pipe_tar(pack_cmd, dest)
        print("[sandbox] artifacts task_id=%s dir=%s" % (task_id, dest), flush=True)
        return str(dest)

    def _pipe_tar(self, pack_cmd: list[str], dest: Path) -> None:
        """Stream a tar producer into the host tar extractor. No shell pipe."""
        pack = subprocess.Popen(
            pack_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
        extract: subprocess.Popen[bytes] | None = None
        try:
            if pack.stdout is None or pack.stderr is None:
                raise SandboxError("artifact tar did not provide pipes")
            extract = subprocess.Popen(
                ["tar", "-xf", "-", "-C", str(dest)],
                stdin=pack.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
            pack.stdout.close()
            pack_err: list[bytes] = []

            def _drain_pack_stderr() -> None:
                assert pack.stderr is not None
                pack_err.append(pack.stderr.read())

            drainer = threading.Thread(target=_drain_pack_stderr, daemon=True)
            drainer.start()
            try:
                _out, extract_err = extract.communicate(timeout=180)
            except subprocess.TimeoutExpired as exc:
                extract.kill()
                pack.kill()
                extract.communicate()
                raise SandboxError("timed out exporting workspace artifacts") from exc
            finally:
                drainer.join(timeout=30)
            pack.wait(timeout=30)
            if pack.returncode != 0 or extract.returncode != 0:
                detail = _clip(b"".join(pack_err)) + " " + _clip(extract_err)
                raise SandboxError(f"artifact export failed: {detail.strip()}")
        finally:
            if pack.poll() is None:
                pack.kill()
                pack.wait(timeout=10)
            if extract is not None and extract.poll() is None:
                extract.kill()
                extract.wait(timeout=10)

    def remove_sandbox(self, sandbox_id: str, force: bool = False, *, complete_task: bool = False) -> dict:
        """Export /workspace, then stop, delete the container and both volumes.

        sandbox_id may be the container name sa-{task_id}-{name}, or the
        sandbox name itself, such as agent-999. Transition:
        active|kept|failed|creating -> removing -> removed.
        Artifacts are copied to .worktrees/artifacts/{task_id}/ before the
        container is stopped. .git and __pycache__ are left behind.
        docker stop -t 10 is followed by docker rm -f even when stop fails.
        force is kept for existing callers. complete_task=True marks the task completed.
        """
        sandbox_id = self._resolve_sandbox_id(sandbox_id)
        task_id, _name = parse_sandbox_id(sandbox_id)
        entry = self._load_entry(sandbox_id)
        previous = entry["status"]
        if previous == "removed":
            return entry
        if previous not in {"creating", "active", "kept", "failed", "removing"}:
            raise SandboxError(f"cannot remove sandbox from status {previous}")

        artifacts_dir = self._export_artifacts(
            sandbox_id,
            task_id,
            str(entry.get("workspace_volume") or ""),
        )
        self._emit("sandbox.remove.before", entry)
        if previous != "removing":
            entry = self._mutate(lambda data: self._transition(data, sandbox_id, "removing", extra={"error": ""}))

        workspace_volume = entry["workspace_volume"]
        output_volume = entry["output_volume"]
        try:
            # stop -t 10; a stop failure still continues to docker rm -f.
            self._stop_for_remove(sandbox_id)
        except SandboxError:
            self._emit("sandbox.remove.failed", entry, error="docker stop failed")
            raise
        try:
            self._run_allow_missing(["docker", "rm", "-f", sandbox_id], timeout=60)
            self._run_allow_missing(["docker", "volume", "rm", workspace_volume], timeout=60)
            self._run_allow_missing(["docker", "volume", "rm", output_volume], timeout=60)
        except Exception as exc:
            self._mark_failed(sandbox_id, str(exc))
            self._emit("sandbox.remove.failed", entry, error=str(exc))
            raise

        if self._container_exists(sandbox_id) or self._volume_exists(workspace_volume) or self._volume_exists(output_volume):
            self._mark_failed(sandbox_id, "remove finished but Docker resources are still present")
            self._emit("sandbox.remove.failed", entry, error="leftover resources")
            raise SandboxConsistencyError(
                f"remove of {sandbox_id} left Docker resources behind; index marked failed"
            )

        try:
            removed_extra: dict = {"removed_at": time.time(), "error": ""}
            if artifacts_dir:
                removed_extra["artifacts_dir"] = artifacts_dir
            saved = self._mutate(
                lambda data: self._transition(
                    data,
                    sandbox_id,
                    "removed",
                    extra=removed_extra,
                )
            )
        except Exception as exc:
            raise SandboxConsistencyError(
                f"Docker resources for {sandbox_id} are gone, but index.json is still not removed: {exc}"
            ) from exc

        try:
            self._unbind_task(task_id, complete=complete_task)
        except Exception as exc:
            self._emit("sandbox.remove.failed", saved, error=f"unbind failed: {exc}")
            raise SandboxConsistencyError(
                f"sandbox {sandbox_id} is removed, but task {task_id} could not be unbound: {exc}"
            ) from exc
        if complete_task:
            self._emit("task.completed", saved)
        self._emit("sandbox.remove.after", saved)
        return saved

    def list_expired(self, now: float | None = None) -> list[dict]:
        """Active lanes whose index deadline_ts is already due.

        The deadline lives in index.json, so a restarted process sees the same
        leases. Entries with no numeric deadline are left alone.
        """
        moment = time.time() if now is None else now

        def read(data: dict) -> list[dict]:
            due: list[dict] = []
            for item in data.get("sandboxes", []):
                if item.get("status") != "active":
                    continue
                deadline = item.get("deadline_ts")
                if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
                    continue
                if deadline <= moment:
                    due.append(copy.deepcopy(item))
            return due

        return self._mutate(read, save=False)

    def timeout_sandbox(self, sandbox_id: str) -> dict:
        """Destroy one lane because its index deadline has passed.

        Order: sandbox.timeout, docker stop -t 10, docker rm -f, delete both
        volumes, mark the index timed_out, unbind the task. Task status stays
        in_progress.
        """
        task_id, _name = parse_sandbox_id(sandbox_id)
        entry = self._load_entry(sandbox_id)
        if entry["status"] == "timed_out":
            return entry
        if entry["status"] != "active":
            raise SandboxError(f"cannot timeout sandbox from status {entry['status']}")
        deadline = entry.get("deadline_ts")
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or deadline > time.time():
            return entry

        self._emit("sandbox.timeout", entry)
        workspace_volume = entry["workspace_volume"]
        output_volume = entry["output_volume"]
        try:
            self._stop_for_remove(sandbox_id)
            self._run_allow_missing(["docker", "rm", "-f", sandbox_id], timeout=60)
            self._run_allow_missing(["docker", "volume", "rm", workspace_volume], timeout=60)
            self._run_allow_missing(["docker", "volume", "rm", output_volume], timeout=60)
        except Exception as exc:
            self._mark_failed(sandbox_id, str(exc))
            raise

        if (
            self._container_exists(sandbox_id)
            or self._volume_exists(workspace_volume)
            or self._volume_exists(output_volume)
        ):
            self._mark_failed(sandbox_id, "timeout finished but Docker resources are still present")
            raise SandboxConsistencyError(
                f"timeout of {sandbox_id} left Docker resources behind; index marked failed"
            )

        try:
            saved = self._mutate(
                lambda data: self._transition(
                    data,
                    sandbox_id,
                    "timed_out",
                    extra={"timed_out_at": time.time(), "error": ""},
                )
            )
        except Exception as exc:
            raise SandboxConsistencyError(
                f"Docker resources for {sandbox_id} are gone, but index.json could not be marked timed_out: {exc}"
            ) from exc

        try:
            self._unbind_task(task_id, complete=False)
        except Exception as exc:
            raise SandboxConsistencyError(
                f"sandbox {sandbox_id} timed out, but task {task_id} could not be unbound: {exc}"
            ) from exc
        return saved

    def startup_reconciliation(self) -> dict:
        """Compare managed containers with index.json.

        An index entry in creating, active, kept, or removing with no container
        is marked failed. A container labeled safeagent.managed=true whose name
        is absent from the index is removed with docker rm -f.
        """
        present = set(self._list_managed_containers())

        def mark_missing(data: dict) -> list[str]:
            failed: list[str] = []
            for item in data.get("sandboxes", []):
                name = item.get("container_name") or ""
                if item.get("status") not in {"creating", "active", "kept", "removing"}:
                    continue
                if name and name not in present:
                    item["status"] = "failed"
                    item["error"] = "container missing at startup reconciliation"
                    item["failed_at"] = time.time()
                    failed.append(name)
            return failed

        failed = self._mutate(mark_missing)
        known = self._indexed_container_names()
        orphans = sorted(present - known)
        removed: list[str] = []
        for name in orphans:
            self._run_allow_missing(["docker", "rm", "-f", name], timeout=60)
            removed.append(name)
        return {"failed": failed, "removed_orphans": removed}

    def _list_managed_containers(self) -> list[str]:
        result = self._run(
            [
                "docker", "ps", "-a",
                "--filter", "label=safeagent.managed=true",
                "--format", "{{.Names}}",
            ],
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise SandboxError(f"failed to list managed containers: {detail}")
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def _indexed_container_names(self) -> set[str]:
        def read(data: dict) -> set[str]:
            return {
                item.get("container_name") or ""
                for item in data.get("sandboxes", [])
                if item.get("container_name")
            }

        return self._mutate(read, save=False)

    def _fail_create(
        self,
        container_name: str,
        containers: list[str],
        volumes: list[str],
        exc: Exception,
    ) -> None:
        """Reverse cleanup: containers, then volumes, then index status failed."""
        rollback_errors = self._rollback_containers(containers) + self._rollback_volumes(volumes)
        marked = self._mark_failed(container_name, str(exc))
        self._emit("sandbox.create.failed", {"container_name": container_name}, error=str(exc))
        if not marked:
            raise SandboxConsistencyError(
                f"create failed for {container_name}, and index.json could not be marked failed: {exc}"
            ) from exc
        try:
            leftover = self._container_exists(container_name) or any(
                self._volume_exists(volume) for volume in volumes
            )
        except SandboxError:
            leftover = True
        if leftover or rollback_errors:
            raise SandboxConsistencyError(
                f"create failed and leftover Docker resources remain for {container_name}: {exc}"
                + (f"; rollback: {'; '.join(rollback_errors)}" if rollback_errors else "")
            ) from exc

    def build_run_command(
        self,
        *,
        container_name: str,
        task_id: int,
        workspace_volume: str,
        output_volume: str,
    ) -> list[str]:
        """docker run argv for a long-lived lane.

        The process environment is not copied. Only SANDBOX_ENV_ALLOWLIST
        names are added with -e, and .env is never mounted as a file.
        """
        command = [
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
        ]
        command.extend(self._sandbox_env_flags())
        command.extend(["--entrypoint", "sleep", self.image, "infinity"])
        return command

    def _sandbox_env_flags(self) -> list[str]:
        flags: list[str] = []
        for name in SANDBOX_ENV_ALLOWLIST:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                continue
            value = os.environ.get(name)
            if value is None or "\n" in value or "\r" in value or "\x00" in value:
                continue
            flags.extend(["-e", "%s=%s" % (name, value)])
        return flags

    def _build_exec_argv(self, sandbox_id: str, command: str, token: str) -> list[str]:
        out = f"/tmp/sa-{token}.out"
        err = f"/tmp/sa-{token}.err"
        pid = f"/tmp/sa-{token}.pid"
        # setsid starts the command in its own process group (pgid == $!).
        # The wrapper shell stays in a different group, so killing -pgid does
        # not kill container PID 1.
        script = "\n".join([
            "set +e",
            f'out="{out}"',
            f'err="{err}"',
            f'pidfile="{pid}"',
            'if command -v setsid >/dev/null 2>&1; then',
            '  setsid sh -c "$SA_CMD" >"$out" 2>"$err" </dev/null &',
            "else",
            '  sh -c "$SA_CMD" >"$out" 2>"$err" </dev/null &',
            "fi",
            'echo "$!" > "$pidfile"',
            'wait "$!"',
            "status=$?",
            'cat "$out"',
            'cat "$err" >&2',
            'exit "$status"',
        ])
        return [
            "docker", "exec",
            "-u", "1000:1000",
            "-w", "/workspace",
            "-e", f"SA_CMD={command}",
            sandbox_id,
            "sh", "-c", script,
        ]

    def _terminate_exec_group(self, sandbox_id: str, token: str) -> None:
        """SIGTERM the exec process group, then SIGKILL after the grace period."""
        pgid = self._read_exec_pgid(sandbox_id, token)
        if pgid is None:
            return
        self._signal_group(sandbox_id, pgid, "TERM")
        time.sleep(self.kill_grace)
        self._signal_group(sandbox_id, pgid, "KILL")

    def _read_exec_pgid(self, sandbox_id: str, token: str) -> int | None:
        result = self._run(
            ["docker", "exec", "-u", "1000:1000", sandbox_id, "cat", f"/tmp/sa-{token}.pid"],
            timeout=20,
            check=False,
        )
        text = (result.stdout or "").strip()
        if result.returncode != 0 or not text.isdigit():
            return None
        pgid = int(text)
        if pgid <= 1:
            return None
        return pgid

    def _signal_group(self, sandbox_id: str, pgid: int, signal: str) -> None:
        if signal not in {"TERM", "KILL"} or pgid <= 1:
            raise SandboxError("refusing to signal container PID 1")
        self._run(
            ["docker", "exec", "-u", "1000:1000", sandbox_id, "kill", "-s", signal, "--", f"-{pgid}"],
            timeout=20,
            check=False,
        )

    def _ensure_helper_image(self) -> str:
        """Pull alpine through a registry mirror before seeding."""
        return self._pull_via_mirrors("library/alpine:latest", HELPER_IMAGE)

    def _ensure_sandbox_image(self) -> None:
        """Build safeagent-sandbox:py310 locally when it is not already present.

        This name is not on Docker Hub. docker run would otherwise try
        registry-1.docker.io and time out. The Python base image is pulled
        through the same registry mirrors, then git and a uid 1000 user are
        installed on top.
        """
        if self._image_exists(self.image):
            return
        base = self._pull_via_mirrors("library/python:3.10-slim-bookworm", "python:3.10-slim-bookworm")
        dockerfile = Path(__file__).resolve().parent / "docker" / "Dockerfile"
        if not dockerfile.is_file():
            raise SandboxError(f"sandbox Dockerfile not found: {dockerfile}")
        self._run(
            [
                "docker", "build",
                "-f", str(dockerfile),
                "-t", self.image,
                "--build-arg", f"BASE_IMAGE={base}",
                str(dockerfile.parent),
            ],
            timeout=600,
        )

    def _pull_via_mirrors(self, repository: str, local_name: str) -> str:
        """Pull repository from REGISTRY_MIRRORS and tag it local_name."""
        if self._image_exists(local_name):
            return local_name
        errors: list[str] = []
        for mirror in REGISTRY_MIRRORS:
            image = self._mirror_image(mirror, repository)
            if self._image_exists(image):
                if image != local_name:
                    self._run(["docker", "tag", image, local_name], timeout=30)
                return local_name
            result = self._run(["docker", "pull", image], timeout=600, check=False)
            if result.returncode == 0:
                if image != local_name:
                    self._run(["docker", "tag", image, local_name], timeout=30)
                return local_name
            errors.append(f"{image}: {self._last_line(result.stderr or result.stdout)}")
        result = self._run(["docker", "pull", local_name], timeout=600, check=False)
        if result.returncode == 0:
            return local_name
        errors.append(f"{local_name}: {self._last_line(result.stderr or result.stdout)}")
        raise SandboxError(
            f"failed to pull {local_name} via registry mirrors: " + " | ".join(errors)
        )

    def _mirror_image(self, mirror: str, name: str = "library/alpine:latest") -> str:
        host = mirror
        for prefix in ("https://", "http://"):
            if host.startswith(prefix):
                host = host[len(prefix):]
        return f"{host.rstrip('/')}/{name}"

    def _image_exists(self, image: str) -> bool:
        result = self._run(["docker", "image", "inspect", image], timeout=30, check=False)
        return result.returncode == 0

    @staticmethod
    def _last_line(text: str | None) -> str:
        lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
        return lines[-1] if lines else "pull failed"

    def _seed_workspace(
        self,
        workspace_volume: str,
        commit: str,
        container_name: str,
        helper_image: str,
    ) -> None:
        """Copy a git tree into the named volume without a shell pipe.

        `git archive` stdout is attached to `docker run -i tar` stdin. The
        parent closes its copy of that pipe so tar sees EOF when git exits.
        """
        archive_cmd = [
            "git", "-C", str(self.repo_root), "archive", commit, "--", ".",
            *_ARCHIVE_EXCLUDES,
        ]
        extract_cmd = [
            "docker", "run", "--name", container_name, "--rm", "-i",
            "-v", f"{workspace_volume}:/workspace",
            helper_image,
            "tar", "-x", "-C", "/workspace",
        ]
        archive = subprocess.Popen(
            archive_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
        extract: subprocess.Popen[bytes] | None = None
        try:
            if archive.stdout is None or archive.stderr is None:
                raise SandboxError("git archive did not provide pipes")
            extract = subprocess.Popen(
                extract_cmd,
                stdin=archive.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
            archive.stdout.close()
            git_err: list[bytes] = []

            def _drain_git_stderr() -> None:
                assert archive.stderr is not None
                git_err.append(archive.stderr.read())

            drainer = threading.Thread(target=_drain_git_stderr, daemon=True)
            drainer.start()
            try:
                _out, extract_err = extract.communicate(timeout=180)
            except subprocess.TimeoutExpired as exc:
                extract.kill()
                archive.kill()
                extract.communicate()
                raise SandboxError("timed out while seeding the workspace volume") from exc
            finally:
                drainer.join(timeout=30)
            archive.wait(timeout=30)
            if archive.returncode != 0 or extract.returncode != 0:
                git_text = (git_err[0] if git_err else b"").decode("utf-8", "replace").strip()
                docker_text = (extract_err or b"").decode("utf-8", "replace").strip()
                raise SandboxError(
                    "workspace seed failed: "
                    f"git({archive.returncode}) {git_text}; "
                    f"docker({extract.returncode}) {docker_text}"
                )
        finally:
            if archive.poll() is None:
                archive.kill()
                archive.wait(timeout=10)
            if extract is not None and extract.poll() is None:
                extract.kill()
                extract.wait(timeout=10)
            try:
                self._run_allow_missing(["docker", "rm", "-f", container_name], timeout=30)
            except SandboxError:
                pass

    def _chown_volumes(
        self,
        workspace_volume: str,
        output_volume: str,
        container_name: str,
        helper_image: str,
    ) -> None:
        """Archive extraction runs as root. The lane user is uid 1000."""
        try:
            self._run(
                [
                    "docker", "run", "--name", container_name, "--rm",
                    "-v", f"{workspace_volume}:/workspace",
                    "-v", f"{output_volume}:/outputs",
                    helper_image,
                    "chown", "-R", "1000:1000", "/workspace", "/outputs",
                ],
                timeout=120,
            )
        finally:
            try:
                self._run_allow_missing(["docker", "rm", "-f", container_name], timeout=30)
            except SandboxError:
                pass

    def _create_fresh_volume(self, volume: str) -> None:
        if self._volume_exists(volume):
            self._run(["docker", "volume", "rm", volume], timeout=60)
        self._run(["docker", "volume", "create", volume], timeout=60)

    def _resolve_commit(self, base_ref: str) -> str:
        if not base_ref or any(ch in base_ref for ch in "\n\r\x00"):
            raise ValueError("invalid base_ref")
        result = self._run(
            [
                "git", "-C", str(self.repo_root),
                "rev-parse", "--verify", "--end-of-options",
                f"{base_ref}^{{commit}}",
            ],
            timeout=30,
        )
        commit = result.stdout.strip()
        if not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
            raise SandboxError(f"base_ref did not resolve to a commit: {base_ref}")
        return commit

    def _stop_for_remove(self, sandbox_id: str) -> None:
        if not self._container_exists(sandbox_id):
            return
        self._run(["docker", "stop", "-t", "10", sandbox_id], timeout=30, check=False)

    def _rollback_containers(self, names: list[str]) -> list[str]:
        errors: list[str] = []
        for name in reversed(names):
            try:
                self._run_allow_missing(["docker", "rm", "-f", name], timeout=60)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        return errors

    def _rollback_volumes(self, volumes: list[str]) -> list[str]:
        errors: list[str] = []
        for volume in reversed(volumes):
            try:
                self._run_allow_missing(["docker", "volume", "rm", volume], timeout=60)
            except Exception as exc:
                errors.append(f"{volume}: {exc}")
        return errors

    def _mark_failed(self, sandbox_id: str, error: str) -> bool:
        def mutate(data: dict) -> bool:
            current = self._latest(data, sandbox_id)
            if current is None:
                return False
            if current["status"] in {"removed", "failed"}:
                return True
            if "failed" not in _TRANSITIONS.get(current["status"], set()):
                return False
            current["status"] = "failed"
            current["error"] = error
            current["failed_at"] = time.time()
            return True

        try:
            return self._mutate(mutate)
        except SandboxError:
            return False

    def _reserve(self, data: dict, entry: dict) -> None:
        sandboxes = data.setdefault("sandboxes", [])
        active = [item for item in sandboxes if item.get("status") in {"creating", "active"}]
        if len(active) >= self.max_active:
            raise SandboxError(f"active sandbox limit reached ({self.max_active})")
        for item in sandboxes:
            if item.get("status") not in _LIVE_STATUSES:
                continue
            if item.get("task_id") == entry["task_id"]:
                raise SandboxError(f"task {entry['task_id']} already has sandbox {item.get('container_name')}")
            if item.get("name") == entry["name"] or item.get("container_name") == entry["container_name"]:
                raise SandboxError(f"sandbox name {entry['name']} is already in use")
        sandboxes.append(copy.deepcopy(entry))

    def _activate(self, data: dict, entry: dict) -> dict:
        current = self._latest(data, entry["container_name"])
        if current is None:
            raise SandboxConsistencyError(f"index lost sandbox {entry['container_name']} during create")
        if current["status"] != "creating":
            raise SandboxConsistencyError(
                f"refusing to activate {entry['container_name']} from status {current['status']}"
            )
        current.update({
            "status": "active",
            "container_id": entry["container_id"],
            "base_commit": entry.get("base_commit", ""),
            "error": "",
        })
        return copy.deepcopy(current)

    def _transition(self, data: dict, sandbox_id: str, status: str, extra: dict | None = None) -> dict:
        current = self._latest(data, sandbox_id)
        if current is None:
            raise SandboxConsistencyError(f"index missing sandbox {sandbox_id}")
        allowed = _TRANSITIONS.get(current["status"], set())
        if status not in allowed:
            raise SandboxError(f"illegal transition {current['status']} -> {status}")
        current["status"] = status
        if extra:
            current.update(extra)
        return copy.deepcopy(current)

    def _latest(self, data: dict, sandbox_id: str) -> dict | None:
        found = None
        for item in data.get("sandboxes", []):
            if item.get("container_name") == sandbox_id:
                found = item
        return found

    def _resolve_sandbox_id(self, sandbox_id: str) -> str:
        """Return the container name for a container id or a sandbox name like agent-999."""
        try:
            parse_sandbox_id(sandbox_id)
            return sandbox_id
        except ValueError:
            pass

        removable = {"creating", "active", "kept", "failed", "removing"}

        def read(data: dict) -> str:
            found = ""
            live = ""
            for item in data.get("sandboxes", []):
                if item.get("name") != sandbox_id:
                    continue
                container = str(item.get("container_name") or "")
                if not container:
                    continue
                found = container
                if item.get("status") in removable:
                    live = container
            chosen = live or found
            if not chosen:
                raise SandboxError(f"Unknown sandbox '{sandbox_id}'")
            return chosen

        return self._mutate(read, save=False)

    def _load_entry(self, sandbox_id: str) -> dict:
        def read(data: dict) -> dict:
            current = self._latest(data, sandbox_id)
            if current is None:
                raise SandboxError(f"Unknown sandbox '{sandbox_id}'")
            return copy.deepcopy(current)

        return self._mutate(read, save=False)

    def _mutate(self, fn: Callable[[dict], _T], save: bool = True) -> _T:
        with self._locked():
            data = self._read_index()
            result = fn(data)
            if save:
                self._write_index(data)
            return result

    def _read_index(self) -> dict:
        if not self.index_path.exists():
            return {"sandboxes": []}
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SandboxError(f"index.json is corrupt: {exc}") from exc
        if not isinstance(raw, dict):
            raise SandboxError("index.json must be a JSON object")
        raw.setdefault("sandboxes", [])
        if not isinstance(raw["sandboxes"], list):
            raise SandboxError("index.json sandboxes must be a list")
        return raw

    def _write_index(self, data: dict) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, indent=2, ensure_ascii=False)
        tmp = self.index_path.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        last_error: PermissionError | None = None
        for attempt in range(5):
            try:
                os.replace(tmp, self.index_path)
                return
            except PermissionError as exc:
                last_error = exc
                if attempt < 4:
                    time.sleep(0.1)
        raise SandboxConsistencyError(
            f"failed to replace {self.index_path} after 5 attempts. "
            "Another program may be locking the index file."
        ) from last_error

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self._lock_path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if handle.seek(0, os.SEEK_END) == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                deadline = time.time() + 30
                while True:
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.time() >= deadline:
                            raise SandboxError("timed out waiting for the sandbox index lock")
                        time.sleep(0.05)
            else:
                import fcntl
                # Windows typeshed omits these Unix-only attributes.
                flock = getattr(fcntl, "flock")
                flock(handle.fileno(), getattr(fcntl, "LOCK_EX"))
            yield
        finally:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    flock = getattr(fcntl, "flock")
                    flock(handle.fileno(), getattr(fcntl, "LOCK_UN"))
            except OSError:
                pass
            handle.close()

    def _bind_task(self, task_id: int, sandbox_name: str) -> None:
        path = self._task_path(task_id)
        task = self._read_task(path)
        task["worktree"] = sandbox_name
        if task.get("status") == "pending":
            task["status"] = "in_progress"
        task["updated_at"] = time.time()
        self._write_task(path, task)

    def _unbind_task(self, task_id: int, *, complete: bool) -> None:
        path = self._task_path(task_id)
        if not path.is_file():
            return
        task = self._read_task(path)
        task["worktree"] = ""
        task["updated_at"] = time.time()
        if complete:
            task["status"] = "completed"
        self._write_task(path, task)

    def _assert_task_exists(self, task_id: int) -> None:
        if not self._task_path(task_id).is_file():
            raise ValueError(f"Task {task_id} not found")

    def _task_path(self, task_id: int) -> Path:
        return self.tasks_dir / f"task_{task_id}.json"

    def _read_task(self, path: Path) -> dict:
        try:
            task = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SandboxError(f"task file is corrupt: {path.name}") from exc
        if not isinstance(task, dict):
            raise SandboxError(f"task file must be a JSON object: {path.name}")
        return task

    def _write_task(self, path: Path, task: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(task, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _validate_name(self, name: str) -> None:
        if not _NAME_RE.fullmatch(name or ""):
            raise ValueError("Invalid sandbox name. Use 1-40 chars: letters, numbers, ., _, -")

    def _validate_task_id(self, task_id: int) -> None:
        if isinstance(task_id, bool) or not isinstance(task_id, int) or task_id < 1:
            raise ValueError("task_id must be a positive integer")

    def _container_exists(self, name: str) -> bool:
        result = self._run(["docker", "inspect", "-f", "{{.Id}}", name], timeout=30, check=False)
        return result.returncode == 0

    def _container_running(self, name: str) -> bool:
        result = self._run(
            ["docker", "inspect", "-f", "{{.State.Running}}", name],
            timeout=30,
            check=False,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"

    def _volume_exists(self, name: str) -> bool:
        result = self._run(["docker", "volume", "inspect", name], timeout=30, check=False)
        return result.returncode == 0

    def _run(
        self,
        argv: list[str],
        *,
        timeout: float = 120,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SandboxError(f"command timed out: {argv[0]}") from exc
        except FileNotFoundError as exc:
            raise SandboxError(f"executable not found: {argv[0]}") from exc
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise SandboxError(f"command failed ({result.returncode}): {' '.join(argv[:4])}: {detail}")
        return result

    def _run_allow_missing(
        self,
        argv: list[str],
        *,
        timeout: float = 60,
    ) -> subprocess.CompletedProcess[str]:
        result = self._run(argv, timeout=timeout, check=False)
        if result.returncode == 0:
            return result
        detail = f"{result.stderr or ''}{result.stdout or ''}".lower()
        if "no such" in detail:
            return result
        raise SandboxError(f"command failed ({result.returncode}): {' '.join(argv[:4])}: {detail.strip()}")

    def _emit(self, event: str, sandbox: dict | None = None, error: str | None = None) -> None:
        body = sandbox or {}
        payload: dict = {
            "event": event,
            "ts": time.time(),
            "sandbox": {
                "name": body.get("name", ""),
                "task_id": body.get("task_id"),
                "container_name": body.get("container_name", ""),
                "status": body.get("status", ""),
            },
        }
        if error:
            payload["error"] = error
        try:
            self.events_path.parent.mkdir(parents=True, exist_ok=True)
            with self.events_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError:
            return
