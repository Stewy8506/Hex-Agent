"""Durable, verified team-turn packaging and delivery."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from harness_core.capabilities.proof import sign_proof_artifact
from harness_core.storage.db import utcnow_iso

from .team_mesh import (
    DEVICE_ID_PATTERN,
    TurnCommitBundle,
    SignedTurnCommitBundle,
    sign_turn_commit,
)
from .team_replication import team_payload_digest

_OUTBOX_VERSION = 1
_MAX_ARCHIVE_BYTES = 48 * 1024 * 1024
_MAX_PAYLOAD_BYTES = 64 * 1024 * 1024
_MAX_LEDGER_EVENTS = 10_000


class _AsyncScopeRequired(ValueError):
    pass


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
        scope_provider: Callable[[str], Mapping[str, Any] | Awaitable[Mapping[str, Any]]],
        publisher: Callable[[SignedTurnCommitBundle, Mapping[str, Any]], Any],
        history_limit: int = 256,
        history_bytes_limit: int = 1024 * 1024 * 1024,
    ) -> None:
        self.root = Path(data_dir).resolve() / "team_outbox"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.pending_dir = self.root / "pending"
        self.pending_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.history_dir = self.root / "history"
        self.history_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        os.chmod(self.pending_dir, 0o700)
        os.chmod(self.history_dir, 0o700)
        if type(history_limit) is not int or history_limit < 1:
            raise ValueError("history_limit must be a positive integer.")
        if type(history_bytes_limit) is not int or history_bytes_limit < 1:
            raise ValueError("history_bytes_limit must be a positive integer.")
        self.history_limit = history_limit
        self.history_bytes_limit = history_bytes_limit
        self.ledger = ledger
        self.cow = cow
        self.cognition = cognition
        self.signer = signer
        self.scope_provider = scope_provider
        self.publisher = publisher
        self._lock = threading.RLock()
        self._inflight: set[str] = set()

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(list(self.pending_dir.glob("*.json")))

    def history_range(
        self, project_session_id: str, first_seq: int, last_seq: int
    ) -> list[dict[str, Any]]:
        if type(first_seq) is not int or type(last_seq) is not int or not 1 <= first_seq <= last_seq:
            raise ValueError("Invalid team history range.")
        with self._lock:
            records: list[dict[str, Any]] = []
            for path in self.history_dir.glob("*.json"):
                try:
                    record = self._load(path)
                    signed = SignedTurnCommitBundle.from_dict(record["signed"])
                except Exception:
                    continue
                if (
                    signed.bundle.project_session_id == project_session_id
                    and first_seq <= signed.bundle.turn_seq <= last_seq
                ):
                    records.append(record)
            records.sort(key=lambda item: item["signed"]["bundle"]["turn_seq"])
            return records

    def __call__(self, turn: VerifiedTeamTurn) -> dict[str, Any]:
        with self._lock:
            record = self._load_or_create(turn)
            try:
                record = self._advance(record)
            except _AsyncScopeRequired:
                return {"status": "queued", "turn_id": record["turn_id"]}
            if record["status"] == "skipped":
                return {"status": "skipped", "reason": "NO_WORKSPACE_CHANGES"}
            return self._deliver(record)

    def flush(self) -> dict[str, Any]:
        delivered = 0
        queued = 0
        with self._lock:
            for path in sorted(self.pending_dir.glob("*.json")):
                try:
                    record = self._advance(self._load(path))
                    if record["status"] == "skipped":
                        continue
                    outcome = self._deliver(record)
                    if outcome["status"] == "delivered":
                        delivered += 1
                    else:
                        queued += 1
                except Exception as exc:
                    queued += 1
                    self._record_failure(path, type(exc).__name__)
        return {"delivered": delivered, "queued": queued}

    async def publish_async(self, turn: VerifiedTeamTurn) -> dict[str, Any]:
        with self._lock:
            record = self._load_or_create(turn)
        record = await self._advance_async(record)
        if record["status"] == "skipped":
            return {"status": "skipped", "reason": "NO_WORKSPACE_CHANGES"}
        return await self._deliver_async(record)

    async def flush_async(self) -> dict[str, Any]:
        delivered = 0
        queued = 0
        for path in sorted(self.pending_dir.glob("*.json")):
            try:
                record = await self._load_and_advance_async(path)
                if record is None:
                    continue
                outcome = await self._deliver_async(record)
                if outcome["status"] == "delivered":
                    delivered += 1
                else:
                    queued += 1
            except Exception as exc:
                queued += 1
                self._record_failure(path, type(exc).__name__)
        return {"delivered": delivered, "queued": queued}

    async def _advance_async(self, record: dict[str, Any]) -> dict[str, Any]:
        if record["status"] == "preparing":
            record = await asyncio.to_thread(self._prepare, record)
        if record["status"] == "skipped":
            return record
        if record["status"] in {"prepared", "signing"}:
            turn = VerifiedTeamTurn.from_dict(record["turn"])
            scope = await self._scope_async(turn.task_id)
            record = await asyncio.to_thread(self._sign, record, scope=scope)
        elif record["status"] == "ready":
            turn = VerifiedTeamTurn.from_dict(record["turn"])
            scope = await self._scope_async(turn.task_id)
            if scope != record["scope"]:
                record = await asyncio.to_thread(self._sign, record, scope=scope)
        if record["status"] != "ready":
            raise ValueError("Team turn is not ready for delivery.")
        return record

    async def _load_and_advance_async(self, path: Path) -> dict[str, Any] | None:
        record = await asyncio.to_thread(self._load, path)
        record = await self._advance_async(record)
        return None if record["status"] == "skipped" else record

    async def _scope_async(self, task_id: str) -> Mapping[str, Any]:
        if inspect.iscoroutinefunction(self.scope_provider):
            scope = await self.scope_provider(task_id)
        else:
            scope = await asyncio.to_thread(self.scope_provider, task_id)
            if inspect.isawaitable(scope):
                scope = await scope
        return self._validated_scope(scope)

    def _sync_scope(self, task_id: str) -> Mapping[str, Any]:
        if inspect.iscoroutinefunction(self.scope_provider):
            raise _AsyncScopeRequired(
                "An async team scope provider requires publish_async() or flush_async()."
            )
        scope = self.scope_provider(task_id)
        if inspect.isawaitable(scope):
            if inspect.iscoroutine(scope):
                scope.close()
            raise _AsyncScopeRequired(
                "An async team scope provider requires publish_async() or flush_async()."
            )
        return self._validated_scope(scope)

    def _advance(self, record: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if record["status"] == "preparing":
                record = self._prepare(record)
            if record["status"] == "skipped":
                return record
            if record["status"] in {"prepared", "signing"}:
                record = self._sign(record)
            elif record["status"] == "ready":
                turn = VerifiedTeamTurn.from_dict(record["turn"])
                scope = self._validated_scope(self._sync_scope(turn.task_id))
                if scope != record["scope"]:
                    record = self._sign(record, scope=scope)
            if record["status"] != "ready":
                raise ValueError("Team turn is not ready for delivery.")
            return record

    def _load_and_advance(self, path: Path) -> dict[str, Any] | None:
        with self._lock:
            record = self._advance(self._load(path))
            return None if record["status"] == "skipped" else record

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

    def _sign(
        self, record: dict[str, Any], *, scope: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        turn = VerifiedTeamTurn.from_dict(record["turn"])
        prepared = record["prepared"]
        selected_scope = self._validated_scope(
            self._sync_scope(turn.task_id) if scope is None else scope
        )
        if record.get("scope") != selected_scope:
            record = {**record, "status": "signing", "scope": selected_scope}
            self._write(self._path(turn.task_id, turn.attempt_id), record)
        scope = selected_scope
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

    @staticmethod
    def _validated_scope(scope: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(scope, Mapping) or set(scope) != {
            "project_session_id", "host_epoch", "turn_seq"
        }:
            raise ValueError("Team turn scope is malformed.")
        if not isinstance(scope["project_session_id"], str) or not DEVICE_ID_PATTERN.fullmatch(
            scope["project_session_id"]
        ):
            raise ValueError("Team turn scope has an invalid session id.")
        if type(scope["host_epoch"]) is not int or scope["host_epoch"] < 1:
            raise ValueError("Team turn scope has an invalid epoch.")
        if type(scope["turn_seq"]) is not int or scope["turn_seq"] < 1:
            raise ValueError("Team turn scope has an invalid sequence.")
        return dict(scope)

    def _ready_turn(self, record: dict[str, Any]) -> tuple[Path, SignedTurnCommitBundle, Mapping[str, Any]]:
        path = self.pending_dir / f"{record['turn_id']}.json"
        signed = SignedTurnCommitBundle.from_dict(record["signed"])
        payload = record["payload"]
        if not signed.verify(self.signer.signing_public_key):
            raise ValueError("Pending signed team turn failed verification.")
        if team_payload_digest(payload) != record.get("payload_digest"):
            raise ValueError("Pending team payload changed.")
        return path, signed, payload

    def _claim_delivery(self, turn_id: str) -> bool:
        with self._lock:
            if turn_id in self._inflight:
                return False
            self._inflight.add(turn_id)
            return True

    def _release_delivery(self, turn_id: str) -> None:
        with self._lock:
            self._inflight.discard(turn_id)

    def _deliver(self, record: dict[str, Any]) -> dict[str, Any]:
        path, signed, payload = self._ready_turn(record)
        if not self._claim_delivery(record["turn_id"]):
            return {"status": "queued", "turn_id": record["turn_id"]}
        try:
            if inspect.iscoroutinefunction(self.publisher):
                self._write_failure(path, record, "ASYNC_PUBLISHER_REQUIRED")
                return {"status": "queued", "turn_id": record["turn_id"]}
            try:
                result = self.publisher(signed, payload)
                if inspect.isawaitable(result):
                    if inspect.iscoroutine(result):
                        result.close()
                    self._write_failure(path, record, "ASYNC_PUBLISHER_REQUIRED")
                    return {"status": "queued", "turn_id": record["turn_id"]}
            except Exception as exc:
                self._write_failure(path, record, type(exc).__name__)
                return {"status": "queued", "turn_id": record["turn_id"]}
            if not self._complete_delivery(path, record, result):
                return {"status": "queued", "turn_id": record["turn_id"]}
            return {"status": "delivered", "turn_id": record["turn_id"]}
        finally:
            self._release_delivery(record["turn_id"])

    async def _deliver_async(self, record: dict[str, Any]) -> dict[str, Any]:
        path, signed, payload = self._ready_turn(record)
        if not self._claim_delivery(record["turn_id"]):
            return {"status": "queued", "turn_id": record["turn_id"]}
        try:
            try:
                result = self.publisher(signed, payload)
                if inspect.isawaitable(result):
                    result = await result
            except Exception as exc:
                self._write_failure(path, record, type(exc).__name__)
                return {"status": "queued", "turn_id": record["turn_id"]}
            if not self._complete_delivery(path, record, result):
                return {"status": "queued", "turn_id": record["turn_id"]}
            return {"status": "delivered", "turn_id": record["turn_id"]}
        finally:
            self._release_delivery(record["turn_id"])

    def _complete_delivery(
        self, path: Path, record: dict[str, Any], result: Any
    ) -> bool:
        accepted = (
            result.get("accepted") is True or result.get("ack") is True
            if isinstance(result, Mapping)
            else result is True
        )
        if not accepted:
            self._write_failure(path, record, "RuntimeError")
            return False
        self._archive_history(path, record)
        return True

    def _write_failure(self, path: Path, record: dict[str, Any], error_type: str) -> None:
        failed = {
            **record,
            "last_attempt_at": utcnow_iso(),
            "last_error_type": error_type,
            "attempts": int(record.get("attempts", 0)) + 1,
        }
        self._write(path, failed)

    def _archive_history(self, pending_path: Path, record: Mapping[str, Any]) -> None:
        history_path = self.history_dir / pending_path.name
        if history_path.exists():
            existing = self._load(history_path)
            if (
                existing.get("signed") != record.get("signed")
                or existing.get("payload_digest") != record.get("payload_digest")
            ):
                raise ValueError("Conflicting team turn already exists in history.")
            pending_path.unlink(missing_ok=True)
        else:
            os.replace(pending_path, history_path)
        self._fsync_dir(self.history_dir)
        self._fsync_dir(self.pending_dir)
        self._prune_history()

    def _prune_history(self) -> None:
        entries: list[tuple[Path, int, int]] = []
        for path in self.history_dir.glob("*.json"):
            try:
                record = self._load(path)
                signed = SignedTurnCommitBundle.from_dict(record["signed"])
                sequence = signed.bundle.turn_seq
            except Exception:
                continue
            entries.append((path, path.stat().st_size, sequence))
        entries.sort(key=lambda item: (item[0].stat().st_mtime_ns, item[2], item[0].name))
        total_bytes = sum(item[1] for item in entries)
        excess = max(0, len(entries) - self.history_limit)
        for path, size, _sequence in entries:
            if excess <= 0 and total_bytes <= self.history_bytes_limit:
                break
            path.unlink(missing_ok=True)
            excess -= 1
            total_bytes -= size
        self._fsync_dir(self.history_dir)

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
