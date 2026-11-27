"""Client-side Team Primary Host coordinator, TurnCommitBundle, and epoch fencing."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .updater import verify_signed_envelope

TURN_COMMIT_VERSION = 1
TURN_COMMIT_DOMAIN = b"hex/turn-commit-bundle/v1\0"
TURN_ACK_DOMAIN = b"hex/turn-ack/v1\0"
BUNDLE_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


@dataclass(frozen=True)
class TurnCommitBundle:
    project_session_id: str
    host_epoch: int
    turn_seq: int
    cow_delta_sha256: str
    ledger_chain_tip: str
    memory_delta_sha256: str
    proof_merkle_root: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_session_id": self.project_session_id,
            "host_epoch": self.host_epoch,
            "turn_seq": self.turn_seq,
            "cow_delta_sha256": self.cow_delta_sha256,
            "ledger_chain_tip": self.ledger_chain_tip,
            "memory_delta_sha256": self.memory_delta_sha256,
            "proof_merkle_root": self.proof_merkle_root,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "TurnCommitBundle":
        fields = {
            "project_session_id": str,
            "host_epoch": int,
            "turn_seq": int,
            "cow_delta_sha256": str,
            "ledger_chain_tip": str,
            "memory_delta_sha256": str,
            "proof_merkle_root": str,
        }
        if set(payload) != set(fields):
            raise ValueError("Malformed TurnCommitBundle fields.")
        values: dict[str, Any] = {}
        for name, expected in fields.items():
            value = payload[name]
            if type(value) is not expected:
                raise ValueError(f"Invalid TurnCommitBundle field: {name}.")
            values[name] = value
        bundle = cls(**values)
        if not DEVICE_ID_PATTERN.fullmatch(bundle.project_session_id):
            raise ValueError("Invalid project session id.")
        if bundle.host_epoch < 1 or bundle.turn_seq < 1:
            raise ValueError("Turn epoch and sequence must be positive.")
        for value in (
            bundle.cow_delta_sha256,
            bundle.ledger_chain_tip,
            bundle.memory_delta_sha256,
            bundle.proof_merkle_root,
        ):
            if not BUNDLE_HASH_PATTERN.fullmatch(value):
                raise ValueError("Turn bundle contains an invalid content hash.")
        return bundle

    def compute_digest(self) -> str:
        raw = canonical_json_bytes(self.to_dict())
        return hashlib.sha256(raw).hexdigest()


def canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _decode_signature(value: Any) -> bytes:
    if not isinstance(value, str) or not value.startswith("ed25519:"):
        raise ValueError("Turn bundle signature must use Ed25519.")
    segment = value.removeprefix("ed25519:")
    if not re.fullmatch(r"[A-Za-z0-9_-]{86}", segment):
        raise ValueError("Malformed turn bundle signature.")
    try:
        signature = base64.urlsafe_b64decode(segment + "==")
    except ValueError as exc:
        raise ValueError("Malformed turn bundle signature.") from exc
    if len(signature) != 64:
        raise ValueError("Malformed turn bundle signature.")
    return signature


def _public_key_bytes(value: str) -> bytes:
    normalized = value.removeprefix("ed25519:")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", normalized):
        raise ValueError("Invalid enrolled device public key.")
    return bytes.fromhex(normalized)


@dataclass(frozen=True)
class SignedTurnCommitBundle:
    bundle: TurnCommitBundle
    author_device_record_id: str
    signature: str
    protocol_version: int = TURN_COMMIT_VERSION

    @property
    def signing_payload(self) -> dict[str, Any]:
        return {
            "bundle": self.bundle.to_dict(),
            "author_device_record_id": self.author_device_record_id,
            "protocol_version": self.protocol_version,
        }

    @property
    def signing_bytes(self) -> bytes:
        return TURN_COMMIT_DOMAIN + canonical_json_bytes(self.signing_payload)

    def to_dict(self) -> dict[str, Any]:
        return {**self.signing_payload, "signature": self.signature}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SignedTurnCommitBundle":
        if set(payload) != {
            "bundle", "author_device_record_id", "signature", "protocol_version"
        }:
            raise ValueError("Malformed signed turn bundle fields.")
        if type(payload["protocol_version"]) is not int or payload["protocol_version"] != TURN_COMMIT_VERSION:
            raise ValueError("Unsupported turn bundle protocol version.")
        author = payload["author_device_record_id"]
        if not isinstance(author, str) or not DEVICE_ID_PATTERN.fullmatch(author):
            raise ValueError("Invalid turn bundle author device.")
        if not isinstance(payload["bundle"], Mapping):
            raise ValueError("Malformed turn bundle.")
        _decode_signature(payload["signature"])
        return cls(
            bundle=TurnCommitBundle.from_dict(payload["bundle"]),
            author_device_record_id=author,
            signature=payload["signature"],
            protocol_version=payload["protocol_version"],
        )

    def verify(self, author_public_key: str) -> bool:
        try:
            Ed25519PublicKey.from_public_bytes(_public_key_bytes(author_public_key)).verify(
                _decode_signature(self.signature), self.signing_bytes
            )
            return True
        except (InvalidSignature, ValueError):
            return False


def sign_turn_commit(bundle: TurnCommitBundle, signer: Any) -> SignedTurnCommitBundle:
    bundle = TurnCommitBundle.from_dict(bundle.to_dict())
    unsigned = SignedTurnCommitBundle(
        bundle=bundle,
        author_device_record_id=signer.device_id,
        signature="",
    )
    signature = signer.sign(unsigned.signing_bytes)
    return SignedTurnCommitBundle(
        bundle=bundle,
        author_device_record_id=signer.device_id,
        signature="ed25519:" + base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii"),
    )


def sign_turn_ack(
    signer: Any,
    project_session_id: str,
    host_epoch: int,
    turn_seq: int,
    bundle_digest: str,
) -> dict[str, Any]:
    if (
        not DEVICE_ID_PATTERN.fullmatch(project_session_id)
        or type(host_epoch) is not int or host_epoch < 1
        or type(turn_seq) is not int or turn_seq < 1
        or not re.fullmatch(r"[0-9a-f]{64}", bundle_digest)
    ):
        raise ValueError("Invalid turn acknowledgement.")
    payload = {
        "project_session_id": project_session_id,
        "host_epoch": host_epoch,
        "turn_seq": turn_seq,
        "bundle_digest": bundle_digest,
    }
    signature = signer.sign(TURN_ACK_DOMAIN + canonical_json_bytes(payload))
    return {
        **payload,
        "signature": "ed25519:" + base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii"),
    }


def verify_turn_ack(ack: Mapping[str, Any], device_public_key: str) -> dict[str, Any]:
    fields = {"project_session_id", "host_epoch", "turn_seq", "bundle_digest", "signature"}
    if not isinstance(ack, Mapping) or set(ack) != fields:
        raise ValueError("Malformed turn acknowledgement.")
    if not isinstance(ack["project_session_id"], str) or not DEVICE_ID_PATTERN.fullmatch(
        ack["project_session_id"]
    ):
        raise ValueError("Invalid acknowledgement session.")
    if type(ack["host_epoch"]) is not int or ack["host_epoch"] < 1:
        raise ValueError("Invalid acknowledgement epoch.")
    if type(ack["turn_seq"]) is not int or ack["turn_seq"] < 1:
        raise ValueError("Invalid acknowledgement sequence.")
    if not isinstance(ack["bundle_digest"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", ack["bundle_digest"]
    ):
        raise ValueError("Invalid acknowledgement bundle digest.")
    try:
        Ed25519PublicKey.from_public_bytes(_public_key_bytes(device_public_key)).verify(
            _decode_signature(ack["signature"]),
            TURN_ACK_DOMAIN + canonical_json_bytes(
                {key: value for key, value in ack.items() if key != "signature"}
            ),
        )
    except (InvalidSignature, ValueError) as exc:
        raise ValueError("Turn acknowledgement signature verification failed.") from exc
    return {key: value for key, value in ack.items() if key != "signature"}


class TeamMeshCoordinator:
    """Enforces host_epoch fencing and validates incoming TurnCommitBundles on Replica Peers."""

    def __init__(
        self,
        local_device_id: str,
        server_public_key_hex: str,
        project_session_id: str | None = None,
        replication_service: Any | None = None,
    ) -> None:
        self.local_device_id = local_device_id
        self._server_public_key_hex = server_public_key_hex
        self.project_session_id = project_session_id
        self.replication_service = replication_service
        self.current_epoch: int = 0
        self.primary_device_id: str | None = None
        self.last_applied_turn_seq: int = 0
        self.committed_turn_seq: int = 0

    @property
    def mode(self) -> str:
        if not self.primary_device_id:
            return "LOCAL"
        return (
            "TEAM_PRIMARY_HOST"
            if self.primary_device_id == self.local_device_id
            else "TEAM_REPLICA_PEER"
        )

    def apply_signed_host_lease(self, signed_lease: dict[str, Any]) -> str:
        """Accept a newer host_epoch lease; reject stale epochs to prevent split-brain."""
        if not verify_signed_envelope(signed_lease, self._server_public_key_hex):
            raise ValueError("HostLease signature verification failed.")
        lease = signed_lease["payload"]
        if not isinstance(lease, dict):
            raise ValueError("Malformed HostLease.")
        session_id = lease.get("project_session_id")
        primary_device = lease.get("primary_device_record_id")
        epoch = lease.get("host_epoch")
        sequence = lease.get("last_committed_turn_seq", 0)
        if not isinstance(session_id, str) or not DEVICE_ID_PATTERN.fullmatch(session_id):
            raise ValueError("Invalid HostLease project session.")
        if self.project_session_id is not None and session_id != self.project_session_id:
            raise ValueError("HostLease belongs to a different project session.")
        if not isinstance(primary_device, str) or not DEVICE_ID_PATTERN.fullmatch(primary_device):
            raise ValueError("Invalid HostLease Coordinator device.")
        if type(epoch) is not int or epoch < 1 or type(sequence) is not int or sequence < 0:
            raise ValueError("Invalid HostLease epoch or sequence.")
        incoming_epoch = epoch
        if incoming_epoch < self.current_epoch:
            raise ValueError(
                f"Rejected stale HostLease epoch {incoming_epoch} < current {self.current_epoch}."
            )
        self.project_session_id = session_id
        self.current_epoch = incoming_epoch
        self.primary_device_id = primary_device
        self.committed_turn_seq = max(self.committed_turn_seq, sequence)
        return self.mode

    @property
    def missing_turn_range(self) -> tuple[int, int] | None:
        """Return the contiguous catch-up range advertised by the trusted lease."""
        if self.committed_turn_seq <= self.last_applied_turn_seq:
            return None
        return self.last_applied_turn_seq + 1, self.committed_turn_seq

    def note_committed_turn(
        self, project_session_id: str, host_epoch: int, turn_seq: int
    ) -> None:
        if self.mode != "TEAM_PRIMARY_HOST":
            raise ValueError("Only the active Coordinator can record a committed turn.")
        if type(host_epoch) is not int or host_epoch < 1:
            raise ValueError("Committed turn epoch must be positive.")
        if self.project_session_id != project_session_id or host_epoch > self.current_epoch:
            raise ValueError("Committed turn does not match the active Coordinator lease.")
        if type(turn_seq) is not int or turn_seq < 1:
            raise ValueError("Committed turn sequence must be positive.")
        if host_epoch < self.current_epoch and turn_seq > self.committed_turn_seq:
            raise ValueError("Historical committed turn exceeds the trusted lease sequence.")
        self.committed_turn_seq = max(self.committed_turn_seq, turn_seq)

    def accept_turn_commit(
        self,
        signed_bundle: SignedTurnCommitBundle | Mapping[str, Any],
        device_public_keys: Mapping[str, str],
        *,
        sender_device_id: str,
        replication_payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Apply and acknowledge a signed turn relayed by the active Coordinator."""
        if self.primary_device_id is None or sender_device_id != self.primary_device_id:
            raise ValueError("TurnCommitBundle was not relayed by the active Coordinator.")
        if not isinstance(signed_bundle, SignedTurnCommitBundle):
            signed_bundle = SignedTurnCommitBundle.from_dict(signed_bundle)
        bundle = signed_bundle.bundle
        if self.project_session_id is None or bundle.project_session_id != self.project_session_id:
            raise ValueError("TurnCommitBundle belongs to a different project session.")
        if bundle.host_epoch > self.current_epoch:
            raise ValueError(
                f"TurnCommitBundle epoch mismatch: got {bundle.host_epoch}, expected <= {self.current_epoch}."
            )
        public_key = device_public_keys.get(signed_bundle.author_device_record_id)
        if not public_key or not signed_bundle.verify(public_key):
            raise ValueError("TurnCommitBundle author signature verification failed.")
        if self.replication_service is None or replication_payload is None:
            raise ValueError("Verified team turn content is required for replication.")
        if (
            bundle.turn_seq > self.last_applied_turn_seq
            and bundle.turn_seq != self.last_applied_turn_seq + 1
        ):
            raise ValueError(
                f"Team turn sequence gap: expected {self.last_applied_turn_seq + 1}, got {bundle.turn_seq}."
            )
        result = self.replication_service.apply_turn(
            signed_bundle,
            replication_payload,
            device_public_keys,
        )
        self.last_applied_turn_seq = max(self.last_applied_turn_seq, bundle.turn_seq)
        self.committed_turn_seq = max(self.committed_turn_seq, bundle.turn_seq)
        return {
            "ack": True,
            "project_session_id": bundle.project_session_id,
            "author_device_record_id": signed_bundle.author_device_record_id,
            "turn_seq": bundle.turn_seq,
            "bundle_digest": bundle.compute_digest(),
            "replication_status": result["status"],
        }
