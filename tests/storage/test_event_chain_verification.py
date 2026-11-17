"""Unit and integration tests for LedgerStore hash chain integrity and pre-commit verification script (GAP-03)."""

from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

from harness_core.storage.db import LedgerStore
from harness_core.core.models import TaskSubmitRequest

# Import script runner directly
from tests import REPO_ROOT as _repo_root
_scripts_dir = _repo_root / "scripts"
if str(_scripts_dir) not in sys.path:
    sys.path.insert(0, str(_scripts_dir))

from verify_event_chain import run_verification


class TestEventChainIntegrity(unittest.TestCase):
    """Verifies cryptographic event chain auditing and pre-commit verification script."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_ledger.db"
        self.store = LedgerStore(self.db_path)

    def tearDown(self) -> None:
        if self.store.conn:
            self.store.conn.close()
        self.temp_dir.cleanup()

    def test_ledger_verify_chain_integrity_empty_ledger(self) -> None:
        """Empty ledger has zero events and reports valid integrity."""
        report = self.store.verify_chain_integrity()
        self.assertTrue(report.valid)
        self.assertEqual(report.verified_events, 0)
        self.assertEqual(report.tasks_checked, 0)
        self.assertEqual(len(report.errors), 0)

    def test_ledger_verify_chain_integrity_valid_events(self) -> None:
        """Ledger with multi-turn event history verifies cryptographic chain intact."""
        task1 = self.store.submit_task(TaskSubmitRequest(goal="Task 1"))
        t1_id = task1["task_id"]
        self.store.record_event(t1_id, "RUNNING", reason_code="STARTED", payload={"step": 1})
        self.store.record_event(t1_id, "TOOL_CALL", reason_code="EXEC", payload={"cmd": "ls"})
        self.store.record_event(t1_id, "SUCCEEDED", reason_code="COMPLETED", payload={"exit": 0})

        task2 = self.store.submit_task(TaskSubmitRequest(goal="Task 2"))
        t2_id = task2["task_id"]
        self.store.record_event(t2_id, "RUNNING", reason_code="STARTED", payload={"step": 1})

        report = self.store.verify_chain_integrity()
        self.assertTrue(report.valid)
        self.assertTrue(bool(report))
        self.assertEqual(report.tasks_checked, 2)
        # Task 1: SUBMITTED (seq 1), RUNNING (seq 2), TOOL_CALL (seq 3), SUCCEEDED (seq 4) = 4 events
        # Task 2: SUBMITTED (seq 1), RUNNING (seq 2) = 2 events -> total 6 events
        self.assertEqual(report.verified_events, 6)
        self.assertEqual(len(report.errors), 0)

    def test_ledger_verify_chain_integrity_detects_payload_tampering(self) -> None:
        """Tampering with an event payload invalidates cryptographic hash."""
        task = self.store.submit_task(TaskSubmitRequest(goal="Tamper test"))
        tid = task["task_id"]
        self.store.record_event(tid, "RUNNING", reason_code="STARTED", payload={"secret": "abc"})
        self.store.record_event(tid, "SUCCEEDED", reason_code="DONE", payload={"res": 42})

        # Pre-tamper check
        rep_pre = self.store.verify_chain_integrity()
        self.assertTrue(rep_pre.valid)

        # Directly tamper with SQLite payload without updating SHA-256
        self.store.conn.execute(
            "UPDATE task_events SET payload_json = ? WHERE task_id = ? AND sequence_no = 2",
            ("{\"secret\": \"tampered_hacked\"}", tid),
        )
        self.store.conn.commit()

        # Post-tamper check
        rep_post = self.store.verify_chain_integrity()
        self.assertFalse(rep_post.valid)
        self.assertFalse(bool(rep_post))
        self.assertTrue(len(rep_post.errors) >= 1)
        self.assertTrue(any("hash mismatch" in err for err in rep_post.errors))

    def test_ledger_verify_chain_integrity_detects_sequence_gap(self) -> None:
        """Altering sequence numbers to create a discontinuity is detected."""
        task = self.store.submit_task(TaskSubmitRequest(goal="Discontinuity test"))
        tid = task["task_id"]
        self.store.record_event(tid, "RUNNING", reason_code="STARTED")
        self.store.record_event(tid, "VERIFYING", reason_code="CHECK")

        # Corrupt sequence_no from 3 to 5
        self.store.conn.execute(
            "UPDATE task_events SET sequence_no = 5 WHERE task_id = ? AND sequence_no = 3",
            (tid,),
        )
        self.store.conn.commit()

        rep = self.store.verify_chain_integrity()
        self.assertFalse(rep.valid)
        self.assertTrue(any("sequence discontinuity" in err for err in rep.errors))

    def test_verify_event_chain_script_run_verification_success(self) -> None:
        """verify_event_chain.run_verification returns 0 on valid database."""
        task = self.store.submit_task(TaskSubmitRequest(goal="Script test"))
        tid = task["task_id"]
        self.store.record_event(tid, "RUNNING", reason_code="OK")

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = run_verification(
                db_url=f"sqlite:///{self.db_path}",
                json_output=True,
            )
        self.assertEqual(code, 0)
        report = json.loads(buf.getvalue())
        self.assertEqual(report["status"], "VALID")
        self.assertTrue(report["chain_valid"])
        self.assertEqual(report["verified_events"], 2)

    def test_verify_event_chain_script_run_verification_tampered(self) -> None:
        """verify_event_chain.run_verification returns 1 and tamper details when compromised."""
        task = self.store.submit_task(TaskSubmitRequest(goal="Script tamper test"))
        tid = task["task_id"]
        self.store.record_event(tid, "RUNNING", reason_code="OK")

        # Tamper hash in database
        self.store.conn.execute(
            "UPDATE task_events SET event_sha256 = '0123456789abcdef' WHERE task_id = ? AND sequence_no = 2",
            (tid,),
        )
        self.store.conn.commit()

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = run_verification(
                db_url=f"sqlite:///{self.db_path}",
                json_output=True,
            )
        self.assertEqual(code, 1)
        report = json.loads(buf.getvalue())
        self.assertEqual(report["status"], "COMPROMISED")
        self.assertFalse(report["chain_valid"])
        self.assertTrue(len(report["errors"]) > 0)

    def test_verify_event_chain_script_cli_executable(self) -> None:
        """Executing scripts/verify_event_chain.py directly via subprocess succeeds."""
        script_path = _repo_root / "scripts" / "verify_event_chain.py"
        res = subprocess.run(
            [sys.executable, str(script_path), "--db-url", f"sqlite:///{self.db_path}", "--json"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Script failed: {res.stderr}")
        report = json.loads(res.stdout)
        self.assertEqual(report["status"], "VALID")


if __name__ == "__main__":
    unittest.main()
