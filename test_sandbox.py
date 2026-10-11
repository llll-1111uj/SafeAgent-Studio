import json
import subprocess
from pathlib import Path
from agents.sandbox_manager import SandboxManager, SandboxError  # 假设 SandboxError 在 sandbox_manager 中定义

def check_residuals(task_id):
    """自动检查宿主机是否有残留的容器和卷"""
    print(f"\n正在检查任务 {task_id} 的残留资源...")
    container_name = f"sa-{task_id}"
    
    # 检查容器
    res = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name={container_name}", "--format", "{{.Names}}"],
        capture_output=True, text=True
    )
    containers = res.stdout.strip()
    if containers:
        print(f"❌ 发现残留容器: {containers}")
    else:
        print("✅ 无残留容器")

    # 检查卷
    res = subprocess.run(
        ["docker", "volume", "ls", "--filter", f"name=sa-ws-{task_id}", "--format", "{{.Name}}"],
        capture_output=True, text=True
    )
    volumes = res.stdout.strip()
    if volumes:
        print(f"❌ 发现残留卷: {volumes}")
    else:
        print("✅ 无残留卷")

if __name__ == "__main__":
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
    res = sm.create_sandbox(name="test-sandbox", task_id=999)
    sandbox_id = res["container_name"]
    print("container_id:", res["container_id"])
    print("status:", res["status"])

    exec_result = sm.execute(sandbox_id, "echo hello world")
    print("execute:", exec_result)
    
    remove_res = sm.remove_sandbox(sandbox_id)
    print("remove status:", remove_res["status"])

    # ========== 4. 测试一：回滚测试（异常路径） ==========
    print("\n===== 测试一：回滚测试，不存在的base_ref =====")
    try:
        # 故意传入不存在的分支，触发底层 git archive 失败
        res = sm.create_sandbox(name="test-fail", task_id=1001, base_ref="NON_EXISTENT_BRANCH")
        print("❌ 不应该创建成功！", res)
    except Exception as e:  # 最好捕获 SandboxError
        print("✅ 捕获到预期异常：", e)

    # ========== 5. 自动验证残留与索引状态 ==========
    check_residuals(1001)

    # 验证索引状态
    index_path = Path(".worktrees") / "index.json"
    if index_path.exists():
        data = json.loads(index_path.read_text(encoding="utf-8"))
        for item in data.get("sandboxes", []):
            if item.get("task_id") == 1001:
                print(f"✅ 索引中任务 1001 的状态: {item.get('status')} (预期为 failed)")
                if item.get("status") != "failed":
                    print("❌ 状态异常！")
                break
        else:
            print("⚠️ 索引中找不到任务 1001 的记录")

    # ========== 6. 测试二：启动对账（制造孤儿容器） ==========
    print("\n===== 测试二：启动对账（孤儿容器清理） =====")
    import subprocess
    # 手动制造一个带 managed 标签的孤儿容器
    subprocess.run(["docker", "run", "-d", "--label", "safeagent.managed=true", "--name", "orphan-test", "alpine", "sleep", "1000"])
    print("已手动创建孤儿容器 orphan-test，现在触发对账...")
    
    # 重新实例化 SandboxManager，触发 __init__ 中的 startup_reconciliation
    sm_reconciled = SandboxManager(repo_root=".")
    print("对账完成，检查孤儿容器是否被删除：")
    res = subprocess.run(["docker", "ps", "-a", "--filter", "name=orphan-test", "--format", "{{.Names}}"], capture_output=True, text=True)
    if not res.stdout.strip():
        print("✅ 孤儿容器已被成功清理")
    else:
        print("❌ 孤儿容器仍然存在！")

    # ========== 7. 测试三：Keep 状态 ==========
    print("\n===== 测试三：Keep 状态 =====")
    # 重新创建 task 999 的沙箱
    res = sm_reconciled.create_sandbox(name="test-keep", task_id=999)
    sandbox_id_keep = res["container_name"]
    print(f"创建沙箱 {sandbox_id_keep}，现在执行 keep_sandbox...")
    
    keep_res = sm_reconciled.keep_sandbox(sandbox_id_keep)
    print("keep status:", keep_res["status"])
    
    # 检查容器是否已停止，卷是否保留
    res = subprocess.run(["docker", "ps", "-a", "--filter", f"name={sandbox_id_keep}", "--format", "{{.Status}}"], capture_output=True, text=True)
    print(f"容器状态: {res.stdout.strip()} (预期为 Exited)")
    
    res = subprocess.run(["docker", "volume", "ls", "--filter", f"name=sa-ws-999", "--format", "{{.Name}}"], capture_output=True, text=True)
    if res.stdout.strip():
        print(f"✅ 卷 {res.stdout.strip()} 已保留")
    else:
        print("❌ 卷被错误地删除了！")