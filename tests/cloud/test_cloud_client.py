"""Unit & cross-repo contract tests for Harness/cloud/ client modules."""

from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from cloud.auth_client import (
    DeviceIdentity,
    encrypt_device_id,
    enrollment_signing_payload,
    generate_device_identity,
    sign_enrollment_challenge,
)
from cloud.remote_config import RemoteConfigClient
from cloud.p2p_transport import PeerIdentity
from cloud.signaling import P2PSignalingClient
from cloud.team_mesh import (
    SignedTurnCommitBundle,
    TeamMeshCoordinator,
    TurnCommitBundle,
    sign_turn_ack,
    sign_turn_commit,
)
from cloud.updater import ComponentUpdaterClient, canonical_json_bytes, verify_signed_envelope


class _TestServerKey:
    """Stands in for hex-backend's Ed25519 identity when signing test envelopes."""

    def __init__(self) -> None:
        self._private = Ed25519PrivateKey.generate()
        public_raw = self._private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        self.public_key_hex = public_raw.hex()

    def sign_envelope(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw = canonical_json_bytes(payload)
        sig = base64.urlsafe_b64encode(self._private.sign(raw)).rstrip(b"=").decode("ascii")
        return {"payload": payload, "signature": f"ed25519:{sig}", "etag": "test"}


class _RecordingReplication:
    def apply_turn(self, signed, payload, keys):
        if not payload:
            raise ValueError("missing content")
        return {"status": "applied"}


class HarnessCloudClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _TestServerKey()

    def test_encrypted_device_identity_never_leaks_plaintext(self) -> None:
        ident = generate_device_identity("user-account-root-key")
        self.assertTrue(ident.encrypted_device_id.startswith("dev_enc_v1:"))
        self.assertNotIn(ident.raw_device_id, ident.encrypted_device_id)
        self.assertTrue(ident.device_pubkey.startswith("ed25519:"))
        self.assertEqual(len(ident.device_pubkey.split(":", 1)[1]), 64)
        self.assertIsNotNone(ident.private_key)

    def test_device_enrollment_signature_matches_backend_canonicalization(self) -> None:
        """Cross-repo contract: signature verifies over hex-backend's exact canonical payload."""
        ident = generate_device_identity("k")
        challenge = "hex_ch_unit_test_challenge"
        signature_b64 = sign_enrollment_challenge(ident, challenge)

        # This mirrors hex_backend.devices.service.enrollment_payload + canonical_json_bytes.
        backend_payload = json.dumps(
            {
                "challenge": challenge,
                "device_pubkey": ident.device_pubkey,
                "encrypted_device_id": ident.encrypted_device_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

        public_raw = bytes.fromhex(ident.device_pubkey.split(":", 1)[1])
        verifying_key = Ed25519PublicKey.from_public_bytes(public_raw)
        verifying_key.verify(base64.urlsafe_b64decode(signature_b64 + "==" ), backend_payload)

        # Tampered payload must not verify.
        with self.assertRaises(Exception):
            verifying_key.verify(
                base64.urlsafe_b64decode(signature_b64 + "=="),
                backend_payload.replace(b'"challenge"', b'"chalenge"'),
            )

    def test_enrollment_requires_locally_generated_private_key(self) -> None:
        ident = DeviceIdentity(
            raw_device_id="host:abc",
            encrypted_device_id=encrypt_device_id("host:abc", "k"),
            device_pubkey="ed25519:" + "00" * 32,
            platform_name="test",
            private_key=None,
        )
        with self.assertRaises(ValueError):
            sign_enrollment_challenge(ident, "hex_ch_x")

    def test_signaling_tracks_peers_kill_switches_and_revocation(self) -> None:
        sig = P2PSignalingClient("dev_local")
        relayed: list[tuple[str, dict[str, Any]]] = []
        sig.on_p2p_signal(lambda sender, payload: relayed.append((sender, payload)))

        sig.handle_server_event(
            {
                "type": "welcome",
                "online_peers": [
                    {"device_record_id": "dev_peer", "user_id": "usr_1", "device_pubkey": "ed25519:p"}
                ],
            }
        )
        self.assertIn("dev_peer", sig.online_peers)

        sig.handle_server_event(
            {"type": "p2p_signal", "from_device_id": "dev_peer", "payload": {"ice": "udp:1.2.3.4"}}
        )
        self.assertEqual(len(relayed), 1)

        sig.handle_server_event(
            {"type": "kill_switch_updated", "event": {"switch": "cloud_task_workers", "active": True}}
        )
        self.assertIn("cloud_task_workers", sig.active_kill_switches)

        sig.handle_server_event({"type": "device_revoked", "device_record_id": "dev_local"})
        self.assertTrue(sig.revoked)
        self.assertEqual(sig.online_peers, {})

    def test_updater_and_remote_config_verify_signatures_and_local_denials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rb_file = Path(tmp) / "rollback.json"
            updater = ComponentUpdaterClient(self.server.public_key_hex, rollback_file=rb_file)

            signed_upd = self.server.sign_envelope(
                {
                    "compatible": True,
                    "mandatory_upgrade_required": False,
                    "updates": [{"component": "hex-core", "target_version": "0.23.0"}],
                    "rollbacks": [],
                }
            )
            decision = updater.evaluate_update_response(signed_upd)
            self.assertTrue(decision["apply_allowed"])
            updater.record_rollback_checkpoint("hex-core", "0.22.0")
            self.assertEqual(json.loads(rb_file.read_text())["hex-core"], "0.22.0")

            # Tampered payload is rejected.
            signed_upd["payload"]["mandatory_upgrade_required"] = True
            with self.assertRaises(ValueError):
                updater.evaluate_update_response(signed_upd)

            # A foreign server key is rejected.
            stranger = _TestServerKey()
            with self.assertRaises(ValueError):
                ComponentUpdaterClient(stranger.public_key_hex).evaluate_update_response(
                    self.server.sign_envelope({"compatible": True})
                )

            # Remote config honors local security denials over remote enabled flags.
            rc = RemoteConfigClient(self.server.public_key_hex)
            signed_cfg = self.server.sign_envelope(
                {
                    "feature_flags": {
                        "p2p_cross_device_sync": True,
                        "cloud_task_workers": True,
                    },
                    "active_kill_switches": {},
                    "provider_routing": {"mode": "byok_direct"},
                }
            )
            rc.apply_signed_config(signed_cfg, local_denied_features={"cloud_task_workers"})
            self.assertTrue(rc.is_enabled("p2p_cross_device_sync"))
            self.assertFalse(rc.is_enabled("cloud_task_workers"))

    def test_signed_turn_ack_matches_backend_domain_and_rejects_tampering(self) -> None:
        signer = PeerIdentity.generate("dev_peer_a")
        ack = sign_turn_ack(signer, "proj_1", 2, 7, "a" * 64)
        payload = {key: value for key, value in ack.items() if key != "signature"}
        raw = b"hex/turn-ack/v1\0" + canonical_json_bytes(payload)
        signature = base64.urlsafe_b64decode(ack["signature"].split(":", 1)[1] + "==")
        public_raw = bytes.fromhex(signer.signing_public_key.split(":", 1)[1])
        Ed25519PublicKey.from_public_bytes(public_raw).verify(signature, raw)
        with self.assertRaises(InvalidSignature):
            Ed25519PublicKey.from_public_bytes(public_raw).verify(
                signature, raw.replace(b'"turn_seq":7', b'"turn_seq":8')
            )
        with self.assertRaises(ValueError):
            sign_turn_ack(signer, "proj_1", 2, 0, "a" * 64)

    def test_team_mesh_host_epoch_fencing_and_turn_commit_replication(self) -> None:
        mesh = TeamMeshCoordinator(
            "dev_peer_b",
            self.server.public_key_hex,
            "proj_1",
            replication_service=_RecordingReplication(),
        )
        self.assertEqual(mesh.mode, "LOCAL")

        lease_e1 = self.server.sign_envelope(
            {
                "project_session_id": "proj_1",
                "host_epoch": 1,
                "primary_device_record_id": "dev_host_a",
                "last_committed_turn_seq": 0,
            }
        )
        self.assertEqual(mesh.apply_signed_host_lease(lease_e1), "TEAM_REPLICA_PEER")

        author = PeerIdentity.generate("dev_peer_a")
        bundle = TurnCommitBundle(
            project_session_id="proj_1",
            host_epoch=1,
            turn_seq=1,
            cow_delta_sha256="sha256:" + "a" * 64,
            ledger_chain_tip="sha256:" + "b" * 64,
            memory_delta_sha256="sha256:" + "c" * 64,
            proof_merkle_root="sha256:" + "d" * 64,
        )
        signed = sign_turn_commit(bundle, author)
        keys = {author.device_id: author.signing_public_key}
        ack = mesh.accept_turn_commit(
            signed, keys, sender_device_id="dev_host_a", replication_payload={"content": True}
        )
        self.assertTrue(ack["ack"])
        self.assertEqual(ack["author_device_record_id"], author.device_id)
        self.assertEqual(mesh.last_applied_turn_seq, 1)

        forged = signed.to_dict()
        forged["bundle"]["turn_seq"] = 2
        with self.assertRaises(ValueError):
            mesh.accept_turn_commit(SignedTurnCommitBundle.from_dict(forged), keys, sender_device_id="dev_host_a")
        with self.assertRaises(ValueError):
            mesh.accept_turn_commit(signed, keys, sender_device_id=author.device_id)
        with self.assertRaises(ValueError):
            mesh.accept_turn_commit(signed, {}, sender_device_id="dev_host_a")

        # Failover promotes dev_peer_b at epoch 2.
        lease_e2 = self.server.sign_envelope(
            {
                "project_session_id": "proj_1",
                "host_epoch": 2,
                "primary_device_record_id": "dev_peer_b",
                "last_committed_turn_seq": 1,
            }
        )
        self.assertEqual(mesh.apply_signed_host_lease(lease_e2), "TEAM_PRIMARY_HOST")

        # Stale epoch 1 lease is rejected to prevent split-brain.
        with self.assertRaises(ValueError):
            mesh.apply_signed_host_lease(lease_e1)


if __name__ == "__main__":
    unittest.main()
