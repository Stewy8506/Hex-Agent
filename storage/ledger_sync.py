"""Cursor export and conflict-safe import for personal ledger synchronization."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
from typing import TYPE_CHECKING, Any

from harness_core.core.models import utcnow_iso

if TYPE_CHECKING:
    from harness_core.storage.db import LedgerStore

_EVENT_FIELDS = (
    "event_id", "task_id", "attempt_id", "sequence_no", "event_type",
    "prior_state", "next_state", "reason_code", "workspace_tree_sha",
    "policy_version", "payload_json", "actor", "occurred_at",
    "correlation_id", "event_sha256",
)
_BRANCH_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_TERMINAL_STATES = {"SUCCEEDED", "FAILED", "CANCELLED", "REJECTED"}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _validate_page(since_seq: Any, limit: Any) -> None:
    if isinstance(since_seq, bool) or not isinstance(since_seq, int) or since_seq < 0:
        raise ValueError("since_seq must be a non-negative integer.")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5000:
        raise ValueError("limit must be between 1 and 5000.")


def _stored_event(row: Any) -> dict[str, Any]:
    event = {field: row[field] for field in _EVENT_FIELDS}
    payload = event["payload_json"]
    event["payload"] = json.loads(payload) if isinstance(payload, str) else payload
    return event


def _validate_chain(
    task_id: str,
    events: Any,
    *,
    since_seq: int = 0,
    base_event_sha256: str | None = None,
) -> list[dict[str, Any]]:
    from harness_core.storage.db import compute_event_hash

    if not isinstance(task_id, str) or not task_id or len(task_id) > 256:
        raise ValueError("task_id must be a non-empty string.")
    _validate_page(since_seq, 1)
    if base_event_sha256 is not None and not isinstance(base_event_sha256, str):
        raise ValueError("Invalid remote ledger base hash.")
    previous_hash = base_event_sha256 or ("0" * 64)
    if not re.fullmatch(r"[0-9a-f]{64}", previous_hash):
        raise ValueError("Invalid remote ledger base hash.")
    if since_seq == 0 and base_event_sha256 not in (None, "0" * 64):
        raise ValueError("A full ledger chain must start from the zero hash.")
    if since_seq > 0 and base_event_sha256 is None:
        raise ValueError("A ledger delta requires its base event hash.")
    if not isinstance(events, list) or len(events) > 10_000:
        raise ValueError("Ledger events must be a list of at most 10000 entries.")
    normalized: list[dict[str, Any]] = []
    expected_sequence = since_seq + 1
    event_ids: set[str] = set()
    for raw in events:
        if not isinstance(raw, dict) or raw.get("task_id") != task_id:
            raise ValueError("Every ledger event must belong to the requested task.")
        payload = raw.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("Ledger event payload must be an object.")
        sequence = raw.get("sequence_no")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence != expected_sequence:
            raise ValueError("Remote ledger chain has a sequence discontinuity.")
        for field in ("event_id", "event_type", "actor", "occurred_at", "correlation_id", "event_sha256"):
            if not isinstance(raw.get(field), str) or not raw[field] or len(raw[field]) > 4096:
                raise ValueError(f"Remote ledger event has an invalid {field}.")
        for field in ("attempt_id", "prior_state", "next_state", "reason_code", "workspace_tree_sha", "policy_version"):
            if raw.get(field) is not None and not isinstance(raw[field], str):
                raise ValueError(f"Remote ledger event has an invalid {field}.")
        if raw["event_id"] in event_ids:
            raise ValueError("Remote ledger chain contains duplicate event ids.")
        if not re.fullmatch(r"[0-9a-f]{64}", raw["event_sha256"]):
            raise ValueError("Remote ledger event has an invalid SHA-256 hash.")
        payload_json = _canonical(payload)
        event = {field: raw.get(field) for field in _EVENT_FIELDS}
        event["payload_json"] = payload_json
        event["payload"] = payload
        computed = compute_event_hash(
            prev_hash=previous_hash,
            task_id=task_id,
            seq_no=sequence,
            event_type=raw["event_type"],
            prior_state=raw.get("prior_state"),
            next_state=raw.get("next_state"),
            reason_code=str(raw.get("reason_code")),
            workspace_tree_sha=raw.get("workspace_tree_sha"),
            payload_str=payload_json,
            occurred_at=raw["occurred_at"],
        )
        if computed != raw["event_sha256"]:
            raise ValueError(f"Remote ledger hash mismatch at sequence {sequence}.")
        event_ids.add(raw["event_id"])
        normalized.append(event)
        previous_hash = computed
        expected_sequence += 1
    return normalized


def _same_event(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return all(left[field] == right[field] for field in _EVENT_FIELDS)


def _merge_plan(
    task_id: str, local: list[dict[str, Any]], remote: list[dict[str, Any]]
) -> tuple[str, int, str | None]:
    if not remote:
        return "unchanged", 0, None
    common = 0
    for left, right in zip(local, remote):
        if not _same_event(left, right):
            break
        common += 1
    if common == len(remote):
        return "unchanged", 0, None
    if common == len(local):
        return "append", common, None
    digest = hashlib.sha256(f"{task_id}|{remote[-1]['event_sha256']}".encode()).hexdigest()
    return "conflict", 0, f"br_sync_{digest[:24]}"


def _anchored_remote(
    local: list[dict[str, Any]],
    remote: list[dict[str, Any]],
    since_seq: int,
    base_event_sha256: str | None,
) -> list[dict[str, Any]]:
    if since_seq == 0:
        return remote
    if len(local) < since_seq or local[since_seq - 1]["event_sha256"] != base_event_sha256:
        raise ValueError("Remote delta base does not match the local ledger chain.")
    return [*local[:since_seq], *remote]


def _validate_sender(sender: Any) -> str:
    if not isinstance(sender, str) or not sender or len(sender) > 256:
        raise ValueError("sender_device_record_id must be a non-empty string.")
    return sender


def _validate_turns(task_id: str, turns: Any) -> list[dict[str, Any]]:
    if not isinstance(turns, list) or len(turns) > 50_000:
        raise ValueError("Dialogue turns must be a list of at most 50000 entries.")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in turns:
        if not isinstance(raw, dict) or raw.get("task_id") != task_id:
            raise ValueError("Every dialogue turn must belong to the requested task.")
        for field in ("turn_id", "role", "content", "branch_id", "created_at"):
            if not isinstance(raw.get(field), str):
                raise ValueError(f"Dialogue turn has an invalid {field}.")
        if not raw["turn_id"] or raw["turn_id"] in seen:
            raise ValueError("Dialogue turns contain empty or duplicate ids.")
        if not isinstance(raw.get("turn_index"), int) or isinstance(raw.get("turn_index"), bool):
            raise ValueError("Dialogue turn index must be an integer.")
        if not isinstance(raw.get("metadata"), dict):
            raise ValueError("Dialogue turn metadata must be an object.")
        active = raw.get("is_active")
        if not isinstance(active, (bool, int)) or active not in (0, 1, False, True):
            raise ValueError("Dialogue turn is_active must be boolean.")
        seen.add(raw["turn_id"])
        result.append({
            "turn_id": raw["turn_id"], "task_id": task_id,
            "turn_index": raw["turn_index"], "role": raw["role"],
            "content": raw["content"], "is_active": int(active),
            "branch_id": raw["branch_id"], "metadata_json": _canonical(raw["metadata"]),
            "created_at": raw["created_at"],
        })
    return result


def _synced_task_values(snapshot: dict[str, Any], sender: str) -> tuple[Any, ...]:
    for field in ("goal", "created_at", "updated_at"):
        if not isinstance(snapshot.get(field), str):
            raise ValueError(f"Task snapshot has an invalid {field}.")
    constraints = snapshot.get("constraints") or {}
    budget = snapshot.get("budget") or {}
    if not isinstance(constraints, dict) or not isinstance(budget, dict):
        raise ValueError("Task snapshot constraints and budget must be objects.")
    priority = snapshot.get("priority", 50)
    source_status = snapshot.get("status")
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise ValueError("Task snapshot has an invalid priority.")
    if source_status is not None and not isinstance(source_status, str):
        raise ValueError("Task snapshot has an invalid status.")
    for field in ("deliverable", "policy_snapshot_id", "active_workspace_tree_sha", "terminal_reason"):
        if snapshot.get(field) is not None and not isinstance(snapshot[field], str):
            raise ValueError(f"Task snapshot has an invalid {field}.")
    status = source_status if source_status in _TERMINAL_STATES else "SUCCEEDED"
    constraints = {**constraints, "hex_sync_imported": True, "hex_sync_source_device": sender}
    return (
        snapshot["goal"], status, priority, _canonical(budget), _canonical(constraints),
        snapshot.get("deliverable"), snapshot.get("policy_snapshot_id") or "sync-imported",
        snapshot.get("active_workspace_tree_sha"), snapshot["created_at"],
        snapshot["updated_at"], snapshot.get("terminal_reason") or "SYNC_IMPORTED_HISTORY",
    )


class LedgerSyncMixin:
    def export_events_since(
        self: LedgerStore,
        task_id: str,
        since_seq: int = 0,
        *,
        limit: int = 500,
        branch_id: str = "main",
    ) -> list[dict[str, Any]]:
        _validate_page(since_seq, limit)
        if branch_id != "main" and (not isinstance(branch_id, str) or not _BRANCH_ID.fullmatch(branch_id)):
            raise ValueError("Invalid ledger branch id.")
        if self.backend_type == "postgres":
            return self._run_sync(
                self.export_events_since_async, task_id, since_seq,
                limit=limit, branch_id=branch_id,
            )
        with self._lock:
            if branch_id == "main":
                rows = self.conn.execute(
                    "SELECT * FROM task_events WHERE task_id = ? AND sequence_no > ? "
                    "ORDER BY sequence_no ASC LIMIT ?",
                    (task_id, since_seq, limit),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT e.* FROM sync_ledger_branch_events e "
                    "JOIN sync_ledger_branches b ON b.branch_id = e.branch_id "
                    "WHERE e.task_id = ? AND e.branch_id = ? AND e.sequence_no > ? "
                    "ORDER BY e.sequence_no ASC LIMIT ?",
                    (task_id, branch_id, since_seq, limit),
                ).fetchall()
            return [_stored_event(row) for row in rows]

    async def export_events_since_async(
        self: LedgerStore,
        task_id: str,
        since_seq: int = 0,
        *,
        limit: int = 500,
        branch_id: str = "main",
    ) -> list[dict[str, Any]]:
        _validate_page(since_seq, limit)
        if branch_id != "main" and (not isinstance(branch_id, str) or not _BRANCH_ID.fullmatch(branch_id)):
            raise ValueError("Invalid ledger branch id.")
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(
                self.export_events_since, task_id, since_seq, limit=limit, branch_id=branch_id
            )
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            if branch_id == "main":
                rows = await conn.fetch(
                    "SELECT * FROM task_events WHERE task_id = $1 AND sequence_no > $2 "
                    "ORDER BY sequence_no ASC LIMIT $3",
                    task_id, since_seq, limit,
                )
            else:
                rows = await conn.fetch(
                    "SELECT e.* FROM sync_ledger_branch_events e "
                    "JOIN sync_ledger_branches b ON b.branch_id = e.branch_id "
                    "WHERE e.task_id = $1 AND e.branch_id = $2 AND e.sequence_no > $3 "
                    "ORDER BY e.sequence_no ASC LIMIT $4",
                    task_id, branch_id, since_seq, limit,
                )
            return [_stored_event(row) for row in rows]

    def export_sync_delta(
        self: LedgerStore, task_id: str, since_seq: int = 0, *, limit: int = 500
    ) -> dict[str, Any]:
        _validate_page(since_seq, limit)
        if self.backend_type == "postgres":
            return self._run_sync(self.export_sync_delta_async, task_id, since_seq, limit=limit)
        with self._lock:
            task = self.get_task(task_id)
            if task is None:
                raise KeyError(f"Task not found: {task_id}")
            events = self.export_events_since(task_id, since_seq, limit=limit)
            next_seq = events[-1]["sequence_no"] if events else since_seq
            base_row = self.conn.execute(
                "SELECT event_sha256 FROM task_events WHERE task_id = ? AND sequence_no = ?",
                (task_id, since_seq),
            ).fetchone()
            base_hash = base_row["event_sha256"] if base_row else "0" * 64
            if since_seq > 0 and base_row is None:
                raise ValueError("since_seq does not identify an existing ledger event.")
            return {
                "version": 1,
                "since_seq": since_seq,
                "task": task,
                "events": events,
                "base_event_sha256": base_hash,
                "dialogue_turns": self.get_all_dialogue_turns(task_id),
                "next_seq": next_seq,
                "has_more": bool(
                    self.conn.execute(
                        "SELECT 1 FROM task_events WHERE task_id = ? AND sequence_no > ? LIMIT 1",
                        (task_id, next_seq),
                    ).fetchone()
                ),
            }

    async def export_sync_delta_async(
        self: LedgerStore, task_id: str, since_seq: int = 0, *, limit: int = 500
    ) -> dict[str, Any]:
        _validate_page(since_seq, limit)
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.export_sync_delta, task_id, since_seq, limit=limit)
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("SELECT * FROM tasks WHERE task_id = $1", task_id)
                if row is None:
                    raise KeyError(f"Task not found: {task_id}")
                task = self._row_to_dict(row)
                event_rows = await conn.fetch(
                    "SELECT * FROM task_events WHERE task_id = $1 AND sequence_no > $2 "
                    "ORDER BY sequence_no ASC LIMIT $3",
                    task_id, since_seq, limit,
                )
                events = [_stored_event(event_row) for event_row in event_rows]
                next_seq = events[-1]["sequence_no"] if events else since_seq
                base_hash = await conn.fetchval(
                    "SELECT event_sha256 FROM task_events WHERE task_id = $1 AND sequence_no = $2",
                    task_id, since_seq,
                )
                if since_seq > 0 and base_hash is None:
                    raise ValueError("since_seq does not identify an existing ledger event.")
                base_hash = base_hash or "0" * 64
                more = await conn.fetchval(
                    "SELECT 1 FROM task_events WHERE task_id = $1 AND sequence_no > $2 LIMIT 1",
                    task_id, next_seq,
                )
                turn_rows = await conn.fetch(
                    "SELECT * FROM dialogue_turns WHERE task_id = $1 "
                    "ORDER BY turn_index ASC, created_at ASC", task_id,
                )
                turns = [self._row_to_dict(turn_row) for turn_row in turn_rows]
        return {
            "version": 1, "since_seq": since_seq, "task": task, "events": events,
            "base_event_sha256": base_hash,
            "dialogue_turns": turns, "next_seq": next_seq, "has_more": bool(more),
        }

    def merge_remote_chain(
        self: LedgerStore,
        task_id: str,
        events: list[dict[str, Any]],
        *,
        task_snapshot: dict[str, Any] | None = None,
        sender_device_record_id: str = "remote",
        dialogue_turns: list[dict[str, Any]] | None = None,
        since_seq: int = 0,
        base_event_sha256: str | None = None,
    ) -> dict[str, Any]:
        sender = _validate_sender(sender_device_record_id)
        remote = _validate_chain(
            task_id, events, since_seq=since_seq, base_event_sha256=base_event_sha256
        )
        turns = _validate_turns(task_id, dialogue_turns or [])
        if task_snapshot is not None:
            if not isinstance(task_snapshot, dict) or task_snapshot.get("task_id") != task_id:
                raise ValueError("Task snapshot does not match the ledger task id.")
        if self.backend_type == "postgres":
            return self._run_sync(
                self.merge_remote_chain_async, task_id, remote,
                task_snapshot=task_snapshot, sender_device_record_id=sender,
                dialogue_turns=turns, since_seq=since_seq,
                base_event_sha256=base_event_sha256,
            )
        with self._lock:
            try:
                with self.conn:
                    local_task = self.conn.execute(
                        "SELECT task_id FROM tasks WHERE task_id = ?", (task_id,)
                    ).fetchone()
                    imported_task = False
                    if local_task is None:
                        if task_snapshot is None:
                            raise ValueError("A task snapshot is required for a new remote task.")
                        self._insert_synced_task_sqlite(task_id, task_snapshot, sender)
                        imported_task = True
                    local_rows = self.conn.execute(
                        "SELECT * FROM task_events WHERE task_id = ? ORDER BY sequence_no",
                        (task_id,),
                    ).fetchall()
                    local = [_stored_event(row) for row in local_rows]
                    incoming = _anchored_remote(local, remote, since_seq, base_event_sha256)
                    action, offset, branch_id = _merge_plan(task_id, local, incoming)
                    imported = 0
                    if action == "append":
                        for event in incoming[offset:]:
                            self._insert_sync_event_sqlite("task_events", event)
                            imported += 1
                    elif action == "conflict":
                        assert branch_id is not None
                        existing = self.conn.execute(
                            "SELECT 1 FROM sync_ledger_branches WHERE branch_id = ?",
                            (branch_id,),
                        ).fetchone()
                        if existing:
                            branch_rows = self.conn.execute(
                                "SELECT * FROM sync_ledger_branch_events WHERE branch_id = ? "
                                "ORDER BY sequence_no", (branch_id,),
                            ).fetchall()
                            if len(branch_rows) != len(incoming) or any(
                                not _same_event(_stored_event(row), event)
                                for row, event in zip(branch_rows, incoming)
                            ):
                                raise ValueError("Conflicting data uses the same remote chain tip.")
                        else:
                            self.conn.execute(
                                "INSERT INTO sync_ledger_branches VALUES (?, ?, ?, ?, ?, ?)",
                                (branch_id, task_id, sender, local[-1]["event_sha256"],
                                 incoming[-1]["event_sha256"], utcnow_iso()),
                            )
                            for event in incoming:
                                self._insert_sync_event_sqlite("sync_ledger_branch_events", event, branch_id)
                                imported += 1
                    imported_turns = self._merge_dialogue_turns_sqlite(task_id, turns)
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"Remote ledger conflicts with existing data: {exc}") from exc
        return {
            "status": action, "task_id": task_id, "imported_events": imported,
            "imported_dialogue_turns": imported_turns, "task_imported": imported_task,
            "branch_id": branch_id, "next_seq": incoming[-1]["sequence_no"] if incoming else since_seq,
        }

    async def merge_remote_chain_async(
        self: LedgerStore,
        task_id: str,
        events: list[dict[str, Any]],
        *,
        task_snapshot: dict[str, Any] | None = None,
        sender_device_record_id: str = "remote",
        dialogue_turns: list[dict[str, Any]] | None = None,
        since_seq: int = 0,
        base_event_sha256: str | None = None,
    ) -> dict[str, Any]:
        sender = _validate_sender(sender_device_record_id)
        remote = _validate_chain(
            task_id, events, since_seq=since_seq, base_event_sha256=base_event_sha256
        )
        turns = _validate_turns(task_id, dialogue_turns or [])
        if task_snapshot is not None:
            if not isinstance(task_snapshot, dict) or task_snapshot.get("task_id") != task_id:
                raise ValueError("Task snapshot does not match the ledger task id.")
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(
                self.merge_remote_chain, task_id, remote, task_snapshot=task_snapshot,
                sender_device_record_id=sender, dialogue_turns=turns,
                since_seq=since_seq, base_event_sha256=base_event_sha256,
            )
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                existing_task = await conn.fetchval(
                    "SELECT task_id FROM tasks WHERE task_id = $1", task_id
                )
                imported_task = False
                if existing_task is None:
                    if task_snapshot is None:
                        raise ValueError("A task snapshot is required for a new remote task.")
                    await self._insert_synced_task_postgres(conn, task_id, task_snapshot, sender)
                    imported_task = True
                rows = await conn.fetch(
                    "SELECT * FROM task_events WHERE task_id = $1 ORDER BY sequence_no", task_id
                )
                local = [_stored_event(row) for row in rows]
                incoming = _anchored_remote(local, remote, since_seq, base_event_sha256)
                action, offset, branch_id = _merge_plan(task_id, local, incoming)
                imported = 0
                if action == "append":
                    for event in incoming[offset:]:
                        await self._insert_sync_event_postgres(conn, "task_events", event)
                        imported += 1
                elif action == "conflict":
                    assert branch_id is not None
                    existing = await conn.fetchval(
                        "SELECT 1 FROM sync_ledger_branches WHERE branch_id = $1", branch_id
                    )
                    if existing:
                        branch_rows = await conn.fetch(
                            "SELECT * FROM sync_ledger_branch_events WHERE branch_id = $1 "
                            "ORDER BY sequence_no", branch_id,
                        )
                        if len(branch_rows) != len(incoming) or any(
                            not _same_event(_stored_event(row), event)
                            for row, event in zip(branch_rows, incoming)
                        ):
                            raise ValueError("Conflicting data uses the same remote chain tip.")
                    else:
                        await conn.execute(
                            "INSERT INTO sync_ledger_branches VALUES ($1, $2, $3, $4, $5, $6)",
                            (branch_id, task_id, sender, local[-1]["event_sha256"],
                             incoming[-1]["event_sha256"], utcnow_iso()),
                        )
                        for event in incoming:
                            await self._insert_sync_event_postgres(
                                conn, "sync_ledger_branch_events", event, branch_id
                            )
                            imported += 1
                imported_turns = await self._merge_dialogue_turns_postgres(conn, task_id, turns)
        return {
            "status": action, "task_id": task_id, "imported_events": imported,
            "imported_dialogue_turns": imported_turns, "task_imported": imported_task,
            "branch_id": branch_id, "next_seq": incoming[-1]["sequence_no"] if incoming else since_seq,
        }

    def list_sync_branches(self: LedgerStore, task_id: str | None = None) -> list[dict[str, Any]]:
        if self.backend_type == "postgres":
            return self._run_sync(self.list_sync_branches_async, task_id)
        with self._lock:
            if task_id is None:
                rows = self.conn.execute(
                    "SELECT b.*, COUNT(e.event_id) AS event_count FROM sync_ledger_branches b "
                    "LEFT JOIN sync_ledger_branch_events e ON e.branch_id = b.branch_id "
                    "GROUP BY b.branch_id ORDER BY b.created_at, b.branch_id"
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT b.*, COUNT(e.event_id) AS event_count FROM sync_ledger_branches b "
                    "LEFT JOIN sync_ledger_branch_events e ON e.branch_id = b.branch_id "
                    "WHERE b.task_id = ? GROUP BY b.branch_id ORDER BY b.created_at, b.branch_id",
                    (task_id,),
                ).fetchall()
            return [dict(row) for row in rows]

    async def list_sync_branches_async(
        self: LedgerStore, task_id: str | None = None
    ) -> list[dict[str, Any]]:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.list_sync_branches, task_id)
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            if task_id is None:
                rows = await conn.fetch(
                    "SELECT b.*, COUNT(e.event_id) AS event_count FROM sync_ledger_branches b "
                    "LEFT JOIN sync_ledger_branch_events e ON e.branch_id = b.branch_id "
                    "GROUP BY b.branch_id ORDER BY b.created_at, b.branch_id"
                )
            else:
                rows = await conn.fetch(
                    "SELECT b.*, COUNT(e.event_id) AS event_count FROM sync_ledger_branches b "
                    "LEFT JOIN sync_ledger_branch_events e ON e.branch_id = b.branch_id "
                    "WHERE b.task_id = $1 GROUP BY b.branch_id ORDER BY b.created_at, b.branch_id",
                    task_id,
                )
            return [dict(row) for row in rows]

    def verify_sync_branch_integrity(
        self: LedgerStore, task_id: str | None = None
    ) -> dict[str, Any]:
        if self.backend_type == "postgres":
            return self._run_sync(self.verify_sync_branch_integrity_async, task_id)
        from harness_core.storage.db import compute_event_hash

        with self._lock:
            if task_id is None:
                rows = self.conn.execute(
                    "SELECT * FROM sync_ledger_branch_events ORDER BY task_id, branch_id, sequence_no"
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM sync_ledger_branch_events WHERE task_id = ? "
                    "ORDER BY branch_id, sequence_no", (task_id,),
                ).fetchall()
        errors: list[str] = []
        current_branch: str | None = None
        previous_hash = "0" * 64
        expected_sequence = 1
        for row in rows:
            branch = str(row["branch_id"])
            if branch != current_branch:
                current_branch = branch
                previous_hash = "0" * 64
                expected_sequence = 1
            sequence = int(row["sequence_no"])
            if sequence != expected_sequence:
                errors.append(f"Branch {branch}: sequence discontinuity.")
            computed = compute_event_hash(
                prev_hash=previous_hash, task_id=str(row["task_id"]), seq_no=sequence,
                event_type=str(row["event_type"]), prior_state=row["prior_state"],
                next_state=row["next_state"], reason_code=str(row["reason_code"]),
                workspace_tree_sha=row["workspace_tree_sha"], payload_str=str(row["payload_json"]),
                occurred_at=str(row["occurred_at"]),
            )
            if computed != row["event_sha256"]:
                errors.append(f"Branch {branch} event seq {sequence}: hash mismatch.")
            previous_hash = str(row["event_sha256"])
            expected_sequence = sequence + 1
        return {
            "valid": not errors, "verified_events": len(rows),
            "branches_checked": len({str(row["branch_id"]) for row in rows}),
            "errors": errors,
        }

    async def verify_sync_branch_integrity_async(
        self: LedgerStore, task_id: str | None = None
    ) -> dict[str, Any]:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.verify_sync_branch_integrity, task_id)
        from harness_core.storage.db import compute_event_hash

        pool = await self.get_pool()
        async with pool.acquire() as conn:
            if task_id is None:
                rows = await conn.fetch(
                    "SELECT * FROM sync_ledger_branch_events ORDER BY task_id, branch_id, sequence_no"
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM sync_ledger_branch_events WHERE task_id = $1 "
                    "ORDER BY branch_id, sequence_no", task_id,
                )
        errors: list[str] = []
        current_branch: str | None = None
        previous_hash = "0" * 64
        expected_sequence = 1
        for row in rows:
            branch = str(row["branch_id"])
            if branch != current_branch:
                current_branch = branch
                previous_hash = "0" * 64
                expected_sequence = 1
            sequence = int(row["sequence_no"])
            if sequence != expected_sequence:
                errors.append(f"Branch {branch}: sequence discontinuity.")
            computed = compute_event_hash(
                prev_hash=previous_hash, task_id=str(row["task_id"]), seq_no=sequence,
                event_type=str(row["event_type"]), prior_state=row["prior_state"],
                next_state=row["next_state"], reason_code=str(row["reason_code"]),
                workspace_tree_sha=row["workspace_tree_sha"], payload_str=str(row["payload_json"]),
                occurred_at=str(row["occurred_at"]),
            )
            if computed != row["event_sha256"]:
                errors.append(f"Branch {branch} event seq {sequence}: hash mismatch.")
            previous_hash = str(row["event_sha256"])
            expected_sequence = sequence + 1
        return {
            "valid": not errors, "verified_events": len(rows),
            "branches_checked": len({str(row["branch_id"]) for row in rows}),
            "errors": errors,
        }

    def _insert_synced_task_sqlite(
        self: LedgerStore, task_id: str, snapshot: dict[str, Any], sender: str
    ) -> None:
        self.conn.execute(
            "INSERT INTO tasks (task_id, goal, execution_mode, status, priority, budget_json, "
            "constraints_json, deliverable, policy_snapshot_id, active_workspace_tree_sha, "
            "version, pinned, title_provenance, created_at, updated_at, terminal_reason) "
            "VALUES (?, ?, 'standard', ?, ?, ?, ?, ?, ?, ?, 1, 0, 'imported', ?, ?, ?)",
            (task_id, *_synced_task_values(snapshot, sender)),
        )

    async def _insert_synced_task_postgres(
        self: LedgerStore, conn: Any, task_id: str, snapshot: dict[str, Any], sender: str
    ) -> None:
        await conn.execute(
            "INSERT INTO tasks (task_id, goal, execution_mode, status, priority, budget_json, "
            "constraints_json, deliverable, policy_snapshot_id, active_workspace_tree_sha, "
            "version, pinned, title_provenance, created_at, updated_at, terminal_reason) "
            "VALUES ($1, $2, 'standard', $3, $4, $5, $6, $7, $8, $9, 1, 0, 'imported', $10, $11, $12)",
            (task_id, *_synced_task_values(snapshot, sender)),
        )

    def _insert_sync_event_sqlite(
        self: LedgerStore, table: str, event: dict[str, Any], branch_id: str | None = None
    ) -> None:
        if table not in {"task_events", "sync_ledger_branch_events"}:
            raise ValueError("Invalid sync event table.")
        columns = _EVENT_FIELDS
        values = [event[field] for field in columns]
        placeholders = ",".join("?" for _ in columns)
        if branch_id is not None:
            columns = ("branch_id", *columns)
            values.insert(0, branch_id)
            placeholders = f"?,{placeholders}"
        self.conn.execute(
            f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})", values
        )

    async def _insert_sync_event_postgres(
        self: LedgerStore, conn: Any, table: str, event: dict[str, Any], branch_id: str | None = None
    ) -> None:
        if table not in {"task_events", "sync_ledger_branch_events"}:
            raise ValueError("Invalid sync event table.")
        columns = _EVENT_FIELDS
        values = [event[field] for field in columns]
        placeholders = ",".join(f"${index}" for index in range(1, len(columns) + 1))
        if branch_id is not None:
            columns = ("branch_id", *columns)
            values.insert(0, branch_id)
            placeholders = f"$1,{','.join(f'${index}' for index in range(2, len(columns) + 1))}"
        await conn.execute(
            f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})", *values
        )

    def _merge_dialogue_turns_sqlite(
        self: LedgerStore, task_id: str, turns: list[dict[str, Any]]
    ) -> int:
        imported = 0
        for turn in turns:
            existing = self.conn.execute(
                "SELECT * FROM dialogue_turns WHERE turn_id = ?", (turn["turn_id"],)
            ).fetchone()
            if existing is None:
                self.conn.execute(
                    "INSERT INTO dialogue_turns (turn_id, task_id, turn_index, role, content, "
                    "is_active, branch_id, metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    tuple(turn[field] for field in (
                        "turn_id", "task_id", "turn_index", "role", "content", "is_active",
                        "branch_id", "metadata_json", "created_at",
                    )),
                )
                imported += 1
                continue
            if existing["task_id"] != task_id or any(
                existing[field] != turn[field]
                for field in ("turn_index", "role", "content", "created_at")
            ):
                raise ValueError(f"Dialogue turn id collision: {turn['turn_id']}")
            self.conn.execute(
                "UPDATE dialogue_turns SET is_active = ?, branch_id = ?, metadata_json = ? "
                "WHERE turn_id = ?",
                (turn["is_active"], turn["branch_id"], turn["metadata_json"], turn["turn_id"]),
            )
        return imported

    async def _merge_dialogue_turns_postgres(
        self: LedgerStore, conn: Any, task_id: str, turns: list[dict[str, Any]]
    ) -> int:
        imported = 0
        for turn in turns:
            existing = await conn.fetchrow(
                "SELECT * FROM dialogue_turns WHERE turn_id = $1", turn["turn_id"]
            )
            if existing is None:
                await conn.execute(
                    "INSERT INTO dialogue_turns (turn_id, task_id, turn_index, role, content, "
                    "is_active, branch_id, metadata_json, created_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
                    tuple(turn[field] for field in (
                        "turn_id", "task_id", "turn_index", "role", "content", "is_active",
                        "branch_id", "metadata_json", "created_at",
                    )),
                )
                imported += 1
                continue
            if existing["task_id"] != task_id or any(
                existing[field] != turn[field]
                for field in ("turn_index", "role", "content", "created_at")
            ):
                raise ValueError(f"Dialogue turn id collision: {turn['turn_id']}")
            await conn.execute(
                "UPDATE dialogue_turns SET is_active = $1, branch_id = $2, metadata_json = $3 "
                "WHERE turn_id = $4",
                (turn["is_active"], turn["branch_id"], turn["metadata_json"], turn["turn_id"]),
            )
        return imported
