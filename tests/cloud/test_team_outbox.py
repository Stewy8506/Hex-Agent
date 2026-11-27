"""Regression tests for durable verified team-turn packaging and delivery."""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import shutil
import stat
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from cloud.cognition_sync import CognitionSyncStore
from cloud.p2p_transport import PeerIdentity
from cloud.team_mesh import TeamMeshCoordinator
from cloud.team_outbox import TeamTurnOutbox, VerifiedTeamTurn
from cloud.team_replication import TeamReplicationService
from cloud.updater import canonical_json_bytes
from harness_core.capabilities.proof import sign_proof_artifact
from harness_core.core.models import TaskSubmitRequest
from harness_core.storage.cow import (
    APFSAndPortableCoWProvider,
    compute_tree_sha,
)
from harness_core.storage.db import LedgerStore


class _ServerKey:
    def __init__(self) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        self._private = Ed25519PrivateKey.generate()
        self.public_key_hex = self._private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        ).hex()

    def lease(self) -> dict:
        payload = {
            "project_session_id": "proj_1",
            "host_epoch": 1,
            "primary_device_record_id": "dev_host_a",
            "last_committed_turn_seq": 0,
        }
        signature = base64.urlsafe_b64encode(
            self._private.sign(canonical_json_bytes(payload))
        ).rstrip(b"=").decode("ascii")
        return {"payload": payload, "signature": f"ed25519:{signature}", "etag": "test"}


class TeamTurnOutboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ledger = LedgerStore(self.root / "author.db")
        self.replica_ledger = LedgerStore(self.root / "replica.db")
        self.cow = APFSAndPortableCoWProvider(self.root / "snapshots")
        self.base = self.root / "base"
        self.head = self.root / "head"
        self.replica = self.root / "replica"
        for directory in (self.base, self.head, self.replica):
            directory.mkdir()
        (self.base / "app.py").write_text("value = 1\n", encoding="utf-8")
        (self.head / "app.py").write_text("value = 2\n", encoding="utf-8")
        (self.base / ".env").write_text("AUTHOR_KEY=base-secret\n", encoding="utf-8")
        (self.head / ".env").write_text("AUTHOR_KEY=head-secret\n", encoding="utf-8")
        (self.replica / "app.py").write_text("value = 1\n", encoding="utf-8")
        (self.replica / ".env").write_text("REPLICA_KEY=local-secret\n", encoding="utf-8")
        self.signer = PeerIdentity.generate("dev_author")
        self.cognition = CognitionSyncStore(
            self.head, global_home=self.root / "author-home", workspace_id="proj_1"
        )
        (self.cognition.global_home / "USER.md").write_text(
            "Team cognition\n", encoding="utf-8"
        )
        self.replica_cognition = CognitionSyncStore(
            self.replica,
            global_home=self.root / "replica-home",
            workspace_id="proj_1",
            ledger=self.replica_ledger,
        )
        self.service = TeamReplicationService(
            self.root / "replica-data", self.replica, self.replica_ledger, self.replica_cognition
        )
        self.server = _ServerKey()
        self.mesh = TeamMeshCoordinator(
            "dev_replica", self.server.public_key_hex, "proj_1", replication_service=self.service
        )
        self.mesh.apply_signed_host_lease(self.server.lease())
        self.turn = self._verified_turn()

    def tearDown(self) -> None:
        self.ledger.close()
        self.replica_ledger.close()
        self.tmp.cleanup()

    def _verified_turn(self, *, unchanged: bool = False) -> VerifiedTeamTurn:
        task = self.ledger.submit_task(TaskSubmitRequest(goal="ship verified team turn"))
        self.ledger.record_dialogue_turn(task["task_id"], 0, "user", "private team chat")
        workspace = self.base if unchanged else self.head
        tree = compute_tree_sha(workspace)
        self.ledger.record_event(
            task["task_id"],
            "TURN_VERIFIED",
            reason_code="TEAM_TURN",
            attempt_id="att_1",
            workspace_tree_sha=tree,
            payload={"verified": True},
        )
        proof = self.ledger.save_proof_bundle(
            task_id=task["task_id"],
            attempt_id="att_1",
            git_diff_sha=hashlib.sha256(b"").hexdigest(),
            git_diff_text="",
            workspace_tree_sha=tree,
            runtime_digest="b" * 64,
            ast_impact={"modified_files": [] if unchanged else ["app.py"]},
            verification_results={"passed": True, "exit_code": 0},
        )
        return VerifiedTeamTurn(
            task_id=task["task_id"],
            attempt_id="att_1",
            workspace_dir=workspace,
            base_snapshot_path=self.base,
            proof=proof,
        )

    def _outbox(self, publisher, scope_provider=None) -> TeamTurnOutbox:
        return TeamTurnOutbox(
            self.root / "outbox-data",
            ledger=self.ledger,
            cow=self.cow,
            cognition=self.cognition,
            signer=self.signer,
            scope_provider=scope_provider or (
                lambda _task_id: {
                    "project_session_id": "proj_1", "host_epoch": 1, "turn_seq": 1
                }
            ),
            publisher=publisher,
        )

    def _pending_path(self, turn: VerifiedTeamTurn) -> Path:
        identity = hashlib.sha256((turn.task_id + "\0" + turn.attempt_id).encode()).hexdigest()
        return self.root / "outbox-data" / "team_outbox" / "pending" / f"{identity}.json"

    def test_failed_delivery_resumes_identical_signed_turn_after_restart(self) -> None:
        captured: dict[str, object] = {}

        def fail(signed, payload):
            captured["signed"] = copy.deepcopy(signed.to_dict())
            captured["payload"] = copy.deepcopy(payload)
            raise ConnectionError("offline")

        outbox = self._outbox(fail)
        self.assertEqual(outbox(self.turn)["status"], "queued")
        self.assertEqual(outbox.pending_count, 1)
        pending = self._pending_path(self.turn)
        self.assertEqual(stat.S_IMODE(pending.stat().st_mode), 0o600)
        archive = base64.b64decode(captured["payload"]["workspace_archive_base64"])
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
            self.assertFalse(any(".env" in member.name for member in bundle.getmembers()))
        shutil.rmtree(self.head)

        delivered = []

        def publish(signed, payload):
            delivered.append((signed.to_dict(), copy.deepcopy(payload)))
            return self.mesh.accept_turn_commit(
                signed,
                {self.signer.device_id: self.signer.signing_public_key},
                sender_device_id="dev_host_a",
                replication_payload=payload,
            )

        restarted = self._outbox(publish)
        self.assertEqual(restarted.flush(), {"delivered": 1, "queued": 0})
        self.assertEqual(restarted.pending_count, 0)
        self.assertEqual(delivered, [(captured["signed"], captured["payload"])])
        history = restarted.history_range("proj_1", 1, 1)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["signed"], captured["signed"])
        self.assertEqual(history[0]["payload"], captured["payload"])
        self.assertEqual(stat.S_IMODE(restarted.history_dir.joinpath(history[0]["turn_id"] + ".json").stat().st_mode), 0o600)
        self.assertEqual((self.replica / "app.py").read_text(), "value = 2\n")
        self.assertEqual((self.replica / ".env").read_text(), "REPLICA_KEY=local-secret\n")
        self.assertEqual(self.replica_cognition.global_home.joinpath("USER.md").read_text(), "Team cognition\n")

    def test_prepared_turn_survives_scope_failure_before_signing(self) -> None:
        def unavailable(_task_id):
            raise ConnectionError("coordinator offline")

        outbox = self._outbox(Mock(return_value=True), unavailable)
        with self.assertRaises(ConnectionError):
            outbox(self.turn)
        record = json.loads(self._pending_path(self.turn).read_text())
        self.assertEqual(record["status"], "prepared")
        self.assertNotIn("signed", record)
        shutil.rmtree(self.head)

        publisher = Mock(return_value=True)
        restarted = self._outbox(publisher)
        self.assertEqual(restarted.flush(), {"delivered": 1, "queued": 0})
        publisher.assert_called_once()
        attached = self.ledger.get_proof_bundle(self.turn.task_id)
        self.assertTrue(attached["signed_proof"]["signature"].startswith("ed25519:"))

    def test_no_workspace_change_is_skipped_without_signing_or_delivery(self) -> None:
        turn = self._verified_turn(unchanged=True)
        publisher = Mock(return_value=True)
        outbox = self._outbox(publisher)
        self.assertEqual(outbox(turn), {"status": "skipped", "reason": "NO_WORKSPACE_CHANGES"})
        publisher.assert_not_called()
        self.assertEqual(outbox.pending_count, 0)
        self.assertIsNone(self.ledger.get_proof_bundle(turn.task_id).get("signed_proof"))

    def test_tampered_pending_payload_is_never_delivered(self) -> None:
        outbox = self._outbox(Mock(side_effect=ConnectionError("offline")))
        self.assertEqual(outbox(self.turn)["status"], "queued")
        pending = self._pending_path(self.turn)
        record = json.loads(pending.read_text())
        encoded = record["payload"]["workspace_archive_base64"]
        replacement = "A" if encoded[0] != "A" else "B"
        record["payload"]["workspace_archive_base64"] = replacement + encoded[1:]
        pending.write_text(json.dumps(record), encoding="utf-8")

        publisher = Mock(return_value=True)
        restarted = self._outbox(publisher)
        self.assertEqual(restarted.flush(), {"delivered": 0, "queued": 1})
        publisher.assert_not_called()
        self.assertEqual(restarted.pending_count, 1)

    def test_history_retention_keeps_the_newest_exact_turn(self) -> None:
        outbox = TeamTurnOutbox(
            self.root / "outbox-data",
            ledger=self.ledger,
            cow=self.cow,
            cognition=self.cognition,
            signer=self.signer,
            scope_provider=lambda _task_id: {
                "project_session_id": "proj_1", "host_epoch": 1, "turn_seq": 1
            },
            publisher=Mock(return_value=True),
            history_limit=1,
        )
        self.assertEqual(outbox(self.turn)["status"], "delivered")
        second = self._verified_turn()
        outbox.scope_provider = lambda _task_id: {
            "project_session_id": "proj_1", "host_epoch": 1, "turn_seq": 2
        }
        self.assertEqual(outbox(second)["status"], "delivered")
        history = outbox.history_range("proj_1", 1, 2)
        self.assertEqual([item["signed"]["bundle"]["turn_seq"] for item in history], [2])
        self.assertEqual(outbox.history_range("proj_1", 1, 1), [])

    def test_flush_reports_offline_delivery_as_queued(self) -> None:
        outbox = self._outbox(Mock(side_effect=ConnectionError("offline")))
        self.assertEqual(outbox(self.turn)["status"], "queued")
        restarted = self._outbox(Mock(side_effect=ConnectionError("still offline")))
        self.assertEqual(restarted.flush(), {"delivered": 0, "queued": 1})
        self.assertEqual(restarted.pending_count, 1)

    def test_unaccepted_transport_result_remains_durably_queued(self) -> None:
        outbox = self._outbox(Mock(return_value={"accepted": False}))
        self.assertEqual(outbox(self.turn)["status"], "queued")
        self.assertEqual(outbox.pending_count, 1)
        record = json.loads(self._pending_path(self.turn).read_text())
        self.assertEqual(record["attempts"], 1)
        self.assertEqual(record["last_error_type"], "RuntimeError")


if __name__ == "__main__":
    unittest.main()
