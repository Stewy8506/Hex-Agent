"""B12 regression: enforced delegation deadlines/budgets, cancel fan-out, and
a terminal-state guard so a late child result cannot overwrite INTERRUPTED.
"""

from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from harness_core.core.orchestrator import HarnessOrchestrator
from harness_core.core.subagent import (
    SubagentManager,
    SubagentRecord,
    SubagentRole,
    SubagentStatus,
)
from harness_core.storage.cow import get_default_snapshot_provider
from harness_core.storage.db import LedgerStore


class SlowRouter:
    """Router whose reasoning call blocks for a fixed delay."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.calls = 0

    def execute_routed_request(self, **kwargs):
        self.calls += 1
        time.sleep(self.delay)
        return None, SimpleNamespace(content="slow result")


class TestSubagentDeadlines(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.ws = (Path(self.temp_dir) / "workspace").resolve()
        self.ws.mkdir(parents=True)
        (self.ws / "app.py").write_text("x = 1\n", encoding="utf-8")
        self.ledger = LedgerStore(Path(self.temp_dir) / "ledger.db")
        self.cow = get_default_snapshot_provider(Path(self.temp_dir) / "cow")

    def tearDown(self) -> None:
        self.ledger.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _mgr(self, router=None) -> SubagentManager:
        return SubagentManager(ledger=self.ledger, cow=self.cow, router=router, max_depth=2)

    def test_deadline_is_enforced_not_just_accepted(self) -> None:
        mgr = self._mgr(SlowRouter(delay=3.0))
        start = time.monotonic()
        res = mgr.spawn_subagent(
            parent_task_id="tsk_deadline",
            goal="slow child",
            role=SubagentRole.RESEARCHER,
            parent_ws=self.ws,
            timeout=0.3,
            wait=True,
        )
        elapsed = time.monotonic() - start
        self.assertEqual(res.status, "timed_out")
        self.assertLess(elapsed, 2.0, "deadline must bound the wait, not block for the full reasoning call")

    def test_budget_timeout_tightens_deadline(self) -> None:
        mgr = self._mgr(SlowRouter(delay=3.0))
        start = time.monotonic()
        res = mgr.spawn_subagent(
            parent_task_id="tsk_budget",
            goal="slow child",
            role=SubagentRole.RESEARCHER,
            parent_ws=self.ws,
            timeout=120.0,
            budget={"timeout": 0.3},
            wait=True,
        )
        elapsed = time.monotonic() - start
        self.assertEqual(res.status, "timed_out")
        self.assertLess(elapsed, 2.0)

    def test_budget_tool_call_ceiling_enforced(self) -> None:
        mgr = self._mgr(router=None)
        record = SubagentRecord(
            subagent_id="sub_budget",
            parent_task_id="tsk_budget_calls",
            role=SubagentRole.GENERAL,
            goal="over budget",
            depth=1,
            status=SubagentStatus.PENDING,
            started_at=time.time(),
            workspace_path=self.ws,
            budget={"max_tool_calls": 3},
            tool_calls_count=5,
        )
        mgr._run_subagent_sync(record, timeout=5.0)
        self.assertEqual(record.status, SubagentStatus.FAILED)
        self.assertIn("budget", (record.error or "").lower())

    def test_late_result_cannot_overwrite_interrupted(self) -> None:
        mgr = self._mgr(SlowRouter(delay=0.8))
        sid = mgr.spawn_subagent(
            parent_task_id="tsk_late",
            goal="interrupted child",
            role=SubagentRole.GENERAL,
            parent_ws=self.ws,
            timeout=10.0,
            wait=False,
        )
        # Wait until the child is actually running its reasoning call.
        for _ in range(200):
            if mgr.get_subagent_status(sid)["status"] == "running":
                break
            time.sleep(0.01)
        self.assertTrue(mgr.interrupt_subagent(sid))
        mgr.wait_subagent(sid, timeout=5.0)
        self.assertEqual(mgr.get_subagent_status(sid)["status"], "interrupted")

        # Let the late reasoning call finish; it must not resurrect COMPLETED.
        time.sleep(1.0)
        self.assertEqual(mgr.get_subagent_status(sid)["status"], "interrupted")

    def test_interrupt_children_of_only_matching_parent(self) -> None:
        mgr = self._mgr(SlowRouter(delay=1.0))
        a1 = mgr.spawn_subagent(parent_task_id="parent_A", goal="a1", parent_ws=self.ws, wait=False)
        a2 = mgr.spawn_subagent(parent_task_id="parent_A", goal="a2", parent_ws=self.ws, wait=False)
        b1 = mgr.spawn_subagent(parent_task_id="parent_B", goal="b1", parent_ws=self.ws, wait=False)
        for sid in (a1, a2, b1):
            for _ in range(200):
                if mgr.get_subagent_status(sid)["status"] == "running":
                    break
                time.sleep(0.01)

        count = mgr.interrupt_children_of("parent_A")
        self.assertEqual(count, 2)
        self.assertEqual(mgr.get_subagent_status(a1)["status"], "interrupted")
        self.assertEqual(mgr.get_subagent_status(a2)["status"], "interrupted")
        self.assertEqual(mgr.get_subagent_status(b1)["status"], "running")
        mgr.interrupt_children_of("parent_B")


class TestParentCancelFanOut(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.ws = (Path(self.temp_dir) / "workspace").resolve()
        self.ws.mkdir(parents=True)
        self.orch = HarnessOrchestrator(
            data_dir=Path(self.temp_dir) / "data",
            default_workspace=self.ws,
        )
        self.orch.subagent_manager.router = SlowRouter(delay=1.0)

    def tearDown(self) -> None:
        self.orch.ledger.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_cancel_task_interrupts_live_children(self) -> None:
        task = self.orch.ledger.submit_task({"goal": "parent with children", "execution_mode": "standard"})
        tid = task["task_id"]
        try:
            self.orch.ledger.transition_task(tid, "RUNNING", reason_code="TEST_START")
        except Exception:
            pass
        mgr = self.orch.subagent_manager
        sid = mgr.spawn_subagent(parent_task_id=tid, goal="child", parent_ws=self.ws, wait=False)
        for _ in range(200):
            if mgr.get_subagent_status(sid)["status"] == "running":
                break
            time.sleep(0.01)

        self.orch.cancel_task(tid, actor="test")

        self.assertEqual(mgr.get_subagent_status(sid)["status"], "interrupted")
        self.assertEqual(self.orch.ledger.get_task(tid)["status"], "CANCELED")


if __name__ == "__main__":
    unittest.main()