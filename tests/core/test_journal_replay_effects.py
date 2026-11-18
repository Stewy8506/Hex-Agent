"""B36 regression: journal replay is bound to the restored workspace.

A crash can roll the workspace back to a checkpoint that predates a sealed
call. Replay must not report that call as done when its effect is absent; the
gateway re-executes instead so the tree and the journal agree again.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from harness_core.gateway import ToolGateway, sealed_effect_state
from harness_core.storage.db import LedgerStore


class TestJournalReplayEffects(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.ws = (Path(self.temp_dir) / "workspace").resolve()
        self.ws.mkdir(parents=True)
        (self.ws / "app.py").write_text("original = True\n", encoding="utf-8")
        self.ledger = LedgerStore(Path(self.temp_dir) / "ledger.db")
        self.gateway = ToolGateway(self.ledger)
        self.task = self.ledger.submit_task(
            {"goal": "journal replay", "execution_mode": "standard"}
        )
        self.task_id = self.task["task_id"]

    def tearDown(self) -> None:
        self.ledger.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _invoke_write(self, path: str, content: str, sequence: int = 1) -> dict:
        return self.gateway.invoke(
            task_id=self.task_id,
            attempt_id="att_replay",
            step_id="step_1",
            sequence_in_step=sequence,
            tool_id="write_file",
            args={"path": path, "content": content},
            workspace_dir=self.ws,
        )

    def test_rolled_back_write_is_reapplied_not_replayed(self) -> None:
        first = self._invoke_write("generated.py", "value = 42\n")
        self.assertTrue(first["ok"])
        self.assertFalse(first["_replayed"])
        created = self.ws / "generated.py"
        self.assertTrue(created.is_file())

        # Simulate the crash rollback that removes the uncommitted effect.
        created.unlink()

        second = self._invoke_write("generated.py", "value = 42\n")
        self.assertTrue(second["ok"])
        self.assertFalse(second["_replayed"], "a missing effect must be re-executed")
        self.assertEqual(created.read_text(encoding="utf-8"), "value = 42\n")

    def test_write_reverted_to_older_content_is_reapplied(self) -> None:
        self._invoke_write("app.py", "patched = True\n")
        # Checkpoint restore puts the pre-call content back.
        (self.ws / "app.py").write_text("original = True\n", encoding="utf-8")

        result = self._invoke_write("app.py", "patched = True\n")
        self.assertFalse(result["_replayed"])
        self.assertEqual((self.ws / "app.py").read_text(encoding="utf-8"), "patched = True\n")

    def test_intact_effect_replays_without_executing_handler(self) -> None:
        first = self._invoke_write("stable.py", "stable = 1\n")
        self.assertTrue(first["ok"])

        original_handler = self.gateway.tools["write_file"].handler

        def _fail(*args, **kwargs):
            raise AssertionError("handler must not run when the sealed effect is present")

        self.gateway.tools["write_file"].handler = _fail
        try:
            replayed = self._invoke_write("stable.py", "stable = 1\n")
        finally:
            self.gateway.tools["write_file"].handler = original_handler

        self.assertTrue(replayed["_replayed"])
        self.assertEqual(replayed["_replayed_from_call_id"], first["_call_id"])
        self.assertEqual((self.ws / "stable.py").read_text(encoding="utf-8"), "stable = 1\n")

    def test_patch_effect_is_verified_before_replay(self) -> None:
        first = self.gateway.invoke(
            task_id=self.task_id,
            attempt_id="att_replay",
            step_id="step_2",
            sequence_in_step=1,
            tool_id="patch",
            args={"path": "app.py", "find": "original = True", "replace": "patched = True"},
            workspace_dir=self.ws,
        )
        self.assertTrue(first["ok"])
        self.assertEqual(
            (self.ws / "app.py").read_text(encoding="utf-8"), "patched = True\n"
        )

        # Roll the file back to its original content.
        (self.ws / "app.py").write_text("original = True\n", encoding="utf-8")

        second = self.gateway.invoke(
            task_id=self.task_id,
            attempt_id="att_replay",
            step_id="step_2",
            sequence_in_step=1,
            tool_id="patch",
            args={"path": "app.py", "find": "original = True", "replace": "patched = True"},
            workspace_dir=self.ws,
        )
        self.assertFalse(second["_replayed"])
        self.assertEqual(
            (self.ws / "app.py").read_text(encoding="utf-8"), "patched = True\n"
        )

    def test_later_edit_is_preserved_and_marked_as_mismatch(self) -> None:
        self._invoke_write("edited.py", "first = 1\n")
        (self.ws / "edited.py").write_text("user_edit = True\n", encoding="utf-8")

        replayed = self._invoke_write("edited.py", "first = 1\n")
        self.assertTrue(replayed["_replayed"])
        self.assertTrue(replayed.get("_replay_effect_mismatch"))
        # Replay must not clobber work that landed after the sealed call.
        self.assertEqual(
            (self.ws / "edited.py").read_text(encoding="utf-8"), "user_edit = True\n"
        )

    def test_sealed_effect_state_classification(self) -> None:
        from harness_core.gateway import sha256_hex

        current = sha256_hex((self.ws / "app.py").read_bytes())
        present = {
            "tool_id": "write_file",
            "result": {"ok": True, "path": "app.py", "sha256": current},
        }
        self.assertEqual(sealed_effect_state(present, self.ws), "present")

        rolled_back = {
            "tool_id": "write_file",
            "result": {
                "ok": True,
                "path": "app.py",
                "sha256": "0" * 64,
                "_before_sha256": current,
            },
        }
        self.assertEqual(sealed_effect_state(rolled_back, self.ws), "rolled_back")

        diverged = {
            "tool_id": "write_file",
            "result": {"ok": True, "path": "app.py", "sha256": "0" * 64},
        }
        self.assertEqual(sealed_effect_state(diverged, self.ws), "diverged")

        # Non-file tools are not gated by file content.
        self.assertEqual(
            sealed_effect_state({"tool_id": "read_file", "result": {}}, self.ws),
            "present",
        )


if __name__ == "__main__":
    unittest.main()
