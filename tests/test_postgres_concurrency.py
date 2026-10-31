"""Multi-worker concurrency, SKIP LOCKED task claiming, and fencing token stress tests.

Verifies Phase 01 Plan 03:
1. Multi-Worker Queue Race: 10 concurrent workers drain 30 queued tasks with varying priorities.
   Exactly 30 claims succeed, 0 duplicates, and strict priority ordering is maintained.
2. Monotonic Fencing Token Validation: Successive lease acquisitions increment fencing tokens monotonically;
   stale commits and tool calls with superseded tokens raise ConcurrencyFenceError.
3. SKIP LOCKED SQL Construction & Verification: Validates FOR UPDATE SKIP LOCKED query formatting,
   and proves that locked candidate tasks are skipped immediately without blocking.
4. Async Coroutine Parity: 10 asyncio workers concurrently drain 30 queued tasks via claim_next_task_async.
5. Append-only Event Hash Chaining: Validates that each claim records a STATE_RUNNING event with valid SHA-256 chain.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from harness_core.db import (
    ConcurrencyFenceError,
    LedgerStore,
    compute_event_hash,
)
from harness_core.models import TaskState, TaskSubmitRequest


class TestConcurrentWorkerClaims(unittest.TestCase):
    """Stress tests concurrent task claiming across multi-threaded and async worker pools."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "concurrency_test.db"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_concurrent_worker_claims_no_duplicates(self) -> None:
        """10 worker threads concurrently claim 30 tasks with distinct priorities.

        Verifies:
        - Exactly 30 claims succeed.
        - Exactly 30 unique tasks are claimed (0 duplicates).
        - Highest priority tasks are claimed first (strictly descending priority order).
        - Worker pool gracefully terminates when queue is drained.
        """
        store = LedgerStore(self.db_path)
        try:
            # 1. Seed 30 tasks with priorities 1..30 in QUEUED state
            task_ids: list[str] = []
            for i in range(1, 31):
                req = TaskSubmitRequest(
                    goal=f"Concurrent task {i}",
                    project_id="concurrency_stress",
                    priority=i,
                    idempotency_key=f"idem-concurrency-{i}",
                )
                task = store.submit_task(req)
                t_id = task["task_id"]
                store.transition_task(t_id, TaskState.VALIDATING, reason_code="VALIDATING")
                store.transition_task(t_id, TaskState.QUEUED, reason_code="QUEUED")
                task_ids.append(t_id)

            self.assertEqual(len(task_ids), 30)

            # 2. Spawn 10 concurrent worker threads
            num_workers = 10
            barrier = threading.Barrier(num_workers)
            all_claims: list[dict[str, Any]] = []
            claims_lock = threading.Lock()
            claims_by_worker: dict[str, list[dict[str, Any]]] = {
                f"worker-{w}": [] for w in range(num_workers)
            }

            def worker_loop(worker_name: str) -> None:
                # Synchronize all threads at the starting line
                barrier.wait()
                while True:
                    claim = store.claim_next_task(worker_name)
                    if claim is None:
                        break
                    with claims_lock:
                        all_claims.append(claim)
                    claims_by_worker[worker_name].append(claim)

            threads = [
                threading.Thread(target=worker_loop, args=(f"worker-{w}",), name=f"WorkerThread-{w}")
                for w in range(num_workers)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10.0)
                self.assertFalse(t.is_alive(), f"Thread {t.name} timed out or deadlocked")

            # 3. Exactly 30 claims succeed
            self.assertEqual(len(all_claims), 30)

            # 4. Zero duplicate task claims (all 30 unique)
            claimed_task_ids = [c["task_id"] for c in all_claims]
            self.assertEqual(len(set(claimed_task_ids)), 30)
            self.assertEqual(set(claimed_task_ids), set(task_ids))

            # 5. Priority order: highest priorities claimed first
            claimed_priorities = [c["priority"] for c in all_claims]
            self.assertEqual(claimed_priorities, sorted(claimed_priorities, reverse=True))
            self.assertEqual(claimed_priorities, list(range(30, 0, -1)))

            # 6. Verify each claimed task state and metadata
            for claim in all_claims:
                self.assertEqual(claim["status"], "RUNNING")
                self.assertTrue(claim["attempt_id"].startswith("att_"))
                self.assertTrue(claim["lease_id"].startswith("lse_"))
                self.assertEqual(claim["fencing_token"], 1)

            # 7. Verify subsequent claim returns None (queue drained)
            self.assertIsNone(store.claim_next_task("worker-late"))
        finally:
            store.close()

    def test_concurrent_worker_claims_async_no_duplicates(self) -> None:
        """10 asyncio workers concurrently claim 30 tasks via claim_next_task_async."""
        store = LedgerStore(self.db_path)

        async def run_async_test() -> None:
            # 1. Seed 30 tasks with priorities 10..300 in QUEUED state
            task_ids: list[str] = []
            for i in range(1, 31):
                prio = i * 10
                req = TaskSubmitRequest(
                    goal=f"Async concurrent task {i}",
                    project_id="async_stress",
                    priority=prio,
                    idempotency_key=f"idem-async-{i}",
                )
                task = await store.submit_task_async(req)
                t_id = task["task_id"]
                await store.transition_task_async(t_id, TaskState.VALIDATING, reason_code="VALIDATING")
                await store.transition_task_async(t_id, TaskState.QUEUED, reason_code="QUEUED")
                task_ids.append(t_id)

            all_claims: list[dict[str, Any]] = []
            claims_lock = asyncio.Lock()
            start_event = asyncio.Event()

            async def async_worker(worker_name: str) -> None:
                await start_event.wait()
                while True:
                    claim = await store.claim_next_task_async(worker_name)
                    if claim is None:
                        break
                    async with claims_lock:
                        all_claims.append(claim)

            workers = [asyncio.create_task(async_worker(f"async-worker-{w}")) for w in range(10)]
            start_event.set()
            await asyncio.gather(*workers)

            # Assertions
            self.assertEqual(len(all_claims), 30)
            claimed_task_ids = [c["task_id"] for c in all_claims]
            self.assertEqual(len(set(claimed_task_ids)), 30)
            self.assertEqual(set(claimed_task_ids), set(task_ids))

            claimed_priorities = [c["priority"] for c in all_claims]
            self.assertEqual(claimed_priorities, sorted(claimed_priorities, reverse=True))

            # Queue empty
            self.assertIsNone(await store.claim_next_task_async("worker-empty"))

        try:
            asyncio.run(run_async_test())
        finally:
            store.close()

    def test_empty_queue_returns_none(self) -> None:
        """Calling claim_next_task on an empty ledger returns None immediately."""
        store = LedgerStore(":memory:")
        try:
            self.assertIsNone(store.claim_next_task("worker-0"))
            self.assertIsNone(asyncio.run(store.claim_next_task_async("worker-0")))
        finally:
            store.close()


