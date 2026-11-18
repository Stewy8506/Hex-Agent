"""B40 regression: dialogue turns reach the ledger and /rewind stays honest.

``dialogue_turns`` had writers but no caller, so a rewind rebuilt from an empty
transcript, and the TUI claimed tool executions were reverted when only
conversation rows had moved.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from harness_core.core.react_loop import ReActConfig, ReActLoop
from harness_core.storage.db import LedgerStore
from harness_core.storage.timeline_rewind import TimelineRewindManager


class TestDialoguePersistence(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="harness_dialogue_"))
        self.ledger = LedgerStore(self.tmp_dir / "ledger.db")

    def tearDown(self) -> None:
        self.ledger.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_completed_turn_is_persisted(self) -> None:
        loop = ReActLoop(
            model_caller=lambda messages: "All done.",
            ledger=self.ledger,
            config=ReActConfig(max_turns=2),
        )
        loop.run("Please summarize the change", task_id="tsk_dialogue")

        rows = self.ledger.get_active_dialogue("tsk_dialogue")
        self.assertTrue(rows)
        self.assertEqual(rows[0]["role"], "user")
        self.assertIn("Please summarize the change", rows[0]["content"])
        self.assertTrue(any(row["role"] == "assistant" for row in rows))
        self.assertTrue(any(row["content"] == "All done." for row in rows))


class TestRewindHonesty(unittest.TestCase):
    def test_only_archived_tool_messages_are_counted(self) -> None:
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "answer one"},
            {"role": "tool", "content": "old tool output"},
            {"role": "user", "content": "second"},
            {"role": "assistant", "content": "answer two"},
            {"role": "tool", "content": "new tool output"},
        ]
        result = TimelineRewindManager(ledger=None).rewind(
            task_id="tsk_rewind", target=1, messages=messages
        )

        self.assertEqual(result.turns_rewound, 1)
        self.assertEqual(result.reverted_tool_count, 1)
        self.assertNotIn(
            "new tool output",
            [message.get("content") for message in result.live_messages],
        )


if __name__ == "__main__":
    unittest.main()
