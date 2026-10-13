"""Lane reaper. Deadlines come from index.json, not from in-memory timers.

Every tick re-reads status=active entries and destroys those whose
deadline_ts is already in the past. A process restart therefore finishes
leases that expired while it was down.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Optional, Union

ROOT = Path(__file__).resolve().parent
AGENTS = ROOT / "agents"
if str(AGENTS) not in sys.path:
    sys.path.insert(0, str(AGENTS))

from sandbox_manager import SandboxError, SandboxManager


class SandboxReaper:
    """Poll the sandbox index and destroy expired active lanes."""

    def __init__(
        self,
        repo_root: Optional[Union[Path, str]] = None,
        *,
        manager: Optional[SandboxManager] = None,
        interval: float = 5.0,
    ):
        if interval <= 0:
            raise ValueError("interval must be positive")
        self.manager = manager if manager is not None else SandboxManager(repo_root=repo_root)
        self.interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def startup_reconciliation(self) -> dict:
        """Compare safeagent.managed=true containers with the index.

        Index entry without a container is marked failed.
        A managed container missing from the index is deleted.
        """
        return self.manager.startup_reconciliation()

    def scan_once(self) -> list[str]:
        """Destroy every active lane whose deadline_ts is due. Returns container names."""
        destroyed: list[str] = []
        for entry in self.manager.list_expired():
            name = str(entry.get("container_name") or "")
            if not name:
                continue
            try:
                saved = self.manager.timeout_sandbox(name)
            except SandboxError as exc:
                print(f"[sandbox] timeout {name} failed: {exc}", file=sys.stderr)
                continue
            if saved.get("status") == "timed_out":
                destroyed.append(name)
                print(f"[sandbox] timeout {name} task={saved.get('task_id')}")
        return destroyed

    def start(self) -> None:
        """Start the background scan. The first pass runs immediately."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="sandbox-reaper",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 30.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.scan_once()
            except SandboxError as exc:
                print(f"[sandbox] reaper scan failed: {exc}", file=sys.stderr)
            except Exception as exc:
                print(f"[sandbox] reaper scan failed: {exc}", file=sys.stderr)
            if self._stop.wait(self.interval):
                return


def startup_reconciliation(repo_root: Optional[Union[Path, str]] = None) -> dict:
    """Run startup reconciliation against repo_root and return the report."""
    return SandboxReaper(repo_root=repo_root or ROOT).startup_reconciliation()


def main() -> None:
    """Reconcile, then keep scanning until Ctrl+C.

    The worker is a daemon thread, so the main thread has to stay alive.
    Otherwise the process exits and the scan never repeats.
    """
    try:
        reaper = SandboxReaper(repo_root=ROOT)
        report = reaper.startup_reconciliation()
    except SandboxError as exc:
        print(f"[sandbox] reaper not started: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)
    print("[sandbox] reconciliation", report, flush=True)
    reaper.start()
    print("[sandbox] reaper running, interval=5s, source=index deadline_ts. Ctrl+C to stop.", flush=True)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[sandbox] stopping reaper")
    finally:
        reaper.stop()


if __name__ == "__main__":
    main()

