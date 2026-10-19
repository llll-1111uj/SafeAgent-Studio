#!/usr/bin/env python3
"""
core_agent.py - Tools

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

import sys
from pathlib import Path as _RepoPath
_REPO_ROOT = _RepoPath(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
from utils.logger import get_logger
log = get_logger('core_agent')

import base64
import contextvars
import json
import os
import re
import shlex
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote

import requests
from anthropic import Anthropic
from dotenv import load_dotenv
from context_compact import compact_repeated_failures
from sandbox_manager import SandboxError, SandboxManager

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

SYSTEM = (
    "【绝对红线】当用户的指令包含执行、运行、安装、查看、读取等动作词时，"
    "你必须调用 bash、write_file 等工具获取真实结果。"
    "绝对禁止在未调用工具的情况下，在最终回答里伪造终端提示符（如 $, >）或错误堆栈（如 ERROR:, Traceback）。"
    "如果用户是在要求解释、讲解、科普，或在问「是什么」，可以直接回答，不必调用工具。"
    f"You are a coding agent at {WORKDIR}. "
    "bash, read_file, write_file, and edit_file run inside the bound sandbox "
    "at /workspace. Refuse them when the current task has no active sandbox. "
    "Act, don't explain. "
    "你处于断网沙箱，禁止尝试任何网络安装（pip install, apt-get）或外网下载。"
    "如果遇到缺失依赖，请使用纯 Python 或本地已有库降级实现。"
    "如果用户询问实时信息（天气、新闻、股票），请优先调用 web_search 工具获取数据，"
    "而不是尝试在沙箱内 curl 或 wget。"
    "工作目录是 /workspace。所有新建或编辑的文件必须放在 outputs/ 子目录下。"
    "请使用相对路径，例如 outputs/test.py。严禁使用绝对路径，严禁使用 cd 命令切换目录。"
)

_sandbox = None
_task_id = contextvars.ContextVar("safeagent_task_id", default=None)
_parent_task_id = contextvars.ContextVar("safeagent_parent_task_id", default=None)
_approval_state = contextvars.ContextVar("safeagent_approval", default="auto")
_SECRET_VALUE = re.compile(
    r"(?i)(api[_-]?key|token|secret)\s*=\s*\S+|sk-[A-Za-z0-9_\-]{8,}"
)
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


def set_parent_task_id(parent_task_id):
    """Remember which artifacts to copy in when the sandbox is created later."""
    return _parent_task_id.set(parent_task_id)


def reset_parent_task_id(token) -> None:
    _parent_task_id.reset(token)


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
        _approval_state.set("auto")
        return None
    try:
        decision = waiter(task_id, action, args_preview)
    except Exception as exc:
        return tool_error(exc)
    if decision is None:
        _approval_state.set("timeout")
        return "Approval timed out, action denied"
    if not decision:
        _approval_state.set("rejected")
        return "Error: %s rejected" % action
    _approval_state.set("approved")
    return None


def _one_line(text, limit: int = 200) -> str:
    """Single log line with secret values removed."""
    flat = " ".join(str(text or "").split())
    flat = _SECRET_VALUE.sub("[redacted]", flat)
    if len(flat) <= limit:
        return flat
    return flat[:limit] + "..."


def _args_summary(name: str, tool_input) -> str:
    """Short tool arguments. File bodies stay out of the trace."""
    if not isinstance(tool_input, dict):
        return _one_line(tool_input)
    if name in ("write_file", "edit_file", "read_file"):
        return _one_line(tool_input.get("path") or "")
    if name == "bash":
        return _one_line(tool_input.get("command") or "")
    if name == "web_search":
        return _one_line(tool_input.get("query") or "")
    return _one_line(json.dumps(tool_input, ensure_ascii=False))


def _append_tool_trace(name: str, args: str, approval: str, result: str, elapsed_ms: int) -> None:
    """One JSON object per tool call, keyed by task_id. Not app.log."""
    task_id = _task_id.get()
    if task_id is None:
        return
    path = WORKDIR / ".worktrees" / "traces" / ("%s.jsonl" % task_id)
    row = {
        "task_id": int(task_id),
        "tool": name,
        "args": args,
        "approval": approval,
        "result": result,
        "ms": int(elapsed_ms),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:
        log.info("[tool] trace write failed task_id=%s: %s" % (task_id, exc))


def _log_tool_step(name: str, output: str, approval: str, result: str, elapsed_ms: int) -> None:
    task_id = _task_id.get()
    line = "[tool] task_id=%s tool=%s result=%s approval=%s ms=%s output=%s" % (
        task_id, name, result, approval, int(elapsed_ms), _one_line(output),
    )
    if result == "error":
        log.warning(line)
        return
    log.info(line)


def _load_task_record(task_id: int) -> dict:
    path = WORKDIR / ".tasks" / ("task_%s.json" % task_id)
    if not path.is_file():
        return {}
    try:
        task = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return task if isinstance(task, dict) else {}


def _container_named(task_id: int, worktree: str) -> str:
    index_path = WORKDIR / ".worktrees" / "index.json"
    if not worktree or not index_path.is_file():
        return ""
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    for item in data.get("sandboxes", []):
        if item.get("task_id") == task_id and item.get("name") == worktree and item.get("status") == "active":
            return str(item.get("container_name") or "")
    return ""


def _parent_for_lazy_create(task_id: int):
    parent = _parent_task_id.get()
    if not isinstance(parent, int) or isinstance(parent, bool):
        stored = _load_task_record(task_id).get("parent_task_id")
        parent = stored if isinstance(stored, int) and not isinstance(stored, bool) else None
    if not isinstance(parent, int) or isinstance(parent, bool) or parent < 1 or parent == task_id:
        return None
    return parent


def _lazy_create_sandbox(task_id: int) -> str:
    """Create the lane on the first sandbox tool call. Not used for web_search."""
    name = "agent-%s" % task_id
    parent = _parent_for_lazy_create(task_id)
    log.info("[sandbox] lazy create task_id=%s name=%s parent_task_id=%s" % (task_id, name, parent))
    try:
        saved = get_sandbox().create_sandbox(name, task_id, parent_task_id=parent)
    except (SandboxError, ValueError) as exc:
        log.info("[sandbox] lazy create failed task_id=%s: %s" % (task_id, exc))
        raise RuntimeError(tool_error(exc)) from exc
    container = str(saved.get("container_name") or "")
    if not container:
        raise RuntimeError("Error: current task has no bound sandbox")
    log.info("[sandbox] lazy create after task_id=%s container=%s" % (task_id, container))
    return container


_lazy_ready = {}
_lazy_lock = threading.Lock()


def require_bound_sandbox() -> str:
    """Return the active container, creating it the first time this task needs one.

    With no task_id, only an already bound in-progress task is accepted.
    That keeps CLI tests from starting a container by accident.
    """
    task_id = _task_id.get()
    if task_id is not None:
        with _lazy_lock:
            cached = _lazy_ready.get(task_id)
            if cached:
                return cached
            task = _load_task_record(task_id)
            container = _container_named(task_id, str(task.get("worktree") or "").strip())
            if not container:
                container = _lazy_create_sandbox(task_id)
            _lazy_ready[task_id] = container
            return container
    tasks_dir = WORKDIR / ".tasks"
    current = []
    if tasks_dir.is_dir():
        for path in sorted(tasks_dir.glob("task_*.json")):
            try:
                task = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(task, dict) and task.get("status") == "in_progress" and str(task.get("worktree") or "").strip():
                current.append(task)
    if len(current) != 1:
        raise RuntimeError("Error: current task has no bound sandbox")
    task = current[0]
    container = _container_named(task.get("id"), str(task.get("worktree") or ""))
    if not container:
        raise RuntimeError("Error: current task has no bound sandbox")
    return container


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
    """Return an error string when the path is illegal, else "".

    The sandbox is created later, on the first real exec, so a rejected
    approval does not start a container.
    """
    try:
        if path:
            workspace_rel(path)
    except ValueError as exc:
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


_ABSOLUTE_PATH_ERROR = "Error: 不允许使用绝对路径。请使用相对路径，例如 outputs/test.py。"
_OUTPUTS_PATH_ERROR = (
    "Error: 路径错误。请使用相对路径重试，示例：write_file('outputs/test.py', ...)。"
    "文件必须保存在 outputs/ 目录下。"
)
_ABORT_MESSAGE = "无法完成，请检查工具参数或路径规则"
_TOOL_FAIL_LIMIT = 3


def _is_absolute_path(path: str) -> bool:
    if not path or path.startswith(("/", "\\")):
        return bool(path)
    if len(path) >= 2 and path[1] == ":":
        return True
    return Path(path).is_absolute()


def _check_output_path(path: str) -> str:
    """Error text for write_file and edit_file. Empty when the path is allowed."""
    if _is_absolute_path(path or ""):
        return _ABSOLUTE_PATH_ERROR
    try:
        rel = workspace_rel(path)
    except ValueError:
        return _OUTPUTS_PATH_ERROR
    parts = [part for part in rel.replace("\\", "/").split("/") if part and part != "."]
    if ".." in parts or len(parts) < 2 or parts[0] != "outputs":
        return _OUTPUTS_PATH_ERROR
    return ""


def _tool_call_failed(output) -> bool:
    return isinstance(output, str) and (
        output.startswith("Error:") or output.startswith("Approval timed out")
    )


def run_write(path: str, content: str) -> str:
    denied = _check_output_path(path)
    if denied:
        return denied
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
    denied = _check_output_path(path)
    if denied:
        return denied
    denied = _ready_for_tool(path)
    if denied:
        return denied
    denied = wait_for_approval("edit_file", path)
    if denied:
        return denied
    old_b64 = base64.b64encode(old_text.encode("utf-8")).decode("ascii")
    new_b64 = base64.b64encode(new_text.encode("utf-8")).decode("ascii")
    return run_file_op("edit", path, old_b64, new_b64)


_WEATHER_HINT = re.compile(r"天气|气温|温度|weather|forecast", re.I)
_WEATHER_FILLER = re.compile(
    r"今天|明天|现在|怎么样|如何|怎样|查询|的|天气|气温|温度|weather|forecast|what(?:'s| is)?|\bin\b|\bthe\b",
    re.I,
)


_SEARCH_ATTEMPTS = 2
_SEARCH_BACKOFF = 0.5
_SEARCH_UNAVAILABLE = (
    "Error: Web search service is unavailable. "
    "Please try to use local tools to find the answer."
)
_SEARCH_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _search_get(url, params=None, user_agent=None):
    """GET on the host with a short timeout and exponential backoff.

    The sandbox has no network. Two attempts, 5 seconds each. The wait
    between them starts at 0.5s and doubles, so a dead host fails quickly.
    """
    delay = _SEARCH_BACKOFF
    last = None
    for attempt in range(_SEARCH_ATTEMPTS):
        try:
            response = requests.get(
                url,
                params=params,
                timeout=5,
                allow_redirects=True,
                headers={
                    "User-Agent": user_agent or _SEARCH_UA,
                    "Accept-Language": "zh-CN,zh;q=0.9",
                },
            )
            response.raise_for_status()
            return response
        except (requests.Timeout, requests.ConnectionError) as exc:
            last = exc
            if attempt + 1 >= _SEARCH_ATTEMPTS:
                raise
            time.sleep(delay)
            delay *= 2
    raise last


def _weather_city(query: str) -> str:
    cleaned = " ".join(_WEATHER_FILLER.sub(" ", query).split())
    if not cleaned or len(cleaned) > 40:
        return ""
    if re.search(r"[/\\?#@]|^\.|\.\.", cleaned):
        return ""
    if not re.fullmatch(r"[\w\u4e00-\u9fff .'-]+", cleaned):
        return ""
    return cleaned


def _local_name(tag):
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _strip_tags(html):
    text = re.sub(r"(?is)<script[\s\S]*?</script>", " ", html or "")
    text = re.sub(r"(?is)<style[\s\S]*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return " ".join(text.split())


def _search_bing(query):
    """Bing China RSS. Reachable from the host without a proxy."""
    response = _search_get(
        "https://cn.bing.com/search",
        {"q": query, "format": "rss", "count": "5"},
    )
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError:
        return ""
    lines = []
    for node in root.iter():
        if _local_name(node.tag) != "item":
            continue
        title = ""
        description = ""
        for child in list(node):
            name = _local_name(child.tag)
            value = " ".join((child.text or "").split())
            if name == "title":
                title = value
            elif name == "description":
                description = _strip_tags(value)
        line = title
        if description and description != title:
            line = "%s — %s" % (title, description[:180])
        if line:
            lines.append(line)
        if len(lines) >= 5:
            break
    return "\n".join(lines)


def _search_baidu(query):
    """Baidu result titles. Used only when Bing China did not answer."""
    response = _search_get(
        "https://www.baidu.com/s",
        {"wd": query, "rn": "5", "ie": "utf-8"},
    )
    titles = re.findall(r"(?is)<h3[^>]*>(.*?)</h3>", response.text or "")
    lines = []
    for raw in titles:
        title = _strip_tags(raw)
        if title and title not in lines:
            lines.append(title)
        if len(lines) >= 5:
            break
    return "\n".join(lines)


def _search_web(query):
    network_failures = 0
    sources = (_search_bing, _search_baidu)
    for search in sources:
        try:
            found = search(query)
        except (requests.Timeout, requests.ConnectionError):
            network_failures += 1
            continue
        except requests.RequestException:
            continue
        if found:
            return found[:4000]
    if network_failures == len(sources):
        return _SEARCH_UNAVAILABLE
    return "Error: no search result for %s" % query


def run_web_search(query: str) -> str:
    """Host-side read-only lookup. Never calls the sandbox or HITL."""
    text = (query or "").strip()
    if not text or len(text) > 200:
        return "Error: query must be 1-200 characters"
    try:
        if _WEATHER_HINT.search(text):
            city = _weather_city(text)
            if not city:
                return "Error: web_search needs a city name, for example 吉林天气"
            body = _search_get(
                "https://wttr.in/%s" % quote(city),
                {"format": "3"},
                user_agent="curl/8.0.0",
            ).text
            line = " ".join(body.split())
            return line or "Error: empty weather response"
        return _search_web(text)
    except (requests.Timeout, requests.ConnectionError):
        return _SEARCH_UNAVAILABLE
    except Exception as exc:
        return tool_error(exc)


# -- The dispatch map: {tool_name: handler} --
TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "web_search": lambda **kw: run_web_search(kw["query"]),
}

TOOLS = [
    {"name": "bash", "description": "Run a shell command in the bound sandbox /workspace.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read a file under the bound sandbox /workspace.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write a file under outputs/ using a relative path, for example outputs/test.py. Absolute paths are rejected.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file under outputs/. Use a relative path such as outputs/test.py.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "web_search", "description": "Look up live weather, news, or stock quotes from the host via Bing China, with Baidu as fallback. The sandbox has no network. For weather, include the city, for example 吉林天气.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
]


def _final_text(content) -> str:
    """Plain text from the assistant turn that ended the loop."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict):
            if block.get("type") not in (None, "text"):
                continue
            text = block.get("text")
        else:
            if getattr(block, "type", "text") != "text":
                continue
            text = getattr(block, "text", None)
        if text:
            parts.append(str(text).strip())
    return "\n\n".join(part for part in parts if part)


