"""Collapse repeated failed tool calls before the next model request.

The live loop in core_agent calls compact_repeated_failures at the start of
each turn. Consecutive failures of the same tool become one summary, so the
earlier arguments are not sent again.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from utils.logger import get_logger

log = get_logger("context_compact")

_PATH_MARKERS = (
    "路径错误",
    "绝对路径",
    "Absolute paths are not allowed",
    "files must be written under",
)


def compact_repeated_failures(messages: list) -> list:
    """Merge consecutive failures of one tool into a single summary result.

    A run of two or more single-tool failures keeps only the last tool call.
    Its result text becomes "尝试 write_file 失败 3 次，原因均为路径错误" when
    every error in the run is a path error. The messages list is updated in place.
    """
    if len(messages) < 4:
        return messages
    compacted: list = []
    index = 0
    total = len(messages)
    while index < total:
        run = _failure_run(messages, index)
        if len(run) < 2:
            compacted.append(messages[index])
            index += 1
            continue
        last_at = run[-1][2]
        summary = _failure_summary(run[0][0], [item[1] for item in run])
        compacted.append(messages[last_at])
        compacted.append(_with_summary(messages[last_at + 1], summary))
        index = last_at + 2
    if len(compacted) != len(messages):
        log.info(
            "[compact] collapsed repeated tool failures %s -> %s messages"
            % (len(messages), len(compacted))
        )
    messages[:] = compacted
    return messages


def _failure_run(messages: list, start: int) -> list:
    """Consecutive (tool name, error text, assistant index) pairs from start."""
    run = []
    index = start
    total = len(messages)
    while index + 1 < total:
        failed = _single_failed_tool(messages[index], messages[index + 1])
        if failed is None:
            break
        name, text = failed
        if run and name != run[0][0]:
            break
        run.append((name, text, index))
        index += 2
    return run


def _single_failed_tool(assistant: dict, user: dict):
    if not isinstance(assistant, dict) or assistant.get("role") != "assistant":
        return None
    if not isinstance(user, dict) or user.get("role") != "user":
        return None
    uses = [block for block in _blocks(assistant.get("content")) if _block_type(block) == "tool_use"]
    if len(uses) != 1:
        return None
    content = user.get("content")
    if not isinstance(content, list):
        return None
    results = [block for block in content if isinstance(block, dict) and block.get("type") == "tool_result"]
    if len(results) != 1:
        return None
    text = result_text(results[0].get("content"))
    if not text.startswith("Error:"):
        return None
    name = _block_name(uses[0])
    if not name:
        return None
    return name, text


def _with_summary(user: dict, summary: str) -> dict:
    content = []
    for block in user.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            copied = dict(block)
            copied["content"] = summary
            content.append(copied)
            continue
        content.append(block)
    return {"role": "user", "content": content}


def _failure_summary(name: str, texts: list) -> str:
    if texts and all(_is_path_error(text) for text in texts):
        return "尝试 %s 失败 %d 次，原因均为路径错误" % (name, len(texts))
    return "尝试 %s 失败 %d 次" % (name, len(texts))


def _is_path_error(text: str) -> bool:
    return any(marker in text for marker in _PATH_MARKERS)


def result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("text"):
                parts.append(str(block.get("text")))
        return "\n".join(parts)
    return "" if content is None else str(content)


def _blocks(content) -> list:
    if isinstance(content, list):
        return content
    return []


def _block_type(block) -> str:
    if isinstance(block, dict):
        return str(block.get("type") or "")
    return str(getattr(block, "type", "") or "")


def _block_name(block) -> str:
    if isinstance(block, dict):
        return str(block.get("name") or "")
    return str(getattr(block, "name", "") or "")
