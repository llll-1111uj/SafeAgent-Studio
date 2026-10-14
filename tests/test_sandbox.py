import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.sandbox_manager import SandboxManager, SandboxError

def check_residuals(task_id):
    """检查该任务是否还留着容器或卷。

    Docker 的 name 过滤是子串匹配，sa-1 会命中 sa-1001。
    这里先按前缀缩小范围，再只保留 sa-{task_id}- 开头的名字。
    返回 True 表示容器和卷都已清干净。
    """
    print(f"\n正在检查任务 {task_id} 的残留资源...")
    prefix = f"sa-{task_id}-"
    res = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name={prefix}", "--format", "{{.Names}}"],
        capture_output=True, text=True,
    )
    containers = [
        name.strip()
        for name in res.stdout.splitlines()
        if name.strip().startswith(prefix)
    ]
    if containers:
        print(f"❌ 发现残留容器: {', '.join(containers)}")
    else:
        print("✅ 无残留容器")

    wanted = {f"sa-ws-{task_id}", f"sa-out-{task_id}"}
    res = subprocess.run(
        ["docker", "volume", "ls", "--format", "{{.Name}}"],
        capture_output=True, text=True,
    )
    volumes = [name.strip() for name in res.stdout.splitlines() if name.strip() in wanted]
    if volumes:
        print(f"❌ 发现残留卷: {', '.join(volumes)}")
    else:
        print("✅ 无残留卷")
    return not containers and not volumes


def release_live_sandboxes(sm, task_id):
    """删掉该任务仍处于存活状态的沙箱，避免下次 create 被占住。"""
    index_path = Path(".worktrees") / "index.json"
    if not index_path.is_file():
        return
    data = json.loads(index_path.read_text(encoding="utf-8"))
    live = {"creating", "active", "kept", "removing"}
    names = []
    for item in data.get("sandboxes", []):
        if item.get("task_id") != task_id or item.get("status") not in live:
            continue
        name = item.get("container_name")
        if name and name not in names:
            names.append(name)
    for name in names:
        print(f"清理任务 {task_id} 的旧沙箱 {name}（status 仍占用该任务）...")
        sm.remove_sandbox(name, force=True)


def record(results, name, ok):
    results.append({"name": name, "ok": bool(ok)})


def print_summary(results):
    passed = sum(1 for item in results if item["ok"])
    failed = len(results) - passed
    print("\n===== 测试汇总 =====")
    print(f"通过 {passed}，失败 {failed}，合计 {len(results)}")
    for item in results:
        mark = "✅" if item["ok"] else "❌"
        print(f"  {mark} {item['name']}")


