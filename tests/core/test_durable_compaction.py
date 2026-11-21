"""B59 regression: ReAct compaction is durable and reconstructable on resume.

Before B59 the loop compacted ``self.messages`` in memory only: the summary was
never persisted, no production code wrote a compaction checkpoint, and a resumed
run replayed raw turns (or nothing at all). Now every successful compaction
persists a checkpoint (summary + cut boundary) into ``dialogue_turns`` via
``LedgerStore.save_compaction_checkpoint``, and ``reconstruct_active_dialogue``
rebuilds the compacted window (checkpoint + post-cut rows) for resume, which
``run_agent_loop(resume_from_task_id=...)`` and ``resume_task`` wire into the
ReAct loop.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from harness_core.core.models import TaskState, TaskSubmitRequest
from harness_core.core.orchestrator import HarnessOrchestrator
from harness_core.core.react_loop import ReActConfig, ReActLoop
from harness_core.cognition.context_compressor import CompactionConfig, ContextCompactor
from harness_core.storage.db import LedgerStore


CHECKPOINT_TYPE = "compaction_checkpoint"
SUMMARY_MARKER = "Investigated"


def _compactor(budget: int) -> ContextCompactor:
    # Small protection windows so a short synthetic conversation has a real
    # compressible middle; production defaults protect the last 6 messages.
    return ContextCompactor(
        CompactionConfig(
            max_context_tokens=budget,
            trigger_threshold=0.8,
            protect_last_n=2,
            keep_tool_rounds=1,
        ),
    )


class _MultiTurnCaller:
    """Emits long tool-call turns (forcing compaction), then a final answer."""

    def __init__(self, tool_turns: int = 4) -> None:
        self.tool_turns = tool_turns
        self.turn = 0
        # Varied tokens: repetitive filler would trip the degenerate-repetition sentry.
        self.filler = " ".join(
            f"module{i} handles case{i} via path{i} returning value{i}." for i in range(180)
        )

    def __call__(self, messages: list[dict[str, Any]]) -> str:
        self.turn += 1
        if self.turn <= self.tool_turns:
            # Filler in visible assistant text: <thought> blocks are stripped
            # from history, so they would never grow the context enough to
            # cross the compaction threshold.
            return (
                f"{self.filler} pass {self.turn}\n"
                f'<tool_call>{{"name": "search_files", "arguments": {{"pattern": "sym_{self.turn}"}}}}</tool_call>'
            )
        return "<finish>All investigations complete.</finish>"


class TestDurableCompactionCheckpoint(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="hex_durable_compaction_"))
        self.ledger = LedgerStore(self.tmp_dir / "ledger.db")

    def tearDown(self) -> None:
        self.ledger.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_midrun_compaction_persists_checkpoint(self) -> None:
        task_id = "tsk_react_durable"
        loop = ReActLoop(
            model_caller=_MultiTurnCaller(),
            ledger=self.ledger,
            config=ReActConfig(max_turns=8, budget_tokens=1200),
            context_compactor=_compactor(1200),
        )
        result = loop.run("Investigate the module thoroughly", task_id=task_id)
        self.assertTrue(result.success)

        checkpoint = self.ledger.load_compaction_checkpoint(task_id)
        self.assertIsNotNone(checkpoint, "compaction must persist a durable checkpoint")
        self.assertEqual(checkpoint["metadata"]["type"], CHECKPOINT_TYPE)
        self.assertGreater(len(checkpoint["content"]), 0)
        self.assertGreaterEqual(int(checkpoint["metadata"]["cut_turn_index"]), 1)
        # The checkpoint row is active and carries the summary + cut boundary.
        active = self.ledger.get_active_dialogue(task_id)
        self.assertTrue(any(r["turn_id"] == checkpoint["turn_id"] for r in active))

    def test_preflight_compaction_persists_checkpoint(self) -> None:
        task_id = "tsk_react_preflight"
        old_turns = [
            {"role": "user", "content": "earlier question " * 400},
            {"role": "assistant", "content": "earlier answer " * 400},
        ]
        loop = ReActLoop(
            model_caller=lambda messages: "<finish>done</finish>",
            ledger=self.ledger,
            config=ReActConfig(max_turns=2, budget_tokens=1500),
            context_compactor=_compactor(1500),
        )
        loop.run("Follow-up question", task_id=task_id, initial_messages=old_turns)

        checkpoint = self.ledger.load_compaction_checkpoint(task_id)
        self.assertIsNotNone(checkpoint, "preflight compaction must persist a checkpoint")
        # Only the resumed prompt row (turn 0) is recorded when the preflight fires,
        # so the checkpoint's cut boundary covers it.
        self.assertGreaterEqual(int(checkpoint["metadata"]["cut_turn_index"]), 1)

    def test_reconstruct_without_checkpoint_returns_raw_transcript(self) -> None:
        task_id = "tsk_recon_raw"
        self.ledger.record_dialogue_turn(task_id, 0, "user", "hello there")
        self.ledger.record_dialogue_turn(task_id, 1, "assistant", "hi, ready to work")

        window = self.ledger.reconstruct_active_dialogue(task_id)
        self.assertEqual(len(window["messages"]), 2)
        self.assertEqual(window["messages"][0]["role"], "user")
        self.assertEqual(window["messages"][1]["content"], "hi, ready to work")
        self.assertEqual(window["next_turn_index"], 2)
        self.assertIsNone(window["checkpoint"])

    def test_reconstruct_honors_cut_boundary(self) -> None:
        task_id = "tsk_recon_cut"
        self.ledger.record_dialogue_turn(task_id, 0, "user", "first prompt")
        self.ledger.record_dialogue_turn(task_id, 1, "assistant", "worked a bit")
        self.ledger.record_dialogue_turn(task_id, 2, "tool", "old observation")
        self.ledger.record_dialogue_turn(task_id, 4, "assistant", "kept working")
        self.ledger.record_dialogue_turn(task_id, 5, "tool", "recent observation")
        self.ledger.save_compaction_checkpoint(
            task_id,
            summary_text=f"{SUMMARY_MARKER}: covered turns 0-2",
            cut_turn_index=3,
            metadata={"original_tokens": 900, "compacted_tokens": 300},
        )

        window = self.ledger.reconstruct_active_dialogue(task_id)
        # Window = checkpoint summary + rows strictly after the cut boundary.
        self.assertEqual(len(window["messages"]), 3)
        self.assertIn(SUMMARY_MARKER, window["messages"][0]["content"])
        self.assertEqual(window["messages"][0]["role"], "system")
        self.assertEqual(window["messages"][1]["content"], "kept working")
        self.assertEqual(window["messages"][2]["content"], "recent observation")
        contents = [m["content"] for m in window["messages"]]
        self.assertNotIn("first prompt", contents)
        self.assertNotIn("old observation", contents)
        self.assertEqual(window["next_turn_index"], 6)
        self.assertIsNotNone(window["checkpoint"])

    def test_reconstruct_uses_newest_checkpoint(self) -> None:
        task_id = "tsk_recon_newest"
        for idx in range(6):
            self.ledger.record_dialogue_turn(task_id, idx, "assistant", f"turn {idx} content")
        self.ledger.save_compaction_checkpoint(
            task_id, summary_text="older checkpoint", cut_turn_index=2
        )
        self.ledger.save_compaction_checkpoint(
            task_id, summary_text=f"{SUMMARY_MARKER} newest checkpoint", cut_turn_index=4
        )

        window = self.ledger.reconstruct_active_dialogue(task_id)
        self.assertIn("newest checkpoint", window["messages"][0]["content"])
        self.assertNotIn("older checkpoint", [m["content"] for m in window["messages"]])
        # Window = newest checkpoint + rows strictly after cut=4 (row 5); the
        # row AT the cut is covered by the summary.
        self.assertEqual(len(window["messages"]), 2)
        self.assertEqual(window["messages"][1]["content"], "turn 5 content")
        self.assertEqual(window["next_turn_index"], 6)

    def test_resume_run_continues_durable_window(self) -> None:
        task_id = "tsk_react_resume"

        first = ReActLoop(
            model_caller=_MultiTurnCaller(),
            ledger=self.ledger,
            config=ReActConfig(max_turns=8, budget_tokens=1200),
            context_compactor=_compactor(1200),
        )
        self.assertTrue(first.run("Investigate the module thoroughly", task_id=task_id).success)
        window = self.ledger.reconstruct_active_dialogue(task_id)
        checkpoint = window["checkpoint"]
        self.assertIsNotNone(checkpoint)
        cut = int(checkpoint["metadata"]["cut_turn_index"])
        pre_cut_rows = [
            r
            for r in self.ledger.get_active_dialogue(task_id)
            if int(r["turn_index"]) < cut
            and (r.get("metadata") or {}).get("type") != CHECKPOINT_TYPE
        ]
        self.assertTrue(pre_cut_rows, "raw rows before the cut stay active for forensics")
        self.assertNotIn(
            pre_cut_rows[0]["content"], [m["content"] for m in window["messages"]],
            "reconstruction must not replay pre-cut raw turns",
        )

        second = ReActLoop(
            model_caller=lambda messages: "<finish>Resumed and finished.</finish>",
            ledger=self.ledger,
            config=ReActConfig(max_turns=3, budget_tokens=1200),
            context_compactor=_compactor(1200),
            dialogue_turn_base=window["next_turn_index"],
        )
        second.run("continue the work", task_id=task_id, initial_messages=window["messages"])

        final_window = self.ledger.reconstruct_active_dialogue(task_id)
        contents = [m["content"] for m in final_window["messages"]]
        self.assertIn("continue the work", contents)
        self.assertIn("Resumed and finished.", contents)
        # The resumed prompt row lands after every pre-existing row, not at index 0.
        rows = self.ledger.get_active_dialogue(task_id)
        resumed_prompt = next(r for r in rows if r["content"] == "continue the work")
        self.assertGreaterEqual(
            int(resumed_prompt["turn_index"]), window["next_turn_index"]
        )


class TestResumeTaskReactRouting(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="hex_resume_routing_"))
        self.ws = self.tmp_dir / "ws"
        self.ws.mkdir(parents=True, exist_ok=True)
        self.orch = HarnessOrchestrator(self.tmp_dir / "data", default_workspace=self.ws)

    def tearDown(self) -> None:
        self.orch.shutdown()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _react_task(self, goal: str) -> str:
        task = self.orch.ledger.create_task(
            TaskSubmitRequest(
                goal=goal,
                execution_mode="react_loop",
                workspace_root=str(self.ws),
            )
        )
        task_id = task["task_id"]
        self.orch.ledger.transition_task(task_id, TaskState.VALIDATING, reason_code="TEST_SETUP")
        self.orch.ledger.transition_task(task_id, TaskState.RUNNING, reason_code="TEST_SETUP")
        return task_id

    def test_resume_task_routes_react_loop_with_durable_window(self) -> None:
        task_id = self._react_task("Investigate and fix")

        with patch.object(
            HarnessOrchestrator, "run_agent_loop", return_value={"task_id": task_id}
        ) as mock_run:
            result = self.orch.resume_task(task_id)

        self.assertEqual(result.get("task_id"), task_id)
        mock_run.assert_called_once()
        kwargs = mock_run.call_args.kwargs
        self.assertEqual(kwargs["goal"], "Investigate and fix")
        self.assertEqual(kwargs["task_id"], task_id)
        self.assertEqual(kwargs["resume_from_task_id"], task_id)

    def test_resume_task_standard_mode_still_uses_dag_executor(self) -> None:
        task = self.orch.ledger.create_task(
            TaskSubmitRequest(
                goal="standard dag work",
                execution_mode="standard",
                workspace_root=str(self.ws),
                steps=[
                    {
                        "step_id": "step_1_inspect",
                        "tool_id": "read_file",
                        "args": {"path": "README.md"},
                        "risk_class": "R0",
                    }
                ],
            )
        )
        task_id = task["task_id"]
        self.orch.ledger.transition_task(task_id, TaskState.VALIDATING, reason_code="TEST_SETUP")
        self.orch.ledger.transition_task(task_id, TaskState.RUNNING, reason_code="TEST_SETUP")

        with patch.object(
            HarnessOrchestrator, "_execute_standard_dag", return_value=task
        ) as mock_dag:
            result = self.orch.resume_task(task_id)

        self.assertIs(result, task)
        mock_dag.assert_called_once()


if __name__ == "__main__":
    unittest.main()