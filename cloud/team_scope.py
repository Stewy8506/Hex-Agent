"""Durable Coordinator scope reservation for signed team turns."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:
    msvcrt = None  # type: ignore[assignment]

from .team_mesh import DEVICE_ID_PATTERN
from .updater import verify_signed_envelope

_SCOPE_VERSION = 1
_SCOPE_FIELDS = {"project_session_id", "host_epoch", "turn_seq"}


class TeamScopeUnavailable(RuntimeError):
    """The trusted Coordinator lease cannot currently allocate another turn."""


class TeamScopeAllocator:
    """Serialize next-sequence reservations against verified backend leases."""

    def __init__(
        self,
        data_dir: Path | str,
        *,
        local_device_id: str,
        server_public_key_hex: str,
        lease_provider: Callable[[], Mapping[str, Any]],
    ) -> None:
        if not isinstance(local_device_id, str) or not DEVICE_ID_PATTERN.fullmatch(local_device_id):
            raise ValueError("Invalid local Coordinator device id.")
        if not callable(lease_provider):
            raise TypeError("lease_provider must be callable.")
        self.root = Path(data_dir).resolve() / "team_scope"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.state_path = self.root / "state.json"
        self.lock_path = self.root / "reserve.lock"
        self.local_device_id = local_device_id
        self.server_public_key_hex = server_public_key_hex
        self.lease_provider = lease_provider
        self._thread_lock = threading.RLock()

    def reserve(self, task_id: str) -> dict[str, Any]:
        if not isinstance(task_id, str) or not task_id or len(task_id) > 512:
            raise ValueError("A valid task id is required for team scope reservation.")
        with self._thread_lock, self._process_lock():
            state = self._load()
            lease = self._verified_lease()
            session_id = lease["project_session_id"]
            epoch = lease["host_epoch"]
            committed = lease["last_committed_turn_seq"]
            active = state["active"]
            if (
                active is not None
                and active["project_session_id"] == session_id
                and active["host_epoch"] == epoch
                and active["turn_seq"] > committed
            ):
                if active["task_id"] == task_id:
                    reservation = state["reservations"].get(task_id)
                    if reservation != {key: active[key] for key in _SCOPE_FIELDS}:
                        raise ValueError("Active team scope reservation is inconsistent.")
                    return dict(reservation)
                reservation = state["reservations"].get(task_id)
                if (
                    reservation is not None
                    and reservation["project_session_id"] == session_id
                    and reservation["host_epoch"] == epoch
                    and reservation["turn_seq"] <= committed
                ):
                    return dict(reservation)
                raise TeamScopeUnavailable(
                    "Another team turn already owns the next Coordinator sequence."
                )

            reservation = state["reservations"].get(task_id)
            if (
                reservation is not None
                and reservation["project_session_id"] == session_id
                and reservation["host_epoch"] == epoch
            ):
                return dict(reservation)

            scope = {
                "project_session_id": session_id,
                "host_epoch": epoch,
                "turn_seq": committed + 1,
            }
            state["reservations"][task_id] = scope
            state["active"] = {"task_id": task_id, **scope}
            self._write(state)
            return dict(scope)

    def status(self) -> dict[str, Any]:
        with self._thread_lock, self._process_lock():
            state = self._load()
            return {
                "active": dict(state["active"]) if state["active"] is not None else None,
                "reserved_tasks": len(state["reservations"]),
            }

    def _verified_lease(self) -> dict[str, Any]:
        signed = self.lease_provider()
        if not isinstance(signed, Mapping):
            raise TeamScopeUnavailable("Coordinator lease is malformed.")
        envelope = dict(signed)
        if isinstance(envelope.get("payload"), Mapping):
            envelope["payload"] = dict(envelope["payload"])
        if not verify_signed_envelope(envelope, self.server_public_key_hex):
            raise TeamScopeUnavailable("Coordinator lease signature verification failed.")
        payload = envelope.get("payload")
        required = {
            "project_session_id", "host_epoch", "primary_device_record_id",
            "last_committed_turn_seq",
        }
        if not isinstance(payload, Mapping) or not required.issubset(payload):
            raise TeamScopeUnavailable("Coordinator lease is malformed.")
        session_id = payload["project_session_id"]
        primary = payload["primary_device_record_id"]
        epoch = payload["host_epoch"]
        committed = payload["last_committed_turn_seq"]
        if not isinstance(session_id, str) or not DEVICE_ID_PATTERN.fullmatch(session_id):
            raise TeamScopeUnavailable("Coordinator lease has an invalid session id.")
        if not isinstance(primary, str) or not DEVICE_ID_PATTERN.fullmatch(primary):
            raise TeamScopeUnavailable("Coordinator lease has an invalid device id.")
        if type(epoch) is not int or epoch < 1:
            raise TeamScopeUnavailable("Coordinator lease has an invalid epoch.")
        if type(committed) is not int or committed < 0:
            raise TeamScopeUnavailable("Coordinator lease has an invalid sequence.")
        if primary != self.local_device_id:
            raise TeamScopeUnavailable("This device is not the active Coordinator.")
        return {
            "project_session_id": session_id,
            "host_epoch": epoch,
            "last_committed_turn_seq": committed,
        }

    def _load(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"version": _SCOPE_VERSION, "reservations": {}, "active": None}
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or set(state) != {"version", "reservations", "active"}:
            raise ValueError("Malformed team scope state.")
        if state["version"] != _SCOPE_VERSION or not isinstance(state["reservations"], dict):
            raise ValueError("Unsupported team scope state.")
        for task_id, scope in state["reservations"].items():
            if not isinstance(task_id, str) or not task_id or len(task_id) > 512:
                raise ValueError("Malformed team scope task id.")
            self._validate_scope(scope)
        active = state["active"]
        if active is not None:
            if not isinstance(active, dict) or set(active) != {"task_id", *_SCOPE_FIELDS}:
                raise ValueError("Malformed active team scope reservation.")
            if not isinstance(active["task_id"], str) or not active["task_id"]:
                raise ValueError("Malformed active team scope task id.")
            self._validate_scope({key: active[key] for key in _SCOPE_FIELDS})
            if state["reservations"].get(active["task_id"]) != {
                key: active[key] for key in _SCOPE_FIELDS
            }:
                raise ValueError("Active team scope reservation is inconsistent.")
        return state

    @staticmethod
    def _validate_scope(scope: Any) -> None:
        if not isinstance(scope, dict) or set(scope) != _SCOPE_FIELDS:
            raise ValueError("Malformed team scope reservation.")
        if not isinstance(scope["project_session_id"], str) or not DEVICE_ID_PATTERN.fullmatch(
            scope["project_session_id"]
        ):
            raise ValueError("Malformed team scope session id.")
        if type(scope["host_epoch"]) is not int or scope["host_epoch"] < 1:
            raise ValueError("Malformed team scope epoch.")
        if type(scope["turn_seq"]) is not int or scope["turn_seq"] < 1:
            raise ValueError("Malformed team scope sequence.")

    @contextmanager
    def _process_lock(self) -> Iterator[None]:
        if fcntl is None and msvcrt is None:
            raise RuntimeError("Team scope reservation requires an OS file lock.")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.lock_path, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            if msvcrt is not None and os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
                os.lseek(descriptor, 0, os.SEEK_SET)
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            else:
                msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
            yield
        finally:
            try:
                if fcntl is not None:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                else:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            os.close(descriptor)

    def _write(self, state: Mapping[str, Any]) -> None:
        raw = json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")
        descriptor, temporary = tempfile.mkstemp(prefix=".scope-", dir=self.root)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)