class TestMonotonicFencingTokenEnforcement(unittest.TestCase):
    """Verifies that lease acquisition increments fencing tokens and rejects stale commits."""

    def setUp(self) -> None:
        self.store = LedgerStore(":memory:")
        req = TaskSubmitRequest(goal="Fencing token test task", priority=50)
        task = self.store.submit_task(req)
        self.task_id = task["task_id"]
        self.store.transition_task(self.task_id, TaskState.VALIDATING, reason_code="VALIDATING")
        self.store.transition_task(self.task_id, TaskState.QUEUED, reason_code="QUEUED")

    def tearDown(self) -> None:
        self.store.close()

    def test_monotonic_fencing_token_enforcement(self) -> None:
        """Successive lease acquisitions increment fencing token; stale commits are rejected."""
        t_id = self.task_id

        # 1. Acquire lease 1
        l1 = self.store.acquire_lease_and_attempt(t_id, "worker-1")
        self.assertEqual(l1["fencing_token"], 1)

        # 2. Acquire lease 2 on same task (simulates lease expiration or re-assignment)
        l2 = self.store.acquire_lease_and_attempt(t_id, "worker-2")
        self.assertEqual(l2["fencing_token"], 2)

        # 3. Acquire lease 3 on same task
        l3 = self.store.acquire_lease_and_attempt(t_id, "worker-3")
        self.assertEqual(l3["fencing_token"], 3)

        # 4. Verify stale worker 1 state transition is rejected
        with self.assertRaises(ConcurrencyFenceError) as ctx1:
            self.store.transition_task(
                t_id,
                TaskState.RUNNING,
                reason_code="STALE_WORKER_1",
                fencing_token=1,
                attempt_id=l1["attempt_id"],
            )
        self.assertIn("Stale fencing token 1 < active token 3", str(ctx1.exception))

        # 5. Verify stale worker 2 state transition is rejected
        with self.assertRaises(ConcurrencyFenceError) as ctx2:
            self.store.transition_task(
                t_id,
                TaskState.RUNNING,
                reason_code="STALE_WORKER_2",
                fencing_token=2,
                attempt_id=l2["attempt_id"],
            )
        self.assertIn("Stale fencing token 2 < active token 3", str(ctx2.exception))

        # 6. Verify stale worker 1 cannot begin tool calls
        with self.assertRaises(ConcurrencyFenceError) as ctx3:
            self.store.begin_tool_call(
                task_id=t_id,
                attempt_id=l1["attempt_id"],
                step_id="step-1",
                sequence_in_step=1,
                tool_id="test_tool",
                risk_class="R0",
                canonical_args_hash="hash_01",
                arguments_redacted={},
                policy_decision="ALLOW",
                idempotency_key="call-stale-01",
                fencing_token=1,
            )
        self.assertIn("Stale fencing token 1 < active token 3", str(ctx3.exception))

        # 7. Verify active worker 3 state transition succeeds
        updated = self.store.transition_task(
            t_id,
            TaskState.RUNNING,
            reason_code="ACTIVE_WORKER_3",
            fencing_token=3,
            attempt_id=l3["attempt_id"],
        )
        self.assertEqual(updated["status"], TaskState.RUNNING.value)

        # 8. Verify active worker 3 can begin and seal tool calls
        call_id = self.store.begin_tool_call(
            task_id=t_id,
            attempt_id=l3["attempt_id"],
            step_id="step-1",
            sequence_in_step=1,
            tool_id="test_tool",
            risk_class="R0",
            canonical_args_hash="hash_03",
            arguments_redacted={"arg": "val"},
            policy_decision="ALLOW",
            idempotency_key="call-active-03",
            fencing_token=3,
        )
        self.assertTrue(call_id.startswith("cal_"))

        # Verify stale token on seal_tool_call raises ConcurrencyFenceError
        with self.assertRaises(ConcurrencyFenceError):
            self.store.seal_tool_call(
                call_id,
                {"output": "stale"},
                fencing_token=1,
            )

        # Active token on seal_tool_call succeeds
        sealed = self.store.seal_tool_call(
            call_id,
            {"output": "valid"},
            fencing_token=3,
        )
        self.assertEqual(sealed["status"], "SEALED")

    def test_monotonic_fencing_token_enforcement_async(self) -> None:
        """Verifies monotonic fencing token enforcement across async APIs."""
        t_id = self.task_id

        async def run_async_fencing() -> None:
            l1 = await self.store.acquire_lease_and_attempt_async(t_id, "worker-async-1")
            l2 = await self.store.acquire_lease_and_attempt_async(t_id, "worker-async-2")
            self.assertEqual(l1["fencing_token"], 1)
            self.assertEqual(l2["fencing_token"], 2)

            # Stale commit raises ConcurrencyFenceError
            with self.assertRaises(ConcurrencyFenceError):
                await self.store.transition_task_async(
                    t_id,
                    TaskState.RUNNING,
                    reason_code="STALE_ASYNC_1",
                    fencing_token=1,
                    attempt_id=l1["attempt_id"],
                )

            # Active commit succeeds
            res = await self.store.transition_task_async(
                t_id,
                TaskState.RUNNING,
                reason_code="ACTIVE_ASYNC_2",
                fencing_token=2,
                attempt_id=l2["attempt_id"],
            )
            self.assertEqual(res["status"], TaskState.RUNNING.value)

            # Tool call with stale token raises ConcurrencyFenceError
            with self.assertRaises(ConcurrencyFenceError):
                await self.store.begin_tool_call_async(
                    task_id=t_id,
                    attempt_id=l1["attempt_id"],
                    step_id="step-1",
                    sequence_in_step=1,
                    tool_id="test_tool",
                    risk_class="R0",
                    canonical_args_hash="hash_a1",
                    arguments_redacted={},
                    policy_decision="ALLOW",
                    idempotency_key="call-stale-async",
                    fencing_token=1,
                )

        asyncio.run(run_async_fencing())


