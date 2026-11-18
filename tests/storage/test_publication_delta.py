"""B01 regression: verified-delta publication must preserve metadata and concurrent work.

Covers the publication contract at storage/cow.py::publish_delta and the
standard-task publication call site in the orchestrator. The pre-existing
fresh-directory clone contract (clone_to_workspace) is intentionally unchanged.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness_core.storage.cow import (
    APFSAndPortableCoWProvider,
    PublicationConflictError,
    compute_tree_sha,
    publish_delta,
)
from harness_core.core.models import TaskState, TaskSubmitRequest
from harness_core.core.orchestrator import HarnessOrchestrator


def _make_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "unrelated.txt").write_text("user file\n", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (root / ".hex").mkdir()
    (root / ".hex" / "state.json").write_text("{}\n", encoding="utf-8")
    (root / ".venv").mkdir()
    (root / ".venv" / "pyvenv.cfg").write_text("home = python\n", encoding="utf-8")


class TestPublishDeltaProvider(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="b01_pub_"))
        self.source = self.tmp / "repo"
        _make_repo(self.source)
        self.provider = APFSAndPortableCoWProvider(self.tmp / "snaps")
        self.base = self.provider.create_snapshot(self.source, label="base")
        self.attempt = self.tmp / "attempt"
        self.provider.clone_to_workspace(self.base.snapshot_path, self.attempt)
        # Task changes app.py and adds a new file.
        (self.attempt / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        (self.attempt / "feature.py").write_text("NEW = True\n", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_publication_preserves_metadata_and_unrelated_files(self) -> None:
        self.provider.publish_delta(self.base.snapshot_path, self.attempt, self.source)
        self.assertEqual((self.source / "app.py").read_text(), "VALUE = 2\n")
        self.assertTrue((self.source / "feature.py").is_file())
        self.assertTrue((self.source / ".git" / "HEAD").is_file())
        self.assertTrue((self.source / ".hex" / "state.json").is_file())
        self.assertTrue((self.source / ".venv" / "pyvenv.cfg").is_file())
        self.assertEqual((self.source / "unrelated.txt").read_text(), "user file\n")

    def test_concurrent_user_file_created_after_snapshot_survives(self) -> None:
        (self.source / "user_new_work.txt").write_text("in progress\n", encoding="utf-8")
        self.provider.publish_delta(self.base.snapshot_path, self.attempt, self.source)
        self.assertEqual((self.source / "user_new_work.txt").read_text(), "in progress\n")

    def test_concurrent_edit_to_changed_file_stops_publication(self) -> None:
        (self.source / "app.py").write_text("VALUE = 999  # user edit\n", encoding="utf-8")
        before_sha = compute_tree_sha(self.source)
        with self.assertRaises(PublicationConflictError) as ctx:
            self.provider.publish_delta(self.base.snapshot_path, self.attempt, self.source)
        self.assertIn("app.py", ctx.exception.conflicts)
        # Nothing mutated: user edit intact, no partial delta applied.
        self.assertEqual((self.source / "app.py").read_text(), "VALUE = 999  # user edit\n")
        self.assertFalse((self.source / "feature.py").exists())
        self.assertEqual(compute_tree_sha(self.source), before_sha)

    def test_concurrent_edit_to_unrelated_file_is_not_a_conflict(self) -> None:
        (self.source / "unrelated.txt").write_text("user updated\n", encoding="utf-8")
        self.provider.publish_delta(self.base.snapshot_path, self.attempt, self.source)
        self.assertEqual((self.source / "unrelated.txt").read_text(), "user updated\n")
        self.assertEqual((self.source / "app.py").read_text(), "VALUE = 2\n")

    def test_deletion_delta_applies_and_conflicts_on_user_edit(self) -> None:
        (self.attempt / "unrelated.txt").unlink()
        # User touched the file the task deleted -> conflict, file kept.
        (self.source / "unrelated.txt").write_text("keep me\n", encoding="utf-8")
        with self.assertRaises(PublicationConflictError):
            self.provider.publish_delta(self.base.snapshot_path, self.attempt, self.source)
        self.assertEqual((self.source / "unrelated.txt").read_text(), "keep me\n")

    def test_deletion_delta_applies_when_unchanged(self) -> None:
        (self.attempt / "unrelated.txt").unlink()
        self.provider.publish_delta(self.base.snapshot_path, self.attempt, self.source)
        self.assertFalse((self.source / "unrelated.txt").exists())
        self.assertTrue((self.source / ".git" / "HEAD").is_file())

    def test_apply_failure_rolls_back_already_applied_changes(self) -> None:
        real_copy2 = shutil.copy2
        calls = {"n": 0}

        def flaky_copy2(src, dst, *a, **kw):
            # Fail on the second *destination* copy (first is a backup copy).
            dst_p = Path(dst)
            if dst_p.is_relative_to(self.source):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise OSError("simulated disk failure")
            return real_copy2(src, dst, *a, **kw)

        with mock.patch("harness_core.storage.cow.shutil.copy2", side_effect=flaky_copy2):
            with self.assertRaises(OSError):
                self.provider.publish_delta(self.base.snapshot_path, self.attempt, self.source)
        # First applied change (app.py) must have been rolled back.
        self.assertEqual((self.source / "app.py").read_text(), "VALUE = 1\n")
        self.assertFalse((self.source / "feature.py").exists())


class TestStandardTaskPublication(unittest.TestCase):
    """Full standard-task publication in a repo with .git/.hex/.venv and concurrent work."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="b01_task_"))
        self.ws = self.tmp / "repo"
        _make_repo(self.ws)
        (self.ws / "test_app.py").write_text(
            "import unittest\n"
            "from app import VALUE\n"
            "class T(unittest.TestCase):\n"
            "    def test_value(self):\n"
            "        self.assertGreaterEqual(VALUE, 1)\n",
            encoding="utf-8",
        )
        self.orch = HarnessOrchestrator(
            data_dir=self.tmp / "harness_data",
            default_workspace=self.ws,
        )

    def tearDown(self) -> None:
        if hasattr(self, "orch"):
            self.orch.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _submit_edit(self) -> dict:
        req = TaskSubmitRequest(
            goal="Update VALUE in app.py",
            execution_mode="standard",
            workspace_root=str(self.ws),
            steps=[
                {
                    "step_id": "edit_app",
                    "tool_id": "write_file",
                    "args": {"path": "app.py", "content": "VALUE = 2\n"},
                    "risk_class": "R2",
                }
            ],
        )
        return self.orch.submit_and_run(req, auto_execute=True)

    def test_success_preserves_git_hex_venv_and_concurrent_work(self) -> None:
        (self.ws / "user_notes.md").write_text("concurrent notes\n", encoding="utf-8")
        task = self._submit_edit()
        self.assertEqual(task["status"], TaskState.SUCCEEDED.value)
        self.assertEqual((self.ws / "app.py").read_text(), "VALUE = 2\n")
        self.assertTrue((self.ws / ".git" / "HEAD").is_file(), ".git metadata destroyed")
        self.assertTrue((self.ws / ".hex" / "state.json").is_file(), ".hex state destroyed")
        self.assertTrue((self.ws / ".venv" / "pyvenv.cfg").is_file(), ".venv destroyed")
        self.assertEqual((self.ws / "user_notes.md").read_text(), "concurrent notes\n")

    def test_conflicting_concurrent_edit_stops_publication(self) -> None:
        # Simulate the user editing app.py after the base snapshot was taken by
        # hooking create_snapshot: right after the base snapshot of source_ws,
        # mutate the live file.
        real_create = self.orch.cow.create_snapshot
        fired = {"done": False}

        def create_then_user_edit(source_dir, label="chk"):
            snap = real_create(source_dir, label=label)
            if not fired["done"] and Path(source_dir).resolve() == self.ws.resolve():
                fired["done"] = True
                (self.ws / "app.py").write_text("VALUE = 42  # user\n", encoding="utf-8")
            return snap

        with mock.patch.object(self.orch.cow, "create_snapshot", side_effect=create_then_user_edit):
            task = self._submit_edit()
        self.assertTrue(fired["done"], "hook never fired; test invalid")
        self.assertEqual(task["status"], TaskState.NEEDS_ATTENTION.value)
        self.assertIn("Publication stopped", task.get("terminal_reason") or "")
        events = self.orch.ledger.list_events(task["task_id"])
        self.assertTrue(
            any(e.get("reason_code") == "PUBLICATION_CONFLICT" for e in events),
            "no PUBLICATION_CONFLICT event in journal",
        )
        self.assertEqual((self.ws / "app.py").read_text(), "VALUE = 42  # user\n")
        self.assertTrue((self.ws / ".git" / "HEAD").is_file())


if __name__ == "__main__":
    unittest.main()