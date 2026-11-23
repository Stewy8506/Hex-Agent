"""Unit tests: signed update checks, remote-config merge, and flags singleton.

All envelopes are signed locally with a throwaway Ed25519 key, mirroring
hex-backend's canonical-JSON signing without importing the backend package.
"""

from __future__ import annotations

import base64
import json
import sys
import time
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cloud import flags as cloud_flags
from cloud.remote_config import RemoteConfigClient
from cloud.updater import (
    ComponentUpdaterClient,
    canonical_json_bytes,
    verify_signed_envelope,
)

TEST_KEY = Ed25519PrivateKey.generate()
TEST_KEY_HEX = (
    TEST_KEY.public_key().public_bytes_raw().hex()
)


def _fake_jwt(exp_offset: int) -> str:
    def seg(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode("ascii")

    return f"{seg({'alg': 'EdDSA'})}.{seg({'exp': int(time.time()) + exp_offset})}.sig"


def _sign(payload: dict) -> dict:
    sig = TEST_KEY.sign(canonical_json_bytes(payload))
    b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")
    return {"payload": payload, "signature": f"ed25519:{b64}"}


def _signed_update_response(compatible: bool = True, **overrides) -> dict:
    payload = {
        "channel": "stable",
        "manifest_version": "1",
        "compatible": compatible,
        "mandatory_upgrade_required": overrides.get("mandatory", False),
        "updates": overrides.get(
            "updates",
            [{
                "component": "hex-core",
                "installed_version": "0.21.0",
                "target_version": "0.22.0",
                "mandatory": False,
            }],
        ),
        "rollbacks": overrides.get("rollbacks", []),
        "compatibility_issues": overrides.get("compatibility_issues", []),
    }
    return _sign(payload)


def _signed_config(**overrides) -> dict:
    payload = {
        "feature_flags": overrides.get(
            "feature_flags",
            {"p2p_cross_device_sync": True, "moa_parallel_proposers": True},
        ),
        "active_kill_switches": overrides.get("kill_switches", {}),
        "provider_routing": overrides.get("provider_routing", {"preferred": "gateway"}),
    }
    return _sign(payload)


class EnvelopeVerificationTests(unittest.TestCase):
    def test_valid_envelope_verifies(self) -> None:
        self.assertTrue(verify_signed_envelope(_sign({"a": 1}), TEST_KEY_HEX))

    def test_tampered_payload_fails(self) -> None:
        envelope = _sign({"a": 1})
        envelope["payload"]["a"] = 999
        self.assertFalse(verify_signed_envelope(envelope, TEST_KEY_HEX))

    def test_wrong_key_fails(self) -> None:
        other = Ed25519PrivateKey.generate()
        other_hex = other.public_key().public_bytes_raw().hex()
        self.assertFalse(verify_signed_envelope(_sign({"a": 1}), other_hex))

    def test_malformed_envelope_fails(self) -> None:
        self.assertFalse(verify_signed_envelope({}, TEST_KEY_HEX))
        self.assertFalse(verify_signed_envelope({"payload": {}, "signature": "garbage"}, TEST_KEY_HEX))


class UpdaterClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = ComponentUpdaterClient(TEST_KEY_HEX)
        cloud_flags.reset_for_tests()
        self.addCleanup(cloud_flags.reset_for_tests)

    def test_compatible_response(self) -> None:
        verdict = self.client.evaluate_update_response(_signed_update_response())
        self.assertTrue(verdict["apply_allowed"])
        self.assertFalse(verdict["mandatory_upgrade_required"])
        self.assertEqual(verdict["updates"][0]["component"], "hex-core")

    def test_incompatible_response(self) -> None:
        verdict = self.client.evaluate_update_response(
            _signed_update_response(
                compatible=False, compatibility_issues=[{"component": "hex-core"}]
            )
        )
        self.assertFalse(verdict["apply_allowed"])
        self.assertEqual(verdict["reason"], "cross_component_incompatibility")

    def test_bad_signature_raises(self) -> None:
        envelope = _signed_update_response()
        envelope["signature"] = "ed25519:AAAA"
        with self.assertRaises(ValueError):
            self.client.evaluate_update_response(envelope)

    def test_rollback_checkpoint_roundtrip(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollback.json"
            tracker = ComponentUpdaterClient(TEST_KEY_HEX, rollback_file=path)
            tracker.record_rollback_checkpoint("hex-core", "0.21.0")
            tracker.record_rollback_checkpoint("hex-protocol", "1.0.2")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data, {"hex-core": "0.21.0", "hex-protocol": "1.0.2"})


class RemoteConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        cloud_flags.reset_for_tests()
        self.addCleanup(cloud_flags.reset_for_tests)

    def test_flags_merge_and_kill_switch(self) -> None:
        client = RemoteConfigClient(TEST_KEY_HEX)
        merged = client.apply_signed_config(
            _signed_config(
                feature_flags={
                    "p2p_cross_device_sync": True,
                    "team_primary_host_mesh": True,
                    "moa_parallel_proposers": True,
                },
                kill_switches={"team_primary_host_mesh": {"active": True, "reason": "incident"}},
            ),
            local_denied_features={"p2p_cross_device_sync"},
        )
        self.assertFalse(merged["p2p_cross_device_sync"])  # local denial wins
        self.assertFalse(merged["team_primary_host_mesh"])  # kill switch wins
        self.assertTrue(merged["moa_parallel_proposers"])
        self.assertFalse(client.is_enabled("team_primary_host_mesh"))
        self.assertFalse(client.is_enabled("unknown_flag", default=False))
        self.assertTrue(client.is_enabled("moa_parallel_proposers", default=False))
        self.assertEqual(client.kill_switches["team_primary_host_mesh"]["reason"], "incident")
        self.assertEqual(client.provider_routing["preferred"], "gateway")

    def test_bad_signature_raises(self) -> None:
        client = RemoteConfigClient(TEST_KEY_HEX)
        envelope = _signed_config()
        envelope["payload"]["feature_flags"] = {}
        with self.assertRaises(ValueError):
            client.apply_signed_config(envelope)


class ManagerUpdateConfigTests(unittest.TestCase):
    """CloudAccountManager.check_updates/evaluate_remote_config with a fake transport."""

    def setUp(self) -> None:
        cloud_flags.reset_for_tests()
        self.addCleanup(cloud_flags.reset_for_tests)
        from cloud.session import SESSION_ACCOUNT, KEYCHAIN_SERVICE, CloudAccountManager, MemorySecretStore

        self._cloud_session = __import__("cloud.session", fromlist=["CloudAccountManager"])
        self.store = MemorySecretStore()
        self.manager = CloudAccountManager(base_url="http://backend.test", store=self.store)
        self.store.store(
            KEYCHAIN_SERVICE,
            SESSION_ACCOUNT,
            json.dumps({
                "access_token": _fake_jwt(3600),
                "refresh_token": "refresh",
                "email": "dev@example.com",
                "user_id": "user_1",
                "device_record_id": "dev_1",
                "saved_at": 0,
            }),
        )

        class FakeAuthClient:
            instances: list["FakeAuthClient"] = []

            def __init__(self, base_url: str) -> None:
                self.base_url = base_url
                FakeAuthClient.instances.append(self)

            def fetch_server_pubkey(self) -> dict:
                return {"algorithm": "ed25519", "public_key": TEST_KEY_HEX, "key_id": "k1"}

            def check_updates(self, token, installed_versions, channel, platform_name=None):
                assert token.startswith("access.jwt") or token.count(".") == 2
                assert installed_versions == {"hex-core": "0.21.0"}
                self.last_installed = installed_versions
                return _signed_update_response()

            def evaluate_config(self, token, subscription_tier, channel, profile, beta_opt_in):
                assert subscription_tier == "pro"
                return _signed_config()

        self.FakeAuthClient = FakeAuthClient
        self._orig_client = self._cloud_session.HexAuthClient
        self._cloud_session.HexAuthClient = FakeAuthClient
        self.addCleanup(setattr, self._cloud_session, "HexAuthClient", self._orig_client)

    def test_check_updates_verified_and_evaluated(self) -> None:
        verdict = self.manager.check_updates(installed_versions={"hex-core": "0.21.0"})
        self.assertTrue(verdict["apply_allowed"])
        self.assertEqual(verdict["updates"][0]["target_version"], "0.22.0")

    def test_evaluate_remote_config_publishes_singleton(self) -> None:
        flags = self.manager.evaluate_remote_config(subscription_tier="pro")
        self.assertTrue(flags["p2p_cross_device_sync"])
        active = cloud_flags.remote_config()
        self.assertIsNotNone(active)
        self.assertTrue(cloud_flags.flag_enabled("moa_parallel_proposers"))
        self.assertFalse(cloud_flags.flag_enabled("nonexistent_flag"))


if __name__ == "__main__":
    unittest.main()