_COMMAND_ACTION = re.compile(
    r"执行|运行|安装|查看|读取|\bpip\b|\bls\b|\bpwd\b",
    re.IGNORECASE,
)
_EXPLAIN_TASK = re.compile(r"解释|讲解|是什么|科普")
_TOOL_NUDGE = (
    "系统检测到用户要求执行操作，但你未调用任何工具。"
    "请立刻调用 bash 工具执行真实命令，不要凭记忆编造结果。"
)
_FABRICATION_ERROR = (
    "Error: Agent attempted to fabricate execution output without calling tools. "
    "Task aborted for safety."
)
_TOOL_RETRIES = 2


def _latest_user_text(messages: list) -> str:
    """Text of the newest user turn. Tool-result turns are skipped."""
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content
        if not isinstance(content, list):
            continue
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
                continue
            if not isinstance(block, dict) or block.get("type") not in (None, "text"):
                continue
            text = block.get("text")
            if text:
                parts.append(str(text))
        if parts:
            return "\n".join(parts)
    return ""


def _history_has_tool_call(messages: list) -> bool:
    """True when any assistant turn contains a tool_use block."""
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict):
                kind = block.get("type")
            else:
                kind = getattr(block, "type", None)
            if kind == "tool_use":
                return True
    return False


def _validate_tool_execution(prompt: str, messages: list, retries: int) -> str:
    """Gate a final answer that never called a tool.

    "" allows completion. The nudge continues the loop, at most twice.
    The safety error replaces the fabricated answer.
    Explanatory prompts may finish with no tool call.
    """
    text = prompt or ""
    if _EXPLAIN_TASK.search(text):
        return ""
    if not _COMMAND_ACTION.search(text):
        return ""
    if _history_has_tool_call(messages):
        return ""
    if retries < _TOOL_RETRIES:
        return _TOOL_NUDGE
    return _FABRICATION_ERROR


