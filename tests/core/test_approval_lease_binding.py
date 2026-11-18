"""B45 regression: approval leases are narrowly bound.

Grants bind to the exact command or tool+arguments, carry task/session scope,
and never treat cwd as authority over arbitrary effects. Bindings survive
ledger persistence and hydration.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness_core.capabilities.gateways.reaction_listener import ApprovalRequest
from harness_core.client.acp_approval import AcpApprovalBridge
from harness_core.core.approval_lease import ApprovalLeaseManager
from harness_core.storage.db import LedgerStore


class TestLeaseBinding(unittest.TestCase):
    def setUp(self) -> None:
        self.mgr = ApprovalLeaseManager()

    def test_session_bound_lease_requires_matching_session(self) -> None:
        lease = self.mgr.grant_lease(command_prefix="pytest", session_id="sess_a")
        matched, lid = self.mgr.check_lease("terminal", {"command": "pytest -v"}, session_id="sess_a")
        self.assertTrue(matched)
        self.assertEqual(lid, lease.lease_id)
        self.assertFalse(self.mgr.check_lease("terminal", {"command": "pytest -v"}, session_id="sess_b")[0])
        self.assertFalse(self.mgr.check_lease("terminal", {"command": "pytest -v"})[0])

    def test_task_bound_lease_requires_matching_task(self) -> None:
        self.mgr.grant_lease(command_prefix="pytest", task_id="tsk_a")
        self.assertTrue(self.mgr.check_lease("terminal", {"command": "pytest"}, task_id="tsk_a")[0])
        self.assertFalse(self.mgr.check_lease("terminal", {"command": "pytest"}, task_id="tsk_b")[0])
        self.assertFalse(self.mgr.check_lease("terminal", {"command": "pytest"})[0])

    def test_cwd_is_not_path_authority(self) -> None:
        with tempfile.TemporaryDirectory() as ws:
            self.mgr.grant_lease(allowed_paths=[ws])
            # A command whose only "path" is cwd gains no authority from it.
            self.assertFalse(
                self.mgr.check_lease("terminal", {"command": "rm -rf /", "cwd": ws})[0]
            )
            # An explicit target path inside the grant still matches.
            self.assertTrue(
                self.mgr.check_lease("write_file", {"path": str(Path(ws) / "a.txt"), "content": "x"})[0]
            )

    def test_exact_command_lease_rejects_modified_arguments(self) -> None:
        self.mgr.grant_lease(command_prefix="git push origin main", match_mode="exact")
        self.assertTrue(self.mgr.check_lease("terminal", {"command": "git push origin main"})[0])
        self.assertFalse(self.mgr.check_lease("terminal", {"command": "git push origin main --force"})[0])
        self.assertFalse(self.mgr.check_lease("terminal", {"command": "git push origin dev"})[0])

    def test_tool_and_fingerprint_binding(self) -> None:
        args = {"target": "prod"}
        self.mgr.grant_lease(
            tool_name="deploy",
            args_fingerprint=self.mgr.fingerprint_args(args),
            task_id="tsk_a",
        )
        self.assertTrue(self.mgr.check_lease("deploy", args, task_id="tsk_a")[0])
        self.assertFalse(self.mgr.check_lease("deploy", {"target": "staging"}, task_id="tsk_a")[0])
        self.assertFalse(self.mgr.check_lease("write_file", args, task_id="tsk_a")[0])


class TestBridgeScopeGrants(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.ledger = LedgerStore(db_path=Path(self.temp_dir) / "ledger.db")
        self.mgr = ApprovalLeaseManager(self.ledger)
        self.bridge = AcpApprovalBridge(
            emit=lambda obj: None,
            session_for_task=lambda tid: "sess_x",
            workspace_root=self.temp_dir,
            lease_manager=self.mgr,
        )

    def tearDown(self) -> None:
        self.ledger.close()
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _request(self, tool: str, args: dict) -> ApprovalRequest:
        return ApprovalRequest(
            request_id="apr_test",
            task_id="tsk_a",
            tool_name=tool,
            arguments=args,
            risk_class="R3",
            blast_radius="test",
        )

    def test_command_grant_binds_full_command_not_basename(self) -> None:
        self.bridge._grant_scope_lease(self._request("terminal", {"command": "git push origin main"}))
        leases = self.mgr.list_active_leases()
        self.assertEqual(len(leases), 1)
        self.assertEqual(leases[0].command_prefix, "git push origin main")
        self.assertEqual(leases[0].match_mode, "exact")
        self.assertEqual(leases[0].session_id, "sess_x")
        self.assertEqual(leases[0].allowed_paths, [])

        self.assertTrue(
            self.mgr.check_lease(
                "terminal", {"command": "git push origin main"}, task_id="tsk_a", session_id="sess_x"
            )[0]
        )
        # Unrelated privileged command with the same binary is not covered.
        self.assertFalse(
            self.mgr.check_lease(
                "terminal", {"command": "git push origin main --force"}, task_id="tsk_a", session_id="sess_x"
            )[0]
        )

    def test_no_command_grant_does_not_cover_workspace(self) -> None:
        self.bridge._grant_scope_lease(self._request("deploy", {"target": "prod"}))
        leases = self.mgr.list_active_leases()
        self.assertEqual(len(leases), 1)
        self.assertEqual(leases[0].allowed_paths, [])
        self.assertEqual(leases[0].tool_name, "deploy")
        self.assertIsNotNone(leases[0].args_fingerprint)

        self.assertTrue(
            self.mgr.check_lease("deploy", {"target": "prod"}, task_id="tsk_a", session_id="sess_x")[0]
        )
        # Later workspace file calls are NOT auto-approved by the grant.
        self.assertFalse(
            self.mgr.check_lease(
                "write_file",
                {"path": str(Path(self.temp_dir) / "anything.txt"), "content": "x"},
                task_id="tsk_a",
                session_id="sess_x",
            )[0]
        )
        self.assertFalse(
            self.mgr.check_lease("deploy", {"target": "staging"}, task_id="tsk_a", session_id="sess_x")[0]
        )

    def test_bindings_survive_ledger_hydration(self) -> None:
        self.bridge._grant_scope_lease(self._request("terminal", {"command": "cargo test --workspace"}))
        mgr2 = ApprovalLeaseManager(self.ledger)
        self.assertEqual(len(mgr2.list_active_leases()), 1)
        self.assertTrue(
            mgr2.check_lease(
                "terminal", {"command": "cargo test --workspace"}, task_id="tsk_a", session_id="sess_x"
            )[0]
        )
        self.assertFalse(
            mgr2.check_lease(
                "terminal", {"command": "cargo test --workspace"}, task_id="tsk_a", session_id="other"
            )[0]
        )
        self.assertFalse(
            mgr2.check_lease("terminal", {"command": "cargo build"}, task_id="tsk_a", session_id="sess_x")[0]
        )


if __name__ == "__main__":
    unittest.main()