"""前端模拟器：运行时输入任意 Prompt，验证 HITL 连续审批。

先让用户输入 Prompt，再生成唯一 task_id。
先连接 ws://127.0.0.1:8000/ws/agent?task_id={TASK_ID}，
再 POST /api/tasks/{TASK_ID}/run。
收到 pending_approval 时停下来等 y/n，并调用 /api/approve。
收到 completed 或 error 后关闭 WebSocket。
"""

import asyncio
import ctypes
import json
import sys
import time

import requests
import websockets

API = "http://127.0.0.1:8000"
APPROVE_URL = API + "/api/approve"
DONE = {"completed", "error"}
RED = "\033[91m"
RESET = "\033[0m"

if not hasattr(asyncio, "to_thread"):
    async def _to_thread(func, *args, **kwargs):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: func(*args, **kwargs))

    asyncio.to_thread = _to_thread


def enable_windows_ansi():
    if sys.platform != "win32":
        return
    handle = ctypes.windll.kernel32.GetStdHandle(-11)
    mode = ctypes.c_uint()
    if ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        ctypes.windll.kernel32.SetConsoleMode(handle, mode.value | 0x0004)


def read_prompt():
    print("请输入你的 Prompt:", flush=True)
    while True:
        text = input().strip()
        if text:
            return text
        print("Prompt 不能为空，请重新输入:", flush=True)


def start_task(task_id, prompt):
    url = "%s/api/tasks/%s/run" % (API, task_id)
    print("POST %s" % url, flush=True)
    try:
        response = requests.post(url, json={"prompt": prompt}, timeout=30)
    except requests.RequestException as exc:
        print("启动任务失败: %s" % exc, flush=True)
        return False
    print("-> %s %s" % (response.status_code, response.text), flush=True)
    if response.status_code == 409:
        print("任务已在运行，不再重复启动。", flush=True)
        return False
    if response.status_code >= 400:
        print("启动任务失败，HTTP %s" % response.status_code, flush=True)
        return False
    return True


def approve(task_id, accepted):
    body = {"task_id": task_id, "approved": accepted}
    print("POST /api/approve %s" % json.dumps(body, ensure_ascii=False), flush=True)
    response = requests.post(APPROVE_URL, json=body, timeout=30)
    print("-> %s %s" % (response.status_code, response.text), flush=True)
    if response.status_code == 409:
        print("当前没有等待中的审批。", flush=True)
        return
    response.raise_for_status()


def print_event(payload):
    status = str(payload.get("status") or "")
    message = payload.get("message") or ""
    action = payload.get("action") or ""
    if status in ("thinking", "tool_result", "tool_call"):
        suffix = " action=%s" % action if action else ""
        print("[%s] %s%s" % (status, message, suffix), flush=True)
        return
    print(json.dumps(payload, ensure_ascii=False), flush=True)


async def ask(payload):
    action = str(payload.get("action") or "tool")
    args = str(payload.get("args") or "").strip()
    headline = args.splitlines()[0].strip() if args else action
    warning = "需要您授权执行 %s 命令：%s" % (action, headline)
    print("\n%s%s%s" % (RED, warning, RESET), flush=True)
    if args and args != headline:
        print("%s%s%s" % (RED, args, RESET), flush=True)
    while True:
        choice = await asyncio.to_thread(input, "批准请输入 y，拒绝请输入 n: ")
        choice = choice.strip().lower()
        if choice in ("y", "n"):
            return choice == "y"
        print("请输入 y 或 n", flush=True)


async def session(task_id, prompt):
    ws_url = "ws://127.0.0.1:8000/ws/agent?task_id=%s" % task_id
    print("TASK_ID = %s" % task_id, flush=True)
    print("连接 %s" % ws_url, flush=True)
    async with websockets.connect(ws_url) as ws:
        print("WebSocket 已连接", flush=True)
        started = await asyncio.to_thread(start_task, task_id, prompt)
        if not started:
            return
        async for raw in ws:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                print(raw, flush=True)
                continue
            status = payload.get("status")
            if status == "pending_approval":
                while True:
                    accepted = await ask(payload)
                    try:
                        await asyncio.to_thread(approve, task_id, accepted)
                        break
                    except requests.RequestException as exc:
                        print("审批请求失败: %s" % exc, flush=True)
                        print("后端仍在等待，请再输入一次 y 或 n。", flush=True)
                continue
            print_event(payload)
            if status in DONE:
                print("任务结束: %s" % status, flush=True)
                return
    print("WebSocket 已关闭", flush=True)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    enable_windows_ansi()
    prompt = read_prompt()
    task_id = int(time.time() * 1000)
    try:
        asyncio.get_event_loop().run_until_complete(session(task_id, prompt))
    except KeyboardInterrupt:
        print("\n已停止", flush=True)
    except (OSError, websockets.WebSocketException) as exc:
        print("WebSocket 连接失败: %s" % exc, flush=True)


if __name__ == "__main__":
    main()
