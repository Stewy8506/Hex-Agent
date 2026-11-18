"""B49 regression: orderly shutdown on headless Ctrl-C and ACP EOF/interrupt.

Interrupting headless execution must cancel in-flight tasks (CoW rollback) and
clean up approval leases instead of exiting silently. ACP EOF/interrupt must
quiesce worker threads, withdraw pending approvals, cancel running turns, and
record an explicit interrupted state.
"""

from __future__ import annotations

import io
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from harness_core.capabilities.gateways.reaction_listener import ApprovalRequest
from harness_core.client import cli
from harness_core.client.acp import ACPServer, ACPSession
from harness_core.client.acp_approval import AcpApprovalBridge
from harness_core.core.approval_lease import ApprovalLeaseManager
from harness_core.core.orchestrator import HarnessOrchestrator
from harness_core.gateway import PolicyBlockedError, RiskClass, ToolSpec


class _EmitCapture:
    def __init__(self) -> None:
        self.messages: list = []
        self.event = threading.Event()

    def __call__(self, obj: dict) -> None:
        self.messages.append(obj)
        self.event.set()

    def wait_for_request(self, timeout: float = 5.0) -> dict:
        assert self.event.wait(timeout), "no approvalRequest emitted"
        for msg in self.messages:
            if msg.get("method") == "session/approvalRequest":
                return msg["params"]
        raise AssertionError("no approvalRequest among emitted messages")


