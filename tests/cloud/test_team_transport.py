"""End-to-end encrypted team-turn delivery, rejection, and failover catch-up."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from cloud.cognition_sync import CognitionSyncStore
from cloud.p2p_transport import InMemoryNetwork, PeerIdentity
from cloud.team_mesh import (
    TeamMeshCoordinator,
    TurnCommitBundle,
    sign_turn_commit,
    sign_turn_ack,
    verify_turn_ack,
)
from cloud.team_replication import TeamReplicationService
from cloud.team_transport import TeamPeerLink, TeamTransportError, TeamTurnPublisher
from cloud.updater import canonical_json_bytes
from harness_core.capabilities.proof import sign_proof_artifact
from harness_core.core.models import TaskSubmitRequest
from harness_core.storage.cow import compute_tree_sha, export_delta_archive
from harness_core.storage.db import LedgerStore


class _ServerKey:
    def __init__(self) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        self._private = Ed25519PrivateKey.generate()
        self.public_key_hex = self._private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        ).hex()

    def _envelope(self, payload: dict) -> dict:
        signature = base64.urlsafe_b64encode(
            self._private.sign(canonical_json_bytes(payload))
        ).rstrip(b"=").decode("ascii")
        return {"payload": payload, "signature": f"ed25519:{signature}", "etag": "test"}

    def lease(self, sequence: int = 0, primary: str = "dev_host_a", epoch: int = 1) -> dict:
        return self._envelope({
            "project_session_id": "proj_1",
            "host_epoch": epoch,
            "primary_device_record_id": primary,
            "last_committed_turn_seq": sequence,
        })

    def receipt(self, signed, primary: str = "dev_host_a") -> dict:
        bundle = signed.bundle
        return self._envelope({
            "project_session_id": bundle.project_session_id,
            "host_epoch": bundle.host_epoch,
            "primary_device_record_id": primary,
            "author_device_record_id": signed.author_device_record_id,
            "turn_seq": bundle.turn_seq,
            "bundle_digest": bundle.compute_digest(),
            "proof_merkle_root": bundle.proof_merkle_root,
        })


class TeamTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.author_ledger = LedgerStore(self.root / "author.db")
        self.replica_ledger = LedgerStore(self.root / "replica.db")
        self.base = self.root / "base"
        self.head = self.root / "head"
        self.replica_workspace = self.root / "replica-workspace"
        self.base.mkdir()
        self.head.mkdir()
        self.replica_workspace.mkdir()
        (self.base / "app.py").write_text("value = 1\n", encoding="utf-8")
        (self.head / "app.py").write_text("value = 2\n", encoding="utf-8")
        (self.replica_workspace / "app.py").write_text("value = 1\n", encoding="utf-8")
        (self.replica_workspace / ".env").write_text("LOCAL_ONLY=secret\n", encoding="utf-8")
        self.author = PeerIdentity.generate("dev_author")
        self.host = PeerIdentity.generate("dev_host_a")
        self.replica = PeerIdentity.generate("dev_replica")
        self.keys = {
            identity.device_id: identity.signing_public_key
            for identity in (self.author, self.host, self.replica)
        }
        self.author_cognition = CognitionSyncStore(
            self.head, global_home=self.root / "author-home", workspace_id="proj_1"
        )
        self.replica_cognition = CognitionSyncStore(
            self.replica_workspace,
            global_home=self.root / "replica-home",
            workspace_id="proj_1",
            ledger=self.replica_ledger,
        )
        (self.author_cognition.global_home / "USER.md").write_text(
            "Private replicated cognition\n", encoding="utf-8"
        )
        self.service = TeamReplicationService(
            self.root / "replica-data",
            self.replica_workspace,
            self.replica_ledger,
            self.replica_cognition,
        )
        self.server = _ServerKey()
        self.coordinator_mesh = TeamMeshCoordinator(
            self.host.device_id, self.server.public_key_hex, "proj_1"
        )
        self.coordinator_mesh.apply_signed_host_lease(self.server.lease())
        self.replica_mesh = TeamMeshCoordinator(
            self.replica.device_id,
            self.server.public_key_hex,
            "proj_1",
            replication_service=self.service,
        )
        self.replica_mesh.apply_signed_host_lease(self.server.lease())
        self.signed, self.payload = self._build_turn()

    def tearDown(self) -> None:
        self.author_ledger.close()
        self.replica_ledger.close()
        self.tmp.cleanup()

    def _build_turn(self, sequence: int = 1):
        task = self.author_ledger.submit_task(TaskSubmitRequest(goal="transport verified team turn"))
        head_tree = compute_tree_sha(self.head)
        event = self.author_ledger.record_event(
            task["task_id"],
            "TURN_VERIFIED",
            reason_code="TEAM_TURN",
            attempt_id="att_1",
            workspace_tree_sha=head_tree,
            payload={"verified": True},
        )
        proof = self.author_ledger.save_proof_bundle(
            task_id=task["task_id"],
            attempt_id="att_1",
            git_diff_sha=hashlib.sha256(b"").hexdigest(),
            git_diff_text="",
            workspace_tree_sha=head_tree,
            runtime_digest="b" * 64,
            ast_impact={"modified_files": ["app.py"]},
            verification_results={"passed": True, "exit_code": 0},
        )
        signed_proof = sign_proof_artifact(
            proof, self.author, project_session_id="proj_1", host_epoch=1, turn_seq=sequence
        )
        archive = self.root / f"turn-{sequence}.tar.gz"
        archive_sha = export_delta_archive(self.head, archive)
        bundle = TurnCommitBundle(
            project_session_id="proj_1",
            host_epoch=1,
            turn_seq=sequence,
            cow_delta_sha256="sha256:" + archive_sha,
            ledger_chain_tip="sha256:" + event["event_sha256"],
            memory_delta_sha256="sha256:" + self.author_cognition.export_manifest()["manifest_sha256"],
            proof_merkle_root=signed_proof.proof_merkle_root,
        )
        payload = {
            "version": 1,
            "workspace_archive_base64": base64.b64encode(archive.read_bytes()).decode("ascii"),
            "base_workspace_tree_sha": "sha256:" + compute_tree_sha(self.base),
            "signed_proof": signed_proof.to_dict(),
            "ledger_delta": self.author_ledger.export_sync_delta(task["task_id"], limit=5000),
            "cognition_manifest": self.author_cognition.export_manifest(),
        }
        return sign_turn_commit(bundle, self.author), payload

    async def _links(self, *, commit=None, history=None, replica_mesh=None, frames=None):
        def record(frame: bytes) -> bytes:
            if frames is not None:
                frames.append(frame)
            return frame

        network = InMemoryNetwork(frame_transform=record)
        host_endpoint = network.endpoint(self.host)
        replica_endpoint = network.endpoint(self.replica)
        host_session = await host_endpoint.connect(replica_endpoint)
        replica_session = await replica_endpoint.accept()
        mesh = replica_mesh or self.replica_mesh
        coordinator_link = TeamPeerLink(
            host_session,
            self.coordinator_mesh,
            self.host,
            self.keys.get,
            server_public_key_hex=self.server.public_key_hex,
            commit_turn=commit or (lambda _signed: self.server.receipt(_signed)),
            history_provider=history,
            timeout=5,
        )
        acks = []
        replica_link = TeamPeerLink(
            replica_session,
            mesh,
            self.replica,
            self.keys.get,
            ack_sink=lambda ack: acks.append(ack),
            timeout=5,
        )
        await asyncio.gather(coordinator_link.start(), replica_link.start())
        self.addAsyncCleanup(replica_link.close)
        self.addAsyncCleanup(coordinator_link.close)
        return coordinator_link, replica_link, acks

    async def _wait_for(self, predicate) -> None:
        for _ in range(500):
            if predicate():
                return
            await asyncio.sleep(0.01)
        self.fail("Timed out waiting for team transport state.")

    async def test_verified_turn_is_encrypted_chunked_applied_and_signed_acknowledged(self) -> None:
        (self.head / "large.bin").write_bytes(os.urandom(700_000))
        self.signed, self.payload = self._build_turn()
        frames = []
        commits = []

        async def commit(signed):
            commits.append(signed)
            return self.server.receipt(signed)

        coordinator, _, acks = await self._links(commit=commit, frames=frames)
        result = await TeamTurnPublisher([coordinator])(self.signed, self.payload)

        self.assertTrue(result["accepted"])
        self.assertEqual(len(commits), 1)
        self.assertEqual(self.coordinator_mesh.committed_turn_seq, 1)
        self.assertEqual(self.replica_mesh.last_applied_turn_seq, 1)
        self.assertEqual((self.replica_workspace / "app.py").read_text(), "value = 2\n")
        self.assertEqual((self.replica_workspace / ".env").read_text(), "LOCAL_ONLY=secret\n")
        self.assertEqual(
            (self.replica_cognition.global_home / "USER.md").read_text(),
            "Private replicated cognition\n",
        )
        self.assertGreater(coordinator.session.status()["bytes_sent"], 512 * 1024)
        self.assertEqual(len(acks), 1)
        self.assertEqual(
            verify_turn_ack(acks[0], self.replica.signing_public_key)["turn_seq"], 1
        )
        encrypted = b"".join(frames)
        self.assertNotIn(b"Private replicated cognition", encrypted)
        self.assertNotIn(self.payload["workspace_archive_base64"].encode(), encrypted)

    async def test_tampered_content_is_rejected_without_applying_or_acknowledging(self) -> None:
        coordinator, replica, acks = await self._links()
        tampered = copy.deepcopy(self.payload)
        tampered["base_workspace_tree_sha"] = "sha256:" + "0" * 64

        try:
            await coordinator.send_turn(self.signed, tampered, commit=False)
        except TeamTransportError:
            pass
        except Exception as exc:
            self.fail(f"Tampered turn was not explicitly rejected: {exc}; {coordinator.status()}; {replica.status()}")

        self.assertEqual(self.replica_mesh.last_applied_turn_seq, 0)
        self.assertEqual((self.replica_workspace / "app.py").read_text(), "value = 1\n")
        self.assertEqual(acks, [])

    async def test_non_active_coordinator_cannot_relay_content(self) -> None:
        mesh = TeamMeshCoordinator(
            self.replica.device_id,
            self.server.public_key_hex,
            "proj_1",
            replication_service=self.service,
        )
        mesh.apply_signed_host_lease(self.server.lease(primary="dev_other"))
        coordinator, replica, acks = await self._links(replica_mesh=mesh)

        try:
            await coordinator.send_turn(self.signed, self.payload, commit=False)
        except TeamTransportError:
            pass
        except Exception as exc:
            self.fail(f"Untrusted relay was not explicitly rejected: {exc}; {coordinator.status()}; {replica.status()}")

        self.assertEqual(mesh.last_applied_turn_seq, 0)
        self.assertEqual(acks, [])

    async def test_invalid_server_receipt_prevents_all_peer_delivery(self) -> None:
        coordinator, _, _ = await self._links(commit=lambda _signed: {})
        publisher = TeamTurnPublisher([coordinator])

        with self.assertRaises(TeamTransportError):
            await publisher(self.signed, self.payload)

        self.assertEqual(self.coordinator_mesh.committed_turn_seq, 0)
        self.assertEqual(self.replica_mesh.last_applied_turn_seq, 0)

    async def test_failover_catchup_replays_contiguous_history_without_recommit(self) -> None:
        first_signed, first_payload = self.signed, self.payload
        (self.base / "app.py").write_text("value = 2\n", encoding="utf-8")
        (self.head / "app.py").write_text("value = 3\n", encoding="utf-8")
        second_signed, second_payload = self._build_turn(sequence=2)
        catchup_ledger = LedgerStore(self.root / "catchup.db")
        self.addCleanup(catchup_ledger.close)
        catchup_workspace = self.root / "catchup-workspace"
        catchup_workspace.mkdir()
        (catchup_workspace / "app.py").write_text("value = 1\n", encoding="utf-8")
        catchup_cognition = CognitionSyncStore(
            catchup_workspace,
            global_home=self.root / "catchup-home",
            workspace_id="proj_1",
            ledger=catchup_ledger,
        )
        service = TeamReplicationService(
            self.root / "catchup-data", catchup_workspace, catchup_ledger, catchup_cognition
        )
        mesh = TeamMeshCoordinator(
            self.replica.device_id,
            self.server.public_key_hex,
            "proj_1",
            replication_service=service,
        )
        mesh.apply_signed_host_lease(self.server.lease(sequence=2))
        self.coordinator_mesh.apply_signed_host_lease(self.server.lease(sequence=2))
        records = [
            {"signed": signed.to_dict(), "payload": payload}
            for signed, payload in ((first_signed, first_payload), (second_signed, second_payload))
        ]
        commits = []
        coordinator, replica, acks = await self._links(
            commit=lambda signed: commits.append(signed),
            history=lambda _session, first, last: [
                record for record in records
                if first <= record["signed"]["bundle"]["turn_seq"] <= last
            ],
            replica_mesh=mesh,
        )

        try:
            await self._wait_for(lambda: mesh.last_applied_turn_seq == 2)
        except AssertionError:
            self.fail(f"Catch-up stalled: coordinator={coordinator.status()}; replica={replica.status()}; mesh={mesh.__dict__}")
        self.assertEqual((catchup_workspace / "app.py").read_text(), "value = 3\n")
        self.assertEqual([ack["turn_seq"] for ack in acks], [1, 2])
        self.assertEqual(commits, [])
        self.assertTrue(all(verify_turn_ack(ack, self.replica.signing_public_key) for ack in acks))
        self.assertIsNone(mesh.missing_turn_range)
        self.assertEqual(coordinator.mesh.committed_turn_seq, 2)

    async def test_exact_signed_retry_reuses_receipt_and_replica_ack_is_idempotent(self) -> None:
        commits = []

        def commit(signed):
            commits.append(signed)
            return self.server.receipt(signed)

        coordinator, _, acks = await self._links(commit=commit)
        publisher = TeamTurnPublisher([coordinator])
        self.assertTrue((await publisher(self.signed, self.payload))["accepted"])
        self.assertTrue((await publisher(self.signed, self.payload))["accepted"])
        self.assertEqual(len(commits), 2)
        self.assertEqual(self.replica_mesh.last_applied_turn_seq, 1)
        self.assertEqual([ack["turn_seq"] for ack in acks], [1, 1])

    async def test_non_contiguous_sequence_is_rejected_before_backend_submission(self) -> None:
        self.coordinator_mesh.apply_signed_host_lease(self.server.lease(sequence=1))
        commits = []
        coordinator, _, _ = await self._links(commit=lambda signed: commits.append(signed))
        skipped = sign_turn_commit(
            TurnCommitBundle(
                project_session_id="proj_1",
                host_epoch=1,
                turn_seq=3,
                cow_delta_sha256=self.signed.bundle.cow_delta_sha256,
                ledger_chain_tip=self.signed.bundle.ledger_chain_tip,
                memory_delta_sha256=self.signed.bundle.memory_delta_sha256,
                proof_merkle_root=self.signed.bundle.proof_merkle_root,
            ),
            self.author,
        )
        with self.assertRaises(TeamTransportError):
            await coordinator.commit_signed(skipped)
        self.assertEqual(commits, [])

    async def test_pending_historical_turn_verifies_receipt_after_failover(self) -> None:
        self.coordinator_mesh.apply_signed_host_lease(self.server.lease(sequence=1, epoch=2))
        self.replica_mesh.apply_signed_host_lease(self.server.lease(sequence=1, epoch=2))
        commits = []
        coordinator, _, acks = await self._links(
            commit=lambda signed: commits.append(signed) or self.server.receipt(signed)
        )

        result = await TeamTurnPublisher([coordinator])(self.signed, self.payload)

        self.assertTrue(result["accepted"])
        self.assertEqual(commits, [self.signed])
        self.assertEqual(self.replica_mesh.last_applied_turn_seq, 1)
        self.assertEqual([ack["host_epoch"] for ack in acks], [1])

    async def test_conflicting_historical_sequence_is_not_relayed(self) -> None:
        self.coordinator_mesh.apply_signed_host_lease(self.server.lease(sequence=1, epoch=2))
        self.replica_mesh.apply_signed_host_lease(self.server.lease(sequence=1, epoch=2))
        commits = []
        coordinator, _, acks = await self._links(
            commit=lambda signed: commits.append(signed) or self.server.receipt(self.signed)
        )
        conflicting = sign_turn_commit(
            TurnCommitBundle(
                project_session_id="proj_1",
                host_epoch=1,
                turn_seq=1,
                cow_delta_sha256="sha256:" + "a" * 64,
                ledger_chain_tip=self.signed.bundle.ledger_chain_tip,
                memory_delta_sha256=self.signed.bundle.memory_delta_sha256,
                proof_merkle_root=self.signed.bundle.proof_merkle_root,
            ),
            self.author,
        )

        with self.assertRaises(TeamTransportError):
            await coordinator.commit_signed(conflicting)
        self.assertEqual(commits, [conflicting])
        self.assertEqual(acks, [])
        self.assertEqual(self.replica_mesh.last_applied_turn_seq, 0)

    async def test_signed_ack_rejects_changed_binding(self) -> None:
        ack = sign_turn_ack(self.replica, "proj_1", 1, 1, "a" * 64)
        ack["turn_seq"] = 2
        with self.assertRaises(ValueError):
            verify_turn_ack(ack, self.replica.signing_public_key)
