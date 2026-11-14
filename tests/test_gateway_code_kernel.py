"""Integration and unit tests for ToolGateway CodeKernel integration, ReActLoop, and TUI /kernel.

Covers:
- ToolGateway registration of code_kernel with RiskClass.R2 default-deny gating.
- State persistence across consecutive gateway tool executions within the same session.
- Namespace reset with reset=True clearing user variables.
- Secret redaction on kernel output streams (stdout, stderr, traceback).
- Stale module warning generation when an imported project file is modified on disk.
- Timeout handling and error reporting.
- Policy evaluation rejecting R2 code_kernel on fast_path tasks.
- ReActLoop multi-turn execution maintaining kernel state across turns.
- HexConsole TUI /kernel slash commands (status, reset, vars, exec).
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness_core.code_kernel import (
    CodeKernel,
    KernelRegistry,
    get_kernel_registry,
    reset_kernel_registry,
)
from harness_core.db import LedgerStore
from harness_core.gateway import (
    TOOL_DEFINITIONS,
    PolicyBlockedError,
    ToolGateway,
    redact_secrets,
)
from harness_core.models import PolicyDecisionType, RiskClass
from harness_core.orchestrator import HarnessOrchestrator
from harness_core.react_loop import ReActConfig, ReActLoop, ToolCallRequest
from harness_core.tui import HexConsole


class TestToolGatewayCodeKernel(unittest.TestCase):
    """Test suite for code_kernel tool registered in ToolGateway."""

    def setUp(self) -> None:
        reset_kernel_registry()
        self.temp_dir = tempfile.mkdtemp()
        self.workspace_dir = Path(self.temp_dir) / "workspace"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = Path(self.temp_dir) / "ledger.db"
        self.ledger = LedgerStore(self.db_path)
        self.registry = KernelRegistry(max_kernels=2, idle_timeout=60.0)
        self.gateway = ToolGateway(self.ledger, kernel_registry=self.registry)

    def tearDown(self) -> None:
        self.registry.shutdown()
        reset_kernel_registry()
        self.ledger.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_tool_registration_and_risk_class(self) -> None:
        """Verify code_kernel tool is registered in ToolGateway with RiskClass.R2."""
        self.assertIn("code_kernel", self.gateway.tools)
        spec = self.gateway.tools["code_kernel"]
        self.assertEqual(spec.risk_class, RiskClass.R2)
        self.assertIn("persistent interactive REPL", spec.description)

        # Check tool definitions list
        definitions = ToolGateway.list_tool_definitions()
        kernel_defs = [d for d in definitions if d.get("id") == "code_kernel"]
        self.assertEqual(len(kernel_defs), 1)
        k_def = kernel_defs[0]
        self.assertEqual(k_def.get("risk_class"), "R2")
        self.assertIn("code", k_def.get("parameters", {}).get("required", []))

    def test_state_persistence_across_calls(self) -> None:
        """Test persistent execution state across consecutive execute_tool calls."""
        res1 = self.gateway.execute_tool(
            "code_kernel",
            {"code": "session_val = 42\ndef calc(n): return session_val * n"},
            workspace_dir=self.workspace_dir,
        )
        self.assertTrue(res1["ok"])
        self.assertEqual(res1["status"], "success")
        self.assertEqual(res1["execution_count"], 1)

        res2 = self.gateway.execute_tool(
            "code_kernel",
            {"code": "print(f'RESULT={calc(3)}')"},
            workspace_dir=self.workspace_dir,
        )
        self.assertTrue(res2["ok"])
        self.assertEqual(res2["status"], "success")
        self.assertIn("RESULT=126", res2["output"])
        self.assertEqual(res2["execution_count"], 2)

    def test_reset_clears_variables(self) -> None:
        """Test reset=True clears previously defined variables and functions."""
        # Define variable
        res1 = self.gateway.execute_tool(
            "code_kernel",
            {"code": "ephemeral_var = 'stored_in_session'"},
            workspace_dir=self.workspace_dir,
        )
        self.assertTrue(res1["ok"])

        # Execute with reset=True
        res2 = self.gateway.execute_tool(
            "code_kernel",
            {"code": "print('reset_complete')", "reset": True},
            workspace_dir=self.workspace_dir,
        )
        self.assertTrue(res2["ok"])
        self.assertTrue(res2["state_reset"])
        self.assertIn("reset_complete", res2["output"])

        # Verify old variable is no longer accessible
        res3 = self.gateway.execute_tool(
            "code_kernel",
            {"code": "print(ephemeral_var)"},
            workspace_dir=self.workspace_dir,
        )
        self.assertFalse(res3["ok"])
        self.assertIn("NameError", res3["output"])

    def test_secret_redaction(self) -> None:
        """Test that secrets in kernel output streams are redacted before returning."""
        raw_token = "ghp_1234567890abcdef1234567890"
        raw_key = "sk-1234567890abcdef1234567890"

        code = f"""
