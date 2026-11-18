"""B11 regression: missing execution backends must fail closed.

A subagent with no router (or an empty model response) must not manufacture a
COMPLETED summary, and a worker with no orchestrator must not mark a task
SUCCEEDED without executing it. The deterministic stand-in is only available
under explicit demo mode.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from harness_core.core.subagent import SubagentManager, SubagentRole
from harness_core.gateway import ToolGateway
from harness_core.storage.cow import get_default_snapshot_provider
from harness_core.storage.db import LedgerStore
from harness_core.runtime.worker_daemon import DirectLedgerWorkerClient


class TestSubagentFailClosed(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.ws = (Path(self.temp_dir) / "workspace").resolve()
        self.ws.mkdir(parents=True, exist_ok=True)
        self.ledger = LedgerStore(Path(self.temp_dir) / "ledger.db")
        self.cow = get_default_snapshot_provider(Path(self.temp_dir) / "cow")
        self.gateway = ToolGateway(self.ledger)

    def tearDown(self) -> None:
        self.ledger.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _manager(self, **kwargs) -> SubagentManager:
        mgr = SubagentManager(ledger=self.ledger, cow=self.cow, gateway=self.gateway, **kwargs)
        self.gateway.subagent_manager = mgr
        return mgr

    def test_no_router_sync_fails(self) -> None:
        mgr = self._manager()
        res = mgr.spawn_subagent(
            parent_task_id="tsk_b11",
            goal="Analyze architecture",
            role=SubagentRole.RESEARCHER,
            parent_ws=self.ws,
            wait=True,
        )
        self.assertEqual(res.status, "failed")
        self.assertTrue(res.error)
        self.assertNotIn("processed goal", res.summary or "")

    def test_no_router_async_fails(self) -> None:
        mgr = self._manager()
        sid = mgr.spawn_subagent(
            parent_task_id="tsk_b11_async",
            goal="Background work",
            role=SubagentRole.PLANNER,
            parent_ws=self.ws,
            wait=False,
        )
        res = mgr.wait_subagent(sid, timeout=10.0)
        self.assertEqual(res.status, "failed")

    def test_gateway_delegate_no_router_fails(self) -> None:
        self._manager()
        res = self.gateway._tool_delegate(
            self.ws, {"goal": "Audit security", "role": "reviewer", "wait": True}
        )
        self.assertEqual(res["status"], "failed")
        self.assertEqual(res["result"]["status"], "failed")

    def test_demo_mode_completes_with_labeled_standin(self) -> None:
        mgr = self._manager(demo_mode=True)
        res = mgr.spawn_subagent(
            parent_task_id="tsk_b11_demo",
            goal="Analyze architecture",
            role=SubagentRole.RESEARCHER,
            parent_ws=self.ws,
            wait=True,
        )
        self.assertEqual(res.status, "completed")
        self.assertIn("processed goal", res.summary)
        self.assertIn("[demo]", res.summary)

    def test_router_empty_content_fails(self) -> None:
        router = MagicMock()
        router.execute_routed_request.return_value = (MagicMock(), MagicMock(content=None))
        mgr = self._manager(router=router)
        res = mgr.spawn_subagent(
            parent_task_id="tsk_b11_empty",
            goal="Analyze architecture",
            role=SubagentRole.RESEARCHER,
            parent_ws=self.ws,
            wait=True,
        )
        self.assertEqual(res.status, "failed")

    def test_router_with_content_completes(self) -> None:
        router = MagicMock()
        router.execute_routed_request.return_value = (MagicMock(), MagicMock(content="real findings"))
        mgr = self._manager(router=router)
        res = mgr.spawn_subagent(
            parent_task_id="tsk_b11_real",
            goal="Analyze architecture",
            role=SubagentRole.RESEARCHER,
            parent_ws=self.ws,
            wait=True,
        )
        self.assertEqual(res.status, "completed")
        self.assertEqual(res.summary, "real findings")


class TestWorkerFailClosed(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.ledger = LedgerStore(Path(self.temp_dir) / "ledger.db")

    def tearDown(self) -> None:
        self.ledger.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_no_orchestrator_fails_task(self) -> None:
        self.ledger.submit_task({"goal": "Unexecuted work", "status": "QUEUED"})
        claimed = self.ledger.claim_next_task(worker_id="w-b11", ttl_seconds=60)
        self.assertIsNotNone(claimed)
        task_id = claimed["task_id"]
        client = DirectLedgerWorkerClient(self.ledger)
        self.assertIsNone(client.orch)
        client.execute_task(task_id, worker_id="w-b11")
        task = self.ledger.get_task(task_id)
        self.assertEqual(task["status"], "FAILED")
        self.assertNotEqual(task["status"], "SUCCEEDED")


if __name__ == "__main__":
    unittest.main()