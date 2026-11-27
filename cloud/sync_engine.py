"""Bidirectional personal sync orchestration over authenticated peer sessions."""

from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import re
import tempfile
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .p2p_transport import SecurePeerSession
from .sync_contract import SyncEnvelope, SyncStream, canonical_json_bytes, ledger_scope

_PROTOCOL_VERSION = 1
_CAPABILITIES = ("ledger", "cognition")
_MAX_CONTROL_BYTES = 4 * 1024 * 1024
_MAX_TASKS = 10_000
_MAX_PAGES_PER_TASK = 100_000


class SyncEngineError(Exception):
    """Raised when peers cannot complete a validated sync exchange."""


@dataclass(slots=True)
class SyncResult:
    remote_device_id: str
    ledger_pages_sent: int = 0
    ledger_pages_received: int = 0
    ledger_events_sent: int = 0
    ledger_events_received: int = 0
    dialogue_turns_received: int = 0
    cognition_sent: bool = False
    cognition_received: bool = False
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    peer_summary: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "remote_device_id": self.remote_device_id,
            "ledger_pages_sent": self.ledger_pages_sent,
            "ledger_pages_received": self.ledger_pages_received,
            "ledger_events_sent": self.ledger_events_sent,
            "ledger_events_received": self.ledger_events_received,
            "dialogue_turns_received": self.dialogue_turns_received,
            "cognition_sent": self.cognition_sent,
            "cognition_received": self.cognition_received,
            "conflicts": self.conflicts,
        }


