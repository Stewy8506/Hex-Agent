"""Comprehensive Unit & Integration Test Suite for FTS5 Search, State Backup/Restore & Diagnostic Dump.

Covers:
1. FTS5 Search Engine (`TaskSearchIndex`, `sanitize_fts5_query`, BM25 ranking, snippet generation).
2. LedgerStore search synchronization (task creation, state transitions, tool call append, reindexing).
3. CLI search command (`hex search`, project filter, JSON output, reindex flag).
4. State Backup creation (`create_backup`, core inclusion, transient exclusion, quick mode).
5. State Restore (`restore_backup`, zip-slip path traversal defense, dry-run mode).
6. Diagnostic Support Dump (`generate_dump`, 7 required sections, secret scrubbing, error reporting).
7. CLI backup, restore, and dump subcommands (`hex backup`, `hex restore`, `hex dump`).
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from harness_core.backup import (
    SecurityViolationError,
    create_backup,
    generate_dump,
    restore_backup,
)
from harness_core.cli import main as cli_main
from harness_core.db import LedgerStore
from harness_core.models import TaskState, TaskSubmitRequest
from harness_core.search import (
    TaskSearchIndex,
    TaskSearchResult,
    sanitize_fts5_query,
)


class TestFTS5SearchEngine(unittest.TestCase):
    def test_fts5_virtual_table_creation(self) -> None:
        """Verify task_search_fts virtual table creation and tokenizer setup."""
        conn = sqlite3.connect(":memory:")
        index = TaskSearchIndex(conn)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='task_search_fts'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "task_search_fts")
        index.close()
        conn.close()

    def test_fts5_query_sanitization(self) -> None:
        """Verify sanitize_fts5_query strips dangerous operators, handles unmatched quotes, and supports prefix tokens."""
        # Dangerous special characters stripped
        raw = "Fix auth +{}:()^@/#&|~[]<>,;!?$=\\ error"
        sanitized = sanitize_fts5_query(raw)
        self.assertNotIn("+", sanitized)
        self.assertNotIn(":", sanitized)
        self.assertNotIn("{", sanitized)
        self.assertIn("Fix auth", sanitized)
        self.assertIn("error", sanitized)

        # Unmatched double quotes handled gracefully
        unmatched = 'unmatched "quote test'
        self.assertEqual(sanitize_fts5_query(unmatched), "unmatched quote test")

        # Balanced double quotes preserved as phrase
        balanced = 'fix "auth token" failure'
        self.assertEqual(sanitize_fts5_query(balanced), 'fix "auth token" failure')

        # Prefix wildcard matching preserved
        prefix = "auth*"
        self.assertEqual(sanitize_fts5_query(prefix), "auth*")

        # Repeated wildcards collapsed
        repeated = "auth***"
        self.assertEqual(sanitize_fts5_query(repeated), "auth*")

        # Leading wildcard stripped
        leading = "*auth"
        self.assertEqual(sanitize_fts5_query(leading), "auth")

        # Dangling boolean operators stripped
        self.assertEqual(sanitize_fts5_query("AND auth OR"), "auth")
        self.assertEqual(sanitize_fts5_query("NOT auth"), "auth")
        self.assertEqual(sanitize_fts5_query("AND OR NOT"), "")

        # Hyphenated / dotted words wrapped in quotes
        self.assertEqual(sanitize_fts5_query("auth-service"), '"auth-service"')

    def test_task_indexing_and_bm25_search(self) -> None:
        """Index multiple tasks with varied goals; assert exact match ranks higher and snippets highlight tags."""
        index = TaskSearchIndex(":memory:")
        index.index_task(
            task_id="tsk_1",
            project_id="proj_a",
            goal="Fix authentication token expiry in jwt service",
            status="COMPLETED",
            tool_calls_text="jwt verify",
        )
        index.index_task(
            task_id="tsk_2",
            project_id="proj_b",
            goal="Update CSS layout and typography for dashboard",
            status="RUNNING",
        )
        index.index_task(
            task_id="tsk_3",
            project_id="proj_a",
            goal="Implement user authentication profile page authentication",
            status="QUEUED",
        )

        # Search for 'authentication'
        results = index.search("authentication")
        self.assertEqual(len(results), 2)
        task_ids = [r.task_id for r in results]
        self.assertIn("tsk_1", task_ids)
        self.assertIn("tsk_3", task_ids)
        self.assertNotIn("tsk_2", task_ids)

        # Snippet tags verification
        self.assertIn("<b>", results[0].snippet)
        self.assertIn("</b>", results[0].snippet)
        self.assertEqual(results[0].matched_field, "goal")

        # Top result has highest BM25 score
        self.assertTrue(results[0].score >= results[1].score)

        # Project ID filtering
        proj_b_res = index.search("layout", project_id="proj_b")
        self.assertEqual(len(proj_b_res), 1)
        self.assertEqual(proj_b_res[0].task_id, "tsk_2")

        # Non-matching project
        proj_a_res = index.search("layout", project_id="proj_a")
        self.assertEqual(len(proj_a_res), 0)

        index.close()

    def test_tool_call_transcript_indexing(self) -> None:
        """Test append_tool_call and searching for specific tool arguments or error outputs."""
        index = TaskSearchIndex(":memory:")
        index.index_task("tsk_tool", "default", "Deploy cloud cluster", "RUNNING")

        # Append tool calls
        index.append_tool_call(
            "tsk_tool",
            "terraform_runner",
            '{"action": "apply", "target": "aws_iam_role"}',
            '{"status": "error", "code": 403, "msg": "access_denied_forbidden"}',
        )

        # Search by tool argument
        res_arg = index.search("aws_iam_role")
        self.assertEqual(len(res_arg), 1)
        self.assertEqual(res_arg[0].task_id, "tsk_tool")
        self.assertEqual(res_arg[0].matched_field, "tool_calls")
        self.assertIn("<b>aws_iam_role</b>", res_arg[0].snippet)

        # Search by error output
        res_err = index.search("access_denied_forbidden")
        self.assertEqual(len(res_err), 1)
        self.assertEqual(res_err[0].matched_field, "tool_calls")

        index.close()


class TestLedgerSearchIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="hex_search_ledger_")
        self.db_path = Path(self.tmp.name) / "hex.db"
        self.ledger = LedgerStore(self.db_path)

    def tearDown(self) -> None:
        if self.ledger.conn:
            self.ledger.conn.close()
        self.tmp.cleanup()

    def test_ledger_store_search_integration(self) -> None:
        """Initialize LedgerStore, submit tasks, transition state, seal tool calls, and execute search_tasks."""
        # 1. Create task with initial QUEUED status
        task = self.ledger.submit_task(
            {
                "status": "QUEUED",
                "goal": "Optimize PostgreSQL query execution plan with explain analyze",
                "project_id": "core_backend",
                "execution_mode": "standard",
            }
        )
        task_id = task["task_id"]

        # Search should find it immediately
        res1 = self.ledger.search_tasks("PostgreSQL")
        self.assertEqual(len(res1), 1)
        self.assertEqual(res1[0]["task_id"], task_id)
        self.assertEqual(res1[0]["status"], "QUEUED")

        # 2. Transition state (QUEUED -> RUNNING is a valid state transition)
        self.ledger.transition_task(task_id, TaskState.RUNNING)
        res2 = self.ledger.search_tasks("PostgreSQL")
        self.assertEqual(res2[0]["status"], "RUNNING")

        # 3. Seal tool call
        self.ledger.record_tool_call(
            call_id="call_99",
            task_id=task_id,
            tool_name="sql_explain",
            risk_class="R1",
            args={"query": "SELECT * FROM users WHERE active = true"},
        )
        self.ledger.seal_tool_call(
            call_id="call_99",
            result={"cost": 128.45, "engine": "pg_cost_evaluator"},
        )

        # Search for tool result
        res3 = self.ledger.search_tasks("pg_cost_evaluator")
        self.assertEqual(len(res3), 1)
        self.assertEqual(res3[0]["matched_field"], "tool_calls")
        self.assertIn("<b>pg_cost_evaluator</b>", res3[0]["snippet"])

    def test_fts5_reindex_all(self) -> None:
        """Test rebuilding the FTS5 index from historical ledger records."""
        # Submit multiple tasks
        self.ledger.submit_task(TaskSubmitRequest(goal="Task Alpha Database Refactor"))
        self.ledger.submit_task(TaskSubmitRequest(goal="Task Beta Webhook Integration"))
        self.ledger.submit_task(TaskSubmitRequest(goal="Task Gamma Cron Scheduler"))

        idx = self.ledger.search_index
        # Verify initial indexing
        self.assertEqual(len(idx.search("Alpha")), 1)

        # Manually clear FTS index
        with idx._lock:
            idx._conn.execute("DELETE FROM task_search_fts")
            idx._conn.commit()

        self.assertEqual(len(idx.search("Alpha")), 0)

        # Run reindex_all
        count = idx.reindex_all(self.ledger)
        self.assertEqual(count, 3)

        # Verify searches succeed again
        self.assertEqual(len(idx.search("Alpha")), 1)
        self.assertEqual(len(idx.search("Webhook")), 1)
        self.assertEqual(len(idx.search("Scheduler")), 1)

    def test_cli_search_subcommand(self) -> None:
        """Invoke hex search via CLI and assert formatted output and JSON mode."""
        self.ledger.submit_task(TaskSubmitRequest(goal="Implement oauth2 session authentication tokens"))

        # CLI text mode
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            code = cli_main(["--data-dir", str(self.tmp.name), "search", "oauth2"])
        self.assertEqual(code, 0)
        output = buf.getvalue()
        self.assertIn("Found 1 matching tasks", output)
        self.assertIn("TASK ID", output)

        # CLI JSON mode
        buf_json = io.StringIO()
        with patch("sys.stdout", buf_json):
            code_json = cli_main(["--data-dir", str(self.tmp.name), "search", "oauth2", "--json"])
        self.assertEqual(code_json, 0)
        parsed = json.loads(buf_json.getvalue())
        self.assertIsInstance(parsed, list)
        self.assertEqual(len(parsed), 1)
        self.assertIn("oauth2", parsed[0]["snippet"])

        # CLI no matches
        buf_none = io.StringIO()
        with patch("sys.stdout", buf_none):
            code_none = cli_main(["--data-dir", str(self.tmp.name), "search", "nonexistent_pattern_999"])
        self.assertEqual(code_none, 0)
        self.assertIn("No tasks found matching", buf_none.getvalue())


class TestStateBackupAndRestore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="hex_backup_test_")
        self.ws = Path(self.tmp.name)
        self.hex_dir = self.ws / ".hex"
        self.data_dir = self.hex_dir / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # Create core files
        (self.ws / "config.yaml").write_text("model_profile: local_m4", encoding="utf-8")
        (self.hex_dir / "config.yaml").write_text("version: 1.0", encoding="utf-8")
        (self.data_dir / "hex.db").write_text("MOCK SQLITE DB CONTENT", encoding="utf-8")
        (self.hex_dir / "schedules.json").write_text(
            json.dumps([{"name": "health_check", "cron_expression": "0 * * * *", "goal": "Check system health"}]),
            encoding="utf-8",
        )
        custom_skill = self.ws / "skills" / "custom" / "audit_tool"
        custom_skill.mkdir(parents=True, exist_ok=True)
        (custom_skill / "SKILL.md").write_text("# Audit Skill\nRuns audit.", encoding="utf-8")

        # Create transient files that must be excluded
        cow_dir = self.hex_dir / "cow_workspaces" / "ws_dirty"
        cow_dir.mkdir(parents=True, exist_ok=True)
        (cow_dir / "ephemeral.tmp").write_text("DIRTY COW DATA", encoding="utf-8")

        venv_dir = self.ws / ".venv" / "bin"
        venv_dir.mkdir(parents=True, exist_ok=True)
        (venv_dir / "python").write_text("MOCK PYTHON BINARY", encoding="utf-8")

        cache_dir = self.hex_dir / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "cached_ast.json").write_text("CACHE", encoding="utf-8")

        (self.data_dir / "hex.db-wal").write_text("WAL", encoding="utf-8")
        (self.data_dir / "test.lock").write_text("LOCK", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_create_backup_includes_core_excludes_transient(self) -> None:
        """Create backup; verify archive contains core state and excludes transient files."""
        res = create_backup(workspace_root=self.ws, data_dir=self.data_dir)
        backup_path = Path(res["backup_path"])
        self.assertTrue(backup_path.is_file())
        self.assertTrue(res["files_archived"] >= 4)
        self.assertTrue(len(res["sha256"]) == 64)

        with zipfile.ZipFile(backup_path, "r") as zf:
            names = zf.namelist()
            # Core files must be present
            self.assertTrue(any("config.yaml" in n for n in names))
            self.assertTrue(any("hex.db" in n for n in names))
            self.assertTrue(any("schedules.json" in n for n in names))
            self.assertTrue(any("SKILL.md" in n for n in names))

            # Excluded files must NOT be present
            self.assertFalse(any("cow_workspaces" in n for n in names))
            self.assertFalse(any(".venv" in n for n in names))
            self.assertFalse(any("/cache/" in n for n in names))
            self.assertFalse(any(n.endswith(".db-wal") for n in names))
            self.assertFalse(any(n.endswith(".lock") for n in names))

    def test_create_backup_quick_mode(self) -> None:
        """Verify quick=True archives only database and config files."""
        res = create_backup(workspace_root=self.ws, data_dir=self.data_dir, quick=True)
        with zipfile.ZipFile(res["backup_path"], "r") as zf:
            names = zf.namelist()
            self.assertTrue(any("hex.db" in n for n in names))
            self.assertTrue(any("config.yaml" in n for n in names))
            # Skill markdown should be skipped in quick mode
            self.assertFalse(any(n.endswith("SKILL.md") for n in names))

    def test_restore_backup_roundtrip(self) -> None:
        """Create backup, restore to target directory, and verify contents match originals."""
        res = create_backup(workspace_root=self.ws, data_dir=self.data_dir)
        restore_dir = self.ws / "restored_workbench"

        restore_res = restore_backup(archive_path=res["backup_path"], target_dir=restore_dir)
        self.assertFalse(restore_res["dry_run"])
        self.assertTrue(len(restore_res["restored_files"]) >= 4)

        # Check restored file contents
        restored_cfg = list(restore_dir.rglob("config.yaml"))
        self.assertTrue(len(restored_cfg) >= 1)
        self.assertIn("model_profile", restored_cfg[0].read_text(encoding="utf-8"))

        restored_sched = list(restore_dir.rglob("schedules.json"))
        self.assertTrue(len(restored_sched) >= 1)
        self.assertIn("health_check", restored_sched[0].read_text(encoding="utf-8"))

    def test_restore_backup_zip_slip_defense(self) -> None:
        """Construct malicious zip with path traversal; assert restore_backup raises SecurityViolationError."""
        bad_zip_path = self.ws / "malicious.zip"
        with zipfile.ZipFile(bad_zip_path, "w") as zf:
            zf.writestr("../traversal_pwned.txt", "ATTACK CONTENT")

        dest_dir = self.ws / "safe_target"
        with self.assertRaises((SecurityViolationError, ValueError)):
            restore_backup(archive_path=bad_zip_path, target_dir=dest_dir)

        # Verify traversal file was NOT extracted outside dest_dir
        self.assertFalse((self.ws / "traversal_pwned.txt").exists())

    def test_restore_backup_dry_run(self) -> None:
        """Verify dry_run=True returns file list without touching the destination directory."""
        res = create_backup(workspace_root=self.ws, data_dir=self.data_dir)
        dry_dir = self.ws / "dry_run_target"

        restore_res = restore_backup(archive_path=res["backup_path"], target_dir=dry_dir, dry_run=True)
        self.assertTrue(restore_res["dry_run"])
        self.assertTrue(len(restore_res["restored_files"]) >= 4)
        # Directory should not have files extracted
        if dry_dir.exists():
            self.assertEqual(len(list(dry_dir.iterdir())), 0)


class TestDiagnosticDump(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="hex_dump_test_")
        self.ws = Path(self.tmp.name)
        self.data_dir = self.ws / ".hex" / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.ledger = LedgerStore(self.data_dir / "hex.db")

    def tearDown(self) -> None:
        if self.ledger.conn:
            self.ledger.conn.close()
        self.tmp.cleanup()

    def test_generate_dump_sections(self) -> None:
        """Verify generated dump contains all 7 required diagnostic sections."""
        dump_text = generate_dump(workspace_root=self.ws, data_dir=self.data_dir)
        required_sections = [
            "1. HEADER & SYSTEM INFO",
            "2. HARDWARE & ACCELERATION",
            "3. STORAGE & LEDGER METRICS",
            "4. WORKSPACE & COW ISOLATION",
            "5. ACTIVE INTEGRATIONS & RECEPTORS",
            "6. SCHEDULES & CUSTOM SKILLS",
            "7. RECENT ERRORS",
        ]
        for section in required_sections:
            self.assertIn(section, dump_text, f"Missing section: {section}")

        self.assertIn("Python Version", dump_text)
        self.assertIn("Storage Path", dump_text)
        self.assertIn("Total Tasks", dump_text)

    def test_generate_dump_secret_redaction(self) -> None:
        """Verify API keys and tokens are never printed in plain text in dump output."""
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "sk-proj-super-secret-key-12345",
            "ANTHROPIC_API_KEY": "sk-ant-super-secret-key-67890",
        }):
            dump_text = generate_dump(workspace_root=self.ws, data_dir=self.data_dir)
            self.assertNotIn("sk-proj-super-secret-key-12345", dump_text)
            self.assertNotIn("sk-ant-super-secret-key-67890", dump_text)
            self.assertIn("OPENAI_API_KEY              : [CONFIGURED - REDACTED]", dump_text)
            self.assertIn("ANTHROPIC_API_KEY           : [CONFIGURED - REDACTED]", dump_text)

    def test_generate_dump_recent_failures(self) -> None:
        """Verify failed tasks and terminal reasons are included in the Recent Errors section."""
        # Create a task with RUNNING status and transition to FAILED
        t = self.ledger.submit_task({"status": "RUNNING", "goal": "Deploy buggy microservice"})
        self.ledger.transition_task(
            t["task_id"],
            TaskState.FAILED,
            terminal_reason="OOM_KILLED_OUT_OF_MEMORY",
        )

        dump_text = generate_dump(workspace_root=self.ws, data_dir=self.data_dir)
        self.assertIn("Deploy buggy microservice", dump_text)
        self.assertIn("OOM_KILLED_OUT_OF_MEMORY", dump_text)
        self.assertIn(t["task_id"], dump_text)

    def test_cli_backup_restore_dump_subcommands(self) -> None:
        """Verify hex backup, hex restore, and hex dump CLI commands execute successfully."""
        out_zip = self.ws / "manual_backup.zip"
        # 1. hex backup
        buf_bck = io.StringIO()
        with patch("sys.stdout", buf_bck):
            code_bck = cli_main([
                "backup",
                "--workspace", str(self.ws),
                "--output", str(out_zip),
            ])
        self.assertEqual(code_bck, 0)
        self.assertTrue(out_zip.is_file())
        self.assertIn("Backup created successfully", buf_bck.getvalue())

        # 2. hex restore --dry-run
        buf_rst_dry = io.StringIO()
        with patch("sys.stdout", buf_rst_dry):
            code_rst_dry = cli_main([
                "restore",
                str(out_zip),
                "--dry-run",
            ])
        self.assertEqual(code_rst_dry, 0)
        self.assertIn("Dry run inspection", buf_rst_dry.getvalue())

        # 3. hex restore
        restore_target = self.ws / "restored_dir"
        buf_rst = io.StringIO()
        with patch("sys.stdout", buf_rst):
            code_rst = cli_main([
                "restore",
                str(out_zip),
                "--target", str(restore_target),
            ])
        self.assertEqual(code_rst, 0)
        self.assertIn("Restore completed", buf_rst.getvalue())

        # 4. hex dump
        dump_out = self.ws / "diagnostic_dump.txt"
        buf_dmp = io.StringIO()
        with patch("sys.stdout", buf_dmp):
            code_dmp = cli_main([
                "dump",
                "--workspace", str(self.ws),
                "--output", str(dump_out),
            ])
        self.assertEqual(code_dmp, 0)
        self.assertTrue(dump_out.is_file())
        self.assertIn("Diagnostic dump saved", buf_dmp.getvalue())


if __name__ == "__main__":
    unittest.main()
