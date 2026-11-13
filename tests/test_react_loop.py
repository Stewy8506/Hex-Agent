"""Unit tests for Continuous Multi-Turn ReAct Tool Loop (Phase 24-01 & 24-02)."""

import asyncio
from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest.mock import MagicMock, patch

from harness_core.cli import main as cli_main
from harness_core.db import LedgerStore
from harness_core.gateway import RiskClass, ToolGateway, ToolSpec
from harness_core.orchestrator import HarnessOrchestrator
from harness_core.react_loop import (
    ReActConfig,
    ReActLoop,
    ReActSessionResult,
    ReActTurn,
    ToolCallRequest,
    ToolCallResult,
)
from harness_core.tui import HexConsole


class TestReActDataStructures(unittest.TestCase):
    """Test data structures and default configurations."""

    def test_default_config(self) -> None:
        cfg = ReActConfig()
        self.assertEqual(cfg.max_turns, 30)
        self.assertEqual(cfg.temperature, 0.2)
        self.assertEqual(cfg.system_prompt, "")
        self.assertEqual(cfg.budget_tokens, 100_000)
        self.assertEqual(cfg.stop_sequences, [])

    def test_turn_and_session_result_defaults(self) -> None:
        req = ToolCallRequest(call_id="call_1", name="read_file", arguments={"path": "a.txt"})
        res = ToolCallResult(call_id="call_1", name="read_file", output={"content": "abc"})
        turn = ReActTurn(turn_index=1, thought="Reading file", tool_calls=[req], tool_results=[res], latency_ms=12.5)
        self.assertEqual(turn.turn_index, 1)
        self.assertEqual(len(turn.tool_calls), 1)
        self.assertEqual(len(turn.tool_results), 1)

        session = ReActSessionResult(
            success=True,
            final_answer="Done",
            total_turns=1,
            turns=[turn],
            termination_reason="finish",
        )
        self.assertTrue(session.success)
        self.assertEqual(session.final_answer, "Done")
        self.assertEqual(session.total_turns, 1)
        self.assertEqual(session.termination_reason, "finish")


class TestToolCallParsing(unittest.TestCase):
    """Test parsing of model responses across various formats."""

    def test_parse_json_dict_response(self) -> None:
        payload = {
            "thought": "I will read the configuration.",
            "tool_calls": [
                {"name": "read_file", "arguments": {"path": "config.yaml"}},
            ],
        }
        thought, calls = ReActLoop.parse_tool_calls(payload)
        self.assertEqual(thought, "I will read the configuration.")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "read_file")
        self.assertEqual(calls[0].arguments, {"path": "config.yaml"})

    def test_parse_single_tool_dict_response(self) -> None:
        payload = {
            "thought": "Running search",
            "tool": "search_files",
            "args": {"pattern": "def main"},
        }
        thought, calls = ReActLoop.parse_tool_calls(payload)
        self.assertEqual(thought, "Running search")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "search_files")
        self.assertEqual(calls[0].arguments, {"pattern": "def main"})

    def test_parse_xml_tool_call_block(self) -> None:
        text = """I will inspect the repository structure first.
<tool_call>
{
    "name": "list_files",
    "arguments": {"directory": "src"}
}
</tool_call>
Let's see what is there."""
        thought, calls = ReActLoop.parse_tool_calls(text)
        self.assertIn("inspect the repository structure", thought)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "list_files")
        self.assertEqual(calls[0].arguments, {"directory": "src"})

    def test_parse_xml_tool_call_with_tag_name(self) -> None:
        text = """<tool_call name="read_file">
{"path": "README.md"}
</tool_call>"""
        thought, calls = ReActLoop.parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "read_file")
        self.assertEqual(calls[0].arguments, {"path": "README.md"})

    def test_parse_markdown_json_code_fence(self) -> None:
        text = """Thinking about the next action...
```json
{
  "tool_calls": [
    {"name": "write_file", "arguments": {"path": "out.txt", "content": "alpha"}}
  ]
}
```
Waiting for execution."""
        thought, calls = ReActLoop.parse_tool_calls(text)
        self.assertIn("Thinking about the next action", thought)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "write_file")
        self.assertEqual(calls[0].arguments, {"path": "out.txt", "content": "alpha"})

    def test_parse_markdown_code_fence_single_tool(self) -> None:
        text = """```
{"tool": "read_file", "args": {"path": "foo.py"}}
```"""
        thought, calls = ReActLoop.parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "read_file")
        self.assertEqual(calls[0].arguments, {"path": "foo.py"})

    def test_parse_raw_json_string(self) -> None:
        text = '{"name": "read_file", "arguments": {"path": "test.txt"}}'
        thought, calls = ReActLoop.parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "read_file")
        self.assertEqual(calls[0].arguments, {"path": "test.txt"})

    def test_parse_embedded_json_in_text(self) -> None:
        text = 'Here is the action: {"tool": "read_file", "args": {"path": "sample.py"}}'
        thought, calls = ReActLoop.parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "read_file")
        self.assertEqual(calls[0].arguments, {"path": "sample.py"})

    def test_parse_no_tool_calls(self) -> None:
        text = "<thought>The solution is complete.</thought>\nHere is the answer."
        thought, calls = ReActLoop.parse_tool_calls(text)
        self.assertEqual(thought, "The solution is complete.")
        self.assertEqual(calls, [])


