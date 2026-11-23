"""Client-side signed component manifest verifier and rollback tracker.

All hex-backend artifacts (update checks, release manifests, remote config,
host leases, turn-commit receipts) are Ed25519-signed by the server identity
key published at GET /v1/server/pubkey. Clients verify with that public key —
no shared secret is ever embedded in the client.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

ENVELOPE_ALG = "ed25519"


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    """Canonical form shared with hex-backend's signing module."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def verify_signed_envelope(envelope: dict[str, Any], server_public_key_hex: str) -> bool:
    """Verify an Ed25519-signed envelope from hex-backend against its published key."""
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
