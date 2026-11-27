"""Wire contract shared with crates/hex-protocol for personal device sync."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

try:
    from enum import StrEnum
except ImportError:
    from backports.strenum import StrEnum


class SyncStream(StrEnum):
    LEDGER = "ledger"
    COGNITION = "cognition"
    WORKSPACE = "workspace"


@dataclass(frozen=True, slots=True)
class SyncEnvelope:
    stream: SyncStream
    scope: str
    sender_device_record_id: str
    since_seq: int
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "stream": self.stream.value,
            "scope": self.scope,
            "sender_device_record_id": self.sender_device_record_id,
            "since_seq": self.since_seq,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SyncEnvelope":
        if not isinstance(value, dict):
            raise ValueError("Sync envelope must be an object.")
        try:
            stream = SyncStream(value["stream"])
            scope = value["scope"]
            sender = value["sender_device_record_id"]
            since_seq = value["since_seq"]
            payload = value["payload"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Invalid sync envelope.") from exc
        if not isinstance(scope, str) or not scope or len(scope) > 512:
            raise ValueError("Sync scope must be a non-empty string.")
        if not isinstance(sender, str) or not sender or len(sender) > 256:
            raise ValueError("Sync sender device id must be a non-empty string.")
        if isinstance(since_seq, bool) or not isinstance(since_seq, int) or since_seq < 0:
            raise ValueError("Sync cursor must be a non-negative integer.")
        if not isinstance(payload, dict):
            raise ValueError("Sync payload must be an object.")
        return cls(stream, scope, sender, since_seq, payload)


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def ledger_scope(task_id: str) -> str:
    if not isinstance(task_id, str) or not task_id or len(task_id) > 256:
        raise ValueError("task_id must be a non-empty string.")
    return f"ledger:{task_id}"


def task_id_from_ledger_scope(scope: str) -> str:
    prefix = "ledger:"
    if not isinstance(scope, str) or not scope.startswith(prefix):
        raise ValueError("Ledger sync scope must start with 'ledger:'.")
    task_id = scope[len(prefix):]
    if not task_id or len(task_id) > 256:
        raise ValueError("Ledger sync scope contains an invalid task id.")
    return task_id


def cognition_scope(workspace_id: str) -> str:
    if not isinstance(workspace_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", workspace_id
    ):
        raise ValueError("workspace_id must be a safe bounded identifier.")
    return f"cognition:{workspace_id}"


def workspace_id_from_cognition_scope(scope: str) -> str:
    prefix = "cognition:"
    if not isinstance(scope, str) or not scope.startswith(prefix):
        raise ValueError("Cognition sync scope must start with 'cognition:'.")
    workspace_id = scope[len(prefix):]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", workspace_id):
        raise ValueError("Cognition sync scope contains an invalid workspace id.")
    return workspace_id
