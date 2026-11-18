"""B42 regression: a claim that cannot open its transaction fails visibly.

The claim path swallowed ``BEGIN IMMEDIATE`` failures and then ran the claim
statement outside a transaction, so a busy database looked like an empty queue.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any

from harness_core.storage.db import ClaimTransactionError, LedgerStore


class FlakyConnection:
    """Delegates to a real connection, failing the first N BEGIN IMMEDIATE calls."""

    def __init__(self, real: sqlite3.Connection, failures: int) -> None:
        self._real = real
        self.failures = failures

    def execute(self, sql: str, *params: Any) -> Any:
        if sql.strip().upper().startswith("BEGIN IMMEDIATE") and self.failures > 0:
            self.failures -= 1
            raise sqlite3.OperationalError("database is locked")
        return self._real.execute(sql, *params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class TestClaimTransactionFailure(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="harness_claim_txn_"))
        self.ledger = LedgerStore(self.tmp_dir / "ledger.db")
        self.real_conn = self.ledger.conn

    def tearDown(self) -> None:
        self.ledger.conn = self.real_conn
        self.ledger.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_locked_database_raises_instead_of_returning_no_work(self) -> None:
        task = self.ledger.submit_task({"goal": "queued work", "execution_mode": "standard"})
        self.ledger.conn = FlakyConnection(self.real_conn, failures=10)

        with self.assertRaises(ClaimTransactionError):
            self.ledger.claim_next_task(worker_id="worker-a")

        self.ledger.conn = self.real_conn
        self.assertEqual(self.ledger.get_task(task["task_id"])["status"], "QUEUED")

    def test_transient_lock_is_retried_and_the_task_is_claimed(self) -> None:
        task = self.ledger.submit_task({"goal": "queued work", "execution_mode": "standard"})
        self.ledger.conn = FlakyConnection(self.real_conn, failures=1)

        claimed = self.ledger.claim_next_task(worker_id="worker-a")

        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["task_id"], task["task_id"])
        self.assertEqual(self.ledger.get_task(task["task_id"])["status"], "RUNNING")


if __name__ == "__main__":
    unittest.main()
