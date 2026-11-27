"""Cross-repo e2e: cloud.session against a live in-process hex-backend.

Skipped unless the sibling hex-backend checkout and uvicorn are importable —
run with the backend repo's dev environment installed to exercise the real
HTTP wire path: register → login → challenge enrollment → device list →
token rotation → revocation.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import importlib.util
import urllib.request
import uuid
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from cloud.session import (
    SESSION_ACCOUNT,
    KEYCHAIN_SERVICE,
    CloudAccountManager,
    MemorySecretStore,
)

_BACKEND_ROOT = _REPO_ROOT.parent / "hex-backend"
sys.path.insert(0, str(_BACKEND_ROOT / "src"))

_HAS_BACKEND = (_BACKEND_ROOT / "src" / "hex_backend").is_dir()
_HAS_UVICORN = importlib.util.find_spec("uvicorn") is not None


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class LiveBackend:
    """Boots create_app() under uvicorn on an ephemeral port with temp storage."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        with patch.dict(
            os.environ,
            {
                "HEX_BACKEND_DB": ":memory:",
                "HEX_SIGNING_KEY_PATH": str(tmp / "import-app-signing.pem"),
            },
        ):
            from hex_backend.api.app import app as import_time_app
            from hex_backend.api.app import create_app
            from hex_backend.config import Settings

        import_time_app.state.store.close()
        settings = Settings(
            db_path=tmp / "e2e.db",
            manifests_dir=_BACKEND_ROOT / "manifests",
            telemetry_dir=tmp / "telemetry",
            signing_key_path=tmp / "server-signing.pem",
            inference_gateway_enabled=False,
        )
        self.app = create_app(settings)
        self.port = _free_port()
        import uvicorn

        config = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/health", timeout=1
                ) as resp:
                    if resp.status == 200:
                        return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("hex-backend did not become healthy in time")

    def close(self) -> None:
        try:
            self.app.state.store.close()
        except Exception:
            pass
        self.server.should_exit = True
        self.thread.join(timeout=10)
        try:
            self._tmp.cleanup()
        except OSError:
            pass


