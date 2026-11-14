"""Integration and unit tests for ToolGateway PTY Process Supervisor tools and ReActLoop.

Covers:
- start_process: Background execution, PTY allocation, secret redaction, path confinement
- read_terminal: Incremental output slicing, secret redaction, non-error status on failed processes
- kill_process: Clean signal termination and state transition to KILLED
- list_processes: Status filtering and sorting
- Gateway policy evaluation: R0 read-only fast-path vs R2 mutation gating
- ReActLoop multi-turn tool invocation and observation handling
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

from harness_core.db import LedgerStore
from harness_core.gateway import PolicyBlockedError, ToolGateway
from harness_core.models import RiskClass, TaskSubmitRequest
from harness_core.process_registry import ProcessRegistry, ProcessStatus, reset_process_registry
from harness_core.react_loop import ReActConfig, ReActLoop, ToolCallRequest


class TestGatewayProcessTools(unittest.TestCase):
    """Test suite for process management tools registered in ToolGateway."""

    def setUp(self) -> None:
        reset_process_registry()
        self.temp_dir = tempfile.mkdtemp()
        self.workspace_dir = Path(self.temp_dir) / "workspace"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = Path(self.temp_dir) / "ledger.db"
        self.ledger = LedgerStore(self.db_path)
        self.registry = ProcessRegistry()
        self.gateway = ToolGateway(self.ledger, process_registry=self.registry)

    def tearDown(self) -> None:
        self.registry.cleanup_all()
        self.registry.close()
        reset_process_registry()
        self.ledger.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_tool_registration_and_risk_classes(self) -> None:
        """Verify the 4 process tools are registered with correct risk tiers."""
        self.assertIn("start_process", self.gateway.tools)
        self.assertIn("read_terminal", self.gateway.tools)
        self.assertIn("kill_process", self.gateway.tools)
        self.assertIn("list_processes", self.gateway.tools)

        self.assertEqual(self.gateway.tools["start_process"].risk_class, RiskClass.R2)
        self.assertEqual(self.gateway.tools["read_terminal"].risk_class, RiskClass.R0)
        self.assertEqual(self.gateway.tools["kill_process"].risk_class, RiskClass.R2)
        self.assertEqual(self.gateway.tools["list_processes"].risk_class, RiskClass.R0)

    def test_start_process_and_read_terminal_incremental(self) -> None:
        """Test start_process spawns background job and read_terminal streams slices."""
        cmd = f'{sys.executable} -u -c "import time; print(\'HELLO_PROCESS\', flush=True); time.sleep(0.1); print(\'LINE_TWO\', flush=True)"'
        start_res = self.gateway._tool_start_process(
            self.workspace_dir,
            {"command": cmd, "pty": True},
        )
        self.assertTrue(start_res["ok"])
        proc_id = start_res["process_id"]
        self.assertIsNotNone(proc_id)
        self.assertIn(start_res["status"], ("running", "completed"))

        # Wait briefly for output
        time.sleep(0.3)

        # First read slice with limit=10
        read_res1 = self.gateway._tool_read_terminal(
            self.workspace_dir,
            {"process_id": proc_id, "offset": 0, "limit": 10},
        )
        self.assertTrue(read_res1["ok"])
        self.assertIn("output", read_res1)
        first_chunk = read_res1["output"]
        self.assertGreater(len(first_chunk), 0)

        # Second read slice from end of first
        read_res2 = self.gateway._tool_read_terminal(
            self.workspace_dir,
            {"process_id": proc_id, "offset": len(first_chunk), "limit": 2000},
        )
        self.assertTrue(read_res2["ok"])
        full_read = first_chunk + read_res2["output"]
        self.assertIn("HELLO_PROCESS", full_read)

    def test_secret_scrubbing_in_process_tools(self) -> None:
        """Test secrets are scrubbed from commands and output in process tools."""
        secret_token = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"
        cmd = f'{sys.executable} -c "print(\'token: {secret_token}\')"'

        start_res = self.gateway._tool_start_process(
            self.workspace_dir,
            {"command": cmd},
        )
        self.assertTrue(start_res["ok"])
        proc_id = start_res["process_id"]
        self.assertNotIn(secret_token, start_res["command"])
        self.assertIn("[REDACTED_SECRET]", start_res["command"])

        time.sleep(0.3)

        read_res = self.gateway._tool_read_terminal(
            self.workspace_dir,
            {"process_id": proc_id, "offset": 0, "limit": 2000},
        )
        self.assertTrue(read_res["ok"])
        self.assertNotIn(secret_token, read_res["output"])
        self.assertIn("[REDACTED_SECRET]", read_res["output"])

    def test_path_confinement_in_start_process(self) -> None:
        """Test cwd parameter is strictly confined to workspace boundary."""
        with self.assertRaises(PolicyBlockedError):
            self.gateway._tool_start_process(
                self.workspace_dir,
                {"command": "echo test", "cwd": "../../../etc"},
            )

    def test_kill_process_clean_termination(self) -> None:
        """Test kill_process cleanly terminates an active long-running process."""
        cmd = f'{sys.executable} -c "import time; time.sleep(60)"'
        start_res = self.gateway._tool_start_process(
            self.workspace_dir,
            {"command": cmd, "pty": False},
        )
        self.assertTrue(start_res["ok"])
        proc_id = start_res["process_id"]

        # Verify running
        procs_running = self.gateway._tool_list_processes(self.workspace_dir, {"status": "running"})
        self.assertTrue(any(p["id"] == proc_id for p in procs_running["processes"]))

        # Kill process
        kill_res = self.gateway._tool_kill_process(
            self.workspace_dir,
            {"process_id": proc_id, "force": False},
        )
        self.assertTrue(kill_res["ok"])
        self.assertEqual(kill_res["status"], ProcessStatus.KILLED.value)

        # Verify list shows killed
        procs_killed = self.gateway._tool_list_processes(self.workspace_dir, {"status": "killed"})
        self.assertTrue(any(p["id"] == proc_id for p in procs_killed["processes"]))

    def test_policy_risk_gating_fast_path(self) -> None:
        """Test fast_path tasks allow R0 process tools but block R2 mutations."""
        # Create fast_path task in ledger
        task_id = self.ledger.submit_task(
            TaskSubmitRequest(
                goal="Inspect logs in fast path",
                workspace_root=str(self.workspace_dir),
                execution_mode="fast_path",
            )
        )["task_id"]

        task = self.ledger.get_task(task_id)

        # R0 list_processes should be allowed
        decision, _ = self.gateway.evaluate_policy(
            task,
            self.gateway.tools["list_processes"],
            {"status": "all"},
        )
        self.assertEqual(decision.value, "ALLOW")

        # R0 read_terminal should be allowed
        decision, _ = self.gateway.evaluate_policy(
            task,
            self.gateway.tools["read_terminal"],
            {"process_id": "proc_dummy"},
        )
        self.assertEqual(decision.value, "ALLOW")

        # R2 start_process must be denied in fast_path
        decision, reason = self.gateway.evaluate_policy(
            task,
            self.gateway.tools["start_process"],
            {"command": "echo 1"},
        )
        self.assertEqual(decision.value, "DENY")
        self.assertIn("Fast-Path tasks only permit R0/R1 tools", reason)

        # R2 kill_process must be denied in fast_path
        decision, reason = self.gateway.evaluate_policy(
            task,
            self.gateway.tools["kill_process"],
            {"process_id": "proc_dummy"},
        )
        self.assertEqual(decision.value, "DENY")

    def test_backward_compatible_process_tool(self) -> None:
        """Test existing 'process' tool delegates to process_registry."""
        cmd = f'{sys.executable} -c "import time; time.sleep(10)"'
        start_res = self.gateway._tool_start_process(
            self.workspace_dir,
            {"command": cmd},
        )
        self.assertTrue(start_res["ok"])
        proc_id = start_res["process_id"]

        # Use legacy 'process' tool to list
        legacy_list = self.gateway._tool_process(self.workspace_dir, {"action": "list"})
        self.assertTrue(legacy_list["ok"])
        self.assertTrue(any(p.get("id") == proc_id for p in legacy_list["processes"]))

        # Use legacy 'process' tool to kill via process_id
        legacy_kill = self.gateway._tool_process(self.workspace_dir, {"action": "kill", "process_id": proc_id})
        self.assertTrue(legacy_kill["ok"])

    def test_react_loop_process_tools_execution(self) -> None:
        """Test ReActLoop drives start_process, read_terminal, and kill_process."""
        loop = ReActLoop(
            gateway=self.gateway,
            workspace_root=self.workspace_dir,
            config=ReActConfig(max_turns=5),
        )

        cmd = f'{sys.executable} -u -c "import time; print(\'REACT_BG_OUTPUT\', flush=True); time.sleep(5)"'
        start_call = ToolCallRequest(
            call_id="call_start_01",
            name="start_process",
            arguments={"command": cmd, "pty": False},
        )
        start_result = loop._execute_tool_call(start_call)
        self.assertFalse(start_result.is_error)
        self.assertIn("process_id", start_result.output)
        proc_id = start_result.output["process_id"]

        time.sleep(0.3)

        read_call = ToolCallRequest(
            call_id="call_read_02",
            name="read_terminal",
            arguments={"process_id": proc_id, "offset": 0, "limit": 2000},
        )
        read_result = loop._execute_tool_call(read_call)
        self.assertFalse(read_result.is_error)
        self.assertIn("REACT_BG_OUTPUT", read_result.output["output"])

        kill_call = ToolCallRequest(
            call_id="call_kill_03",
            name="kill_process",
            arguments={"process_id": proc_id, "force": True},
        )
        kill_result = loop._execute_tool_call(kill_call)
        self.assertFalse(kill_result.is_error)
        self.assertEqual(kill_result.output["status"], ProcessStatus.KILLED.value)


if __name__ == "__main__":
    unittest.main()
