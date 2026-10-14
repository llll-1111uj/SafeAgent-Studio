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
    "【绝对红线】当用户的指令包含执行、运行、安装、查看、读取等动作词时，"
    "你必须调用 bash、write_file 等工具获取真实结果。"
    "绝对禁止在未调用工具的情况下，在最终回答里伪造终端提示符（如 $, >）或错误堆栈（如 ERROR:, Traceback）。"
    "如果用户是在要求解释、讲解、科普，或在问「是什么」，可以直接回答，不必调用工具。"
    f"You are a coding agent at {os.getcwd()}. Use bash to solve tasks. Act, don't explain."
)

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
    user_prompt = _latest_user_text(messages)
    retries = 0
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
            return
        # Execute each tool call, collect results
        results = []
        for block in response.content:
            if block.type == "tool_use":
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
