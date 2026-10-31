"""Unit tests for LedgerStore dual-driver architecture (SQLite WAL & PostgreSQL asyncpg).

Verifies PRD Section 6.1, 6.2, 9, 10, and Phase 01 Plan 02:
- Zero-dependency SQLite profile with sync and async parity.
- PostgreSQL connection string parsing, DSN normalization, and pool configuration.
- Query parameter placeholder translation (? -> $1, $2, ...) and JSON normalization.
- Cryptographic hash chaining integrity across both sync and async operations.
- Graceful connection error handling on unreachable PostgreSQL hosts.
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from harness_core.db import (
    ConcurrencyFenceError,
    InvalidTransitionError,
    LedgerStore,
    canonical_json,
    compute_event_hash,
    normalize_param,
    normalize_query,
    sha256_hex,
)
from harness_core.models import TaskBudget, TaskState, TaskSubmitRequest


class TestDualDriverQueryNormalization(unittest.TestCase):
    """Verifies parameter placeholder translation and JSON parameter normalization."""

    def test_normalize_query_sqlite_preserves_placeholders(self) -> None:
        query = "SELECT * FROM tasks WHERE status = ? AND priority > ?"
        self.assertEqual(normalize_query(query, "sqlite"), query)

    def test_normalize_query_postgres_translates_placeholders(self) -> None:
        query = "SELECT * FROM tasks WHERE status = ? AND priority > ? AND project_id = ?"
        expected = "SELECT * FROM tasks WHERE status = $1 AND priority > $2 AND project_id = $3"
        self.assertEqual(normalize_query(query, "postgres"), expected)

    def test_normalize_query_ignores_question_mark_inside_string_literals(self) -> None:
        query = "SELECT * FROM tasks WHERE goal = 'What is this?' AND status = ?"
        expected = "SELECT * FROM tasks WHERE goal = 'What is this?' AND status = $1"
        self.assertEqual(normalize_query(query, "postgres"), expected)

    def test_normalize_query_handles_escaped_quotes_inside_literals(self) -> None:
        query = "SELECT * FROM tasks WHERE prompt = 'Can''t find ?' AND id = ?"
        expected = "SELECT * FROM tasks WHERE prompt = 'Can''t find ?' AND id = $1"
        self.assertEqual(normalize_query(query, "postgres"), expected)

    def test_normalize_param_types(self) -> None:
        # Dictionary & list serialization
        self.assertEqual(normalize_param({"b": 2, "a": 1}), '{"a":1,"b":2}')
        self.assertEqual(normalize_param([3, 1, 2]), "[3,1,2]")
        # Enum value extraction
        self.assertEqual(normalize_param(TaskState.RUNNING), "RUNNING")
        # Primitives untouched
        self.assertEqual(normalize_param(42), 42)
        self.assertEqual(normalize_param("text"), "text")
        self.assertEqual(normalize_param(3.14), 3.14)
        self.assertEqual(normalize_param(True), True)

    def test_row_to_dict_json_normalization(self) -> None:
        store = LedgerStore(":memory:")
        try:
            # Case 1: JSON string from TEXT column
            row_raw_str = {"task_id": "tsk_01", "budget_json": '{"max_seconds": 60}'}
            d1 = store._row_to_dict(row_raw_str)
            self.assertIsNotNone(d1)
            self.assertEqual(d1["budget"], {"max_seconds": 60})

            # Case 2: Already decoded dict from JSONB column
            row_raw_dict = {"task_id": "tsk_02", "budget_json": {"max_seconds": 120}}
            d2 = store._row_to_dict(row_raw_dict)
            self.assertIsNotNone(d2)
            self.assertEqual(d2["budget"], {"max_seconds": 120})
        finally:
            store.close()


class TestPostgreSQLConfigurationAndParsing(unittest.TestCase):
    """Verifies URI parsing, connection pool parameter handling, and error handling."""

    def test_postgres_url_detection_and_dsn_normalization(self) -> None:
        variants = [
            ("postgresql://app:pass@localhost:5432/harness", "postgresql://app:pass@localhost:5432/harness"),
            ("postgres://app:pass@localhost:5432/harness", "postgresql://app:pass@localhost:5432/harness"),
            ("postgresql+asyncpg://app:pass@localhost:5432/harness", "postgresql://app:pass@localhost:5432/harness"),
        ]
        for url, expected_dsn in variants:
            store = LedgerStore(url, min_size=3, max_size=15, command_timeout=45.0)
            self.assertEqual(store.backend_type, "postgres")
            self.assertEqual(store.dsn, expected_dsn)
            self.assertEqual(store.min_size, 3)
            self.assertEqual(store.max_size, 15)
            self.assertEqual(store.command_timeout, 45.0)
            store.close()

    def test_unreachable_postgres_host_raises_connection_error(self) -> None:
        # Point to an unallocated high port with 0.5s timeout
        unreachable_url = "postgresql://user:pass@127.0.0.1:59998/harness_unreachable"
        store = LedgerStore(unreachable_url, command_timeout=0.5)

        async def attempt_connect() -> None:
            await store.get_pool()

        with self.assertRaises(Exception):
            asyncio.run(attempt_connect())
        store.close()


class TestSQLiteSyncAndAsyncParity(unittest.TestCase):
    """Verifies that all LedgerStore methods operate identically in sync and async on SQLite."""

    def setUp(self) -> None:
        self.store = LedgerStore(":memory:")

    def tearDown(self) -> None:
        self.store.close()

    def test_task_intake_and_queries_sync(self) -> None:
        req = TaskSubmitRequest(
            goal="Refactor authentication layer",
            project_id="proj_test",
            priority=70,
            idempotency_key="idem-sync-01",
        )
        task = self.store.submit_task(req)
        self.assertEqual(task["status"], TaskState.SUBMITTED.value)
        self.assertEqual(task["priority"], 70)

        # Idempotency
        idempotent = self.store.submit_task(req)
        self.assertEqual(idempotent["task_id"], task["task_id"])

        # Fetch
        fetched = self.store.get_task(task["task_id"])
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["goal"], req.goal)

        # List
        tasks = self.store.list_tasks(status=TaskState.SUBMITTED.value)
        self.assertTrue(any(t["task_id"] == task["task_id"] for t in tasks))

    def test_task_intake_and_queries_async(self) -> None:
        async def run() -> None:
            req = TaskSubmitRequest(
                goal="Async intake verification",
                project_id="proj_async",
                priority=80,
                idempotency_key="idem-async-01",
            )
            task = await self.store.submit_task_async(req)
            self.assertEqual(task["status"], TaskState.SUBMITTED.value)

            fetched = await self.store.get_task_async(task["task_id"])
            self.assertIsNotNone(fetched)
            self.assertEqual(fetched["task_id"], task["task_id"])

            tasks = await self.store.list_tasks_async(limit=10)
            self.assertTrue(len(tasks) >= 1)

        asyncio.run(run())

    def test_state_transitions_and_cas_fencing(self) -> None:
        async def run() -> None:
            req = TaskSubmitRequest(goal="State machine validation", project_id="proj_state")
            task = await self.store.submit_task_async(req)
            task_id = task["task_id"]

            # SUBMITTED -> VALIDATING -> RUNNING -> VERIFYING -> SUCCEEDED
            t1 = await self.store.transition_task_async(
                task_id, TaskState.VALIDATING, reason_code="VALIDATING_INPUT", expected_version=1
            )
            self.assertEqual(t1["status"], TaskState.VALIDATING.value)
            self.assertEqual(t1["version"], 2)

            t2 = await self.store.transition_task_async(
                task_id, TaskState.RUNNING, reason_code="WORKER_DISPATCHED", expected_version=2
            )
            self.assertEqual(t2["status"], TaskState.RUNNING.value)
            self.assertEqual(t2["version"], 3)

            # Stale version CAS conflict
            with self.assertRaises(ConcurrencyFenceError):
                await self.store.transition_task_async(
                    task_id, TaskState.VERIFYING, reason_code="TEST", expected_version=2
                )

            # Illegal transition: RUNNING -> SUBMITTED violates state machine
            with self.assertRaises(InvalidTransitionError):
                await self.store.transition_task_async(
                    task_id, TaskState.SUBMITTED, reason_code="ILLEGAL"
                )

            t3 = await self.store.transition_task_async(
                task_id, TaskState.VERIFYING, reason_code="VERIFY_STARTED", expected_version=3
            )
            self.assertEqual(t3["status"], TaskState.VERIFYING.value)
            self.assertEqual(t3["version"], 4)

            # VERIFYING -> SUCCEEDED terminal
            t4 = await self.store.transition_task_async(
                task_id, TaskState.SUCCEEDED, reason_code="ALL_CHECKS_PASSED"
            )
            self.assertEqual(t4["status"], TaskState.SUCCEEDED.value)

        asyncio.run(run())

    def test_workers_leases_and_fencing_tokens(self) -> None:
        async def run() -> None:
            worker = await self.store.register_worker_async(
                worker_id="wrk_test_01",
                machine_class="m4_max",
                capabilities={"max_parallel": 8},
            )
            self.assertEqual(worker["worker_id"], "wrk_test_01")
            self.assertEqual(worker["capabilities"]["max_parallel"], 8)

            workers = await self.store.list_workers_async()
            self.assertTrue(any(w["worker_id"] == "wrk_test_01" for w in workers))

            req = TaskSubmitRequest(goal="Lease test task", project_id="proj_lease")
            task = await self.store.submit_task_async(req)
            task_id = task["task_id"]

            lease1 = await self.store.acquire_lease_and_attempt_async(
                task_id=task_id, worker_id="wrk_test_01"
            )
            self.assertEqual(lease1["fencing_token"], 1)

            # Monotonically increasing fencing token on second acquire
            lease2 = await self.store.acquire_lease_and_attempt_async(
                task_id=task_id, worker_id="wrk_test_01"
            )
            self.assertEqual(lease2["fencing_token"], 2)

            # Attempt updates
            updated_attempt = await self.store.update_attempt_async(
                lease2["attempt_id"],
                status="SUCCEEDED",
                head_tree_sha="sha_head_123",
            )
            self.assertEqual(updated_attempt["status"], "SUCCEEDED")
            self.assertEqual(updated_attempt["head_tree_sha"], "sha_head_123")

            attempts = await self.store.list_attempts_async(task_id)
            self.assertEqual(len(attempts), 2)

        asyncio.run(run())

    def test_plans_checkpoints_tool_calls_approvals_artifacts(self) -> None:
        async def run() -> None:
            req = TaskSubmitRequest(goal="Lifecycle validation", project_id="proj_life")
            task = await self.store.submit_task_async(req)
            task_id = task["task_id"]

            # Plan save & fetch & append blast radius
            plan = await self.store.save_plan_async(
                task_id=task_id,
                dag_nodes=[{"id": "step_1", "action": "test"}],
                blast_radius_checks=["tests/test_core.py"],
            )
            self.assertEqual(plan["revision"], 1)
            self.assertEqual(plan["blast_radius_checks"], ["tests/test_core.py"])

            appended_plan = await self.store.append_blast_radius_checks_async(
                task_id=task_id,
                checks=["tests/test_extra.py"],
                warning_summary="Downstream caller detected",
            )
            self.assertIn("tests/test_extra.py", appended_plan["blast_radius_checks"])

            latest_plan = await self.store.get_latest_plan_async(task_id)
            self.assertEqual(latest_plan["plan_id"], plan["plan_id"])

            # Register worker and acquire attempt for foreign keys
            await self.store.register_worker_async("wrk_chk_01")
            lease = await self.store.acquire_lease_and_attempt_async(task_id=task_id, worker_id="wrk_chk_01")
            attempt_id = lease["attempt_id"]

            # Checkpoint save & fetch
            chk = await self.store.create_checkpoint_async(
                task_id=task_id,
                attempt_id=attempt_id,
                plan_revision=1,
                completed_steps=["step_1"],
                pending_steps=[],
                workspace_tree_sha="sha_tree_abc",
                snapshot_path="/tmp/snapshots/chk1",
            )
            self.assertEqual(chk["validity"], "VALID")
            latest_chk = await self.store.get_latest_checkpoint_async(task_id)
            self.assertEqual(latest_chk["checkpoint_id"], chk["checkpoint_id"])

            # Tool call begin & seal
            call_id = await self.store.begin_tool_call_async(
                task_id=task_id,
                attempt_id=attempt_id,
                step_id="step_1",
                sequence_in_step=1,
                tool_id="bash",
                risk_class="R1",
                canonical_args_hash="sha_arg_123",
                arguments_redacted={"cmd": "ls"},
                policy_decision="ALLOW",
                idempotency_key="call-idem-001",
            )
            self.assertTrue(call_id.startswith("cal_"))

            sealed = await self.store.seal_tool_call_async(
                call_id=call_id,
                result={"exit_code": 0, "stdout": "ok"},
            )
            self.assertEqual(sealed["status"], "SEALED")

            found_call = await self.store.find_sealed_tool_call_async(
                task_id=task_id,
                step_id="step_1",
                tool_id="bash",
                canonical_args_hash="sha_arg_123",
                idempotency_key="call-idem-001",
            )
            self.assertIsNotNone(found_call)
            self.assertEqual(found_call["call_id"], call_id)

            tool_calls = await self.store.list_tool_calls_async(task_id)
            self.assertEqual(len(tool_calls), 1)

            # Approval create & decide
            apr = await self.store.create_approval_async(
                task_id=task_id,
                attempt_id=attempt_id,
                step_id="step_1",
                action_type="git_push",
                action_params={"remote": "origin", "branch": "main"},
                blast_radius_summary="Pushes to production branch",
            )
            self.assertEqual(apr["decision"], "PENDING")

            decided = await self.store.decide_approval_async(
                approval_id=apr["approval_id"],
                decision="APPROVED",
                decided_by="security_admin",
                comment="Authorized for CI release",
            )
            self.assertEqual(decided["decision"], "APPROVED")

            approvals = await self.store.list_approvals_async(task_id=task_id)
            self.assertEqual(len(approvals), 1)

            # Artifacts & Proof bundle
            art = await self.store.record_artifact_async(
                task_id=task_id,
                kind="diff_patch",
                uri="file:///tmp/diff.patch",
                sha256="sha_patch_123",
                size_bytes=1024,
            )
            self.assertEqual(art["kind"], "diff_patch")

            artifacts = await self.store.list_artifacts_async(task_id)
            self.assertEqual(len(artifacts), 1)

            bundle = await self.store.save_proof_bundle_async(
                task_id=task_id,
                attempt_id=attempt_id,
                git_diff_sha="diff_sha_abc",
                git_diff_text="--- a\n+++ b",
                workspace_tree_sha="sha_tree_abc",
                runtime_digest="sha_run_abc",
                ast_impact={"callers": ["test_billing.py"]},
                verification_results={"tests": "PASS"},
            )
            self.assertTrue(bundle["ledger_signature"].startswith("ed25519-hmac-sha256:"))

            fetched_bundle = await self.store.get_proof_bundle_async(task_id)
            self.assertIsNotNone(fetched_bundle)
            self.assertEqual(fetched_bundle["proof_id"], bundle["proof_id"])

        asyncio.run(run())


class TestHashChainingIntegrity(unittest.TestCase):
    """Verifies that cryptographic hash chaining on task_events maintains strict SHA256 integrity."""

    def setUp(self) -> None:
        self.store = LedgerStore(":memory:")

    def tearDown(self) -> None:
        self.store.close()

    def test_hash_chain_sequential_integrity(self) -> None:
        req = TaskSubmitRequest(goal="Cryptographic audit task", project_id="proj_hash")
        task = self.store.submit_task(req)
        task_id = task["task_id"]

        self.store.transition_task(task_id, TaskState.VALIDATING, reason_code="VALIDATING_INPUT")
        self.store.transition_task(task_id, TaskState.RUNNING, reason_code="DISPATCHED")
        self.store.record_event(task_id, "INTERMEDIATE_PROGRESS", reason_code="CHECKPOINT_SAVED")
        self.store.transition_task(task_id, TaskState.VERIFYING, reason_code="VERIFYING_CHECKS")
        self.store.transition_task(task_id, TaskState.SUCCEEDED, reason_code="GOAL_ATTAINED")

        events = self.store.list_events(task_id)
        self.assertEqual(len(events), 6)

        prev_hash = "0" * 64
        for ev in events:
            expected_sha = compute_event_hash(
                prev_hash=prev_hash,
                task_id=task_id,
                seq_no=ev["sequence_no"],
                event_type=ev["event_type"],
                prior_state=ev["prior_state"],
                next_state=ev["next_state"],
                reason_code=ev["reason_code"],
                workspace_tree_sha=ev["workspace_tree_sha"],
                payload_str=canonical_json(ev["payload"]),
                occurred_at=ev["occurred_at"],
            )
            self.assertEqual(
                ev["event_sha256"],
                expected_sha,
                f"Broken hash chain at sequence {ev['sequence_no']}",
            )
            prev_hash = ev["event_sha256"]


if __name__ == "__main__":
    unittest.main()
