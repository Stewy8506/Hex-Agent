"""Durable, cross-process Coordinator sequence reservation regressions."""

from __future__ import annotations

import base64
import copy
import json
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cloud.team_scope import TeamScopeAllocator, TeamScopeUnavailable
from cloud.updater import canonical_json_bytes, verify_signed_envelope


class TeamScopeAllocatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.private = Ed25519PrivateKey.generate()
        self.public_key_hex = self.private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        ).hex()
        self.lease = {
            "project_session_id": "proj_1",
            "team_id": "team_1",
            "host_epoch": 1,
            "primary_device_record_id": "dev_host",
            "last_committed_turn_seq": 0,
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _signed_lease(self) -> dict:
        signature = base64.urlsafe_b64encode(
            self.private.sign(canonical_json_bytes(self.lease))
        ).rstrip(b"=").decode("ascii")
        return {"payload": copy.deepcopy(self.lease), "signature": f"ed25519:{signature}", "etag": "test"}

    def _allocator(self, directory: Path | None = None) -> TeamScopeAllocator:
        return TeamScopeAllocator(
            directory or self.root,
            local_device_id="dev_host",
            server_public_key_hex=self.public_key_hex,
            lease_provider=self._signed_lease,
        )

    def test_reservation_is_contiguous_persistent_and_single_owner(self) -> None:
        allocator = self._allocator()
        self.assertEqual(allocator.reserve("task_1"), {
            "project_session_id": "proj_1", "host_epoch": 1, "turn_seq": 1,
        })
        with self.assertRaises(TeamScopeUnavailable):
            allocator.reserve("task_2")
        self.assertEqual(allocator.reserve("task_1")["turn_seq"], 1)
        self.assertEqual(allocator.status()["reserved_tasks"], 1)
        self.assertEqual(stat.S_IMODE(allocator.state_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(allocator.root.stat().st_mode), 0o700)

        restarted = self._allocator()
        self.assertEqual(restarted.reserve("task_1")["turn_seq"], 1)
        self.lease["last_committed_turn_seq"] = 1
        self.assertEqual(restarted.reserve("task_2"), {
            "project_session_id": "proj_1", "host_epoch": 1, "turn_seq": 2,
        })
        self.assertEqual(restarted.reserve("task_1")["turn_seq"], 1)

    def test_peer_reservation_returns_lease_and_verifies_exact_ownership(self) -> None:
        allocator = self._allocator()
        reserved = allocator.reserve_with_lease("peer_task")
        scope = reserved["scope"]
        self.assertTrue(verify_signed_envelope(reserved["signed_lease"], self.public_key_hex))
        self.assertTrue(allocator.verify_reservation("peer_task", scope))
        self.assertFalse(allocator.verify_reservation("other_task", scope))
        self.assertFalse(allocator.verify_reservation("peer_task", {**scope, "turn_seq": 2}))
        self.lease["host_epoch"] = 2
        self.assertFalse(allocator.verify_reservation("peer_task", scope))
        self.lease["last_committed_turn_seq"] = 1
        self.assertTrue(allocator.verify_reservation("peer_task", scope))

    def test_concurrent_threads_cannot_share_the_next_sequence(self) -> None:
        allocator = self._allocator()
        barrier = threading.Barrier(2)

        def reserve(task_id: str) -> dict:
            barrier.wait(timeout=2)
            try:
                return {"scope": allocator.reserve(task_id)}
            except TeamScopeUnavailable as exc:
                return {"busy": str(exc)}

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, ["task_1", "task_2"]))
        self.assertEqual(sum("scope" in result for result in results), 1)
        self.assertEqual(sum("busy" in result for result in results), 1)

    def test_separate_processes_share_the_durable_reservation(self) -> None:
        lease_path = self.root / "lease.json"
        lease_path.write_text(json.dumps(self._signed_lease()), encoding="utf-8")
        worker = r'''
import json
import sys
from pathlib import Path
from cloud.team_scope import TeamScopeAllocator, TeamScopeUnavailable
lease = json.loads(Path(sys.argv[2]).read_text())
allocator = TeamScopeAllocator(
    sys.argv[1], local_device_id="dev_host",
    server_public_key_hex=sys.argv[3], lease_provider=lambda: lease,
)
try:
    print(json.dumps({"scope": allocator.reserve(sys.argv[4])}))
except TeamScopeUnavailable as exc:
    print(json.dumps({"busy": str(exc)}))
'''
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", worker, str(self.root), str(lease_path), self.public_key_hex, task_id],
                cwd=Path(__file__).resolve().parents[2], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True,
            )
            for task_id in ("task_1", "task_2")
        ]
        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, stderr)
            results.append(json.loads(stdout))
        self.assertEqual(sum("scope" in result for result in results), 1)
        self.assertEqual(sum("busy" in result for result in results), 1)
        self.assertEqual(self._allocator().status()["reserved_tasks"], 1)

    def test_new_epoch_supersedes_only_uncommitted_scope(self) -> None:
        allocator = self._allocator()
        self.assertEqual(allocator.reserve("task_1")["turn_seq"], 1)
        self.lease["host_epoch"] = 2
        self.assertEqual(allocator.reserve("task_1"), {
            "project_session_id": "proj_1", "host_epoch": 2, "turn_seq": 1,
        })
        self.lease["last_committed_turn_seq"] = 1
        self.assertEqual(allocator.reserve("task_2")["turn_seq"], 2)
        self.assertEqual(allocator.reserve("task_1")["host_epoch"], 2)

    def test_committed_historical_scope_survives_new_epoch_and_active_successor(self) -> None:
        allocator = self._allocator()
        historical = allocator.reserve("task_1")
        self.lease["host_epoch"] = 2
        self.lease["last_committed_turn_seq"] = 1
        self.assertEqual(allocator.reserve("task_2")["turn_seq"], 2)
        self.assertEqual(allocator.reserve("task_1"), historical)

    def test_invalid_or_non_coordinator_leases_fail_closed(self) -> None:
        allocator = self._allocator()
        signed = self._signed_lease()
        signed["payload"]["last_committed_turn_seq"] = 9
        allocator.lease_provider = lambda: signed
        with self.assertRaises(TeamScopeUnavailable):
            allocator.reserve("task_1")

        self.lease["primary_device_record_id"] = "dev_other"
        allocator.lease_provider = self._signed_lease
        with self.assertRaisesRegex(TeamScopeUnavailable, "not the active Coordinator"):
            allocator.reserve("task_1")
        self.assertFalse(allocator.state_path.exists())

    def test_invalid_task_and_corrupt_state_are_rejected(self) -> None:
        allocator = self._allocator()
        for task_id in ("", "x" * 513, None):
            with self.subTest(task_id=task_id), self.assertRaises(ValueError):
                allocator.reserve(task_id)
        allocator.state_path.write_text('{"version":1,"reservations":[],"active":null}', encoding="utf-8")
        with self.assertRaises(ValueError):
            allocator.reserve("task_1")


if __name__ == "__main__":
    unittest.main()
