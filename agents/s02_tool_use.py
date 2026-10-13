#!/usr/bin/env python3
"""
s02_tool_use.py - Tools

The agent loop from s01 didn't change. We just added tools to the array
and a dispatch map to route calls.

    +----------+      +-------+      +------------------+
    |   User   | ---> |  LLM  | ---> | Tool Dispatch    |
    |  prompt  |      |       |      | {                |
    +----------+      +---+---+      |   bash: run_bash |
                          ^          |   read: run_read |
                          |          |   write: run_wr  |
                          +----------+   edit: run_edit |
                          tool_result| }                |
                                     +------------------+

Key insight: "The loop didn't change at all. I just added tools."
"""

import base64
import contextvars
import json
import os
import re
import shlex
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv
from sandbox_manager import SandboxError, SandboxManager

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

SYSTEM = (
    f"You are a coding agent at {WORKDIR}. "
    "bash, read_file, write_file, and edit_file run inside the bound sandbox "
    "at /workspace. Refuse them when the current task has no active sandbox. "
    "Act, don't explain. "
    "你处于断网沙箱，禁止尝试任何网络安装（pip install, apt-get）或外网下载。"
    "如果遇到缺失依赖，请使用纯 Python 或本地已有库降级实现。"
)

_sandbox = None
_task_id = contextvars.ContextVar("safeagent_task_id", default=None)
_approval_waiter = None
_status_publisher = None
_WIN_PATH = re.compile(r"[A-Za-z]:\\[^\s\"']+")
_UNIX_PATH = re.compile(r"(?<![A-Za-z0-9_])/(?:[\w.-]+/)+[\w.-]*")

FILE_OPS = r"""
import base64, pathlib, sys
root = pathlib.Path("/workspace").resolve()
op, rel = sys.argv[1], sys.argv[2]
target = (root / rel).resolve()
try:
    target.relative_to(root)
except ValueError:
    sys.stderr.write(
        "Absolute paths are not allowed. Please use a relative path inside "
        "/workspace (e.g., 'test.py' or './src/test.py').\n"
    )
    raise SystemExit(1)
if op == "read":
    raw_limit = sys.argv[3] if len(sys.argv) > 3 else ""
    limit = int(raw_limit) if raw_limit.isdigit() else 0
    lines = target.read_text(encoding="utf-8").splitlines()
    if limit and limit < len(lines):
        lines = lines[:limit] + ["... (%d more lines)" % (len(lines) - limit)]
    sys.stdout.write("\n".join(lines))
elif op == "write":
    data = base64.b64decode(sys.argv[3])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    sys.stdout.write("Wrote %d bytes to %s" % (len(data), rel))
elif op == "edit":
    old = base64.b64decode(sys.argv[3])
    new = base64.b64decode(sys.argv[4])
    content = target.read_bytes()
    if old not in content:
        sys.stderr.write("Text not found in %s\n" % rel)
        raise SystemExit(1)
    target.write_bytes(content.replace(old, new, 1))
    sys.stdout.write("Edited %s" % rel)
else:
    sys.stderr.write("unknown file op\n")
    raise SystemExit(1)
"""


def get_sandbox() -> SandboxManager:
    global _sandbox
    if _sandbox is None:
        _sandbox = SandboxManager(repo_root=WORKDIR)
    return _sandbox


def set_task_id(task_id: int):
    """Bind task_id to this thread. Call this inside the agent thread."""
    return _task_id.set(task_id)


def reset_task_id(token) -> None:
    _task_id.reset(token)


def set_approval_waiter(waiter) -> None:
    """waiter(task_id, action, args) -> True, False, or None on timeout."""
    global _approval_waiter
    _approval_waiter = waiter


def set_status_publisher(publisher) -> None:
    """publisher(payload: dict) -> None. Wired to WebSocket by main."""
    global _status_publisher
    _status_publisher = publisher


def tool_error(exc: BaseException) -> str:
    """Error text safe to send back as a model tool_result."""
    text = str(exc)
    if "Absolute paths are not allowed" in text:
        if text.startswith("Error:"):
            return text
        return "Error: " + text
    secrets = []
    try:
        secrets.append(str(WORKDIR))
        secrets.append(str(WORKDIR.resolve()))
    except OSError:
        pass
    try:
        secrets.append(str(Path.home()))
    except OSError:
        pass
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            text = text.replace(secret, "[workspace]")
    text = _WIN_PATH.sub("[path]", text)
    text = _UNIX_PATH.sub("[path]", text)
    text = re.sub(r"\[workspace\][\\/][^\s\"']*", "[workspace]", text)
    text = " ".join(text.split())
    if not text:
        text = "tool failed"
    if text.startswith("Error:"):
        return text
    return "Error: " + text


