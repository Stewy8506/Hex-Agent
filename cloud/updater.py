"""Client-side signed component manifest verifier and rollback tracker.

All hex-backend artifacts (update checks, release manifests, remote config,
host leases, turn-commit receipts) are Ed25519-signed by the server identity
key published at GET /v1/server/pubkey. Clients verify with that public key —
no shared secret is ever embedded in the client.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import time
from pathlib import Path
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

ENVELOPE_ALG = "ed25519"


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    """Canonical form shared with hex-backend's signing module."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def verify_signed_envelope(
    envelope: dict[str, Any],
    server_public_key_hex: str | ServerKeyring,
) -> bool:
    """Verify an Ed25519-signed envelope from hex-backend against its published key."""
    if isinstance(server_public_key_hex, ServerKeyring):
        return server_public_key_hex.verify(envelope)
    payload = envelope.get("payload")
    sig_header = envelope.get("signature", "")
    if not isinstance(payload, dict) or not sig_header.startswith(f"{ENVELOPE_ALG}:"):
        return False
    try:
        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(server_public_key_hex))
        public_key.verify(_b64url_decode(sig_header.split(":", 1)[1]), canonical_json_bytes(payload))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


class ServerKeyring:
    """Current backend identity plus a bounded rollover window for prior keys."""

    def __init__(
        self,
        current_key_hex: str,
        *,
        current_key_id: str | None = None,
        previous_keys: Mapping[str, str] | None = None,
        previous_key_expiries: Mapping[str, float] | None = None,
        now: float | None = None,
    ) -> None:
        current_key_hex = self._validate_key(current_key_hex)
        derived_key_id = hashlib.sha256(bytes.fromhex(current_key_hex)).hexdigest()[:16]
        if current_key_id is not None and current_key_id != derived_key_id:
            raise ValueError("Server public key id does not match its Ed25519 key.")
        self.current_key_hex = current_key_hex
        self.current_key_id = current_key_id or derived_key_id
        expiries = dict(previous_key_expiries or {})
        timestamp = time.time() if now is None else now
        self.previous_keys: dict[str, str] = {}
        self.previous_key_expiries: dict[str, float] = {}
        for key_id, key_hex in (previous_keys or {}).items():
            expiry = expiries.get(key_id)
            if (
                not isinstance(key_id, str)
                or not re.fullmatch(r"[0-9a-f]{16}", key_id)
                or key_id == self.current_key_id
                or not isinstance(key_hex, str)
                or type(expiry) not in {int, float}
                or not math.isfinite(expiry)
            ):
                raise ValueError("Malformed previous server public key.")
            if expiry <= timestamp:
                continue
            normalized = self._validate_key(key_hex)
            if hashlib.sha256(bytes.fromhex(normalized)).hexdigest()[:16] != key_id:
                raise ValueError("Previous server public key id does not match its Ed25519 key.")
            self.previous_keys[key_id] = normalized
            self.previous_key_expiries[key_id] = float(expiry)

    @classmethod
    def from_document(
        cls,
        document: Mapping[str, Any],
        *,
        now: float | None = None,
    ) -> "ServerKeyring":
        required = {"algorithm", "public_key", "key_id"}
        if not isinstance(document, Mapping) or set(document) not in (
            required, required | {"previous_keys"}
        ):
            raise ValueError("Malformed server public key response.")
        if document["algorithm"] != "ed25519":
            raise ValueError("Server published an unsupported identity key type.")
        key_id = document["key_id"]
        if not isinstance(key_id, str) or not re.fullmatch(r"[0-9a-f]{16}", key_id):
            raise ValueError("Server published an invalid identity key id.")
        current = cls._validate_key(document["public_key"])
        if hashlib.sha256(bytes.fromhex(current)).hexdigest()[:16] != key_id:
            raise ValueError("Server public key id does not match its Ed25519 key.")
        previous_documents = document.get("previous_keys", [])
        if not isinstance(previous_documents, list) or len(previous_documents) > 4:
            raise ValueError("Server published a malformed previous-key set.")
        previous_keys: dict[str, str] = {}
        expiries: dict[str, float] = {}
        for item in previous_documents:
            if not isinstance(item, Mapping) or set(item) != {
                "algorithm", "public_key", "key_id", "valid_until"
            }:
                raise ValueError("Server published a malformed previous key.")
            if item["algorithm"] != "ed25519":
                raise ValueError("Server published an unsupported previous-key type.")
            previous_id = item["key_id"]
            expiry = item["valid_until"]
            if (
                not isinstance(previous_id, str)
                or not re.fullmatch(r"[0-9a-f]{16}", previous_id)
                or previous_id == key_id
                or previous_id in previous_keys
                or type(expiry) not in {int, float}
                or not math.isfinite(expiry)
            ):
                raise ValueError("Server published an invalid previous key.")
            previous_keys[previous_id] = cls._validate_key(item["public_key"])
            expiries[previous_id] = float(expiry)
        return cls(
            current,
            current_key_id=key_id,
            previous_keys=previous_keys,
            previous_key_expiries=expiries,
            now=now,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "current_key_hex": self.current_key_hex,
            "current_key_id": self.current_key_id,
            "previous_keys": dict(self.previous_keys),
            "previous_key_expiries": dict(self.previous_key_expiries),
        }

    @classmethod
    def from_state(
        cls, state: Mapping[str, Any], *, now: float | None = None
    ) -> "ServerKeyring":
        if not isinstance(state, Mapping) or set(state) != {
            "version", "current_key_hex", "current_key_id",
            "previous_keys", "previous_key_expiries",
        }:
            raise ValueError("Malformed server keyring state.")
        if state["version"] != 1:
            raise ValueError("Unsupported server keyring state.")
        if not isinstance(state["previous_keys"], Mapping) or not isinstance(
            state["previous_key_expiries"], Mapping
        ):
            raise ValueError("Malformed server keyring state.")
        return cls(
            state["current_key_hex"],
            current_key_id=state["current_key_id"],
            previous_keys=state["previous_keys"],
            previous_key_expiries=state["previous_key_expiries"],
            now=now,
        )

    def verify(self, envelope: Mapping[str, Any]) -> bool:
        payload = envelope.get("payload")
        sig_header = envelope.get("signature", "")
        if not isinstance(payload, dict) or not sig_header.startswith(f"{ENVELOPE_ALG}:"):
            return False
        key_id = envelope.get("key_id")
        if key_id is None:
            candidates = [(self.current_key_id, self.current_key_hex)]
            candidates.extend(self.previous_keys.items())
        elif key_id == self.current_key_id:
            candidates = [(self.current_key_id, self.current_key_hex)]
        elif isinstance(key_id, str) and key_id in self.previous_keys:
            candidates = [(key_id, self.previous_keys[key_id])]
        else:
            return False
        try:
            signature = _b64url_decode(sig_header.split(":", 1)[1])
            raw = canonical_json_bytes(payload)
            for _candidate_id, key_hex in candidates:
                try:
                    Ed25519PublicKey.from_public_bytes(bytes.fromhex(key_hex)).verify(signature, raw)
                    return True
                except InvalidSignature:
                    continue
        except (InvalidSignature, ValueError, TypeError):
            return False
        return False

    @staticmethod
    def _validate_key(value: Any) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
            raise ValueError("Server published an invalid Ed25519 public key.")
        return value.lower()


