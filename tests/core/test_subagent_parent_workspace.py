"""B17 regression: subagent diffs use the explicit parent workspace.

The child workspace must record its actual parent at spawn and diff against
it, never a directory inferred by walking three levels up (which points at an
unrelated directory for nested or temporary child workspaces).
"""

from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path

from harness_core.core.subagent import (
    SubagentManager,
    SubagentRecord,
    SubagentRole,
    SubagentStatus,
)
from harness_core.storage.cow import get_default_snapshot_provider
from harness_core.storage.db import LedgerStore


class TestSubagentParentWorkspace(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.root = Path(self.temp_dir).resolve()
        self.parent = self.root / "parent"
        self.parent.mkdir(parents=True)
        (self.parent / "app.py").write_text("line_original\n", encoding="utf-8")
        self.ledger = LedgerStore(self.root / "ledger.db")
        self.cow = get_default_snapshot_provider(self.root / "cow")
        self.mgr = SubagentManager(
            ledger=self.ledger, cow=self.cow, max_depth=2, demo_mode=True
        )

    def tearDown(self) -> None:
        self.ledger.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _child(self, rel: str) -> Path:
        child = self.parent / rel
        child.mkdir(parents=True, exist_ok=True)
        (child / "app.py").write_text("line_modified\n", encoding="utf-8")
        return child

    def _record(self, child: Path, parent_ws: Path | None) -> SubagentRecord:
        return SubagentRecord(
            subagent_id="sub_b17",
            parent_task_id="tsk_b17",
            role=SubagentRole.GENERAL,
            goal="diff check",
            depth=1,
            status=SubagentStatus.PENDING,
            started_at=time.time(),
            workspace_path=child,
            parent_workspace=parent_ws,
        )

    def test_spawn_records_explicit_parent(self) -> None:
        res = self.mgr.spawn_subagent(
            parent_task_id="tsk_b17",
            goal="work",
            role=SubagentRole.RESEARCHER,
            parent_ws=self.parent,
            wait=True,
        )
        record = self.mgr._active_subagents[res.subagent_id]
        self.assertEqual(record.parent_workspace, self.parent.resolve())

    def test_temp_spawn_has_no_parent(self) -> None:
        res = self.mgr.spawn_subagent(
            parent_task_id="tsk_b17",
            goal="work",
            role=SubagentRole.RESEARCHER,
            parent_ws=None,
            wait=True,
        )
        record = self.mgr._active_subagents[res.subagent_id]
        self.assertIsNone(record.parent_workspace)

    def test_nested_child_diffs_against_real_parent(self) -> None:
        # Three levels up from this nested child is `.hex`, NOT the parent.
        child = self._child(".hex/subagents/group/sub_x")
        record = self._record(child, self.parent.resolve())
        self.mgr._run_subagent_sync(record, timeout=5.0)
        self.assertEqual(record.status, SubagentStatus.COMPLETED)
        diff = record.result.diff_summary
        self.assertIn("app.py", diff)
        self.assertIn("line_original", diff)
        self.assertIn("line_modified", diff)

    def test_no_parent_yields_no_diff(self) -> None:
        child = self._child(".hex/subagents/group/sub_y")
        record = self._record(child, None)
        self.mgr._run_subagent_sync(record, timeout=5.0)
        self.assertEqual(record.status, SubagentStatus.COMPLETED)
        self.assertEqual(record.result.diff_summary, "")


if __name__ == "__main__":
    unittest.main()