"""Directory escape check. read_file rejects this path before any approval."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "agents"))

import core_agent as agent

PATH = "../../Windows/System32/drivers/etc/hosts"
approvals = []


def waiter(*args):
    approvals.append(args)
    return True


def main():
    agent.set_approval_waiter(waiter)
    token = agent.set_task_id(1)
    try:
        output = agent.run_read(PATH)
    finally:
        agent.reset_task_id(token)
        agent.set_approval_waiter(None)
    print(output, flush=True)
    if "path escapes /workspace" not in output:
        raise SystemExit("FAIL: expected path escape, got: %s" % output)
    if approvals:
        raise SystemExit("FAIL: read_file asked for approval: %s" % (approvals,))
    print("PASS path escape blocked without HITL", flush=True)


if __name__ == "__main__":
    main()
