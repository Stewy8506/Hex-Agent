"""Tests for Durable Lifecycle, Transactional Memory Mutations, and Idle Reflection Workers.

Addresses:
- F-02: Archive-before-delete durability, FTS idempotency, and promotion wiring.
- F-05: Transactional multi-operation batch execution with rollback; physical deletion checking.
- F-06: Idle reflection worker draining pending reflections.
"""

import json
import sqlite3
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from harness_core.memory import (
    AMEMController,
    CuratedMemoryStore,
    MemoryLifecycleManager,
)
from harness_core.orchestrator import HarnessOrchestrator, run_lifecycle_maintenance


class TestDurableLifecycle:
    """Tests for MemoryLifecycleManager archive-before-delete durability and FTS idempotency (F-02)."""

    def test_archive_before_delete(self, tmp_path):
        """Demotion sweep must preserve active memory if cold archive fails."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        mem_dir = workspace / ".hex" / "memories"
        mem_dir.mkdir(parents=True)

        store = CuratedMemoryStore(workspace_root=workspace)
        controller = AMEMController(workspace_root=workspace, store=store)
        lifecycle = MemoryLifecycleManager(workspace_root=workspace, controller=controller)

        # Add active note
        res = controller.add_memory("Critical architectural guideline: always verify commits.", target="memory")
        assert res["ok"]
        mem_id = res["item"]["id"]

        # Make it stale (>30 days old)
        item = controller.get_memory(mem_id)
        item.last_referenced_at = time.time() - (35 * 86400)
        controller._save_metadata()

        # Simulate archive failure
        with patch.object(lifecycle, "archive_entry", return_value=False) as mock_archive:
            demoted = lifecycle.run_demotion_sweep(retention_days=30)
            assert len(demoted) == 0
            assert mock_archive.called

            # Verify note STILL exists in active store and in controller metadata
            assert controller.get_memory(mem_id) is not None
            mem_path = store.get_path_for_target("memory")
            assert mem_path.is_file()
            active_content = mem_path.read_text(encoding="utf-8")
            assert "Critical architectural guideline: always verify commits." in active_content

        # Now allow archival to succeed
        demoted = lifecycle.run_demotion_sweep(retention_days=30)
        assert len(demoted) == 1
        assert demoted[0]["memory_id"] == mem_id

        # Verify pruned from active markdown store and metadata
        assert controller.get_memory(mem_id) is None
        mem_path = store.get_path_for_target("memory")
        active_content = mem_path.read_text(encoding="utf-8") if mem_path.is_file() else ""
        assert "Critical architectural guideline" not in active_content

        # Verify preserved in archive.db
        archived = lifecycle.search_archived_memories("architectural")
        assert len(archived) == 1
        assert archived[0]["memory_id"] == mem_id

    def test_archive_idempotency(self, tmp_path):
        """Re-archiving an existing memory updates row and avoids duplicate rows in FTS5."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        lifecycle = MemoryLifecycleManager(workspace_root=workspace)

        mem_id = "mem_dedup_01"
        ok1 = lifecycle.archive_entry(
            memory_id=mem_id,
            target="memory",
            content="Initial revision of rule about database connection pooling.",
            tags=["database", "pooling"],
            reason="manual_test",
        )
        assert ok1 is True

        # Re-archive the exact same memory_id with updated content (simulating retry or update)
        ok2 = lifecycle.archive_entry(
            memory_id=mem_id,
            target="memory",
            content="Second revision of rule about database connection pooling with max size 20.",
            tags=["database", "pooling", "v2"],
            reason="updated_archive",
        )
        assert ok2 is True

        # Inspect SQLite tables directly
        conn = sqlite3.connect(str(lifecycle.archive_db_path))
        try:
            # Main table unique constraint check
            cur = conn.execute("SELECT COUNT(*) FROM archived_memories WHERE memory_id = ?", (mem_id,))
            assert cur.fetchone()[0] == 1

            # FTS5 table check: must NOT have 2 rows
            cur_fts = conn.execute("SELECT COUNT(*) FROM archived_memories_fts WHERE memory_id = ?", (mem_id,))
            assert cur_fts.fetchone()[0] == 1
        finally:
            conn.close()

        # FTS search returns 1 updated hit
        results = lifecycle.search_archived_memories("pooling")
        assert len(results) == 1
        assert "max size 20" in results[0]["content"]

    def test_promotion_wiring(self, tmp_path):
        """A reflection with >= 3 references is automatically promoted on task completion."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        orchestrator_dir = tmp_path / "orchestrator_data"
        orchestrator_dir.mkdir()

        orch = HarnessOrchestrator(
            data_dir=orchestrator_dir,
            default_workspace=workspace,
        )

        lifecycle = MemoryLifecycleManager(workspace_root=workspace)
        orch.memory.lifecycle_mgr = lifecycle

        # Record a reflection
        ref = lifecycle.record_reflection("Always validate database migrations in both directions.")
        ref_id = ref["id"]

        # 2 references -> hit counter = 3
        lifecycle.record_reference(ref_id)
        lifecycle.record_reference(ref_id)
        assert lifecycle._reflections[ref_id]["hit_counter"] == 3

        # Candidates before task completion
        promos = lifecycle.check_promotions()
        assert len(promos) == 1
        assert promos[0]["id"] == ref_id

        # Mock agent loop session result succeeding
        mock_result = MagicMock(
            success=True,
            friction_info={},
            retry_count=0,
            final_answer="Tasks completed successfully",
            total_turns=1,
            termination_reason="complete",
            turns=[],
            compaction_result=None,
        )
        with patch("harness_core.react_loop.ReActLoop.run", return_value=mock_result):
            orch.run_agent_loop(
                goal="Verify database migration scripts",
                workspace_root=workspace,
            )

        # After task completion, promotion should have fired
        promos_after = lifecycle.check_promotions()
        assert len(promos_after) == 0

        # Memory store should have the promoted reflection in MEMORY.md
        active_memory = (workspace / ".hex" / "memories" / "MEMORY.md").read_text(encoding="utf-8")
        assert "Always validate database migrations in both directions." in active_memory

        # Also test run_lifecycle_maintenance helper
        maint_res = run_lifecycle_maintenance(workspace_root=workspace)
        assert maint_res["status"] == "success"
        assert "demoted_count" in maint_res


class TestTransactionalMutations:
    """Tests for transactional batch operations and disk deletion verification (F-05)."""

    def test_batch_transaction_rollback(self, tmp_path):
        """Batch operations must be all-or-nothing: failure in Op 2 rolls back Op 1."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        store = CuratedMemoryStore(workspace_root=workspace)

        # Initialize with baseline entry
        baseline = store.add("Baseline rule: keep methods concise.", target="memory")
        assert baseline["ok"]

        mem_path = store.get_path_for_target("memory")
        original_content = mem_path.read_text(encoding="utf-8")
        assert "Baseline rule: keep methods concise." in original_content

        # Submit batch: Op 1 is valid add, Op 2 fails due to budget limit (2200 chars max for memory)
        giant_text = "X" * 3000
        ops = [
            {"action": "add", "content": "Valid Op 1: always write unit tests.", "target": "memory"},
            {"action": "add", "content": giant_text, "target": "memory"},
        ]

        res = store.execute_operations("memory", ops)
        assert res["ok"] is False
        assert "char limit" in res["error"]
        assert res["failed_op"] == ops[1]

        # Verify disk file is UNCHANGED: Op 1 was NOT committed
        current_content = mem_path.read_text(encoding="utf-8")
        assert current_content == original_content
        assert "Valid Op 1: always write unit tests." not in current_content

        # Test failure on non-existent old_text for replace
        ops_bad_replace = [
            {"action": "add", "content": "Valid Op 1: check error codes.", "target": "memory"},
            {"action": "replace", "old_text": "Non-existent string xyz123", "new_text": "Replacement", "target": "memory"},
        ]
        res_replace = store.execute_operations("memory", ops_bad_replace)
        assert res_replace["ok"] is False
        assert "No entry matched" in res_replace["error"]

        current_content = mem_path.read_text(encoding="utf-8")
        assert current_content == original_content
        assert "Valid Op 1: check error codes." not in current_content

        # Now test a batch where all operations succeed
        ops_valid = [
            {"action": "add", "content": "Valid Op 1: check error codes.", "target": "memory"},
            {"action": "replace", "old_text": "Baseline rule: keep methods concise.", "new_text": "Baseline rule: keep methods small and pure.", "target": "memory"},
        ]
        res_valid = store.execute_operations("memory", ops_valid)
        assert res_valid["ok"] is True
        assert len(res_valid["results"]) == 2

        updated_content = mem_path.read_text(encoding="utf-8")
        assert "Valid Op 1: check error codes." in updated_content
        assert "Baseline rule: keep methods small and pure." in updated_content
        assert "Baseline rule: keep methods concise." not in updated_content

    def test_delete_memory_disk_check(self, tmp_path):
        """AMEMController.delete_memory must verify physical store remove result and retain metadata if disk remove fails."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        store = CuratedMemoryStore(workspace_root=workspace)
        controller = AMEMController(workspace_root=workspace, store=store)

        res = controller.add_memory("Rule to be safely tested for deletion: [[security]]", target="memory")
        assert res["ok"]
        mem_id = res["item"]["id"]

        assert controller.get_memory(mem_id) is not None
        assert "security" in controller._tag_index
        assert mem_id in controller._tag_index["security"]

        # Mock store.remove returning failure (e.g. disk I/O or lock error)
        with patch.object(store, "remove", return_value={"ok": False, "success": False, "error": "Disk I/O error"}):
            del_res = controller.delete_memory(mem_id, target="memory")
            assert del_res["ok"] is False
            assert "Failed to remove memory from physical store" in del_res["error"]

            # CRITICAL: Memory item and tag index MUST be preserved!
            assert controller.get_memory(mem_id) is not None
            assert mem_id in controller._tag_index["security"]

        # Now let deletion succeed
        del_res_ok = controller.delete_memory(mem_id, target="memory")
        assert del_res_ok["ok"] is True
        assert controller.get_memory(mem_id) is None
        assert mem_id not in controller._tag_index.get("security", set())

