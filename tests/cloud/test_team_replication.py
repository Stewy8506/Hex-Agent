"""Regression tests for verified team workspace, ledger, proof, and cognition replication."""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cloud.cognition_sync import CognitionSyncStore
from cloud.p2p_transport import PeerIdentity
from cloud.team_mesh import (
    TeamMeshCoordinator,
    TurnCommitBundle,
    sign_turn_commit,
)
from cloud.team_replication import TeamReplicationService
from cloud.updater import canonical_json_bytes
from harness_core.capabilities.proof import SignedProofArtifact, sign_proof_artifact
from harness_core.core.models import TaskSubmitRequest
from harness_core.storage.cow import PublicationConflictError, compute_tree_sha, export_delta_archive
from harness_core.storage.db import LedgerStore


class _ServerKey:
    def __init__(self) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        self._private = Ed25519PrivateKey.generate()
        self.public_key_hex = self._private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        ).hex()

    def lease(self, sequence: int = 0, epoch: int = 1) -> dict:
        payload = {
            "project_session_id": "proj_1",
            "host_epoch": epoch,
            "primary_device_record_id": "dev_host_a",
            "last_committed_turn_seq": sequence,
        }
        signature = base64.urlsafe_b64encode(
            self._private.sign(canonical_json_bytes(payload))
        ).rstrip(b"=").decode("ascii")
        return {"payload": payload, "signature": f"ed25519:{signature}", "etag": "test"}