def _validate_device_id(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise SyncEngineError("Invalid peer device id.")
    if any(ord(char) < 32 or char.isspace() for char in value):
        raise SyncEngineError("Invalid peer device id.")
    return value


def _validate_task_id(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise SyncEngineError("Invalid ledger task id.")
    return value


def _validate_hash(value: Any) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
        raise SyncEngineError("Invalid sync hash.")
    return value.lower()


def _validate_nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SyncEngineError(f"Invalid {label}.")
    return value


def _require_fields(value: dict[str, Any], expected: set[str]) -> None:
    if set(value) != expected:
        raise SyncEngineError("Unexpected sync control message fields.")


def _load_control_json(raw: bytes) -> dict[str, Any]:
    def object_from_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise SyncEngineError("Duplicate sync control key.")
            result[key] = item
        return result

    def reject_constant(value: str) -> Any:
        raise SyncEngineError("Non-standard JSON constant in sync control message.")

    try:
        parsed = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=object_from_pairs,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise SyncEngineError("Invalid sync control JSON.") from exc
    if not isinstance(parsed, dict):
        raise SyncEngineError("Sync control message must be an object.")
    return parsed


def _validate_capabilities(value: Any) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > len(_CAPABILITIES):
        raise SyncEngineError("Invalid sync capabilities.")
    if any(item not in _CAPABILITIES for item in value) or len(set(value)) != len(value):
        raise SyncEngineError("Unsupported sync capabilities.")
    return list(value)


def _validate_catalog(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > _MAX_TASKS:
        raise SyncEngineError("Invalid ledger task catalog.")
    task_ids = [_validate_task_id(item) for item in value]
    if len(set(task_ids)) != len(task_ids):
        raise SyncEngineError("Ledger task catalog contains duplicates.")
    return task_ids


def _default_state(workspace_id: str) -> dict[str, Any]:
    return {"version": 1, "workspace_id": workspace_id, "peers": {}}


def _load_state(path: Path, workspace_id: str) -> dict[str, Any]:
    if not path.exists():
        return _default_state(workspace_id)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        raise SyncEngineError("Sync cursor state is unreadable.") from exc
    if not isinstance(value, dict) or set(value) != {"version", "workspace_id", "peers"}:
        raise SyncEngineError("Sync cursor state has an invalid schema.")
    if type(value["version"]) is not int or value["version"] != 1 or value["workspace_id"] != workspace_id:
        raise SyncEngineError("Sync cursor state belongs to another schema or workspace.")
    if not isinstance(value["peers"], dict):
        raise SyncEngineError("Sync cursor state has invalid peers.")
    for peer_id, peer_state in value["peers"].items():
        _validate_device_id(peer_id)
        if not isinstance(peer_state, dict) or set(peer_state) != {"ledger", "cognition"}:
            raise SyncEngineError("Sync cursor state has invalid peer data.")
        if not isinstance(peer_state["ledger"], dict) or not isinstance(peer_state["cognition"], dict):
            raise SyncEngineError("Sync cursor state has invalid stream data.")
        for task_id, cursor in peer_state["ledger"].items():
            _validate_task_id(task_id)
            _validate_nonnegative_int(cursor, "ledger cursor")
        cognition = peer_state["cognition"]
        if cognition:
            if set(cognition) != {"revision", "manifest_sha256"}:
                raise SyncEngineError("Sync cursor state has invalid cognition data.")
            _validate_nonnegative_int(cognition["revision"], "cognition revision")
            _validate_hash(cognition["manifest_sha256"])
    return value


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)
        raise


async def _send_control(session: SecurePeerSession, message_type: str, **fields: Any) -> None:
    raw = canonical_json_bytes(
        {"protocol_version": _PROTOCOL_VERSION, "type": message_type, **fields}
    )
    if len(raw) > _MAX_CONTROL_BYTES:
        raise SyncEngineError("Sync control message is too large.")
    await session.send(raw)


async def _receive_control(
    session: SecurePeerSession, expected_type: str
) -> dict[str, Any]:
    raw = await session.receive()
    if raw is None:
        raise SyncEngineError("Peer closed during sync control exchange.")
    if len(raw) > _MAX_CONTROL_BYTES:
        raise SyncEngineError("Sync control message is too large.")
    value = _load_control_json(raw)
    if not {"protocol_version", "type"}.issubset(value):
        raise SyncEngineError("Sync control message is missing required fields.")
    if type(value["protocol_version"]) is not int or value["protocol_version"] != _PROTOCOL_VERSION:
        raise SyncEngineError("Unsupported sync protocol version.")
    message_type = value["type"]
    if message_type == "sync_error":
        message = value.get("message")
        if not isinstance(message, str) or len(message) > 500:
            raise SyncEngineError("Peer synchronization failed.")
        raise SyncEngineError(message)
    if message_type != expected_type:
        raise SyncEngineError(f"Expected {expected_type}, received {message_type}.")
    return {key: item for key, item in value.items() if key not in {"protocol_version", "type"}}


def _validate_delta(delta: Any, task_id: str, since_seq: int) -> dict[str, Any]:
    expected = {
        "version", "since_seq", "task", "events", "base_event_sha256",
        "dialogue_turns", "next_seq", "has_more",
    }
    if not isinstance(delta, dict) or set(delta) != expected:
        raise SyncEngineError("Invalid ledger delta payload.")
    if type(delta["version"]) is not int or delta["version"] != 1:
        raise SyncEngineError("Unsupported ledger delta version.")
    if (
        type(delta["since_seq"]) is not int
        or delta["since_seq"] != since_seq
    ):
        raise SyncEngineError("Ledger delta cursor does not match its envelope.")
    if not isinstance(delta["task"], dict) or delta["task"].get("task_id") != task_id:
        raise SyncEngineError("Ledger delta task does not match its scope.")
    if not isinstance(delta["events"], list) or not isinstance(delta["dialogue_turns"], list):
        raise SyncEngineError("Ledger delta contains invalid event data.")
    if not isinstance(delta["has_more"], bool):
        raise SyncEngineError("Ledger delta has an invalid continuation flag.")
    next_seq = _validate_nonnegative_int(delta["next_seq"], "ledger next cursor")
    if next_seq < since_seq or (delta["has_more"] and next_seq <= since_seq):
        raise SyncEngineError("Ledger delta cursor does not advance.")
    _validate_hash(delta["base_event_sha256"])
    return delta


class SyncEngine:
    def __init__(
        self,
        ledger: Any,
        cognition: Any,
        device_id: str,
        *,
        state_path: Path | str | None = None,
        page_size: int = 500,
        timeout: float = 120.0,
    ) -> None:
        self.ledger = ledger
        self.cognition = cognition
        self.device_id = _validate_device_id(device_id)
        if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 5000:
            raise ValueError("page_size must be between 1 and 5000.")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("timeout must be finite and positive.")
        self.page_size = page_size
        self.timeout = float(timeout)
        self.workspace_id = str(cognition.workspace_id)
        self.state_path = (
            Path(state_path).resolve()
            if state_path is not None
            else Path(cognition.global_home) / ".sync" / "engine" / f"{self.workspace_id}.json"
        )
        self._state = _load_state(self.state_path, self.workspace_id)
        self._sync_lock = asyncio.Lock()

    def status(self) -> dict[str, Any]:
        return copy.deepcopy(self._state)

    def _peer_state(self, remote_device_id: str) -> dict[str, Any]:
        return self._state["peers"].setdefault(
            remote_device_id, {"ledger": {}, "cognition": {}}
        )

    def _ledger_cursor(self, remote_device_id: str, task_id: str) -> int:
        return int(self._peer_state(remote_device_id)["ledger"].get(task_id, 0))

    def _advance_ledger_cursor(
        self, remote_device_id: str, task_id: str, next_seq: int
    ) -> None:
        _validate_nonnegative_int(next_seq, "ledger cursor")
        self._peer_state(remote_device_id)["ledger"][task_id] = next_seq
        _save_state(self.state_path, self._state)

    def _advance_cognition_cursor(
        self, remote_device_id: str, revision: int, manifest_sha256: str
    ) -> None:
        _validate_nonnegative_int(revision, "cognition revision")
        _validate_hash(manifest_sha256)
        self._peer_state(remote_device_id)["cognition"] = {
            "revision": revision,
            "manifest_sha256": manifest_sha256.lower(),
        }
        _save_state(self.state_path, self._state)

    async def sync_with(self, session: SecurePeerSession) -> SyncResult:
        async with self._sync_lock:
            remote_device_id = _validate_device_id(session.remote_device_id)
            if remote_device_id == self.device_id:
                raise SyncEngineError("A device cannot sync with itself.")
            try:
                operation = (
                    self._run_initiator(session, remote_device_id)
                    if self.device_id < remote_device_id
                    else self._run_responder(session, remote_device_id)
                )
                return await asyncio.wait_for(operation, timeout=self.timeout)
            except SyncEngineError:
                with suppress(Exception):
                    await _send_control(
                        session, "sync_error", message="Peer synchronization failed validation."
                    )
                raise
            except Exception as exc:
                with suppress(Exception):
                    await _send_control(
                        session, "sync_error", message="Peer synchronization failed."
                    )
                raise SyncEngineError("Peer synchronization failed.") from exc

    async def _run_initiator(
        self, session: SecurePeerSession, remote_device_id: str
    ) -> SyncResult:
        await _send_control(
            session,
            "sync_hello",
            device_id=self.device_id,
            workspace_id=self.workspace_id,
            capabilities=list(_CAPABILITIES),
        )
        ready = await _receive_control(session, "sync_ready")
        _require_fields(ready, {"device_id", "workspace_id", "capabilities"})
        self._validate_peer_hello(ready, remote_device_id)

        local_catalog = _validate_catalog(await self.ledger.list_sync_task_ids_async())
        await _send_control(session, "ledger_catalog", task_ids=local_catalog)
        remote_fields = await _receive_control(session, "ledger_catalog")
        _require_fields(remote_fields, {"task_ids"})
        remote_catalog = _validate_catalog(remote_fields["task_ids"])

        result = SyncResult(remote_device_id)
        await self._send_ledger_deltas(session, local_catalog, result)
        await self._send_cognition(session, result)
        await self._receive_ledger_deltas(session, remote_catalog, result)
        await self._receive_cognition(session, result)
        await _send_control(session, "sync_complete")
        summary = await _receive_control(session, "sync_result")
        self._validate_peer_summary(summary, remote_device_id)
        result.peer_summary = summary
        return result

    async def _run_responder(
        self, session: SecurePeerSession, remote_device_id: str
    ) -> SyncResult:
        hello = await _receive_control(session, "sync_hello")
        _require_fields(hello, {"device_id", "workspace_id", "capabilities"})
        self._validate_peer_hello(hello, remote_device_id)
        await _send_control(
            session,
            "sync_ready",
            device_id=self.device_id,
            workspace_id=self.workspace_id,
            capabilities=list(_CAPABILITIES),
        )

        remote_fields = await _receive_control(session, "ledger_catalog")
        _require_fields(remote_fields, {"task_ids"})
        remote_catalog = _validate_catalog(remote_fields["task_ids"])
        local_catalog = _validate_catalog(await self.ledger.list_sync_task_ids_async())
        await _send_control(session, "ledger_catalog", task_ids=local_catalog)

        result = SyncResult(remote_device_id)
        await self._receive_ledger_deltas(session, remote_catalog, result)
        await self._receive_cognition(session, result)
        await self._send_ledger_deltas(session, local_catalog, result)
        await self._send_cognition(session, result)
        complete = await _receive_control(session, "sync_complete")
        _require_fields(complete, set())
        summary = result.to_dict()
        del summary["remote_device_id"]
        summary["device_id"] = self.device_id
        await _send_control(session, "sync_result", **summary)
        return result

    def _validate_peer_hello(
        self, value: dict[str, Any], remote_device_id: str
    ) -> None:
        if value["device_id"] != remote_device_id:
            raise SyncEngineError("Peer control identity does not match the authenticated session.")
        if value["workspace_id"] != self.workspace_id:
            raise SyncEngineError("Peer workspace id does not match this workspace.")
        remote_capabilities = set(_validate_capabilities(value["capabilities"]))
        if remote_capabilities != set(_CAPABILITIES):
            raise SyncEngineError("Peers do not support the same sync capabilities.")

    async def _send_ledger_deltas(
        self,
        session: SecurePeerSession,
        task_ids: list[str],
        result: SyncResult,
    ) -> None:
        for task_id in task_ids:
            since_seq = self._ledger_cursor(result.remote_device_id, task_id)
            for _page in range(_MAX_PAGES_PER_TASK):
                delta = await self.ledger.export_sync_delta_async(
                    task_id, since_seq, limit=self.page_size
                )
                _validate_delta(delta, task_id, since_seq)
                await session.send_envelope(
                    SyncEnvelope(
                        SyncStream.LEDGER,
                        ledger_scope(task_id),
                        self.device_id,
                        since_seq,
                        delta,
                    )
                )
                ack = await _receive_control(session, "ledger_ack")
                _require_fields(
                    ack,
                    {"task_id", "since_seq", "next_seq", "has_more", "status"},
                )
                if ack["task_id"] != task_id or type(ack["since_seq"]) is not int or ack["since_seq"] != since_seq:
                    raise SyncEngineError("Ledger acknowledgement does not match the request.")
                next_seq = _validate_nonnegative_int(ack["next_seq"], "ledger next cursor")
                if next_seq != delta["next_seq"] or ack["has_more"] is not delta["has_more"]:
                    raise SyncEngineError("Ledger acknowledgement cursor does not match the delta.")
                if not isinstance(ack["status"], str):
                    raise SyncEngineError("Ledger acknowledgement has an invalid status.")
                self._advance_ledger_cursor(result.remote_device_id, task_id, next_seq)
                result.ledger_pages_sent += 1
                result.ledger_events_sent += len(delta["events"])
                if not delta["has_more"]:
                    break
                since_seq = next_seq
            else:
                raise SyncEngineError("Ledger delta exceeded the page safety limit.")

    async def _receive_ledger_deltas(
        self,
        session: SecurePeerSession,
        task_ids: list[str],
        result: SyncResult,
    ) -> None:
        for task_id in task_ids:
            expected_since: int | None = None
            for _page in range(_MAX_PAGES_PER_TASK):
                envelope = await session.receive_envelope()
                if envelope is None:
                    raise SyncEngineError("Peer closed during ledger sync.")
                if (
                    envelope.stream != SyncStream.LEDGER
                    or envelope.scope != ledger_scope(task_id)
                    or envelope.sender_device_record_id != result.remote_device_id
                ):
                    raise SyncEngineError("Unexpected ledger sync envelope.")
                if expected_since is not None and envelope.since_seq != expected_since:
                    raise SyncEngineError("Ledger page cursor is discontinuous.")
                delta = _validate_delta(envelope.payload, task_id, envelope.since_seq)
                merged = await self.ledger.merge_remote_chain_async(
                    task_id,
                    delta["events"],
                    task_snapshot=delta["task"],
                    dialogue_turns=delta["dialogue_turns"],
                    sender_device_record_id=result.remote_device_id,
                    since_seq=delta["since_seq"],
                    base_event_sha256=delta["base_event_sha256"],
                )
                next_seq = _validate_nonnegative_int(merged.get("next_seq"), "ledger next cursor")
                if next_seq != delta["next_seq"]:
                    raise SyncEngineError("Merged ledger cursor does not match the delta.")
                await _send_control(
                    session,
                    "ledger_ack",
                    task_id=task_id,
                    since_seq=delta["since_seq"],
                    next_seq=next_seq,
                    has_more=delta["has_more"],
                    status=str(merged["status"]),
                )
                result.ledger_pages_received += 1
                result.ledger_events_received += int(merged.get("imported_events", 0))
                result.dialogue_turns_received += int(merged.get("imported_dialogue_turns", 0))
                if merged["status"] == "conflict":
                    result.conflicts.append(
                        {
                            "kind": "ledger",
                            "task_id": task_id,
                            "branch_id": merged.get("branch_id"),
                        }
                    )
                if not delta["has_more"]:
                    break
                expected_since = next_seq
            else:
                raise SyncEngineError("Ledger delta exceeded the page safety limit.")

    async def _send_cognition(
        self, session: SecurePeerSession, result: SyncResult
    ) -> None:
        manifest = await asyncio.to_thread(self.cognition.export_manifest)
        if manifest.get("workspace_id") != self.workspace_id:
            raise SyncEngineError("Cognition manifest belongs to another workspace.")
        revision = _validate_nonnegative_int(manifest.get("revision"), "cognition revision")
        if revision == 0:
            raise SyncEngineError("Cognition manifest revision must be positive.")
        manifest_sha256 = _validate_hash(manifest.get("manifest_sha256"))
        await session.send_envelope(
            SyncEnvelope(
                SyncStream.COGNITION,
                self.cognition.scope,
                self.device_id,
                revision,
                manifest,
            )
        )
        ack = await _receive_control(session, "cognition_ack")
        _require_fields(
            ack, {"revision", "manifest_sha256", "status", "conflicts"}
        )
        acknowledgement_hash = _validate_hash(ack["manifest_sha256"])
        acknowledgement_revision = _validate_nonnegative_int(
            ack["revision"], "cognition revision"
        )
        if acknowledgement_revision != revision or acknowledgement_hash != manifest_sha256:
            raise SyncEngineError("Cognition acknowledgement does not match the manifest.")
        if not isinstance(ack["status"], str):
            raise SyncEngineError("Cognition acknowledgement has an invalid status.")
        _validate_nonnegative_int(ack["conflicts"], "cognition conflict count")
        self._advance_cognition_cursor(result.remote_device_id, revision, manifest_sha256)
        result.cognition_sent = True

    async def _receive_cognition(
        self, session: SecurePeerSession, result: SyncResult
    ) -> None:
        envelope = await session.receive_envelope()
        if envelope is None:
            raise SyncEngineError("Peer closed during cognition sync.")
        if (
            envelope.stream != SyncStream.COGNITION
            or envelope.scope != self.cognition.scope
            or envelope.sender_device_record_id != result.remote_device_id
        ):
            raise SyncEngineError("Unexpected cognition sync envelope.")
        manifest = envelope.payload
        if manifest.get("workspace_id") != self.workspace_id:
            raise SyncEngineError("Cognition manifest belongs to another workspace.")
        applied = await asyncio.to_thread(
            self.cognition.apply_manifest,
            manifest,
            sender_device_record_id=result.remote_device_id,
        )
        revision = _validate_nonnegative_int(manifest.get("revision"), "cognition revision")
        if revision == 0:
            raise SyncEngineError("Cognition manifest revision must be positive.")
        manifest_sha256 = _validate_hash(manifest.get("manifest_sha256"))
        conflicts = applied.get("conflicts")
        if not isinstance(conflicts, list):
            raise SyncEngineError("Cognition merge returned an invalid conflict list.")
        await _send_control(
            session,
            "cognition_ack",
            revision=revision,
            manifest_sha256=manifest_sha256,
            status=str(applied["status"]),
            conflicts=len(conflicts),
        )
        result.cognition_received = True
        result.conflicts.extend(
            {
                "kind": "cognition",
                "conflict_kind": conflict.get("kind"),
                **{key: value for key, value in conflict.items() if key != "kind"},
            }
            for conflict in conflicts
        )

    @staticmethod
    def _validate_peer_summary(value: dict[str, Any], remote_device_id: str) -> None:
        expected = set(SyncResult(remote_device_id).to_dict())
        expected.remove("remote_device_id")
        expected.add("device_id")
        _require_fields(value, expected)
        if value["device_id"] != remote_device_id:
            raise SyncEngineError("Peer summary identity does not match the session.")
        for key in (
            "ledger_pages_sent", "ledger_pages_received", "ledger_events_sent",
            "ledger_events_received", "dialogue_turns_received",
        ):
            _validate_nonnegative_int(value[key], key)
        for key in ("cognition_sent", "cognition_received"):
            if not isinstance(value[key], bool):
                raise SyncEngineError("Peer summary contains an invalid cognition flag.")
        if not isinstance(value["conflicts"], list):
            raise SyncEngineError("Peer summary contains an invalid conflict list.")


__all__ = ["SyncEngine", "SyncEngineError", "SyncResult"]
