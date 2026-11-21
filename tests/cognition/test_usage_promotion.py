"""B60/B61 regression: usage hit-counting is live and promotion routes honestly.

Before this fix ``record_reference`` had no production caller (notes surfaced to
the model never accumulated usage), and ``check_promotions`` always claimed
``"MEMORY.md"`` as the target, so the orchestrator's promotion loop could never
select the ``skill`` branch and ``promote_to_skill`` was unreachable. Now the
frozen snapshot (Slot #4) credits one usage per surfaced note per session via
``record_reference_for_content``, verdicts name their real destination, and a
5-hit reflection compiles an actual SKILL.md bundle.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from harness_core.cognition.memory import (
    CuratedMemoryStore,
    MemoryItem,
    MemoryLifecycleManager,
    MemorySnapshot,
)


class _UsagePromotionBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="hex_usage_promotion_"))
        self.global_home = self.tmp / "home"
        self.global_home.mkdir(parents=True, exist_ok=True)
        MemorySnapshot.clear_all_snapshots()

    def tearDown(self) -> None:
        MemorySnapshot.clear_all_snapshots()
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestSurfacedNoteHitCounting(_UsagePromotionBase):
    def test_frozen_snapshot_bumps_surfaced_notes_per_session(self) -> None:
        ws = self.tmp / "ws"
        ws.mkdir(parents=True)
        store = CuratedMemoryStore(workspace_root=ws, global_home=self.global_home)
        add_res = store.add(content="Always run the linter before committing", target="memory")
        self.assertTrue(add_res.get("ok"), add_res)

        # Session A surfaces the note: the curated bullet is matched back to its
        # AMEM item and one usage is credited (sync-init 1 + bump 1).
        MemorySnapshot.build_frozen_snapshot(
            workspace_root=ws, global_home=self.global_home, session_id="sess_a"
        )
        lifecycle_a = MemoryLifecycleManager(workspace_root=ws, global_home=self.global_home)
        item_a = next(
            i for i in lifecycle_a.controller.list_memories("memory") if "linter" in i.text
        )
        self.assertGreaterEqual(item_a.hit_counter, 2, "surfaced note must record a usage hit")

        # Session B surfaces the same note: usage accumulates across sessions.
        MemorySnapshot.build_frozen_snapshot(
            workspace_root=ws, global_home=self.global_home, session_id="sess_b"
        )
        lifecycle_b = MemoryLifecycleManager(workspace_root=ws, global_home=self.global_home)
        item_b = next(
            i for i in lifecycle_b.controller.list_memories("memory") if "linter" in i.text
        )
        self.assertGreater(item_b.hit_counter, item_a.hit_counter)

    def test_record_reference_for_content_matches_reflection_note_and_rejects_unknown(self) -> None:
        ws = self.tmp / "ws2"
        store = CuratedMemoryStore(workspace_root=ws, global_home=self.global_home)
        store.add(content="Deploy via the blue-green pipeline", target="memory")
        lifecycle = MemoryLifecycleManager(workspace_root=ws, global_home=self.global_home)

        ref = lifecycle.record_reflection("Pin the CI runner to ubuntu-24.04")
        self.assertEqual(ref["hit_counter"], 1)

        bumped_ref = lifecycle.record_reference_for_content("Pin the CI runner to ubuntu-24.04")
        self.assertIsNotNone(bumped_ref)
        self.assertEqual(bumped_ref["hit_counter"], 2)

        bumped_note = lifecycle.record_reference_for_content("Deploy via the blue-green pipeline")
        self.assertIsNotNone(bumped_note)
        self.assertEqual(bumped_note["hit_counter"], 2)

        self.assertIsNone(
            lifecycle.record_reference_for_content("Content that was never tracked"),
            "untracked content must be an honest no-op",
        )


class TestHonestPromotionTargets(_UsagePromotionBase):
    def _reflection_with_hits(self, ws: Path, content: str, hits: int) -> dict:
        lifecycle = MemoryLifecycleManager(workspace_root=ws, global_home=self.global_home)
        ref = lifecycle.record_reflection(content)
        for _ in range(hits - 1):
            ref = lifecycle.record_reference(ref["id"])
        return ref

    def test_three_hits_target_curated_memory(self) -> None:
        from harness_core.core.orchestrator import _promotion_target_for

        ws = self.tmp / "ws3"
        ref = self._reflection_with_hits(ws, "Retry the flaky network probe with backoff", 3)
        lifecycle = MemoryLifecycleManager(workspace_root=ws, global_home=self.global_home)
        cand = next(c for c in lifecycle.check_promotions() if c["id"] == ref["id"])
        self.assertEqual(cand["action"], "promote_to_memory")
        self.assertEqual(cand["target"], "MEMORY.md")
        self.assertEqual(_promotion_target_for(cand), "memory")

    def test_five_hits_flag_for_skill(self) -> None:
        from harness_core.core.orchestrator import _promotion_target_for

        ws = self.tmp / "ws4"
        ref = self._reflection_with_hits(ws, "Rotate the API keys every 90 days", 5)
        lifecycle = MemoryLifecycleManager(workspace_root=ws, global_home=self.global_home)
        cand = next(c for c in lifecycle.check_promotions() if c["id"] == ref["id"])
        self.assertEqual(cand["action"], "flag_for_skill")
        self.assertEqual(cand["target"], "SKILL")
        self.assertEqual(_promotion_target_for(cand), "skill")


class TestSkillPromotionEndToEnd(_UsagePromotionBase):
    def test_flag_for_skill_compiles_real_bundle(self) -> None:
        from harness_core.core.orchestrator import _promotion_target_for

        ws = self.tmp / "ws5"
        lifecycle = MemoryLifecycleManager(workspace_root=ws, global_home=self.global_home)
        ref = lifecycle.record_reflection("Rotate the API keys every 90 days without downtime")
        for _ in range(4):
            lifecycle.record_reference(ref["id"])

        cand = next(c for c in lifecycle.check_promotions() if c["id"] == ref["id"])
        self.assertEqual(_promotion_target_for(cand), "skill")

        ok = lifecycle.promote_entry(ref["id"], target="skill")
        self.assertTrue(ok, "skill promotion must compile a bundle")
        self.assertTrue(lifecycle._reflections[ref["id"]]["promoted"])
        bundles = list((ws / ".hex" / "skills").glob("*/SKILL.md"))
        self.assertTrue(bundles, "the compiled SKILL.md bundle must exist on disk")


class TestPromotedNotesStopReflagging(_UsagePromotionBase):
    def test_promoted_amem_item_is_not_reflagged(self) -> None:
        ws = self.tmp / "ws6"
        lifecycle = MemoryLifecycleManager(workspace_root=ws, global_home=self.global_home)
        item = MemoryItem(
            id="mem_episodic_test01",
            text="Use pgBouncer in front of Postgres under load",
            tags=[],
            scope="episodic",
            hit_counter=3,
        )
        lifecycle.controller._items[item.id] = item
        lifecycle.controller._save_metadata()

        self.assertTrue(any(c["id"] == item.id for c in lifecycle.check_promotions()))
        self.assertTrue(lifecycle.promote_entry(item.id, target="memory"))
        self.assertTrue(item.promoted)
        self.assertFalse(
            any(c["id"] == item.id for c in lifecycle.check_promotions()),
            "a promoted note must not be re-flagged on every subsequent task",
        )

    def test_promoted_reflection_is_not_reflagged(self) -> None:
        ws = self.tmp / "ws7"
        lifecycle = MemoryLifecycleManager(workspace_root=ws, global_home=self.global_home)
        ref = lifecycle.record_reflection("Cache the compiled wasm module between runs")
        lifecycle.record_reference(ref["id"])
        lifecycle.record_reference(ref["id"])

        self.assertTrue(any(c["id"] == ref["id"] for c in lifecycle.check_promotions()))
        self.assertTrue(lifecycle.promote_entry(ref["id"], target="memory"))
        self.assertFalse(any(c["id"] == ref["id"] for c in lifecycle.check_promotions()))


if __name__ == "__main__":
    unittest.main()
