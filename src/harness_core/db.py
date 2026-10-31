"""Authoritative Task Ledger, Append-Only Event Stream, Lease/Fencing Manager, and Call Journal.

Implements PRD Sections 6.1, 6.2, 9, and 10 over SQLite WAL (default zero-config local profile)
or PostgreSQL 16+ connection strings, maintaining cryptographic hash chains on task_events.
"""

from __future__ import annotations

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


class ConcurrencyFenceError(RuntimeError):
    """Raised when a stale worker fencing token or version mismatch attempts a commit."""


class InvalidTransitionError(ValueError):
    """Raised when a state transition violates the PRD Section 9 state machine."""


class LedgerStore:
    """Thread-safe transactional Task Ledger & Call Journal."""

    def __init__(self, db_path: Path | str, schema_sql_path: Path | str | None = None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA foreign_keys=ON;")
        self._init_schema(schema_sql_path)

    def _init_schema(self, schema_sql_path: Path | str | None) -> None:
        if schema_sql_path is None:
            candidate = Path(__file__).resolve().parents[2] / "migrations" / "0001_initial_schema.sql"
            if candidate.is_file():
                schema_sql_path = candidate
        if schema_sql_path and Path(schema_sql_path).is_file():
            sql = Path(schema_sql_path).read_text(encoding="utf-8")
            with self._lock:
                self.conn.executescript(sql)
                self.conn.commit()

    def _row_to_dict(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        d = dict(row)
        for k in list(d.keys()):
            if k.endswith("_json") and isinstance(d[k], str):
                try:
                    d[k[:-5]] = json.loads(d[k])
                except json.JSONDecodeError:
                    d[k[:-5]] = d[k]
        return d

    # ------------------------------------------------------------------
    # Task Intake & State Machine (FR-001..FR-006, FR-010..FR-013)
    # ------------------------------------------------------------------

    def submit_task(self, req: TaskSubmitRequest, policy_snapshot_id: str = "pol-v1.3") -> dict[str, Any]:
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

        chain_material = (
            f"{prev_hash}|{task_id}|{seq_no}|{event_type}|{prior_state}|{next_state}|"
            f"{reason_code}|{workspace_tree_sha}|{payload_str}|{now}"
        )
        event_sha = sha256_hex(chain_material)

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

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            return self._row_to_dict(row)

    def list_tasks(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
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

    def list_events(self, task_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM task_events WHERE task_id = ? ORDER BY sequence_no ASC",
                (task_id,),
            ).fetchall()
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

    def list_workers(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM workers ORDER BY registered_at ASC").fetchall()
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

    def update_attempt(
        self,
        attempt_id: str,
        *,
        status: str | None = None,
        head_tree_sha: str | None = None,
        workspace_path: str | None = None,
        failure_class: str | None = None,
    ) -> dict[str, Any]:
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

    def list_attempts(self, task_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM attempts WHERE task_id = ? ORDER BY started_at ASC", (task_id,)
            ).fetchall()
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

    def get_latest_plan(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM plans WHERE task_id = ? ORDER BY revision DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            return self._row_to_dict(row)

    def append_blast_radius_checks(self, task_id: str, checks: list[str], warning_summary: str) -> dict[str, Any] | None:
        """FR-036a: Dynamically append impacted downstream tests/typechecks to active plan."""
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

    def get_latest_checkpoint(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM checkpoints WHERE task_id = ? AND validity = 'VALID' ORDER BY created_at DESC LIMIT 1",
                (task_id,),
            ).fetchone()
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

    def seal_tool_call(
        self,
        call_id: str,
        result: dict[str, Any],
        *,
        status: str = "SEALED",
        result_ref: str | None = None,
    ) -> dict[str, Any]:
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

    def list_tool_calls(self, task_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM tool_calls WHERE task_id = ? ORDER BY started_at ASC", (task_id,)
            ).fetchall()
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

    def decide_approval(
        self,
        approval_id: str,
        decision: str,
        decided_by: str = "owner",
        comment: str = "",
    ) -> dict[str, Any]:
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

    def list_approvals(self, task_id: str | None = None, decision: str | None = None) -> list[dict[str, Any]]:
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

    def list_artifacts(self, task_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM artifacts WHERE task_id = ? ORDER BY created_at ASC", (task_id,)
            ).fetchall()
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

    def get_proof_bundle(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM proof_bundles WHERE task_id = ? ORDER BY created_at DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            return self._row_to_dict(row)
