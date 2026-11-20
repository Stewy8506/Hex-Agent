"""Comprehensive test suite for Agent Client Protocol (ACP) Server in HEX.

Tests:
- ACP JSON-RPC 2.0 handshake (initialize).
- Session lifecycle (session/new, session/load, session/list, session/cancel).
- Session prompt execution with orchestrator / fast-path task runner.
- Tool discovery and invocation through ACP (tools/list, tools/call).
- CLI commands 'hex acp --check' and 'hex acp --setup'.
"""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness_core.client.acp import ACPServer, ACPSession
from harness_core.client.cli import main as cli_main
from harness_core.storage.db import LedgerStore
from harness_core.core.orchestrator import HarnessOrchestrator


class TestACPServer(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.workspace_dir = Path(self.temp_dir) / "workspace"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        (self.workspace_dir / "index.py").write_text("print('hello acp')\n", encoding="utf-8")

        self.data_dir = Path(self.temp_dir) / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.orchestrator = HarnessOrchestrator(
            data_dir=self.data_dir,
            default_workspace=self.workspace_dir,
        )
        self.server = ACPServer(
            orchestrator=self.orchestrator,
            workspace_root=self.workspace_dir,
        )

    def tearDown(self) -> None:
        self.orchestrator.ledger.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_acp_initialize(self) -> None:
        """Verify ACP initialize handshake conforms to specification."""
        req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        }
        resp = self.server.handle_request(req)
        self.assertEqual(resp["jsonrpc"], "2.0")
        self.assertEqual(resp["id"], 1)
        self.assertIn("result", resp)
        res = resp["result"]
        self.assertEqual(res["serverInfo"]["name"], "HEX")
        from harness_core import __version__

        self.assertEqual(res["serverInfo"]["version"], __version__)
        self.assertTrue(res["capabilities"]["session"]["streaming"])
        self.assertTrue(res["capabilities"]["session"]["cancel"])

    def test_acp_initialize_labels_private_dialect(self) -> None:
        """3.7: initialize honestly labels the private dialect (no certified
        standard-ACP claim) while staying wire-compatible with the HEX client."""
        resp = self.server.handle_request({
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {},
        })
        res = resp["result"]
        self.assertEqual(res["protocolDialect"], "hex-private-acp/1")
        self.assertEqual(res["protocolVersion"], "2024-11-05")
        self.assertEqual(res["serverInfo"]["name"], "HEX")
        desc = res["serverInfo"]["description"].lower()
        self.assertIn("private", desc)
        self.assertIn("not a certified", desc)

    def test_acp_unknown_method_error_is_actionable(self) -> None:
        """3.7: an unknown method lists supported methods so a client can
        recover, and identifies the private dialect."""
        resp = self.server.handle_request({
            "jsonrpc": "2.0", "id": 9, "method": "session/bogus", "params": {},
        })
        self.assertEqual(resp["error"]["code"], -32601)
        msg = resp["error"]["message"]
        self.assertIn("session/bogus", msg)
        self.assertIn("Supported methods:", msg)
        self.assertIn("initialize", msg)
        self.assertIn("session/prompt", msg)
        self.assertIn("hex-private-acp/1", msg)

    def test_acp_session_new_load_and_list(self) -> None:
        """Verify creating, inspecting, loading, and listing ACP sessions."""
        # 1. session/new
        new_req = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/new",
            "params": {"cwd": str(self.workspace_dir), "model": "gemini-2.5-flash"},
        }
        new_resp = self.server.handle_request(new_req)
        self.assertIn("result", new_resp)
        sid = new_resp["result"]["sessionId"]
        self.assertTrue(sid.startswith("acp_sess_"))

        # 2. session/load
        load_req = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "session/load",
            "params": {"sessionId": sid},
        }
        load_resp = self.server.handle_request(load_req)
        self.assertEqual(load_resp["result"]["sessionId"], sid)
        self.assertEqual(load_resp["result"]["model"], "gemini-2.5-flash")

        # 3. session/list
        list_req = {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "session/list",
            "params": {},
        }
        list_resp = self.server.handle_request(list_req)
        sids = [s["sessionId"] for s in list_resp["result"]["sessions"]]
        self.assertIn(sid, sids)

    def test_acp_tools_list_and_call(self) -> None:
        """Verify tools/list returns all registered tools and tools/call executes tools."""
        # 1. tools/list
        list_req = {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/list",
            "params": {},
        }
        list_resp = self.server.handle_request(list_req)
        tools = list_resp["result"]["tools"]
        tool_ids = [t["id"] for t in tools]
        self.assertIn("read_file", tool_ids)
        self.assertIn("delegate", tool_ids)
        self.assertIn("browser", tool_ids)
        self.assertIn("image_inspect", tool_ids)
        self.assertIn("start_process", tool_ids)
        self.assertIn("read_terminal", tool_ids)
        self.assertIn("kill_process", tool_ids)
        self.assertIn("list_processes", tool_ids)
        self.assertIn("code_kernel", tool_ids)
        self.assertEqual(len(tools), 31)

        # 2. tools/call read_file
        call_req = {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "read_file",
                "arguments": {"path": "index.py"},
            },
        }
        call_resp = self.server.handle_request(call_req)
        self.assertIn("result", call_resp)
        content_items = call_resp["result"]["content"]
        self.assertTrue(len(content_items) > 0)
        self.assertIn("hello acp", content_items[0]["text"])

    def test_acp_discovery_and_invocation_of_dynamic_mcp_tool(self) -> None:
        """B57: a dynamically registered MCP tool is advertised by tools/list and
        invocable through tools/call (the static catalog could not advertise it)."""
        from harness_core.mcp_client import MCPClientAdapter

        class _FakeTransport:
            def __init__(self):
                self.calls = []

            def send_request(self, method, params=None, timeout=30.0):
                params = params or {}
                if method == "initialize":
                    return {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                            "serverInfo": {"name": "fake", "version": "1.0"}}
                if method == "tools/list":
                    return {"tools": [{
                        "name": "lookup_docs",
                        "description": "Query architectural documentation",
                        "inputSchema": {"type": "object",
                                        "properties": {"query": {"type": "string"}},
                                        "required": ["query"]},
                    }]}
                if method == "tools/call":
                    self.calls.append((params.get("name"), params.get("arguments")))
                    return {"content": [{"type": "text", "text": "docs-result"}], "isError": False}
                raise ValueError(method)

            def send_notification(self, method, params=None):
                pass

            def close(self):
                pass

        transport = _FakeTransport()
        adapter = MCPClientAdapter("fake_docs", transport)
        self.orchestrator.gateway.register_mcp_server(adapter, enforce_scan=False)

        # Discovery advertises the dynamic tool (namespaced + alias) and built-ins.
        list_resp = self.server.handle_request(
            {"jsonrpc": "2.0", "id": 30, "method": "tools/list", "params": {}}
        )
        tools = list_resp["result"]["tools"]
        ids = [t["id"] for t in tools]
        self.assertIn("fake_docs__lookup_docs", ids)
        self.assertIn("lookup_docs", ids)
        self.assertIn("read_file", ids)
        dyn = next(t for t in tools if t["id"] == "fake_docs__lookup_docs")
        self.assertEqual(dyn["parameters"]["required"], ["query"])

        # Invocation reaches the MCP server through the mandatory gateway boundary.
        call_resp = self.server.handle_request({
            "jsonrpc": "2.0", "id": 31, "method": "tools/call",
            "params": {"name": "fake_docs__lookup_docs", "arguments": {"query": "cow"}},
        })
        self.assertIn("result", call_resp)
        self.assertNotIn("isError", call_resp["result"])
        self.assertEqual(transport.calls[-1], ("lookup_docs", {"query": "cow"}))
        self.assertIn("docs-result", call_resp["result"]["content"][0]["text"])

    def test_acp_session_prompt_execution(self) -> None:
        """Verify session/prompt executes task through orchestrator and returns text."""
        new_resp = self.server.handle_request({
            "jsonrpc": "2.0",
            "id": 10,
            "method": "session/new",
            "params": {"cwd": str(self.workspace_dir)},
        })
        sid = new_resp["result"]["sessionId"]

        prompt_req = {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "session/prompt",
            "params": {
                "sessionId": sid,
                "prompt": "Inspect workspace index.py",
            },
        }
        prompt_resp = self.server.handle_request(prompt_req)
        self.assertIn("result", prompt_resp)
        self.assertEqual(prompt_resp["result"]["sessionId"], sid)
        self.assertIn(prompt_resp["result"]["status"], ("completed", "ready", "running"))
        content = prompt_resp["result"]["content"]
        self.assertTrue(len(content) > 0)
        self.assertEqual(content[0]["type"], "text")

    def test_acp_session_cancel(self) -> None:
        """Verify session/cancel marks session cancelled."""
        new_resp = self.server.handle_request({
            "jsonrpc": "2.0",
            "id": 20,
            "method": "session/new",
            "params": {"cwd": str(self.workspace_dir)},
        })
        sid = new_resp["result"]["sessionId"]

        cancel_resp = self.server.handle_request({
            "jsonrpc": "2.0",
            "id": 21,
            "method": "session/cancel",
            "params": {"sessionId": sid},
        })
        self.assertTrue(cancel_resp["result"]["cancelled"])

    def test_acp_unknown_method_error(self) -> None:
        """Verify unknown method returns -32601 JSON-RPC error."""
        resp = self.server.handle_request({
            "jsonrpc": "2.0",
            "id": 99,
            "method": "nonexistent/method",
            "params": {},
        })
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32601)

    def test_acp_approval_decision(self) -> None:
        """Verify operator approval decisions can be recorded via session/approvalDecision."""
        resp = self.server.handle_request({
            "jsonrpc": "2.0",
            "id": 40,
            "method": "session/approvalDecision",
            "params": {
                "approvalId": "appr_test_123",
                "decision": "approve",
                "reason": "Operator verified safe command",
            },
        })
        self.assertIn("result", resp)
        self.assertEqual(resp["result"]["approvalId"], "appr_test_123")
        self.assertEqual(resp["result"]["decision"], "approve")
        self.assertEqual(resp["result"]["status"], "acknowledged")
        self.assertIn("appr_test_123", self.server.pending_approvals)
        self.assertEqual(self.server.pending_approvals["appr_test_123"]["decision"], "approve")

    def test_acp_session_command_forwarding(self) -> None:
        """Verify session/command executes slash commands and returns structured output."""
        # /help
        resp_help = self.server.handle_request({
            "jsonrpc": "2.0",
            "id": 50,
            "method": "session/command",
            "params": {"sessionId": "test_s1", "command": "/help"},
        })
        self.assertIn("result", resp_help)
        self.assertIn("/status", resp_help["result"]["output"])
        self.assertFalse(resp_help["result"]["isError"])

        # /doctor
        resp_doc = self.server.handle_request({
            "jsonrpc": "2.0",
            "id": 51,
            "method": "session/command",
            "params": {"sessionId": "test_s1", "command": "/doctor"},
        })
        self.assertIn("result", resp_doc)
        self.assertIn("HEX SYSTEM DIAGNOSTIC & HEALTH REPORT", resp_doc["result"]["output"])
        self.assertIn("Overall Status:", resp_doc["result"]["output"])

        # /model
        resp_mod = self.server.handle_request({
            "jsonrpc": "2.0",
            "id": 52,
            "method": "session/command",
            "params": {"sessionId": "test_s1", "command": "/model gemini-pro-test"},
        })
        self.assertIn("result", resp_mod)
        self.assertIn("gemini-pro-test", resp_mod["result"]["output"])

    def test_acp_cli_check_and_setup(self) -> None:
        """Verify CLI 'hex acp --check' and 'hex acp --setup' exit cleanly."""
        out_check = io.StringIO()
        with patch("sys.stdout", out_check):
            code_check = cli_main(["acp", "--check", "--workspace", str(self.workspace_dir)])
            self.assertEqual(code_check, 0)
            self.assertIn("HEX ACP Health Check", out_check.getvalue())
            self.assertIn("JSON-RPC 2.0 stdio", out_check.getvalue())

        out_setup = io.StringIO()
        with patch("sys.stdout", out_setup):
            code_setup = cli_main(["acp", "--setup"])
            self.assertEqual(code_setup, 0)
            self.assertIn("HEX Editor Integration Snippets", out_setup.getvalue())
            self.assertIn(".zed/settings.json", out_setup.getvalue())

    def test_acp_conversational_turn_runs_zero_tools(self) -> None:
        """Verify conversational turns like 'Hi what can you do?' execute 0 tools."""
        new_resp = self.server.handle_request({
            "jsonrpc": "2.0",
            "id": 60,
            "method": "session/new",
            "params": {"cwd": str(self.workspace_dir)},
        })
        sid = new_resp["result"]["sessionId"]

        tool_calls_fired = []
        with patch.object(self.orchestrator.gateway, "invoke", side_effect=tool_calls_fired.append):
            prompt_resp = self.server.handle_request({
                "jsonrpc": "2.0",
                "id": 61,
                "method": "session/prompt",
                "params": {
                    "sessionId": sid,
                    "prompt": "Hi what can you do?",
                },
            })

        self.assertEqual(len(tool_calls_fired), 0, "Conversational questions must not trigger tool calls")
        self.assertIn("result", prompt_resp)
        content = prompt_resp["result"]["content"]
        self.assertTrue(len(content) > 0)
        self.assertEqual(content[0]["type"], "text")

    def test_acp_commands_query_runs_zero_tools(self) -> None:
        """Verify 'What are the commands i can give you?' executes 0 tools."""
        new_resp = self.server.handle_request({
            "jsonrpc": "2.0",
            "id": 62,
            "method": "session/new",
            "params": {"cwd": str(self.workspace_dir)},
        })
        sid = new_resp["result"]["sessionId"]

        tool_calls_fired = []
        with patch.object(self.orchestrator.gateway, "invoke", side_effect=tool_calls_fired.append):
            prompt_resp = self.server.handle_request({
                "jsonrpc": "2.0",
                "id": 63,
                "method": "session/prompt",
                "params": {
                    "sessionId": sid,
                    "prompt": "What are the commands i can give you?",
                },
            })

        self.assertEqual(len(tool_calls_fired), 0, "Commands questions must not trigger tool calls")
        self.assertIn("result", prompt_resp)
        content = prompt_resp["result"]["content"]
        self.assertTrue(len(content) > 0)

    def test_acp_conceptual_question_runs_zero_tools(self) -> None:
        """Verify non-workspace conceptual questions do not trigger list_files/query_graphify."""
        new_resp = self.server.handle_request({
            "jsonrpc": "2.0",
            "id": 64,
            "method": "session/new",
            "params": {"cwd": str(self.workspace_dir)},
        })
        sid = new_resp["result"]["sessionId"]

        tool_calls_fired = []
        with patch.object(self.orchestrator.gateway, "invoke", side_effect=tool_calls_fired.append):
            prompt_resp = self.server.handle_request({
                "jsonrpc": "2.0",
                "id": 65,
                "method": "session/prompt",
                "params": {
                    "sessionId": sid,
                    "prompt": "Explain how KV-cache prefix freezing works.",
                },
            })

        self.assertEqual(len(tool_calls_fired), 0, "Conceptual questions must not trigger workspace scan tools")
        self.assertIn("result", prompt_resp)
        content = prompt_resp["result"]["content"]
        self.assertTrue(len(content) > 0)


if __name__ == "__main__":
    unittest.main()
