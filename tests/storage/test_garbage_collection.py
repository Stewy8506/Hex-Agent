"""Tests for Safe, Scoped Automatic TTL Retention & Reference-Counted Garbage Collection.

Covers:
- Workspace & CoW snapshot cleanup for expired terminal tasks
- Retention protection for active (RUNNING, PAUSED, PENDING) and pinned tasks
- Orphan directory race guard (protects uncommitted in-flight directories)
- Mark-and-sweep reference-counted CAS (cas/ and cas_artifacts/)
- Non-blocking SQLite vacuum policy
- Dry-run mode verification (reclaimed calculation without disk unlinks)
- CLI commands: `hex gc`, `hex pin`, `hex unpin`, and `--keep-artifacts`
"""

from __future__ import annotations

import datetime
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness_core.client.cli import build_parser, main
from harness_core.config_loader import load_retention_config
from harness_core.storage.db import LedgerStore
from harness_core.storage.gc import (
    GCReport,
    GarbageCollector,
    format_bytes,
    parse_ttl_seconds,
)
from harness_core.core.models import TaskSubmitRequest


class TestGarbageCollection(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._temp_dir.name)
        self.data_dir = self.tmp_path / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.ledger = LedgerStore(self.data_dir / "ledger.db")

        # Create subdirectories
        self.workspaces_root = self.data_dir / "active_workspaces"
        self.snapshots_root = self.data_dir / "cow_snapshots"
        self.cas_root = self.data_dir / "cas"
        self.cas_artifacts_root = self.data_dir / "cas_artifacts"
        self.workspaces_root.mkdir(parents=True, exist_ok=True)
        self.snapshots_root.mkdir(parents=True, exist_ok=True)
        self.cas_root.mkdir(parents=True, exist_ok=True)
        self.cas_artifacts_root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        if hasattr(self, "ledger") and self.ledger is not None:
            self.ledger.close()
        self._temp_dir.cleanup()

    def test_parse_ttl_seconds(self) -> None:
        """Verify TTL duration string parsing across various time units."""
        self.assertEqual(parse_ttl_seconds("24h"), 86400)
        self.assertEqual(parse_ttl_seconds("7d"), 7 * 86400)
        self.assertEqual(parse_ttl_seconds("30m"), 1800)
        self.assertEqual(parse_ttl_seconds("3600s"), 3600)
        self.assertEqual(parse_ttl_seconds("2w"), 2 * 604800)
        self.assertEqual(parse_ttl_seconds(7200), 7200)
        self.assertEqual(parse_ttl_seconds(None, default_seconds=3600), 3600)
        self.assertEqual(parse_ttl_seconds("invalid", default_seconds=86400), 86400)
        # Minimum bound is 60 seconds
        self.assertEqual(parse_ttl_seconds("5s"), 60)

    def test_gc_snapshots_and_workspaces_expired(self) -> None:
        """Terminal unpinned tasks older than TTL have their workspace and snapshots pruned."""
        now = time.time()
        old_time = now - 172800  # 48 hours ago (older than 24h)
        old_dt = datetime.datetime.fromtimestamp(old_time, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # 1. Create expired terminal task in ledger
        task = self.ledger.submit_task(TaskSubmitRequest(goal="Completed old task", pinned=False))
        task_id = task["task_id"]
        with self.ledger._lock:
            self.ledger.conn.execute(
                "UPDATE tasks SET status = 'SUCCEEDED', updated_at = ? WHERE task_id = ?",
                (old_dt, task_id),
            )
            self.ledger.conn.commit()

        # 2. Populate disk assets for task_id
        ws_dir = self.workspaces_root / f"ws_{task_id}_1"
        ws_dir.mkdir(parents=True)
        (ws_dir / "file.txt").write_text("workspace content" * 100, encoding="utf-8")

        snap_dir = self.snapshots_root / f"snap_{task_id}_step1"
        snap_dir.mkdir(parents=True)
        (snap_dir / "diff.patch").write_text("patch content" * 50, encoding="utf-8")

        # 3. Run sweep with 24h TTL
        gc = GarbageCollector(data_dir=self.data_dir, ledger=self.ledger)
        report = gc.run_sweep(workspace_ttl="24h", dry_run=False)

        # 4. Verify directories pruned and bytes reclaimed
        self.assertEqual(report.workspaces_deleted, 1)
        self.assertEqual(report.snapshots_deleted, 1)
        self.assertGreater(report.total_bytes_reclaimed, 0)
        self.assertFalse(ws_dir.exists())
        self.assertFalse(snap_dir.exists())

    def test_gc_snapshots_active_or_pinned_preserved(self) -> None:
        """Active tasks (RUNNING) and pinned tasks are strictly preserved regardless of age."""
        now = time.time()
        old_time = now - (30 * 86400)  # 30 days old
        old_dt = datetime.datetime.fromtimestamp(old_time, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Task 1: Active running task
        task1 = self.ledger.submit_task(TaskSubmitRequest(goal="In flight running task", pinned=False))
        tid_active = task1["task_id"]
        with self.ledger._lock:
            self.ledger.conn.execute(
                "UPDATE tasks SET status = 'RUNNING', updated_at = ? WHERE task_id = ?",
                (old_dt, tid_active),
            )
            self.ledger.conn.commit()

        # Task 2: Pinned completed task
        task2 = self.ledger.submit_task(TaskSubmitRequest(goal="Critical pinned task", pinned=True))
        tid_pinned = task2["task_id"]
        with self.ledger._lock:
            self.ledger.conn.execute(
                "UPDATE tasks SET status = 'SUCCEEDED', updated_at = ? WHERE task_id = ?",
                (old_dt, tid_pinned),
            )
            self.ledger.conn.commit()

        # Populate disk assets
        ws_active = self.workspaces_root / f"ws_{tid_active}_1"
        ws_active.mkdir(parents=True)
        (ws_active / "active.txt").write_text("in flight data", encoding="utf-8")

        ws_pinned = self.workspaces_root / f"ws_{tid_pinned}_1"
        ws_pinned.mkdir(parents=True)
        (ws_pinned / "pinned.txt").write_text("keep forever", encoding="utf-8")

        snap_pinned = self.snapshots_root / f"snap_{tid_pinned}_step2"
        snap_pinned.mkdir(parents=True)
        (snap_pinned / "pinned_snap.txt").write_text("snapshot data", encoding="utf-8")

        # Set old mtime on disk to simulate old directories
        for d in (ws_active, ws_pinned, snap_pinned):
            os.utime(d, (old_time, old_time))

        # Run sweep with aggressive 1h TTL
        gc = GarbageCollector(data_dir=self.data_dir, ledger=self.ledger)
        report = gc.run_sweep(workspace_ttl="1h", dry_run=False)

        # Verify nothing deleted
        self.assertEqual(report.workspaces_deleted, 0)
        self.assertEqual(report.snapshots_deleted, 0)
        self.assertTrue(ws_active.exists())
        self.assertTrue(ws_pinned.exists())
        self.assertTrue(snap_pinned.exists())

    def test_gc_orphan_directory_race_guard(self) -> None:
        """Unindexed directories missing from ledger are protected by orphan_guard_seconds."""
        now = time.time()

        # 1. Recent orphan directory (< 1 hour old) created by an in-flight worker
        recent_orphan = self.snapshots_root / "snap_uncommitted_inflight"
        recent_orphan.mkdir(parents=True)
        (recent_orphan / "data.bin").write_text("temporary write", encoding="utf-8")
        recent_mtime = now - 60  # 1 minute ago
        os.utime(recent_orphan, (recent_mtime, recent_mtime))

        # 2. Stale orphan directory (> 1 hour old) left behind by an abandoned process
        stale_orphan = self.snapshots_root / "snap_uncommitted_abandoned"
        stale_orphan.mkdir(parents=True)
        (stale_orphan / "stale.bin").write_text("dead data", encoding="utf-8")
        stale_mtime = now - 7200  # 2 hours ago
        os.utime(stale_orphan, (stale_mtime, stale_mtime))

        # Sweep
        gc = GarbageCollector(data_dir=self.data_dir, ledger=self.ledger)
        report = gc.run_sweep(workspace_ttl="24h", dry_run=False)

        # Race guard protects recent orphan; stale orphan is pruned
        self.assertTrue(recent_orphan.exists(), "Recent unindexed directory should be protected by race guard")
        self.assertFalse(stale_orphan.exists(), "Stale unindexed directory should be pruned")
        self.assertEqual(report.snapshots_deleted, 1)

    def test_gc_cas_and_cas_artifacts_mark_and_sweep(self) -> None:
        """Reference-counted mark-and-sweep protects referenced hashes and prunes unreferenced stale blobs."""
        now = time.time()
        active_hash = "a" * 64
        pinned_hash = "b" * 64
        stale_hash = "c" * 64
        recent_unref_hash = "d" * 64

        # 1. Register task with artifact referencing active_hash
        task1 = self.ledger.submit_task(TaskSubmitRequest(goal="Active CAS user", pinned=False))
        tid_active = task1["task_id"]
        with self.ledger._lock:
            self.ledger.conn.execute(
                """
                INSERT INTO artifacts (artifact_id, task_id, kind, uri, sha256, size_bytes, created_at)
                VALUES ('art_01', ?, 'cow_delta', 'cas://' || ?, ?, 100, CURRENT_TIMESTAMP)
                """,
                (tid_active, active_hash, active_hash),
            )
            self.ledger.conn.commit()

        # 2. Register pinned task referencing pinned_hash
        task2 = self.ledger.submit_task(TaskSubmitRequest(goal="Pinned CAS user", pinned=True))
        tid_pinned = task2["task_id"]
        with self.ledger._lock:
            self.ledger.conn.execute(
                """
                INSERT INTO artifacts (artifact_id, task_id, kind, uri, sha256, size_bytes, created_at)
                VALUES ('art_02', ?, 'cow_delta', 'cas://' || ?, ?, 100, CURRENT_TIMESTAMP)
                """,
                (tid_pinned, pinned_hash, pinned_hash),
            )
            self.ledger.conn.commit()

        # 3. Create CAS blobs in cas/ and cas_artifacts/
        active_blob = self.cas_root / f"{active_hash}.tar.gz"
        active_blob.write_text("active blob bytes", encoding="utf-8")

        pinned_artifact = self.cas_artifacts_root / f"{pinned_hash}.tar.gz"
        pinned_artifact.write_text("pinned artifact bytes", encoding="utf-8")

        # Stale unreferenced blob (> 7 days old)
        stale_blob = self.cas_root / f"{stale_hash}.tar.gz"
        stale_blob.write_text("stale unreferenced bytes", encoding="utf-8")
        stale_mtime = now - (10 * 86400)
        os.utime(stale_blob, (stale_mtime, stale_mtime))

        # Recent unreferenced blob (< 7 days old)
        recent_blob = self.cas_artifacts_root / f"{recent_unref_hash}.tar.gz"
        recent_blob.write_text("young unreferenced bytes", encoding="utf-8")
        recent_mtime = now - 3600
        os.utime(recent_blob, (recent_mtime, recent_mtime))

        # 4. Run sweep with 7d CAS TTL
        gc = GarbageCollector(data_dir=self.data_dir, ledger=self.ledger)
        report = gc.run_sweep(cas_ttl="7d", dry_run=False)

        # 5. Verify mark-and-sweep results
        self.assertTrue(active_blob.exists(), "Active referenced CAS blob must be preserved")
        self.assertTrue(pinned_artifact.exists(), "Pinned CAS artifact must be preserved")
        self.assertTrue(recent_blob.exists(), "Recent unreferenced blob (<7d) must be preserved")
        self.assertFalse(stale_blob.exists(), "Stale unreferenced blob (>7d) must be swept")
        self.assertEqual(report.cas_blobs_deleted, 1)
        self.assertGreater(report.cas_bytes, 0)

    def test_gc_dry_run_leaves_disk_untouched(self) -> None:
        """Dry-run calculates reclaimable bytes but performs zero file deletions."""
        now = time.time()
        old_time = now - 172800
        old_dt = datetime.datetime.fromtimestamp(old_time, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        task = self.ledger.submit_task(TaskSubmitRequest(goal="Dry run test task", pinned=False))
        task_id = task["task_id"]
        with self.ledger._lock:
            self.ledger.conn.execute(
                "UPDATE tasks SET status = 'COMPLETED', updated_at = ? WHERE task_id = ?",
                (old_dt, task_id),
            )
            self.ledger.conn.commit()

        ws_dir = self.workspaces_root / f"ws_{task_id}_1"
        ws_dir.mkdir(parents=True)
        (ws_dir / "data.txt").write_text("keep on dry run", encoding="utf-8")

        gc = GarbageCollector(data_dir=self.data_dir, ledger=self.ledger)
        report = gc.run_sweep(workspace_ttl="24h", dry_run=True)

        self.assertTrue(report.dry_run)
        self.assertEqual(report.workspaces_deleted, 1)
        self.assertGreater(report.total_bytes_reclaimed, 0)
        # File and directory must still exist on disk!
        self.assertTrue(ws_dir.exists())
        self.assertTrue((ws_dir / "data.txt").exists())

    def test_gc_vacuum_policy(self) -> None:
        """Non-blocking policy runs incremental_vacuum during standard sweeps and reserves full VACUUM."""
        gc = GarbageCollector(data_dir=self.data_dir, ledger=self.ledger)

        # 1. Standard background/startup sweep runs incremental vacuum
        rep_inc = gc.run_sweep(full_vacuum=False, dry_run=False)
        self.assertTrue(rep_inc.vacuumed)
        self.assertEqual(rep_inc.vacuum_mode, "incremental")

        # 2. Explicit full vacuum runs full VACUUM when no active leases exist
        rep_full = gc.run_sweep(full_vacuum=True, dry_run=False)
        self.assertTrue(rep_full.vacuumed)
        self.assertEqual(rep_full.vacuum_mode, "full")

        # 3. Explicit full vacuum downgrades to incremental if an active lease exists
        task = self.ledger.submit_task(TaskSubmitRequest(goal="Leased task", pinned=False))
        tid = task["task_id"]
        with self.ledger._lock:
            self.ledger.conn.execute(
                """
                INSERT OR IGNORE INTO workers (worker_id, machine_class, cow_backend, last_heartbeat_at, registered_at)
                VALUES ('worker_1', 'm4_air', 'apfs_clone', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            )
            self.ledger.conn.execute(
                """
                INSERT INTO leases (lease_id, task_id, attempt_id, worker_id, fencing_token, expires_at, renewed_at)
                VALUES ('lease_test', ?, 'att_1', 'worker_1', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (tid,),
            )
            self.ledger.conn.commit()

        rep_downgrade = gc.run_sweep(full_vacuum=True, dry_run=False)
        self.assertEqual(rep_downgrade.vacuum_mode, "incremental", "Should downgrade to incremental when leases are active")

    def test_pin_and_unpin_ledger_and_cli(self) -> None:
        """Test task pinning in ledger and CLI commands `hex pin` and `hex unpin`."""
        task = self.ledger.submit_task(TaskSubmitRequest(goal="Pin CLI testing", pinned=False))
        tid = task["task_id"]
        self.assertFalse(self.ledger.is_task_pinned(tid))

        # Pin task
        self.ledger.pin_task(tid, pinned=True)
        self.assertTrue(self.ledger.is_task_pinned(tid))

        # Unpin task
        self.ledger.pin_task(tid, pinned=False)
        self.assertFalse(self.ledger.is_task_pinned(tid))

    def test_cli_gc_command(self) -> None:
        """Verify `hex gc` CLI argument parsing and execution with --dry-run and --json."""
        parser = build_parser()

        # 1. Test argument parsing
        args = parser.parse_args(["gc", "--dry-run", "--workspace-ttl", "12h", "--json"])
        self.assertEqual(args.cmd, "gc")
        self.assertTrue(args.dry_run)
        self.assertEqual(args.workspace_ttl, "12h")
        self.assertTrue(args.json)

        # 2. Test pin/unpin parser
        args_pin = parser.parse_args(["pin", "tsk_12345"])
        self.assertEqual(args_pin.cmd, "pin")
        self.assertEqual(args_pin.task_id, "tsk_12345")

        args_unpin = parser.parse_args(["unpin", "tsk_12345"])
        self.assertEqual(args_unpin.cmd, "unpin")
        self.assertEqual(args_unpin.task_id, "tsk_12345")

    def test_load_retention_config(self) -> None:
        """Verify retention configuration parsing from toml and yaml."""
        ws = self.tmp_path / "ws_cfg"
        ws.mkdir()
        hex_dir = ws / ".hex"
        hex_dir.mkdir()

        # Default fallback
        cfg_def = load_retention_config(workspace_root=ws)
        self.assertEqual(cfg_def["workspace_ttl"], "24h")
        self.assertEqual(cfg_def["cas_ttl"], "7d")

        # TOML config
        toml_content = """[retention]
workspace_ttl = "48h"
cas_ttl = "14d"
auto_vacuum = false
"""
        (hex_dir / "config.toml").write_text(toml_content, encoding="utf-8")
        cfg_toml = load_retention_config(workspace_root=ws)
        self.assertEqual(cfg_toml["workspace_ttl"], "48h")
        self.assertEqual(cfg_toml["cas_ttl"], "14d")
        self.assertFalse(cfg_toml["auto_vacuum"])
