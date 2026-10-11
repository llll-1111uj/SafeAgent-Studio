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
import json
import os
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
    "Act, don't explain."
)

_sandbox = None

FILE_OPS = r"""
import base64, pathlib, sys
root = pathlib.Path("/workspace").resolve()
op, rel = sys.argv[1], sys.argv[2]
target = (root / rel).resolve()
try:
    target.relative_to(root)
except ValueError:
    sys.stderr.write("path escapes /workspace\n")
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


def workspace_rel(path: str) -> str:
    if not path or path.startswith(("/", "\\")) or (len(path) >= 2 and path[1] == ":"):
        raise ValueError("path must stay inside /workspace")
    parts = Path(path).parts
    if Path(path).is_absolute() or ".." in parts:
        raise ValueError(f"path escapes /workspace: {path}")
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
        return str(exc)
    try:
        result = get_sandbox().execute(sandbox_id, command)
    except (SandboxError, ValueError) as exc:
        return f"Error: {exc}"
    return format_exec(result)


def run_file_op(op: str, path: str, *args: str) -> str:
    try:
        rel = workspace_rel(path)
        sandbox_id = require_bound_sandbox()
    except (RuntimeError, ValueError) as exc:
        return f"Error: {exc}" if not str(exc).startswith("Error:") else str(exc)
    command = "python -c " + shlex.quote(FILE_OPS) + " " + shlex.quote(op) + " " + shlex.quote(rel)
    for arg in args:
        command += " " + shlex.quote(arg)
    try:
        result = get_sandbox().execute(sandbox_id, command)
    except (SandboxError, ValueError) as exc:
        return f"Error: {exc}"
    return format_exec(result)


def run_bash(command: str) -> str:
    return run_in_sandbox(command)


def run_read(path: str, limit: int = None) -> str:
    limit_arg = str(limit) if isinstance(limit, int) and limit > 0 else ""
    return run_file_op("read", path, limit_arg)


def run_write(path: str, content: str) -> str:
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    return run_file_op("write", path, encoded)


def run_edit(path: str, old_text: str, new_text: str) -> str:
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
    while True:
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return
        results = []
        for block in response.content:
            if block.type == "tool_use":
                handler = TOOL_HANDLERS.get(block.name)
                output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                print(f"> {block.name}: {output[:200]}")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
        messages.append({"role": "user", "content": results})


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
