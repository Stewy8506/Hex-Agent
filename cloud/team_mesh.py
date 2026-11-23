"""Client-side Team Primary Host coordinator, TurnCommitBundle, and epoch fencing."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .updater import verify_signed_envelope


@dataclass(frozen=True)
class TurnCommitBundle:
    project_session_id: str
    host_epoch: int
    turn_seq: int
    cow_delta_sha256: str
    ledger_chain_tip: str
    memory_delta_sha256: str
    proof_merkle_root: str

    def compute_digest(self) -> str:
        raw = json.dumps(self.__dict__, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


class TeamMeshCoordinator:
    """Enforces host_epoch fencing and validates incoming TurnCommitBundles on Replica Peers."""

    def __init__(self, local_device_id: str, signing_secret: str) -> None:
        self.local_device_id = local_device_id
        self._signing_secret = signing_secret
        self.current_epoch: int = 0
        self.primary_device_id: str | None = None
        self.last_applied_turn_seq: int = 0

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
        if not verify_signed_envelope(signed_lease, self._signing_secret):
            raise ValueError("HostLease signature verification failed.")
        lease = signed_lease["payload"]
        incoming_epoch = int(lease["host_epoch"])
        if incoming_epoch < self.current_epoch:
            raise ValueError(
                f"Rejected stale HostLease epoch {incoming_epoch} < current {self.current_epoch}."
            )
        self.current_epoch = incoming_epoch
        self.primary_device_id = str(lease["primary_device_record_id"])
        self.last_applied_turn_seq = max(
            self.last_applied_turn_seq, int(lease.get("last_committed_turn_seq", 0))
        )
        return self.mode

    def accept_turn_commit(self, bundle: TurnCommitBundle) -> dict[str, Any]:
        """Validate a replicated TurnCommitBundle from the active Primary Host."""
        if bundle.host_epoch != self.current_epoch:
            raise ValueError(
                f"TurnCommitBundle epoch mismatch: got {bundle.host_epoch}, expected {self.current_epoch}."
            )
        if bundle.turn_seq <= self.last_applied_turn_seq:
            raise ValueError(
                f"Stale turn_seq {bundle.turn_seq} <= already applied {self.last_applied_turn_seq}."
            )
        self.last_applied_turn_seq = bundle.turn_seq
        return {
            "ack": True,
            "project_session_id": bundle.project_session_id,
            "turn_seq": bundle.turn_seq,
            "bundle_digest": bundle.compute_digest(),
        }