def _run_tests(results):
    # ========== 1. 准备任务文件 ==========
    for tid in [999, 1001]:
        task_path = Path(".tasks") / f"task_{tid}.json"
        if not task_path.exists():
            task_path.parent.mkdir(parents=True, exist_ok=True)
            task_path.write_text(
                json.dumps({
                    "id": tid, "subject": "smoke test", "description": "",
                    "status": "pending", "owner": "", "worktree": "", "blockedBy": []
                }, indent=2),
                encoding="utf-8",
            )

    # ========== 2. 实例化 SandboxManager ==========
    print("===== 初始化 SandboxManager（将触发启动对账） =====")
    sm = SandboxManager(repo_root=".")

    # ========== 3. 基础冒烟测试（正常路径） ==========
    print("\n===== 基础冒烟测试 task 999 =====")
    release_live_sandboxes(sm, 999)
    res = sm.create_sandbox(name="test-sandbox", task_id=999)
    sandbox_id = res["container_name"]
    print("container_id:", res["container_id"])
    print("status:", res["status"])

    exec_result = sm.execute(sandbox_id, "echo hello world")
    print("execute:", exec_result)
    
    remove_res = sm.remove_sandbox(sandbox_id)
    print("remove status:", remove_res["status"])
    record(results, "冒烟测试 create 为 active", res.get("status") == "active")
    record(
        results,
        "冒烟测试 execute 成功",
        exec_result.get("exit_code") == 0 and not exec_result.get("timed_out"),
    )
    record(results, "冒烟测试 remove 为 removed", remove_res.get("status") == "removed")

    # ========== 4. 测试一：回滚测试（异常路径） ==========
    print("\n===== 测试一：回滚测试，不存在的base_ref =====")
    try:
        # 故意传入不存在的分支，触发底层 git archive 失败
        res = sm.create_sandbox(name="test-fail", task_id=1001, base_ref="NON_EXISTENT_BRANCH")
        print("❌ 不应该创建成功！", res)
        record(results, "测试一 不存在的 base_ref 应失败", False)
    except SandboxError as e:
        print("✅ 捕获到预期 SandboxError：", e)
        # 沙箱回滚可能清掉 index 里的失败条目，这里在外部另存一份。
        backup_path = Path(".worktrees/failed_tasks.jsonl")
        backup_entry = {
            "task_id": 1001,
            "name": "test-fail",
            "base_ref": "NON_EXISTENT_BRANCH",
            "status": "failed",
            "error": str(e),
            "time": time.time(),
        }
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        with backup_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(backup_entry, ensure_ascii=False) + "\n")
        print(f"📝 失败任务记录已备份到 {backup_path}")
        record(results, "测试一 捕获 SandboxError 并备份", backup_path.is_file())
    except Exception as e:
        print("❌ 捕获非预期异常", e)
        record(results, "测试一 捕获 SandboxError 并备份", False)

    # ========== 5. 自动验证残留与索引状态 ==========
    record(results, "测试一 无残留容器和卷", check_residuals(1001))

    # 验证索引状态
    index_path = Path(".worktrees") / "index.json"
    index_failed = False
    if index_path.exists():
        data = json.loads(index_path.read_text(encoding="utf-8"))
        matched = [item for item in data.get("sandboxes", []) if item.get("task_id") == 1001]
        if matched:
            item = matched[-1]
            print(f"索引中任务 1001 的最新状态: {item.get('status')} (预期为 failed)")
            index_failed = item.get("status") == "failed"
            if not index_failed:
                print("状态异常")
        else:
            print("索引中找不到任务 1001 的记录")
    record(results, "测试一 索引状态为 failed", index_failed)

    # ========== 6. 测试二：启动对账（制造孤儿容器） ==========
    print("\n===== 测试二：启动对账（孤儿容器清理） =====")
    print("清理上一次可能残留的 orphan-test ...")
    subprocess.run(["docker", "rm", "-f", "orphan-test"], capture_output=True, text=True)
    sm_reconciled = sm
    created = subprocess.run(
        ["docker", "run", "-d", "--label", "safeagent.managed=true", "--name", "orphan-test", "alpine", "sleep", "1000"],
        capture_output=True, text=True,
    )
    if created.returncode != 0:
        print("❌ 孤儿容器创建失败:", (created.stderr or created.stdout).strip())
        record(results, "测试二 孤儿容器已被清理", False)
    else:
        print("已手动创建孤儿容器 orphan-test，现在触发对账...")
    
    # 重新实例化 SandboxManager，触发 __init__ 中的 startup_reconciliation
        sm_reconciled = SandboxManager(repo_root=".")
        print("对账完成，检查孤儿容器是否被删除：")
        res = subprocess.run(
            ["docker", "ps", "-a", "--filter", "name=orphan-test", "--format", "{{.Names}}"],
            capture_output=True, text=True,
        )
        still_there = any(name.strip() == "orphan-test" for name in res.stdout.splitlines())
        if not still_there:
            print("✅ 孤儿容器已被成功清理")
        else:
            print("❌ 孤儿容器仍然存在！")
        record(results, "测试二 孤儿容器已被清理", not still_there)

    # ========== 7. 测试三：Keep 状态 ==========
    print("\n===== 测试三：Keep 状态 =====")
    # 重新创建 task 999 的沙箱
    res = sm_reconciled.create_sandbox(name="test-keep", task_id=999)
    sandbox_id_keep = res["container_name"]
    print(f"创建沙箱 {sandbox_id_keep}，现在执行 keep_sandbox...")
    
    keep_res = sm_reconciled.keep_sandbox(sandbox_id_keep)
    print("keep status:", keep_res["status"])
    record(results, "测试三 keep 状态为 kept", keep_res.get("status") == "kept")

    # 检查容器是否已停止，卷是否保留
    res = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name={sandbox_id_keep}", "--format", "{{.Names}} {{.Status}}"],
        capture_output=True, text=True,
    )
    kept_line = ""
    for line in res.stdout.splitlines():
        if line.startswith(sandbox_id_keep + " "):
            kept_line = line
            break
    print(f"容器状态: {kept_line or '(未找到)'} (预期为 Exited)")
    record(results, "测试三 容器已停止", "Exited" in kept_line)

    res = subprocess.run(["docker", "volume", "ls", "--format", "{{.Name}}"], capture_output=True, text=True)
    kept_volume = "sa-ws-999" in {name.strip() for name in res.stdout.splitlines()}
    if kept_volume:
        print("✅ 卷 sa-ws-999 已保留")
    else:
        print("❌ 卷被错误地删除了！")
    record(results, "测试三 卷已保留", kept_volume)
    release_live_sandboxes(sm_reconciled, 999)


if __name__ == "__main__":
    results = []
    try:
        _run_tests(results)
    except Exception as e:
        record(results, "测试过程未捕获异常", False)
        print("❌ 测试中断:", e)
    finally:
        print_summary(results)