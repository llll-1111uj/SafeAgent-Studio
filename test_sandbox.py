"""Manual runner for SandboxManager.

create_sandbox / execute / remove_sandbox call Docker for real.
Requires a git commit in this repo, plus the alpine and
safeagent-sandbox:py310 images.
"""

import json
from pathlib import Path

from agents.sandbox_manager import SandboxManager

if __name__ == "__main__":
    task_path = Path(".tasks") / "task_999.json"
    if not task_path.exists():
        task_path.parent.mkdir(parents=True, exist_ok=True)
        task_path.write_text(
            json.dumps(
                {
                    "id": 999,
                    "subject": "sandbox smoke test",
                    "description": "",
                    "status": "pending",
                    "owner": "",
                    "worktree": "",
                    "blockedBy": [],
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    sm = SandboxManager(repo_root=".")
    res = sm.create_sandbox(name="test-sandbox", task_id=999)
    sandbox_id = res["container_name"]
    print("container_id:", res["container_id"])
    print("sandbox_id:", sandbox_id)
    print("status:", res["status"])
    print("deadline_ts:", res["deadline_ts"])

    exec_result = sm.execute(sandbox_id, "echo hello world")
    print("execute:", exec_result)

    remove_res = sm.remove_sandbox(sandbox_id)
    print("remove status:", remove_res["status"])
