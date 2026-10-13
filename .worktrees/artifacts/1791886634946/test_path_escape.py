"""Directory escape check. read_file rejects this path before any approval."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT / "agents"))

import s02_tool_use as s02

PATH = "../../Windows/System32/drivers/etc/hosts"
approvals = []


def waiter(*args):
    approvals.append(args)
    return True


def main():
    s02.set_approval_waiter(waiter)
    token = s02.set_task_id(1)
    try:
        output = s02.run_read(PATH)
    finally:
        s02.reset_task_id(token)
        s02.set_approval_waiter(None)
    print(output, flush=True)
    if "path escapes /workspace" not in output:
        raise SystemExit("FAIL: expected path escape, got: %s" % output)
    if approvals:
        raise SystemExit("FAIL: read_file asked for approval: %s" % (approvals,))
    print("PASS path escape blocked without HITL", flush=True)


if __name__ == "__main__":
    main()
