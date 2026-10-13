"""FastAPI entry: WebSocket status feed and HITL approval.

Agent loops run on their own threads. contextvars are set inside that
thread. Approval waits on a per-task threading.Event so the event loop
stays free to accept /api/approve.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent
AGENTS_DIR = ROOT / "agents"
os.chdir(ROOT)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import s02_tool_use as s02
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from reaper import SandboxReaper
from sandbox_manager import SandboxError
from session_memory import SessionMemory, estimate_tokens

APPROVAL_TIMEOUT_SECONDS = 300.0
APPROVAL_DENIED = "Approval timed out, action denied"


def _sandbox_name(task_id: int) -> str:
    return "agent-%s" % task_id


class ApproveRequest(BaseModel):
    task_id: int = Field(..., ge=1)
    approved: bool
    keep_sandbox: bool = False


class RunRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    session_id: Optional[str] = None


class Pending:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.approved = False


class ConnectionHub:
    """Fan out JSON events to the sockets subscribed to one task_id."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: dict[int, set[WebSocket]] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def add(self, task_id: int, websocket: WebSocket) -> None:
        with self._lock:
            self._clients.setdefault(task_id, set()).add(websocket)

    def discard(self, task_id: int, websocket: WebSocket) -> None:
        with self._lock:
            group = self._clients.get(task_id)
            if not group:
                return
            group.discard(websocket)
            if not group:
                self._clients.pop(task_id, None)

    def publish(self, task_id: int, payload: dict) -> None:
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        future = asyncio.run_coroutine_threadsafe(self._fanout(task_id, dict(payload)), loop)
        try:
            future.result(timeout=5)
        except Exception as exc:
            print("[ws] publish failed task_id=%s: %s" % (task_id, exc), flush=True)

    async def _fanout(self, task_id: int, payload: dict) -> None:
        with self._lock:
            targets = list(self._clients.get(task_id, ()))
        stale: list[WebSocket] = []
        for websocket in targets:
            try:
                await websocket.send_json(payload)
            except Exception:
                stale.append(websocket)
        for websocket in stale:
            self.discard(task_id, websocket)


class ApprovalBroker:
    """One in-flight approval per task_id."""

    def __init__(self, hub: ConnectionHub, timeout: float = APPROVAL_TIMEOUT_SECONDS) -> None:
        self._hub = hub
        self._timeout = timeout
        self._lock = threading.Lock()
        self._pending: dict[int, Pending] = {}

    def wait(self, task_id: int, action: str, args_preview: str):
        pending = Pending()
        with self._lock:
            if task_id in self._pending:
                raise RuntimeError("task already has a pending approval")
            self._pending[task_id] = pending
        shown = args_preview if len(args_preview) <= 500 else args_preview[:500] + "..."
        headline = shown.strip().splitlines()[0].strip() if shown.strip() else action
        print(
            "[hitl] waiting for approval task_id=%s action=%s timeout=%.0fs"
            % (task_id, action, self._timeout),
            flush=True,
        )
        self._hub.publish(task_id, {
            "status": "pending_approval",
            "task_id": task_id,
            "action": action,
            "args": shown,
            "message": "需要您授权执行 %s" % headline,
            "ui": "approval_dialog",
        })
        try:
            signaled = pending.event.wait(self._timeout)
        finally:
            with self._lock:
                if self._pending.get(task_id) is pending:
                    self._pending.pop(task_id, None)
        if not signaled:
            print("[hitl] approval timed out task_id=%s action=%s" % (task_id, action), flush=True)
            self._hub.publish(task_id, {
                "status": "timeout",
                "task_id": task_id,
                "action": action,
                "message": APPROVAL_DENIED,
            })
            _mark_approval_timeout(task_id)
            return None
        return pending.approved

    def decide(self, task_id: int, approved: bool) -> bool:
        with self._lock:
            pending = self._pending.get(task_id)
            if pending is None:
                return False
            pending.approved = bool(approved)
            pending.event.set()
            return True


hub = ConnectionHub()
broker = ApprovalBroker(hub)
_runs_lock = threading.Lock()
_running: set[int] = set()
_keep_tasks: set[int] = set()
_reaper: Optional[SandboxReaper] = None
_sessions = SessionMemory()
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")


def _task_path(task_id: int) -> Path:
    return ROOT / ".tasks" / ("task_%s.json" % task_id)