class TestMultiTurnReActLoop(unittest.TestCase):
    """Test full multi-turn cycle execution, observations, and termination."""

    def test_multi_turn_cycle_with_finish(self) -> None:
        """Test a 3-turn cycle: read file -> write file -> finish."""
        responses = [
            # Turn 1: Call read_file
            '<thought>Need to read input.txt</thought>\n<tool_call>\n{"name": "read_file", "arguments": {"path": "input.txt"}}\n</tool_call>',
            # Turn 2: Call write_file
            '<thought>Read successfully, now write output.txt</thought>\n<tool_call>\n{"name": "write_file", "arguments": {"path": "output.txt", "content": "HELLO"}}\n</tool_call>',
            # Turn 3: Complete task
            '<thought>Both operations succeeded.</thought>\n<finish>Successfully processed input.txt to output.txt</finish>',
        ]
        turn_count = 0

        def mock_model(messages: list[dict]) -> str:
            nonlocal turn_count
            res = responses[turn_count]
            turn_count += 1
            return res

        # Setup mock gateway
        tool_executed: list[str] = []

        def handle_read(ws: Path, args: dict) -> dict:
            tool_executed.append("read_file")
            return {"content": "hello world"}

        def handle_write(ws: Path, args: dict) -> dict:
            tool_executed.append("write_file")
            return {"status": "ok", "bytes": len(args.get("content", ""))}

        mock_gateway = MagicMock()
        mock_gateway.tools = {
            "read_file": ToolSpec(tool_id="read_file", description="", risk_class=RiskClass.R0, handler=handle_read),
            "write_file": ToolSpec(tool_id="write_file", description="", risk_class=RiskClass.R1, handler=handle_write),
        }

        loop = ReActLoop(
            model_caller=mock_model,
            gateway=mock_gateway,
            config=ReActConfig(max_turns=5),
        )

        session = loop.run("Process input.txt")
        self.assertTrue(session.success)
        self.assertEqual(session.termination_reason, "finish")
        self.assertEqual(session.final_answer, "Successfully processed input.txt to output.txt")
        self.assertEqual(session.total_turns, 3)
        self.assertEqual(tool_executed, ["read_file", "write_file"])

        # Verify messages history has observation blocks
        user_msgs = [m for m in loop.messages if m.get("role") == "user"]
        # 1 initial prompt + 2 observation messages
        self.assertEqual(len(user_msgs), 3)
        self.assertIn('<observation tool="read_file">', user_msgs[1]["content"])
        self.assertIn('<observation tool="write_file">', user_msgs[2]["content"])

    def test_natural_final_answer_termination(self) -> None:
        """Test termination when the model gives an answer without calling any tools."""
        def mock_model(messages: list[dict]) -> str:
            return "Paris is the capital of France."

        loop = ReActLoop(model_caller=mock_model)
        session = loop.run("What is the capital of France?")
        self.assertTrue(session.success)
        self.assertEqual(session.termination_reason, "no_tool_calls")
        self.assertEqual(session.final_answer, "Paris is the capital of France.")
        self.assertEqual(session.total_turns, 1)

    def test_max_turns_limit_reached(self) -> None:
        """Test loop cleanly stops and returns success=False when max turns exceeded."""
        def mock_model(messages: list[dict]) -> str:
            return '<tool_call>{"name": "noop", "arguments": {}}</tool_call>'

        mock_gateway = MagicMock()
        mock_gateway.tools = {
            "noop": ToolSpec(tool_id="noop", description="", risk_class=RiskClass.R0, handler=lambda ws, args: {"status": "ok"}),
        }

        loop = ReActLoop(
            model_caller=mock_model,
            gateway=mock_gateway,
            config=ReActConfig(max_turns=3),
        )
        session = loop.run("Loop forever")
        self.assertFalse(session.success)
        self.assertEqual(session.termination_reason, "max_turns")
        self.assertEqual(session.total_turns, 3)

    def test_tool_execution_error_recovery(self) -> None:
        """Test tool execution error is fed back to the model as an observation, allowing retry."""
        step = 0

        def mock_model(messages: list[dict]) -> str:
            nonlocal step
            if step == 0:
                step += 1
                return '<tool_call>{"name": "fail_tool", "arguments": {"invalid": true}}</tool_call>'
            else:
                # Check that previous message contains the error observation
                last_msg = messages[-1]["content"]
                assert "Error" in last_msg
                return "<finish>Recovered from error gracefully</finish>"

        def failing_handler(ws: Path, args: dict) -> dict:
            raise ValueError("Invalid argument supplied")

        mock_gateway = MagicMock()
        mock_gateway.tools = {
            "fail_tool": ToolSpec(tool_id="fail_tool", description="", risk_class=RiskClass.R1, handler=failing_handler),
        }

        loop = ReActLoop(
            model_caller=mock_model,
            gateway=mock_gateway,
            config=ReActConfig(max_turns=5),
        )
        session = loop.run("Execute with error")
        self.assertTrue(session.success)
        self.assertEqual(session.termination_reason, "finish")
        self.assertEqual(session.final_answer, "Recovered from error gracefully")
        self.assertEqual(session.total_turns, 2)
        self.assertTrue(session.turns[0].tool_results[0].is_error)

    def test_unknown_tool_error(self) -> None:
        """Test calling a tool not present in gateway returns an error observation."""
        step = 0

        def mock_model(messages: list[dict]) -> str:
            nonlocal step
            if step == 0:
                step += 1
                return '<tool_call>{"name": "non_existent_tool", "arguments": {}}</tool_call>'
            return "<finish>Handled missing tool</finish>"

        mock_gateway = MagicMock()
        mock_gateway.tools = {}

        loop = ReActLoop(
            model_caller=mock_model,
            gateway=mock_gateway,
            config=ReActConfig(max_turns=5),
        )
        session = loop.run("Missing tool test")
        self.assertTrue(session.success)
        self.assertEqual(session.final_answer, "Handled missing tool")
        self.assertTrue(session.turns[0].tool_results[0].is_error)

    def test_model_caller_exception(self) -> None:
        """Test unhandled exception in model caller terminates with error."""
        def broken_model(messages: list[dict]) -> str:
            raise RuntimeError("Provider connection lost")

        loop = ReActLoop(model_caller=broken_model)
        session = loop.run("Test crash")
        self.assertFalse(session.success)
        self.assertEqual(session.termination_reason, "error")
        self.assertIn("Provider connection lost", session.final_answer)


