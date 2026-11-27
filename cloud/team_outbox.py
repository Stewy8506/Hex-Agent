"""Durable, verified team-turn packaging and delivery."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from harness_core.capabilities.proof import sign_proof_artifact
from harness_core.storage.db import utcnow_iso

from .team_mesh import TurnCommitBundle, SignedTurnCommitBundle, sign_turn_commit
from .team_replication import team_payload_digest

_OUTBOX_VERSION = 1
_MAX_ARCHIVE_BYTES = 48 * 1024 * 1024
_MAX_PAYLOAD_BYTES = 64 * 1024 * 1024
_MAX_LEDGER_EVENTS = 10_000


@dataclass(frozen=True)
class VerifiedTeamTurn:
    task_id: str
    attempt_id: str
    workspace_dir: Path | str
    base_snapshot_path: Path | str
    proof: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "workspace_dir": str(Path(self.workspace_dir).resolve()),
            "base_snapshot_path": str(Path(self.base_snapshot_path).resolve()),
            "proof": dict(self.proof),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "VerifiedTeamTurn":
        if not isinstance(value, Mapping) or set(value) != {
            "task_id", "attempt_id", "workspace_dir", "base_snapshot_path", "proof"
        }:
            raise ValueError("Malformed pending team turn context.")
        if not all(isinstance(value[name], str) and value[name] for name in (
            "task_id", "attempt_id", "workspace_dir", "base_snapshot_path"
        )):
            raise ValueError("Invalid pending team turn context.")
        if not isinstance(value["proof"], Mapping):
            raise ValueError("Invalid pending team turn proof.")
        return cls(
            task_id=value["task_id"],
            attempt_id=value["attempt_id"],
            workspace_dir=value["workspace_dir"],
            base_snapshot_path=value["base_snapshot_path"],
            proof=dict(value["proof"]),
        )


class TeamTurnOutbox:
    """Persist verified content locally; the publisher must use authenticated E2EE."""

    def __init__(
        self,
        data_dir: Path | str,
        *,
        ledger: Any,
        cow: Any,
        cognition: Any,
        signer: Any,
        scope_provider: Callable[[str], Mapping[str, Any]],
        publisher: Callable[[SignedTurnCommitBundle, Mapping[str, Any]], Any],
    ) -> None:
        self.root = Path(data_dir).resolve() / "team_outbox"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.pending_dir = self.root / "pending"
        self.pending_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        os.chmod(self.pending_dir, 0o700)
        self.ledger = ledger
        self.cow = cow
        self.cognition = cognition
        self.signer = signer
        self.scope_provider = scope_provider
        self.publisher = publisher
        self._lock = threading.RLock()

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(list(self.pending_dir.glob("*.json")))

    def __call__(self, turn: VerifiedTeamTurn) -> dict[str, Any]:
        with self._lock:
            record = self._load_or_create(turn)
            if record["status"] == "preparing":
                record = self._prepare(record)
            if record["status"] == "skipped":
                return {"status": "skipped", "reason": "NO_WORKSPACE_CHANGES"}
            if record["status"] in {"prepared", "signing"}:
                record = self._sign(record)
            if record["status"] != "ready":
                raise ValueError("Team turn is not ready for delivery.")
            return self._deliver(record)

    def flush(self) -> dict[str, Any]:
        delivered = 0
        queued = 0
        with self._lock:
            for path in sorted(self.pending_dir.glob("*.json")):
                try:
                    record = self._load(path)
                    if record["status"] == "preparing":
                        record = self._prepare(record)
                    if record["status"] == "skipped":
                        continue
                    if record["status"] in {"prepared", "signing"}:
                        record = self._sign(record)
                    if record["status"] != "ready":
                        raise ValueError("Team turn is not ready for delivery.")
                    outcome = self._deliver(record)
                    if outcome["status"] == "delivered":
                        delivered += 1
                    else:
                        queued += 1
                except Exception as exc:
                    queued += 1
                    self._record_failure(path, type(exc).__name__)
        return {"delivered": delivered, "queued": queued}

    def _load_or_create(self, turn: VerifiedTeamTurn) -> dict[str, Any]:
        path = self._path(turn.task_id, turn.attempt_id)
        if path.exists():
            record = self._load(path)
            if record["turn"] != turn.to_dict():
                raise ValueError("A pending team turn already uses this task attempt.")
            return record
        record = {
            "version": _OUTBOX_VERSION,
            "turn_id": path.stem,
            "status": "preparing",
            "created_at": utcnow_iso(),
            "turn": turn.to_dict(),
        }
        self._write(path, record)
        return record

    def _prepare(self, record: dict[str, Any]) -> dict[str, Any]:
        turn = VerifiedTeamTurn.from_dict(record["turn"])
        proof = dict(turn.proof)
        if proof.get("task_id") != turn.task_id or proof.get("attempt_id") != turn.attempt_id:
            raise ValueError("Proof does not belong to the verified team turn.")
        if proof.get("verification_results", {}).get("passed") is not True:
            raise ValueError("Only successfully verified proofs can create team turns.")
        workspace = Path(turn.workspace_dir)
        base = Path(turn.base_snapshot_path)
        if not workspace.is_dir() or not base.is_dir():
            raise FileNotFoundError("Verified team workspace or base snapshot is missing.")
        base_tree = self.cow.compute_tree_sha(base)
        head_tree = self.cow.compute_tree_sha(workspace)
        if proof.get("workspace_tree_sha") != head_tree:
            raise ValueError("Proof workspace hash does not match the verified head.")
        if base_tree == head_tree:
            self.pending_dir.joinpath(f"{record['turn_id']}.json").unlink(missing_ok=True)
            self._fsync_dir(self.pending_dir)
            return {**record, "status": "skipped"}
        archive_path = self.root / f"{record['turn_id']}.tar.gz"
        try:
            archive_sha = self.cow.export_delta_archive(workspace, archive_path)
            archive_bytes = archive_path.read_bytes()
        finally:
            archive_path.unlink(missing_ok=True)
        if not archive_bytes or len(archive_bytes) > _MAX_ARCHIVE_BYTES:
            raise ValueError("Team workspace archive exceeds the size limit.")
        if hashlib.sha256(archive_bytes).hexdigest() != archive_sha:
            raise ValueError("Team workspace archive failed its digest check.")
        ledger_delta = self._complete_ledger_delta(turn.task_id)
        events = ledger_delta["events"]
        if not events or events[-1].get("workspace_tree_sha") != head_tree:
            raise ValueError("The final ledger event does not match the verified workspace.")
        manifest = self.cognition.export_manifest()
        record = {
            **record,
            "status": "prepared",
            "prepared_at": utcnow_iso(),
            "prepared": {
                "base_workspace_tree_sha": "sha256:" + base_tree,
                "head_workspace_tree_sha": "sha256:" + head_tree,
                "archive_sha256": "sha256:" + archive_sha,
                "workspace_archive_base64": base64.b64encode(archive_bytes).decode("ascii"),
                "ledger_delta": ledger_delta,
                "cognition_manifest": manifest,
            },
        }
        self._write(self._path(turn.task_id, turn.attempt_id), record)
        return record

    def _complete_ledger_delta(self, task_id: str) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        since_seq = 0
        while True:
            page = self.ledger.export_sync_delta(task_id, since_seq, limit=5000)
            events.extend(page["events"])
            if len(events) > _MAX_LEDGER_EVENTS:
                raise ValueError("Team ledger delta exceeds the supported size.")
            if page["has_more"] is False:
                break
            since_seq = page["next_seq"]
        return {
            "version": 1,
            "since_seq": 0,
            "task": page["task"],
            "events": events,
            "base_event_sha256": "0" * 64,
            "dialogue_turns": page["dialogue_turns"],
            "next_seq": len(events),
            "has_more": False,
        }

    def _sign(self, record: dict[str, Any]) -> dict[str, Any]:
        turn = VerifiedTeamTurn.from_dict(record["turn"])
        prepared = record["prepared"]
        scope = record.get("scope")
        if scope is None:
            scope = dict(self.scope_provider(turn.task_id))
            if set(scope) != {"project_session_id", "host_epoch", "turn_seq"}:
                raise ValueError("Team turn scope is malformed.")
            record = {**record, "status": "signing", "scope": scope}
            self._write(self._path(turn.task_id, turn.attempt_id), record)
        session_id = scope["project_session_id"]
        if self.cognition.workspace_id != session_id:
            raise ValueError("Cognition workspace does not match the team session.")
        signed_proof = sign_proof_artifact(
            turn.proof,
            self.signer,
            project_session_id=session_id,
            host_epoch=scope["host_epoch"],
            turn_seq=scope["turn_seq"],
        )
        self.ledger.attach_signed_proof(turn.proof["proof_id"], signed_proof.to_dict())
        events = prepared["ledger_delta"]["events"]
        manifest = prepared["cognition_manifest"]
        bundle = TurnCommitBundle(
            project_session_id=session_id,
            host_epoch=scope["host_epoch"],
            turn_seq=scope["turn_seq"],
            cow_delta_sha256=prepared["archive_sha256"],
            ledger_chain_tip="sha256:" + events[-1]["event_sha256"],
            memory_delta_sha256="sha256:" + manifest["manifest_sha256"],
            proof_merkle_root=signed_proof.proof_merkle_root,
        )
        signed = sign_turn_commit(bundle, self.signer)
        payload = {
            "version": 1,
            "workspace_archive_base64": prepared["workspace_archive_base64"],
            "base_workspace_tree_sha": prepared["base_workspace_tree_sha"],
            "signed_proof": signed_proof.to_dict(),
            "ledger_delta": prepared["ledger_delta"],
            "cognition_manifest": manifest,
        }
        payload_digest = team_payload_digest(payload)
        if len(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")) > _MAX_PAYLOAD_BYTES:
            raise ValueError("Team replication payload exceeds the size limit.")
        record = {
            **record,
            "status": "ready",
            "signed": signed.to_dict(),
            "payload": payload,
            "payload_digest": payload_digest,
            "ready_at": utcnow_iso(),
        }
        self._write(self._path(turn.task_id, turn.attempt_id), record)
        return record

    def _deliver(self, record: dict[str, Any]) -> dict[str, Any]:
        path = self.pending_dir / f"{record['turn_id']}.json"
        signed = SignedTurnCommitBundle.from_dict(record["signed"])
        payload = record["payload"]
        if not signed.verify(self.signer.signing_public_key):
            raise ValueError("Pending signed team turn failed verification.")
        if team_payload_digest(payload) != record.get("payload_digest"):
            raise ValueError("Pending team payload changed.")
        try:
            result = self.publisher(signed, payload)
            accepted = (
                result.get("accepted") is True or result.get("ack") is True
                if isinstance(result, Mapping)
                else result is True
            )
            if not accepted:
                raise RuntimeError("Team transport did not accept the turn.")
        except Exception as exc:
            record = {
                **record,
                "last_attempt_at": utcnow_iso(),
                "last_error_type": type(exc).__name__,
                "attempts": int(record.get("attempts", 0)) + 1,
            }
            self._write(path, record)
            return {"status": "queued", "turn_id": record["turn_id"]}
        path.unlink(missing_ok=True)
        self._fsync_dir(self.pending_dir)
        return {"status": "delivered", "turn_id": record["turn_id"]}

    def _record_failure(self, path: Path, error_type: str) -> None:
        try:
            record = self._load(path)
            record["last_error_type"] = error_type
            self._write(path, record)
        except Exception:
            pass

    def _load(self, path: Path) -> dict[str, Any]:
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or record.get("version") != _OUTBOX_VERSION:
            raise ValueError("Unsupported team outbox record.")
        if record.get("status") not in {"preparing", "prepared", "signing", "ready"}:
            raise ValueError("Malformed team outbox status.")
        if not isinstance(record.get("turn_id"), str) or path.stem != record["turn_id"]:
            raise ValueError("Team outbox identity does not match its filename.")
        VerifiedTeamTurn.from_dict(record["turn"])
        return record

    def _path(self, task_id: str, attempt_id: str) -> Path:
        identity = hashlib.sha256((task_id + "\0" + attempt_id).encode("utf-8")).hexdigest()
        return self.pending_dir / f"{identity}.json"

    def _write(self, path: Path, record: Mapping[str, Any]) -> None:
        payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
        fd, temporary = tempfile.mkstemp(prefix=".turn-", dir=self.pending_dir)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            self._fsync_dir(self.pending_dir)
        finally:
            Path(temporary).unlink(missing_ok=True)

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
