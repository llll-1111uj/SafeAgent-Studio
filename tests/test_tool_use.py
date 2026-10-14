"""隔离验证 core_agent 的 bash / read_file / write_file。

core_agent 不接收沙箱参数。它只认唯一一个 status=in_progress 且 worktree 非空的任务，
再按 task_id + worktree 到 .worktrees/index.json 里找 status=active 的容器名。
因此创建沙箱之后必须把 task_888.json 写成 in_progress / worktree=tool-test，
结束时再改回 pending / worktree=""。
"""

import json
import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "agents"))

from sandbox_manager import SandboxError, SandboxManager
from core_agent import run_bash, run_read, run_write

TASK_ID = 888
SANDBOX_NAME = "tool-test"
TASK_PATH = ROOT / ".tasks" / "task_888.json"
WORKTREES = ROOT / ".worktrees"
INDEX_PATH = WORKTREES / "index.json"


def fail(message):
    raise SystemExit("FAIL: " + message)


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save_task(status, worktree):
    task = load_json(TASK_PATH) if TASK_PATH.is_file() else {
        "id": TASK_ID,
        "subject": "tool isolation",
        "description": "",
        "owner": "",
        "blockedBy": [],
    }
    task["id"] = TASK_ID
    task["status"] = status
    task["worktree"] = worktree
    task["updated_at"] = time.time()
    TASK_PATH.parent.mkdir(parents=True, exist_ok=True)
    TASK_PATH.write_text(json.dumps(task, indent=2, ensure_ascii=False), encoding="utf-8")
    return task


def latest_index_entry():
    if not INDEX_PATH.is_file():
        return None
    data = load_json(INDEX_PATH)
    match = None
    for item in data.get("sandboxes", []):
        if item.get("task_id") == TASK_ID and item.get("name") == SANDBOX_NAME:
            match = item
    return match


def dump_binding(title):
    print("--- %s ---" % title)
    if TASK_PATH.is_file():
        task = load_json(TASK_PATH)
        print(
            "task_888: status=%s worktree=%r"
            % (task.get("status"), task.get("worktree"))
        )
    else:
        print("task_888: 文件不存在")
    entry = latest_index_entry()
    if entry is None:
        print("index: 没有 task 888 / %s 的沙箱记录" % SANDBOX_NAME)
    else:
        print(
            "index: status=%s container_name=%s container_id=%s"
            % (entry.get("status"), entry.get("container_name"), entry.get("container_id"))
        )


def write_task_file():
    if not TASK_PATH.exists():
        save_task("pending", "")


def release_task(sm):
    """删掉任务 888 的存活沙箱，并把任务文件改回未绑定。"""
    if INDEX_PATH.is_file():
        data = load_json(INDEX_PATH)
        live = {"creating", "active", "kept", "removing"}
        seen = []
        for item in data.get("sandboxes", []):
            if item.get("task_id") != TASK_ID or item.get("status") not in live:
                continue
            name = item.get("container_name")
            if name and name not in seen:
                seen.append(name)
        for name in seen:
            print("清理沙箱", name)
            try:
                sm.remove_sandbox(name, force=True)
            except SandboxError as exc:
                print("SandboxError:", exc)
                raise
    if TASK_PATH.is_file():
        save_task("pending", "")


def assert_no_other_bound_task():
    tasks_dir = ROOT / ".tasks"
    if not tasks_dir.is_dir():
        return
    for path in tasks_dir.glob("task_*.json"):
        task = load_json(path)
        if task.get("id") == TASK_ID:
            continue
        if task.get("status") == "in_progress" and str(task.get("worktree") or "").strip():
            fail("已有其他进行中的绑定任务: %s status=%s worktree=%r" % (
                path.name, task.get("status"), task.get("worktree"),
            ))


def test_unbound():
    output = run_bash("echo hi")
    print("未绑沙箱 bash:", output)
    if output != "Error: current task has no bound sandbox":
        fail("未绑沙箱时期望拒绝，实际: %s" % output)
    print("PASS 未绑沙箱")


def test_bash_inside_container():
    output = run_bash("ls -la /")
    print("容器内 ls /:", output)
    if output.startswith("Error:"):
        fail("容器内 ls 失败: %s" % output)
    if "workspace" not in output:
        fail("ls / 的输出里没有 workspace: %s" % output)
    lowered = output.lower()
    if "d:" in lowered or "shixi" in lowered:
        fail("ls / 泄漏了宿主机路径: %s" % output)
    print("PASS 容器内执行")


def test_path_escape():
    output = run_read("../../Windows/System32/drivers/etc/hosts")
    print("目录穿越 read_file:", output)
    if not output.startswith("Error:") or "escapes /workspace" not in output:
        fail("目录穿越未被拒绝: %s" % output)
    print("PASS 防目录穿越")


def test_write_read_not_on_host():
    written = run_write("test.txt", "hello")
    print("write_file:", written)
    if written.startswith("Error:"):
        fail("write_file 失败: %s" % written)
    read_back = run_read("test.txt")
    print("read_file:", read_back)
    if read_back != "hello":
        fail("read_file 期望 hello，实际: %s" % read_back)

    host_paths = [
        ROOT / "test.txt",
        WORKTREES / "test.txt",
        WORKTREES / SANDBOX_NAME / "test.txt",
        WORKTREES / ("sa-%s-%s" % (TASK_ID, SANDBOX_NAME)) / "test.txt",
    ]
    for path in host_paths:
        if os.path.exists(path):
            fail("test.txt 出现在宿主机: %s" % path)
    if WORKTREES.is_dir():
        for path in WORKTREES.rglob("test.txt"):
            fail("test.txt 出现在宿主机工作树: %s" % path)
    print("PASS 读写与宿主机隔离")


def main():
    write_task_file()
    sm = None
    try:
        sm = SandboxManager(repo_root=ROOT)
        release_task(sm)
        dump_binding("释放后，未绑定")
        assert_no_other_bound_task()
        test_unbound()

        print("创建沙箱", SANDBOX_NAME, "task", TASK_ID)
        try:
            created = sm.create_sandbox(SANDBOX_NAME, TASK_ID)
        except SandboxError as exc:
            print("SandboxError:", exc)
            raise
        print("create_sandbox 返回:", created.get("container_name"), created.get("status"))
        save_task("in_progress", SANDBOX_NAME)
        dump_binding("绑定后，工具应能找到这个沙箱")

        test_bash_inside_container()
        test_path_escape()
        test_write_read_not_on_host()
    except SandboxError as exc:
        print("SandboxError:", exc)
        traceback.print_exc()
        raise SystemExit(1)
    except Exception:
        traceback.print_exc()
        raise
    finally:
        if sm is not None:
            try:
                release_task(sm)
            except SandboxError as exc:
                print("清理时 SandboxError:", exc)
        dump_binding("结束后，任务应回到 pending 且 worktree 为空")
    print("ALL PASSED")


if __name__ == "__main__":
    main()
