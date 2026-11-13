"""Unit tests for Continuous Multi-Turn ReAct Tool Loop (Phase 24-01)."""

import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock

from harness_core.db import LedgerStore
from harness_core.gateway import RiskClass, ToolGateway, ToolSpec
from harness_core.react_loop import (
    ReActConfig,
    ReActLoop,
    ReActSessionResult,
    ReActTurn,
    ToolCallRequest,
    ToolCallResult,
)


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


if __name__ == "__main__":
    unittest.main()
