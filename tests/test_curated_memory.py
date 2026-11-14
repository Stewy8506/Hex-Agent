"""Unit and integration tests for CuratedMemoryStore, MemorySnapshot, and StreamingContextScrubber."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from harness_core.memory import (
    ENTRY_DELIMITER,
    GUARDRAIL_CHAR_LIMIT,
    MEMORY_CHAR_LIMIT,
    USER_CHAR_LIMIT,
    CuratedMemoryStore,
    GuardrailsManager,
    MemorySnapshot,
    StreamingContextScrubber,
    _drop_repeated_recall_lines,
    _find_unique_match,
    _normalize_for_match,
    sanitize_context,
)


class TestCuratedMemoryStore(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.workspace = Path(self.temp_dir) / "workspace"
        self.global_home = Path(self.temp_dir) / "global_home"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.global_home.mkdir(parents=True, exist_ok=True)
        MemorySnapshot.reset_cache()

        self.store = CuratedMemoryStore(
            workspace_root=self.workspace,
            global_home=self.global_home,
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        MemorySnapshot.reset_cache()

    def test_curated_store_add_and_list(self):
        res = self.store.add("Prefers pytest over unittest", target="user")
        self.assertTrue(res["ok"])
        self.assertEqual(res["action"], "add")

        res_mem = self.store.add("Architecture uses dual SQLite/Postgres engine", target="memory")
        self.assertTrue(res_mem["ok"])

        # Check list
        listing = self.store.list_entries(target="user")
        self.assertTrue(listing["ok"])
        entries = listing["targets"]["user"]["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0], "Prefers pytest over unittest")

    def test_curated_store_char_limits(self):
        # Exceed USER_CHAR_LIMIT (1375)
        big_text = "A" * (USER_CHAR_LIMIT + 10)
        res = self.store.add(big_text, target="user")
        self.assertFalse(res["ok"])
        self.assertIn("exceed", res["error"])

        # Below limit works
        valid_text = "B" * 500
        res2 = self.store.add(valid_text, target="user")
        self.assertTrue(res2["ok"])

        # Adding another 1000 should exceed 1375
        another_text = "C" * 1000
        res3 = self.store.add(another_text, target="user")
        self.assertFalse(res3["ok"])
        self.assertIn("exceed", res3["error"])

    def test_curated_store_replace_and_tolerant_matching(self):
        # Add entry with smart quotes and em-dash
        self.store.add("Always use “fast-mode” — avoids timeout", target="memory")

        # Replace using regular straight quotes and hyphen
        res = self.store.replace(
            old_text='Always use "fast-mode" - avoids timeout',
            new_text="Always use turbo-mode for fast builds",
            target="memory",
        )
        self.assertTrue(res["ok"], f"Failed with: {res.get('error')}")

        entries = self.store.read_entries("memory")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0], "Always use turbo-mode for fast builds")

    def test_curated_store_patch(self):
        self.store.add("Database port is 5432 and timeout is 30s", target="memory")

        # Patch just the port
        res = self.store.patch("5432", "5433", target="memory")
        self.assertTrue(res["ok"])

        entries = self.store.read_entries("memory")
        self.assertEqual(entries[0], "Database port is 5433 and timeout is 30s")

        # Repeated span patch ambiguity
        self.store.add("foo bar foo baz", target="memory")
        res_ambig = self.store.patch("foo", "qux", target="memory")
        self.assertFalse(res_ambig["ok"])
        self.assertIn("appears 2 times", res_ambig["error"])

    def test_curated_store_remove(self):
        self.store.add("Note 1", target="memory")
        self.store.add("Note 2", target="memory")
        self.assertEqual(len(self.store.read_entries("memory")), 2)

        res = self.store.remove("Note 1", target="memory")
        self.assertTrue(res["ok"])
        remaining = self.store.read_entries("memory")
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0], "Note 2")

    def test_curated_store_drift_detection(self):
        # Manually write an un-delimited oversized entry directly to disk
        path = self.store.get_path_for_target("memory")
        path.parent.mkdir(parents=True, exist_ok=True)
        oversized = "X" * (MEMORY_CHAR_LIMIT + 500)
        path.write_text(oversized, encoding="utf-8")

        # Mutation should detect drift, write .bak snapshot, and refuse write
        res = self.store.add("New note", target="memory")
        self.assertFalse(res["ok"])
        self.assertIn("drift", res["error"].lower())
        self.assertTrue(Path(res["drift_backup"]).is_file())


class TestStreamingContextScrubber(unittest.TestCase):
    def test_scrubber_passthrough(self):
        scrubber = StreamingContextScrubber()
        out = scrubber.feed("Hello world! How are you?") + scrubber.flush()
        self.assertEqual(out, "Hello world! How are you?")

    def test_scrubber_strips_full_tag(self):
        scrubber = StreamingContextScrubber()
        raw = "Hello\n<memory-context>\nSystem notes here\n</memory-context>\nWorld!"
        out = scrubber.feed(raw) + scrubber.flush()
        self.assertEqual(out.strip(), "Hello\n\nWorld!")

    def test_scrubber_chunked_straddling(self):
        scrubber = StreamingContextScrubber()
        chunks = [
            "Before memory block.\n",
            "<memo",
            "ry-context>\n",
            "Sensitive internal notes line 1\n",
            "Sensitive line 2\n",
            "</memory-",
            "context>\n",
            "After memory block.",
        ]
        emitted = []
        for chunk in chunks:
            res = scrubber.feed(chunk)
            if res:
                emitted.append(res)
        res_flush = scrubber.flush()
        if res_flush:
            emitted.append(res_flush)

        combined = "".join(emitted)
        self.assertNotIn("Sensitive", combined)
        self.assertNotIn("memory-context", combined)
        self.assertIn("Before memory block.", combined)
        self.assertIn("After memory block.", combined)


class TestFrozenSnapshot(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.workspace = Path(self.temp_dir) / "workspace"
        self.global_home = Path(self.temp_dir) / "global_home"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.global_home.mkdir(parents=True, exist_ok=True)
        MemorySnapshot.reset_cache()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        MemorySnapshot.reset_cache()

    def test_frozen_snapshot_assembly_and_deduplication(self):
        store = CuratedMemoryStore(workspace_root=self.workspace, global_home=self.global_home)
        store.add("- prefers pnpm", target="user")
        store.add("- prefers pnpm", target="user")  # duplicate bullet
        store.add("Use PostgreSQL for ledgers", target="memory")

        snap = MemorySnapshot.build_frozen_snapshot(self.workspace, self.global_home)
        self.assertIn("<memory-context>", snap)
        self.assertIn("</memory-context>", snap)
        self.assertIn("prefers pnpm", snap)
        self.assertIn("Use PostgreSQL for ledgers", snap)

        # Confirm bullet deduplicated
        self.assertEqual(snap.count("- prefers pnpm"), 1)

    def test_frozen_snapshot_immutability_mid_session(self):
        store = CuratedMemoryStore(workspace_root=self.workspace, global_home=self.global_home)
        store.add("Initial note", target="memory")

        # Session start Turn 0: build frozen snapshot
        snap1 = store.build_frozen_snapshot()
        self.assertIn("Initial note", snap1)

        # Mid-session tool write updates disk
        store.add("Mid-session added fact", target="memory")
        disk_entries = store.read_entries("memory")
        self.assertEqual(len(disk_entries), 2)
        self.assertIn("Mid-session added fact", disk_entries)

        # Frozen snapshot must remain 100% byte-identical for KV-cache preservation!
        snap2 = store.build_frozen_snapshot()
        self.assertEqual(snap1, snap2)
        self.assertNotIn("Mid-session added fact", snap2)


if __name__ == "__main__":
    unittest.main()