def _load_task(task_id: int) -> Optional[dict]:
    path = _task_path(task_id)
    if not path.is_file():
        return None
    task = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(task, dict):
        raise HTTPException(status_code=400, detail="task file is corrupt")
    return task


def _ensure_task_file(task_id: int) -> dict:
    """Create a pending task file when /run is called for a new id."""
    task = _load_task(task_id)
    if task is not None:
        return task
    path = _task_path(task_id)
    task = {
        "id": task_id,
        "subject": "task %s" % task_id,
        "description": "",
        "status": "pending",
        "owner": "",
        "worktree": "",
        "blockedBy": [],
        "updated_at": time.time(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(task, indent=2, ensure_ascii=False), encoding="utf-8")
    print("[api] created task file %s" % path, flush=True)
    return task


def _publish_status(payload: dict) -> None:
    task_id = payload.get("task_id")
    if isinstance(task_id, int):
        hub.publish(task_id, payload)


def _update_task(task_id: int, **fields) -> dict:
    path = _task_path(task_id)
    task = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(task, dict):
        raise SandboxError("task file must be a JSON object")
    task.update(fields)
    task["updated_at"] = time.time()
    path.write_text(json.dumps(task, indent=2, ensure_ascii=False), encoding="utf-8")
    return task


def _active_sandbox(task_id: int, name: str) -> Optional[dict]:
    index_path = ROOT / ".worktrees" / "index.json"
    if not name or not index_path.is_file():
        return None
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print("[sandbox] index read failed task_id=%s: %s" % (task_id, exc), flush=True)
        return None
    found = None
    for item in data.get("sandboxes", []):
        if item.get("task_id") == task_id and item.get("name") == name and item.get("status") == "active":
            found = item
    return found


def _bind_sandbox(task_id: int, saved: dict) -> dict:
    """Point the task at the sandbox s02 looks up: in_progress plus worktree name."""
    name = str(saved.get("name") or "")
    bound = _update_task(
        task_id,
        status="in_progress",
        worktree=name,
        sandbox={
            "name": name,
            "container_name": saved.get("container_name", ""),
            "container_id": saved.get("container_id", ""),
            "status": saved.get("status", ""),
        },
        error="",
    )
    print(
        "[sandbox] task bound task_id=%s status=%s worktree=%r container_name=%s"
        % (task_id, bound.get("status"), bound.get("worktree"), saved.get("container_name")),
        flush=True,
    )
    return bound


def _mark_approval_timeout(task_id: int) -> None:
    """Record the denial. The agent thread keeps running and can choose another action."""
    try:
        _update_task(task_id, status="timeout", error=APPROVAL_DENIED)
    except (OSError, json.JSONDecodeError, SandboxError) as exc:
        print("[hitl] failed to mark task %s timeout: %s" % (task_id, exc), flush=True)


def _mark_task_failed(task_id: int, error: str) -> None:
    try:
        task = _update_task(task_id, status="failed", worktree="", error=error)
    except (OSError, json.JSONDecodeError, SandboxError) as exc:
        print("[sandbox] failed to mark task %s failed: %s" % (task_id, exc), flush=True)
        return
    print(
        "[sandbox] task marked failed task_id=%s status=%s worktree=%r"
        % (task_id, task.get("status"), task.get("worktree")),
        flush=True,
    )


def _ensure_sandbox(task_id: int) -> bool:
    """Create and bind a sandbox. Return False when the agent must not start."""
    task = _load_task(task_id) or {}
    bound_name = str(task.get("worktree") or "").strip()
    if task.get("status") == "in_progress" and bound_name:
        existing = _active_sandbox(task_id, bound_name)
        if existing:
            print(
                "[sandbox] reuse active sandbox task_id=%s name=%s container_name=%s"
                % (task_id, bound_name, existing.get("container_name")),
                flush=True,
            )
            return True
    sandbox_name = _sandbox_name(task_id)
    print("[sandbox] create_sandbox before task_id=%s name=%s" % (task_id, sandbox_name), flush=True)
    try:
        saved = s02.get_sandbox().create_sandbox(sandbox_name, task_id)
    except SandboxError as exc:
        print("[sandbox] create_sandbox error task_id=%s: %s" % (task_id, exc), flush=True)
        _mark_task_failed(task_id, str(exc))
        s02.publish_status("error", s02.tool_error(exc))
        return False
    print(
        "[sandbox] create_sandbox after task_id=%s return=%s"
        % (task_id, json.dumps(saved, ensure_ascii=False)),
        flush=True,
    )
    try:
        _bind_sandbox(task_id, saved)
    except (OSError, json.JSONDecodeError, SandboxError) as exc:
        print("[sandbox] bind error task_id=%s: %s" % (task_id, exc), flush=True)
        _mark_task_failed(task_id, str(exc))
        s02.publish_status("error", s02.tool_error(exc))
        return False
    return True


def _assistant_stdout(messages: list) -> str:
    if not messages or not isinstance(messages[-1], dict):
        return ""
    content = messages[-1].get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        if text:
            parts.append(str(text))
    return "\n".join(parts)


def _container_for_task(task_id: int) -> str:
    task = _load_task(task_id) or {}
    sandbox = task.get("sandbox")
    if isinstance(sandbox, dict):
        name = str(sandbox.get("container_name") or "").strip()
        if name:
            return name
    index_path = ROOT / ".worktrees" / "index.json"
    if not index_path.is_file():
        return ""
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    found = ""
    for item in data.get("sandboxes", []):
        if item.get("task_id") != task_id:
            continue
        if item.get("status") not in {"creating", "active", "kept", "failed"}:
            continue
        container = str(item.get("container_name") or "").strip()
        if container:
            found = container
    return found


def _should_keep_sandbox(task_id: int) -> bool:
    if task_id in _keep_tasks:
        return True
    task = _load_task(task_id) or {}
    return task.get("keep") is True


def _release_sandbox(task_id: int) -> None:
    """Destroy the lane after the terminal status has already been pushed.

    keep=True, or a HITL choice to retain the sandbox, stops the container
    and keeps the volumes for 24 hours. Otherwise the container and both
    volumes are removed immediately.
    """
    container = _container_for_task(task_id)
    try:
        if not container:
            return
        manager = s02.get_sandbox()
        if _should_keep_sandbox(task_id):
            saved = manager.keep_sandbox(container)
            print(
                "[sandbox] kept task_id=%s container=%s until=%s"
                % (task_id, container, saved.get("volume_retain_until")),
                flush=True,
            )
            return
        manager.remove_sandbox(container, complete_task=False)
        print("[sandbox] removed task_id=%s container=%s" % (task_id, container), flush=True)
    except SandboxError as exc:
        print("[sandbox] release failed task_id=%s container=%s: %s" % (task_id, container, exc), flush=True)
    finally:
        _keep_tasks.discard(task_id)


def _session_id_of(value: Optional[str]) -> str:
    text = (value or "").strip()
    if _SESSION_ID_RE.fullmatch(text):
        return text
    return uuid.uuid4().hex


def _summarize_turns(older: list) -> str:
    """Ask the model to fold older session turns into one short summary."""
    lines = []
    for item in older:
        role = item.get("role") or "user"
        content = item.get("content")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        lines.append("%s: %s" % (role, content))
    prompt = (
        "请把下面的对话压缩成一段中文摘要，保留用户目标、关键结论、文件名和未完成事项。不要寒暄。\n\n"
        + "\n".join(lines)
    )
    response = s02.client.messages.create(
        model=s02.MODEL,
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )
    text = s02._final_text(response.content).strip()
    return text or "此前对话已压缩。"


def _agent_thread(task_id: int, prompt: str, session_id: str) -> None:
    # contextvars do not cross into a new thread. Set the id here.
    token = s02.set_task_id(task_id)
    messages = _sessions.messages_for(session_id, prompt, _summarize_turns)
    answer = ""
    try:
        _update_task(task_id, stdout="", error="", session_id=session_id)
        if not _ensure_sandbox(task_id):
            answer = "上一轮没有完成：沙箱未能创建。"
            return
        s02.agent_loop(messages)
        answer = _assistant_stdout(messages)
        _update_task(task_id, status="completed", stdout=answer, error="")
    except Exception as exc:
        message = s02.tool_error(exc)
        answer = "上一轮没有完成：" + message
        print("[agent] task %s failed: %s" % (task_id, message), file=sys.stderr, flush=True)
        try:
            _update_task(task_id, status="failed", error=message)
        except (OSError, json.JSONDecodeError, SandboxError) as write_exc:
            print("[agent] failed to record task %s error: %s" % (task_id, write_exc), flush=True)
    finally:
        try:
            stored = _sessions.commit(session_id, prompt, answer, _summarize_turns)
            print(
                "[session] remember session_id=%s turns=%s tokens~%s"
                % (session_id, len(stored), estimate_tokens(stored)),
                flush=True,
            )
        except Exception as exc:
            print("[session] remember failed session_id=%s: %s" % (session_id, exc), flush=True)
        _release_sandbox(task_id)
        s02.reset_task_id(token)
        with _runs_lock:
            _running.discard(task_id)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _reaper
    hub.bind_loop(asyncio.get_running_loop())
    s02.set_approval_waiter(broker.wait)
    s02.set_status_publisher(_publish_status)
    _log_routes()
    try:
        _reaper = SandboxReaper(repo_root=ROOT)
        print("[sandbox] reconciliation", _reaper.startup_reconciliation(), flush=True)
        _reaper.start()
    except SandboxError as exc:
        print("[sandbox] reaper not started: %s" % exc, file=sys.stderr, flush=True)
        _reaper = None
    try:
        yield
    finally:
        if _reaper is not None:
            _reaper.stop()
            _reaper = None


app = FastAPI(title="SafeAgent Studio", lifespan=lifespan)

# Browser calls from the Vite dev server. 3000 is the configured port; 5173 is Vite's default.
_CORS_ORIGINS = [
    "http://127.0.0.1:3000",
    "http://localhost:3000",
    "http://127.0.0.1:5173",
    "http://localhost:5173",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _log_routes() -> None:
    print("[route] registered paths:", flush=True)
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        path = getattr(route, "path", "")
        label = ",".join(sorted(methods)) if methods else "WEBSOCKET"
        print("[route] %s %s" % (label, path), flush=True)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.websocket("/ws/agent")
async def ws_agent(websocket: WebSocket, task_id: int) -> None:
    if task_id < 1:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    hub.add(task_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        hub.discard(task_id, websocket)


@app.post("/api/approve")
def approve(body: ApproveRequest) -> dict:
    if body.keep_sandbox:
        _keep_tasks.add(body.task_id)
        try:
            _update_task(body.task_id, keep=True)
        except (OSError, json.JSONDecodeError, SandboxError) as exc:
            print("[hitl] failed to mark task %s keep: %s" % (body.task_id, exc), flush=True)
    if not broker.decide(body.task_id, body.approved):
        raise HTTPException(status_code=404, detail="no pending approval for this task")
    hub.publish(body.task_id, {
        "status": "approved" if body.approved else "rejected",
        "task_id": body.task_id,
        "message": "approved" if body.approved else "rejected",
    })
    return {"task_id": body.task_id, "approved": body.approved}


@app.get("/api/tasks/{task_id}")
def get_task(task_id: int):
    """Return the finished task only.

    Live states, including pending_approval, are pushed on
    /ws/agent?task_id=. A pending_approval event has ui=approval_dialog
    and is the signal to open the approval popup.
    """
    if task_id < 1:
        raise HTTPException(status_code=400, detail="invalid task_id")
    try:
        task = _load_task(task_id)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="task file is corrupt") from exc
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    status = str(task.get("status") or "")
    with _runs_lock:
        running = task_id in _running
    if running or status not in ("completed", "failed", "timeout"):
        return JSONResponse(status_code=202, content={
            "task_id": task_id,
            "websocket": "/ws/agent?task_id=%s" % task_id,
        })
    return {
        "task_id": task_id,
        "status": status,
        "stdout": str(task.get("stdout") or ""),
        "error": str(task.get("error") or ""),
    }


@app.post("/api/tasks/{task_id}/run", status_code=202)
def run_task(task_id: int, body: RunRequest) -> dict:
    """Start one agent thread.

    The task file is already in_progress once a sandbox is bound, so that
    field cannot mean "an agent thread exists". The running set is the
    guard: a second /run for the same task_id returns 409.
    """
    if task_id < 1:
        raise HTTPException(status_code=400, detail="invalid task_id")
    try:
        task = _ensure_task_file(task_id)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="task file is corrupt") from exc
    if task.get("status") == "completed":
        raise HTTPException(status_code=409, detail="task is completed")
    with _runs_lock:
        if task_id in _running:
            raise HTTPException(status_code=409, detail="task agent is already running")
        _running.add(task_id)
    session_id = _session_id_of(body.session_id)
    try:
        threading.Thread(
            target=_agent_thread,
            args=(task_id, body.prompt, session_id),
            name="agent-%s" % task_id,
            daemon=True,
        ).start()
    except Exception:
        with _runs_lock:
            _running.discard(task_id)
        raise
    return {"task_id": task_id, "status": "started", "session_id": session_id}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