print("TOKEN: {raw_token}")
print("KEY: {raw_key}")
"""
        res = self.gateway.execute_tool(
            "code_kernel",
            {"code": code},
            workspace_dir=self.workspace_dir,
        )
        self.assertTrue(res["ok"])
        # Verify raw credentials are not present
        self.assertNotIn(raw_token, res["output"])
        self.assertNotIn(raw_key, res["output"])
        self.assertNotIn(raw_token, res["stdout"])
        self.assertNotIn(raw_key, res["stdout"])
        # Verify redaction marker appears
        self.assertIn("[REDACTED_SECRET]", res["output"])

    def test_stale_module_warning(self) -> None:
        """Test that modifying an imported module generates a stale module warning."""
        module_file = self.workspace_dir / "my_math_mod.py"
        module_file.write_text("def add_three(x): return x + 3\n")

        # First execution: import module and call it
        code_import = (
            "import sys\n"
            "sys.path.insert(0, '.')\n"
            "import my_math_mod\n"
            "print(f'RES={my_math_mod.add_three(10)}')\n"
        )
        res1 = self.gateway.execute_tool(
            "code_kernel",
            {"code": code_import},
            workspace_dir=self.workspace_dir,
        )
        self.assertTrue(res1["ok"])
        self.assertIn("RES=13", res1["output"])

        # Modify module on disk with distinct mtime
        time.sleep(0.05)
        module_file.write_text("def add_three(x): return x + 300\n")
        new_mtime = time.time() + 10.0
        os.utime(module_file, (new_mtime, new_mtime))

        # Second execution: detect stale module
        res2 = self.gateway.execute_tool(
            "code_kernel",
            {"code": "print('Checking staleness')"},
            workspace_dir=self.workspace_dir,
        )
        self.assertTrue(res2["ok"])
        self.assertIn("warning", res2)
        self.assertIn("my_math_mod", res2["warning"])

    def test_empty_code_returns_error(self) -> None:
        """Test empty or whitespace-only code returns an error dictionary."""
        res = self.gateway.execute_tool(
            "code_kernel",
            {"code": "   \n  "},
            workspace_dir=self.workspace_dir,
        )
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "error")
        self.assertIn("code", res["error"])

    def test_timeout_handling(self) -> None:
        """Test execution timeout terminates the cell and returns status=timeout."""
        code = "import time\ntime.sleep(2.0)\nprint('Done')"
        res = self.gateway.execute_tool(
            "code_kernel",
            {"code": code, "timeout": 0.3},
            workspace_dir=self.workspace_dir,
        )
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "timeout")
        self.assertIn("timeout", res["output"].lower())

    def test_risk_policy_gating_r2(self) -> None:
        """Verify code_kernel is rejected on fast_path tasks and permitted on standard tasks."""
        spec = self.gateway.tools["code_kernel"]

        # Fast-path task permits only R0/R1 tools
        fast_path_task = {"task_id": "t_fp", "execution_mode": "fast_path"}
        decision, reason = self.gateway.evaluate_policy(fast_path_task, spec, {"code": "print(1)"})
        self.assertEqual(decision, PolicyDecisionType.DENY)
        self.assertIn("Fast-Path tasks only permit R0/R1", reason)

        # Standard task permits R2 tools
        std_task = {"task_id": "t_std", "execution_mode": "auto"}
        decision2, reason2 = self.gateway.evaluate_policy(std_task, spec, {"code": "print(1)"})
        self.assertEqual(decision2, PolicyDecisionType.ALLOW)

        # Constrained task with allowed_tools excluding code_kernel
        constrained_task = {"task_id": "t_c", "constraints": {"allowed_tools": ["read_file"]}}
        decision3, reason3 = self.gateway.evaluate_policy(constrained_task, spec, {"code": "print(1)"})
        self.assertEqual(decision3, PolicyDecisionType.DENY)
        self.assertIn("allowed_tools", reason3)


class TestReActLoopCodeKernelIntegration(unittest.TestCase):
    """Test suite for ReActLoop interaction with persistent code_kernel."""

    def setUp(self) -> None:
        reset_kernel_registry()
        self.temp_dir = tempfile.mkdtemp()
        self.workspace_dir = Path(self.temp_dir) / "workspace"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = Path(self.temp_dir) / "ledger.db"
        self.ledger = LedgerStore(self.db_path)
        self.registry = KernelRegistry(max_kernels=2, idle_timeout=60.0)
        self.gateway = ToolGateway(self.ledger, kernel_registry=self.registry)

    def tearDown(self) -> None:
        self.registry.shutdown()
        reset_kernel_registry()
        self.ledger.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_react_loop_multi_turn_state_retention(self) -> None:
        """Test ReActLoop executes a 2-turn workflow sharing persistent kernel variables."""
        call_count = 0

        def mock_model(messages: list[dict]) -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                tc = json.dumps({
                    "name": "code_kernel",
                    "arguments": {"code": "numbers = [10, 20, 30]\nprint('Initialized', len(numbers))"}
                })
                return f"Thought: Initializing data in kernel.\n<tool_call>\n{tc}\n</tool_call>"
            elif call_count == 2:
                tc = json.dumps({
                    "name": "code_kernel",
                    "arguments": {"code": "print('SUM=' + str(sum(numbers)))"}
                })
                return f"Thought: Computing sum using stored numbers.\n<tool_call>\n{tc}\n</tool_call>"
            else:
                return "Thought: Task complete.\n<finish>Sum computed as 60.</finish>"

        loop = ReActLoop(
            model_caller=mock_model,
            gateway=self.gateway,
            workspace_root=self.workspace_dir,
            config=ReActConfig(max_turns=5),
            session_id="react_test_session_42",
        )

        res = loop.run("Compute sum of numbers in code kernel")
        self.assertTrue(res.success)
        self.assertIn("60", res.final_answer)
        self.assertEqual(res.termination_reason, "finish")
        self.assertEqual(len(res.turns), 3)

        # Verify kernel tool calls in turns
        self.assertEqual(res.turns[0].tool_calls[0].name, "code_kernel")
        self.assertEqual(res.turns[1].tool_calls[0].name, "code_kernel")
        self.assertIn("SUM=60", str(res.turns[1].tool_results[0].output))

    def test_session_id_context_propagation(self) -> None:
        """Verify session_id is forwarded to kernel ownership so consecutive loops share kernel."""
        fixed_session_id = "shared_agent_session_100"

        loop1 = ReActLoop(
            gateway=self.gateway,
            workspace_root=self.workspace_dir,
            session_id=fixed_session_id,
        )
        tc1 = ToolCallRequest(
            call_id="c1",
            name="code_kernel",
            arguments={"code": "shared_constant = 9999"},
        )
        res1 = loop1._execute_tool_call(tc1)
        self.assertFalse(res1.is_error)

        # Separate loop instance with identical session_id
        loop2 = ReActLoop(
            gateway=self.gateway,
            workspace_root=self.workspace_dir,
            session_id=fixed_session_id,
        )
        tc2 = ToolCallRequest(
            call_id="c2",
            name="code_kernel",
            arguments={"code": "print('READ=' + str(shared_constant))"},
        )
        res2 = loop2._execute_tool_call(tc2)
        self.assertFalse(res2.is_error)
        self.assertIn("READ=9999", res2.output.get("output", ""))


class TestHexConsoleKernelSlashCommand(unittest.TestCase):
    """Test suite for HexConsole /kernel slash commands (status, reset, vars, exec)."""

    def setUp(self) -> None:
        reset_kernel_registry()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.orch = HarnessOrchestrator(":memory:", default_workspace=Path(self.temp_dir.name))
        self.console = HexConsole(
            self.orch,
            project_id="kernel_test_project",
            workspace_root=Path(self.temp_dir.name),
            stream=False,
            model_profile="hybrid",
        )

    def tearDown(self) -> None:
        if hasattr(self.orch, "gateway") and self.orch.gateway.kernel_registry:
            self.orch.gateway.kernel_registry.shutdown()
        reset_kernel_registry()
        self.temp_dir.cleanup()

    def test_slash_command_autocompletion_and_help(self) -> None:
        """Verify /kernel and /repl are present in autocompletion list and help docs."""
        self.assertIn("/kernel", HexConsole.SLASH_COMMANDS)
        self.assertIn("/repl", HexConsole.SLASH_COMMANDS)

        out = io.StringIO()
        with patch("sys.stdout", out):
            self.console.handle_slash_command("/help")
        text = out.getvalue()
        self.assertIn("/kernel", text)
        self.assertIn("status|reset|vars|exec", text)

    def test_kernel_status_empty_and_active(self) -> None:
        """Verify /kernel status displays empty notification and active table."""
        out = io.StringIO()
        with patch("sys.stdout", out):
            self.console.handle_slash_command("/kernel status")
        self.assertIn("No active code kernels currently tracked", out.getvalue())

        # Spawn kernel by executing code
        out_exec = io.StringIO()
        with patch("sys.stdout", out_exec):
            self.console.handle_slash_command("/kernel exec my_var = 12345; print(my_var)")
        self.assertIn("12345", out_exec.getvalue())

        # Now status should display active kernel table
        out_status = io.StringIO()
        with patch("sys.stdout", out_status):
            self.console.handle_slash_command("/kernel status")
        status_text = out_status.getvalue()
        self.assertIn("HEX REPL Code Kernels", status_text)
        self.assertIn("RUNNING", status_text)

    def test_kernel_vars_and_reset(self) -> None:
        """Verify /kernel vars lists variables and /kernel reset clears them."""
        # Define variable
        with patch("sys.stdout", io.StringIO()):
            self.console.handle_slash_command("/kernel exec user_data = 'alpha_beta'")

        # Query vars
        out_vars = io.StringIO()
        with patch("sys.stdout", out_vars):
            self.console.handle_slash_command("/kernel vars")
        self.assertIn("user_data", out_vars.getvalue())
        self.assertIn("alpha_beta", out_vars.getvalue())

        # Reset kernel
        out_reset = io.StringIO()
        with patch("sys.stdout", out_reset):
            self.console.handle_slash_command("/kernel reset")
        self.assertIn("Code kernel reset", out_reset.getvalue())

        # Query vars again -> should be empty
        out_vars2 = io.StringIO()
        with patch("sys.stdout", out_vars2):
            self.console.handle_slash_command("/kernel vars")
        self.assertIn("No user-defined variables", out_vars2.getvalue())

    def test_kernel_invalid_command_shows_usage(self) -> None:
        """Verify unknown /kernel subcommands display usage instructions."""
        out = io.StringIO()
        with patch("sys.stdout", out):
            self.console.handle_slash_command("/kernel unknown_action")
        self.assertIn("Usage: /kernel", out.getvalue())


if __name__ == "__main__":
    unittest.main()
