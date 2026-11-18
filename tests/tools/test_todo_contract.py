"""B07/B08 regression: todo contract matches its schema and persists honestly.

B07: the schema-advertised `complete` action must actually complete a checklist
item, and unsupported actions must be rejected instead of returning ok.
B08: persistence is atomic, failures propagate, ids stay unique after deletion,
and statuses are a finite set.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from harness_core.gateway import ToolGateway
from harness_core.storage.db import LedgerStore


class TestTodoContract(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="harness_todo_"))
        self.ws = self.tmp_dir / "workspace"
        self.ws.mkdir(parents=True)
        self.ledger = LedgerStore(self.tmp_dir / "ledger.db")
        self.gateway = ToolGateway(self.ledger)
        self.todo = self.gateway.tools["todo"].handler
        self.todo_file = self.ws / ".hex" / "todo.json"

    def tearDown(self) -> None:
        self.ledger.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_schema_complete_action_marks_item_completed(self) -> None:
        self.todo(self.ws, {"action": "add", "task": "Wire the parser"})
        res = self.todo(self.ws, {"action": "complete", "task": "Wire the parser"})

        self.assertTrue(res["ok"])
        self.assertEqual(res["todos"][0]["status"], "completed")
        persisted = json.loads(self.todo_file.read_text(encoding="utf-8"))
        self.assertEqual(persisted[0]["status"], "completed")

    def test_complete_reports_missing_match(self) -> None:
        res = self.todo(self.ws, {"action": "complete", "task": "nonexistent"})
        self.assertFalse(res["ok"])
        self.assertIn("No matching todo item", res["error"])

    def test_unsupported_action_is_rejected(self) -> None:
        self.todo(self.ws, {"action": "add", "task": "keep me"})
        res = self.todo(self.ws, {"action": "archive_everything"})
        self.assertFalse(res["ok"])
        self.assertIn("Unsupported todo action", res["error"])
        # The rejected action did not rewrite the persisted checklist.
        self.assertEqual(len(json.loads(self.todo_file.read_text(encoding="utf-8"))), 1)

    def test_invalid_status_is_rejected_without_writing(self) -> None:
        self.todo(self.ws, {"action": "add", "task": "task one"})
        res = self.todo(self.ws, {"action": "update", "index": 0, "status": "banana"})
        self.assertFalse(res["ok"])
        self.assertIn("Invalid todo status", res["error"])
        persisted = json.loads(self.todo_file.read_text(encoding="utf-8"))
        self.assertEqual(persisted[0]["status"], "pending")

        res_add = self.todo(
            self.ws, {"action": "add", "items": [{"task": "bad", "status": "done"}]}
        )
        self.assertFalse(res_add["ok"])

    def test_ids_stay_unique_after_delete(self) -> None:
        self.todo(self.ws, {"action": "add", "items": ["one", "two", "three"]})
        self.todo(self.ws, {"action": "remove", "index": 1})
        res = self.todo(self.ws, {"action": "add", "task": "four"})

        ids = [str(td["id"]) for td in res["todos"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 3)

    def test_corrupt_state_is_reported_not_silently_reset(self) -> None:
        self.todo_file.parent.mkdir(parents=True, exist_ok=True)
        self.todo_file.write_text("{not valid json", encoding="utf-8")

        res = self.todo(self.ws, {"action": "read"})
        self.assertFalse(res["ok"])
        self.assertIn("unreadable", res["error"])

        # An explicit clear is still allowed to recover the checklist.
        recovered = self.todo(self.ws, {"action": "clear"})
        self.assertTrue(recovered["ok"])
        self.assertEqual(json.loads(self.todo_file.read_text(encoding="utf-8")), [])

    def test_persistence_failure_propagates(self) -> None:
        # A file where the state directory belongs makes mkdir fail.
        blocking = self.ws / ".hex"
        blocking.write_text("not a directory", encoding="utf-8")

        with self.assertRaises(OSError) as ctx:
            self.todo(self.ws, {"action": "add", "task": "cannot persist"})
        self.assertIn("Failed to persist todo state", str(ctx.exception))

    def test_write_is_atomic_and_leaves_no_temp_file(self) -> None:
        self.todo(self.ws, {"action": "add", "task": "atomic"})
        self.assertFalse((self.todo_file.parent / "todo.json.tmp").exists())
        self.assertEqual(json.loads(self.todo_file.read_text(encoding="utf-8"))[0]["task"], "atomic")


if __name__ == "__main__":
    unittest.main()
