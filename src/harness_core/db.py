"""Authoritative Task Ledger, Append-Only Event Stream, Lease/Fencing Manager, and Call Journal.

Implements PRD Sections 6.1, 6.2, 9, and 10 over SQLite WAL (default zero-config local profile)
or PostgreSQL 16+ connection strings, maintaining cryptographic hash chains on task_events.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import TaskBudget, TaskState, TaskSubmitRequest, VALID_TRANSITIONS, utcnow_iso


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def sha256_hex(text: str | bytes) -> str:
    if isinstance(text, str):
        text = text.encode("utf-8")
    return hashlib.sha256(text).hexdigest()


def compute_event_hash(
    *,
    prev_hash: str,
    task_id: str,
    seq_no: int,
    event_type: str,
    prior_state: str | None,
    next_state: str | None,
    reason_code: str,
    workspace_tree_sha: str | None,
    payload_str: str,
    occurred_at: str,
) -> str:
    chain_material = (
        f"{prev_hash}|{task_id}|{seq_no}|{event_type}|{prior_state}|{next_state}|"
        f"{reason_code}|{workspace_tree_sha}|{payload_str}|{occurred_at}"
    )
    return sha256_hex(chain_material)


def normalize_query(sql: str, backend_type: str = "sqlite") -> str:
    """Translates parameter placeholders: converts '?' to '$1, $2, ...' for postgres,
    keeping '?' unchanged for sqlite. Ignores '?' inside single-quoted string literals."""
    if backend_type != "postgres":
        return sql
    out: list[str] = []
    param_idx = 1
    in_string = False
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            if in_string and i + 1 < n and sql[i + 1] == "'":
                out.append("''")
                i += 2
                continue
            in_string = not in_string
            out.append(ch)
            i += 1
        elif ch == "?" and not in_string:
            out.append(f"${param_idx}")
            param_idx += 1
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def normalize_param(val: Any) -> Any:
    """Normalizes query parameter for persistence across SQLite and PostgreSQL."""
    if isinstance(val, (dict, list)):
        return canonical_json(val)
    if hasattr(val, "value") and not isinstance(val, (int, float, str, bytes, bool)):
        return val.value
    return val


class ConcurrencyFenceError(RuntimeError):
    """Raised when a stale worker fencing token or version mismatch attempts a commit."""


class InvalidTransitionError(ValueError):
    """Raised when a state transition violates the PRD Section 9 state machine."""


class _SyncRunner:
    """Dedicated background event-loop runner for synchronous execution of asyncpg operations."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True, name="ledger-sync-bridge")
        self._thread.start()

    def run(self, coro: Any) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    def close(self) -> None:
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=2.0)


