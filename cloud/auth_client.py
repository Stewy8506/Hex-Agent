"""Device identity, encrypted device ID generation, and hex-backend auth client.

Devices hold a real Ed25519 keypair: the public key is registered with
hex-backend, and enrollment proves private-key possession by signing a
single-use server challenge. Designed to stay 100% offline-safe: if
hex-backend is unconfigured or offline, Harness continues running locally.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import platform
import secrets
import socket
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


@dataclass(frozen=True)
class DeviceIdentity:
    raw_device_id: str
    encrypted_device_id: str
    device_pubkey: str
    platform_name: str
    private_key: Ed25519PrivateKey | None = field(default=None, repr=False)


def encrypt_device_id(raw_device_id: str, account_key: str) -> str:
    """Pseudonymize the device identifier client-side so hex-backend stores only ciphertext.

    The backend compares this blob for equality and can never invert it: the
    derivation key never leaves the user's devices.
    """
    nonce = secrets.token_bytes(16)
    key = hashlib.sha256(account_key.encode("utf-8") + nonce).digest()
    pt = raw_device_id.encode("utf-8")
    ct = bytes(b ^ key[i % len(key)] for i, b in enumerate(pt))
    mac = hmac.new(key, nonce + ct, hashlib.sha256).digest()[:16]
    return "dev_enc_v1:" + base64.urlsafe_b64encode(nonce + mac + ct).decode("ascii")


def generate_device_identity(account_key: str) -> DeviceIdentity:
    """Generate a real Ed25519 device identity + encrypted ID ready for OS Keychain storage."""
    host = socket.gethostname()
    raw_id = f"{host}:{secrets.token_hex(12)}"
    private_key = Ed25519PrivateKey.generate()
    public_raw = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return DeviceIdentity(
        raw_device_id=raw_id,
        encrypted_device_id=encrypt_device_id(raw_id, account_key),
        device_pubkey=f"ed25519:{public_raw.hex()}",
        platform_name=platform.system().lower(),
        private_key=private_key,
    )


def enrollment_signing_payload(
    challenge: str, device_pubkey: str, encrypted_device_id: str
) -> bytes:
    """Canonical bytes the device signs; must match hex-backend devices.service.enrollment_payload."""
    return json.dumps(
        {
            "challenge": challenge,
            "device_pubkey": device_pubkey,
            "encrypted_device_id": encrypted_device_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sign_enrollment_challenge(
    identity: DeviceIdentity, challenge: str
) -> str:
    """Sign the server-issued enrollment challenge with the device's Ed25519 private key."""
    if identity.private_key is None:
        raise ValueError("DeviceIdentity has no private key; generate it locally first.")
    raw = enrollment_signing_payload(
        challenge, identity.device_pubkey, identity.encrypted_device_id
    )
    sig = identity.private_key.sign(raw)
    return base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")


def identity_material(identity: DeviceIdentity) -> dict[str, Any]:
    """Serialize a DeviceIdentity (including its private key) for OS Keychain storage."""
    material: dict[str, Any] = {
        "raw_device_id": identity.raw_device_id,
        "encrypted_device_id": identity.encrypted_device_id,
        "device_pubkey": identity.device_pubkey,
        "platform": identity.platform_name,
    }
    if identity.private_key is not None:
        material["private_key_pem"] = identity.private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode("ascii")
    return material


def identity_from_material(material: dict[str, Any]) -> DeviceIdentity:
    """Restore a DeviceIdentity from its keychain material; private key optional."""
    private_key: Ed25519PrivateKey | None = None
    pem = material.get("private_key_pem")
    if pem:
        loaded = serialization.load_pem_private_key(pem.encode("ascii"), password=None)
        if not isinstance(loaded, Ed25519PrivateKey):
            raise ValueError("Stored device key material is not an Ed25519 private key.")
        private_key = loaded
    return DeviceIdentity(
        raw_device_id=str(material["raw_device_id"]),
        encrypted_device_id=str(material["encrypted_device_id"]),
        device_pubkey=str(material["device_pubkey"]),
        platform_name=str(material.get("platform") or platform.system().lower()),
        private_key=private_key,
    )