class TestSkipLockedQueryStructure(unittest.TestCase):
    """Verifies PostgreSQL FOR UPDATE SKIP LOCKED query formatting and non-blocking skip behavior."""

    def test_skip_locked_query_structure(self) -> None:
        """Validates that PostgreSQL query explicitly formats SELECT ... FOR UPDATE SKIP LOCKED."""
        query = LedgerStore.CLAIM_TASK_POSTGRES_SQL
        self.assertIn("SELECT task_id, priority, version", query)
        self.assertIn("WHERE status = 'QUEUED'", query)
        self.assertIn("ORDER BY priority DESC, created_at ASC", query)
        self.assertIn("FOR UPDATE SKIP LOCKED", query)
        self.assertIn("LIMIT 1", query)

        # Also inspect method source code
        source = inspect.getsource(LedgerStore.claim_next_task_async)
        self.assertIn("CLAIM_TASK_POSTGRES_SQL", source)
        self.assertIn("conn.transaction()", source)

    def test_skip_locked_simulated_non_blocking_skip(self) -> None:
        """Simulates concurrent PostgreSQL connections where one connection locks a task.

        Verifies:
        - When Connection 1 holds a lock on Task 1 (priority 100),
          a secondary worker query immediately skips Task 1 without blocking
          and claims Task 2 (priority 90).
        """
        # Simulated database state representing tasks, leases, attempts, task_events
        db_state: dict[str, Any] = {
            "tasks": {
                "tsk_01": {
                    "task_id": "tsk_01",
                    "status": "QUEUED",
                    "priority": 100,
                    "version": 1,
                    "active_workspace_tree_sha": "sha_ws_01",
                    "policy_snapshot_id": "pol-v1.3",
                    "created_at": "2026-10-31T12:00:00Z",
                    "updated_at": "2026-10-31T12:00:00Z",
                },
                "tsk_02": {
                    "task_id": "tsk_02",
                    "status": "QUEUED",
                    "priority": 90,
                    "version": 1,
                    "active_workspace_tree_sha": "sha_ws_02",
                    "policy_snapshot_id": "pol-v1.3",
                    "created_at": "2026-10-31T12:01:00Z",
                    "updated_at": "2026-10-31T12:01:00Z",
                },
            },
            "leases": [],
            "attempts": [],
            "task_events": [],
        }
        active_locks: set[str] = set()

        class MockAsyncpgConn:
            def __init__(self, state: dict[str, Any], locks: set[str]):
                self.state = state
                self.locks = locks
                self.held: set[str] = set()

            def transaction(self):
                conn = self
                class Tx:
                    async def __aenter__(self):
                        return conn
                    async def __aexit__(self, exc_type, exc_val, exc_tb):
                        for k in conn.held:
                            conn.locks.discard(k)
                        conn.held.clear()
                return Tx()

            async def fetchrow(self, query: str, *args: Any):
                if "FOR UPDATE SKIP LOCKED" in query:
                    # Select highest priority QUEUED task not in active_locks
                    candidates = [
                        t for t in self.state["tasks"].values()
                        if t["status"] == "QUEUED" and t["task_id"] not in self.locks
                    ]
                    if not candidates:
                        return None
                    candidates.sort(key=lambda t: (-t["priority"], t["created_at"]))
                    chosen = candidates[0]
                    self.locks.add(chosen["task_id"])
                    self.held.add(chosen["task_id"])
                    return dict(chosen)
                elif "SELECT COALESCE(MAX(fencing_token), 0) AS max_tok FROM leases" in query:
                    task_id = args[0]
                    leases = [l for l in self.state["leases"] if l["task_id"] == task_id]
                    max_tok = max([l["fencing_token"] for l in leases], default=0)
                    return {"max_tok": max_tok}
                elif "SELECT * FROM tasks WHERE task_id = $1" in query:
                    return dict(self.state["tasks"].get(args[0], {}))
                elif "SELECT sequence_no, event_sha256 FROM task_events" in query:
                    evts = [e for e in self.state["task_events"] if e["task_id"] == args[0]]
                    if not evts:
                        return None
                    last_evt = sorted(evts, key=lambda e: e["sequence_no"])[-1]
                    return {"sequence_no": last_evt["sequence_no"], "event_sha256": last_evt["event_sha256"]}
                return None

            async def execute(self, query: str, *args: Any):
                if "UPDATE leases SET released_at = $1" in query:
                    return "UPDATE 1"
                elif "INSERT INTO attempts" in query:
                    self.state["attempts"].append({"attempt_id": args[0], "task_id": args[1]})
                    return "INSERT 0 1"
                elif "INSERT INTO leases" in query:
                    self.state["leases"].append({
                        "lease_id": args[0], "task_id": args[1], "fencing_token": args[4]
                    })
                    return "INSERT 0 1"
                elif "UPDATE tasks" in query:
                    task_id = args[2]
                    t = self.state["tasks"][task_id]
                    t["status"] = "RUNNING"
                    t["version"] += 1
                    return "UPDATE 1"
                elif "INSERT INTO task_events" in query:
                    self.state["task_events"].append({
                        "event_id": args[0], "task_id": args[1], "sequence_no": args[3],
                        "event_sha256": args[14]
                    })
                    return "INSERT 0 1"
                return "OK"

        class MockPool:
            def acquire(self):
                class Ctx:
                    async def __aenter__(self):
                        return MockAsyncpgConn(db_state, active_locks)
                    async def __aexit__(self, *args):
                        pass
                return Ctx()

        # Simulate Connection 1 holding a lock on task 'tsk_01'
        active_locks.add("tsk_01")

        # Now worker 2 executes claim_next_task_async on a Postgres store configured with MockPool
        store = LedgerStore("postgresql://mock:pass@127.0.0.1:5432/mock_db")

        async def run_skip_locked():
            store._pool = MockPool()
            store._pool_loop = asyncio.get_running_loop()
            claim = await store.claim_next_task_async("worker-2")
            return claim

        claimed = asyncio.run(run_skip_locked())
        # Assert worker 2 immediately skipped locked tsk_01 and claimed tsk_02
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["task_id"], "tsk_02")
        self.assertEqual(claimed["priority"], 90)
        self.assertEqual(claimed["status"], "RUNNING")
        self.assertEqual(claimed["worker_id"], "worker-2")