class TestLedgerIntegration(unittest.TestCase):
    """Test step and tool call recording into LedgerStore."""

    def test_ledger_step_recording(self) -> None:
        mock_ledger = MagicMock()

        def mock_model(messages: list[dict]) -> str:
            if len(messages) == 1:
                return '<tool_call>{"name": "test_tool", "arguments": {"k": "v"}}</tool_call>'
            return "<finish>Complete</finish>"

        mock_gateway = MagicMock()
        mock_gateway.tools = {
            "test_tool": ToolSpec(tool_id="test_tool", description="", risk_class=RiskClass.R0, handler=lambda ws, a: {"ok": True}),
        }

        loop = ReActLoop(
            model_caller=mock_model,
            gateway=mock_gateway,
            ledger=mock_ledger,
            config=ReActConfig(max_turns=3),
        )
        session = loop.run("Run with ledger", task_id="tsk_123")
        self.assertTrue(session.success)

        # Check ledger recording was called
        self.assertTrue(mock_ledger.record_tool_call.called or mock_ledger.record_step.called or mock_ledger.begin_tool_call.called)
        self.assertTrue(mock_ledger.seal_tool_call.called or mock_ledger.seal_step.called)

    def test_real_ledger_store_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            ledger = LedgerStore(db_path=db_path)
            try:
                task = ledger.submit_task({"goal": "ReAct Task"})
                task_id = task["task_id"]

                def mock_model(messages: list[dict]) -> str:
                    if len(messages) == 1:
                        return '<tool_call>{"name": "op", "arguments": {"x": 1}}</tool_call>'
                    return "<finish>Done</finish>"

                mock_gateway = MagicMock()
                mock_gateway.tools = {
                    "op": ToolSpec(tool_id="op", description="", risk_class=RiskClass.R0, handler=lambda ws, a: {"result": 42}),
                }

                loop = ReActLoop(
                    model_caller=mock_model,
                    gateway=mock_gateway,
                    ledger=ledger,
                    config=ReActConfig(max_turns=3),
                )
                session = loop.run("Task prompt", task_id=task_id)
                self.assertTrue(session.success)

                # Verify tool call was stored and sealed in SQLite DB
                calls = ledger.list_tool_calls(task_id)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0]["tool_id"], "op")
                self.assertEqual(calls[0]["status"], "SEALED")
            finally:
                ledger.close()