@unittest.skipUnless(_HAS_BACKEND and _HAS_UVICORN, "hex_backend + uvicorn not installed")
class BackendIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.backend = LiveBackend()
        cls.base_url = f"http://127.0.0.1:{cls.backend.port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.backend.close()

    def setUp(self) -> None:
        self.manager = CloudAccountManager(base_url=self.base_url, store=MemorySecretStore())
        self.email = f"dev-{uuid.uuid4().hex[:10]}@example.com"
        self.password = "correct-horse-battery"

    def test_full_account_lifecycle(self) -> None:
        registered = self.manager.register(self.email, self.password)
        self.assertIn("user_id", registered)

        first = self.manager.login(self.email, self.password)
        self.assertTrue(first["logged_in"])
        self.assertEqual(first["email"], self.email)
        self.assertTrue(first["device_enrolled"])
        device_id = first["device_record_id"]
        self.assertTrue(device_id.startswith("dev_"))

        # Server-side server pubkey endpoint must be reachable for clients that
        # verify signed envelopes (remote config, releases).
        with urllib.request.urlopen(f"{self.base_url}/v1/server/pubkey", timeout=5) as resp:
            pubkey_doc = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(pubkey_doc["algorithm"], "ed25519")
        self.assertIn("public_key", pubkey_doc)
        self.assertIn("key_id", pubkey_doc)

        devices = self.manager.list_devices()
        self.assertEqual([d["device_record_id"] for d in devices], [device_id])
        self.assertEqual(devices[0]["trust_state"], "trusted")

        self.manager.logout()
        self.assertFalse(self.manager.status()["logged_in"])

        # Re-login reuses the enrolled device instead of stacking records.
        second = self.manager.login(self.email, self.password)
        self.assertEqual(second["device_record_id"], device_id)
        self.assertEqual(len(self.manager.list_devices()), 1)

        # Revoking the local device must clear the local session.
        result = self.manager.revoke_device(device_id)
        self.assertTrue(result["revoked"])
        from cloud.session import CloudAccountError

        with self.assertRaises(CloudAccountError):
            self.manager.list_devices()  # session was cleared by self-revocation
        self.assertFalse(self.manager.status()["logged_in"])
        self.assertFalse(self.manager.status()["device_enrolled"])

    def test_expired_access_token_refreshes_against_backend(self) -> None:
        import base64 as _b64

        self.manager.register(self.email, self.password)
        self.manager.login(self.email, self.password)
        session = json.loads(self.manager._store.get(KEYCHAIN_SERVICE, SESSION_ACCOUNT))
        header = _b64.urlsafe_b64encode(
            json.dumps({"alg": "EdDSA", "typ": "JWT"}).encode()
        ).rstrip(b"=").decode()
        payload = _b64.urlsafe_b64encode(
            json.dumps({"exp": int(time.time()) - 3600, "sub": "x"}).encode()
        ).rstrip(b"=").decode()
        session["access_token"] = f"{header}.{payload}.sig"
        self.manager._write_blob(SESSION_ACCOUNT, session)

        devices = self.manager.list_devices()
        self.assertEqual(len(devices), 1)  # auto-refresh recovered the call
        rotated = json.loads(self.manager._store.get(KEYCHAIN_SERVICE, SESSION_ACCOUNT))
        self.assertNotEqual(rotated["access_token"], session["access_token"])

    def test_signed_release_check_and_manifest(self) -> None:
        from cloud.updater import verify_signed_envelope

        self.manager.register(self.email, self.password)
        self.manager.login(self.email, self.password)

        verdict = self.manager.check_updates(installed_versions={"hex-core": "0.21.0"})
        self.assertTrue(verdict["apply_allowed"])
        by_component = {u["component"]: u for u in verdict["updates"]}
        self.assertEqual(by_component["hex-core"]["target_version"], "0.22.0")

        yanked = self.manager.check_updates(installed_versions={"hex-core": "0.21.3"})
        self.assertTrue(
            any(rb["rollback_to"] == "0.21.4" for rb in yanked["rollbacks"]),
            yanked["rollbacks"],
        )

        manifest = self.manager.release_manifest("stable")
        self.assertIn("hex-core", manifest["components"])

        # Raw envelope verifies against the published server key, breaks when tampered.
        from cloud.auth_client import HexAuthClient

        envelope = HexAuthClient(self.base_url).get_manifest("stable")
        pubkey = self.manager.server_public_key()
        self.assertTrue(verify_signed_envelope(envelope, pubkey))
        envelope["payload"]["components"] = {}
        self.assertFalse(verify_signed_envelope(envelope, pubkey))

    def test_evaluate_remote_config_publishes_flags(self) -> None:
        from cloud import flags as cloud_flags

        self.manager.register(self.email, self.password)
        self.manager.login(self.email, self.password)
        self.addCleanup(cloud_flags.reset_for_tests)

        flags = self.manager.evaluate_remote_config()
        self.assertIn("inference_gateway", flags)
        self.assertEqual(cloud_flags.active_kill_switches(), {})
        self.assertIsNotNone(cloud_flags.remote_config())

    def test_signal_link_connects_to_live_backend(self) -> None:
        from cloud.signaling import P2PSignalingClient, SignalLink

        self.manager.register(self.email, self.password)
        self.manager.login(self.email, self.password)

        signaling = P2PSignalingClient(self.manager.device_record_id())
        link = SignalLink(
            self.base_url,
            signaling,
            token_provider=self.manager.access_token,
            device_id_provider=self.manager.device_record_id,
            reconnect_initial=0.05,
        )
        link.start()
        try:
            deadline = time.time() + 10
            while time.time() < deadline and not link.status()["connected"]:
                time.sleep(0.05)
            status = link.status()
            self.assertTrue(status["connected"], status)
            self.assertEqual(status["attempts"], 0)

            # Outbound frames are accepted by the real server socket.
            self.assertTrue(link.send_turn_ack("proj_e2e", 1))
            self.assertTrue(link.send_p2p_signal("dev_nowhere", {"hello": True}))
        finally:
            link.stop()
        self.assertFalse(link.status()["running"])


if __name__ == "__main__":
    unittest.main()
