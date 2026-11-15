"""Tests for AST Memory Precision, SQLite FTS5 Storage & Untrusted Observation Fencing.

Verifies:
- F-03: Symbol-body hashing prevents false staleness from unrelated module edits.
- F-03: SQLite FTS5 indexing (.hex/memories/node_memories.db) and legacy JSON migration.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from harness_core.memory import (
    ASTNodeMemory,
    ASTNodeMemoryStore,
    compute_code_fingerprint,
    is_fingerprint_stale,
)


class TestASTSymbolPrecision(unittest.TestCase):
    """Test AST symbol-body fingerprinting precision and staleness resilience (F-03)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name).resolve()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_fingerprint_unrelated_function_edit_does_not_stale(self) -> None:
        """Editing an unrelated function or comment must NOT mark a target symbol memory stale."""
        store = ASTNodeMemoryStore(workspace_root=self.ws)
        src = self.ws / "math_ops.py"
        src.write_text(
            "def add(a, b):\n"
            "    return a + b\n\n"
            "def multiply(a, b):\n"
            "    return a * b\n",
            encoding="utf-8",
        )

        mem = store.record_node_memory(
            symbol_name="multiply",
            rel_path="math_ops.py",
            note="May overflow on unbounded integer products.",
            category="gotcha",
        )

        self.assertFalse(store.is_memory_stale(mem))
        self.assertFalse(
            is_fingerprint_stale(
                self.ws,
                "math_ops.py",
                mem.code_fingerprint,
                symbol_name="multiply",
            )
        )

        # Edit unrelated function 'add' and add comments
        src.write_text(
            "# Module documentation update\n"
            "def add(a, b):\n"
            "    # Added debug comment\n"
            "    return (a + b) + 0\n\n"
            "def multiply(a, b):\n"
            "    return a * b\n",
            encoding="utf-8",
        )

        # Memory attached to 'multiply' MUST remain fresh
        self.assertFalse(store.is_memory_stale(mem))
        self.assertFalse(
            is_fingerprint_stale(
                self.ws,
                "math_ops.py",
                mem.code_fingerprint,
                symbol_name="multiply",
            )
        )

    def test_fingerprint_target_symbol_edit_causes_staleness(self) -> None:
        """Modifying the target symbol body causes is_fingerprint_stale to return True."""
        store = ASTNodeMemoryStore(workspace_root=self.ws)
        src = self.ws / "math_ops.py"
        src.write_text(
            "def multiply(a, b):\n"
            "    return a * b\n",
            encoding="utf-8",
        )

        mem = store.record_node_memory(
            symbol_name="multiply",
            rel_path="math_ops.py",
            note="Must handle negatives.",
            category="invariant",
        )

        self.assertFalse(store.is_memory_stale(mem))

        # Mutate multiply definition body
        src.write_text(
            "def multiply(a, b):\n"
            "    if a < 0 or b < 0:\n"
            "        raise ValueError('Negative factors forbidden')\n"
            "    return a * b\n",
            encoding="utf-8",
        )

        self.assertTrue(store.is_memory_stale(mem))
        self.assertTrue(
            is_fingerprint_stale(
                self.ws,
                "math_ops.py",
                mem.code_fingerprint,
                symbol_name="multiply",
            )
        )

    def test_fingerprint_target_symbol_deletion_causes_staleness(self) -> None:
        """Deleting the target symbol causes is_fingerprint_stale to return True."""
        store = ASTNodeMemoryStore(workspace_root=self.ws)
        src = self.ws / "math_ops.py"
        src.write_text(
            "def foo():\n    return 1\n\ndef bar():\n    return 2\n",
            encoding="utf-8",
        )

        mem = store.record_node_memory(
            symbol_name="bar",
            rel_path="math_ops.py",
            note="Note about bar.",
        )

        # Delete 'bar' from the file
        src.write_text("def foo():\n    return 1\n", encoding="utf-8")

        self.assertTrue(store.is_memory_stale(mem))
        self.assertTrue(
            is_fingerprint_stale(
                self.ws,
                "math_ops.py",
                mem.code_fingerprint,
                symbol_name="bar",
            )
        )


