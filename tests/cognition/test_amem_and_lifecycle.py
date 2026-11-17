"""Tests for Zettelkasten A-MEM Controller, Memory Lifecycle & Lightweight Rollback."""

import shutil
import tempfile
import time
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness_core.client.cli import main as cli_main
from harness_core.cognition.memory import (
    AMEMController,
    CuratedMemoryStore,
    HistorySnapshotManager,
    MemoryItem,
    MemoryLifecycleManager,
    extract_wiki_tags,
)
from harness_core.client.tui import HexConsole


class TestAMEMController(unittest.TestCase):
    """Test A-MEM CRUD operations, contradiction resolution, and wiki-style concept links."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.workspace = Path(self.temp_dir) / "workspace"
        self.global_home = Path(self.temp_dir) / "global_home"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.global_home.mkdir(parents=True, exist_ok=True)

        self.store = CuratedMemoryStore(
            workspace_root=self.workspace,
            global_home=self.global_home,
        )
        self.controller = AMEMController(
            workspace_root=self.workspace,
            global_home=self.global_home,
            store=self.store,
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_amem_crud(self):
        # 1. ADD
        res = self.controller.add_memory("Database engine is SQLite [[database-config]]", target="memory")
        self.assertTrue(res["ok"])
        self.assertEqual(res["action"], "ADD")
        item_id = res["item"]["id"]
        self.assertIn("database-config", res["item"]["tags"])

        # Verify get_memory
        item = self.controller.get_memory(item_id)
        self.assertIsNotNone(item)
        self.assertEqual(item.text, "Database engine is SQLite [[database-config]]")

        # Verify list_memories
        all_mem = self.controller.list_memories("memory")
        self.assertEqual(len(all_mem), 1)

        # 2. NOOP duplicate
        res_dup = self.controller.add_memory("Database engine is SQLite [[database-config]]", target="memory")
        self.assertTrue(res_dup["ok"])
        self.assertEqual(res_dup["action"], "NOOP")
        # hit_counter incremented
        updated_item = self.controller.get_memory(item_id)
        self.assertGreaterEqual(updated_item.hit_counter, 2)

        # 3. UPDATE
        res_up = self.controller.update_memory(
            item_id, "Database engine is PostgreSQL 16 [[database-config]]", target="memory"
        )
        self.assertTrue(res_up["ok"])
        self.assertEqual(res_up["action"], "UPDATE")
        self.assertEqual(self.controller.get_memory(item_id).text, "Database engine is PostgreSQL 16 [[database-config]]")

        # 4. DELETE
        res_del = self.controller.delete_memory(item_id, target="memory")
        self.assertTrue(res_del["ok"])
        self.assertEqual(res_del["action"], "DELETE")
        self.assertIsNone(self.controller.get_memory(item_id))
        self.assertEqual(len(self.controller.list_memories("memory")), 0)

    def test_contradiction(self):
        # Case 1: "Use pytest instead of unittest" contradicts "Use unittest for testing"
        res1 = self.controller.add_memory("Use unittest for testing [[testing]]", target="memory")
        self.assertEqual(res1["action"], "ADD")
        old_id = res1["item"]["id"]

        res2 = self.controller.add_memory("Use pytest instead of unittest [[testing]]", target="memory")
        self.assertEqual(res2["action"], "UPDATE")
        self.assertIn("superseded_text", res2)
        self.assertIn("unittest", res2["superseded_text"])

        # Check physical store only contains the updated note, no duplicate or contradictory notes
        active_entries = self.store.read_entries("memory")
        self.assertEqual(len(active_entries), 1)
        self.assertIn("pytest", active_entries[0])
        self.assertNotIn("Use unittest for testing", active_entries[0])

        # Case 2: "Migrated DB from SQLite to PostgreSQL" contradicts "Use SQLite for data storage"
        res_db1 = self.controller.add_memory("Use SQLite for data storage [[db]]", target="memory")
        self.assertEqual(res_db1["action"], "ADD")

        res_db2 = self.controller.add_memory("Migrated DB from SQLite to PostgreSQL [[db]]", target="memory")
        self.assertEqual(res_db2["action"], "UPDATE")

        active_entries = self.store.read_entries("memory")
        self.assertEqual(len(active_entries), 2)  # pytest note + migrated db note
        db_entries = [e for e in active_entries if "[[db]]" in e]
        self.assertEqual(len(db_entries), 1)
        self.assertIn("PostgreSQL", db_entries[0])

        # Case 3: Deprecation / Removal -> DELETE
        self.controller.add_memory("XML parser configuration [[parser]]", target="memory")
        res_del = self.controller.add_memory("Removed XML parser [[parser]]", target="memory")
        self.assertEqual(res_del["action"], "DELETE")

        # Case 4: Concept tag polarity shift (e.g. dark vs light mode)
        res_theme1 = self.controller.add_memory("Prefers dark mode [[ui-theme]]", target="user")
        self.assertEqual(res_theme1["action"], "ADD")
        res_theme2 = self.controller.add_memory("Prefers light mode [[ui-theme]]", target="user")
        self.assertEqual(res_theme2["action"], "UPDATE")
        user_entries = self.store.read_entries("user")
        self.assertEqual(len(user_entries), 1)
        self.assertIn("light mode", user_entries[0])

    def test_wiki_links(self):
        self.controller.add_memory("Auth uses JWT tokens [[auth-flow]] [[security]]", target="memory")
        self.controller.add_memory("Refresh token rotation endpoint [[auth-flow]] [[api-router]]", target="memory")
        self.controller.add_memory("Database connection pooling [[database-config]]", target="memory")

        # Query neighborhood by tag
        auth_notes = self.controller.get_linked_neighborhood("auth-flow")
        self.assertEqual(len(auth_notes), 2)
        auth_texts = [n.text for n in auth_notes]
        self.assertTrue(any("JWT tokens" in t for t in auth_texts))
        self.assertTrue(any("token rotation" in t for t in auth_texts))

        # With brackets [[tag]]
        db_notes = self.controller.get_linked_neighborhood("[[database-config]]")
        self.assertEqual(len(db_notes), 1)
        self.assertIn("connection pooling", db_notes[0].text)

        # Non-existent tag
        empty_notes = self.controller.get_linked_neighborhood("unknown-tag")
        self.assertEqual(len(empty_notes), 0)


class TestMemoryLifecycleManager(unittest.TestCase):
    """Test hit-counting, reflection promotion, and SQLite FTS5 cold archival demotion."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.workspace = Path(self.temp_dir) / "workspace"
        self.global_home = Path(self.temp_dir) / "global_home"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.global_home.mkdir(parents=True, exist_ok=True)

        self.store = CuratedMemoryStore(
            workspace_root=self.workspace,
            global_home=self.global_home,
        )
        self.controller = AMEMController(
            workspace_root=self.workspace,
            global_home=self.global_home,
            store=self.store,
        )
        self.lifecycle = MemoryLifecycleManager(
            workspace_root=self.workspace,
            global_home=self.global_home,
            controller=self.controller,
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_lifecycle_hit_counter(self):
        # 1. Active memory reference
        res = self.controller.add_memory("Use pytest-asyncio for async tests [[testing]]", target="memory")
        mid = res["item"]["id"]
        initial_hits = res["item"]["hit_counter"]

        ref_res = self.lifecycle.record_reference(mid)
        self.assertIsNotNone(ref_res)
        self.assertEqual(ref_res["hit_counter"], initial_hits + 1)

        # 2. Reflection reference
        ref_entry = self.lifecycle.record_reflection("Always wrap asyncpg connections in pool context [[db]]")
        ref_id = ref_entry["id"]
        self.assertEqual(ref_entry["hit_counter"], 1)

        ref_res2 = self.lifecycle.record_reference(ref_id)
        self.assertEqual(ref_res2["hit_counter"], 2)

    def test_promotion(self):
        # Record a reflection from LESSONS.md
        ref = self.lifecycle.record_reflection("Pin numpy version to <2.0 for torch compatibility [[python]]")
        ref_id = ref["id"]

        # No promotions yet
        self.assertEqual(len(self.lifecycle.check_promotions()), 0)

        # Hit 2
        self.lifecycle.record_reference(ref_id)
        self.assertEqual(len(self.lifecycle.check_promotions()), 0)

        # Hit 3 -> qualifies for promotion!
        self.lifecycle.record_reference(ref_id)
        promotions = self.lifecycle.check_promotions()
        self.assertEqual(len(promotions), 1)
        self.assertEqual(promotions[0]["id"], ref_id)
        self.assertEqual(promotions[0]["action"], "promote_to_memory")

        # Execute promotion into MEMORY.md
        ok = self.lifecycle.promote_entry(ref_id, target="memory")
        self.assertTrue(ok)

        # Verify active MEMORY.md now contains the promoted lesson
        active_entries = self.store.read_entries("memory")
        self.assertTrue(any("Pin numpy version" in e for e in active_entries))

        # Once promoted, check_promotions() no longer returns it
        self.assertEqual(len(self.lifecycle.check_promotions()), 0)

    def test_demotion_fts5(self):
        # Add an active note
        res = self.controller.add_memory("Legacy XML reporting endpoint configured at /v1/report.xml [[reporting]]", target="memory")
        mid = res["item"]["id"]

        # 1. Without staleness, demotion sweep keeps it active
        demoted = self.lifecycle.run_demotion_sweep(retention_days=30)
        self.assertEqual(len(demoted), 0)
        self.assertEqual(len(self.store.read_entries("memory")), 1)

        # 2. Simulate staleness > 30 days
        item = self.controller.get_memory(mid)
        self.assertIsNotNone(item)
        item.last_referenced_at = time.time() - (35 * 86400)
        self.controller._save_metadata()

        # Run demotion sweep
        demoted = self.lifecycle.run_demotion_sweep(retention_days=30)
        self.assertEqual(len(demoted), 1)
        self.assertEqual(demoted[0]["memory_id"], mid)
        self.assertEqual(demoted[0]["reason"], "unreferenced_stale_30d")

        # Verify pruned from active MEMORY.md
        self.assertEqual(len(self.store.read_entries("memory")), 0)
        self.assertEqual(len(self.controller.list_memories("memory")), 0)

        # Verify indexed in SQLite FTS5 cold archive
        archived = self.lifecycle.search_archived_memories("report.xml")
        self.assertGreaterEqual(len(archived), 1)
        self.assertEqual(archived[0]["memory_id"], mid)
        self.assertIn("Legacy XML reporting", archived[0]["content"])
        self.assertEqual(archived[0]["reason"], "unreferenced_stale_30d")

        # Search with wildcards / all
        all_archived = self.lifecycle.search_archived_memories("*")
        self.assertGreaterEqual(len(all_archived), 1)


class TestHistoryAndRollback(unittest.TestCase):
    """Test lightweight .history/ timestamped snapshots and rollback engine."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.workspace = Path(self.temp_dir) / "workspace"
        self.global_home = Path(self.temp_dir) / "global_home"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.global_home.mkdir(parents=True, exist_ok=True)

        self.store = CuratedMemoryStore(
            workspace_root=self.workspace,
            global_home=self.global_home,
        )
        self.history_mgr = HistorySnapshotManager(
            workspace_root=self.workspace,
            global_home=self.global_home,
            max_snapshots=20,
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_history_snapshots_and_rollback(self):
        # 1. First write creates initial entry
        self.store.add("Version 1: Initial architecture note [[arch]]", target="memory")
        initial_entries = self.store.read_entries("memory")
        self.assertEqual(initial_entries[0], "Version 1: Initial architecture note [[arch]]")

        # 2. Second write captures a snapshot of Version 1
        self.store.replace(
            "Version 1: Initial architecture note [[arch]]",
            "Version 2: Upgraded architecture note [[arch]]",
            target="memory",
        )
        snaps = self.history_mgr.list_history("memory")
        self.assertGreaterEqual(len(snaps), 1)
        snap_ts = snaps[0]["timestamp"]

        # Check content is currently Version 2
        self.assertEqual(self.store.read_entries("memory")[0], "Version 2: Upgraded architecture note [[arch]]")

        # 3. Rollback to Version 1 snapshot
        ok = self.history_mgr.rollback("memory", snap_ts)
        self.assertTrue(ok)
        rolled_back_entries = self.store.read_entries("memory")
        self.assertEqual(rolled_back_entries[0], "Version 1: Initial architecture note [[arch]]")

        # 4. Test pruning: create 25 snapshots, verify capped at max_snapshots (20)
        mem_file = self.store.get_path_for_target("memory")
        for i in range(25):
            mem_file.write_text(f"Note variant {i}", encoding="utf-8")
            self.history_mgr.create_snapshot("memory", mem_file)

        snaps_after = self.history_mgr.list_history("memory")
        self.assertLessEqual(len(snaps_after), 20)


class TestCliAndTuiMemory(unittest.TestCase):
    """Test CLI subcommands and TUI slash commands for memory inspection and rollback."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.workspace = Path(self.temp_dir) / "workspace"
        self.global_home = Path(self.temp_dir) / "global_home"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.global_home.mkdir(parents=True, exist_ok=True)

        self.store = CuratedMemoryStore(
            workspace_root=self.workspace,
            global_home=self.global_home,
        )
        self.controller = AMEMController(
            workspace_root=self.workspace,
            global_home=self.global_home,
            store=self.store,
        )
        self.lifecycle = MemoryLifecycleManager(
            workspace_root=self.workspace,
            global_home=self.global_home,
            controller=self.controller,
        )
        self.history_mgr = HistorySnapshotManager(
            workspace_root=self.workspace,
            global_home=self.global_home,
        )

        # Seed memories
        self.controller.add_memory("PostgreSQL 16 connection pooling configured [[database]]", target="memory")
        self.controller.add_memory("Preferred language is Python 3.14 [[lang]]", target="user")

        # Mock orchestrator for TUI
        self.orch = MagicMock()
        self.orch.workspace_root = self.workspace
        self.console = HexConsole(orchestrator=self.orch)
        self.console.workspace_root = self.workspace

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_cli_memory_list_and_search(self):
        # 1. hex memory list --json
        buf = StringIO()
        with patch("sys.stdout", buf):
            code = cli_main(["memory", "list", "--workspace", str(self.workspace), "--json"])
        self.assertEqual(code, 0)
        self.assertIn("targets", buf.getvalue())
        self.assertIn("database", buf.getvalue())

        # 2. hex memory search
        buf_srch = StringIO()
        with patch("sys.stdout", buf_srch):
            code_srch = cli_main(["memory", "search", "PostgreSQL", "--workspace", str(self.workspace)])
        self.assertEqual(code_srch, 0)
        self.assertIn("PostgreSQL", buf_srch.getvalue())

    def test_cli_memory_history_and_rollback(self):
        # Mutate to create history snapshot
        self.store.replace(
            "PostgreSQL 16 connection pooling configured [[database]]",
            "PostgreSQL 17 connection pooling configured [[database]]",
            target="memory",
        )

        # hex memory history --json
        buf_hist = StringIO()
        with patch("sys.stdout", buf_hist):
            code_hist = cli_main(["memory", "history", "--workspace", str(self.workspace), "--json"])
        self.assertEqual(code_hist, 0)
        self.assertIn("timestamp", buf_hist.getvalue())

        snaps = self.history_mgr.list_history("memory")
        self.assertTrue(len(snaps) > 0)
        ts = snaps[0]["timestamp"]

        # hex memory rollback
        buf_rb = StringIO()
        with patch("sys.stdout", buf_rb):
            code_rb = cli_main(["memory", "rollback", "memory", ts, "--workspace", str(self.workspace)])
        self.assertEqual(code_rb, 0)
        self.assertIn("Rolled back", buf_rb.getvalue())

    def test_tui_memory_slash_commands(self):
        # 1. /memory (summary)
        buf = StringIO()
        with patch("sys.stdout", buf):
            self.console.handle_slash_command("/memory")
        out = buf.getvalue()
        self.assertIn("Governed Curated Memory Summary", out)
        self.assertIn("MEMORY", out)

        # Mutate memory to produce a history snapshot
        self.store.replace(
            "PostgreSQL 16 connection pooling configured [[database]]",
            "PostgreSQL 17 connection pooling configured [[database]]",
            target="memory",
        )
        snaps = self.history_mgr.list_history("memory")
        self.assertGreaterEqual(len(snaps), 1)
        ts = snaps[0]["timestamp"]

        # 2. /memory history
        buf_hist = StringIO()
        with patch("sys.stdout", buf_hist):
            self.console.handle_slash_command("/memory history")
        self.assertIn("Memory History", buf_hist.getvalue())
        self.assertIn(ts, buf_hist.getvalue())

        # 3. /memory rollback
        buf_rb = StringIO()
        with patch("sys.stdout", buf_rb):
            self.console.handle_slash_command(f"/memory rollback {ts}")
        self.assertIn("Successfully rolled back memory", buf_rb.getvalue())

        # 4. /memory <query>
        buf_query = StringIO()
        with patch("sys.stdout", buf_query):
            self.console.handle_slash_command("/memory PostgreSQL")
        self.assertIn("Memory Results for 'PostgreSQL'", buf_query.getvalue())


