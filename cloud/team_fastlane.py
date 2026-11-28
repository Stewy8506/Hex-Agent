"""Signed fast-lane activity, soft scope claims, and the local Team Blackboard."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .team_mesh import DEVICE_ID_PATTERN, canonical_json_bytes

TEAM_ACTIVITY_VERSION = 1
TEAM_ACTIVITY_DOMAIN = b"hex/team-activity/v1\0"
TEAM_ACTIVITY_KINDS = frozenset({
    "PEER_STATE", "INTENT_CLAIM", "SCOPE_RELEASE", "CONTRACT_CHANGE",
    "BLACKBOARD_NOTE", "PEER_QUERY_ANSWER",
})
_PEER_STATES = frozenset({"IDLE", "PLANNING", "RUNNING", "VERIFYING", "COMPLETED"})
_EVENT_FIELDS = frozenset({
    "version", "event_id", "kind", "project_session_id", "host_epoch",
    "author_device_record_id", "created_at", "summary", "paths", "symbols", "data",
})
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_REQUEST_RE = re.compile(r"^[0-9a-f]{32}$")
_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{14,}|ghp_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{25,}|"
    r"(?:api_key|secret|token|password)\s*[:=]\s*['\"]?[A-Za-z0-9_-]{8,}['\"]?)",
    re.IGNORECASE,
)
_CREDENTIAL_NAMES = frozenset({
    ".env", ".env.local", ".env.production", "id_rsa", "id_ed25519",
    "credentials", "credentials.json", ".npmrc", ".pypirc", ".netrc",
})


class TeamActivityError(ValueError):
    pass


def _redact(value: str) -> str:
    return _SECRET_RE.sub("[REDACTED_SECRET]", value)


def _text(value: Any, label: str, limit: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise TeamActivityError(f"Invalid team activity {label}.")
    if len(value) > limit or "\x00" in value:
        raise TeamActivityError(f"Team activity {label} is too large.")
    return _redact(value)


def _path(value: Any) -> str:
    value = _text(value, "path", 512).replace("\\", "/")
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise TeamActivityError("Team activity paths must be workspace-relative.")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise TeamActivityError("Team activity path traversal is not allowed.")
    if parts[-1].lower() in _CREDENTIAL_NAMES or parts[-1].startswith(".env."):
        raise TeamActivityError("Credential paths cannot be published to a team.")
    return "/".join(parts)


def _paths(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > 32:
        raise TeamActivityError("Invalid team activity path set.")
    normalized = [_path(item) for item in value]
    if len(set(normalized)) != len(normalized):
        raise TeamActivityError("Duplicate team activity paths.")
    return sorted(normalized)


def _symbols(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > 64:
        raise TeamActivityError("Invalid team activity symbol set.")
    normalized = []
    for item in value:
        symbol = _text(item, "symbol", 256)
        if any(character.isspace() for character in symbol):
            raise TeamActivityError("Team activity symbols must be identifiers.")
        normalized.append(symbol)
    if len(set(normalized)) != len(normalized):
        raise TeamActivityError("Duplicate team activity symbols.")
    return sorted(normalized)


def validate_activity_event(value: Any, project_session_id: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _EVENT_FIELDS:
        raise TeamActivityError("Malformed team activity event.")
    if type(value["version"]) is not int or value["version"] != TEAM_ACTIVITY_VERSION:
        raise TeamActivityError("Unsupported team activity version.")
    if not isinstance(value["event_id"], str) or not _ID_RE.fullmatch(value["event_id"]):
        raise TeamActivityError("Invalid team activity id.")
    kind = value["kind"]
    if kind not in TEAM_ACTIVITY_KINDS:
        raise TeamActivityError("Unsupported team activity kind.")
    if value["project_session_id"] != project_session_id:
        raise TeamActivityError("Team activity belongs to a different session.")
    if type(value["host_epoch"]) is not int or value["host_epoch"] < 1:
        raise TeamActivityError("Invalid team activity epoch.")
    author = value["author_device_record_id"]
    if not isinstance(author, str) or not DEVICE_ID_PATTERN.fullmatch(author):
        raise TeamActivityError("Invalid team activity author.")
    if type(value["created_at"]) is not int or value["created_at"] < 1:
        raise TeamActivityError("Invalid team activity timestamp.")
    summary = _text(value["summary"], "summary", 1024)
    paths = _paths(value["paths"])
    symbols = _symbols(value["symbols"])
    data = value["data"]
    if not isinstance(data, Mapping):
        raise TeamActivityError("Malformed team activity data.")
    data = dict(data)
    if kind == "PEER_STATE":
        if set(data) != {"state"} or data["state"] not in _PEER_STATES:
            raise TeamActivityError("Invalid peer state event.")
    elif kind in {"INTENT_CLAIM", "SCOPE_RELEASE"}:
        required = {"task_id", "claim_id"}
        if kind == "INTENT_CLAIM":
            required.add("ttl_seconds")
        if set(data) != required:
            raise TeamActivityError("Invalid scope claim event.")
        data["task_id"] = _text(data["task_id"], "task id", 512)
        data["claim_id"] = _text(data["claim_id"], "claim id", 128)
        if not _ID_RE.fullmatch(data["claim_id"]):
            raise TeamActivityError("Invalid scope claim id.")
        if kind == "INTENT_CLAIM":
            if type(data["ttl_seconds"]) is not int or not 1 <= data["ttl_seconds"] <= 300:
                raise TeamActivityError("Invalid scope claim TTL.")
            if not paths and not symbols:
                raise TeamActivityError("A scope claim requires paths or symbols.")
        elif not paths and not symbols:
            raise TeamActivityError("A scope release requires its claimed paths or symbols.")
    elif kind == "CONTRACT_CHANGE":
        if set(data) != {"task_id", "path", "symbol", "before", "after"}:
            raise TeamActivityError("Invalid contract change event.")
        data["task_id"] = _text(data["task_id"], "task id", 512)
        data["path"] = _path(data["path"])
        data["symbol"] = _text(data["symbol"], "symbol", 256)
        data["before"] = _text(data["before"], "previous contract", 512, empty=True)
        data["after"] = _text(data["after"], "new contract", 512, empty=True)
        if data["path"] not in paths:
            paths.append(data["path"])
            paths.sort()
        if data["symbol"] not in symbols:
            symbols.append(data["symbol"])
            symbols.sort()
        if len(paths) > 32 or len(symbols) > 64:
            raise TeamActivityError("Team activity path or symbol set is too large.")
    elif kind == "BLACKBOARD_NOTE":
        if set(data) != {"task_id", "body", "pinned"} or type(data["pinned"]) is not bool:
            raise TeamActivityError("Invalid blackboard note.")
        if data["task_id"] is not None:
            data["task_id"] = _text(data["task_id"], "task id", 512)
        data["body"] = _text(data["body"], "blackboard body", 4096)
    else:
        if set(data) != {"request_id", "question", "answer", "peer_device_record_id"}:
            raise TeamActivityError("Invalid peer query answer.")
        if not isinstance(data["request_id"], str) or not _REQUEST_RE.fullmatch(data["request_id"]):
            raise TeamActivityError("Invalid peer query id.")
        peer = data["peer_device_record_id"]
        if not isinstance(peer, str) or not DEVICE_ID_PATTERN.fullmatch(peer) or peer != author:
            raise TeamActivityError("Invalid queried peer.")
        data["question"] = _text(data["question"], "query question", 4096)
        data["answer"] = _text(data["answer"], "query answer", 16384)
    encoded = canonical_json_bytes({**dict(value), "summary": summary, "paths": paths, "symbols": symbols, "data": data})
    if len(encoded) > 24 * 1024:
        raise TeamActivityError("Team activity event is too large.")
    return json.loads(encoded)


def activity_signing_bytes(event: Mapping[str, Any]) -> bytes:
    return TEAM_ACTIVITY_DOMAIN + canonical_json_bytes(dict(event))


def sign_activity_event(event: Mapping[str, Any], signer: Any, project_session_id: str) -> dict[str, Any]:
    validated = validate_activity_event(event, project_session_id)
    if validated["author_device_record_id"] != signer.device_id:
        raise TeamActivityError("Only the author can sign a team activity event.")
    signature = signer.sign(activity_signing_bytes(validated))
    return {
        "event": validated,
        "signature": "ed25519:" + base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii"),
    }


def verify_activity_event(
    envelope: Any,
    *,
    project_session_id: str,
    public_key: str,
    now: float | None = None,
) -> dict[str, Any]:
    if not isinstance(envelope, Mapping) or set(envelope) != {"event", "signature"}:
        raise TeamActivityError("Malformed signed team activity.")
    event = validate_activity_event(envelope["event"], project_session_id)
    header = envelope["signature"]
    if not isinstance(header, str) or not header.startswith("ed25519:"):
        raise TeamActivityError("Team activity must use Ed25519.")
    segment = header.split(":", 1)[1]
    if not re.fullmatch(r"[A-Za-z0-9_-]{86}", segment):
        raise TeamActivityError("Malformed team activity signature.")
    normalized_key = public_key.removeprefix("ed25519:")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", normalized_key):
        raise TeamActivityError("Invalid enrolled activity key.")
    try:
        signature = base64.b64decode(segment + "==", altchars=b"-_", validate=True)
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(normalized_key)).verify(
            signature, activity_signing_bytes(event)
        )
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise TeamActivityError("Team activity signature verification failed.") from exc
    current = time.time() if now is None else now
    if abs(event["created_at"] - current) > 300:
        raise TeamActivityError("Team activity timestamp is outside the acceptance window.")
    return event


def make_activity_event(
    *,
    signer: Any,
    project_session_id: str,
    host_epoch: int,
    kind: str,
    summary: str,
    paths: list[str] | None = None,
    symbols: list[str] | None = None,
    data: Mapping[str, Any] | None = None,
    created_at: int | None = None,
) -> dict[str, Any]:
    return sign_activity_event({
        "version": TEAM_ACTIVITY_VERSION,
        "event_id": uuid.uuid4().hex,
        "kind": kind,
        "project_session_id": project_session_id,
        "host_epoch": host_epoch,
        "author_device_record_id": signer.device_id,
        "created_at": int(time.time() if created_at is None else created_at),
        "summary": summary,
        "paths": list(paths or []),
        "symbols": list(symbols or []),
        "data": dict(data or {}),
    }, signer, project_session_id)


def _paths_overlap(left: list[str], right: list[str]) -> bool:
    for first in left:
        for second in right:
            if first == second or first.startswith(second + "/") or second.startswith(first + "/"):
                return True
    return False


def activity_conflicts(event: Mapping[str, Any], claim: Mapping[str, Any]) -> bool:
    if event["author_device_record_id"] == claim["author_device_record_id"]:
        return False
    if not _paths_overlap(event["paths"], claim["paths"]):
        return False
    if not event["symbols"] or not claim["symbols"]:
        return True
    return bool(set(event["symbols"]) & set(claim["symbols"]))


class TeamActivityStore:
    """Bounded, tamper-evident local journal shared by every Teams surface."""

    def __init__(
        self,
        workspace_root: Path | str,
        project_session_id: str,
        *,
        event_limit: int = 1024,
        bytes_limit: int = 8 * 1024 * 1024,
    ) -> None:
        if not isinstance(project_session_id, str) or not DEVICE_ID_PATTERN.fullmatch(project_session_id):
            raise ValueError("Invalid team project session id.")
        if type(event_limit) is not int or event_limit < 1 or type(bytes_limit) is not int or bytes_limit < 1:
            raise ValueError("Team activity limits must be positive integers.")
        self.project_session_id = project_session_id
        self.root = Path(workspace_root).resolve() / ".hex"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.path = self.root / "team_blackboard.jsonl"
        self.event_limit = event_limit
        self.bytes_limit = bytes_limit
        self._lock = threading.RLock()
        self._entries: list[dict[str, Any]] = []
        self._event_ids: dict[str, dict[str, Any]] = {}
        self._tip = "0" * 64
        self._load()

    def accept(
        self,
        envelope: Mapping[str, Any],
        public_key: str,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        event = verify_activity_event(
            envelope,
            project_session_id=self.project_session_id,
            public_key=public_key,
            now=now,
        )
        event_id = event["event_id"]
        with self._lock:
            existing = self._event_ids.get(event_id)
            if existing is not None:
                if existing != event:
                    raise TeamActivityError("Conflicting team activity id.")
                return {"accepted": True, "duplicate": True, "event": existing}
            entry = {"envelope": {"event": event, "signature": envelope["signature"]}}
            entry["previous_hash"] = self._tip
            entry["entry_hash"] = hashlib.sha256(
                self._tip.encode("ascii") + canonical_json_bytes(entry)
            ).hexdigest()
            self._entries.append(entry)
            self._event_ids[event_id] = event
            self._tip = entry["entry_hash"]
            self._prune_locked()
            self._write_locked()
            return {"accepted": True, "duplicate": False, "event": event}

    def scope_locks(self, *, now: float | None = None, host_epoch: int | None = None) -> list[dict[str, Any]]:
        current = time.time() if now is None else now
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        with self._lock:
            events = [entry["envelope"]["event"] for entry in self._entries]
        for event in events:
            if event["kind"] not in {"INTENT_CLAIM", "SCOPE_RELEASE"}:
                continue
            if host_epoch is not None and event["host_epoch"] != host_epoch:
                continue
            key = (event["author_device_record_id"], event["data"]["task_id"])
            previous = latest.get(key)
            if previous is None or event["created_at"] >= previous["created_at"]:
                if (
                    event["kind"] == "SCOPE_RELEASE"
                    and previous is not None
                    and previous["kind"] == "INTENT_CLAIM"
                    and event["data"]["claim_id"] != previous["data"]["claim_id"]
                ):
                    continue
                latest[key] = event
        locks = []
        for event in latest.values():
            if event["kind"] != "INTENT_CLAIM":
                continue
            expires_at = event["created_at"] + event["data"]["ttl_seconds"]
            if expires_at <= current:
                continue
            locks.append({
                **event,
                "expires_at": expires_at,
                "conflicts": False,
            })
        for index, lock in enumerate(locks):
            lock["conflicts"] = any(
                activity_conflicts(lock, other)
                for other_index, other in enumerate(locks)
                if other_index != index
            )
        locks.sort(key=lambda item: (item["expires_at"], item["author_device_record_id"], item["data"]["task_id"]))
        return locks

    def peer_activity(self, *, host_epoch: int | None = None) -> list[dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        with self._lock:
            events = [entry["envelope"]["event"] for entry in self._entries]
        for event in events:
            if event["kind"] != "PEER_STATE":
                continue
            if host_epoch is not None and event["host_epoch"] != host_epoch:
                continue
            previous = latest.get(event["author_device_record_id"])
            if previous is None or event["created_at"] >= previous["created_at"]:
                latest[event["author_device_record_id"]] = event
        return sorted(latest.values(), key=lambda item: item["author_device_record_id"])

    def blackboard(self, *, limit: int = 50) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Blackboard limit must be between 1 and 100.")
        with self._lock:
            events = [entry["envelope"]["event"] for entry in self._entries]
        notes = [
            event for event in events
            if event["kind"] in {"BLACKBOARD_NOTE", "PEER_QUERY_ANSWER"}
        ]
        notes.sort(key=lambda item: (item["data"].get("pinned") is True, item["created_at"], item["event_id"]), reverse=True)
        return notes[:limit]

    def recent_activity(self, *, limit: int = 50, host_epoch: int | None = None) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Activity limit must be between 1 and 100.")
        with self._lock:
            events = [entry["envelope"]["event"] for entry in self._entries]
        if host_epoch is not None:
            events = [event for event in events if event["host_epoch"] == host_epoch]
        events.sort(key=lambda item: (item["created_at"], item["event_id"]), reverse=True)
        return events[:limit]

    def status(self) -> dict[str, Any]:
        with self._lock:
            size = self.path.stat().st_size if self.path.exists() else 0
            return {
                "events": len(self._entries),
                "bytes": size,
                "event_limit": self.event_limit,
                "bytes_limit": self.bytes_limit,
                "corrupt": False,
                "path": str(self.path),
            }

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = self.path.read_bytes()
            if len(raw) > self.bytes_limit + 64 * 1024:
                raise TeamActivityError("Team activity journal exceeds its limit.")
            previous = "0" * 64
            for line in raw.splitlines():
                entry = json.loads(line)
                if not isinstance(entry, dict) or set(entry) != {
                    "envelope", "previous_hash", "entry_hash"
                }:
                    raise TeamActivityError("Malformed team activity journal entry.")
                if entry["previous_hash"] != previous:
                    raise TeamActivityError("Team activity journal chain is broken.")
                digest = hashlib.sha256(
                    previous.encode("ascii") + canonical_json_bytes({
                        key: value for key, value in entry.items() if key != "entry_hash"
                    })
                ).hexdigest()
                if digest != entry["entry_hash"]:
                    raise TeamActivityError("Team activity journal digest mismatch.")
                envelope = entry["envelope"]
                if not isinstance(envelope, Mapping) or set(envelope) != {"event", "signature"}:
                    raise TeamActivityError("Malformed stored team activity.")
                event = validate_activity_event(envelope["event"], self.project_session_id)
                self._entries.append(entry)
                self._event_ids[event["event_id"]] = event
                previous = digest
            self._tip = previous
        except (OSError, UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise TeamActivityError("Stored team activity journal is malformed.") from exc

    def _prune_locked(self) -> None:
        def encoded_size(entries: list[dict[str, Any]]) -> int:
            return sum(len(canonical_json_bytes(entry)) + 1 for entry in entries)

        total = encoded_size(self._entries)
        remove = 0
        while remove < len(self._entries) and (
            len(self._entries) - remove > self.event_limit or total > self.bytes_limit
        ):
            total -= len(canonical_json_bytes(self._entries[remove])) + 1
            remove += 1
        if remove:
            removed = self._entries[:remove]
            self._entries = self._entries[remove:]
            for entry in removed:
                self._event_ids.pop(entry["envelope"]["event"]["event_id"], None)
            previous = "0" * 64
            for entry in self._entries:
                entry["previous_hash"] = previous
                entry["entry_hash"] = hashlib.sha256(
                    previous.encode("ascii") + canonical_json_bytes({
                        key: value for key, value in entry.items() if key != "entry_hash"
                    })
                ).hexdigest()
                previous = entry["entry_hash"]
            self._tip = previous

    def _write_locked(self) -> None:
        encoded = b"".join(canonical_json_bytes(entry) + b"\n" for entry in self._entries)
        descriptor, temporary = tempfile.mkstemp(prefix=".team-blackboard-", dir=self.root)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)
