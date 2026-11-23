"""Unit tests for cloud/session.py CloudAccountManager (keychain-backed account state)."""

from __future__ import annotations

import base64
import io
import json
import time
import unittest
import urllib.error

from cloud.auth_client import generate_device_identity, identity_from_material, identity_material
from cloud.session import (
    IDENTITY_ACCOUNT,
    KEYCHAIN_SERVICE,
    SESSION_ACCOUNT,
    CloudAccountError,
    CloudAccountManager,
    MemorySecretStore,
    _jwt_exp,
)


def _fake_jwt(exp: int) -> str:
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "EdDSA", "typ": "JWT"}).encode("utf-8")
    ).rstrip(b"=").decode("ascii")
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp, "sub": "u_1"}).encode("utf-8")
    ).rstrip(b"=").decode("ascii")
    return f"{header}.{payload}.sig"


class FakeAuthClient:
    instances: list["FakeAuthClient"] = []
    refresh_fails = False

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.calls: list[tuple] = []
        self.rotations = 0
        self.next_device_id = "dev_aaaa000000000001"
        FakeAuthClient.instances.append(self)

    def register(self, email: str, password: str) -> dict:
        self.calls.append(("register", email))
        return {"user_id": "u_1"}

    def login(self, email: str, password: str, device_record_id: str | None = None) -> dict:
        self.calls.append(("login", email, device_record_id))
        return {
            "user": {"email": email, "user_id": "u_1"},
            "access_token": "acc_0",
            "refresh_token": "ref_0",
        }

    def enroll_device(self, access_token: str, identity, host_eligible=True, challenge=None) -> dict:
        self.calls.append(("enroll", identity.device_pubkey))
        assert access_token == "acc_0"
        issued = f"dev_{len(self.calls):016x}"
        self.next_device_id = issued
        return {"device_record_id": issued}

    def refresh(self, refresh_token: str) -> dict:
        self.calls.append(("refresh", refresh_token))
        if self.refresh_fails:
            raise urllib.error.HTTPError(
                self.base_url, 401, "Unauthorized", {},
                io.BytesIO(b'{"detail":"Invalid refresh token."}'),
            )
        self.rotations += 1
        return {"access_token": f"acc_{self.rotations}", "refresh_token": f"ref_{self.rotations}"}

    def list_devices(self, access_token: str, include_revoked: bool = False) -> list[dict]:
        self.calls.append(("list_devices", access_token))
        return [{"device_record_id": "dev_other", "platform": "windows"}]

    def revoke_device(self, access_token: str, device_record_id: str) -> dict:
        self.calls.append(("revoke", device_record_id))
        return {"revoked": True, "device_record_id": device_record_id}


class IdentityMaterialTests(unittest.TestCase):
    def test_roundtrip_preserves_keypair(self) -> None:
        identity = generate_device_identity("k" * 32)
        restored = identity_from_material(identity_material(identity))
        self.assertEqual(identity.device_pubkey, restored.device_pubkey)
        self.assertEqual(identity.encrypted_device_id, restored.encrypted_device_id)
        assert restored.private_key is not None
        sig = restored.private_key.sign(b"payload")
        identity.private_key.public_key().verify(sig, b"payload")

    def test_jwt_exp_decodes_without_verification(self) -> None:
        self.assertEqual(_jwt_exp(_fake_jwt(1234)), 1234)
        self.assertIsNone(_jwt_exp("not-a-jwt"))


class CloudSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemorySecretStore()
        self.manager = CloudAccountManager(base_url="http://127.0.0.1:9999", store=self.store)
        FakeAuthClient.instances = []
        FakeAuthClient.refresh_fails = False
        self._orig_client = None
        import cloud.session as session_mod

        self._session_mod = session_mod
        self._orig_client = session_mod.HexAuthClient
        session_mod.HexAuthClient = FakeAuthClient
        self.addCleanup(setattr, session_mod, "HexAuthClient", self._orig_client)

    @property
    def latest_client(self) -> FakeAuthClient:
        return FakeAuthClient.instances[-1]

    def test_status_logged_out(self) -> None:
        status = self.manager.status()
        self.assertFalse(status["logged_in"])
        self.assertFalse(status["device_enrolled"])
        self.assertEqual(status["base_url"], "http://127.0.0.1:9999")

    def test_register_requires_credentials(self) -> None:
        with self.assertRaises(CloudAccountError):
            self.manager.register("", "")

    def test_login_enrolls_once_then_reuses_device_record(self) -> None:
        first = self.manager.login("dev@example.com", "pw")
        self.assertTrue(first["logged_in"])
        self.assertTrue(first["device_enrolled"])
        self.assertEqual(first["device_record_id"], self.latest_client.next_device_id)

        identity = self.manager._load_identity()
        assert identity is not None
        self.assertEqual(identity["device_record_id"], first["device_record_id"])
        session = self.store.get(KEYCHAIN_SERVICE, SESSION_ACCOUNT)
        assert session is not None
        self.assertIn("acc_0", session)

        second = self.manager.login("dev@example.com", "pw")
        self.assertEqual(second["device_record_id"], first["device_record_id"])
        all_calls = [c for inst in FakeAuthClient.instances for c in inst.calls]
        enroll_calls = [c for c in all_calls if c[0] == "enroll"]
        self.assertEqual(len(enroll_calls), 1)
        login_calls = [c for c in all_calls if c[0] == "login"]
        self.assertEqual(login_calls[-1][2], first["device_record_id"])

    def test_expired_access_token_triggers_refresh(self) -> None:
        self.manager.login("dev@example.com", "pw")
        session = json.loads(self.store.get(KEYCHAIN_SERVICE, SESSION_ACCOUNT))
        session["access_token"] = _fake_jwt(int(time.time()) - 3600)
        self.manager._write_blob(SESSION_ACCOUNT, session)

        devices = self.manager.list_devices()
        self.assertEqual(devices[0]["device_record_id"], "dev_other")
        refreshed = json.loads(self.store.get(KEYCHAIN_SERVICE, SESSION_ACCOUNT))
        self.assertEqual(refreshed["access_token"], "acc_1")
        self.assertIn("refresh", self.latest_client.calls[0])

    def test_refresh_failure_clears_session(self) -> None:
        self.manager.login("dev@example.com", "pw")
        session = json.loads(self.store.get(KEYCHAIN_SERVICE, SESSION_ACCOUNT))
        session["access_token"] = _fake_jwt(int(time.time()) - 3600)
        self.manager._write_blob(SESSION_ACCOUNT, session)
        FakeAuthClient.refresh_fails = True

        with self.assertRaises(CloudAccountError) as ctx:
            self.manager.list_devices()
        self.assertIn("sign in again", str(ctx.exception))
        self.assertIsNone(self.store.get(KEYCHAIN_SERVICE, SESSION_ACCOUNT))

    def test_not_signed_in_guard(self) -> None:
        with self.assertRaises(CloudAccountError):
            self.manager.list_devices()

    def test_logout_forgets_session_keeps_identity(self) -> None:
        self.manager.login("dev@example.com", "pw")
        status = self.manager.logout()
        self.assertFalse(status["logged_in"])
        self.assertTrue(status["device_enrolled"])
        self.assertIsNone(self.store.get(KEYCHAIN_SERVICE, SESSION_ACCOUNT))

    def test_revoke_local_device_clears_session(self) -> None:
        self.manager.login("dev@example.com", "pw")
        device_id = self.manager.status()["device_record_id"]
        result = self.manager.revoke_device(device_id)
        self.assertTrue(result["revoked"])
        self.assertFalse(self.manager.status()["logged_in"])
        self.assertFalse(self.manager.status()["device_enrolled"])
        identity = self.manager._load_identity()
        assert identity is not None
        self.assertIsNone(identity["device_record_id"])


if __name__ == "__main__":
    unittest.main()