class TestSQLiteFTSNodeMemoryStore(unittest.TestCase):
    """Test SQLite backing and FTS5 BM25 search for ASTNodeMemoryStore (F-03)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name).resolve()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_sqlite_db_and_fts_table_initialization(self) -> None:
        """Store initializes .hex/memories/node_memories.db with node_memories and node_memories_fts tables."""
        store = ASTNodeMemoryStore(workspace_root=self.ws)
        db_file = self.ws / ".hex" / "memories" / "node_memories.db"
        self.assertTrue(db_file.is_file())

        with sqlite3.connect(str(db_file)) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {row[0] for row in cursor.fetchall()}
            self.assertIn("node_memories", tables)
            self.assertIn("node_memories_fts", tables)

    def test_sqlite_persistence_and_fts_search(self) -> None:
        """Memories persist to SQLite and are searchable via SQLite FTS5 BM25 search."""
        store = ASTNodeMemoryStore(workspace_root=self.ws)
        src = self.ws / "db.py"
        src.write_text("def acquire_lock():\n    pass\n", encoding="utf-8")

        m1 = store.record_node_memory(
            symbol_name="acquire_lock",
            rel_path="db.py",
            note="PostgreSQL advisory lock requires explicit unlock on transaction rollback.",
            category="gotcha",
        )
        m2 = store.record_node_memory(
            symbol_name="cache_lookup",
            rel_path="cache.py",
            note="Redis cache TTL expiration must use jitter to prevent thundering herd.",
            category="invariant",
        )

        # Query by symbol and path
        by_sym = store.get_node_memories("acquire_lock")
        self.assertEqual(len(by_sym), 1)
        self.assertEqual(by_sym[0].id, m1.id)

        # FTS5 search
        res1 = store.search_node_memories("advisory")
        self.assertEqual(len(res1), 1)
        self.assertEqual(res1[0].id, m1.id)

        res2 = store.search_node_memories("thundering herd")
        self.assertEqual(len(res2), 1)
        self.assertEqual(res2[0].id, m2.id)

        # Persistence across fresh store instance
        fresh_store = ASTNodeMemoryStore(workspace_root=self.ws)
        all_reloaded = fresh_store.list_all_node_memories()
        self.assertEqual(len(all_reloaded), 2)

        # Deletion removes from both tables
        self.assertTrue(fresh_store.delete_node_memory(m1.id))
        self.assertEqual(len(fresh_store.search_node_memories("advisory")), 0)
        self.assertEqual(len(fresh_store.list_all_node_memories()), 1)

    def test_legacy_json_auto_migration(self) -> None:
        """Legacy ast_memories.json entries are auto-migrated into node_memories.db on first load."""
        memories_dir = self.ws / ".hex" / "memories"
        memories_dir.mkdir(parents=True, exist_ok=True)
        legacy_file = memories_dir / "ast_memories.json"

        legacy_data = {
            "version": 1,
            "updated_at": 1700000000.0,
            "memories": [
                {
                    "id": "ast_mem_legacy_01",
                    "symbol_name": "legacy_fn",
                    "rel_path": "legacy.py",
                    "note": "Legacy critical invariant migrated automatically.",
                    "category": "invariant",
                    "code_fingerprint": "abc12345",
                    "created_at": 1700000000.0,
                    "updated_at": 1700000000.0,
                }
            ],
        }
        legacy_file.write_text(json.dumps(legacy_data), encoding="utf-8")

        # Initialize store
        store = ASTNodeMemoryStore(workspace_root=self.ws)
        reloaded = store.list_all_node_memories()
        self.assertEqual(len(reloaded), 1)
        self.assertEqual(reloaded[0].id, "ast_mem_legacy_01")
        self.assertEqual(reloaded[0].symbol_name, "legacy_fn")

        # FTS search finds migrated entry
        search_res = store.search_node_memories("migrated automatically")
        self.assertEqual(len(search_res), 1)
        self.assertEqual(search_res[0].id, "ast_mem_legacy_01")
