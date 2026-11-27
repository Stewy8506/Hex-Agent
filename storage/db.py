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
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import harness_core
from harness_core.core.models import TaskBudget, TaskState, TaskSubmitRequest, VALID_TRANSITIONS, utcnow_iso
from harness_core.storage.ledger_sync import LedgerSyncMixin


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


def validate_dag_topology(dag_nodes: list[Any]) -> None:
    """Reject cyclic, dangling, duplicate or empty DAG plans before persisting (B06).

    A plan with an unsatisfiable dependency cannot make progress; saving it only
    defers the failure to execution, where the old code silently ran an
    out-of-order node. Validation belongs at the persistence boundary.
    """
    if not dag_nodes:
        raise ValueError("DAG plan must contain at least one node")
    nodes = [n.to_dict() if hasattr(n, "to_dict") else dict(n) for n in dag_nodes]
    step_ids = [str(n.get("step_id") or "") for n in nodes]
    if any(not sid for sid in step_ids):
        raise ValueError("Every DAG node requires a non-empty step_id")
    if len(set(step_ids)) != len(step_ids):
        duplicates = sorted({sid for sid in step_ids if step_ids.count(sid) > 1})
        raise ValueError(f"Duplicate DAG step_id(s): {duplicates}")

    known = set(step_ids)
    graph: dict[str, list[str]] = {}
    for node, sid in zip(nodes, step_ids):
        deps = [str(d) for d in (node.get("depends_on") or [])]
        for dep in deps:
            if dep == sid:
                raise ValueError(f"DAG node '{sid}' depends on itself")
            if dep not in known:
                raise ValueError(f"DAG node '{sid}' depends on unknown step '{dep}'")
        graph[sid] = deps

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {sid: WHITE for sid in graph}

    def visit(sid: str, path: list[str]) -> None:
        color[sid] = GRAY
        for dep in graph[sid]:
            if color[dep] == GRAY:
                cycle = " -> ".join(path + [sid, dep])
                raise ValueError(f"Cyclic DAG dependency detected: {cycle}")
            if color[dep] == WHITE:
                visit(dep, path + [sid])
        color[sid] = BLACK

    for sid in graph:
        if color[sid] == WHITE:
            visit(sid, [])


class ClaimTransactionError(RuntimeError):
    """Raised when the claim transaction cannot be started (B42)."""


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


class ChainIntegrityReport(dict):
    """Result report from LedgerStore.verify_chain_integrity()."""

    @property
    def valid(self) -> bool:
        return bool(self.get("valid", False))

    @property
    def errors(self) -> list[str]:
        return list(self.get("errors", []))

    @property
    def tasks_checked(self) -> int:
        return int(self.get("tasks_checked", 0))

    @property
    def verified_events(self) -> int:
        return int(self.get("verified_events", 0))

    def __bool__(self) -> bool:
        return self.valid


