"""Device identity, encrypted device ID generation, and hex-backend auth client.

Designed to stay 100% offline-safe: if hex-backend is unconfigured or offline,
Harness continues running locally without blocking.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import platform
import secrets
import socket
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DeviceIdentity:
    raw_device_id: str
    encrypted_device_id: str
    device_pubkey: str
    platform_name: str


def encrypt_device_id(raw_device_id: str, account_key: str) -> str:
    """Encrypt device identifier client-side so hex-backend stores only ciphertext."""
    nonce = secrets.token_bytes(16)
    key = hashlib.sha256(account_key.encode("utf-8") + nonce).digest()
    pt = raw_device_id.encode("utf-8")
    ct = bytes(b ^ key[i % len(key)] for i, b in enumerate(pt))
    mac = hmac.new(key, nonce + ct, hashlib.sha256).digest()[:16]
    return "dev_enc_v1:" + base64.urlsafe_b64encode(nonce + mac + ct).decode("ascii")


def generate_device_identity(account_key: str) -> DeviceIdentity:
    """Generate a local device identity + encrypted ID ready for OS Keychain storage."""
    host = socket.gethostname()
    raw_id = f"{host}:{secrets.token_hex(12)}"
    pubkey = f"ed25519:{secrets.token_hex(32)}"
    return DeviceIdentity(
        raw_device_id=raw_id,
        encrypted_device_id=encrypt_device_id(raw_id, account_key),
        device_pubkey=pubkey,
        platform_name=platform.system().lower(),
    )


class HexAuthClient:
    """Lightweight stdlib HTTP client for hex-backend auth and encrypted device enrollment."""

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    def _post_json(
        self, path: str, body: dict[str, Any], access_token: str | None = None
    ) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if access_token:
            headers["Authorization"] = "Bearer " + access_token
        req = urllib.request.Request(
            f"{self._base_url}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def register(self, email: str, password: str) -> dict[str, Any]:
        return self._post_json("/v1/auth/register", {"email": email, "password": password})

    def login(
        self, email: str, password: str, device_record_id: str | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"email": email, "password": password}
        if device_record_id:
            payload["device_record_id"] = device_record_id
        return self._post_json("/v1/auth/login", payload)

    def enroll_device(
        self, access_token: str, identity: DeviceIdentity, host_eligible: bool = True
    ) -> dict[str, Any]:
        return self._post_json(
            "/v1/devices/register",
            {
                "encrypted_device_id": identity.encrypted_device_id,
                "device_pubkey": identity.device_pubkey,
                "platform": identity.platform_name,
                "host_eligible": host_eligible,
            },
            access_token=access_token,
        )