class TestAsyncAndRealGateway(unittest.TestCase):
    """Test async execution and real ToolGateway interaction."""

    def test_async_react_loop(self) -> None:
        def mock_model(messages: list[dict]) -> str:
            return "<finish>Async completed</finish>"

        loop = ReActLoop(model_caller=mock_model)
        session = asyncio.run(loop.run_async("Async test"))
        self.assertTrue(session.success)
        self.assertEqual(session.final_answer, "Async completed")

    def test_real_tool_gateway_file_read_and_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ws = Path(tmpdir)
            test_file = ws / "input.txt"
            test_file.write_text("Hello HEX ReAct")

            db_path = ws / "ledger.db"
            ledger = LedgerStore(db_path=db_path)
            try:
                gateway = ToolGateway(ledger=ledger)

                step = 0

                def mock_model(messages: list[dict]) -> str:
                    nonlocal step
                    if step == 0:
                        step += 1
                        return '<tool_call>{"name": "read_file", "arguments": {"path": "input.txt"}}</tool_call>'
                    elif step == 1:
                        step += 1
                        return '<tool_call>{"name": "list_files", "arguments": {}}</tool_call>'
                    else:
                        return "<finish>All files verified</finish>"

                loop = ReActLoop(
                    model_caller=mock_model,
                    gateway=gateway,
                    workspace_root=ws,
                    config=ReActConfig(max_turns=5),
                )

                session = loop.run("Check workspace files")
                self.assertTrue(session.success)
                self.assertEqual(session.total_turns, 3)
                self.assertEqual(session.final_answer, "All files verified")
                # Verify read_file output
                read_output = session.turns[0].tool_results[0].output
                self.assertIn("Hello HEX ReAct", str(read_output))
            finally:
                ledger.close()