class LedgerStore(LedgerSyncMixin):
    """Thread-safe transactional Task Ledger & Call Journal."""

    CLAIM_TASK_POSTGRES_SQL = (
        "SELECT task_id, priority, version, active_workspace_tree_sha, policy_snapshot_id "
        "FROM tasks "
        "WHERE status = 'QUEUED' "
        "ORDER BY priority DESC, created_at ASC "
        "FOR UPDATE SKIP LOCKED "
        "LIMIT 1"
    )
    CLAIM_TASK_SQLITE_SQL = (
        "SELECT task_id, priority, version, active_workspace_tree_sha, policy_snapshot_id "
        "FROM tasks "
        "WHERE status = 'QUEUED' "
        "ORDER BY priority DESC, created_at ASC "
        "LIMIT 1"
    )

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
        self._search_index: Any = None

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
            with self._lock:
                try:
                    self.conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_tasks_queue ON tasks (status, priority DESC, created_at ASC);"
                    )
                    self.conn.commit()
                except Exception:
                    pass

    @property
    def search_index(self) -> Any:
        """Lazily initialize or return the TaskSearchIndex instance."""
        if self._search_index is None:
            from harness_core.capabilities.search import TaskSearchIndex

            if self.backend_type == "sqlite" and self.conn:
                self._search_index = TaskSearchIndex(self.conn)
            else:
                search_db_path = Path.home() / ".hex" / "data" / "task_search.db"
                self._search_index = TaskSearchIndex(search_db_path)
        return self._search_index

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
            try:
                async with self._pool.acquire() as conn:
                    await conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_tasks_queue ON tasks (status, priority DESC, created_at ASC);"
                    )
            except Exception:
                pass
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
            # 1. Bundled package schema (storage/migrations or harness_core/migrations)
            for candidate in [
                Path(__file__).resolve().parent / "migrations" / "0001_initial_schema.sql",
                Path(harness_core.__file__).resolve().parent / "migrations" / "0001_initial_schema.sql",
            ]:
                if candidate.is_file():
                    schema_sql_path = candidate
                    break
            if schema_sql_path is None:
                # 2. Repository root migration
                for p in Path(__file__).resolve().parents:
                    candidate = p / "migrations" / "0001_initial_schema.sql"
                    if candidate.is_file():
                        schema_sql_path = candidate
                        break
        if schema_sql_path and Path(schema_sql_path).is_file():
            sql = Path(schema_sql_path).read_text(encoding="utf-8")
            with self._lock:
                if self.conn is not None:
                    self.conn.executescript(sql)
                    try:
                        self.conn.execute("ALTER TABLE tasks ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0;")
                    except sqlite3.OperationalError:
                        pass
                    try:
                        self.conn.execute("ALTER TABLE tasks ADD COLUMN title_provenance TEXT NOT NULL DEFAULT 'derived';")
                    except sqlite3.OperationalError:
                        pass
                    try:
                        self.conn.execute("""
                        CREATE TABLE IF NOT EXISTS dialogue_turns (
                            turn_id TEXT PRIMARY KEY,
                            task_id TEXT NOT NULL REFERENCES tasks(task_id),
                            turn_index INTEGER NOT NULL,
                            role TEXT NOT NULL,
                            content TEXT NOT NULL,
                            is_active INTEGER NOT NULL DEFAULT 1,
                            branch_id TEXT NOT NULL DEFAULT 'main',
                            metadata_json TEXT NOT NULL DEFAULT '{}',
                            created_at TEXT NOT NULL
                        );
                        """)
                        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_dialogue_turns_lookup ON dialogue_turns (task_id, is_active, turn_index);")
                    except sqlite3.OperationalError:
                        pass
                    try:
                        self.conn.execute("""
                        CREATE TABLE IF NOT EXISTS approval_leases (
                            lease_id TEXT PRIMARY KEY,
                            task_id TEXT,
                            command_prefix TEXT,
                            allowed_paths_json TEXT NOT NULL DEFAULT '[]',
                            scope TEXT NOT NULL DEFAULT 'temporal',
                            ttl_seconds INTEGER NOT NULL DEFAULT 900,
                            granted_at REAL NOT NULL,
                            expires_at REAL NOT NULL,
                            is_revoked INTEGER NOT NULL DEFAULT 0,
                            created_at TEXT NOT NULL,
                            bindings_json TEXT NOT NULL DEFAULT '{}'
                        );
                        """)
                        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_approval_leases_lookup ON approval_leases (is_revoked, expires_at);")
                    except sqlite3.OperationalError:
                        pass
                    try:
                        self.conn.execute("ALTER TABLE approval_leases ADD COLUMN bindings_json TEXT NOT NULL DEFAULT '{}';")
                    except sqlite3.OperationalError:
                        pass
                    try:
                        self.conn.execute("ALTER TABLE proof_bundles ADD COLUMN signed_proof_json TEXT;")
                    except sqlite3.OperationalError:
                        pass
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

    def submit_task(self, req: TaskSubmitRequest | dict[str, Any], policy_snapshot_id: str = "pol-v1.3") -> dict[str, Any]:
        is_dict_input = isinstance(req, dict)
        if is_dict_input:
            initial_status = req.get("status", TaskState.QUEUED.value)
            req = TaskSubmitRequest(**{k: v for k, v in req.items() if k != "status"})
        else:
            initial_status = TaskState.SUBMITTED.value
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

            pinned_val = 1 if getattr(req, "pinned", False) else 0

            self.conn.execute(
                """
                INSERT INTO tasks (
                    task_id, parent_task_id, project_id, goal, execution_mode,
                    status, priority, deadline_at, budget_json, constraints_json,
                    deliverable, policy_snapshot_id, idempotency_key,
                    active_workspace_tree_sha, version, pinned, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    task_id,
                    req.parent_task_id,
                    req.project_id or "default",
                    req.goal,
                    mode,
                    initial_status,
                    req.priority,
                    req.deadline_at,
                    canonical_json(budget),
                    canonical_json(constraints),
                    req.deliverable,
                    policy_snapshot_id,
                    req.idempotency_key,
                    None,
                    pinned_val,
                    now,
                    now,
                ),
            )
            self._append_event_locked(
                task_id=task_id,
                attempt_id=None,
                event_type="TASK_SUBMITTED",
                prior_state=None,
                next_state=initial_status,
                reason_code="INTAKE_ACCEPTED",
                workspace_tree_sha=None,
                policy_version=policy_snapshot_id,
                payload={"goal": req.goal, "execution_mode": mode, "project_id": req.project_id},
                actor="control_api",
            )
            self.conn.commit()
            try:
                self.search_index.index_task(
                    task_id=task_id,
                    project_id=req.project_id or "default",
                    goal=req.goal,
                    status=initial_status,
                )
            except Exception:
                pass
            return self.get_task(task_id)  # type: ignore[return-value]

    def create_task(self, req: TaskSubmitRequest | dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        """Convenience alias for submit_task supporting dict, model, or kwargs."""
        if req is None:
            return self.submit_task(kwargs)
        if isinstance(req, dict):
            merged = {**req, **kwargs}
            return self.submit_task(merged)
        return self.submit_task(req)

    async def submit_task_async(self, req: TaskSubmitRequest | dict[str, Any], policy_snapshot_id: str = "pol-v1.3") -> dict[str, Any]:
        is_dict_input = isinstance(req, dict)
        if is_dict_input:
            initial_status = req.get("status", TaskState.QUEUED.value)
            req = TaskSubmitRequest(**{k: v for k, v in req.items() if k != "status"})
        else:
            initial_status = TaskState.SUBMITTED.value
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

                pinned_val = 1 if getattr(req, "pinned", False) else 0

                await conn.execute(
                    """
                    INSERT INTO tasks (
                        task_id, parent_task_id, project_id, goal, execution_mode,
                        status, priority, deadline_at, budget_json, constraints_json,
                        deliverable, policy_snapshot_id, idempotency_key,
                        active_workspace_tree_sha, version, pinned, created_at, updated_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, 1, $15, $16, $17)
                    """,
                    task_id,
                    req.parent_task_id,
                    req.project_id or "default",
                    req.goal,
                    mode,
                    initial_status,
                    req.priority,
                    req.deadline_at,
                    canonical_json(budget),
                    canonical_json(constraints),
                    req.deliverable,
                    policy_snapshot_id,
                    req.idempotency_key,
                    None,
                    pinned_val,
                    now,
                    now,
                )
                await self._append_event_async_conn(
                    conn=conn,
                    task_id=task_id,
                    attempt_id=None,
                    event_type="TASK_SUBMITTED",
                    prior_state=None,
                    next_state=initial_status,
                    reason_code="INTAKE_ACCEPTED",
                    workspace_tree_sha=None,
                    policy_version=policy_snapshot_id,
                    payload={"goal": req.goal, "execution_mode": mode, "project_id": req.project_id},
                    actor="control_api",
                )
                row = await conn.fetchrow("SELECT * FROM tasks WHERE task_id = $1", task_id)
                try:
                    self.search_index.index_task(
                        task_id=task_id,
                        project_id=req.project_id or "default",
                        goal=req.goal,
                        status=initial_status,
                    )
                except Exception:
                    pass
                return self._row_to_dict(row)  # type: ignore[return-value]

    def _is_fast_path_eligible(self, req: TaskSubmitRequest) -> bool:
        """FR-001a: Read-only (R0/R1) tasks with <3 estimated steps qualify for Fast-Path."""
        if req.steps:
            if len(req.steps) >= 3:
                return False
            return all(s.get("risk_class", "R1") in ("R0", "R1") for s in req.steps)
        lowered = req.goal.lower()
        write_keywords = (
            "fix", "edit", "refactor", "write", "modify", "patch",
            "delete", "deploy", "test", "run", "execute", "build", "compile",
            "implement", "create", "add", "update"
        )
        if any(w in lowered for w in write_keywords):
            return False
        return True

    def transition_task(
        self,
        task_id: str,
        next_state: TaskState | str,
        *,
        reason_code: str = "STATE_TRANSITION",
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
            term_reason = terminal_reason if (target.is_terminal or terminal_reason is not None) else row["terminal_reason"]
            new_deliverable = (payload or {}).get("result") or (payload or {}).get("deliverable") or row["deliverable"]

            cursor = self.conn.execute(
                """
                UPDATE tasks
                SET status = ?, version = version + 1, active_workspace_tree_sha = ?,
                    terminal_reason = ?, updated_at = ?, deliverable = ?
                WHERE task_id = ? AND version = ?
                """,
                (target.value, new_tree_sha, term_reason, now, new_deliverable, task_id, cur_version),
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
            try:
                self.search_index.update_task_status(task_id, target.value)
            except Exception:
                pass
            return self.get_task(task_id)  # type: ignore[return-value]

    async def transition_task_async(
        self,
        task_id: str,
        next_state: TaskState | str,
        *,
        reason_code: str = "STATE_TRANSITION",
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
                term_reason = terminal_reason if (target.is_terminal or terminal_reason is not None) else row["terminal_reason"]
                new_deliverable = (payload or {}).get("result") or (payload or {}).get("deliverable") or row["deliverable"]

                res = await conn.execute(
                    """
                    UPDATE tasks
                    SET status = $1, version = version + 1, active_workspace_tree_sha = $2,
                        terminal_reason = $3, updated_at = $4, deliverable = $5
                    WHERE task_id = $6 AND version = $7
                    """,
                    target.value,
                    new_tree_sha,
                    term_reason,
                    now,
                    new_deliverable,
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
                try:
                    self.search_index.update_task_status(task_id, target.value)
                except Exception:
                    pass
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

    record_task_event = record_event

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
            d = self._row_to_dict(row)
            if d and d.get("deliverable") and not d.get("result"):
                d["result"] = d["deliverable"]
            return d

    async def get_task_async(self, task_id: str) -> dict[str, Any] | None:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.get_task, task_id)
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM tasks WHERE task_id = $1", task_id)
            d = self._row_to_dict(row)
            if d and d.get("deliverable") and not d.get("result"):
                d["result"] = d["deliverable"]
            return d

    def get_project_task_sequence(self, task_id: str, project_id: str | None = None) -> int:
        """Return 1-based sequential task index for this task within its project.
        Uses deterministic (created_at, task_id) tie-breaking for stability under identical timestamps.
        """
        if self.backend_type == "postgres":
            return self._run_sync(self.get_project_task_sequence_async, task_id, project_id)
        with self._lock:
            row = self.conn.execute("SELECT project_id, created_at FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not row:
                return 1
            proj = project_id or (row[0] if isinstance(row, (list, tuple)) else row["project_id"]) or "default"
            created_at = row[1] if isinstance(row, (list, tuple)) else row["created_at"]
            c_row = self.conn.execute(
                """
                SELECT COUNT(*) FROM tasks 
                WHERE (project_id = ? OR (project_id IS NULL AND ? = 'default'))
                  AND (created_at < ? OR (created_at = ? AND task_id <= ?))
                """,
                (proj, proj, created_at, created_at, task_id),
            ).fetchone()
            seq = int(c_row[0]) if c_row and c_row[0] is not None else 1
            return max(1, seq)

    def count_project_tasks(self, project_id: str) -> int:
        """Return the authoritative task count for a stable project ID."""
        if self.backend_type == "postgres":
            return self._run_sync(self.count_project_tasks_async, project_id)
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE project_id = ?", (project_id,)
            ).fetchone()
            return int(row[0]) if row else 0

    async def count_project_tasks_async(self, project_id: str) -> int:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.count_project_tasks, project_id)
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            count = await conn.fetchval("SELECT COUNT(*) FROM tasks WHERE project_id = $1", project_id)
            return int(count or 0)

    async def get_project_task_sequence_async(self, task_id: str, project_id: str | None = None) -> int:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.get_project_task_sequence, task_id, project_id)
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT project_id, created_at FROM tasks WHERE task_id = $1", task_id)
            if not row:
                return 1
            proj = project_id or row["project_id"] or "default"
            created_at = row["created_at"]
            cnt = await conn.fetchval(
                """
                SELECT COUNT(*) FROM tasks 
                WHERE (project_id = $1 OR (project_id IS NULL AND $1 = 'default'))
                  AND (created_at < $2 OR (created_at = $2 AND task_id <= $3))
                """,
                proj,
                created_at,
                task_id,
            )
            seq = int(cnt) if cnt is not None else 1
            return max(1, seq)

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

    def verify_chain_integrity(self, task_id: str | None = None) -> ChainIntegrityReport:
        """Verifies cryptographic hash chain integrity across all or specific task events.

        Checks:
        1. Sequence continuity (sequence_no 1, 2, ... with no gaps).
        2. Correct previous hash linking (starts with 64 zeroes, links to preceding event_sha256).
        3. Cryptographic hash validity: recomputes SHA-256 over all event metadata and payload.

        Returns:
            ChainIntegrityReport (evaluates to True if valid, False if compromised).
        """
        if self.backend_type == "postgres":
            return self._run_sync(self.verify_chain_integrity_async, task_id)

        with self._lock:
            if task_id:
                rows = self.conn.execute(
                    "SELECT task_id, sequence_no, event_type, prior_state, next_state, "
                    "reason_code, workspace_tree_sha, payload_json, occurred_at, correlation_id, event_sha256 "
                    "FROM task_events WHERE task_id = ? ORDER BY sequence_no ASC",
                    (task_id,),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT task_id, sequence_no, event_type, prior_state, next_state, "
                    "reason_code, workspace_tree_sha, payload_json, occurred_at, correlation_id, event_sha256 "
                    "FROM task_events ORDER BY task_id ASC, sequence_no ASC"
                ).fetchall()

            return self._verify_event_rows(rows)

    async def verify_chain_integrity_async(
        self, task_id: str | None = None
    ) -> ChainIntegrityReport:
        """Asynchronously verifies cryptographic hash chain integrity across task events."""
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.verify_chain_integrity, task_id)

        pool = await self.get_pool()
        async with pool.acquire() as conn:
            if task_id:
                rows = await conn.fetch(
                    "SELECT task_id, sequence_no, event_type, prior_state, next_state, "
                    "reason_code, workspace_tree_sha, payload_json, occurred_at, correlation_id, event_sha256 "
                    "FROM task_events WHERE task_id = $1 ORDER BY sequence_no ASC",
                    task_id,
                )
            else:
                rows = await conn.fetch(
                    "SELECT task_id, sequence_no, event_type, prior_state, next_state, "
                    "reason_code, workspace_tree_sha, payload_json, occurred_at, correlation_id, event_sha256 "
                    "FROM task_events ORDER BY task_id ASC, sequence_no ASC"
                )
            return self._verify_event_rows(rows)

    def _verify_event_rows(self, rows: list[Any]) -> ChainIntegrityReport:
        tasks_seen: set[str] = set()
        verified_events = 0
        errors: list[str] = []
        current_task_id: str | None = None
        prev_hash = "0" * 64
        expected_seq = 1

        for r in rows:
            t_id = str(r["task_id"])
            seq_no = int(r["sequence_no"])
            stored_hash = str(r["event_sha256"])

            if t_id != current_task_id:
                current_task_id = t_id
                tasks_seen.add(t_id)
                prev_hash = "0" * 64
                expected_seq = 1

            if seq_no != expected_seq:
                errors.append(
                    f"Task {t_id}: sequence discontinuity (expected {expected_seq}, found {seq_no})"
                )

            payload_raw = r["payload_json"]
            payload_str = payload_raw if isinstance(payload_raw, str) else canonical_json(payload_raw)

            recomputed_hash = compute_event_hash(
                prev_hash=prev_hash,
                task_id=t_id,
                seq_no=seq_no,
                event_type=str(r["event_type"]),
                prior_state=r["prior_state"],
                next_state=r["next_state"],
                reason_code=str(r["reason_code"]),
                workspace_tree_sha=r["workspace_tree_sha"],
                payload_str=payload_str,
                occurred_at=str(r["occurred_at"]),
            )

            if recomputed_hash != stored_hash:
                errors.append(
                    f"Task {t_id} event seq {seq_no}: hash mismatch "
                    f"(computed {recomputed_hash}, stored {stored_hash})"
                )

            prev_hash = stored_hash
            expected_seq = seq_no + 1
            verified_events += 1

        return ChainIntegrityReport({
            "valid": len(errors) == 0,
            "tasks_checked": len(tasks_seen),
            "verified_events": verified_events,
            "errors": errors,
        })

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

    def get_worker(self, worker_id: str) -> dict[str, Any] | None:
        if self.backend_type == "postgres":
            return self._run_sync(self.get_worker_async, worker_id)
        with self._lock:
            row = self.conn.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()
            return self._row_to_dict(row)

    async def get_worker_async(self, worker_id: str) -> dict[str, Any] | None:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.get_worker, worker_id)
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM workers WHERE worker_id = $1", worker_id)
            return self._row_to_dict(row)

    def heartbeat_worker(self, worker_id: str, health: str = "HEALTHY") -> dict[str, Any]:
        if self.backend_type == "postgres":
            return self._run_sync(self.heartbeat_worker_async, worker_id, health=health)
        with self._lock:
            now = utcnow_iso()
            self.conn.execute(
                """
                INSERT INTO workers (
                    worker_id, machine_class, capabilities_json, cow_backend,
                    local_llm_models_json, trust_level, health, last_heartbeat_at, registered_at
                ) VALUES (?, 'worker', '{}', 'apfs_clone', '[]', 'trusted_local', ?, ?, ?)
                ON CONFLICT(worker_id) DO UPDATE SET
                    health=excluded.health,
                    last_heartbeat_at=excluded.last_heartbeat_at
                """,
                (worker_id, health, now, now),
            )
            self.conn.commit()
            row = self.conn.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()
            return self._row_to_dict(row)  # type: ignore[return-value]

    async def heartbeat_worker_async(self, worker_id: str, health: str = "HEALTHY") -> dict[str, Any]:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.heartbeat_worker, worker_id, health=health)
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            now = utcnow_iso()
            await conn.execute(
                """
                INSERT INTO workers (
                    worker_id, machine_class, capabilities_json, cow_backend,
                    local_llm_models_json, trust_level, health, last_heartbeat_at, registered_at
                ) VALUES ($1, 'worker', '{}', 'apfs_clone', '[]', 'trusted_local', $2, $3, $4)
                ON CONFLICT(worker_id) DO UPDATE SET
                    health=excluded.health,
                    last_heartbeat_at=excluded.last_heartbeat_at
                """,
                worker_id,
                health,
                now,
                now,
            )
            row = await conn.fetchrow("SELECT * FROM workers WHERE worker_id = $1", worker_id)
            return self._row_to_dict(row)  # type: ignore[return-value]

    def list_skills(self) -> list[dict[str, Any]]:
        if self.backend_type == "postgres":
            return self._run_sync(self.list_skills_async)
        with self._lock:
            rows = self.conn.execute("SELECT * FROM skills ORDER BY name ASC").fetchall()
            results = []
            for r in rows:
                if r is None:
                    continue
                d = self._row_to_dict(r)
                if d and d.get("latest_scan_id"):
                    scan_row = self.conn.execute(
                        "SELECT * FROM skill_scans WHERE scan_id = ?",
                        (d["latest_scan_id"],),
                    ).fetchone()
                    if scan_row:
                        d["scan"] = self._row_to_dict(scan_row)
                results.append(d)
            return results

    async def list_skills_async(self) -> list[dict[str, Any]]:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.list_skills)
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM skills ORDER BY name ASC")
            results = []
            for r in rows:
                if r is None:
                    continue
                d = self._row_to_dict(r)
                if d and d.get("latest_scan_id"):
                    scan_row = await conn.fetchrow(
                        "SELECT * FROM skill_scans WHERE scan_id = $1",
                        d["latest_scan_id"],
                    )
                    if scan_row:
                        d["scan"] = self._row_to_dict(scan_row)
                results.append(d)
            return results

    def set_skill_status(self, skill_id: str, status: str) -> int:
        """Set the lifecycle status of every revision of a skill; returns rows updated."""
        if self.backend_type == "postgres":
            return self._run_sync(self.set_skill_status_async, skill_id, status)
        with self._lock:
            cursor = self.conn.execute(
                "UPDATE skills SET status = ? WHERE skill_id = ?",
                (status, skill_id),
            )
            self.conn.commit()
            return cursor.rowcount

    async def set_skill_status_async(self, skill_id: str, status: str) -> int:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.set_skill_status, skill_id, status)
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE skills SET status = $1 WHERE skill_id = $2",
                status,
                skill_id,
            )
            # asyncpg returns an "UPDATE <n>" status tag.
            try:
                return int(result.split()[-1])
            except (ValueError, IndexError):
                return 0

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
                INSERT INTO workers (
                    worker_id, machine_class, capabilities_json, cow_backend,
                    local_llm_models_json, trust_level, health, last_heartbeat_at, registered_at
                ) VALUES (?, 'worker', '{}', 'apfs_clone', '[]', 'trusted_local', 'HEALTHY', ?, ?)
                ON CONFLICT(worker_id) DO UPDATE SET last_heartbeat_at=excluded.last_heartbeat_at
                """,
                (worker_id, now, now),
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
                    INSERT INTO workers (
                        worker_id, machine_class, capabilities_json, cow_backend,
                        local_llm_models_json, trust_level, health, last_heartbeat_at, registered_at
                    ) VALUES ($1, 'worker', '{}', 'apfs_clone', '[]', 'trusted_local', 'HEALTHY', $2, $3)
                    ON CONFLICT(worker_id) DO UPDATE SET last_heartbeat_at=excluded.last_heartbeat_at
                    """,
                    worker_id,
                    now,
                    now,
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

    def reap_expired_leases(self, now: str | None = None) -> list[str]:
        """Finds all tasks in RUNNING/VERIFYING with expired unreleased leases, releases them, and transitions tasks back to QUEUED."""
        if self.backend_type == "postgres":
            return self._run_sync(self.reap_expired_leases_async, now)
        with self._lock:
            now_str = now or utcnow_iso()
            rows = self.conn.execute(
                """
                SELECT DISTINCT t.task_id, t.version, t.active_workspace_tree_sha, t.policy_snapshot_id, t.status
                FROM tasks t
                JOIN leases l ON t.task_id = l.task_id
                WHERE t.status IN ('RUNNING', 'VERIFYING')
                  AND l.released_at IS NULL
                  AND l.expires_at < ?
                """,
                (now_str,),
            ).fetchall()

            reaped_task_ids: list[str] = []
            for r in rows:
                tid = r["task_id"]
                ver = int(r["version"])
                prior_st = r["status"]
                tree_sha = r["active_workspace_tree_sha"]
                policy_id = r["policy_snapshot_id"]

                # Release expired lease(s)
                self.conn.execute(
                    "UPDATE leases SET released_at = ? WHERE task_id = ? AND released_at IS NULL AND expires_at < ?",
                    (now_str, tid, now_str),
                )
                cursor = self.conn.execute(
                    """
                    UPDATE tasks
                    SET status = 'QUEUED', version = version + 1, updated_at = ?
                    WHERE task_id = ? AND version = ?
                    """,
                    (now_str, tid, ver),
                )
                if cursor.rowcount == 1:
                    reaped_task_ids.append(tid)
                    self._append_event_locked(
                        task_id=tid,
                        attempt_id=None,
                        event_type="STATE_QUEUED",
                        prior_state=prior_st,
                        next_state="QUEUED",
                        reason_code="LEASE_EXPIRED",
                        workspace_tree_sha=tree_sha,
                        policy_version=policy_id,
                        payload={"expired_at": now_str},
                        actor="system",
                    )
            self.conn.commit()
            return reaped_task_ids

    async def reap_expired_leases_async(self, now: str | None = None) -> list[str]:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.reap_expired_leases, now)
        pool = await self.get_pool()
        now_str = now or utcnow_iso()
        async with pool.acquire() as conn:
            async with conn.transaction():
                if not hasattr(conn, "fetch"):
                    return []
                rows = await conn.fetch(
                    """
                    SELECT DISTINCT t.task_id, t.version, t.active_workspace_tree_sha, t.policy_snapshot_id, t.status
                    FROM tasks t
                    JOIN leases l ON t.task_id = l.task_id
                    WHERE t.status IN ('RUNNING', 'VERIFYING')
                      AND l.released_at IS NULL
                      AND l.expires_at < $1
                    FOR UPDATE OF t
                    """,
                    now_str,
                )
                reaped_task_ids: list[str] = []
                for r in rows:
                    tid = r["task_id"]
                    ver = int(r["version"])
                    prior_st = r["status"]
                    tree_sha = r["active_workspace_tree_sha"]
                    policy_id = r["policy_snapshot_id"]

                    await conn.execute(
                        "UPDATE leases SET released_at = $1 WHERE task_id = $2 AND released_at IS NULL AND expires_at < $1",
                        now_str,
                        tid,
                    )
                    res = await conn.execute(
                        """
                        UPDATE tasks
                        SET status = 'QUEUED', version = version + 1, updated_at = $1
                        WHERE task_id = $2 AND version = $3
                        """,
                        now_str,
                        tid,
                        ver,
                    )
                    if res == "UPDATE 1":
                        reaped_task_ids.append(tid)
                        await self._append_event_async_conn(
                            conn=conn,
                            task_id=tid,
                            attempt_id=None,
                            event_type="STATE_QUEUED",
                            prior_state=prior_st,
                            next_state="QUEUED",
                            reason_code="LEASE_EXPIRED",
                            workspace_tree_sha=tree_sha,
                            policy_version=policy_id,
                            payload={"expired_at": now_str},
                            actor="system",
                        )
                return reaped_task_ids

    def claim_next_task(
        self,
        worker_id: str = "worker-default",
        *,
        base_tree_sha: str | None = None,
        workspace_path: str | None = None,
        ttl_seconds: int = 300,
    ) -> dict[str, Any] | None:
        """Atomically claim the highest priority QUEUED task using SKIP LOCKED (PG) or BEGIN IMMEDIATE (SQLite)."""
        if self.backend_type == "postgres":
            return self._run_sync(
                self.claim_next_task_async,
                worker_id,
                base_tree_sha=base_tree_sha,
                workspace_path=workspace_path,
                ttl_seconds=ttl_seconds,
            )
        self.reap_expired_leases()
        with self._lock:
            # B42: a busy database must not fall through to an unprotected
            # claim; retry briefly, then fail visibly.
            begin_error: sqlite3.OperationalError | None = None
            for begin_attempt in range(5):
                try:
                    self.conn.execute("BEGIN IMMEDIATE")
                    begin_error = None
                    break
                except sqlite3.OperationalError as exc:
                    begin_error = exc
                    if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                        break
                    time.sleep(0.02 * (begin_attempt + 1))
            if begin_error is not None:
                raise ClaimTransactionError(
                    f"Could not start the claim transaction: {begin_error}"
                ) from begin_error

            try:
                row = self.conn.execute(self.CLAIM_TASK_SQLITE_SQL).fetchone()
                if not row:
                    self.conn.commit()
                    return None

                task_id = row["task_id"]
                cur_version = int(row["version"])
                policy_id = row["policy_snapshot_id"]
                active_sha = row["active_workspace_tree_sha"]

                max_tok_row = self.conn.execute(
                    "SELECT COALESCE(MAX(fencing_token), 0) AS max_tok FROM leases WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                max_tok = int(max_tok_row["max_tok"]) if max_tok_row else 0
                next_token = max_tok + 1
                now_dt = datetime.now(timezone.utc)
                now = now_dt.isoformat()
                expires = (now_dt + timedelta(seconds=ttl_seconds)).isoformat()

                # Expire any prior unreleased leases for this task
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
                        None,
                        None,
                        worker_id,
                        lease_id,
                        next_token,
                        base_tree_sha,
                        base_tree_sha,
                        workspace_path,
                        now,
                        0,
                    ),
                )

                self.conn.execute(
                    """
                    INSERT INTO workers (
                        worker_id, machine_class, capabilities_json, cow_backend,
                        local_llm_models_json, trust_level, health, last_heartbeat_at, registered_at
                    ) VALUES (?, 'worker', '{}', 'apfs_clone', '[]', 'trusted_local', 'HEALTHY', ?, ?)
                    ON CONFLICT(worker_id) DO UPDATE SET last_heartbeat_at=excluded.last_heartbeat_at
                    """,
                    (worker_id, now, now),
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

                new_tree_sha = base_tree_sha or active_sha
                cursor = self.conn.execute(
                    """
                    UPDATE tasks
                    SET status = 'RUNNING', version = version + 1, active_workspace_tree_sha = ?, updated_at = ?
                    WHERE task_id = ? AND version = ?
                    """,
                    (new_tree_sha, now, task_id, cur_version),
                )
                if cursor.rowcount != 1:
                    raise ConcurrencyFenceError(f"Concurrent update race detected on task {task_id}")

                if max_tok > 0:
                    self._append_event_locked(
                        task_id=task_id,
                        attempt_id=attempt_id,
                        event_type="LEASE_TAKEOVER",
                        prior_state="QUEUED",
                        next_state="RUNNING",
                        reason_code="LEASE_TAKEOVER",
                        workspace_tree_sha=new_tree_sha,
                        policy_version=policy_id,
                        payload={"prior_fencing_token": max_tok, "new_fencing_token": next_token, "worker_id": worker_id, "lease_id": lease_id},
                        actor=worker_id,
                    )

                self._append_event_locked(
                    task_id=task_id,
                    attempt_id=attempt_id,
                    event_type="STATE_RUNNING",
                    prior_state="QUEUED",
                    next_state="RUNNING",
                    reason_code="QUEUE_CLAIMED",
                    workspace_tree_sha=new_tree_sha,
                    policy_version=policy_id,
                    payload={"worker_id": worker_id, "fencing_token": next_token, "lease_id": lease_id},
                    actor=worker_id,
                )

                self.conn.commit()
                claimed_task = self.get_task(task_id) or {}
                claimed_task["attempt_id"] = attempt_id
                claimed_task["lease_id"] = lease_id
                claimed_task["fencing_token"] = next_token
                claimed_task["worker_id"] = worker_id
                claimed_task["expires_at"] = expires
                return claimed_task
            except Exception:
                self.conn.rollback()
                raise

    async def claim_next_task_async(
        self,
        worker_id: str = "worker-default",
        *,
        base_tree_sha: str | None = None,
        workspace_path: str | None = None,
        ttl_seconds: int = 300,
    ) -> dict[str, Any] | None:
        """Atomically claim the highest priority QUEUED task using SELECT ... FOR UPDATE SKIP LOCKED."""
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(
                self.claim_next_task,
                worker_id,
                base_tree_sha=base_tree_sha,
                workspace_path=workspace_path,
                ttl_seconds=ttl_seconds,
            )
        await self.reap_expired_leases_async()
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(self.CLAIM_TASK_POSTGRES_SQL)
                if not row:
                    return None

                task_id = row["task_id"]
                cur_version = int(row["version"])
                policy_id = row["policy_snapshot_id"]
                active_sha = row["active_workspace_tree_sha"]

                max_tok_row = await conn.fetchrow(
                    "SELECT COALESCE(MAX(fencing_token), 0) AS max_tok FROM leases WHERE task_id = $1",
                    task_id,
                )
                max_tok = int(max_tok_row["max_tok"]) if max_tok_row else 0
                next_token = max_tok + 1
                now_dt = datetime.now(timezone.utc)
                now = now_dt.isoformat()
                expires = (now_dt + timedelta(seconds=ttl_seconds)).isoformat()

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
                    None,
                    None,
                    worker_id,
                    lease_id,
                    next_token,
                    base_tree_sha,
                    base_tree_sha,
                    workspace_path,
                    now,
                    0,
                )

                await conn.execute(
                    """
                    INSERT INTO workers (
                        worker_id, machine_class, capabilities_json, cow_backend,
                        local_llm_models_json, trust_level, health, last_heartbeat_at, registered_at
                    ) VALUES ($1, 'worker', '{}', 'apfs_clone', '[]', 'trusted_local', 'HEALTHY', $2, $3)
                    ON CONFLICT(worker_id) DO UPDATE SET last_heartbeat_at=excluded.last_heartbeat_at
                    """,
                    worker_id,
                    now,
                    now,
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

                new_tree_sha = base_tree_sha or active_sha
                res = await conn.execute(
                    """
                    UPDATE tasks
                    SET status = 'RUNNING', version = version + 1, active_workspace_tree_sha = $1, updated_at = $2
                    WHERE task_id = $3 AND version = $4
                    """,
                    new_tree_sha,
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

                if max_tok > 0:
                    await self._append_event_async_conn(
                        conn=conn,
                        task_id=task_id,
                        attempt_id=attempt_id,
                        event_type="LEASE_TAKEOVER",
                        prior_state="QUEUED",
                        next_state="RUNNING",
                        reason_code="LEASE_TAKEOVER",
                        workspace_tree_sha=new_tree_sha,
                        policy_version=policy_id,
                        payload={"prior_fencing_token": max_tok, "new_fencing_token": next_token, "worker_id": worker_id, "lease_id": lease_id},
                        actor=worker_id,
                    )

                await self._append_event_async_conn(
                    conn=conn,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    event_type="STATE_RUNNING",
                    prior_state="QUEUED",
                    next_state="RUNNING",
                    reason_code="QUEUE_CLAIMED",
                    workspace_tree_sha=new_tree_sha,
                    policy_version=policy_id,
                    payload={"worker_id": worker_id, "fencing_token": next_token, "lease_id": lease_id},
                    actor=worker_id,
                )

                updated = await conn.fetchrow("SELECT * FROM tasks WHERE task_id = $1", task_id)
                claimed_task = self._row_to_dict(updated) or {}
                claimed_task["attempt_id"] = attempt_id
                claimed_task["lease_id"] = lease_id
                claimed_task["fencing_token"] = next_token
                claimed_task["worker_id"] = worker_id
                claimed_task["expires_at"] = expires
                return claimed_task

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

    def get_lease(self, lease_id: str) -> dict[str, Any] | None:
        if self.backend_type == "postgres":
            return self._run_sync(self.get_lease_async, lease_id)
        with self._lock:
            row = self.conn.execute("SELECT * FROM leases WHERE lease_id = ?", (lease_id,)).fetchone()
            return self._row_to_dict(row)

    async def get_lease_async(self, lease_id: str) -> dict[str, Any] | None:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.get_lease, lease_id)
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM leases WHERE lease_id = $1", lease_id)
            return self._row_to_dict(row)

    def renew_lease(self, lease_id: str, fencing_token: int, ttl_seconds: int = 300) -> dict[str, Any]:
        if self.backend_type == "postgres":
            return self._run_sync(self.renew_lease_async, lease_id, fencing_token, ttl_seconds=ttl_seconds)
        with self._lock:
            row = self.conn.execute("SELECT * FROM leases WHERE lease_id = ?", (lease_id,)).fetchone()
            if not row:
                raise KeyError(f"Lease not found: {lease_id}")
            task_id = row["task_id"]
            max_tok_row = self.conn.execute(
                "SELECT COALESCE(MAX(fencing_token), 0) AS max_tok FROM leases WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            max_tok = int(max_tok_row["max_tok"]) if max_tok_row else 0
            if int(row["fencing_token"]) != fencing_token or fencing_token < max_tok or row["released_at"] is not None:
                raise ConcurrencyFenceError(
                    f"Stale or mismatched fencing token {fencing_token} (lease token: {row['fencing_token']}, max: {max_tok}, released: {row['released_at'] is not None}) for task {task_id}"
                )
            now_dt = datetime.now(timezone.utc)
            now = now_dt.isoformat()
            cur_exp_dt = datetime.fromisoformat(str(row["expires_at"]))
            target_exp = now_dt + timedelta(seconds=ttl_seconds)
            expires_dt = target_exp if target_exp > cur_exp_dt else (cur_exp_dt + timedelta(seconds=1))
            expires = expires_dt.isoformat()
            self.conn.execute(
                "UPDATE leases SET expires_at = ?, renewed_at = ? WHERE lease_id = ? AND released_at IS NULL",
                (expires, now, lease_id),
            )
            worker_id = row["worker_id"]
            self.conn.execute(
                "UPDATE workers SET last_heartbeat_at = ? WHERE worker_id = ?",
                (now, worker_id),
            )
            self.conn.commit()
            updated = self.conn.execute("SELECT * FROM leases WHERE lease_id = ?", (lease_id,)).fetchone()
            return self._row_to_dict(updated)  # type: ignore[return-value]

    async def renew_lease_async(self, lease_id: str, fencing_token: int, ttl_seconds: int = 300) -> dict[str, Any]:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.renew_lease, lease_id, fencing_token, ttl_seconds=ttl_seconds)
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("SELECT * FROM leases WHERE lease_id = $1", lease_id)
                if not row:
                    raise KeyError(f"Lease not found: {lease_id}")
                task_id = row["task_id"]
                max_tok_row = await conn.fetchrow(
                    "SELECT COALESCE(MAX(fencing_token), 0) AS max_tok FROM leases WHERE task_id = $1",
                    task_id,
                )
                max_tok = int(max_tok_row["max_tok"]) if max_tok_row else 0
                if int(row["fencing_token"]) != fencing_token or fencing_token < max_tok or row["released_at"] is not None:
                    raise ConcurrencyFenceError(
                        f"Stale or mismatched fencing token {fencing_token} (lease token: {row['fencing_token']}, max: {max_tok}, released: {row['released_at'] is not None}) for task {task_id}"
                    )
                now_dt = datetime.now(timezone.utc)
                now = now_dt.isoformat()
                cur_exp_dt = datetime.fromisoformat(str(row["expires_at"]))
                target_exp = now_dt + timedelta(seconds=ttl_seconds)
                expires_dt = target_exp if target_exp > cur_exp_dt else (cur_exp_dt + timedelta(seconds=1))
                expires = expires_dt.isoformat()
                await conn.execute(
                    "UPDATE leases SET expires_at = $1, renewed_at = $2 WHERE lease_id = $3 AND released_at IS NULL",
                    expires,
                    now,
                    lease_id,
                )
                worker_id = row["worker_id"]
                await conn.execute(
                    "UPDATE workers SET last_heartbeat_at = $1 WHERE worker_id = $2",
                    now,
                    worker_id,
                )
                updated = await conn.fetchrow("SELECT * FROM leases WHERE lease_id = $1", lease_id)
                return self._row_to_dict(updated)  # type: ignore[return-value]

    def release_lease(self, lease_id: str, fencing_token: int | None = None) -> dict[str, Any]:
        if self.backend_type == "postgres":
            return self._run_sync(self.release_lease_async, lease_id, fencing_token=fencing_token)
        with self._lock:
            row = self.conn.execute("SELECT * FROM leases WHERE lease_id = ?", (lease_id,)).fetchone()
            if not row:
                raise KeyError(f"Lease not found: {lease_id}")
            task_id = row["task_id"]
            max_tok_row = self.conn.execute(
                "SELECT COALESCE(MAX(fencing_token), 0) AS max_tok FROM leases WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            max_tok = int(max_tok_row["max_tok"]) if max_tok_row else 0
            if fencing_token is not None and (int(row["fencing_token"]) != fencing_token or fencing_token < max_tok):
                raise ConcurrencyFenceError(
                    f"Fencing token mismatch or stale on release: provided {fencing_token}, max {max_tok}, lease has {row['fencing_token']}"
                )
            now = utcnow_iso()
            self.conn.execute(
                "UPDATE leases SET released_at = ? WHERE lease_id = ? AND released_at IS NULL",
                (now, lease_id),
            )
            self.conn.commit()
            updated = self.conn.execute("SELECT * FROM leases WHERE lease_id = ?", (lease_id,)).fetchone()
            return self._row_to_dict(updated)  # type: ignore[return-value]

    async def release_lease_async(self, lease_id: str, fencing_token: int | None = None) -> dict[str, Any]:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.release_lease, lease_id, fencing_token=fencing_token)
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM leases WHERE lease_id = $1", lease_id)
            if not row:
                raise KeyError(f"Lease not found: {lease_id}")
            task_id = row["task_id"]
            max_tok_row = await conn.fetchrow(
                "SELECT COALESCE(MAX(fencing_token), 0) AS max_tok FROM leases WHERE task_id = $1",
                task_id,
            )
            max_tok = int(max_tok_row["max_tok"]) if max_tok_row else 0
            if fencing_token is not None and (int(row["fencing_token"]) != fencing_token or fencing_token < max_tok):
                raise ConcurrencyFenceError(
                    f"Fencing token mismatch or stale on release: provided {fencing_token}, max {max_tok}, lease has {row['fencing_token']}"
                )
            now = utcnow_iso()
            await conn.execute(
                "UPDATE leases SET released_at = $1 WHERE lease_id = $2 AND released_at IS NULL",
                now,
                lease_id,
            )
            updated = await conn.fetchrow("SELECT * FROM leases WHERE lease_id = $1", lease_id)
            return self._row_to_dict(updated)  # type: ignore[return-value]

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
        validate_dag_topology(dag_nodes)
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
            serialized_nodes = [
                n.to_dict() if hasattr(n, "to_dict") else dict(n)
                for n in dag_nodes
            ]

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
                    canonical_json(serialized_nodes),
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
        validate_dag_topology(dag_nodes)
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
                serialized_nodes = [
                    n.to_dict() if hasattr(n, "to_dict") else dict(n)
                    for n in dag_nodes
                ]

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
                    canonical_json(serialized_nodes),
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

    def get_plan_revision(self, task_id: str, revision: int) -> dict[str, Any] | None:
        if self.backend_type == "postgres":
            return self._run_sync(self.get_plan_revision_async, task_id, revision)
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM plans WHERE task_id = ? AND revision = ?",
                (task_id, revision),
            ).fetchone()
            return self._row_to_dict(row)

    async def get_plan_revision_async(self, task_id: str, revision: int) -> dict[str, Any] | None:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.get_plan_revision, task_id, revision)
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM plans WHERE task_id = $1 AND revision = $2",
                task_id,
                revision,
            )
            return self._row_to_dict(row)

    def list_plans(self, task_id: str) -> list[dict[str, Any]]:
        if self.backend_type == "postgres":
            return self._run_sync(self.list_plans_async, task_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM plans WHERE task_id = ? ORDER BY revision ASC",
                (task_id,),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]

    async def list_plans_async(self, task_id: str) -> list[dict[str, Any]]:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.list_plans, task_id)
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM plans WHERE task_id = $1 ORDER BY revision ASC",
                task_id,
            )
            return [self._row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]

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

    def record_tool_call(
        self,
        call_id: str | None = None,
        task_id: str = "",
        tool_name: str = "",
        risk_class: str = "R1",
        args: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        """Convenience method for recording / beginning a tool call."""
        cid = call_id or f"cal_{uuid.uuid4().hex[:12]}"
        now = utcnow_iso()
        attempt_id = kwargs.get("attempt_id", "att_1")
        step_id = kwargs.get("step_id", "stp_1")
        seq = kwargs.get("sequence_in_step", 1)
        args_dict = args or {}
        policy_decision = kwargs.get("policy_decision", "ALLOW")
        idempotency_key = kwargs.get("idempotency_key", f"idem_{cid}")
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO tool_calls (
                    call_id, task_id, attempt_id, step_id, sequence_in_step,
                    tool_id, risk_class, canonical_args_hash, arguments_redacted_json,
                    replayed_from_call_id, policy_decision, idempotency_key,
                    started_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, 'PENDING')
                """,
                (
                    cid,
                    task_id,
                    attempt_id,
                    step_id,
                    seq,
                    tool_name,
                    risk_class,
                    sha256_hex(canonical_json(args_dict)),
                    canonical_json(args_dict),
                    policy_decision,
                    idempotency_key,
                    now,
                ),
            )
            self.conn.commit()
            return cid

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
        fencing_token: int | None = None,
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
                fencing_token=fencing_token,
            )
        with self._lock:
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
        fencing_token: int | None = None,
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
                fencing_token=fencing_token,
            )
        pool = await self.get_pool()
        async with pool.acquire() as conn:
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
        fencing_token: int | None = None,
    ) -> dict[str, Any]:
        if self.backend_type == "postgres":
            return self._run_sync(
                self.seal_tool_call_async,
                call_id,
                result,
                status=status,
                result_ref=result_ref,
                fencing_token=fencing_token,
            )
        with self._lock:
            if fencing_token is not None:
                tc_row = self.conn.execute("SELECT task_id FROM tool_calls WHERE call_id = ?", (call_id,)).fetchone()
                if tc_row:
                    t_id = tc_row["task_id"]
                    max_tok_row = self.conn.execute(
                        "SELECT COALESCE(MAX(fencing_token), 0) AS max_tok FROM leases WHERE task_id = ?",
                        (t_id,),
                    ).fetchone()
                    max_tok = int(max_tok_row["max_tok"]) if max_tok_row else 0
                    if fencing_token < max_tok:
                        raise ConcurrencyFenceError(
                            f"Stale fencing token {fencing_token} < active token {max_tok} for task {t_id}"
                        )
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
            try:
                if row and row["task_id"]:
                    t_name = row["tool_id"] if "tool_id" in row.keys() else (row["tool_name"] if "tool_name" in row.keys() else "")
                    a_text = str(row["arguments_redacted_json"] if "arguments_redacted_json" in row.keys() else (row["args_json"] if "args_json" in row.keys() else ""))
                    self.search_index.append_tool_call(
                        task_id=row["task_id"],
                        tool_name=t_name,
                        args_text=a_text,
                        result_text=json.dumps(result) if isinstance(result, (dict, list)) else str(result),
                    )
            except Exception:
                pass
            return self._row_to_dict(row)  # type: ignore[return-value]

    async def seal_tool_call_async(
        self,
        call_id: str,
        result: dict[str, Any],
        *,
        status: str = "SEALED",
        result_ref: str | None = None,
        fencing_token: int | None = None,
    ) -> dict[str, Any]:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(
                self.seal_tool_call,
                call_id,
                result,
                status=status,
                result_ref=result_ref,
                fencing_token=fencing_token,
            )
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            if fencing_token is not None:
                tc_row = await conn.fetchrow("SELECT task_id FROM tool_calls WHERE call_id = $1", call_id)
                if tc_row:
                    t_id = tc_row["task_id"]
                    max_tok_row = await conn.fetchrow(
                        "SELECT COALESCE(MAX(fencing_token), 0) AS max_tok FROM leases WHERE task_id = $1",
                        t_id,
                    )
                    max_tok = int(max_tok_row["max_tok"]) if max_tok_row else 0
                    if fencing_token < max_tok:
                        raise ConcurrencyFenceError(
                            f"Stale fencing token {fencing_token} < active token {max_tok} for task {t_id}"
                        )
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
            try:
                if row and row["task_id"]:
                    t_name = row["tool_id"] if "tool_id" in row else (row["tool_name"] if "tool_name" in row else "")
                    a_text = str(row["arguments_redacted_json"] if "arguments_redacted_json" in row else (row["args_json"] if "args_json" in row else ""))
                    self.search_index.append_tool_call(
                        task_id=row["task_id"],
                        tool_name=t_name,
                        args_text=a_text,
                        result_text=json.dumps(result) if isinstance(result, (dict, list)) else str(result),
                    )
            except Exception:
                pass
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

    @staticmethod
    def _tool_result_page_bounds(offset: int, limit: int) -> tuple[int, int]:
        if type(offset) is not int or not 0 <= offset < 2**31 - 1:
            raise ValueError("offset must be an integer between 0 and 2147483646")
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit must be a positive integer")
        return offset, min(limit, 4096)

    @staticmethod
    def _tool_result_page(row: Any, offset: int) -> dict[str, Any]:
        if row is None:
            raise KeyError("Tool result not found for this task and call")
        content, total = row["content"], int(row["total_chars"])
        page: dict[str, Any] = {"content": content, "totalChars": total}
        next_offset = offset + len(content)
        if next_offset < total:
            page["nextOffset"] = next_offset
        return page

    def get_tool_result_page(
        self, task_id: str, call_id: str, offset: int = 0, limit: int = 4096
    ) -> dict[str, Any]:
        """Read raw stored JSON by character, without loading/parsing a result.

        Call and task must both match. result_ref is deliberately never opened.
        Session authorization belongs to the calling surface.
        """
        offset, limit = self._tool_result_page_bounds(offset, limit)
        if self.backend_type == "postgres":
            return self._run_sync(self.get_tool_result_page_async, task_id, call_id, offset, limit)
        with self._lock:
            row = self.conn.execute(
                "SELECT substr(result_json, ?, ?) AS content, length(result_json) AS total_chars "
                "FROM tool_calls WHERE task_id = ? AND call_id = ? AND result_json IS NOT NULL",
                (offset + 1, limit, task_id, call_id),
            ).fetchone()
        return self._tool_result_page(row, offset)

    async def get_tool_result_page_async(
        self, task_id: str, call_id: str, offset: int = 0, limit: int = 4096
    ) -> dict[str, Any]:
        offset, limit = self._tool_result_page_bounds(offset, limit)
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.get_tool_result_page, task_id, call_id, offset, limit)
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT substr(result_json::text, $1::integer, $2::integer) AS content, "
                "length(result_json::text) AS total_chars FROM tool_calls "
                "WHERE task_id = $3 AND call_id = $4 AND result_json IS NOT NULL",
                offset + 1, limit, task_id, call_id,
            )
        return self._tool_result_page(row, offset)

    def get_call_journal(self, task_id: str) -> list[dict[str, Any]]:
        return self.list_tool_calls(task_id)

    async def get_call_journal_async(self, task_id: str) -> list[dict[str, Any]]:
        return await self.list_tool_calls_async(task_id)

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
        risk_class: str | None = None,
        **extra: Any,
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
                risk_class=risk_class,
                **extra,
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
            d = self._row_to_dict(row)  # type: ignore[return-value]
            if d is not None:
                if risk_class:
                    d["risk_class"] = risk_class
                elif "risk_class" in extra:
                    d["risk_class"] = extra["risk_class"]
            return d

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
        risk_class: str | None = None,
        **extra: Any,
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
                risk_class=risk_class,
                **extra,
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
            d = self._row_to_dict(row)  # type: ignore[return-value]
            if d is not None:
                if risk_class:
                    d["risk_class"] = risk_class
                elif "risk_class" in extra:
                    d["risk_class"] = extra["risk_class"]
            return d

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        if self.backend_type == "postgres":
            return self._run_sync(self.get_approval_async, approval_id)
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
            return self._row_to_dict(row)

    async def get_approval_async(self, approval_id: str) -> dict[str, Any] | None:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.get_approval, approval_id)
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM approvals WHERE approval_id = $1", approval_id)
            return self._row_to_dict(row)

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

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        if self.backend_type == "postgres":
            return self._run_sync(self.get_artifact_async, artifact_id)
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
            return self._row_to_dict(row)

    async def get_artifact_async(self, artifact_id: str) -> dict[str, Any] | None:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.get_artifact, artifact_id)
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM artifacts WHERE artifact_id = $1", artifact_id
            )
            return self._row_to_dict(row)

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
            ledger_signature = f"integrity-sha256:{sha256_hex(signature_material)}"

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
            ledger_signature = f"integrity-sha256:{sha256_hex(signature_material)}"

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

    def save_signed_proof(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Import an exact peer-signed proof idempotently without regenerating its identity."""
        from harness_core.capabilities.proof import SignedProofArtifact

        signed = SignedProofArtifact.from_dict(payload)
        if self.backend_type == "postgres":
            return self._run_sync(self.save_signed_proof_async, signed.to_dict())
        proof = signed.proof
        values = (
            proof["proof_id"], proof["task_id"], proof["attempt_id"],
            proof["git_diff_sha"], proof["git_diff_text"], proof["workspace_tree_sha"],
            proof["runtime_digest"], canonical_json(proof["ast_impact"]),
            canonical_json(proof["verification_results"]), signed.signature,
            proof["created_at"], canonical_json(signed.to_dict()),
        )
        with self._lock:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO proof_bundles (
                        proof_id, task_id, attempt_id, git_diff_sha, git_diff_text,
                        workspace_tree_sha, runtime_digest, ast_impact_json,
                        verification_results_json, ledger_signature, created_at,
                        signed_proof_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(proof_id) DO NOTHING
                    """,
                    values,
                )
                row = self.conn.execute(
                    "SELECT * FROM proof_bundles WHERE proof_id = ?", (proof["proof_id"],)
                ).fetchone()
            current = self._row_to_dict(row)
            assert current is not None
            current.pop("signed_proof", None)
            current.pop("ast_impact_json", None)
            current.pop("verification_results_json", None)
            expected = {
                **proof,
                "ledger_signature": signed.signature,
                "signed_proof_json": canonical_json(signed.to_dict()),
            }
            if current != expected:
                raise ValueError("Conflicting data uses an existing signed proof id.")
            return current

    async def save_signed_proof_async(self, payload: dict[str, Any]) -> dict[str, Any]:
        from harness_core.capabilities.proof import SignedProofArtifact

        signed = SignedProofArtifact.from_dict(payload)
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.save_signed_proof, signed.to_dict())
        proof = signed.proof
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO proof_bundles (
                        proof_id, task_id, attempt_id, git_diff_sha, git_diff_text,
                        workspace_tree_sha, runtime_digest, ast_impact_json,
                        verification_results_json, ledger_signature, created_at,
                        signed_proof_json
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                    ON CONFLICT (proof_id) DO NOTHING
                    """,
                    proof["proof_id"], proof["task_id"], proof["attempt_id"],
                    proof["git_diff_sha"], proof["git_diff_text"], proof["workspace_tree_sha"],
                    proof["runtime_digest"], canonical_json(proof["ast_impact"]),
                    canonical_json(proof["verification_results"]), signed.signature,
                    proof["created_at"], canonical_json(signed.to_dict()),
                )
                row = await conn.fetchrow(
                    "SELECT * FROM proof_bundles WHERE proof_id = $1", (proof["proof_id"],)
                )
        current = self._row_to_dict(row)
        assert current is not None
        current.pop("signed_proof", None)
        expected = {
            **proof,
            "ledger_signature": signed.signature,
            "signed_proof_json": canonical_json(signed.to_dict()),
        }
        if current != expected:
            raise ValueError("Conflicting data uses an existing signed proof id.")
        return current

    def attach_signed_proof(self, proof_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        from harness_core.capabilities.proof import SignedProofArtifact

        signed = SignedProofArtifact.from_dict(payload)
        if self.backend_type == "postgres":
            return self._run_sync(self.attach_signed_proof_async, proof_id, signed.to_dict())
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM proof_bundles WHERE proof_id = ?", (proof_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Proof bundle not found: {proof_id}")
            current = self._row_to_dict(row)
            assert current is not None
            for name in ("ast_impact", "verification_results"):
                current.pop(f"{name}_json", None)
            current.pop("signed_proof_json", None)
            current.pop("signed_proof", None)
            if current != signed.proof:
                raise ValueError("Signed proof does not match the stored proof artifact.")
            self.conn.execute(
                "UPDATE proof_bundles SET ledger_signature = ?, signed_proof_json = ? WHERE proof_id = ?",
                (signed.signature, canonical_json(signed.to_dict()), proof_id),
            )
            self.conn.commit()
            updated = self.conn.execute(
                "SELECT * FROM proof_bundles WHERE proof_id = ?", (proof_id,)
            ).fetchone()
            return self._row_to_dict(updated)  # type: ignore[return-value]

    async def attach_signed_proof_async(
        self, proof_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        from harness_core.capabilities.proof import SignedProofArtifact

        signed = SignedProofArtifact.from_dict(payload)
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.attach_signed_proof, proof_id, signed.to_dict())
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT * FROM proof_bundles WHERE proof_id = $1", proof_id
                )
                if row is None:
                    raise KeyError(f"Proof bundle not found: {proof_id}")
                current = self._row_to_dict(row)
                assert current is not None
                for name in ("ast_impact", "verification_results"):
                    current.pop(f"{name}_json", None)
                current.pop("signed_proof_json", None)
                current.pop("signed_proof", None)
                if current != signed.proof:
                    raise ValueError("Signed proof does not match the stored proof artifact.")
                await conn.execute(
                    "UPDATE proof_bundles SET ledger_signature = $1, signed_proof_json = $2 "
                    "WHERE proof_id = $3",
                    signed.signature, canonical_json(signed.to_dict()), proof_id,
                )
                updated = await conn.fetchrow(
                    "SELECT * FROM proof_bundles WHERE proof_id = $1", proof_id
                )
                return self._row_to_dict(updated)  # type: ignore[return-value]

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

    def search_tasks(
        self,
        query: str,
        project_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Search tasks, execution transcripts, and logs via FTS5 BM25 index."""
        results = self.search_index.search(query, project_id=project_id, limit=limit)
        return [r.to_dict() for r in results]

    # ------------------------------------------------------------------
    # Garbage Collection & Retention Queries
    # ------------------------------------------------------------------

    def pin_task(self, task_id: str, pinned: bool = True) -> bool:
        """Pin or unpin a task to exempt or unexempt its workspace artifacts from GC."""
        if self.backend_type == "postgres":
            return self._run_sync(self.pin_task_async, task_id, pinned)
        with self._lock:
            cur = self.conn.execute(
                "UPDATE tasks SET pinned = ?, updated_at = ? WHERE task_id = ?",
                (1 if pinned else 0, utcnow_iso(), task_id),
            )
            self.conn.commit()
            return cur.rowcount > 0

    async def pin_task_async(self, task_id: str, pinned: bool = True) -> bool:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.pin_task, task_id, pinned)
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            res = await conn.execute(
                "UPDATE tasks SET pinned = $1, updated_at = $2 WHERE task_id = $3",
                1 if pinned else 0,
                utcnow_iso(),
                task_id,
            )
            return "UPDATE 1" in res

    def is_task_pinned(self, task_id: str) -> bool:
        """Check whether a task is explicitly pinned."""
        task = self.get_task(task_id)
        if not task:
            return False
        return bool(task.get("pinned", 0))

    def get_terminal_unpinned_tasks_older_than(self, cutoff_iso: str) -> list[dict[str, Any]]:
        """Find terminal unpinned tasks updated before cutoff_iso."""
        if self.backend_type == "postgres":
            return self._run_sync(self.get_terminal_unpinned_tasks_older_than_async, cutoff_iso)
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT task_id, status, updated_at, pinned
                FROM tasks
                WHERE (pinned IS NULL OR pinned = 0)
                  AND status IN ('SUCCEEDED', 'COMPLETED', 'FAILED', 'ABANDONED')
                  AND updated_at < ?
                """,
                (cutoff_iso,),
            ).fetchall()
            return [dict(r) for r in rows]

    async def get_terminal_unpinned_tasks_older_than_async(self, cutoff_iso: str) -> list[dict[str, Any]]:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.get_terminal_unpinned_tasks_older_than, cutoff_iso)
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT task_id, status, updated_at, pinned
                FROM tasks
                WHERE (pinned IS NULL OR pinned = 0)
                  AND status IN ('SUCCEEDED', 'COMPLETED', 'FAILED', 'ABANDONED')
                  AND updated_at < $1
                """,
                cutoff_iso,
            )
            return [dict(r) for r in rows]

    def get_active_or_pinned_cas_hashes(self, recent_cutoff_iso: str) -> set[str]:
        """Collect all SHA-256 hashes referenced by active tasks, pinned tasks, or tasks updated >= recent_cutoff_iso."""
        if self.backend_type == "postgres":
            return self._run_sync(self.get_active_or_pinned_cas_hashes_async, recent_cutoff_iso)
        with self._lock:
            protected_query = """
                SELECT task_id FROM tasks
                WHERE pinned = 1
                   OR status NOT IN ('SUCCEEDED', 'COMPLETED', 'FAILED', 'ABANDONED')
                   OR updated_at >= ?
            """
            rows = self.conn.execute(protected_query, (recent_cutoff_iso,)).fetchall()
            protected_task_ids = [r["task_id"] for r in rows]

            hashes: set[str] = set()

            if protected_task_ids:
                # Query in chunks of 500 to avoid SQLite variable limits
                for i in range(0, len(protected_task_ids), 500):
                    chunk = protected_task_ids[i : i + 500]
                    placeholders = ",".join("?" for _ in chunk)

                    art_rows = self.conn.execute(
                        f"SELECT sha256 FROM artifacts WHERE task_id IN ({placeholders}) AND sha256 IS NOT NULL",
                        chunk,
                    ).fetchall()
                    for r in art_rows:
                        if r["sha256"]:
                            hashes.add(r["sha256"].lower().strip())

                    pb_rows = self.conn.execute(
                        f"""
                        SELECT git_diff_sha, workspace_tree_sha, runtime_digest
                        FROM proof_bundles WHERE task_id IN ({placeholders})
                        """,
                        chunk,
                    ).fetchall()
                    for r in pb_rows:
                        for k in ("git_diff_sha", "workspace_tree_sha", "runtime_digest"):
                            val = r[k]
                            if val and len(val) >= 8:
                                hashes.add(val.lower().strip())

                    chk_rows = self.conn.execute(
                        f"""
                        SELECT workspace_tree_sha, sha256
                        FROM checkpoints WHERE task_id IN ({placeholders})
                        """,
                        chunk,
                    ).fetchall()
                    for r in chk_rows:
                        for k in ("workspace_tree_sha", "sha256"):
                            val = r[k]
                            if val and len(val) >= 8:
                                hashes.add(val.lower().strip())

                    att_rows = self.conn.execute(
                        f"""
                        SELECT base_tree_sha, head_tree_sha
                        FROM attempts WHERE task_id IN ({placeholders})
                        """,
                        chunk,
                    ).fetchall()
                    for r in att_rows:
                        for k in ("base_tree_sha", "head_tree_sha"):
                            val = r[k]
                            if val and len(val) >= 8:
                                hashes.add(val.lower().strip())

            t_rows = self.conn.execute(
                "SELECT active_workspace_tree_sha FROM tasks WHERE active_workspace_tree_sha IS NOT NULL"
            ).fetchall()
            for r in t_rows:
                val = r["active_workspace_tree_sha"]
                if val and len(val) >= 8:
                    hashes.add(val.lower().strip())

            return hashes

    async def get_active_or_pinned_cas_hashes_async(self, recent_cutoff_iso: str) -> set[str]:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.get_active_or_pinned_cas_hashes, recent_cutoff_iso)
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            protected_rows = await conn.fetch(
                """
                SELECT task_id FROM tasks
                WHERE pinned = 1
                   OR status NOT IN ('SUCCEEDED', 'COMPLETED', 'FAILED', 'ABANDONED')
                   OR updated_at >= $1
                """,
                recent_cutoff_iso,
            )
            protected_ids = [r["task_id"] for r in protected_rows]
            hashes: set[str] = set()

            if protected_ids:
                art_rows = await conn.fetch(
                    "SELECT sha256 FROM artifacts WHERE task_id = ANY($1::text[]) AND sha256 IS NOT NULL",
                    protected_ids,
                )
                for r in art_rows:
                    if r["sha256"]:
                        hashes.add(r["sha256"].lower().strip())

            return hashes

    def vacuum(self, incremental: bool = True) -> None:
        """Run SQLite vacuum to reclaim space. Uses non-blocking incremental_vacuum when incremental=True."""
        if self.backend_type != "sqlite" or self.conn is None:
            return
        with self._lock:
            try:
                if incremental:
                    self.conn.execute("PRAGMA incremental_vacuum;")
                else:
                    self.conn.execute("VACUUM;")
            except Exception as exc:
                logger.debug("Ledger vacuum exception: %s", exc)

    # ------------------------------------------------------------------
    # Conversational Timeline & Dialogue Tracking (Phase 37)
    # ------------------------------------------------------------------

    def ensure_task_exists(self, task_id: str, goal: str = "") -> None:
        """Ensure task row exists in tasks table to satisfy foreign keys."""
        if self.backend_type == "postgres":
            return self._run_sync(self.ensure_task_exists_async, task_id, goal)
        with self._lock:
            row = self.conn.execute("SELECT task_id FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not row:
                now = utcnow_iso()
                self.conn.execute(
                    """
                    INSERT INTO tasks (
                        task_id, goal, execution_mode, status, priority,
                        budget_json, constraints_json, policy_snapshot_id, version,
                        created_at, updated_at
                    ) VALUES (?, ?, 'standard', 'RUNNING', 50, '{}', '{}', 'pol-v1.3', 1, ?, ?)
                    """,
                    (task_id, goal or f"Session task {task_id}", now, now),
                )
                self.conn.commit()

    async def ensure_task_exists_async(self, task_id: str, goal: str = "") -> None:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.ensure_task_exists, task_id, goal)
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT task_id FROM tasks WHERE task_id = $1", task_id)
            if not row:
                now = utcnow_iso()
                await conn.execute(
                    """
                    INSERT INTO tasks (
                        task_id, goal, execution_mode, status, priority,
                        budget_json, constraints_json, policy_snapshot_id, version,
                        created_at, updated_at
                    ) VALUES ($1, $2, 'standard', 'RUNNING', 50, '{}', '{}', 'pol-v1.3', 1, $3, $4)
                    """,
                    task_id, goal or f"Session task {task_id}", now, now,
                )

    def record_dialogue_turn(
        self,
        task_id: str,
        turn_index: int,
        role: str,
        content: str,
        branch_id: str = "main",
        is_active: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record an individual dialogue turn into the ledger."""
        if self.backend_type == "postgres":
            return self._run_sync(
                self.record_dialogue_turn_async,
                task_id, turn_index, role, content, branch_id, is_active, metadata
            )
        self.ensure_task_exists(task_id)
        with self._lock:
            turn_id = f"trn_{uuid.uuid4().hex[:12]}"
            now = utcnow_iso()
            meta_str = canonical_json(metadata or {})
            self.conn.execute(
                """
                INSERT INTO dialogue_turns (
                    turn_id, task_id, turn_index, role, content,
                    is_active, branch_id, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (turn_id, task_id, turn_index, role, content, 1 if is_active else 0, branch_id, meta_str, now),
            )
            self.conn.commit()
            return {
                "turn_id": turn_id,
                "task_id": task_id,
                "turn_index": turn_index,
                "role": role,
                "content": content,
                "is_active": is_active,
                "branch_id": branch_id,
                "metadata": metadata or {},
                "created_at": now,
            }

    async def record_dialogue_turn_async(
        self,
        task_id: str,
        turn_index: int,
        role: str,
        content: str,
        branch_id: str = "main",
        is_active: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(
                self.record_dialogue_turn, task_id, turn_index, role, content, branch_id, is_active, metadata
            )
        await self.ensure_task_exists_async(task_id)
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            turn_id = f"trn_{uuid.uuid4().hex[:12]}"
            now = utcnow_iso()
            meta_str = canonical_json(metadata or {})
            await conn.execute(
                """
                INSERT INTO dialogue_turns (
                    turn_id, task_id, turn_index, role, content,
                    is_active, branch_id, metadata_json, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                turn_id, task_id, turn_index, role, content, 1 if is_active else 0, branch_id, meta_str, now,
            )
            return {
                "turn_id": turn_id,
                "task_id": task_id,
                "turn_index": turn_index,
                "role": role,
                "content": content,
                "is_active": is_active,
                "branch_id": branch_id,
                "metadata": metadata or {},
                "created_at": now,
            }

    def save_dialogue_turns(
        self,
        task_id: str,
        messages: list[dict[str, Any]],
        branch_id: str = "main",
    ) -> list[dict[str, Any]]:
        """Batch persist a list of messages into dialogue_turns."""
        from harness_core.storage.timeline_rewind import group_messages_by_turn

        groups = group_messages_by_turn(messages)
        saved: list[dict[str, Any]] = []
        for t_idx, msgs in groups:
            for m in msgs:
                rec = self.record_dialogue_turn(
                    task_id=task_id,
                    turn_index=t_idx,
                    role=m.get("role", "user"),
                    content=str(m.get("content", "")),
                    branch_id=branch_id,
                    is_active=True,
                    metadata={"turn_id": m.get("turn_id") or m.get("id")},
                )
                saved.append(rec)
        return saved

    def get_active_dialogue(self, task_id: str) -> list[dict[str, Any]]:
        """Fetch all active dialogue turns for a task in chronological order."""
        if self.backend_type == "postgres":
            return self._run_sync(self.get_active_dialogue_async, task_id)
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT turn_id, task_id, turn_index, role, content, is_active, branch_id, metadata_json, created_at
                FROM dialogue_turns
                WHERE task_id = ? AND is_active = 1
                ORDER BY turn_index ASC, created_at ASC
                """,
                (task_id,),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]

    async def get_active_dialogue_async(self, task_id: str) -> list[dict[str, Any]]:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.get_active_dialogue, task_id)
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT turn_id, task_id, turn_index, role, content, is_active, branch_id, metadata_json, created_at
                FROM dialogue_turns
                WHERE task_id = $1 AND is_active = 1
                ORDER BY turn_index ASC, created_at ASC
                """,
                task_id,
            )
            return [self._row_to_dict(r) for r in rows]

    def get_all_dialogue_turns(self, task_id: str) -> list[dict[str, Any]]:
        """Fetch all dialogue turns (including soft-branched/inactive turns) for a task."""
        if self.backend_type == "postgres":
            return self._run_sync(self.get_all_dialogue_turns_async, task_id)
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT turn_id, task_id, turn_index, role, content, is_active, branch_id, metadata_json, created_at
                FROM dialogue_turns
                WHERE task_id = ?
                ORDER BY turn_index ASC, created_at ASC
                """,
                (task_id,),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]

    async def get_all_dialogue_turns_async(self, task_id: str) -> list[dict[str, Any]]:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.get_all_dialogue_turns, task_id)
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT turn_id, task_id, turn_index, role, content, is_active, branch_id, metadata_json, created_at
                FROM dialogue_turns
                WHERE task_id = $1
                ORDER BY turn_index ASC, created_at ASC
                """,
                task_id,
            )
            return [self._row_to_dict(r) for r in rows]

    def save_compaction_checkpoint(
        self,
        task_id: str,
        summary_text: str,
        cut_turn_index: int,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist a compaction checkpoint: the summary + the cut boundary it covers.

        Stored as an active ``dialogue_turns`` row (role ``system``) tagged
        ``type=compaction_checkpoint`` so ``reconstruct_active_dialogue`` can rebuild
        the compacted window after a restart instead of replaying raw turns.
        Raw rows are left active for forensic/rewind completeness; the cut boundary
        alone decides what reconstruction skips.
        """
        meta = {"type": "compaction_checkpoint", "cut_turn_index": int(cut_turn_index)}
        meta.update(metadata or {})
        row = self.record_dialogue_turn(
            task_id=task_id,
            turn_index=int(cut_turn_index),
            role="system",
            content=summary_text,
            metadata=meta,
        )
        try:
            self.record_event(
                task_id=task_id,
                event_type="CONTEXT_COMPACTED",
                reason_code="COMPACTION_CHECKPOINT_SAVED",
                payload={
                    "cut_turn_index": int(cut_turn_index),
                    "summary_chars": len(summary_text),
                    **{k: v for k, v in (metadata or {}).items() if k != "type"},
                },
            )
        except Exception:
            pass
        return row

    def load_compaction_checkpoint(self, task_id: str) -> dict[str, Any] | None:
        """Return the newest active compaction checkpoint row for a task, or None."""
        checkpoints = [
            r
            for r in self.get_active_dialogue(task_id)
            if (r.get("metadata") or {}).get("type") == "compaction_checkpoint"
        ]
        if not checkpoints:
            return None
        return max(
            checkpoints,
            key=lambda r: (int(r.get("turn_index") or 0), str(r.get("created_at") or "")),
        )

    def reconstruct_active_dialogue(self, task_id: str) -> dict[str, Any]:
        """Rebuild the live conversation window from durable dialogue state.

        Without a checkpoint this is the full raw transcript; with one it is the
        checkpoint summary followed by every active row recorded after the cut
        boundary — the durable equivalent of the in-memory compacted window.
        """
        rows = [r for r in self.get_active_dialogue(task_id) if r.get("content")]
        checkpoint = self.load_compaction_checkpoint(task_id)
        if checkpoint is None:
            messages = [
                {"role": r["role"], "content": r["content"], "turn_index": r.get("turn_index")}
                for r in rows
            ]
            next_index = max((int(r.get("turn_index") or 0) for r in rows), default=-1) + 1
            return {"messages": messages, "next_turn_index": next_index, "checkpoint": None}

        cut = int((checkpoint.get("metadata") or {}).get("cut_turn_index", checkpoint.get("turn_index") or 0))
        messages: list[dict[str, Any]] = [{"role": checkpoint["role"], "content": checkpoint["content"]}]
        messages.extend(
            {"role": r["role"], "content": r["content"], "turn_index": r.get("turn_index")}
            for r in rows
            if int(r.get("turn_index") or 0) > cut
            and (r.get("metadata") or {}).get("type") != "compaction_checkpoint"
        )
        next_index = max((int(r.get("turn_index") or 0) for r in rows), default=cut) + 1
        return {"messages": messages, "next_turn_index": next_index, "checkpoint": checkpoint}

    def revert_tool_calls_for_turns(self, task_id: str, turn_indices: list[int]) -> int:
        """Mark tool calls belonging to specified turn indices as REVERTED."""
        if not turn_indices:
            return 0
        step_ids = [f"turn_{idx}" for idx in turn_indices]
        if self.backend_type == "postgres":
            return self._run_sync(self.revert_tool_calls_for_turns_async, task_id, turn_indices)
        with self._lock:
            placeholders = ",".join(["?"] * len(step_ids))
            cur = self.conn.execute(
                f"""
                UPDATE tool_calls
                SET status = 'REVERTED'
                WHERE task_id = ? AND step_id IN ({placeholders}) AND status != 'REVERTED'
                """,
                [task_id, *step_ids],
            )
            self.conn.commit()
            return cur.rowcount

    async def revert_tool_calls_for_turns_async(self, task_id: str, turn_indices: list[int]) -> int:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.revert_tool_calls_for_turns, task_id, turn_indices)
        step_ids = [f"turn_{idx}" for idx in turn_indices]
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE tool_calls
                SET status = 'REVERTED'
                WHERE task_id = $1 AND step_id = ANY($2::text[]) AND status != 'REVERTED'
                """,
                task_id, step_ids,
            )
            try:
                return int(result.split()[-1])
            except Exception:
                return 0

    def record_timeline_branch(
        self,
        task_id: str,
        branch_id: str,
        superseded_turns: list[dict[str, Any]],
        reason: str = "",
    ) -> dict[str, Any]:
        """Archive superseded dialogue turns under a branch ID, revert corresponding tools,
        and record a cryptographically hash-chained TIMELINE_REWOUND event."""
        if self.backend_type == "postgres":
            return self._run_sync(self.record_timeline_branch_async, task_id, branch_id, superseded_turns, reason)

        self.ensure_task_exists(task_id)

        # 1. Soft-branch dialogue turns in ledger
        superseded_indices: set[int] = set()
        with self._lock:
            for t in superseded_turns:
                t_idx = t.get("turn_index") or t.get("turn")
                if t_idx is not None:
                    superseded_indices.add(int(t_idx))

                turn_id = t.get("turn_id") or t.get("id")
                if turn_id:
                    self.conn.execute(
                        "UPDATE dialogue_turns SET is_active = 0, branch_id = ? WHERE turn_id = ?",
                        (branch_id, turn_id),
                    )
                elif t_idx is not None:
                    self.conn.execute(
                        "UPDATE dialogue_turns SET is_active = 0, branch_id = ? WHERE task_id = ? AND turn_index = ?",
                        (branch_id, task_id, int(t_idx)),
                    )
                else:
                    # Insert as archived record
                    now = utcnow_iso()
                    new_tid = f"trn_{uuid.uuid4().hex[:12]}"
                    self.conn.execute(
                        """
                        INSERT INTO dialogue_turns (
                            turn_id, task_id, turn_index, role, content,
                            is_active, branch_id, metadata_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, 0, ?, '{}', ?)
                        """,
                        (new_tid, task_id, 0, t.get("role", "unknown"), str(t.get("content", "")), branch_id, now),
                    )
            self.conn.commit()

        # 2. Revert uncommitted/trailing tool executions
        reverted_count = self.revert_tool_calls_for_turns(task_id, sorted(superseded_indices))

        # 3. Hash-chained TIMELINE_REWOUND event
        ev = self.record_event(
            task_id=task_id,
            event_type="TIMELINE_REWOUND",
            reason_code="TIMELINE_REWIND",
            payload={
                "branch_id": branch_id,
                "reason": reason,
                "superseded_turn_count": len(superseded_turns),
                "superseded_turn_indices": sorted(superseded_indices),
                "reverted_tool_count": reverted_count,
            },
        )

        return {
            "branch_id": branch_id,
            "event_id": ev.get("event_id"),
            "reverted_tool_count": reverted_count,
            "superseded_count": len(superseded_turns),
        }

    async def record_timeline_branch_async(
        self,
        task_id: str,
        branch_id: str,
        superseded_turns: list[dict[str, Any]],
        reason: str = "",
    ) -> dict[str, Any]:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.record_timeline_branch, task_id, branch_id, superseded_turns, reason)

        await self.ensure_task_exists_async(task_id)
        superseded_indices: set[int] = set()
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            for t in superseded_turns:
                t_idx = t.get("turn_index") or t.get("turn")
                if t_idx is not None:
                    superseded_indices.add(int(t_idx))

                turn_id = t.get("turn_id") or t.get("id")
                if turn_id:
                    await conn.execute(
                        "UPDATE dialogue_turns SET is_active = 0, branch_id = $1 WHERE turn_id = $2",
                        branch_id, turn_id,
                    )
                elif t_idx is not None:
                    await conn.execute(
                        "UPDATE dialogue_turns SET is_active = 0, branch_id = $1 WHERE task_id = $2 AND turn_index = $3",
                        branch_id, task_id, int(t_idx),
                    )
                else:
                    now = utcnow_iso()
                    new_tid = f"trn_{uuid.uuid4().hex[:12]}"
                    await conn.execute(
                        """
                        INSERT INTO dialogue_turns (
                            turn_id, task_id, turn_index, role, content,
                            is_active, branch_id, metadata_json, created_at
                        ) VALUES ($1, $2, $3, $4, $5, 0, $6, '{}', $7)
                        """,
                        new_tid, task_id, 0, t.get("role", "unknown"), str(t.get("content", "")), branch_id, now,
                    )

        reverted_count = await self.revert_tool_calls_for_turns_async(task_id, sorted(superseded_indices))

        ev = await self.record_event_async(
            task_id=task_id,
            event_type="TIMELINE_REWOUND",
            reason_code="TIMELINE_REWIND",
            payload={
                "branch_id": branch_id,
                "reason": reason,
                "superseded_turn_count": len(superseded_turns),
                "superseded_turn_indices": sorted(superseded_indices),
                "reverted_tool_count": reverted_count,
            },
        )

        return {
            "branch_id": branch_id,
            "event_id": ev.get("event_id"),
            "reverted_tool_count": reverted_count,
            "superseded_count": len(superseded_turns),
        }

    # ------------------------------------------------------------------
    # Session Title & Provenance Engine (Phase 37)
    # ------------------------------------------------------------------

    def update_task_goal(self, task_id: str, new_goal: str, provenance: str = "llm") -> bool:
        """Update task goal respecting the provenance hierarchy: USER > LLM > DERIVED."""
        if self.backend_type == "postgres":
            return self._run_sync(self.update_task_goal_async, task_id, new_goal, provenance)

        hierarchy = {"derived": 1, "llm": 2, "user": 3}
        target_rank = hierarchy.get(provenance.lower(), 2)

        with self._lock:
            row = self.conn.execute(
                "SELECT goal, title_provenance FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if not row:
                self.ensure_task_exists(task_id, goal=new_goal)
                row = self.conn.execute(
                    "SELECT goal, title_provenance FROM tasks WHERE task_id = ?",
                    (task_id,),
                ).fetchone()

            current_prov = (row["title_provenance"] if "title_provenance" in row.keys() else "derived") or "derived"
            current_rank = hierarchy.get(str(current_prov).lower(), 1)

            # Strict provenance gate: never overwrite higher provenance
            if current_rank > target_rank:
                return False

            old_goal = row["goal"]
            now = utcnow_iso()
            try:
                self.conn.execute(
                    "UPDATE tasks SET goal = ?, title_provenance = ?, updated_at = ? WHERE task_id = ?",
                    (new_goal, provenance.lower(), now, task_id),
                )
            except sqlite3.OperationalError:
                self.conn.execute(
                    "UPDATE tasks SET goal = ?, updated_at = ? WHERE task_id = ?",
                    (new_goal, now, task_id),
                )
            self.conn.commit()

            try:
                self.record_event(
                    task_id=task_id,
                    event_type="TASK_TITLE_UPDATED",
                    reason_code="TITLE_SYNTHESIS",
                    payload={
                        "old_goal": old_goal,
                        "new_goal": new_goal,
                        "provenance": provenance.lower(),
                    },
                )
            except Exception:
                pass

            return True

    async def update_task_goal_async(self, task_id: str, new_goal: str, provenance: str = "llm") -> bool:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.update_task_goal, task_id, new_goal, provenance)

        hierarchy = {"derived": 1, "llm": 2, "user": 3}
        target_rank = hierarchy.get(provenance.lower(), 2)

        pool = await self.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT goal, title_provenance FROM tasks WHERE task_id = $1",
                task_id,
            )
            if not row:
                await self.ensure_task_exists_async(task_id, goal=new_goal)
                row = await conn.fetchrow(
                    "SELECT goal, title_provenance FROM tasks WHERE task_id = $1",
                    task_id,
                )

            current_prov = (row["title_provenance"] if "title_provenance" in row.keys() else "derived") or "derived"
            current_rank = hierarchy.get(str(current_prov).lower(), 1)

            if current_rank > target_rank:
                return False

            old_goal = row["goal"]
            now = utcnow_iso()
            try:
                await conn.execute(
                    "UPDATE tasks SET goal = $1, title_provenance = $2, updated_at = $3 WHERE task_id = $4",
                    new_goal, provenance.lower(), now, task_id,
                )
            except Exception:
                await conn.execute(
                    "UPDATE tasks SET goal = $1, updated_at = $2 WHERE task_id = $3",
                    new_goal, now, task_id,
                )

            try:
                await self.record_event_async(
                    task_id=task_id,
                    event_type="TASK_TITLE_UPDATED",
                    reason_code="TITLE_SYNTHESIS",
                    payload={
                        "old_goal": old_goal,
                        "new_goal": new_goal,
                        "provenance": provenance.lower(),
                    },
                )
            except Exception:
                pass

            return True

    # ------------------------------------------------------------------
    # Temporal Approval Leasing (Phase 37)
    # ------------------------------------------------------------------

    def record_approval_lease(
        self,
        *,
        lease_id: str,
        command_prefix: str | None = None,
        allowed_paths: list[str] | None = None,
        scope: str = "temporal",
        ttl_seconds: int = 900,
        granted_at: float | None = None,
        expires_at: float | None = None,
        task_id: str | None = None,
        is_revoked: bool = False,
        bindings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.backend_type == "postgres":
            return self._run_sync(
                self.record_approval_lease_async,
                lease_id=lease_id,
                command_prefix=command_prefix,
                allowed_paths=allowed_paths,
                scope=scope,
                ttl_seconds=ttl_seconds,
                granted_at=granted_at,
                expires_at=expires_at,
                task_id=task_id,
                is_revoked=is_revoked,
                bindings=bindings,
            )
        now_f = time.time()
        g_at = granted_at if granted_at is not None else now_f
        exp_at = expires_at if expires_at is not None else (g_at + ttl_seconds)
        now_iso = utcnow_iso()
        paths = allowed_paths or []
        if task_id:
            try:
                self.ensure_task_exists(task_id)
            except Exception:
                pass
        with self._lock:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO approval_leases (
                    lease_id, task_id, command_prefix, allowed_paths_json,
                    scope, ttl_seconds, granted_at, expires_at, is_revoked, created_at,
                    bindings_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lease_id,
                    task_id,
                    command_prefix,
                    canonical_json(paths),
                    scope,
                    ttl_seconds,
                    g_at,
                    exp_at,
                    1 if is_revoked else 0,
                    now_iso,
                    canonical_json(bindings or {}),
                ),
            )
            self.conn.commit()
            if task_id:
                try:
                    self.record_event(
                        task_id=task_id,
                        event_type="APPROVAL_LEASE_GRANTED",
                        payload={"lease_id": lease_id, "command_prefix": command_prefix, "expires_at": exp_at},
                    )
                except Exception:
                    pass
            row = self.conn.execute(
                "SELECT * FROM approval_leases WHERE lease_id = ?", (lease_id,)
            ).fetchone()
            d = self._row_to_dict(row)
            if d and "allowed_paths" not in d and "allowed_paths_json" in d:
                d["allowed_paths"] = json.loads(d["allowed_paths_json"]) if isinstance(d["allowed_paths_json"], str) else d["allowed_paths_json"]
            return d or {}

    async def record_approval_lease_async(
        self,
        *,
        lease_id: str,
        command_prefix: str | None = None,
        allowed_paths: list[str] | None = None,
        scope: str = "temporal",
        ttl_seconds: int = 900,
        granted_at: float | None = None,
        expires_at: float | None = None,
        task_id: str | None = None,
        is_revoked: bool = False,
        bindings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(
                self.record_approval_lease,
                lease_id=lease_id,
                command_prefix=command_prefix,
                allowed_paths=allowed_paths,
                scope=scope,
                ttl_seconds=ttl_seconds,
                granted_at=granted_at,
                expires_at=expires_at,
                task_id=task_id,
                is_revoked=is_revoked,
                bindings=bindings,
            )
        now_f = time.time()
        g_at = granted_at if granted_at is not None else now_f
        exp_at = expires_at if expires_at is not None else (g_at + ttl_seconds)
        now_iso = utcnow_iso()
        paths = allowed_paths or []
        if task_id:
            try:
                await self.ensure_task_exists_async(task_id)
            except Exception:
                pass
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO approval_leases (
                    lease_id, task_id, command_prefix, allowed_paths_json,
                    scope, ttl_seconds, granted_at, expires_at, is_revoked, created_at,
                    bindings_json
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                ON CONFLICT (lease_id) DO UPDATE SET
                    is_revoked = EXCLUDED.is_revoked,
                    expires_at = EXCLUDED.expires_at
                """,
                lease_id, task_id, command_prefix, canonical_json(paths),
                scope, ttl_seconds, g_at, exp_at, 1 if is_revoked else 0, now_iso,
                canonical_json(bindings or {}),
            )
            if task_id:
                try:
                    await self.record_event_async(
                        task_id=task_id,
                        event_type="APPROVAL_LEASE_GRANTED",
                        payload={"lease_id": lease_id, "command_prefix": command_prefix, "expires_at": exp_at},
                    )
                except Exception:
                    pass
            return {
                "lease_id": lease_id,
                "task_id": task_id,
                "command_prefix": command_prefix,
                "allowed_paths": paths,
                "scope": scope,
                "ttl_seconds": ttl_seconds,
                "granted_at": g_at,
                "expires_at": exp_at,
                "is_revoked": is_revoked,
                "created_at": now_iso,
                "bindings_json": canonical_json(bindings or {}),
            }

    def revoke_approval_lease(self, lease_id: str) -> bool:
        if self.backend_type == "postgres":
            return self._run_sync(self.revoke_approval_lease_async, lease_id)
        with self._lock:
            cur = self.conn.execute(
                "UPDATE approval_leases SET is_revoked = 1 WHERE lease_id = ?",
                (lease_id,),
            )
            self.conn.commit()
            return cur.rowcount > 0

    async def revoke_approval_lease_async(self, lease_id: str) -> bool:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.revoke_approval_lease, lease_id)
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            res = await conn.execute(
                "UPDATE approval_leases SET is_revoked = 1 WHERE lease_id = $1",
                lease_id,
            )
            return "UPDATE 1" in res

    def list_approval_leases(self, task_id: str | None = None, active_only: bool = True) -> list[dict[str, Any]]:
        if self.backend_type == "postgres":
            return self._run_sync(self.list_approval_leases_async, task_id, active_only)
        now_f = time.time()
        with self._lock:
            query = "SELECT * FROM approval_leases WHERE 1=1"
            params: list[Any] = []
            if task_id:
                query += " AND task_id = ?"
                params.append(task_id)
            if active_only:
                query += " AND is_revoked = 0 AND expires_at > ?"
                params.append(now_f)
            query += " ORDER BY granted_at DESC"
            rows = self.conn.execute(query, tuple(params)).fetchall()
            results = []
            for r in rows:
                d = self._row_to_dict(r)
                if d:
                    if "allowed_paths" not in d and "allowed_paths_json" in d:
                        try:
                            d["allowed_paths"] = json.loads(d["allowed_paths_json"]) if isinstance(d["allowed_paths_json"], str) else d["allowed_paths_json"]
                        except Exception:
                            d["allowed_paths"] = []
                    results.append(d)
            return results

    async def list_approval_leases_async(self, task_id: str | None = None, active_only: bool = True) -> list[dict[str, Any]]:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.list_approval_leases, task_id, active_only)
        now_f = time.time()
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            query = "SELECT * FROM approval_leases WHERE 1=1"
            params: list[Any] = []
            param_idx = 1
            if task_id:
                query += f" AND task_id = ${param_idx}"
                params.append(task_id)
                param_idx += 1
            if active_only:
                query += f" AND is_revoked = 0 AND expires_at > ${param_idx}"
                params.append(now_f)
                param_idx += 1
            query += " ORDER BY granted_at DESC"
            rows = await conn.fetch(query, *params)
            results = []
            for r in rows:
                d = dict(r)
                if "allowed_paths_json" in d:
                    try:
                        d["allowed_paths"] = json.loads(d["allowed_paths_json"]) if isinstance(d["allowed_paths_json"], str) else d["allowed_paths_json"]
                    except Exception:
                        d["allowed_paths"] = []
                results.append(d)
            return results

    def cleanup_expired_approval_leases(self) -> int:
        if self.backend_type == "postgres":
            return self._run_sync(self.cleanup_expired_approval_leases_async)
        now_f = time.time()
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM approval_leases WHERE expires_at <= ? OR is_revoked = 1",
                (now_f,),
            )
            self.conn.commit()
            return cur.rowcount

    async def cleanup_expired_approval_leases_async(self) -> int:
        if self.backend_type == "sqlite":
            return await asyncio.to_thread(self.cleanup_expired_approval_leases)
        now_f = time.time()
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            res = await conn.execute(
                "DELETE FROM approval_leases WHERE expires_at <= $1 OR is_revoked = 1",
                now_f,
            )
            # res is e.g. "DELETE 3"
            try:
                return int(res.split()[-1])
            except Exception:
                return 0



