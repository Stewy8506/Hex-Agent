"""B44 regression: terminal tasks are not re-entered and orphans are reconciled.

Resuming or executing a task that already reached a terminal state quietly
created a fresh attempt; an orphaned RUNNING task was only settled when a later
worker claimed it.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from harness_core.core.models import TaskState
from harness_core.core.orchestrator import HarnessOrchestrator, TerminalTaskReentryError


class TestTerminalTaskGuard(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="harness_terminal_guard_"))
        self.data_dir = self.tmp_dir / "data"
        self.workspace = self.tmp_dir / "ws"
        self.workspace.mkdir()
        self.orch = HarnessOrchestrator(
            data_dir=self.data_dir, default_workspace=self.workspace
        )

    def tearDown(self) -> None:
        try:
            self.orch.ledger.close()
        except Exception:
            pass
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _succeed(self) -> str:
        task = self.orch.ledger.submit_task(
            {"goal": "already finished", "execution_mode": "standard"}
        )
        task_id = task["task_id"]
        self.orch.ledger.transition_task(task_id, TaskState.RUNNING, reason_code="TEST_RUN")
        self.orch.ledger.transition_task(
            task_id, TaskState.VERIFYING, reason_code="TEST_VERIFY"
        )
        self.orch.ledger.transition_task(
            task_id, TaskState.SUCCEEDED, reason_code="TEST_SUCCESS"
        )
        return task_id

    def test_execute_and_resume_refuse_terminal_tasks(self) -> None:
        task_id = self._succeed()
        attempts_before = len(self.orch.ledger.list_attempts(task_id))

        with self.assertRaises(TerminalTaskReentryError):
            self.orch.execute_task(task_id)
        with self.assertRaises(TerminalTaskReentryError):
            self.orch.resume_task(task_id)

        self.assertEqual(len(self.orch.ledger.list_attempts(task_id)), attempts_before)
        self.assertEqual(self.orch.ledger.get_task(task_id)["status"], TaskState.SUCCEEDED.value)

    def test_startup_reconciles_running_task_without_a_lease(self) -> None:
        task = self.orch.ledger.submit_task(
            {"goal": "orphaned work", "execution_mode": "standard"}
        )
        task_id = task["task_id"]
        self.orch.ledger.transition_task(task_id, TaskState.RUNNING, reason_code="TEST_ORPHAN")
        self.orch.ledger.close()

        recovered = HarnessOrchestrator(
            data_dir=self.data_dir, default_workspace=self.workspace
        )
        try:
            self.assertEqual(
                recovered.ledger.get_task(task_id)["status"], TaskState.RETRY_WAIT.value
            )
        finally:
            recovered.ledger.close()

    def test_live_lease_is_not_reconciled(self) -> None:
        task = self.orch.ledger.submit_task(
            {"goal": "live worker", "execution_mode": "standard"}
        )
        task_id = task["task_id"]
        lease = self.orch.ledger.acquire_lease_and_attempt(
            task_id,
            "worker-live",
            base_tree_sha="tree",
            workspace_path=str(self.workspace),
        )
        self.orch.ledger.transition_task(
            task_id,
            TaskState.RUNNING,
            reason_code="TEST_LIVE",
            attempt_id=lease["attempt_id"],
            fencing_token=lease["fencing_token"],
        )
        self.orch.ledger.close()

        recovered = HarnessOrchestrator(
            data_dir=self.data_dir, default_workspace=self.workspace
        )
        try:
            self.assertEqual(
                recovered.ledger.get_task(task_id)["status"], TaskState.RUNNING.value
            )
        finally:
            recovered.ledger.close()


if __name__ == "__main__":
    unittest.main()
