"""Regression tests for device-signed, secret-redacted proof artifacts."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from harness_core.capabilities.proof import (
    PROOF_LEAF_DOMAIN,
    PROOF_NODE_DOMAIN,
    PROOF_SIGNING_DOMAIN,
    SignedProofArtifact,
    compute_proof_merkle_root,
    redact_proof_diff,
    sign_proof_artifact,
    VerificationAndProofService,
)
from cloud.p2p_transport import PeerIdentity
from harness_core.core.models import TaskSubmitRequest
from harness_core.storage.db import LedgerStore


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class TestSignedProofArtifact(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = LedgerStore(Path(self.tmp.name) / "ledger.db")
        task = self.ledger.submit_task(TaskSubmitRequest(goal="proof signing"))
        self.proof = self.ledger.save_proof_bundle(
            task_id=task["task_id"],
            attempt_id="att_1",
            git_diff_sha=hashlib.sha256(b"").hexdigest(),
            git_diff_text="",
            workspace_tree_sha="a" * 64,
            runtime_digest="b" * 64,
            ast_impact={"modified_files": ["app.py"]},
            verification_results={"passed": True, "exit_code": 0},
        )
        self.signer = PeerIdentity.generate("dev_author")

    def tearDown(self) -> None:
        self.ledger.close()
        self.tmp.cleanup()

    def signed(self):
        return sign_proof_artifact(
            self.proof, self.signer, project_session_id="proj_1", host_epoch=3, turn_seq=9
        )

    def test_signature_domain_and_merkle_root_are_deterministic(self) -> None:
        signed = self.signed()
        payload = signed.to_dict()
        unsigned = {key: value for key, value in payload.items() if key != "signature"}
        raw = PROOF_SIGNING_DOMAIN + canonical(unsigned)
        signature = base64.urlsafe_b64decode(signed.signature.split(":", 1)[1] + "==")
        public = bytes.fromhex(self.signer.signing_public_key.split(":", 1)[1])
        Ed25519PublicKey.from_public_bytes(public).verify(signature, raw)

        leaves = [
            hashlib.sha256(PROOF_LEAF_DOMAIN + canonical({name: self.proof[name]})).digest()
            for name in sorted(self.proof)
            if not name.endswith("_json") and name != "signed_proof"
        ]
        while len(leaves) > 1:
            if len(leaves) % 2:
                leaves.append(leaves[-1])
            leaves = [
                hashlib.sha256(PROOF_NODE_DOMAIN + leaves[index] + leaves[index + 1]).digest()
                for index in range(0, len(leaves), 2)
            ]
        self.assertEqual(signed.proof_merkle_root, "sha256:" + leaves[0].hex())
        self.assertEqual(compute_proof_merkle_root(self.proof), signed.proof_merkle_root)
        self.assertTrue(SignedProofArtifact.from_dict(payload).verify(self.signer.signing_public_key))

    def test_tampering_and_forged_author_are_rejected(self) -> None:
        signed = self.signed()
        for mutate in (
            lambda value: value["proof"].__setitem__("workspace_tree_sha", "c" * 64),
            lambda value: value["proof"]["verification_results"].__setitem__("passed", False),
            lambda value: value.__setitem__("turn_seq", 10),
        ):
            payload = copy.deepcopy(signed.to_dict())
            mutate(payload)
            try:
                parsed = SignedProofArtifact.from_dict(payload)
            except ValueError:
                continue
            self.assertFalse(parsed.verify(self.signer.signing_public_key))
        stranger = PeerIdentity.generate("dev_stranger")
        self.assertFalse(signed.verify(stranger.signing_public_key))
        forged_author = signed.to_dict()
        forged_author["author_device_record_id"] = "dev_other"
        self.assertFalse(
            SignedProofArtifact.from_dict(forged_author).verify(self.signer.signing_public_key)
        )
        with self.assertRaises(ValueError):
            SignedProofArtifact.from_dict({**signed.to_dict(), "extra": True})

    def test_signed_proof_persists_and_reloads_from_ledger(self) -> None:
        signed = self.signed()
        attached = self.ledger.attach_signed_proof(self.proof["proof_id"], signed.to_dict())
        self.assertEqual(attached["ledger_signature"], signed.signature)
        loaded = self.ledger.get_proof_bundle(self.proof["task_id"])
        self.assertEqual(loaded["signed_proof"], signed.to_dict())
        self.assertTrue(
            SignedProofArtifact.from_dict(loaded["signed_proof"]).verify(self.signer.signing_public_key)
        )

        async def reload_async():
            return await self.ledger.get_proof_bundle_async(self.proof["task_id"])

        self.assertEqual(asyncio.run(reload_async())["signed_proof"], signed.to_dict())
        with self.assertRaises(ValueError):
            self.ledger.attach_signed_proof(self.proof["proof_id"], {**signed.to_dict(), "turn_seq": 10})

    def test_signed_proof_import_is_exact_and_idempotent(self) -> None:
        signed = self.signed()
        imported = LedgerStore(Path(self.tmp.name) / "imported.db")
        try:
            imported.merge_remote_chain(
                self.proof["task_id"],
                [],
                task_snapshot=self.ledger.get_task(self.proof["task_id"]),
                sender_device_record_id=self.signer.device_id,
            )
            first = imported.save_signed_proof(signed.to_dict())
            second = imported.save_signed_proof(signed.to_dict())
            self.assertEqual(first, second)
            loaded = imported.get_proof_bundle(self.proof["task_id"])
            self.assertEqual(loaded["signed_proof"], signed.to_dict())
            conflicting = sign_proof_artifact(
                {
                    **self.proof,
                    "git_diff_text": "different",
                    "git_diff_sha": hashlib.sha256(b"different").hexdigest(),
                },
                self.signer,
                project_session_id="proj_1",
                host_epoch=3,
                turn_seq=9,
            ).to_dict()
            with self.assertRaises(ValueError):
                imported.save_signed_proof(conflicting)
        finally:
            imported.close()

    def test_credential_file_diffs_and_secret_values_are_redacted(self) -> None:
        diff = (
            "diff -ruN base/.env head/.env\n"
            "--- base/.env\t2026-01-01\n"
            "+++ head/.env\t2026-01-01\n"
            "@@ -1 +1 @@\n-API_KEY=old\n+API_KEY=supersecretvalue\n"
            "diff -ruN base/app.py head/app.py\n"
            "--- base/app.py\t2026-01-01\n"
            "+++ head/app.py\t2026-01-01\n"
            "@@ -1 +1 @@\n-key = 'sk-abcdefghijklmnopqrstuv'\n"
            "+key = 'sk-zyxwvutsrqponmlkjihg'\n"
        )
        redacted = redact_proof_diff(diff)
        self.assertIn("[OMITTED_SENSITIVE_FILE]", redacted)
        self.assertNotIn("supersecretvalue", redacted)
        self.assertNotIn("sk-abcdefghijklmnopqrstuv", redacted)
        self.assertNotIn("sk-zyxwvutsrqponmlkjihg", redacted)
        self.assertEqual(redacted.count("[REDACTED_SECRET]"), 2)

    def test_service_signs_only_successful_redacted_proofs(self) -> None:
        base = Path(self.tmp.name) / "base"
        workspace = Path(self.tmp.name) / "workspace"
        base.mkdir()
        workspace.mkdir()
        (base / "app.py").write_text("key = 'old'\n", encoding="utf-8")
        (workspace / "app.py").write_text("key = 'sk-abcdefghijklmnopqrstuv'\n", encoding="utf-8")
        (workspace / ".env").write_text("API_KEY=supersecretvalue\n", encoding="utf-8")
        gateway = MagicMock()
        gateway._tool_run_tests.return_value = {
            "ok": True,
            "exit_code": 0,
            "output": "token=abcdefghijklmnop",
            "output_sha256": "c" * 64,
        }
        cow = MagicMock()
        cow.compute_tree_sha.return_value = "a" * 64
        cow.backend_name = "portable_copy"
        graphify = MagicMock()
        graphify.compute_blast_radius.return_value.to_dict.return_value = {
            "modified_files": ["app.py"], "impacted_tests": [],
        }
        service = VerificationAndProofService(
            self.ledger, cow, gateway, graphify, Path(self.tmp.name) / "cas"
        )
        result = service.run_verification_and_build_proof(
            task_id=self.proof["task_id"],
            attempt_id="att_signed",
            base_snapshot_path=base,
            workspace_dir=workspace,
            modified_files=["app.py"],
            proof_signer=self.signer,
            project_session_id="proj_1",
            host_epoch=3,
            turn_seq=9,
        )
        proof = result["proof"]
        signed = SignedProofArtifact.from_dict(proof["signed_proof"])
        self.assertTrue(signed.verify(self.signer.signing_public_key))
        serialized = json.dumps(proof)
        self.assertNotIn("supersecretvalue", serialized)
        self.assertNotIn("sk-abcdefghijklmnopqrstuv", serialized)
        self.assertNotIn("abcdefghijklmnop", serialized)
        self.assertIn("[OMITTED_SENSITIVE_FILE]", proof["git_diff_text"])
        self.assertEqual(
            hashlib.sha256(proof["git_diff_text"].encode()).hexdigest(), proof["git_diff_sha"]
        )

        gateway._tool_run_tests.return_value["ok"] = False
        with self.assertRaises(ValueError):
            service.run_verification_and_build_proof(
                task_id=self.proof["task_id"],
                attempt_id="att_failed",
                base_snapshot_path=base,
                workspace_dir=workspace,
                modified_files=["app.py"],
                proof_signer=self.signer,
                project_session_id="proj_1",
                host_epoch=3,
                turn_seq=10,
            )


if __name__ == "__main__":
    unittest.main()