class TestHeadlessInterruptShutdown(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.ws = Path(self.temp_dir) / "workspace"
        self.ws.mkdir(parents=True)
        self.orch = HarnessOrchestrator(
            data_dir=Path(self.temp_dir) / "data",
            default_workspace=self.ws,
        )
        self._prev_active = cli._active_orchestrator

    def tearDown(self) -> None:
        cli._active_orchestrator = self._prev_active
        self.orch.ledger.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _running_task(self) -> str:
        task = self.orch.ledger.submit_task({"goal": "long running headless task"})
        tid = task["task_id"]
        try:
            self.orch.ledger.transition_task(tid, "RUNNING", reason_code="TEST_START")
        except Exception:
            pass
        return tid

    def test_shutdown_cancels_inflight_task_and_revokes_leases(self) -> None:
        tid = self._running_task()
        leases = ApprovalLeaseManager(self.orch.ledger)
        self.orch.gateway.approval_lease_manager = leases
        leases.grant_lease(command_prefix="pytest", ttl_seconds=900, task_id=tid)
        self.assertEqual(len(leases.list_active_leases()), 1)

        summary = cli._orderly_headless_shutdown(self.orch)

        self.assertTrue(summary["interrupted"])
        self.assertIn(tid, summary["canceled_tasks"])
        self.assertEqual(self.orch.ledger.get_task(tid)["status"], "CANCELED")
        self.assertEqual(summary["leases_revoked"], 1)
        self.assertEqual(leases.list_active_leases(), [])

    def test_shutdown_leaves_terminal_tasks_untouched(self) -> None:
        task = self.orch.ledger.submit_task({"goal": "already done"})
        tid = task["task_id"]
        self.orch.ledger.transition_task(tid, "RUNNING", reason_code="TEST_START")
        self.orch.ledger.transition_task(tid, "FAILED", reason_code="TEST_FAIL")

        summary = cli._orderly_headless_shutdown(self.orch)

        self.assertEqual(self.orch.ledger.get_task(tid)["status"], "FAILED")
        self.assertNotIn(tid, summary["canceled_tasks"])

    def test_run_main_interrupt_runs_orderly_shutdown(self) -> None:
        tid = self._running_task()

        def fake_impl(argv, *, process_handoff):
            cli._register_active_orchestrator(self.orch)
            raise KeyboardInterrupt

        with patch.object(cli, "_main_impl", side_effect=fake_impl):
            rc = cli._run_main(None, process_handoff=False)

        self.assertEqual(rc, 130)
        self.assertEqual(self.orch.ledger.get_task(tid)["status"], "CANCELED")

    def test_shutdown_without_orchestrator_is_safe(self) -> None:
        summary = cli._orderly_headless_shutdown(None)
        self.assertTrue(summary["interrupted"])
        self.assertEqual(summary["canceled_tasks"], [])


class TestACPShutdown(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.ws = Path(self.temp_dir) / "workspace"
        self.ws.mkdir(parents=True)
        self.orch = HarnessOrchestrator(
            data_dir=Path(self.temp_dir) / "data",
            default_workspace=self.ws,
        )
        self.server = ACPServer(orchestrator=self.orch, workspace_root=self.ws)

    def tearDown(self) -> None:
        self.orch.ledger.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _session_with_task(self) -> tuple[ACPSession, str]:
        task = self.orch.ledger.submit_task(
            {"goal": "in-flight acp turn", "execution_mode": "standard"}
        )
        tid = task["task_id"]
        try:
            self.orch.ledger.transition_task(tid, "RUNNING", reason_code="TEST_START")
        except Exception:
            pass
        sess = ACPSession(session_id="sess_shutdown", workspace_root=self.ws)
        sess.last_task_id = tid
        sess.running = True
        self.server.sessions[sess.session_id] = sess
        return sess, tid

    def test_shutdown_cancels_running_session_task(self) -> None:
        sess, tid = self._session_with_task()
        summary = self.server._shutdown_orderly("eof")

        self.assertTrue(summary["interrupted"])
        self.assertEqual(summary["reason"], "eof")
        self.assertIn(tid, summary["canceled_tasks"])
        self.assertEqual(self.orch.ledger.get_task(tid)["status"], "CANCELED")
        self.assertTrue(sess.cancelled)

    def test_shutdown_is_idempotent(self) -> None:
        sess, tid = self._session_with_task()
        first = self.server._shutdown_orderly("eof")
        second = self.server._shutdown_orderly("eof")
        self.assertIn(tid, first["canceled_tasks"])
        self.assertEqual(second["canceled_tasks"], [])

    def test_shutdown_wakes_pending_approval(self) -> None:
        self.orch.gateway.register_tool(
            ToolSpec(
                tool_id="deploy",
                description="privileged",
                risk_class=RiskClass.R3,
                handler=lambda ws, args: {"ok": True},
            )
        )
        capture = _EmitCapture()
        self.server._emit = capture
        assert self.server.orchestrator.gateway is self.orch.gateway  # wires bridge

        sess, tid = self._session_with_task()
        outcome: dict = {}

        def run() -> None:
            try:
                outcome["result"] = self.orch.gateway.invoke(
                    task_id=tid,
                    attempt_id="att-1",
                    step_id="step-1",
                    sequence_in_step=0,
                    tool_id="deploy",
                    args={"target": "staging"},
                    workspace_dir=self.ws,
                )
            except BaseException as exc:  # noqa: BLE001
                outcome["error"] = exc

        thread = threading.Thread(target=run, daemon=True)
        self.server._turn_threads.append(thread)
        thread.start()
        capture.wait_for_request()
        assert thread.is_alive()

        start = time.monotonic()
        self.server._shutdown_orderly("interrupt", join_timeout=2.0)
        elapsed = time.monotonic() - start

        assert not thread.is_alive()
        assert elapsed < 3.0
        assert isinstance(outcome.get("error"), PolicyBlockedError)

    def test_shutdown_joins_turn_threads(self) -> None:
        done = threading.Event()

        def worker() -> None:
            done.wait(2.0)

        thread = threading.Thread(target=worker, daemon=True)
        self.server._turn_threads.append(thread)
        thread.start()
        done.set()
        self.server._shutdown_orderly("eof", join_timeout=2.0)
        assert not thread.is_alive()

    def test_run_stdio_eof_triggers_shutdown(self) -> None:
        self.server._shutdown_orderly = Mock()
        with patch("sys.stdin", io.StringIO("")):
            self.server.run_stdio()
        self.server._shutdown_orderly.assert_called_once_with("eof")

    def test_run_stdio_interrupt_triggers_shutdown(self) -> None:
        class _InterruptStdin:
            def readline(self) -> str:
                raise KeyboardInterrupt

        self.server._shutdown_orderly = Mock()
        with patch("sys.stdin", _InterruptStdin()):
            self.server.run_stdio()
        self.server._shutdown_orderly.assert_called_once_with("interrupt")


if __name__ == "__main__":
    unittest.main()