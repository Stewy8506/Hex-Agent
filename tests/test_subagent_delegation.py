"""Comprehensive test suite for Tier C In-Flight Sub-Agent Delegation in HEX.

Covers:
- Role-constrained least-privilege tool filtering (RESEARCHER, CODER, TESTER, REVIEWER, PLANNER, GENERAL).
- Synchronous delegation execution with structured SubagentResult.
- Asynchronous delegation with subagent_status polling and subagent_wait blocking.
- Strict recursion depth cap enforcement (MaxRecursionDepthExceededError).
- Copy-on-Write workspace branch isolation (.hex/subagents/<id>).
- Active subagent registry inspection and interruption.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness_core.cow import get_default_snapshot_provider
from harness_core.db import LedgerStore
from harness_core.gateway import PolicyBlockedError, ToolGateway
from harness_core.models import RiskClass
from harness_core.subagent import (
    MaxRecursionDepthExceededError,
    ROLE_ALLOWED_TOOLS,
    SubagentManager,
    SubagentNotFoundError,
    SubagentRecord,
    SubagentResult,
    SubagentRole,
    SubagentStatus,
    SubagentTimeoutError,
    normalize_role,
)


class TestSubagentDelegation(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.workspace_dir = (Path(self.temp_dir) / "workspace").resolve()
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        (self.workspace_dir / "app.py").write_text("def main():\n    return 42\n", encoding="utf-8")

        self.db_path = Path(self.temp_dir) / "ledger.db"
        self.ledger = LedgerStore(self.db_path)
        self.cow = get_default_snapshot_provider(Path(self.temp_dir) / "cow_snapshots")
        self.gateway = ToolGateway(self.ledger)
        self.subagent_manager = SubagentManager(
            ledger=self.ledger,
            cow=self.cow,
            gateway=self.gateway,
            max_depth=2,
        )
        self.gateway.subagent_manager = self.subagent_manager

    def tearDown(self) -> None:
        self.ledger.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_role_allowed_tools_least_privilege(self) -> None:
        """Verify role-based tool restrictions enforce least privilege."""
        # Researcher has read-only tools
        researcher_tools = ROLE_ALLOWED_TOOLS[SubagentRole.RESEARCHER]
        self.assertIn("read_file", researcher_tools)
        self.assertIn("search_files", researcher_tools)
        self.assertIn("web_search", researcher_tools)
        self.assertNotIn("write_file", researcher_tools)
        self.assertNotIn("patch", researcher_tools)
        self.assertNotIn("terminal", researcher_tools)
        self.assertNotIn("delegate", researcher_tools)

        # Coder has implementation tools
        coder_tools = ROLE_ALLOWED_TOOLS[SubagentRole.CODER]
        self.assertIn("read_file", coder_tools)
        self.assertIn("write_file", coder_tools)
        self.assertIn("patch", coder_tools)
        self.assertIn("run_tests", coder_tools)
        self.assertNotIn("delegate", coder_tools)

        # Tester has verification tools
        tester_tools = ROLE_ALLOWED_TOOLS[SubagentRole.TESTER]
        self.assertIn("read_file", tester_tools)
        self.assertIn("run_tests", tester_tools)
        self.assertNotIn("write_file", tester_tools)
        self.assertNotIn("patch", tester_tools)

        # General has standard tools but no recursive delegate
        general_tools = ROLE_ALLOWED_TOOLS[SubagentRole.GENERAL]
        self.assertIn("read_file", general_tools)
        self.assertIn("write_file", general_tools)
        self.assertNotIn("delegate", general_tools)

    def test_synchronous_delegation_execution(self) -> None:
        """Verify synchronous spawn of a subagent returns SubagentResult."""
        res = self.subagent_manager.spawn_subagent(
            parent_task_id="tsk_parent_001",
            goal="Analyze codebase architecture",
            role=SubagentRole.RESEARCHER,
            parent_ws=self.workspace_dir,
            current_depth=0,
            wait=True,
        )
        self.assertIsInstance(res, SubagentResult)
        self.assertEqual(res.status, "completed")
        self.assertEqual(res.role, "researcher")
        self.assertEqual(res.parent_task_id, "tsk_parent_001")
        self.assertTrue(res.subagent_id.startswith("sub_"))
        self.assertIn("Subagent [researcher] processed goal", res.summary)

    def test_asynchronous_delegation_status_and_wait(self) -> None:
        """Verify asynchronous spawn, status polling, and wait completion."""
        subagent_id = self.subagent_manager.spawn_subagent(
            parent_task_id="tsk_parent_002",
            goal="Background indexation of documentation",
            role=SubagentRole.PLANNER,
            parent_ws=self.workspace_dir,
            current_depth=0,
            wait=False,
        )
        self.assertIsInstance(subagent_id, str)
        self.assertTrue(subagent_id.startswith("sub_"))

        # Query status
        status_info = self.subagent_manager.get_subagent_status(subagent_id)
        self.assertEqual(status_info["subagent_id"], subagent_id)
        self.assertEqual(status_info["role"], "planner")
        self.assertIn(status_info["status"], ("pending", "running", "completed"))

        # Wait for subagent completion
        result = self.subagent_manager.wait_subagent(subagent_id, timeout=10.0)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.subagent_id, subagent_id)

    def test_max_recursion_depth_enforcement(self) -> None:
        """Verify strict recursion depth limits (max_depth=2)."""
        # Depth 0 -> 1 is allowed
        res1 = self.subagent_manager.spawn_subagent(
            parent_task_id="tsk_depth_0",
            goal="Level 1 task",
            role=SubagentRole.GENERAL,
            parent_ws=self.workspace_dir,
            current_depth=0,
            wait=True,
        )
        self.assertEqual(res1.status, "completed")

        # Depth 1 -> 2 is allowed (since max_depth=2 allows depth < 2)
        res2 = self.subagent_manager.spawn_subagent(
            parent_task_id=res1.subagent_id,
            goal="Level 2 task",
            role=SubagentRole.GENERAL,
            parent_ws=self.workspace_dir,
            current_depth=1,
            wait=True,
        )
        self.assertEqual(res2.status, "completed")

        # Depth 2 -> 3 is rejected because depth 2 >= max_depth 2
        with self.assertRaises(MaxRecursionDepthExceededError):
            self.subagent_manager.spawn_subagent(
                parent_task_id=res2.subagent_id,
                goal="Level 3 task should fail",
                role=SubagentRole.GENERAL,
                parent_ws=self.workspace_dir,
                current_depth=2,
                wait=True,
            )

    def test_cow_workspace_branch_isolation(self) -> None:
        """Verify subagent executes in an isolated workspace and mutations do not leak to parent."""
        subagent_id = self.subagent_manager.spawn_subagent(
            parent_task_id="tsk_cow_test",
            goal="Modify code in child branch",
            role=SubagentRole.CODER,
            parent_ws=self.workspace_dir,
            current_depth=0,
            wait=False,
        )
        # Wait for workspace setup and finish
        result = self.subagent_manager.wait_subagent(subagent_id, timeout=10.0)
        self.assertEqual(result.status, "completed")

        record = self.subagent_manager._active_subagents[subagent_id]
        child_ws = record.workspace_path
        self.assertTrue(child_ws.exists())
        self.assertTrue(str(child_ws).startswith(str(self.workspace_dir)))

        # Simulate child modifying a file in its isolated branch
        child_app = child_ws / "app.py"
        self.assertTrue(child_app.exists())
        child_app.write_text("def main():\n    return 'mutated_by_child'\n", encoding="utf-8")

        # Verify parent file was NOT modified
        parent_app = self.workspace_dir / "app.py"
        self.assertEqual(parent_app.read_text(encoding="utf-8"), "def main():\n    return 42\n")

    def test_gateway_delegate_tool_invocation(self) -> None:
        """Verify calling delegate through ToolGateway._tool_delegate."""
        # 1. Sync delegate call
        res = self.gateway._tool_delegate(
            self.workspace_dir,
            {
                "goal": "Audit security vulnerabilities",
                "role": "reviewer",
                "wait": True,
            },
        )
        self.assertTrue(res["ok"])
        sub_res = res["result"]
        self.assertEqual(sub_res["role"], "reviewer")
        self.assertEqual(sub_res["status"], "completed")

        # 2. Async delegate call
        async_res = self.gateway._tool_delegate(
            self.workspace_dir,
            {
                "goal": "Long running task",
                "role": "tester",
                "wait": False,
            },
        )
        self.assertTrue(async_res["ok"])
        self.assertEqual(async_res["mode"], "async")
        sid = async_res["subagent_id"]

        # Status check via gateway tool
        stat_res = self.gateway._tool_subagent_status(self.workspace_dir, {"subagent_id": sid})
        self.assertTrue(stat_res["ok"])
        self.assertEqual(stat_res["subagent"]["subagent_id"], sid)

        # Wait via gateway tool
        wait_res = self.gateway._tool_subagent_wait(self.workspace_dir, {"subagent_id": sid, "timeout": 5.0})
        self.assertTrue(wait_res["ok"])
        self.assertEqual(wait_res["result"]["subagent_id"], sid)

    def test_interrupt_subagent(self) -> None:
        """Verify interrupting an active subagent halts execution cleanly."""
        subagent_id = self.subagent_manager.spawn_subagent(
            parent_task_id="tsk_interrupt",
            goal="Interrupted child",
            role=SubagentRole.GENERAL,
            parent_ws=self.workspace_dir,
            wait=False,
        )
        interrupted = self.subagent_manager.interrupt_subagent(subagent_id)
        # Subagent may have finished immediately or was marked interrupted
        status = self.subagent_manager.get_subagent_status(subagent_id)
        self.assertIn(status["status"], ("interrupted", "completed"))

    def test_list_active_subagents(self) -> None:
        """Verify listing registered subagents."""
        initial_count = len(self.subagent_manager.list_active_subagents())
        self.subagent_manager.spawn_subagent(
            parent_task_id="tsk_list_1",
            goal="Task A",
            role=SubagentRole.RESEARCHER,
            parent_ws=self.workspace_dir,
            wait=True,
        )
        self.subagent_manager.spawn_subagent(
            parent_task_id="tsk_list_2",
            goal="Task B",
            role=SubagentRole.CODER,
            parent_ws=self.workspace_dir,
            wait=True,
        )
        active = self.subagent_manager.list_active_subagents()
        self.assertEqual(len(active), initial_count + 2)
        goals = [a["goal"] for a in active]
        self.assertIn("Task A", goals)
        self.assertIn("Task B", goals)


if __name__ == "__main__":
    unittest.main()