def agent_loop(messages: list, system_extra=""):
    system = SYSTEM
    extra = system_extra.strip() if isinstance(system_extra, str) else ""
    if extra:
        system = SYSTEM + "\n\n" + extra
    user_prompt = _latest_user_text(messages)
    retries = 0
    fail_tool = ""
    fail_count = 0
    try:
        while True:
            compact_repeated_failures(messages)
            publish_status("thinking", "model request")
            response = client.messages.create(
                model=MODEL, system=system, messages=messages,
                tools=TOOLS, max_tokens=8000,
            )
            usage = getattr(response, "usage", None)
            if usage is not None:
                log.info("[agent] usage task_id=%s input_tokens=%s output_tokens=%s"
                    % (_task_id.get(), getattr(usage, "input_tokens", ""), getattr(usage, "output_tokens", "")))
            messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason != "tool_use":
                verdict = _validate_tool_execution(user_prompt, messages, retries)
                if verdict == _TOOL_NUDGE:
                    retries += 1
                    system = system + "\n" + verdict
                    messages.append({"role": "user", "content": verdict})
                    log.info("[agent] tool execution required retry=%s" % retries)
                    continue
                if verdict:
                    log.info("[agent] hallucination_intercepted")
                    messages[-1] = {"role": "assistant", "content": verdict}
                    publish_status(
                        "completed",
                        "hallucination intercepted",
                        final_answer=verdict,
                    )
                    return
                publish_status(
                    "completed",
                    "task finished",
                    final_answer=_final_text(response.content),
                )
                return
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    publish_status("tool_call", block.name, action=block.name)
                    handler = TOOL_HANDLERS.get(block.name)
                    _approval_state.set("auto")
                    started = time.perf_counter()
                    try:
                        output = handler(**block.input) if handler else "Unknown tool: %s" % block.name
                    except Exception as exc:
                        output = tool_error(exc)
                    elapsed_ms = int((time.perf_counter() - started) * 1000)
                    result = "error" if _tool_call_failed(output) else "ok"
                    approval = _approval_state.get()
                    args_summary = _args_summary(block.name, getattr(block, "input", {}) or {})
                    _append_tool_trace(block.name, args_summary, approval, result, elapsed_ms)
                    _log_tool_step(block.name, output, approval, result, elapsed_ms)
                    preview = _one_line(output, 500)
                    if result == "error":
                        publish_status("tool_result", preview, action=block.name)
                    else:
                        publish_status("tool_result", block.name, action=block.name)
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
                    if _tool_call_failed(output):
                        if block.name == fail_tool:
                            fail_count += 1
                        else:
                            fail_tool = block.name
                            fail_count = 1
                        if fail_count >= _TOOL_FAIL_LIMIT:
                            messages.append({"role": "user", "content": results})
                            messages.append({"role": "assistant", "content": _ABORT_MESSAGE})
                            log.info("[agent] abort consecutive failures tool=%s count=%s"
                                % (fail_tool, fail_count))
                            publish_status(
                                "completed",
                                _ABORT_MESSAGE,
                                final_answer=_ABORT_MESSAGE,
                            )
                            return
                    else:
                        fail_tool = ""
                        fail_count = 0
            messages.append({"role": "user", "content": results})
    except Exception as exc:
        publish_status("error", tool_error(exc))
        raise


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36mcore >> \033[0m")
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
                    log.info(block.text)
        log.info("")
