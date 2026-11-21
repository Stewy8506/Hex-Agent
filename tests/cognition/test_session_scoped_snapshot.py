"""B68 regression: the frozen Turn-0 memory snapshot must be stable per
conversation (not re-hydrated per task), the snapshot cache must be bounded,
and the ReAct path must scrub leaked memory fences from its final answer.

Before B68: ``run_agent_loop`` minted a fresh task UUID per invocation and
passed it as the prompt ``session_id``, so every task re-hydrated the frozen
snapshot (defeating the Turn-0 freeze and re-crediting usage counts);
``MemorySnapshot._cached_snapshots`` grew without bound; and the ReAct loop
delivered its final answer without ``sanitize_context`` (only the fast path
scrubbed), so a model that parroted its ``<memory-context>`` prefix leaked
recalled memory into the user-visible answer.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from harness_core.cognition.memory import CuratedMemoryStore, MemorySnapshot
from harness_core.core.orchestrator import HarnessOrchestrator


def _mock_router(finish_text: str) -> MagicMock:
    router = MagicMock()
    router.cloud_provider.is_available.return_value = True
    router.execute_routed_request.return_value = (
        MagicMock(),
        SimpleNamespace(text=finish_text),
    )
    router.get_context_compactor.return_value = None
    return router


class TestSessionScopedFrozenSnapshot(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="hex_b68_snapshot_"))
        self.workspace = self.tmp_dir / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.global_home = self.tmp_dir / "global_home"
        self.global_home.mkdir(parents=True, exist_ok=True)
        MemorySnapshot.clear_all_snapshots()

    def tearDown(self) -> None:
        MemorySnapshot.clear_all_snapshots()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _build_orchestrator(self, finish_text: str) -> HarnessOrchestrator:
        orch = HarnessOrchestrator(
            data_dir=self.tmp_dir / "data",
            default_workspace=self.workspace,
        )
        orch.soul_manager.global_home = self.global_home
        orch.router = _mock_router(finish_text)
        return orch

    def _run_react(self, orch: HarnessOrchestrator, goal: str) -> dict:
        return orch.run_agent_loop(
            goal=goal,
            workspace_root=self.workspace,
            demo_mode=False,
        )

    def test_react_runs_share_one_frozen_snapshot(self) -> None:
        """B68 core: a second task in the same conversation keeps the Turn-0 freeze."""
        orch = self._build_orchestrator("<finish>done</finish>")
        store = CuratedMemoryStore(workspace_root=self.workspace, global_home=self.global_home)
        store.add("B68_EARLY_NOTE: initial knowledge", target="memory")

        result1 = self._run_react(orch, "first prompt")
        self.assertTrue(result1.get("success"))
        prompt1 = orch.router.execute_routed_request.call_args.kwargs["system_prompt"]
        self.assertIn("B68_EARLY_NOTE", prompt1)

        # Memory mutates on disk between the two conversation turns.
        store.add("B68_LATE_NOTE: added mid-conversation", target="memory")

        result2 = self._run_react(orch, "second prompt")
        prompt2 = orch.router.execute_routed_request.call_args.kwargs["system_prompt"]
        # Still frozen at the conversation's Turn-0: the late note must NOT appear.
        self.assertNotIn("B68_LATE_NOTE", prompt2)
        self.assertIn("B68_EARLY_NOTE", prompt2)

    def test_conversation_id_preferred_from_constraints(self) -> None:
        """A caller-supplied conversation id (ACP session) keys the snapshot; without
        one, the id is stable per orchestrator instead of per task."""
        orch = self._build_orchestrator("<finish>done</finish>")

        self.assertEqual(
            orch._snapshot_session_id({"constraints": {"conversation_id": "acp_sess_x"}}, "task_a"),
            "acp_sess_x",
        )
        conv_a = orch._snapshot_session_id({}, "task_a")
        conv_b = orch._snapshot_session_id({}, "task_b")
        self.assertEqual(conv_a, conv_b)
        self.assertNotEqual(conv_a, "task_a")

    def test_react_final_answer_scrubs_memory_leak(self) -> None:
        """A model that parrots its <memory-context> prefix must not leak it to the user."""
        leak_text = (
            "<finish>Here is the answer. "
            "<memory-context>B68_MEMORY_SECRET recalled context</memory-context> "
            "Done.</finish>"
        )
        orch = self._build_orchestrator(leak_text)
        result = self._run_react(orch, "tell me the secret")

        self.assertTrue(result.get("success"))
        final_answer = str(result.get("final_answer") or "")
        self.assertNotIn("B68_MEMORY_SECRET", final_answer)
        self.assertNotIn("<memory-context>", final_answer)
        self.assertIn("Here is the answer", final_answer)


class TestSnapshotCacheBound(unittest.TestCase):
    def setUp(self) -> None:
        MemorySnapshot.clear_all_snapshots()

    def tearDown(self) -> None:
        MemorySnapshot.clear_all_snapshots()

    def test_snapshot_cache_is_bounded(self) -> None:
        """B68: the class-level snapshot cache must not grow without bound."""
        cap = MemorySnapshot.MAX_CACHED_SNAPSHOTS
        for i in range(cap + 10):
            MemorySnapshot._store_snapshot(f"ws:home:session_{i}", f"snap_{i}")

        self.assertLessEqual(len(MemorySnapshot._cached_snapshots), cap)
        self.assertEqual(len(MemorySnapshot._cached_snapshots), cap)
        # Oldest entries evicted, newest retained.
        self.assertNotIn("ws:home:session_0", MemorySnapshot._cached_snapshots)
        self.assertIn(f"ws:home:session_{cap + 9}", MemorySnapshot._cached_snapshots)
        self.assertEqual(
            MemorySnapshot._cached_snapshots[f"ws:home:session_{cap + 9}"], f"snap_{cap + 9}"
        )


if __name__ == "__main__":
    unittest.main()