def publish_status(status: str, message: str, **extra) -> None:
    publisher = _status_publisher
    task_id = _task_id.get()
    if publisher is None or task_id is None:
        return
    payload = {"status": status, "task_id": task_id, "message": message}
    payload.update(extra)
    try:
        publisher(payload)
    except Exception:
        return


def wait_for_approval(action: str, args_preview: str):
    """Return an error string to block the tool, or None to run it.

    With no waiter registered, CLI and tests run the tool directly.
    """
    waiter = _approval_waiter
    task_id = _task_id.get()
    if waiter is None or task_id is None:
        return None
    try:
        decision = waiter(task_id, action, args_preview)
    except Exception as exc:
        return tool_error(exc)
    if decision is None:
        return "Approval timed out, action denied"
    if not decision:
        return "Error: %s rejected" % action
    return None


def require_bound_sandbox() -> str:
    """Return the active container name for the single in-progress bound task."""
    tasks_dir = WORKDIR / ".tasks"
    bound = []
    if tasks_dir.is_dir():
        for path in sorted(tasks_dir.glob("task_*.json")):
            try:
                task = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(task, dict) and str(task.get("worktree") or "").strip():
                bound.append(task)
    wanted = _task_id.get()
    if wanted is not None:
        current = [
            task for task in bound
            if task.get("id") == wanted and task.get("status") == "in_progress"
        ]
    else:
        current = [task for task in bound if task.get("status") == "in_progress"]
    if len(current) != 1:
        raise RuntimeError("Error: current task has no bound sandbox")
    task = current[0]
    index_path = WORKDIR / ".worktrees" / "index.json"
    if not index_path.is_file():
        raise RuntimeError("Error: current task has no bound sandbox")
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Error: current task has no bound sandbox") from exc
    match = None
    for item in data.get("sandboxes", []):
        if item.get("task_id") == task.get("id") and item.get("name") == task.get("worktree"):
            match = item
    if not match or match.get("status") != "active" or not match.get("container_name"):
        raise RuntimeError("Error: current task has no bound sandbox")
    return match["container_name"]


_PATH_HINT = (
    "Absolute paths are not allowed. Please use a relative path inside "
    "/workspace (e.g., 'test.py' or './src/test.py')."
)


def workspace_rel(path: str) -> str:
    if not path or path.startswith(("/", "\\")) or (len(path) >= 2 and path[1] == ":"):
        raise ValueError(_PATH_HINT)
    parts = Path(path).parts
    if Path(path).is_absolute() or ".." in parts:
        raise ValueError(_PATH_HINT)
    return path.replace("\\", "/")


def format_exec(result: dict) -> str:
    if result.get("timed_out"):
        return result.get("message") or "Error: Timeout (300s)"
    if result.get("exit_code") not in (0, None):
        detail = (result.get("stderr") or result.get("stdout") or "").strip()
        return f"Error: {detail or 'command failed'}"
    out = result.get("stdout") or ""
    text = out[:50000]
    return text if text else "(no output)"


def run_in_sandbox(command: str) -> str:
    try:
        sandbox_id = require_bound_sandbox()
    except RuntimeError as exc:
        return tool_error(exc)
    try:
        result = get_sandbox().execute(sandbox_id, command)
    except (SandboxError, ValueError) as exc:
        return tool_error(exc)
    return format_exec(result)


def run_file_op(op: str, path: str, *args: str) -> str:
    try:
        rel = workspace_rel(path)
        sandbox_id = require_bound_sandbox()
    except (RuntimeError, ValueError) as exc:
        return tool_error(exc)
    command = "python -c " + shlex.quote(FILE_OPS) + " " + shlex.quote(op) + " " + shlex.quote(rel)
    for arg in args:
        command += " " + shlex.quote(arg)
    try:
        result = get_sandbox().execute(sandbox_id, command)
    except (SandboxError, ValueError) as exc:
        return tool_error(exc)
    return format_exec(result)


def _ready_for_tool(path: str = "") -> str:
    """Return an error string when the tool must not run, else ""."""
    try:
        if path:
            workspace_rel(path)
        require_bound_sandbox()
    except (RuntimeError, ValueError) as exc:
        return tool_error(exc)
    return ""


_READONLY_BINARIES = frozenset({
    "pwd", "ls", "cat", "head", "tail", "grep", "find",
    "getent", "env", "echo",
})
_VERSION_BINARIES = frozenset({"python3", "python", "pip", "pip3"})
_FIND_WRITERS = frozenset({
    "-exec", "-execdir", "-ok", "-okdir", "-delete",
    "-fls", "-fprint", "-fprint0", "-fprintf",
})