class TestTaskQueueClaimEventIntegrity(unittest.TestCase):
    """Verifies that claimed tasks have cryptographically valid event chains."""

    def test_claimed_task_event_hash_chain_integrity(self) -> None:
        """Verifies that queue claiming appends a valid STATE_RUNNING event."""
        store = LedgerStore(":memory:")
        try:
            req = TaskSubmitRequest(goal="Event integrity test task", priority=40)
            task = store.submit_task(req)
            t_id = task["task_id"]
            store.transition_task(t_id, TaskState.VALIDATING, reason_code="VALIDATING")
            store.transition_task(t_id, TaskState.QUEUED, reason_code="QUEUED")

            # Claim task
            claim = store.claim_next_task("worker-validator")
            self.assertIsNotNone(claim)

            # Verify events
            events = store.list_events(t_id)
            self.assertEqual(len(events), 4)

            # Events sequence:
            # 1: STATE_SUBMITTED
            # 2: STATE_VALIDATING
            # 3: STATE_QUEUED
            # 4: STATE_RUNNING (QUEUE_CLAIMED)
            claim_event = events[3]
            self.assertEqual(claim_event["event_type"], "STATE_RUNNING")
            self.assertEqual(claim_event["prior_state"], "QUEUED")
            self.assertEqual(claim_event["next_state"], "RUNNING")
            self.assertEqual(claim_event["reason_code"], "QUEUE_CLAIMED")
            self.assertEqual(claim_event["actor"], "worker-validator")
            self.assertEqual(claim_event["sequence_no"], 4)

            # Verify cryptographic hash of the claim event
            prev_event = events[2]
            computed_sha = compute_event_hash(
                prev_hash=prev_event["event_sha256"],
                task_id=t_id,
                seq_no=4,
                event_type="STATE_RUNNING",
                prior_state="QUEUED",
                next_state="RUNNING",
                reason_code="QUEUE_CLAIMED",
                workspace_tree_sha=claim_event["workspace_tree_sha"],
                payload_str=claim_event.get("payload_json") or "{}",
                occurred_at=claim_event["occurred_at"],
            )
            # Both hashes must match exactly
            self.assertEqual(claim_event["event_sha256"], computed_sha)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