class HexAuthClient:
    """Lightweight stdlib HTTP client for hex-backend auth and encrypted device enrollment."""

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    def _request_json(
        self,
        path: str,
        body: dict[str, Any] | None,
        access_token: str | None = None,
        method: str = "POST",
    ) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if access_token:
            headers["Authorization"] = "Bearer " + access_token
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            f"{self._base_url}{path}", data=data, headers=headers, method=method
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _post_json(
        self, path: str, body: dict[str, Any], access_token: str | None = None
    ) -> dict[str, Any]:
        return self._request_json(path, body, access_token=access_token, method="POST")

    def register(self, email: str, password: str) -> dict[str, Any]:
        return self._post_json("/v1/auth/register", {"email": email, "password": password})

    def login(
        self, email: str, password: str, device_record_id: str | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"email": email, "password": password}
        if device_record_id:
            payload["device_record_id"] = device_record_id
        return self._post_json("/v1/auth/login", payload)

    def refresh(self, refresh_token: str) -> dict[str, Any]:
        return self._post_json("/v1/auth/refresh", {"refresh_token": refresh_token})

    def list_devices(
        self, access_token: str, include_revoked: bool = False
    ) -> list[dict[str, Any]]:
        query = "?include_revoked=true" if include_revoked else ""
        return self._request_json(
            f"/v1/devices{query}", None, access_token=access_token, method="GET"
        )["devices"]

    def revoke_device(self, access_token: str, device_record_id: str) -> dict[str, Any]:
        return self._post_json(
            f"/v1/devices/{device_record_id}/revoke", {}, access_token=access_token
        )

    def device_enrollment_challenge(self, access_token: str) -> dict[str, Any]:
        return self._request_json("/v1/devices/challenge", {}, access_token=access_token)

    def enroll_device(
        self,
        access_token: str,
        identity: DeviceIdentity,
        host_eligible: bool = True,
        challenge: str | None = None,
    ) -> dict[str, Any]:
        """Enroll the device with challenge-response proof of its Ed25519 private key."""
        if not challenge:
            challenge = self.device_enrollment_challenge(access_token)["challenge"]
        device_signature = sign_enrollment_challenge(identity, challenge)
        return self._post_json(
            "/v1/devices/register",
            {
                "encrypted_device_id": identity.encrypted_device_id,
                "device_pubkey": identity.device_pubkey,
                "platform": identity.platform_name,
                "host_eligible": host_eligible,
                "challenge": challenge,
                "device_signature": device_signature,
            },
            access_token=access_token,
        )

    # -- signed control-plane artifacts -------------------------------------

    def fetch_server_pubkey(self) -> dict[str, Any]:
        """GET /v1/server/pubkey — the Ed25519 key every signed envelope verifies against."""
        return self._request_json("/v1/server/pubkey", None, method="GET")

    def get_manifest(self, channel: str = "stable") -> dict[str, Any]:
        """GET /v1/releases/manifest/{channel} — signed release manifest envelope."""
        return self._request_json(f"/v1/releases/manifest/{channel}", None, method="GET")

    def check_updates(
        self,
        access_token: str,
        installed_versions: dict[str, str],
        channel: str = "stable",
        platform_name: str | None = None,
    ) -> dict[str, Any]:
        """POST /v1/releases/check — signed compatibility/rollout verdict envelope."""
        if platform_name is None:
            platform_name = f"{platform.system().lower()}-{platform.machine().lower()}"
        return self._post_json(
            "/v1/releases/check",
            {
                "installed_versions": installed_versions,
                "channel": channel,
                "platform": platform_name,
            },
            access_token=access_token,
        )

    def evaluate_config(
        self,
        access_token: str,
        subscription_tier: str = "free",
        channel: str = "stable",
        profile: str = "default",
        beta_opt_in: bool = False,
    ) -> dict[str, Any]:
        """GET /v1/config/evaluate — signed feature-flag / kill-switch bundle envelope."""
        query = urllib.parse.urlencode(
            {
                "subscription_tier": subscription_tier,
                "channel": channel,
                "profile": profile,
                "beta_opt_in": "true" if beta_opt_in else "false",
            }
        )
        return self._request_json(
            f"/v1/config/evaluate?{query}", None, access_token=access_token, method="GET"
        )