def _split_unquoted(text: str, sep: str):
    """Split on sep outside quotes. None means the quotes are unfinished."""
    parts = []
    buf = []
    quote = ""
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(text):
                buf.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == sep:
            part = "".join(buf).strip()
            if not part:
                return None
            parts.append(part)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    if quote:
        return None
    part = "".join(buf).strip()
    if not part:
        return None
    parts.append(part)
    return parts


def _strip_readonly_syntax(segment: str):
    """Drop unquoted 2>/dev/null. None means the segment can change state."""
    out = []
    quote = ""
    i = 0
    n = len(segment)
    while i < n:
        ch = segment[i]
        if quote == "'":
            out.append(ch)
            if ch == "'":
                quote = ""
            i += 1
            continue
        if quote == '"':
            if ch == "$" or ch == "`":
                return None
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(segment[i + 1])
                i += 2
                continue
            if ch == '"':
                quote = ""
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "\\":
            return None
        redirect_at = _allowed_redirect_end(segment, i)
        if redirect_at is not None:
            out.append(" ")
            i = redirect_at
            continue
        if ch in ";&`<>$" or segment.startswith("$(", i):
            return None
        out.append(ch)
        i += 1
    if quote:
        return None
    return "".join(out)


_PYTHON = frozenset({"python", "python3"})
_PYTHON_FLAGS = frozenset({"-u", "-B", "-E", "-s", "-I", "-q"})
_MUTATING = re.compile(
    r"(?<![\w])(?:pip3?\s+install|apt-get|apt\s+install|wget|curl|rm)(?![\w])"
)
_ALLOWED_REDIRECTS = ("2>/dev/null", ">/dev/null", "1>/dev/null", "2>&1")


def _allowed_redirect_end(segment: str, index: int):
    """Return the index after a non-writing redirect, or None."""
    for token in _ALLOWED_REDIRECTS:
        if not segment.startswith(token, index):
            continue
        before_ok = index == 0 or segment[index - 1].isspace()
        after = index + len(token)
        after_ok = after == len(segment) or segment[after].isspace()
        if before_ok and after_ok:
            return after
    return None


def _split_chain(text: str):
    """Split on ; && || outside quotes. A bare & is still rejected."""
    parts = []
    buf = []
    quote = ""
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < n:
                buf.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if text.startswith("&&", i) or text.startswith("||", i):
            part = "".join(buf).strip()
            if not part:
                return None
            parts.append(part)
            buf = []
            i += 2
            continue
        if text.startswith("2>&1", i) or text.startswith(">&1", i):
            token = "2>&1" if text.startswith("2>&1", i) else ">&1"
            buf.append(token)
            i += len(token)
            continue
        if ch == ";":
            part = "".join(buf).strip()
            if not part:
                return None
            parts.append(part)
            buf = []
            i += 1
            continue
        if ch == "&":
            return None
        buf.append(ch)
        i += 1
    if quote:
        return None
    part = "".join(buf).strip()
    if not part:
        return None
    parts.append(part)
    return parts


def _is_workspace_script(path: str) -> bool:
    """Relative *.py, or a script under /workspace. `..` stays outside."""
    normalized = path.replace("\\", "/")
    if normalized.startswith("/workspace/"):
        rest = normalized[len("/workspace/"):]
    elif normalized.startswith("/") or (len(normalized) >= 2 and normalized[1] == ":"):
        return False
    else:
        rest = normalized
    parts = [part for part in rest.split("/") if part and part != "."]
    if not parts or ".." in parts or not parts[-1].endswith(".py"):
        return False
    return True


def _python_is_local(argv: list) -> bool:
    """True for python3 script.py and python3 -c, not for pip or -m pip."""
    if not argv or argv[0] not in _PYTHON:
        return False
    args = list(argv[1:])
    while args and args[0] in _PYTHON_FLAGS:
        args = args[1:]
    if not args:
        return False
    if args[0] in ("--version", "-V") and len(args) == 1:
        return True
    if args[0] == "-m":
        return len(args) >= 2 and args[1] == "pytest"
    if args[0] == "-c":
        return len(args) >= 2
    return _is_workspace_script(args[0])


