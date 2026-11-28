"""Durable local archive for verified team turns and backend receipts."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Mapping

from harness_core.storage.db import utcnow_iso

from .team_mesh import DEVICE_ID_PATTERN, SignedTurnCommitBundle
from .team_replication import team_payload_digest

TEAM_HISTORY_VERSION = 1
_MAX_HISTORY_RANGE = 10_000


class TeamHistory:
    """Keep exact committed turns locally for authenticated replica catch-up."""

    def __init__(
        self,
        data_dir: Path | str,
        *,
        history_limit: int = 256,
        history_bytes_limit: int = 1024 * 1024 * 1024,
    ) -> None:
        if type(history_limit) is not int or history_limit < 1:
            raise ValueError("history_limit must be a positive integer.")
        if type(history_bytes_limit) is not int or history_bytes_limit < 1:
            raise ValueError("history_bytes_limit must be a positive integer.")
        self.root = Path(data_dir).resolve() / "team_history"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.history_limit = history_limit
        self.history_bytes_limit = history_bytes_limit
        self._lock = threading.RLock()

    def archive(
        self,
        signed: SignedTurnCommitBundle,
        payload: Mapping[str, Any],
        receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(signed, SignedTurnCommitBundle):
            signed = SignedTurnCommitBundle.from_dict(signed)
        if not isinstance(payload, Mapping) or not isinstance(receipt, Mapping):
            raise ValueError("Team history requires a payload and commit receipt.")
        digest = team_payload_digest(payload)
        record = {
            "version": TEAM_HISTORY_VERSION,
            "signed": signed.to_dict(),
            "payload": dict(payload),
            "payload_digest": digest,
            "receipt": dict(receipt),
            "archived_at": utcnow_iso(),
        }
        path = self._path(signed.bundle.project_session_id, signed.bundle.turn_seq)
        with self._lock:
            if path.exists():
                existing = self._load(path)
                if (
                    existing["signed"] != record["signed"]
                    or existing["payload_digest"] != digest
                    or existing["receipt"] != record["receipt"]
                ):
                    raise ValueError("Conflicting team turn already exists in history.")
                return existing
            self._write(path, record)
            self._prune_history()
            return record

    def history_range(
        self, project_session_id: str, first_seq: int, last_seq: int
    ) -> list[dict[str, Any]]:
        if not isinstance(project_session_id, str) or not DEVICE_ID_PATTERN.fullmatch(
            project_session_id
        ):
            raise ValueError("Invalid team history session id.")
        if (
            type(first_seq) is not int
            or type(last_seq) is not int
            or not 1 <= first_seq <= last_seq
            or last_seq - first_seq >= _MAX_HISTORY_RANGE
        ):
            raise ValueError("Invalid team history range.")
        records: list[dict[str, Any]] = []
        with self._lock:
            for path in self.root.glob("*.json"):
                try:
                    record = self._load(path)
                    signed = SignedTurnCommitBundle.from_dict(record["signed"])
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    continue
                bundle = signed.bundle
                if (
                    bundle.project_session_id == project_session_id
                    and first_seq <= bundle.turn_seq <= last_seq
                ):
                    records.append(record)
        records.sort(key=lambda item: item["signed"]["bundle"]["turn_seq"])
        return records

    def status(self) -> dict[str, Any]:
        with self._lock:
            paths = list(self.root.glob("*.json"))
            corrupt = 0
            for path in paths:
                try:
                    self._load(path)
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    corrupt += 1
            return {
                "records": len(paths) - corrupt,
                "corrupt_records": corrupt,
                "bytes": sum(path.stat().st_size for path in paths),
                "history_limit": self.history_limit,
                "history_bytes_limit": self.history_bytes_limit,
            }

    def _path(self, project_session_id: str, turn_seq: int) -> Path:
        identity = hashlib.sha256(
            (project_session_id + "\0" + str(turn_seq)).encode("utf-8")
        ).hexdigest()
        return self.root / f"{identity}.json"

    def _load(self, path: Path) -> dict[str, Any]:
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or set(record) != {
            "version", "signed", "payload", "payload_digest", "receipt", "archived_at"
        }:
            raise ValueError("Malformed team history record.")
        if record["version"] != TEAM_HISTORY_VERSION:
            raise ValueError("Unsupported team history record.")
        signed = SignedTurnCommitBundle.from_dict(record["signed"])
        if not isinstance(record["payload"], Mapping) or not isinstance(record["receipt"], Mapping):
            raise ValueError("Malformed team history content.")
        if team_payload_digest(record["payload"]) != record["payload_digest"]:
            raise ValueError("Team history payload digest does not match.")
        if not isinstance(record["archived_at"], str) or not record["archived_at"]:
            raise ValueError("Malformed team history timestamp.")
        if path != self._path(signed.bundle.project_session_id, signed.bundle.turn_seq):
            raise ValueError("Team history identity does not match its filename.")
        return record

    def _write(self, path: Path, record: Mapping[str, Any]) -> None:
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
        descriptor, temporary = tempfile.mkstemp(prefix=".turn-", dir=self.root)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            self._fsync_dir()
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _prune_history(self) -> None:
        entries: list[tuple[Path, int, int]] = []
        for path in self.root.glob("*.json"):
            try:
                record = self._load(path)
                sequence = SignedTurnCommitBundle.from_dict(record["signed"]).bundle.turn_seq
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
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
        self._fsync_dir()

    def _fsync_dir(self) -> None:
        descriptor = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
