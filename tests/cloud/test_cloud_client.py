"""Unit & cross-repo contract tests for Harness/cloud/ client modules."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from cloud.auth_client import generate_device_identity
from cloud.remote_config import RemoteConfigClient
from cloud.signaling import P2PSignalingClient
from cloud.team_mesh import TeamMeshCoordinator, TurnCommitBundle
from cloud.updater import ComponentUpdaterClient

SECRET = "harness-cloud-test-secret"


def _sign(payload: dict[str, Any]) -> dict[str, Any]:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sig = base64.urlsafe_b64encode(
        hmac.new(SECRET.encode("utf-8"), raw, hashlib.sha256).digest()
    ).decode("ascii")
    return {"payload": payload, "signature": f"hmac-sha256:{sig}", "etag": "test"}


class HarnessCloudClientTests(unittest.TestCase):
    def test_encrypted_device_identity_never_leaks_plaintext(self) -> None:
        ident = generate_device_identity("user-account-root-key")
        self.assertTrue(ident.encrypted_device_id.startswith("dev_enc_v1:"))
        self.assertNotIn(ident.raw_device_id, ident.encrypted_device_id)
        self.assertTrue(ident.device_pubkey.startswith("ed25519:"))

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
            updater = ComponentUpdaterClient(SECRET, rollback_file=rb_file)

            signed_upd = _sign(
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

            # Tampered signature is rejected.
            signed_upd["payload"]["mandatory_upgrade_required"] = True
            with self.assertRaises(ValueError):
                updater.evaluate_update_response(signed_upd)

            # Remote config honors local security denials over remote enabled flags.
            rc = RemoteConfigClient(SECRET)
            signed_cfg = _sign(
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

    def test_team_mesh_host_epoch_fencing_and_turn_commit_replication(self) -> None:
        mesh = TeamMeshCoordinator("dev_peer_b", SECRET)
        self.assertEqual(mesh.mode, "LOCAL")

        lease_e1 = _sign(
            {
                "project_session_id": "proj_1",
                "host_epoch": 1,
                "primary_device_record_id": "dev_host_a",
                "last_committed_turn_seq": 0,
            }
        )
        self.assertEqual(mesh.apply_signed_host_lease(lease_e1), "TEAM_REPLICA_PEER")

        ack = mesh.accept_turn_commit(
            TurnCommitBundle(
                project_session_id="proj_1",
                host_epoch=1,
                turn_seq=1,
                cow_delta_sha256="sha256:cow1",
                ledger_chain_tip="sha256:tip1",
                memory_delta_sha256="sha256:mem1",
                proof_merkle_root="sha256:proof1",
            )
        )
        self.assertTrue(ack["ack"])
        self.assertEqual(mesh.last_applied_turn_seq, 1)

        # Failover promotes dev_peer_b at epoch 2.
        lease_e2 = _sign(
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