_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=[^\n`$;&|<>]*$")
_TEST_SUFFIXES = frozenset({".py", ".json", ".txt"})


def _drop_env_prefix(argv: list) -> list:
    rest = list(argv)
    while rest and _ENV_ASSIGN.match(rest[0]):
        rest = rest[1:]
    return rest


def _cd_is_safe(argv: list) -> bool:
    if len(argv) == 1:
        return True
    if len(argv) != 2:
        return False
    dest = argv[1].replace("\\", "/")
    if dest in (".", "/workspace"):
        return True
    if ".." in [part for part in dest.split("/") if part]:
        return False
    if dest.startswith("/workspace/"):
        return True
    return not dest.startswith("/") and not (len(dest) >= 2 and dest[1] == ":")


def _segment_is_readonly(segment: str) -> bool:
    cleaned = _strip_readonly_syntax(segment)
    if cleaned is None:
        return False
    try:
        argv = shlex.split(cleaned, posix=True)
    except ValueError:
        return False
    argv = _drop_env_prefix(argv)
    if not argv or "/" in argv[0] or "\\" in argv[0] or "=" in argv[0]:
        return False
    cmd = argv[0]
    if cmd == "cd":
        return _cd_is_safe(argv)
    if cmd in _READONLY_BINARIES:
        if cmd == "find" and any(arg in _FIND_WRITERS for arg in argv[1:]):
            return False
        return True
    if cmd in ("pytest",) or (cmd in _PYTHON and _python_is_local(argv)):
        return True
    if cmd in _VERSION_BINARIES:
        return argv[1:] in (["--version"], ["-V"])
    return False


def _write_is_auto(path: str) -> bool:
    """Test artifacts under /workspace do not need an approval dialog."""
    name = path.replace("\\", "/").split("/")[-1]
    if "test_" in name:
        return True
    return Path(name).suffix.lower() in _TEST_SUFFIXES


def _bash_is_readonly(command: str) -> bool:
    """True for read-only probes and local python3 test scripts or -c.

    Segments may be joined by |, ;, &&, or ||. stderr redirects 2>&1 and
    2>/dev/null are ignored. pip install, apt-get, rm, wget, curl, and
    file redirects (>) still require approval.
    """
    text = command.strip()
    if not text or "\n" in text or "\r" in text or "\x00" in text:
        return False
    if _MUTATING.search(text):
        return False
    chains = _split_chain(text)
    if not chains:
        return False
    for chain in chains:
        segments = _split_unquoted(chain, "|")
        if not segments or not all(_segment_is_readonly(segment) for segment in segments):
            return False
    return True


def run_bash(command: str) -> str:
    denied = _ready_for_tool()
    if denied:
        return denied
    if not _bash_is_readonly(command):
        denied = wait_for_approval("bash", command)
        if denied:
            return denied
    return run_in_sandbox(command)


def run_read(path: str, limit: int = None) -> str:
    limit_arg = str(limit) if isinstance(limit, int) and limit > 0 else ""
    return run_file_op("read", path, limit_arg)


def run_write(path: str, content: str) -> str:
    denied = _ready_for_tool(path)
    if denied:
        return denied
    if not _write_is_auto(path):
        preview = content if len(content) <= 200 else content[:200] + " ... (%d bytes)" % len(content)
        denied = wait_for_approval("write_file", path + "\n" + preview)
        if denied:
            return denied
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    return run_file_op("write", path, encoded)


def run_edit(path: str, old_text: str, new_text: str) -> str:
    denied = _ready_for_tool(path)
    if denied:
        return denied
    denied = wait_for_approval("edit_file", path)
    if denied:
        return denied
    old_b64 = base64.b64encode(old_text.encode("utf-8")).decode("ascii")
    new_b64 = base64.b64encode(new_text.encode("utf-8")).decode("ascii")
    return run_file_op("edit", path, old_b64, new_b64)


# -- The dispatch map: {tool_name: handler} --
TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
}

TOOLS = [
    {"name": "bash", "description": "Run a shell command in the bound sandbox /workspace.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read a file under the bound sandbox /workspace.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write a file under the bound sandbox /workspace.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file under the bound sandbox /workspace.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
]


def agent_loop(messages: list):
    try:
        while True:
            publish_status("thinking", "model request")
            response = client.messages.create(
                model=MODEL, system=SYSTEM, messages=messages,
                tools=TOOLS, max_tokens=8000,
            )
            messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason != "tool_use":
                publish_status("completed", "task finished")
                return
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    publish_status("tool_call", block.name, action=block.name)
                    handler = TOOL_HANDLERS.get(block.name)
                    try:
                        output = handler(**block.input) if handler else "Unknown tool: %s" % block.name
                    except Exception as exc:
                        output = tool_error(exc)
                    if isinstance(output, str) and output.startswith("Error:"):
                        preview = output if len(output) <= 500 else output[:500] + "..."
                        publish_status("tool_result", preview, action=block.name)
                    else:
                        publish_status("tool_result", block.name, action=block.name)
                    print("> %s: %s" % (block.name, output[:200]))
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
            messages.append({"role": "user", "content": results})
    except Exception as exc:
        publish_status("error", tool_error(exc))
        raise


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms02 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
