"""Langfuse hooks that never raise into the agent loop.

The SDK batches spans on a background thread. Callers must not flush.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from utils.logger import get_logger

log = get_logger("langfuse")

try:
    from langfuse import get_client
    from langfuse import observe as _observe
except Exception as exc:  # package missing or broken install
    get_client = None
    _observe = None
    log.info("[langfuse] import skipped: %s" % exc)


def observe(*args, **kwargs):
    """@observe that stays a no-op until both Langfuse keys are set.

    The check happens when the decorated function is defined. core_agent
    loads .env before those definitions, so a restart picks up new keys.
    Skipping the SDK wrapper avoids its "missing public key" log on every call.
    """
    bare = len(args) == 1 and callable(args[0]) and not kwargs

    def passthrough(func):
        return func

    if _observe is None or not tracing_configured():
        if bare:
            return args[0]
        return passthrough
    if bare:
        try:
            return _observe(args[0])
        except Exception as exc:
            log.info("[langfuse] observe skipped: %s" % exc)
            return args[0]

    def decorator(func):
        try:
            return _observe(*args, **kwargs)(func)
        except Exception as exc:
            log.info("[langfuse] observe skipped: %s" % exc)
            return func

    return decorator


def tracing_configured() -> bool:
    if get_client is None:
        return False
    return bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))


def _client():
    if not tracing_configured():
        return None
    try:
        return get_client()
    except Exception as exc:
        log.info("[langfuse] client skipped: %s" % exc)
        return None


def _call(method: str, **kwargs: Any) -> None:
    client = _client()
    if client is None:
        return
    payload = {key: value for key, value in kwargs.items() if value is not None}
    if not payload:
        return
    try:
        getattr(client, method)(**payload)
    except Exception as exc:
        log.info("[langfuse] %s skipped: %s" % (method, exc))


def update_current_trace(**kwargs: Any) -> None:
    _call("update_current_trace", **kwargs)


def update_current_span(**kwargs: Any) -> None:
    _call("update_current_span", **kwargs)


def update_current_generation(**kwargs: Any) -> None:
    _call("update_current_generation", **kwargs)


def trace_value(value: Any, limit: int = 8000) -> Any:
    """Plain JSON-ish copy. Long strings are clipped. The live object is left alone."""
    if hasattr(value, "model_dump") and not isinstance(value, (str, bytes, dict, list)):
        try:
            value = value.model_dump()
        except Exception:
            return _clip(str(value), limit)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, str):
        return _clip(value, limit)
    if isinstance(value, dict):
        return {str(key): trace_value(item, limit) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [trace_value(item, limit) for item in value]
    return _clip(str(value), limit)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "... (%d chars)" % len(text)


def init_client() -> Optional[object]:
    """Create the process-wide client. Missing keys or a bad host do not raise."""
    if get_client is None:
        log.info("[langfuse] tracing disabled, package not importable")
        return None
    if not tracing_configured():
        log.info("[langfuse] tracing disabled, keys not set")
        return None
    try:
        client = get_client()
    except Exception as exc:
        log.info("[langfuse] init skipped: %s" % exc)
        return None
    log.info("[langfuse] client initialized host=%s" % (os.getenv("LANGFUSE_HOST") or "https://cloud.langfuse.com"))
    return client


def flush_client() -> None:
    """Block until the background queue is delivered. Shutdown only."""
    if get_client is None or not tracing_configured():
        return
    try:
        get_client().flush()
        log.info("[langfuse] flushed")
    except Exception as exc:
        log.info("[langfuse] flush skipped: %s" % exc)
