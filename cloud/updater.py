"""Client-side signed component manifest verifier and rollback tracker."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any


def verify_signed_envelope(envelope: dict[str, Any], signing_secret: str) -> bool:
    """Verify canonical-JSON HMAC-SHA256 envelope from hex-backend."""
    payload = envelope.get("payload")
    sig_header = envelope.get("signature", "")
    if not isinstance(payload, dict) or not sig_header.startswith("hmac-sha256:"):
        return False
    provided = sig_header.split(":", 1)[1]
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    expected = base64.urlsafe_b64encode(
        hmac.new(signing_secret.encode("utf-8"), raw, hashlib.sha256).digest()
    ).decode("ascii")
    return hmac.compare_digest(provided, expected)


class ComponentUpdaterClient:
    """Verifies signed update check responses and records safe rollback metadata."""

    def __init__(self, signing_secret: str, rollback_file: Path | None = None) -> None:
        self._signing_secret = signing_secret
        self._rollback_file = rollback_file

    def evaluate_update_response(self, signed_response: dict[str, Any]) -> dict[str, Any]:
        if not verify_signed_envelope(signed_response, self._signing_secret):
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