class TestOrchestratorAgentLoop(unittest.TestCase):
    """Test orchestrator.run_agent_loop with ReActLoop and deterministic fallback."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name) / "workspace"
        self.ws.mkdir(parents=True, exist_ok=True)
        self.data_dir = Path(self.tmp.name) / "data"
        self.orch = HarnessOrchestrator(data_dir=self.data_dir, default_workspace=self.ws)

    def tearDown(self) -> None:
        if hasattr(self.orch, "ledger") and self.orch.ledger:
            self.orch.ledger.close()
        self.tmp.cleanup()

    def test_run_agent_loop_deterministic_conversational(self) -> None:
        """Verify conversational goal finishes cleanly via deterministic fallback."""
        res = self.orch.run_agent_loop("Hello, what is your mission?")
        self.assertTrue(res["success"])
        self.assertGreaterEqual(res["turns_count"], 1)
        self.assertIn("Hello", res["final_answer"])
        self.assertIn("turns", res)
        # Verify ledger has recorded the task as SUCCEEDED
        task_id = res["task_id"]
        t = self.orch.ledger.get_task(task_id)
        self.assertIsNotNone(t)
        self.assertEqual(t["status"], "SUCCEEDED")

    def test_run_agent_loop_deterministic_tool_execution(self) -> None:
        """Verify file reading tool is selected, executed, observed, and completed."""
        doc = self.ws / "notes.txt"
        doc.write_text("ReAct multi-turn loop verified.", encoding="utf-8")

        res = self.orch.run_agent_loop("Read file notes.txt")
        self.assertTrue(res["success"])
        self.assertGreaterEqual(res["turns_count"], 2)
        self.assertIn("notes.txt", res["final_answer"])
        # Verify tool was read_file
        first_turn = res["turns"][0]
        tool_calls = first_turn.tool_calls if hasattr(first_turn, "tool_calls") else first_turn["tool_calls"]
        self.assertEqual(len(tool_calls), 1)
        name = getattr(tool_calls[0], "name", None) or tool_calls[0]["name"]
        self.assertEqual(name, "read_file")

    def test_run_agent_loop_custom_workspace(self) -> None:
        """Verify custom workspace_root parameter routes tool execution to specified path."""
        custom_ws = Path(self.tmp.name) / "custom_project"
        custom_ws.mkdir(parents=True, exist_ok=True)
        custom_file = custom_ws / "custom.txt"
        custom_file.write_text("Data inside custom directory", encoding="utf-8")

        res = self.orch.run_agent_loop("Read file custom.txt", workspace_root=custom_ws)
        self.assertTrue(res["success"])
        self.assertIn("custom.txt", res["final_answer"])

    def test_run_agent_loop_with_active_router(self) -> None:
        """Verify model router generation is invoked when cloud provider is available."""
        step = 0
        def fake_route(*args: Any, **kwargs: Any) -> tuple[Any, Any]:
            nonlocal step
            dec = MagicMock()
            if step == 0:
                step += 1
                resp = MagicMock(text='<thought>Check dir</thought><tool_call>{"name": "list_files", "arguments": {}}</tool_call>')
            else:
                resp = MagicMock(text='<finish>Router finished successfully</finish>')
            return dec, resp

        mock_provider = MagicMock()
        mock_provider.is_available.return_value = True

        with patch.object(self.orch.router, "cloud_provider", mock_provider), \
             patch.object(self.orch.router, "execute_routed_request", side_effect=fake_route):
            res = self.orch.run_agent_loop("List workspace directory with router")
            self.assertTrue(res["success"])
            self.assertEqual(res["final_answer"], "Router finished successfully")
            self.assertEqual(res["turns_count"], 2)


class TestTUIReActIntegration(unittest.TestCase):
    """Test HexConsole /react slash command and prompt loop execution."""

    def setUp(self) -> None:
        self.mock_orch = MagicMock()
        self.mock_orch.default_workspace = Path("/tmp/dummy")

    def test_react_mode_default_and_toggle(self) -> None:
        console = HexConsole(self.mock_orch, quiet=True)
        self.assertFalse(console.react_mode)

        # Toggle on
        console.handle_slash_command("/react on")
        self.assertTrue(console.react_mode)

        # Toggle off
        console.handle_slash_command("/react off")
        self.assertFalse(console.react_mode)

        # Bare toggle: toggles boolean
        console.handle_slash_command("/react")
        self.assertTrue(console.react_mode)

        console.handle_slash_command("/react")
        self.assertFalse(console.react_mode)

    def test_react_mode_initialized_from_flag(self) -> None:
        console = HexConsole(self.mock_orch, quiet=True, react_mode=True)
        self.assertTrue(console.react_mode)

    def test_handle_user_prompt_react_mode_invokes_agent_loop(self) -> None:
        self.mock_orch.run_agent_loop.return_value = {
            "success": True,
            "final_answer": "Autonomous task executed with excellence",
            "turns_count": 2,
            "termination_reason": "finish",
            "turns": [
                ReActTurn(
                    turn_index=1,
                    thought="Checking files",
                    tool_calls=[ToolCallRequest(call_id="c1", name="list_files", arguments={})],
                    tool_results=[ToolCallResult(call_id="c1", name="list_files", output=["file1.txt"])],
                    latency_ms=10.0,
                ),
                ReActTurn(
                    turn_index=2,
                    thought="Task complete",
                    latency_ms=5.0,
                ),
            ],
            "task_id": "tsk_react_123",
        }

        console = HexConsole(self.mock_orch, quiet=True, react_mode=True)
        console.handle_user_prompt("Inspect the codebase and report")

        self.mock_orch.run_agent_loop.assert_called_once_with(
            goal="Inspect the codebase and report",
            workspace_root=console.workspace_root,
        )
        self.mock_orch.submit_and_run.assert_not_called()
        self.assertEqual(console.active_task_id, "tsk_react_123")


class TestCLIReActFlags(unittest.TestCase):
    """Test CLI argument parsing and dispatch with --react."""

    def test_cli_top_level_react_flag(self) -> None:
        with patch("harness_core.tui.HexConsole") as mock_console_cls, \
             patch("harness_core.cli.HarnessOrchestrator"):
            mock_inst = MagicMock()
            mock_inst.start.return_value = 0
            mock_console_cls.return_value = mock_inst

            code = cli_main(["--react", "--quiet", "--no-stream"])
            self.assertEqual(code, 0)
            mock_console_cls.assert_called_once()
            _, kwargs = mock_console_cls.call_args
            self.assertTrue(kwargs.get("react_mode"))

    def test_cli_chat_react_flag(self) -> None:
        with patch("harness_core.tui.HexConsole") as mock_console_cls, \
             patch("harness_core.cli.HarnessOrchestrator"):
            mock_inst = MagicMock()
            mock_inst.start.return_value = 0
            mock_console_cls.return_value = mock_inst

            code = cli_main(["chat", "--react", "--quiet", "--no-stream"])
            self.assertEqual(code, 0)
            mock_console_cls.assert_called_once()
            _, kwargs = mock_console_cls.call_args
            self.assertTrue(kwargs.get("react_mode"))

    def test_cli_run_react_execution(self) -> None:
        mock_orch = MagicMock()
        mock_orch.run_agent_loop.return_value = {
            "success": True,
            "final_answer": "CLI ReAct run complete",
            "turns_count": 1,
            "termination_reason": "finish",
            "turns": [],
        }

        with patch("harness_core.cli.HarnessOrchestrator", return_value=mock_orch):
            code = cli_main(["run", "--react", "Test autonomous goal"])
            self.assertEqual(code, 0)
            mock_orch.run_agent_loop.assert_called_once()
            call_kwargs = mock_orch.run_agent_loop.call_args[1]
            self.assertEqual(call_kwargs["goal"], "Test autonomous goal")


if __name__ == "__main__":
    unittest.main()
