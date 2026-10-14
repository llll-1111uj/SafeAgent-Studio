#!/usr/bin/env python3
"""
agent_loop.py - The Agent Loop

The entire secret of an AI coding agent in one pattern:

    while stop_reason == "tool_use":
        response = LLM(messages, tools)
        execute tools
        append results

    +----------+      +-------+      +---------+
    |   User   | ---> |  LLM  | ---> |  Tool   |
    |  prompt  |      |       |      | execute |
    +----------+      +---+---+      +----+----+
                          ^               |
                          |   tool_result |
                          +---------------+
                          (loop continues)

This is the core loop: feed tool results back to the model
until the model decides to stop. Production agents layer
policy, hooks, and lifecycle controls on top.
"""

import sys
from pathlib import Path as _RepoPath
_REPO_ROOT = _RepoPath(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
from utils.logger import get_logger
log = get_logger('agent_loop')

import os
import re
import subprocess

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

SYSTEM = (
    "最高优先级：当用户明确要求执行命令、安装依赖、查看目录或验证文件时，"
    "你必须调用 bash 工具获取真实结果。"
    "绝对禁止在未调用工具的情况下，在最终答案中伪造终端输出、错误堆栈或命令执行结果"
    "（如 $ 提示符、ERROR: 等前缀）。"
    "如果你不调用工具，系统将判定任务失败。"
    f"You are a coding agent at {os.getcwd()}. Use bash to solve tasks. Act, don't explain."
)

_COMMAND_ACTION = re.compile(r"执行|运行|安装|\binstall\b|\bpwd\b|\bls\b", re.IGNORECASE)
_TOOL_NUDGE = "你还没有调用任何工具，请立即调用 bash 执行命令并获取真实结果"


def _latest_user_text(messages: list) -> str:
    """Text of the newest user turn. Tool-result turns are skipped."""
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content
    return ""


def _prompt_requires_tool(messages: list) -> bool:
    """True when the user asked to run, install, or inspect via a real command."""
    return bool(_COMMAND_ACTION.search(_latest_user_text(messages)))

TOOLS = [{
    "name": "bash",
    "description": "Run a shell command.",
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}]


def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=os.getcwd(),
                           capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


# -- The core pattern: a while loop that calls tools until the model stops --
def agent_loop(messages: list):
    system = SYSTEM
    requires_tool = _prompt_requires_tool(messages)
    used_tool = False
    nudged = False
    while True:
        response = client.messages.create(
            model=MODEL, system=system, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        # Append assistant turn
        messages.append({"role": "assistant", "content": response.content})
        # If the model didn't call a tool, we're done — unless the prompt
        # asked for a real command and none has run yet.
        if response.stop_reason != "tool_use":
            if requires_tool and not used_tool:
                if not nudged:
                    nudged = True
                    system = system + "\n" + _TOOL_NUDGE
                    messages.append({"role": "user", "content": _TOOL_NUDGE})
                    log.info("[agent] blocked completion without a tool call")
                    continue
                raise RuntimeError("任务失败：要求执行命令但未调用任何工具")
            return
        # Execute each tool call, collect results
        results = []
        for block in response.content:
            if block.type == "tool_use":
                used_tool = True
                log.info(f"\033[33m$ {block.input['command']}\033[0m")
                output = run_bash(block.input["command"])
                log.info(output[:200])
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": output})
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms01 >> \033[0m")
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