class ComponentUpdaterClient:
    """Verifies signed update check responses and records safe rollback metadata."""

    def __init__(self, server_public_key_hex: str, rollback_file: Path | None = None) -> None:
        self._server_public_key_hex = server_public_key_hex
        self._rollback_file = rollback_file

    def evaluate_update_response(self, signed_response: dict[str, Any]) -> dict[str, Any]:
        if not verify_signed_envelope(signed_response, self._server_public_key_hex):
            raise ValueError("Update response signature verification failed.")
        payload = signed_response["payload"]
        if not payload.get("compatible", True):
            return {
                "apply_allowed": False,
                "reason": "cross_component_incompatibility",
                "compatibility_issues": payload.get("compatibility_issues", []),
                "rollbacks": payload.get("rollbacks", []),
            }
        return {
            "apply_allowed": True,
            "mandatory_upgrade_required": bool(payload.get("mandatory_upgrade_required", False)),
            "updates": payload.get("updates", []),
            "rollbacks": payload.get("rollbacks", []),
        }

    def record_rollback_checkpoint(self, component: str, previous_version: str) -> None:
        if not self._rollback_file:
            return
        self._rollback_file.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, str] = {}
        if self._rollback_file.exists():
            data = json.loads(self._rollback_file.read_text(encoding="utf-8"))
        data[component] = previous_version
        self._rollback_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