class TeamReplicationTests(unittest.TestCase):
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
        (self.base / ".env").write_text("AUTHOR_KEY=base-secret\n", encoding="utf-8")
        (self.head / ".env").write_text("AUTHOR_KEY=head-secret\n", encoding="utf-8")
        (self.replica_workspace / "app.py").write_text("value = 1\n", encoding="utf-8")
        (self.replica_workspace / ".env").write_text("REPLICA_KEY=local-secret\n", encoding="utf-8")
        self.signer = PeerIdentity.generate("dev_author")
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
            "Team cognition\n", encoding="utf-8"
        )
        self.service = TeamReplicationService(
            self.root / "replica-data",
            self.replica_workspace,
            self.replica_ledger,
            self.replica_cognition,
        )
        self.server = _ServerKey()
        self.mesh = TeamMeshCoordinator(
            "dev_replica",
            self.server.public_key_hex,
            "proj_1",
            replication_service=self.service,
        )
        self.mesh.apply_signed_host_lease(self.server.lease())
        self.signed, self.payload = self._build_turn()

    def tearDown(self) -> None:
        self.author_ledger.close()
        self.replica_ledger.close()
        self.tmp.cleanup()

    def _build_turn(self, sequence: int = 1, epoch: int = 1) -> tuple:
        task = self.author_ledger.submit_task(TaskSubmitRequest(goal="replicate verified turn"))
        self.author_ledger.record_dialogue_turn(task["task_id"], 0, "user", "private team chat")
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
            proof,
            self.signer,
            project_session_id="proj_1",
            host_epoch=epoch,
            turn_seq=sequence,
        )
        archive = self.root / f"turn-{sequence}-{epoch}.tar.gz"
        archive_sha = export_delta_archive(self.head, archive)
        ledger_delta = self.author_ledger.export_sync_delta(task["task_id"], limit=5000)
        manifest = self.author_cognition.export_manifest()
        bundle = TurnCommitBundle(
            project_session_id="proj_1",
            host_epoch=epoch,
            turn_seq=sequence,
            cow_delta_sha256="sha256:" + archive_sha,
            ledger_chain_tip="sha256:" + event["event_sha256"],
            memory_delta_sha256="sha256:" + manifest["manifest_sha256"],
            proof_merkle_root=signed_proof.proof_merkle_root,
        )
        payload = {
            "version": 1,
            "workspace_archive_base64": base64.b64encode(archive.read_bytes()).decode("ascii"),
            "base_workspace_tree_sha": "sha256:" + compute_tree_sha(self.base),
            "signed_proof": signed_proof.to_dict(),
            "ledger_delta": ledger_delta,
            "cognition_manifest": manifest,
        }
        return sign_turn_commit(bundle, self.signer), payload

    def _accept(self, signed=None, payload=None):
        return self.mesh.accept_turn_commit(
            signed or self.signed,
            {self.signer.device_id: self.signer.signing_public_key},
            sender_device_id="dev_host_a",
            replication_payload=payload if payload is not None else self.payload,
        )

    def test_applies_all_content_then_acks_and_replays_idempotently(self) -> None:
        ack = self._accept()
        self.assertTrue(ack["ack"])
        self.assertEqual(ack["replication_status"], "applied")
        self.assertEqual(self.mesh.last_applied_turn_seq, 1)
        self.assertEqual((self.replica_workspace / "app.py").read_text(), "value = 2\n")
        self.assertEqual(
            (self.replica_workspace / ".env").read_text(),
            "REPLICA_KEY=local-secret\n",
        )
        self.assertEqual(
            (self.replica_cognition.global_home / "USER.md").read_text(),
            "Team cognition\n",
        )
        task_id = self.payload["ledger_delta"]["task"]["task_id"]
        self.assertEqual(
            self.replica_ledger.get_all_dialogue_turns(task_id)[0]["content"],
            "private team chat",
        )
        loaded = self.replica_ledger.get_proof_bundle(task_id)
        self.assertTrue(
            SignedProofArtifact.from_dict(loaded["signed_proof"]).verify(
                self.signer.signing_public_key
            )
        )
        duplicate = self._accept()
        self.assertEqual(duplicate["replication_status"], "unchanged")
        conflicting = copy.deepcopy(self.payload)
        conflicting["base_workspace_tree_sha"] = "sha256:" + "0" * 64
        with self.assertRaises(ValueError):
            self._accept(payload=conflicting)

    def test_lease_does_not_claim_unapplied_turns(self) -> None:
        self.mesh.apply_signed_host_lease(self.server.lease(sequence=4))
        self.assertEqual(self.mesh.last_applied_turn_seq, 0)
        self.assertEqual(self.mesh.committed_turn_seq, 4)
        self.assertEqual(self.mesh.missing_turn_range, (1, 4))

    def test_gap_and_metadata_only_delivery_never_advance_or_ack(self) -> None:
        gap = sign_turn_commit(
            TurnCommitBundle(
                project_session_id="proj_1",
                host_epoch=1,
                turn_seq=2,
                cow_delta_sha256=self.signed.bundle.cow_delta_sha256,
                ledger_chain_tip=self.signed.bundle.ledger_chain_tip,
                memory_delta_sha256=self.signed.bundle.memory_delta_sha256,
                proof_merkle_root=self.signed.bundle.proof_merkle_root,
            ),
            self.signer,
        )
        with self.assertRaises(ValueError):
            self._accept(signed=gap)
        with self.assertRaises(ValueError):
            self.mesh.accept_turn_commit(
                self.signed,
                {self.signer.device_id: self.signer.signing_public_key},
                sender_device_id="dev_host_a",
            )
        self.assertEqual(self.mesh.last_applied_turn_seq, 0)
        self.assertEqual((self.replica_workspace / "app.py").read_text(), "value = 1\n")

    def test_archive_digest_and_forged_proof_are_rejected_before_publication(self) -> None:
        damaged = copy.deepcopy(self.payload)
        damaged["workspace_archive_base64"] = base64.b64encode(b"not the signed archive").decode()
        with self.assertRaises(ValueError):
            self._accept(payload=damaged)
        forged = copy.deepcopy(self.payload)
        forged["signed_proof"]["proof"]["workspace_tree_sha"] = "f" * 64
        with self.assertRaises(ValueError):
            self._accept(payload=forged)
        self.assertEqual(self.mesh.last_applied_turn_seq, 0)
        self.assertEqual((self.replica_workspace / "app.py").read_text(), "value = 1\n")

    def test_credential_archive_is_rejected_and_normal_export_omits_credentials(self) -> None:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            content = b"SECRET=do-not-replicate\n"
            member = tarfile.TarInfo("workspace/.env")
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
        raw = buffer.getvalue()
        payload = copy.deepcopy(self.payload)
        payload["workspace_archive_base64"] = base64.b64encode(raw).decode("ascii")
        bundle = TurnCommitBundle(
            project_session_id="proj_1",
            host_epoch=1,
            turn_seq=1,
            cow_delta_sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
            ledger_chain_tip=self.signed.bundle.ledger_chain_tip,
            memory_delta_sha256=self.signed.bundle.memory_delta_sha256,
            proof_merkle_root=self.signed.bundle.proof_merkle_root,
        )
        with self.assertRaises(ValueError):
            self._accept(signed=sign_turn_commit(bundle, self.signer), payload=payload)
        self.assertFalse((self.replica_workspace / ".env").read_text().startswith("SECRET"))

        link_buffer = io.BytesIO()
        with tarfile.open(fileobj=link_buffer, mode="w:gz") as archive:
            member = tarfile.TarInfo("workspace/credentials-link")
            member.type = tarfile.SYMTYPE
            member.linkname = ".env"
            archive.addfile(member)
        link_raw = link_buffer.getvalue()
        payload["workspace_archive_base64"] = base64.b64encode(link_raw).decode("ascii")
        bundle = TurnCommitBundle(
            project_session_id="proj_1",
            host_epoch=1,
            turn_seq=1,
            cow_delta_sha256="sha256:" + hashlib.sha256(link_raw).hexdigest(),
            ledger_chain_tip=self.signed.bundle.ledger_chain_tip,
            memory_delta_sha256=self.signed.bundle.memory_delta_sha256,
            proof_merkle_root=self.signed.bundle.proof_merkle_root,
        )
        with self.assertRaises(ValueError):
            self._accept(signed=sign_turn_commit(bundle, self.signer), payload=payload)

    def test_tree_hash_and_archive_exclude_local_credentials(self) -> None:
        before = compute_tree_sha(self.head)
        (self.head / ".env").write_text("changed=still-local\n", encoding="utf-8")
        self.assertEqual(compute_tree_sha(self.head), before)
        archive_path = self.root / "credential-safe.tar.gz"
        export_delta_archive(self.head, archive_path)
        with tarfile.open(archive_path, "r:gz") as archive:
            self.assertFalse(any(member.name.endswith(".env") for member in archive))

    def test_concurrent_workspace_conflict_preserves_user_work_until_resolved(self) -> None:
        self.service._base_snapshot("proj_1", "sha256:" + compute_tree_sha(self.base))
        (self.replica_workspace / "app.py").write_text("value = 99\n", encoding="utf-8")
        with self.assertRaises(PublicationConflictError):
            self._accept()
        self.assertEqual((self.replica_workspace / "app.py").read_text(), "value = 99\n")
        self.assertEqual(self.mesh.last_applied_turn_seq, 0)
        (self.replica_workspace / "app.py").write_text("value = 1\n", encoding="utf-8")
        self.assertEqual(self._accept()["replication_status"], "applied")

    def test_failed_later_resource_resumes_after_restart_without_false_ack(self) -> None:
        with patch.object(
            self.replica_cognition,
            "apply_manifest",
            side_effect=RuntimeError("temporary cognition failure"),
        ):
            with self.assertRaises(RuntimeError):
                self._accept()
        self.assertEqual(self.mesh.last_applied_turn_seq, 0)
        self.assertEqual((self.replica_workspace / "app.py").read_text(), "value = 2\n")
        restarted = TeamReplicationService(
            self.root / "replica-data",
            self.replica_workspace,
            self.replica_ledger,
            self.replica_cognition,
        )
        self.mesh.replication_service = restarted
        result = restarted.apply_turn(
            self.signed,
            self.payload,
            {self.signer.device_id: self.signer.signing_public_key},
        )
        self.assertEqual(result["status"], "applied")
        self.assertEqual(
            (self.replica_cognition.global_home / "USER.md").read_text(),
            "Team cognition\n",
        )


if __name__ == "__main__":
    unittest.main()