class LedgerStore:
    """Thread-safe transactional Task Ledger & Call Journal."""

    def __init__(
        self,
        db_path: Path | str,
        schema_sql_path: Path | str | None = None,
        min_size: int = 2,
        max_size: int = 20,
        command_timeout: float = 30.0,
    ):
        db_str = str(db_path).strip()
        self.min_size = min_size
        self.max_size = max_size
        self.command_timeout = command_timeout
        self.schema_sql_path = schema_sql_path
        self._lock = threading.RLock()
        self._sync_runner: _SyncRunner | None = None
        self._pool: Any = None
        self._pool_loop: Any = None

        if (
            db_str.startswith("postgresql://")
            or db_str.startswith("postgres://")
            or db_str.startswith("postgresql+asyncpg://")
        ):
            self.backend_type = "postgres"
            self.db_url = db_str
            if db_str.startswith("postgresql+asyncpg://"):
                self.dsn = "postgresql://" + db_str[len("postgresql+asyncpg://"):]
            elif db_str.startswith("postgres://"):
                self.dsn = "postgresql://" + db_str[len("postgres://"):]
            else:
                self.dsn = db_str
            self.db_path = None
            try:
                import asyncpg  # type: ignore[import-not-found]
            except ImportError as exc:
                raise ImportError(
                    "PostgreSQL support requires 'asyncpg'. Install with: pip install .[postgres]"
                ) from exc
            self.conn = None
        else:
            self.backend_type = "sqlite"
            self.db_url = db_str
            self.dsn = db_str
            if db_str.startswith("sqlite:///"):
                clean_path = db_str[len("sqlite:///"):]
                self.db_path = Path(clean_path)
            elif db_str == ":memory:":
                self.db_path = Path(":memory:")
            else:
                self.db_path = Path(db_path)
            if str(self.db_path) != ":memory:" and self.db_path.parent:
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=command_timeout)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode=WAL;")
            self.conn.execute("PRAGMA foreign_keys=ON;")
            self._init_schema(schema_sql_path)

    def normalize_query(self, sql: str) -> str:
        return normalize_query(sql, self.backend_type)

    def normalize_params(self, params: tuple[Any, ...] | list[Any]) -> tuple[Any, ...]:
        return tuple(normalize_param(p) for p in params)

    async def get_pool(self) -> Any:
        """Returns or lazily initializes the asyncpg connection pool."""
        if self.backend_type != "postgres":
            return None
        import asyncpg

        loop = asyncio.get_running_loop()
        if self._pool is None or self._pool_loop != loop:
            self._pool = await asyncpg.create_pool(
                self.dsn,
                min_size=self.min_size,
                max_size=self.max_size,
                command_timeout=self.command_timeout,
            )
            self._pool_loop = loop
        return self._pool

    async def init_pool(self) -> Any:
        """Explicitly initializes the asyncpg connection pool."""
        return await self.get_pool()

    async def close_async(self) -> None:
        """Asynchronously closes the connection pool if open."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            self._pool_loop = None

    def _get_sync_runner(self) -> _SyncRunner:
        with self._lock:
            if self._sync_runner is None:
                self._sync_runner = _SyncRunner()
            return self._sync_runner

    def _run_sync(self, coro_func: Any, *args: Any, **kwargs: Any) -> Any:
        runner = self._get_sync_runner()
        return runner.run(coro_func(*args, **kwargs))

    def close(self) -> None:
        """Synchronously closes database connections / pools."""
        with self._lock:
            if self._sync_runner is not None:
                self._sync_runner.close()
                self._sync_runner = None
            if self.backend_type == "postgres":
                if self._pool is not None:
                    try:
                        loop = asyncio.get_running_loop()
                        if loop.is_running() and self._pool_loop == loop:
                            loop.create_task(self._pool.close())
                    except RuntimeError:
                        asyncio.run(self.close_async())
            elif self.backend_type == "sqlite":
                if self.conn is not None:
                    self.conn.close()
                    self.conn = None

    def _init_schema(self, schema_sql_path: Path | str | None) -> None:
        if schema_sql_path is None:
            candidate = Path(__file__).resolve().parents[2] / "migrations" / "0001_initial_schema.sql"
            if candidate.is_file():
                schema_sql_path = candidate
        if schema_sql_path and Path(schema_sql_path).is_file():
            sql = Path(schema_sql_path).read_text(encoding="utf-8")
            with self._lock:
                if self.conn is not None:
                    self.conn.executescript(sql)
                    self.conn.commit()

    def _row_to_dict(self, row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        d = dict(row)
        for k in list(d.keys()):
            if k.endswith("_json"):
                base_key = k[:-5]
                val = d[k]
                if isinstance(val, str):
                    try:
                        d[base_key] = json.loads(val)
                    except json.JSONDecodeError:
                        d[base_key] = val
                elif isinstance(val, (dict, list)):
                    d[base_key] = val
        return d

    # ------------------------------------------------------------------
    # Task Intake & State Machine (FR-001..FR-006, FR-010..FR-013)
    # ------------------------------------------------------------------

    def submit_task(self, req: TaskSubmitRequest, policy_snapshot_id: str = "pol-v1.3") -> dict[str, Any]:
        if self.backend_type == "postgres":
            return self._run_sync(self.submit_task_async, req, policy_snapshot_id)
        with self._lock:
            if req.idempotency_key:
                existing = self.conn.execute(
                    "SELECT * FROM tasks WHERE idempotency_key = ?", (req.idempotency_key,)
                ).fetchone()
                if existing:
                    return self._row_to_dict(existing)  # type: ignore[return-value]

            task_id = f"tsk_{uuid.uuid4().hex[:12]}"
            now = utcnow_iso()
            budget = TaskBudget.from_dict(req.budget).to_dict()
            constraints = dict(req.constraints or {})
            if req.workspace_root:
                constraints["workspace_root"] = req.workspace_root
            if req.steps:
                constraints["requested_steps"] = req.steps
            if req.skill_id:
                constraints["skill_id"] = req.skill_id
            if req.approval_policy:
                constraints["approval_policy"] = req.approval_policy

            mode = req.execution_mode
            if mode == "auto":
                mode = "fast_path" if self._is_fast_path_eligible(req) else "standard"

            self.conn.execute(
                """
                INSERT INTO tasks (
                    task_id, parent_task_id, project_id, goal, execution_mode,
                    status, priority, deadline_at, budget_json, constraints_json,
                    deliverable, policy_snapshot_id, idempotency_key,
                    active_workspace_tree_sha, version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    task_id,
                    req.parent_task_id,
                    req.project_id or "default",
                    req.goal,
                    mode,
                    TaskState.SUBMITTED.value,
                    req.priority,
                    req.deadline_at,
                    canonical_json(budget),
                    canonical_json(constraints),
                    req.deliverable,
                    policy_snapshot_id,
                    req.idempotency_key,
                    None,
                    now,
                    now,
                ),
            )
            self._append_event_locked(
                task_id=task_id,
                attempt_id=None,
                event_type="TASK_SUBMITTED",
                prior_state=None,
                next_state=TaskState.SUBMITTED.value,
                reason_code="INTAKE_ACCEPTED",
                workspace_tree_sha=None,
                policy_version=policy_snapshot_id,
                payload={"goal": req.goal, "execution_mode": mode, "project_id": req.project_id},
                actor="control_api",
            )
            self.conn.commit()
            return self.get_task(task_id)  # type: ignore[return-value]

    async def submit_task_async(self, req: TaskSubmitRequest, policy_snapshot_id: str = "pol-v1.3") -> dict[str, Any]:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.submit_task, req, policy_snapshot_id)
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                if req.idempotency_key:
                    existing = await conn.fetchrow(
                        "SELECT * FROM tasks WHERE idempotency_key = $1", req.idempotency_key
                    )
                    if existing:
                        return self._row_to_dict(existing)  # type: ignore[return-value]

                task_id = f"tsk_{uuid.uuid4().hex[:12]}"
                now = utcnow_iso()
                budget = TaskBudget.from_dict(req.budget).to_dict()
                constraints = dict(req.constraints or {})
                if req.workspace_root:
                    constraints["workspace_root"] = req.workspace_root
                if req.steps:
                    constraints["requested_steps"] = req.steps
                if req.skill_id:
                    constraints["skill_id"] = req.skill_id
                if req.approval_policy:
                    constraints["approval_policy"] = req.approval_policy

                mode = req.execution_mode
                if mode == "auto":
                    mode = "fast_path" if self._is_fast_path_eligible(req) else "standard"

                await conn.execute(
                    """
                    INSERT INTO tasks (
                        task_id, parent_task_id, project_id, goal, execution_mode,
                        status, priority, deadline_at, budget_json, constraints_json,
                        deliverable, policy_snapshot_id, idempotency_key,
                        active_workspace_tree_sha, version, created_at, updated_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, 1, $15, $16)
                    """,
                    task_id,
                    req.parent_task_id,
                    req.project_id or "default",
                    req.goal,
                    mode,
                    TaskState.SUBMITTED.value,
                    req.priority,
                    req.deadline_at,
                    canonical_json(budget),
                    canonical_json(constraints),
                    req.deliverable,
                    policy_snapshot_id,
                    req.idempotency_key,
                    None,
                    now,
                    now,
                )
                await self._append_event_async_conn(
                    conn=conn,
                    task_id=task_id,
                    attempt_id=None,
                    event_type="TASK_SUBMITTED",
                    prior_state=None,
                    next_state=TaskState.SUBMITTED.value,
                    reason_code="INTAKE_ACCEPTED",
                    workspace_tree_sha=None,
                    policy_version=policy_snapshot_id,
                    payload={"goal": req.goal, "execution_mode": mode, "project_id": req.project_id},
                    actor="control_api",
                )
                row = await conn.fetchrow("SELECT * FROM tasks WHERE task_id = $1", task_id)
                return self._row_to_dict(row)  # type: ignore[return-value]

    def _is_fast_path_eligible(self, req: TaskSubmitRequest) -> bool:
        """FR-001a: Read-only (R0/R1) tasks with <3 estimated steps qualify for Fast-Path."""
        if req.steps:
            if len(req.steps) >= 3:
                return False
            return all(s.get("risk_class", "R1") in ("R0", "R1") for s in req.steps)
        lowered = req.goal.lower()
        write_keywords = ("fix", "edit", "refactor", "write", "modify", "patch", "delete", "deploy")
        if any(w in lowered for w in write_keywords):
            return False
        return True

    def transition_task(
        self,
        task_id: str,
        next_state: TaskState | str,
        *,
        reason_code: str,
        actor: str = "orchestrator",
        attempt_id: str | None = None,
        workspace_tree_sha: str | None = None,
        fencing_token: int | None = None,
        expected_version: int | None = None,
        payload: dict[str, Any] | None = None,
        terminal_reason: str | None = None,
    ) -> dict[str, Any]:
        """Authoritative CAS state transition with fencing check and append-only event record."""
        if self.backend_type == "postgres":
            return self._run_sync(
                self.transition_task_async,
                task_id,
                next_state,
                reason_code=reason_code,
                actor=actor,
                attempt_id=attempt_id,
                workspace_tree_sha=workspace_tree_sha,
                fencing_token=fencing_token,
                expected_version=expected_version,
                payload=payload,
                terminal_reason=terminal_reason,
            )
        target = TaskState(next_state) if isinstance(next_state, str) else next_state
        with self._lock:
            row = self.conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not row:
                raise KeyError(f"Task not found: {task_id}")
            current = TaskState(row["status"])
            cur_version = int(row["version"])

            if expected_version is not None and cur_version != expected_version:
                raise ConcurrencyFenceError(
                    f"CAS version mismatch on {task_id}: expected {expected_version}, got {cur_version}"
                )

            if fencing_token is not None:
                max_tok_row = self.conn.execute(
                    "SELECT COALESCE(MAX(fencing_token), 0) AS max_tok FROM leases WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                max_tok = int(max_tok_row["max_tok"]) if max_tok_row else 0
                if fencing_token < max_tok:
                    raise ConcurrencyFenceError(
                        f"Stale fencing token {fencing_token} < active token {max_tok} for task {task_id}"
                    )

            if target != current and target not in VALID_TRANSITIONS.get(current, set()):
                raise InvalidTransitionError(
                    f"Illegal state transition for {task_id}: {current.value} -> {target.value}"
                )

            now = utcnow_iso()
            new_tree_sha = workspace_tree_sha or row["active_workspace_tree_sha"]
            term_reason = terminal_reason if target.is_terminal else row["terminal_reason"]

            cursor = self.conn.execute(
                """
                UPDATE tasks
                SET status = ?, version = version + 1, active_workspace_tree_sha = ?,
                    terminal_reason = ?, updated_at = ?
                WHERE task_id = ? AND version = ?
                """,
                (target.value, new_tree_sha, term_reason, now, task_id, cur_version),
            )
            if cursor.rowcount != 1:
                raise ConcurrencyFenceError(f"Concurrent update race detected on task {task_id}")

            self._append_event_locked(
                task_id=task_id,
                attempt_id=attempt_id,
                event_type=f"STATE_{target.value}",
                prior_state=current.value,
                next_state=target.value,
                reason_code=reason_code,
                workspace_tree_sha=new_tree_sha,
                policy_version=row["policy_snapshot_id"],
                payload=payload or {},
                actor=actor,
            )
            self.conn.commit()
            return self.get_task(task_id)  # type: ignore[return-value]

    async def transition_task_async(
        self,
        task_id: str,
        next_state: TaskState | str,
        *,
        reason_code: str,
        actor: str = "orchestrator",
        attempt_id: str | None = None,
        workspace_tree_sha: str | None = None,
        fencing_token: int | None = None,
        expected_version: int | None = None,
        payload: dict[str, Any] | None = None,
        terminal_reason: str | None = None,
    ) -> dict[str, Any]:
        """Authoritative CAS state transition with fencing check and append-only event record."""
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(
                self.transition_task,
                task_id,
                next_state,
                reason_code=reason_code,
                actor=actor,
                attempt_id=attempt_id,
                workspace_tree_sha=workspace_tree_sha,
                fencing_token=fencing_token,
                expected_version=expected_version,
                payload=payload,
                terminal_reason=terminal_reason,
            )
        target = TaskState(next_state) if isinstance(next_state, str) else next_state
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("SELECT * FROM tasks WHERE task_id = $1 FOR UPDATE", task_id)
                if not row:
                    raise KeyError(f"Task not found: {task_id}")
                current = TaskState(row["status"])
                cur_version = int(row["version"])

                if expected_version is not None and cur_version != expected_version:
                    raise ConcurrencyFenceError(
                        f"CAS version mismatch on {task_id}: expected {expected_version}, got {cur_version}"
                    )

                if fencing_token is not None:
                    max_tok_row = await conn.fetchrow(
                        "SELECT COALESCE(MAX(fencing_token), 0) AS max_tok FROM leases WHERE task_id = $1",
                        task_id,
                    )
                    max_tok = int(max_tok_row["max_tok"]) if max_tok_row else 0
                    if fencing_token < max_tok:
                        raise ConcurrencyFenceError(
                            f"Stale fencing token {fencing_token} < active token {max_tok} for task {task_id}"
                        )

                if target != current and target not in VALID_TRANSITIONS.get(current, set()):
                    raise InvalidTransitionError(
                        f"Illegal state transition for {task_id}: {current.value} -> {target.value}"
                    )

                now = utcnow_iso()
                new_tree_sha = workspace_tree_sha or row["active_workspace_tree_sha"]
                term_reason = terminal_reason if target.is_terminal else row["terminal_reason"]

                res = await conn.execute(
                    """
                    UPDATE tasks
                    SET status = $1, version = version + 1, active_workspace_tree_sha = $2,
                        terminal_reason = $3, updated_at = $4
                    WHERE task_id = $5 AND version = $6
                    """,
                    target.value,
                    new_tree_sha,
                    term_reason,
                    now,
                    task_id,
                    cur_version,
                )
                try:
                    rowcount = int(res.split()[-1])
                except (ValueError, IndexError):
                    rowcount = 0
                if rowcount != 1:
                    raise ConcurrencyFenceError(f"Concurrent update race detected on task {task_id}")

                await self._append_event_async_conn(
                    conn=conn,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    event_type=f"STATE_{target.value}",
                    prior_state=current.value,
                    next_state=target.value,
                    reason_code=reason_code,
                    workspace_tree_sha=new_tree_sha,
                    policy_version=row["policy_snapshot_id"],
                    payload=payload or {},
                    actor=actor,
                )
                updated = await conn.fetchrow("SELECT * FROM tasks WHERE task_id = $1", task_id)
                return self._row_to_dict(updated)  # type: ignore[return-value]

    def record_event(
        self,
        task_id: str,
        event_type: str,
        *,
        reason_code: str,
        actor: str = "orchestrator",
        attempt_id: str | None = None,
        workspace_tree_sha: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.backend_type == "postgres":
            return self._run_sync(
                self.record_event_async,
                task_id,
                event_type,
                reason_code=reason_code,
                actor=actor,
                attempt_id=attempt_id,
                workspace_tree_sha=workspace_tree_sha,
                payload=payload,
            )
        with self._lock:
            row = self.conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not row:
                raise KeyError(f"Task not found: {task_id}")
            ev = self._append_event_locked(
                task_id=task_id,
                attempt_id=attempt_id,
                event_type=event_type,
                prior_state=row["status"],
                next_state=row["status"],
                reason_code=reason_code,
                workspace_tree_sha=workspace_tree_sha or row["active_workspace_tree_sha"],
                policy_version=row["policy_snapshot_id"],
                payload=payload or {},
                actor=actor,
            )
            self.conn.commit()
            return ev

    async def record_event_async(
        self,
        task_id: str,
        event_type: str,
        *,
        reason_code: str,
        actor: str = "orchestrator",
        attempt_id: str | None = None,
        workspace_tree_sha: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(
                self.record_event,
                task_id,
                event_type,
                reason_code=reason_code,
                actor=actor,
                attempt_id=attempt_id,
                workspace_tree_sha=workspace_tree_sha,
                payload=payload,
            )
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("SELECT * FROM tasks WHERE task_id = $1", task_id)
                if not row:
                    raise KeyError(f"Task not found: {task_id}")
                ev = await self._append_event_async_conn(
                    conn=conn,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    event_type=event_type,
                    prior_state=row["status"],
                    next_state=row["status"],
                    reason_code=reason_code,
                    workspace_tree_sha=workspace_tree_sha or row["active_workspace_tree_sha"],
                    policy_version=row["policy_snapshot_id"],
                    payload=payload or {},
                    actor=actor,
                )
                return ev

    def _append_event_locked(
        self,
        *,
        task_id: str,
        attempt_id: str | None,
        event_type: str,
        prior_state: str | None,
        next_state: str | None,
        reason_code: str,
        workspace_tree_sha: str | None,
        policy_version: str,
        payload: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        last = self.conn.execute(
            "SELECT sequence_no, event_sha256 FROM task_events WHERE task_id = ? ORDER BY sequence_no DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        seq_no = (int(last["sequence_no"]) + 1) if last else 1
        prev_hash = last["event_sha256"] if last else ("0" * 64)
        event_id = f"evt_{uuid.uuid4().hex[:12]}"
        correlation_id = f"cor_{uuid.uuid4().hex[:10]}"
        now = utcnow_iso()
        payload_str = canonical_json(payload)

        event_sha = compute_event_hash(
            prev_hash=prev_hash,
            task_id=task_id,
            seq_no=seq_no,
            event_type=event_type,
            prior_state=prior_state,
            next_state=next_state,
            reason_code=reason_code,
            workspace_tree_sha=workspace_tree_sha,
            payload_str=payload_str,
            occurred_at=now,
        )

        self.conn.execute(
            """
            INSERT INTO task_events (
                event_id, task_id, attempt_id, sequence_no, event_type,
                prior_state, next_state, reason_code, workspace_tree_sha,
                policy_version, payload_json, actor, occurred_at,
                correlation_id, event_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                task_id,
                attempt_id,
                seq_no,
                event_type,
                prior_state,
                next_state,
                reason_code,
                workspace_tree_sha,
                policy_version,
                payload_str,
                actor,
                now,
                correlation_id,
                event_sha,
            ),
        )
        return {
            "event_id": event_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "sequence_no": seq_no,
            "event_type": event_type,
            "prior_state": prior_state,
            "next_state": next_state,
            "reason_code": reason_code,
            "workspace_tree_sha": workspace_tree_sha,
            "payload": payload,
            "actor": actor,
            "occurred_at": now,
            "correlation_id": correlation_id,
            "event_sha256": event_sha,
        }

    async def _append_event_async_conn(
        self,
        *,
        conn: Any,
        task_id: str,
        attempt_id: str | None,
        event_type: str,
        prior_state: str | None,
        next_state: str | None,
        reason_code: str,
        workspace_tree_sha: str | None,
        policy_version: str,
        payload: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        last = await conn.fetchrow(
            "SELECT sequence_no, event_sha256 FROM task_events WHERE task_id = $1 ORDER BY sequence_no DESC LIMIT 1",
            task_id,
        )
        seq_no = (int(last["sequence_no"]) + 1) if last else 1
        prev_hash = str(last["event_sha256"]) if last else ("0" * 64)
        event_id = f"evt_{uuid.uuid4().hex[:12]}"
        correlation_id = f"cor_{uuid.uuid4().hex[:10]}"
        now = utcnow_iso()
        payload_str = canonical_json(payload)

        event_sha = compute_event_hash(
            prev_hash=prev_hash,
            task_id=task_id,
            seq_no=seq_no,
            event_type=event_type,
            prior_state=prior_state,
            next_state=next_state,
            reason_code=reason_code,
            workspace_tree_sha=workspace_tree_sha,
            payload_str=payload_str,
            occurred_at=now,
        )

        await conn.execute(
            """
            INSERT INTO task_events (
                event_id, task_id, attempt_id, sequence_no, event_type,
                prior_state, next_state, reason_code, workspace_tree_sha,
                policy_version, payload_json, actor, occurred_at,
                correlation_id, event_sha256
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
            """,
            event_id,
            task_id,
            attempt_id,
            seq_no,
            event_type,
            prior_state,
            next_state,
            reason_code,
            workspace_tree_sha,
            policy_version,
            payload_str,
            actor,
            now,
            correlation_id,
            event_sha,
        )
        return {
            "event_id": event_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "sequence_no": seq_no,
            "event_type": event_type,
            "prior_state": prior_state,
            "next_state": next_state,
            "reason_code": reason_code,
            "workspace_tree_sha": workspace_tree_sha,
            "payload": payload,
            "actor": actor,
            "occurred_at": now,
            "correlation_id": correlation_id,
            "event_sha256": event_sha,
        }

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        if self.backend_type == "postgres":
            return self._run_sync(self.get_task_async, task_id)
        with self._lock:
            row = self.conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            return self._row_to_dict(row)

    async def get_task_async(self, task_id: str) -> dict[str, Any] | None:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.get_task, task_id)
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM tasks WHERE task_id = $1", task_id)
            return self._row_to_dict(row)

    def list_tasks(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if self.backend_type == "postgres":
            return self._run_sync(self.list_tasks_async, status, limit)
        with self._lock:
            if status:
                rows = self.conn.execute(
                    "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
            return [self._row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]

    async def list_tasks_async(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.list_tasks, status, limit)
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            if status:
                rows = await conn.fetch(
                    "SELECT * FROM tasks WHERE status = $1 ORDER BY created_at DESC LIMIT $2",
                    status,
                    limit,
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM tasks ORDER BY created_at DESC LIMIT $1", limit
                )
            return [self._row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]

    def list_events(self, task_id: str) -> list[dict[str, Any]]:
        if self.backend_type == "postgres":
            return self._run_sync(self.list_events_async, task_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM task_events WHERE task_id = ? ORDER BY sequence_no ASC",
                (task_id,),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]

    async def list_events_async(self, task_id: str) -> list[dict[str, Any]]:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.list_events, task_id)
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM task_events WHERE task_id = $1 ORDER BY sequence_no ASC",
                task_id,
            )
            return [self._row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Workers, Leases, Attempts, and Fencing Tokens (FR-011, FR-050..FR-052)
    # ------------------------------------------------------------------

    def register_worker(
        self,
        worker_id: str,
        machine_class: str = "m4_air",
        cow_backend: str = "apfs_clone",
        capabilities: dict[str, Any] | None = None,
        local_llm_models: list[str] | None = None,
        trust_level: str = "trusted_local",
    ) -> dict[str, Any]:
        if self.backend_type == "postgres":
            return self._run_sync(
                self.register_worker_async,
                worker_id,
                machine_class=machine_class,
                cow_backend=cow_backend,
                capabilities=capabilities,
                local_llm_models=local_llm_models,
                trust_level=trust_level,
            )
        with self._lock:
            now = utcnow_iso()
            self.conn.execute(
                """
                INSERT INTO workers (
                    worker_id, machine_class, capabilities_json, cow_backend,
                    local_llm_models_json, trust_level, health, last_heartbeat_at, registered_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'HEALTHY', ?, ?)
                ON CONFLICT(worker_id) DO UPDATE SET
                    machine_class=excluded.machine_class,
                    capabilities_json=excluded.capabilities_json,
                    cow_backend=excluded.cow_backend,
                    local_llm_models_json=excluded.local_llm_models_json,
                    health='HEALTHY',
                    last_heartbeat_at=excluded.last_heartbeat_at
                """,
                (
                    worker_id,
                    machine_class,
                    canonical_json(capabilities or {}),
                    cow_backend,
                    canonical_json(local_llm_models or []),
                    trust_level,
                    now,
                    now,
                ),
            )
            self.conn.commit()
            row = self.conn.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()
            return self._row_to_dict(row)  # type: ignore[return-value]

    async def register_worker_async(
        self,
        worker_id: str,
        machine_class: str = "m4_air",
        cow_backend: str = "apfs_clone",
        capabilities: dict[str, Any] | None = None,
        local_llm_models: list[str] | None = None,
        trust_level: str = "trusted_local",
    ) -> dict[str, Any]:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(
                self.register_worker,
                worker_id,
                machine_class=machine_class,
                cow_backend=cow_backend,
                capabilities=capabilities,
                local_llm_models=local_llm_models,
                trust_level=trust_level,
            )
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            now = utcnow_iso()
            await conn.execute(
                """
                INSERT INTO workers (
                    worker_id, machine_class, capabilities_json, cow_backend,
                    local_llm_models_json, trust_level, health, last_heartbeat_at, registered_at
                ) VALUES ($1, $2, $3, $4, $5, $6, 'HEALTHY', $7, $8)
                ON CONFLICT(worker_id) DO UPDATE SET
                    machine_class=excluded.machine_class,
                    capabilities_json=excluded.capabilities_json,
                    cow_backend=excluded.cow_backend,
                    local_llm_models_json=excluded.local_llm_models_json,
                    health='HEALTHY',
                    last_heartbeat_at=excluded.last_heartbeat_at
                """,
                worker_id,
                machine_class,
                canonical_json(capabilities or {}),
                cow_backend,
                canonical_json(local_llm_models or []),
                trust_level,
                now,
                now,
            )
            row = await conn.fetchrow("SELECT * FROM workers WHERE worker_id = $1", worker_id)
            return self._row_to_dict(row)  # type: ignore[return-value]

    def list_workers(self) -> list[dict[str, Any]]:
        if self.backend_type == "postgres":
            return self._run_sync(self.list_workers_async)
        with self._lock:
            rows = self.conn.execute("SELECT * FROM workers ORDER BY registered_at ASC").fetchall()
            return [self._row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]

    async def list_workers_async(self) -> list[dict[str, Any]]:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.list_workers)
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM workers ORDER BY registered_at ASC")
            return [self._row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]

    def acquire_lease_and_attempt(
        self,
        task_id: str,
        worker_id: str,
        *,
        base_tree_sha: str | None = None,
        workspace_path: str | None = None,
        parent_attempt_id: str | None = None,
        branch_label: str | None = None,
        retry_index: int = 0,
        ttl_seconds: int = 300,
    ) -> dict[str, Any]:
        """Monotonically increments fencing_token per task and creates lease + attempt."""
        if self.backend_type == "postgres":
            return self._run_sync(
                self.acquire_lease_and_attempt_async,
                task_id,
                worker_id,
                base_tree_sha=base_tree_sha,
                workspace_path=workspace_path,
                parent_attempt_id=parent_attempt_id,
                branch_label=branch_label,
                retry_index=retry_index,
                ttl_seconds=ttl_seconds,
            )
        with self._lock:
            max_tok_row = self.conn.execute(
                "SELECT COALESCE(MAX(fencing_token), 0) AS max_tok FROM leases WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            next_token = (int(max_tok_row["max_tok"]) if max_tok_row else 0) + 1
            now_dt = datetime.now(timezone.utc)
            now = now_dt.isoformat()
            expires = (now_dt + timedelta(seconds=ttl_seconds)).isoformat()

            # Expire any prior unreleased leases if not speculative branching
            if parent_attempt_id is None:
                self.conn.execute(
                    "UPDATE leases SET released_at = ? WHERE task_id = ? AND released_at IS NULL",
                    (now, task_id),
                )

            attempt_id = f"att_{uuid.uuid4().hex[:12]}"
            lease_id = f"lse_{uuid.uuid4().hex[:12]}"

            self.conn.execute(
                """
                INSERT INTO attempts (
                    attempt_id, task_id, parent_attempt_id, branch_label, worker_id,
                    lease_id, fencing_token, base_tree_sha, head_tree_sha,
                    workspace_path, status, started_at, retry_index
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'RUNNING', ?, ?)
                """,
                (
                    attempt_id,
                    task_id,
                    parent_attempt_id,
                    branch_label,
                    worker_id,
                    lease_id,
                    next_token,
                    base_tree_sha,
                    base_tree_sha,
                    workspace_path,
                    now,
                    retry_index,
                ),
            )
            self.conn.execute(
                """
                INSERT INTO leases (
                    lease_id, task_id, attempt_id, worker_id,
                    fencing_token, expires_at, renewed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (lease_id, task_id, attempt_id, worker_id, next_token, expires, now),
            )
            self.conn.commit()
            return {
                "attempt_id": attempt_id,
                "lease_id": lease_id,
                "task_id": task_id,
                "worker_id": worker_id,
                "fencing_token": next_token,
                "base_tree_sha": base_tree_sha,
                "workspace_path": workspace_path,
                "branch_label": branch_label,
                "expires_at": expires,
            }

    async def acquire_lease_and_attempt_async(
        self,
        task_id: str,
        worker_id: str,
        *,
        base_tree_sha: str | None = None,
        workspace_path: str | None = None,
        parent_attempt_id: str | None = None,
        branch_label: str | None = None,
        retry_index: int = 0,
        ttl_seconds: int = 300,
    ) -> dict[str, Any]:
        """Monotonically increments fencing_token per task and creates lease + attempt."""
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(
                self.acquire_lease_and_attempt,
                task_id,
                worker_id,
                base_tree_sha=base_tree_sha,
                workspace_path=workspace_path,
                parent_attempt_id=parent_attempt_id,
                branch_label=branch_label,
                retry_index=retry_index,
                ttl_seconds=ttl_seconds,
            )
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.fetchrow("SELECT task_id FROM tasks WHERE task_id = $1 FOR UPDATE", task_id)
                max_tok_row = await conn.fetchrow(
                    "SELECT COALESCE(MAX(fencing_token), 0) AS max_tok FROM leases WHERE task_id = $1",
                    task_id,
                )
                next_token = (int(max_tok_row["max_tok"]) if max_tok_row else 0) + 1
                now_dt = datetime.now(timezone.utc)
                now = now_dt.isoformat()
                expires = (now_dt + timedelta(seconds=ttl_seconds)).isoformat()

                if parent_attempt_id is None:
                    await conn.execute(
                        "UPDATE leases SET released_at = $1 WHERE task_id = $2 AND released_at IS NULL",
                        now,
                        task_id,
                    )

                attempt_id = f"att_{uuid.uuid4().hex[:12]}"
                lease_id = f"lse_{uuid.uuid4().hex[:12]}"

                await conn.execute(
                    """
                    INSERT INTO attempts (
                        attempt_id, task_id, parent_attempt_id, branch_label, worker_id,
                        lease_id, fencing_token, base_tree_sha, head_tree_sha,
                        workspace_path, status, started_at, retry_index
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'RUNNING', $11, $12)
                    """,
                    attempt_id,
                    task_id,
                    parent_attempt_id,
                    branch_label,
                    worker_id,
                    lease_id,
                    next_token,
                    base_tree_sha,
                    base_tree_sha,
                    workspace_path,
                    now,
                    retry_index,
                )
                await conn.execute(
                    """
                    INSERT INTO leases (
                        lease_id, task_id, attempt_id, worker_id,
                        fencing_token, expires_at, renewed_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """,
                    lease_id,
                    task_id,
                    attempt_id,
                    worker_id,
                    next_token,
                    expires,
                    now,
                )
                return {
                    "attempt_id": attempt_id,
                    "lease_id": lease_id,
                    "task_id": task_id,
                    "worker_id": worker_id,
                    "fencing_token": next_token,
                    "base_tree_sha": base_tree_sha,
                    "workspace_path": workspace_path,
                    "branch_label": branch_label,
                    "expires_at": expires,
                }

    def update_attempt(
        self,
        attempt_id: str,
        *,
        status: str | None = None,
        head_tree_sha: str | None = None,
        workspace_path: str | None = None,
        failure_class: str | None = None,
    ) -> dict[str, Any]:
        if self.backend_type == "postgres":
            return self._run_sync(
                self.update_attempt_async,
                attempt_id,
                status=status,
                head_tree_sha=head_tree_sha,
                workspace_path=workspace_path,
                failure_class=failure_class,
            )
        with self._lock:
            row = self.conn.execute("SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
            if not row:
                raise KeyError(f"Attempt not found: {attempt_id}")
            new_status = status or row["status"]
            new_head = head_tree_sha or row["head_tree_sha"]
            new_ws = workspace_path or row["workspace_path"]
            new_fail = failure_class if failure_class is not None else row["failure_class"]
            ended = utcnow_iso() if new_status in ("SUCCEEDED", "FAILED", "CANCELED", "DISCARDED") else row["ended_at"]
            self.conn.execute(
                """
                UPDATE attempts
                SET status = ?, head_tree_sha = ?, workspace_path = ?, failure_class = ?, ended_at = ?
                WHERE attempt_id = ?
                """,
                (new_status, new_head, new_ws, new_fail, ended, attempt_id),
            )
            self.conn.commit()
            updated = self.conn.execute("SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
            return self._row_to_dict(updated)  # type: ignore[return-value]

    async def update_attempt_async(
        self,
        attempt_id: str,
        *,
        status: str | None = None,
        head_tree_sha: str | None = None,
        workspace_path: str | None = None,
        failure_class: str | None = None,
    ) -> dict[str, Any]:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(
                self.update_attempt,
                attempt_id,
                status=status,
                head_tree_sha=head_tree_sha,
                workspace_path=workspace_path,
                failure_class=failure_class,
            )
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM attempts WHERE attempt_id = $1", attempt_id)
            if not row:
                raise KeyError(f"Attempt not found: {attempt_id}")
            new_status = status or row["status"]
            new_head = head_tree_sha or row["head_tree_sha"]
            new_ws = workspace_path or row["workspace_path"]
            new_fail = failure_class if failure_class is not None else row["failure_class"]
            ended = utcnow_iso() if new_status in ("SUCCEEDED", "FAILED", "CANCELED", "DISCARDED") else row["ended_at"]
            await conn.execute(
                """
                UPDATE attempts
                SET status = $1, head_tree_sha = $2, workspace_path = $3, failure_class = $4, ended_at = $5
                WHERE attempt_id = $6
                """,
                new_status,
                new_head,
                new_ws,
                new_fail,
                ended,
                attempt_id,
            )
            updated = await conn.fetchrow("SELECT * FROM attempts WHERE attempt_id = $1", attempt_id)
            return self._row_to_dict(updated)  # type: ignore[return-value]

    def list_attempts(self, task_id: str) -> list[dict[str, Any]]:
        if self.backend_type == "postgres":
            return self._run_sync(self.list_attempts_async, task_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM attempts WHERE task_id = ? ORDER BY started_at ASC", (task_id,)
            ).fetchall()
            return [self._row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]

    async def list_attempts_async(self, task_id: str) -> list[dict[str, Any]]:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.list_attempts, task_id)
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM attempts WHERE task_id = $1 ORDER BY started_at ASC", task_id
            )
            return [self._row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Plans & Checkpoints (FR-012, FR-020, FR-021)
    # ------------------------------------------------------------------

    def save_plan(
        self,
        task_id: str,
        dag_nodes: list[dict[str, Any]],
        *,
        blast_radius_checks: list[str] | None = None,
        created_by: str = "orchestrator",
        rationale: str = "Initial DAG plan",
    ) -> dict[str, Any]:
        if self.backend_type == "postgres":
            return self._run_sync(
                self.save_plan_async,
                task_id,
                dag_nodes,
                blast_radius_checks=blast_radius_checks,
                created_by=created_by,
                rationale=rationale,
            )
        with self._lock:
            last = self.conn.execute(
                "SELECT MAX(revision) AS max_rev FROM plans WHERE task_id = ?", (task_id,)
            ).fetchone()
            prev_rev = int(last["max_rev"]) if (last and last["max_rev"] is not None) else None
            rev = (prev_rev + 1) if prev_rev is not None else 1
            plan_id = f"pln_{uuid.uuid4().hex[:12]}"
            now = utcnow_iso()

            self.conn.execute(
                """
                INSERT INTO plans (
                    plan_id, task_id, revision, dag_json, blast_radius_checks_json,
                    created_by, rationale, created_at, supersedes_revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan_id,
                    task_id,
                    rev,
                    canonical_json(dag_nodes),
                    canonical_json(blast_radius_checks or []),
                    created_by,
                    rationale,
                    now,
                    prev_rev,
                ),
            )
            self.conn.commit()
            return self.get_latest_plan(task_id)  # type: ignore[return-value]

    async def save_plan_async(
        self,
        task_id: str,
        dag_nodes: list[dict[str, Any]],
        *,
        blast_radius_checks: list[str] | None = None,
        created_by: str = "orchestrator",
        rationale: str = "Initial DAG plan",
    ) -> dict[str, Any]:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(
                self.save_plan,
                task_id,
                dag_nodes,
                blast_radius_checks=blast_radius_checks,
                created_by=created_by,
                rationale=rationale,
            )
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                last = await conn.fetchrow(
                    "SELECT MAX(revision) AS max_rev FROM plans WHERE task_id = $1", task_id
                )
                prev_rev = int(last["max_rev"]) if (last and last["max_rev"] is not None) else None
                rev = (prev_rev + 1) if prev_rev is not None else 1
                plan_id = f"pln_{uuid.uuid4().hex[:12]}"
                now = utcnow_iso()

                await conn.execute(
                    """
                    INSERT INTO plans (
                        plan_id, task_id, revision, dag_json, blast_radius_checks_json,
                        created_by, rationale, created_at, supersedes_revision
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    """,
                    plan_id,
                    task_id,
                    rev,
                    canonical_json(dag_nodes),
                    canonical_json(blast_radius_checks or []),
                    created_by,
                    rationale,
                    now,
                    prev_rev,
                )
                row = await conn.fetchrow(
                    "SELECT * FROM plans WHERE task_id = $1 ORDER BY revision DESC LIMIT 1",
                    task_id,
                )
                return self._row_to_dict(row)  # type: ignore[return-value]

    def get_latest_plan(self, task_id: str) -> dict[str, Any] | None:
        if self.backend_type == "postgres":
            return self._run_sync(self.get_latest_plan_async, task_id)
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM plans WHERE task_id = ? ORDER BY revision DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            return self._row_to_dict(row)

    async def get_latest_plan_async(self, task_id: str) -> dict[str, Any] | None:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.get_latest_plan, task_id)
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM plans WHERE task_id = $1 ORDER BY revision DESC LIMIT 1",
                task_id,
            )
            return self._row_to_dict(row)

    def append_blast_radius_checks(self, task_id: str, checks: list[str], warning_summary: str) -> dict[str, Any] | None:
        """FR-036a: Dynamically append impacted downstream tests/typechecks to active plan."""
        if self.backend_type == "postgres":
            return self._run_sync(self.append_blast_radius_checks_async, task_id, checks, warning_summary)
        with self._lock:
            plan = self.get_latest_plan(task_id)
            if not plan:
                return None
            existing = list(plan.get("blast_radius_checks") or [])
            for c in checks:
                if c not in existing:
                    existing.append(c)
            self.conn.execute(
                "UPDATE plans SET blast_radius_checks_json = ? WHERE plan_id = ?",
                (canonical_json(existing), plan["plan_id"]),
            )
            self._append_event_locked(
                task_id=task_id,
                attempt_id=None,
                event_type="AST_BLAST_RADIUS_INTERCEPTED",
                prior_state=None,
                next_state=None,
                reason_code="DOWNSTREAM_CALLERS_DETECTED",
                workspace_tree_sha=None,
                policy_version="pol-v1.3",
                payload={"warning": warning_summary, "pinned_checks": existing},
                actor="graphify_interceptor",
            )
            self.conn.commit()
            return self.get_latest_plan(task_id)

    async def append_blast_radius_checks_async(self, task_id: str, checks: list[str], warning_summary: str) -> dict[str, Any] | None:
        """FR-036a: Dynamically append impacted downstream tests/typechecks to active plan."""
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.append_blast_radius_checks, task_id, checks, warning_summary)
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT * FROM plans WHERE task_id = $1 ORDER BY revision DESC LIMIT 1",
                    task_id,
                )
                plan = self._row_to_dict(row)
                if not plan:
                    return None
                existing = list(plan.get("blast_radius_checks") or [])
                for c in checks:
                    if c not in existing:
                        existing.append(c)
                await conn.execute(
                    "UPDATE plans SET blast_radius_checks_json = $1 WHERE plan_id = $2",
                    canonical_json(existing),
                    plan["plan_id"],
                )
                await self._append_event_async_conn(
                    conn=conn,
                    task_id=task_id,
                    attempt_id=None,
                    event_type="AST_BLAST_RADIUS_INTERCEPTED",
                    prior_state=None,
                    next_state=None,
                    reason_code="DOWNSTREAM_CALLERS_DETECTED",
                    workspace_tree_sha=None,
                    policy_version="pol-v1.3",
                    payload={"warning": warning_summary, "pinned_checks": existing},
                    actor="graphify_interceptor",
                )
                updated = await conn.fetchrow(
                    "SELECT * FROM plans WHERE task_id = $1 ORDER BY revision DESC LIMIT 1",
                    task_id,
                )
                return self._row_to_dict(updated)

    def create_checkpoint(
        self,
        task_id: str,
        attempt_id: str,
        plan_revision: int,
        completed_steps: list[str],
        pending_steps: list[str],
        workspace_tree_sha: str,
        snapshot_path: str,
        artifact_uri: str | None = None,
    ) -> dict[str, Any]:
        if self.backend_type == "postgres":
            return self._run_sync(
                self.create_checkpoint_async,
                task_id,
                attempt_id,
                plan_revision,
                completed_steps,
                pending_steps,
                workspace_tree_sha,
                snapshot_path,
                artifact_uri=artifact_uri,
            )
        with self._lock:
            checkpoint_id = f"chk_{uuid.uuid4().hex[:12]}"
            now = utcnow_iso()
            digest = sha256_hex(
                f"{task_id}|{attempt_id}|{plan_revision}|{canonical_json(completed_steps)}|"
                f"{canonical_json(pending_steps)}|{workspace_tree_sha}|{snapshot_path}"
            )
            self.conn.execute(
                """
                INSERT INTO checkpoints (
                    checkpoint_id, task_id, attempt_id, plan_revision,
                    completed_steps_json, pending_steps_json, workspace_tree_sha,
                    snapshot_path, artifact_uri, sha256, schema_version, created_at, validity
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '1.0', ?, 'VALID')
                """,
                (
                    checkpoint_id,
                    task_id,
                    attempt_id,
                    plan_revision,
                    canonical_json(completed_steps),
                    canonical_json(pending_steps),
                    workspace_tree_sha,
                    snapshot_path,
                    artifact_uri,
                    digest,
                    now,
                ),
            )
            self.conn.execute(
                "UPDATE tasks SET active_workspace_tree_sha = ?, updated_at = ? WHERE task_id = ?",
                (workspace_tree_sha, now, task_id),
            )
            self.conn.commit()
            row = self.conn.execute(
                "SELECT * FROM checkpoints WHERE checkpoint_id = ?", (checkpoint_id,)
            ).fetchone()
            return self._row_to_dict(row)  # type: ignore[return-value]

    async def create_checkpoint_async(
        self,
        task_id: str,
        attempt_id: str,
        plan_revision: int,
        completed_steps: list[str],
        pending_steps: list[str],
        workspace_tree_sha: str,
        snapshot_path: str,
        artifact_uri: str | None = None,
    ) -> dict[str, Any]:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(
                self.create_checkpoint,
                task_id,
                attempt_id,
                plan_revision,
                completed_steps,
                pending_steps,
                workspace_tree_sha,
                snapshot_path,
                artifact_uri=artifact_uri,
            )
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                checkpoint_id = f"chk_{uuid.uuid4().hex[:12]}"
                now = utcnow_iso()
                digest = sha256_hex(
                    f"{task_id}|{attempt_id}|{plan_revision}|{canonical_json(completed_steps)}|"
                    f"{canonical_json(pending_steps)}|{workspace_tree_sha}|{snapshot_path}"
                )
                await conn.execute(
                    """
                    INSERT INTO checkpoints (
                        checkpoint_id, task_id, attempt_id, plan_revision,
                        completed_steps_json, pending_steps_json, workspace_tree_sha,
                        snapshot_path, artifact_uri, sha256, schema_version, created_at, validity
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, '1.0', $11, 'VALID')
                    """,
                    checkpoint_id,
                    task_id,
                    attempt_id,
                    plan_revision,
                    canonical_json(completed_steps),
                    canonical_json(pending_steps),
                    workspace_tree_sha,
                    snapshot_path,
                    artifact_uri,
                    digest,
                    now,
                )
                await conn.execute(
                    "UPDATE tasks SET active_workspace_tree_sha = $1, updated_at = $2 WHERE task_id = $3",
                    workspace_tree_sha,
                    now,
                    task_id,
                )
                row = await conn.fetchrow("SELECT * FROM checkpoints WHERE checkpoint_id = $1", checkpoint_id)
                return self._row_to_dict(row)  # type: ignore[return-value]

    def get_latest_checkpoint(self, task_id: str) -> dict[str, Any] | None:
        if self.backend_type == "postgres":
            return self._run_sync(self.get_latest_checkpoint_async, task_id)
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM checkpoints WHERE task_id = ? AND validity = 'VALID' ORDER BY created_at DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            return self._row_to_dict(row)

    async def get_latest_checkpoint_async(self, task_id: str) -> dict[str, Any] | None:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.get_latest_checkpoint, task_id)
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM checkpoints WHERE task_id = $1 AND validity = 'VALID' ORDER BY created_at DESC LIMIT 1",
                task_id,
            )
            return self._row_to_dict(row)

    # ------------------------------------------------------------------
    # Tool Gateway Call-Level Journaling (FR-015)
    # ------------------------------------------------------------------

    def find_sealed_tool_call(
        self,
        task_id: str,
        step_id: str,
        tool_id: str,
        canonical_args_hash: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        if self.backend_type == "postgres":
            return self._run_sync(
                self.find_sealed_tool_call_async,
                task_id,
                step_id,
                tool_id,
                canonical_args_hash,
                idempotency_key,
            )
        with self._lock:
            row = self.conn.execute(
                """
                SELECT * FROM tool_calls
                WHERE task_id = ? AND step_id = ? AND tool_id = ?
                  AND (canonical_args_hash = ? OR idempotency_key = ?)
                  AND status = 'SEALED'
                ORDER BY started_at ASC LIMIT 1
                """,
                (task_id, step_id, tool_id, canonical_args_hash, idempotency_key),
            ).fetchone()
            return self._row_to_dict(row)

    async def find_sealed_tool_call_async(
        self,
        task_id: str,
        step_id: str,
        tool_id: str,
        canonical_args_hash: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(
                self.find_sealed_tool_call,
                task_id,
                step_id,
                tool_id,
                canonical_args_hash,
                idempotency_key,
            )
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM tool_calls
                WHERE task_id = $1 AND step_id = $2 AND tool_id = $3
                  AND (canonical_args_hash = $4 OR idempotency_key = $5)
                  AND status = 'SEALED'
                ORDER BY started_at ASC LIMIT 1
                """,
                task_id,
                step_id,
                tool_id,
                canonical_args_hash,
                idempotency_key,
            )
            return self._row_to_dict(row)

    def begin_tool_call(
        self,
        *,
        task_id: str,
        attempt_id: str,
        step_id: str,
        sequence_in_step: int,
        tool_id: str,
        risk_class: str,
        canonical_args_hash: str,
        arguments_redacted: dict[str, Any],
        policy_decision: str,
        idempotency_key: str,
        replayed_from_call_id: str | None = None,
    ) -> str:
        if self.backend_type == "postgres":
            return self._run_sync(
                self.begin_tool_call_async,
                task_id=task_id,
                attempt_id=attempt_id,
                step_id=step_id,
                sequence_in_step=sequence_in_step,
                tool_id=tool_id,
                risk_class=risk_class,
                canonical_args_hash=canonical_args_hash,
                arguments_redacted=arguments_redacted,
                policy_decision=policy_decision,
                idempotency_key=idempotency_key,
                replayed_from_call_id=replayed_from_call_id,
            )
        with self._lock:
            call_id = f"cal_{uuid.uuid4().hex[:12]}"
            now = utcnow_iso()
            self.conn.execute(
                """
                INSERT INTO tool_calls (
                    call_id, task_id, attempt_id, step_id, sequence_in_step,
                    tool_id, risk_class, canonical_args_hash, arguments_redacted_json,
                    replayed_from_call_id, policy_decision, idempotency_key,
                    started_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
                """,
                (
                    call_id,
                    task_id,
                    attempt_id,
                    step_id,
                    sequence_in_step,
                    tool_id,
                    risk_class,
                    canonical_args_hash,
                    canonical_json(arguments_redacted),
                    replayed_from_call_id,
                    policy_decision,
                    idempotency_key,
                    now,
                ),
            )
            self.conn.commit()
            return call_id

    async def begin_tool_call_async(
        self,
        *,
        task_id: str,
        attempt_id: str,
        step_id: str,
        sequence_in_step: int,
        tool_id: str,
        risk_class: str,
        canonical_args_hash: str,
        arguments_redacted: dict[str, Any],
        policy_decision: str,
        idempotency_key: str,
        replayed_from_call_id: str | None = None,
    ) -> str:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(
                self.begin_tool_call,
                task_id=task_id,
                attempt_id=attempt_id,
                step_id=step_id,
                sequence_in_step=sequence_in_step,
                tool_id=tool_id,
                risk_class=risk_class,
                canonical_args_hash=canonical_args_hash,
                arguments_redacted=arguments_redacted,
                policy_decision=policy_decision,
                idempotency_key=idempotency_key,
                replayed_from_call_id=replayed_from_call_id,
            )
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            call_id = f"cal_{uuid.uuid4().hex[:12]}"
            now = utcnow_iso()
            await conn.execute(
                """
                INSERT INTO tool_calls (
                    call_id, task_id, attempt_id, step_id, sequence_in_step,
                    tool_id, risk_class, canonical_args_hash, arguments_redacted_json,
                    replayed_from_call_id, policy_decision, idempotency_key,
                    started_at, status
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, 'PENDING')
                """,
                call_id,
                task_id,
                attempt_id,
                step_id,
                sequence_in_step,
                tool_id,
                risk_class,
                canonical_args_hash,
                canonical_json(arguments_redacted),
                replayed_from_call_id,
                policy_decision,
                idempotency_key,
                now,
            )
            return call_id

    def seal_tool_call(
        self,
        call_id: str,
        result: dict[str, Any],
        *,
        status: str = "SEALED",
        result_ref: str | None = None,
    ) -> dict[str, Any]:
        if self.backend_type == "postgres":
            return self._run_sync(
                self.seal_tool_call_async,
                call_id,
                result,
                status=status,
                result_ref=result_ref,
            )
        with self._lock:
            now = utcnow_iso()
            self.conn.execute(
                """
                UPDATE tool_calls
                SET result_json = ?, result_ref = ?, ended_at = ?, status = ?
                WHERE call_id = ?
                """,
                (canonical_json(result), result_ref, now, status, call_id),
            )
            self.conn.commit()
            row = self.conn.execute("SELECT * FROM tool_calls WHERE call_id = ?", (call_id,)).fetchone()
            return self._row_to_dict(row)  # type: ignore[return-value]

    async def seal_tool_call_async(
        self,
        call_id: str,
        result: dict[str, Any],
        *,
        status: str = "SEALED",
        result_ref: str | None = None,
    ) -> dict[str, Any]:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(
                self.seal_tool_call,
                call_id,
                result,
                status=status,
                result_ref=result_ref,
            )
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            now = utcnow_iso()
            await conn.execute(
                """
                UPDATE tool_calls
                SET result_json = $1, result_ref = $2, ended_at = $3, status = $4
                WHERE call_id = $5
                """,
                canonical_json(result),
                result_ref,
                now,
                status,
                call_id,
            )
            row = await conn.fetchrow("SELECT * FROM tool_calls WHERE call_id = $1", call_id)
            return self._row_to_dict(row)  # type: ignore[return-value]

    def list_tool_calls(self, task_id: str) -> list[dict[str, Any]]:
        if self.backend_type == "postgres":
            return self._run_sync(self.list_tool_calls_async, task_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM tool_calls WHERE task_id = ? ORDER BY started_at ASC", (task_id,)
            ).fetchall()
            return [self._row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]

    async def list_tool_calls_async(self, task_id: str) -> list[dict[str, Any]]:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.list_tool_calls, task_id)
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM tool_calls WHERE task_id = $1 ORDER BY started_at ASC", task_id
            )
            return [self._row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Approvals (FR-004, FR-023, Section 12.1)
    # ------------------------------------------------------------------

    def create_approval(
        self,
        *,
        task_id: str,
        attempt_id: str | None,
        step_id: str | None,
        action_type: str,
        action_params: dict[str, Any],
        blast_radius_summary: str,
        requested_by: str = "worker",
    ) -> dict[str, Any]:
        if self.backend_type == "postgres":
            return self._run_sync(
                self.create_approval_async,
                task_id=task_id,
                attempt_id=attempt_id,
                step_id=step_id,
                action_type=action_type,
                action_params=action_params,
                blast_radius_summary=blast_radius_summary,
                requested_by=requested_by,
            )
        with self._lock:
            approval_id = f"apr_{uuid.uuid4().hex[:12]}"
            now = utcnow_iso()
            action_hash = sha256_hex(f"{task_id}|{action_type}|{canonical_json(action_params)}")
            self.conn.execute(
                """
                INSERT INTO approvals (
                    approval_id, task_id, attempt_id, step_id, action_type,
                    action_hash, action_params_json, blast_radius_summary,
                    requested_by, decision, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)
                """,
                (
                    approval_id,
                    task_id,
                    attempt_id,
                    step_id,
                    action_type,
                    action_hash,
                    canonical_json(action_params),
                    blast_radius_summary,
                    requested_by,
                    now,
                ),
            )
            self.conn.commit()
            row = self.conn.execute(
                "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
            return self._row_to_dict(row)  # type: ignore[return-value]

    async def create_approval_async(
        self,
        *,
        task_id: str,
        attempt_id: str | None,
        step_id: str | None,
        action_type: str,
        action_params: dict[str, Any],
        blast_radius_summary: str,
        requested_by: str = "worker",
    ) -> dict[str, Any]:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(
                self.create_approval,
                task_id=task_id,
                attempt_id=attempt_id,
                step_id=step_id,
                action_type=action_type,
                action_params=action_params,
                blast_radius_summary=blast_radius_summary,
                requested_by=requested_by,
            )
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            approval_id = f"apr_{uuid.uuid4().hex[:12]}"
            now = utcnow_iso()
            action_hash = sha256_hex(f"{task_id}|{action_type}|{canonical_json(action_params)}")
            await conn.execute(
                """
                INSERT INTO approvals (
                    approval_id, task_id, attempt_id, step_id, action_type,
                    action_hash, action_params_json, blast_radius_summary,
                    requested_by, decision, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'PENDING', $10)
                """,
                approval_id,
                task_id,
                attempt_id,
                step_id,
                action_type,
                action_hash,
                canonical_json(action_params),
                blast_radius_summary,
                requested_by,
                now,
            )
            row = await conn.fetchrow("SELECT * FROM approvals WHERE approval_id = $1", approval_id)
            return self._row_to_dict(row)  # type: ignore[return-value]

    def decide_approval(
        self,
        approval_id: str,
        decision: str,
        decided_by: str = "owner",
        comment: str = "",
    ) -> dict[str, Any]:
        if self.backend_type == "postgres":
            return self._run_sync(
                self.decide_approval_async,
                approval_id,
                decision,
                decided_by=decided_by,
                comment=comment,
            )
        norm = decision.upper()
        if norm not in ("APPROVED", "REJECTED"):
            raise ValueError("Decision must be APPROVED or REJECTED")
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
            if not row:
                raise KeyError(f"Approval not found: {approval_id}")
            now = utcnow_iso()
            self.conn.execute(
                """
                UPDATE approvals
                SET decision = ?, decided_by = ?, decided_at = ?, comment = ?
                WHERE approval_id = ?
                """,
                (norm, decided_by, now, comment, approval_id),
            )
            self._append_event_locked(
                task_id=row["task_id"],
                attempt_id=row["attempt_id"],
                event_type=f"APPROVAL_{norm}",
                prior_state=None,
                next_state=None,
                reason_code=f"OWNER_{norm}",
                workspace_tree_sha=None,
                policy_version="pol-v1.3",
                payload={"approval_id": approval_id, "decision": norm, "comment": comment},
                actor=decided_by,
            )
            self.conn.commit()
            updated = self.conn.execute(
                "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
            return self._row_to_dict(updated)  # type: ignore[return-value]

    async def decide_approval_async(
        self,
        approval_id: str,
        decision: str,
        decided_by: str = "owner",
        comment: str = "",
    ) -> dict[str, Any]:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(
                self.decide_approval,
                approval_id,
                decision,
                decided_by=decided_by,
                comment=comment,
            )
        norm = decision.upper()
        if norm not in ("APPROVED", "REJECTED"):
            raise ValueError("Decision must be APPROVED or REJECTED")
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("SELECT * FROM approvals WHERE approval_id = $1", approval_id)
                if not row:
                    raise KeyError(f"Approval not found: {approval_id}")
                now = utcnow_iso()
                await conn.execute(
                    """
                    UPDATE approvals
                    SET decision = $1, decided_by = $2, decided_at = $3, comment = $4
                    WHERE approval_id = $5
                    """,
                    norm,
                    decided_by,
                    now,
                    comment,
                    approval_id,
                )
                await self._append_event_async_conn(
                    conn=conn,
                    task_id=row["task_id"],
                    attempt_id=row["attempt_id"],
                    event_type=f"APPROVAL_{norm}",
                    prior_state=None,
                    next_state=None,
                    reason_code=f"OWNER_{norm}",
                    workspace_tree_sha=None,
                    policy_version="pol-v1.3",
                    payload={"approval_id": approval_id, "decision": norm, "comment": comment},
                    actor=decided_by,
                )
                updated = await conn.fetchrow("SELECT * FROM approvals WHERE approval_id = $1", approval_id)
                return self._row_to_dict(updated)  # type: ignore[return-value]

    def list_approvals(self, task_id: str | None = None, decision: str | None = None) -> list[dict[str, Any]]:
        if self.backend_type == "postgres":
            return self._run_sync(self.list_approvals_async, task_id=task_id, decision=decision)
        with self._lock:
            query = "SELECT * FROM approvals WHERE 1=1"
            params: list[Any] = []
            if task_id:
                query += " AND task_id = ?"
                params.append(task_id)
            if decision:
                query += " AND decision = ?"
                params.append(decision.upper())
            query += " ORDER BY created_at DESC"
            rows = self.conn.execute(query, tuple(params)).fetchall()
            return [self._row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]

    async def list_approvals_async(self, task_id: str | None = None, decision: str | None = None) -> list[dict[str, Any]]:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.list_approvals, task_id=task_id, decision=decision)
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            query = "SELECT * FROM approvals WHERE 1=1"
            params: list[Any] = []
            if task_id:
                params.append(task_id)
                query += f" AND task_id = ${len(params)}"
            if decision:
                params.append(decision.upper())
                query += f" AND decision = ${len(params)}"
            query += " ORDER BY created_at DESC"
            rows = await conn.fetch(query, *params)
            return [self._row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Artifacts & Proof-Carrying Bundles (FR-017, FR-018)
    # ------------------------------------------------------------------

    def record_artifact(
        self,
        *,
        task_id: str,
        kind: str,
        uri: str,
        sha256: str,
        size_bytes: int,
        media_type: str = "application/json",
        sensitivity: str = "internal",
    ) -> dict[str, Any]:
        if self.backend_type == "postgres":
            return self._run_sync(
                self.record_artifact_async,
                task_id=task_id,
                kind=kind,
                uri=uri,
                sha256=sha256,
                size_bytes=size_bytes,
                media_type=media_type,
                sensitivity=sensitivity,
            )
        with self._lock:
            artifact_id = f"art_{uuid.uuid4().hex[:12]}"
            now = utcnow_iso()
            self.conn.execute(
                """
                INSERT INTO artifacts (
                    artifact_id, task_id, kind, uri, sha256, size_bytes,
                    media_type, sensitivity, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (artifact_id, task_id, kind, uri, sha256, size_bytes, media_type, sensitivity, now),
            )
            self.conn.commit()
            row = self.conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
            return self._row_to_dict(row)  # type: ignore[return-value]

    async def record_artifact_async(
        self,
        *,
        task_id: str,
        kind: str,
        uri: str,
        sha256: str,
        size_bytes: int,
        media_type: str = "application/json",
        sensitivity: str = "internal",
    ) -> dict[str, Any]:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(
                self.record_artifact,
                task_id=task_id,
                kind=kind,
                uri=uri,
                sha256=sha256,
                size_bytes=size_bytes,
                media_type=media_type,
                sensitivity=sensitivity,
            )
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            artifact_id = f"art_{uuid.uuid4().hex[:12]}"
            now = utcnow_iso()
            await conn.execute(
                """
                INSERT INTO artifacts (
                    artifact_id, task_id, kind, uri, sha256, size_bytes,
                    media_type, sensitivity, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                artifact_id,
                task_id,
                kind,
                uri,
                sha256,
                size_bytes,
                media_type,
                sensitivity,
                now,
            )
            row = await conn.fetchrow("SELECT * FROM artifacts WHERE artifact_id = $1", artifact_id)
            return self._row_to_dict(row)  # type: ignore[return-value]

    def list_artifacts(self, task_id: str) -> list[dict[str, Any]]:
        if self.backend_type == "postgres":
            return self._run_sync(self.list_artifacts_async, task_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM artifacts WHERE task_id = ? ORDER BY created_at ASC", (task_id,)
            ).fetchall()
            return [self._row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]

    async def list_artifacts_async(self, task_id: str) -> list[dict[str, Any]]:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.list_artifacts, task_id)
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM artifacts WHERE task_id = $1 ORDER BY created_at ASC", task_id
            )
            return [self._row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]

    def save_proof_bundle(
        self,
        *,
        task_id: str,
        attempt_id: str,
        git_diff_sha: str,
        git_diff_text: str,
        workspace_tree_sha: str,
        runtime_digest: str,
        ast_impact: dict[str, Any],
        verification_results: dict[str, Any],
    ) -> dict[str, Any]:
        if self.backend_type == "postgres":
            return self._run_sync(
                self.save_proof_bundle_async,
                task_id=task_id,
                attempt_id=attempt_id,
                git_diff_sha=git_diff_sha,
                git_diff_text=git_diff_text,
                workspace_tree_sha=workspace_tree_sha,
                runtime_digest=runtime_digest,
                ast_impact=ast_impact,
                verification_results=verification_results,
            )
        with self._lock:
            proof_id = f"prf_{uuid.uuid4().hex[:12]}"
            now = utcnow_iso()
            events = self.list_events(task_id)
            tip_event_sha = events[-1]["event_sha256"] if events else ("0" * 64)
            signature_material = (
                f"{task_id}|{attempt_id}|{git_diff_sha}|{workspace_tree_sha}|"
                f"{runtime_digest}|{canonical_json(ast_impact)}|"
                f"{canonical_json(verification_results)}|{tip_event_sha}"
            )
            ledger_signature = f"ed25519-hmac-sha256:{sha256_hex(signature_material)}"

            self.conn.execute(
                """
                INSERT INTO proof_bundles (
                    proof_id, task_id, attempt_id, git_diff_sha, git_diff_text,
                    workspace_tree_sha, runtime_digest, ast_impact_json,
                    verification_results_json, ledger_signature, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proof_id,
                    task_id,
                    attempt_id,
                    git_diff_sha,
                    git_diff_text,
                    workspace_tree_sha,
                    runtime_digest,
                    canonical_json(ast_impact),
                    canonical_json(verification_results),
                    ledger_signature,
                    now,
                ),
            )
            self.conn.commit()
            row = self.conn.execute(
                "SELECT * FROM proof_bundles WHERE proof_id = ?", (proof_id,)
            ).fetchone()
            return self._row_to_dict(row)  # type: ignore[return-value]

    async def save_proof_bundle_async(
        self,
        *,
        task_id: str,
        attempt_id: str,
        git_diff_sha: str,
        git_diff_text: str,
        workspace_tree_sha: str,
        runtime_digest: str,
        ast_impact: dict[str, Any],
        verification_results: dict[str, Any],
    ) -> dict[str, Any]:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(
                self.save_proof_bundle,
                task_id=task_id,
                attempt_id=attempt_id,
                git_diff_sha=git_diff_sha,
                git_diff_text=git_diff_text,
                workspace_tree_sha=workspace_tree_sha,
                runtime_digest=runtime_digest,
                ast_impact=ast_impact,
                verification_results=verification_results,
            )
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            proof_id = f"prf_{uuid.uuid4().hex[:12]}"
            now = utcnow_iso()
            rows = await conn.fetch(
                "SELECT * FROM task_events WHERE task_id = $1 ORDER BY sequence_no ASC", task_id
            )
            events = [self._row_to_dict(r) for r in rows if r is not None]
            tip_event_sha = events[-1]["event_sha256"] if events else ("0" * 64)
            signature_material = (
                f"{task_id}|{attempt_id}|{git_diff_sha}|{workspace_tree_sha}|"
                f"{runtime_digest}|{canonical_json(ast_impact)}|"
                f"{canonical_json(verification_results)}|{tip_event_sha}"
            )
            ledger_signature = f"ed25519-hmac-sha256:{sha256_hex(signature_material)}"

            await conn.execute(
                """
                INSERT INTO proof_bundles (
                    proof_id, task_id, attempt_id, git_diff_sha, git_diff_text,
                    workspace_tree_sha, runtime_digest, ast_impact_json,
                    verification_results_json, ledger_signature, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """,
                proof_id,
                task_id,
                attempt_id,
                git_diff_sha,
                git_diff_text,
                workspace_tree_sha,
                runtime_digest,
                canonical_json(ast_impact),
                canonical_json(verification_results),
                ledger_signature,
                now,
            )
            row = await conn.fetchrow("SELECT * FROM proof_bundles WHERE proof_id = $1", proof_id)
            return self._row_to_dict(row)  # type: ignore[return-value]

    def get_proof_bundle(self, task_id: str) -> dict[str, Any] | None:
        if self.backend_type == "postgres":
            return self._run_sync(self.get_proof_bundle_async, task_id)
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM proof_bundles WHERE task_id = ? ORDER BY created_at DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            return self._row_to_dict(row)

    async def get_proof_bundle_async(self, task_id: str) -> dict[str, Any] | None:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.get_proof_bundle, task_id)
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM proof_bundles WHERE task_id = $1 ORDER BY created_at DESC LIMIT 1",
                task_id,
            )
            return self._row_to_dict(row)
