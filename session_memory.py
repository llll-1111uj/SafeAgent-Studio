"""In-memory short-term memory for one browser session.

Each session_id keeps user prompts and the assistant's final replies.
When that list estimates above 5000 tokens, older turns are replaced by
one summary pair so the next model call does not overflow.
"""

from __future__ import annotations

import threading
from typing import Callable

TOKEN_LIMIT = 5000
_SUMMARY_USER = "此前对话摘要：\n%s"
_SUMMARY_ASSISTANT = "已记录以上摘要，后续回答会延续这些内容。"


def estimate_tokens(messages: list) -> int:
    """Approximate tokens for mixed Chinese and English text.

    A CJK character counts as one token. Other non-space characters count
    as one token per four characters. This overestimates English a little
    and stays close for Chinese, so compression starts before the limit.
    """
    total = 0
    for message in messages:
        total += _text_tokens(_message_text(message))
    return total


def _message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    return str(content)


def _text_tokens(text: str) -> int:
    cjk = 0
    other = 0
    for char in text:
        if "\u4e00" <= char <= "\u9fff":
            cjk += 1
        elif not char.isspace():
            other += 1
    return cjk + (other + 3) // 4


def _clip(text: str, limit: int = 1500) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


class SessionMemory:
    """session_id -> list of {role, content} user/assistant turns."""

    def __init__(self, token_limit: int = TOKEN_LIMIT) -> None:
        self.token_limit = token_limit
        self._lock = threading.Lock()
        self._items: dict[str, list[dict]] = {}

    def messages_for(self, session_id: str, prompt: str, summarize: Callable[[list], str]) -> list:
        """Return prior turns plus this prompt, compressing first if needed."""
        with self._lock:
            history = [dict(item) for item in self._items.get(session_id, [])]
            history = self._compress(history, summarize, force=False)
            pending = history + [{"role": "user", "content": prompt}]
            if estimate_tokens(pending) > self.token_limit and history:
                history = self._compress(history, summarize, force=True)
                pending = history + [{"role": "user", "content": prompt}]
            self._items[session_id] = history
            return pending

    def commit(self, session_id: str, prompt: str, answer: str, summarize: Callable[[list], str]) -> list:
        """Append this turn, then compress the stored list if it is too long."""
        with self._lock:
            history = [dict(item) for item in self._items.get(session_id, [])]
            history.append({"role": "user", "content": prompt})
            history.append({"role": "assistant", "content": answer or ""})
            history = self._compress(history, summarize, force=False)
            self._items[session_id] = history
            return history

    def _compress(self, history: list, summarize: Callable[[list], str], force: bool) -> list:
        if not history:
            return history
        if not force and estimate_tokens(history) <= self.token_limit:
            return history
        if len(history) > 2 and not force:
            older, recent = history[:-2], history[-2:]
        else:
            older, recent = history, []
        try:
            summary = (summarize(older) or "").strip() or "此前对话已压缩。"
        except Exception:
            summary = _clip(" ".join(_message_text(item) for item in older)) or "此前对话已压缩。"
        packed = [
            {"role": "user", "content": _SUMMARY_USER % summary},
            {"role": "assistant", "content": _SUMMARY_ASSISTANT},
        ]
        packed.extend(recent)
        if estimate_tokens(packed) > self.token_limit:
            packed = packed[:2]
            packed[0] = {"role": "user", "content": _clip(packed[0]["content"], 4000)}
        return packed
