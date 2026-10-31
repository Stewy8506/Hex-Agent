"""
Comprehensive MCP Client Protocol Compliance and Transport Test Suite.

Verifies:
1. Dynamic R0-R4 risk mapping and server overrides.
2. StdioMCPTransport with local mock subprocess server.
3. Handshake negotiation (initialize, notifications/initialized).
4. Dynamic tool discovery and schema extraction (tools/list).
5. Tool execution (tools/call) for success and error payloads.
6. Execution timeouts and graceful subprocess teardown with zero zombies.
7. HttpSSEMCPTransport request and notification roundtrips.
"""

from __future__ import annotations

import io
import json
import socket
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from harness_core.mcp_client import (
    HttpSSEMCPTransport,
    MCPClientAdapter,
    MCPToolDefinition,
    StdioMCPTransport,
    classify_mcp_tool_risk,
)
from harness_core.models import RiskClass


MOCK_SERVER_CODE = """
import sys
import json
import time

def main():
    initialized = False
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue

        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}

        if method == "initialize":
            initialized = True
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "mock-mcp-server", "version": "1.0.0"}
                }
            }
            sys.stdout.write(json.dumps(resp) + "\\n")
            sys.stdout.flush()
        elif method == "notifications/initialized":
            # Handshake notification, no response required
            pass
        elif method == "tools/list":
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "tools": [
                        {
                            "name": "read_weather",
                            "description": "Query weather report for location",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"city": {"type": "string"}},
                                "required": ["city"]
                            }
                        },
                        {
                            "name": "save_note",
                            "description": "Write note to workspace file",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"text": {"type": "string"}},
                                "required": ["text"]
                            }
                        },
                        {
                            "name": "exec_bash",
                            "description": "Execute bash command in subshell",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"cmd": {"type": "string"}},
                                "required": ["cmd"]
                            }
                        },
                        {
                            "name": "admin_wipe_system",
                            "description": "Sudo format and reset admin policy",
                            "inputSchema": {}
                        },
                        {
                            "name": "slow_delay",
                            "description": "Delays response for testing timeouts",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"delay": {"type": "number"}}
                            }
                        },
                        {
                            "name": "failing_tool",
                            "description": "Always returns execution failure",
                            "inputSchema": {}
                        }
                    ]
                }
            }
            sys.stdout.write(json.dumps(resp) + "\\n")
            sys.stdout.flush()
        elif method == "tools/call":
            tool_name = params.get("name")
            args = params.get("arguments") or {}
            if tool_name == "read_weather":
                city = args.get("city", "Unknown")
                res = {
                    "content": [{"type": "text", "text": f"Weather in {city}: 72°F, Sunny"}],
                    "isError": False
                }
                resp = {"jsonrpc": "2.0", "id": req_id, "result": res}
            elif tool_name == "save_note":
                note_text = args.get("text", "")
                res = {
                    "content": [{"type": "text", "text": f"Note saved: {note_text}"}],
                    "isError": False
                }
                resp = {"jsonrpc": "2.0", "id": req_id, "result": res}
            elif tool_name == "exec_bash":
                cmd = args.get("cmd", "")
                res = {
                    "content": [{"type": "text", "text": f"Executed: {cmd}"}],
                    "isError": False
                }
                resp = {"jsonrpc": "2.0", "id": req_id, "result": res}
            elif tool_name == "admin_wipe_system":
                res = {
                    "content": [{"type": "text", "text": "System wiped by admin."}],
                    "isError": False
                }
                resp = {"jsonrpc": "2.0", "id": req_id, "result": res}
            elif tool_name == "slow_delay":
                delay = float(args.get("delay", 1.0))
                time.sleep(delay)
                res = {
                    "content": [{"type": "text", "text": f"Completed delay of {delay}s"}],
                    "isError": False
                }
                resp = {"jsonrpc": "2.0", "id": req_id, "result": res}
            elif tool_name == "failing_tool":
                res = {
                    "content": [{"type": "text", "text": "Disk quota exceeded"}],
                    "isError": True
                }
                resp = {"jsonrpc": "2.0", "id": req_id, "result": res}
            else:
                resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Tool '{tool_name}' not found"}
                }
            sys.stdout.write(json.dumps(resp) + "\\n")
            sys.stdout.flush()
        else:
            if req_id is not None:
                resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Method '{method}' not found"}
                }
                sys.stdout.write(json.dumps(resp) + "\\n")
                sys.stdout.flush()

if __name__ == "__main__":
    main()
"""




class TestMCPClient(unittest.TestCase):
    """Test suite for MCP client adapters, transports, and risk categorization."""

    def test_dynamic_risk_classification(self) -> None:
        """Verify deterministic R0-R4 mapping for standard tool conventions."""
        # R1: Read-only inspection / queries
        self.assertEqual(
            classify_mcp_tool_risk("read_weather", "Query weather report for location"),
            RiskClass.R1,
        )
        self.assertEqual(
            classify_mcp_tool_risk("get_file_info", "returns file metadata"),
            RiskClass.R1,
        )
        self.assertEqual(
            classify_mcp_tool_risk("list_records", "lists all items"),
            RiskClass.R1,
        )
        self.assertEqual(
            classify_mcp_tool_risk("search_docs", "search documentation"),
            RiskClass.R1,
        )

        # R2: Workspace write / build / mutate / test
        self.assertEqual(
            classify_mcp_tool_risk("save_note", "Write note to workspace file"),
            RiskClass.R2,
        )
        self.assertEqual(
            classify_mcp_tool_risk("write_file", "creates a file on disk"),
            RiskClass.R2,
        )
        self.assertEqual(
            classify_mcp_tool_risk("delete_entry", "removes an entry"),
            RiskClass.R2,
        )
        self.assertEqual(
            classify_mcp_tool_risk("run_test_suite", "runs unit test harness"),
            RiskClass.R2,
        )

        # R3: Privileged / Execution / Shell / Deployment
        self.assertEqual(
            classify_mcp_tool_risk("exec_bash", "Execute bash command in subshell"),
            RiskClass.R3,
        )
        self.assertEqual(
            classify_mcp_tool_risk("deploy_service", "Deploy service to staging"),
            RiskClass.R3,
        )
        self.assertEqual(
            classify_mcp_tool_risk("publish_artifact", "Publish build package"),
            RiskClass.R3,
        )

        # R4: Root / Destructive / Policy / Secret
        self.assertEqual(
            classify_mcp_tool_risk("admin_wipe_system", "Sudo format and reset admin policy"),
            RiskClass.R4,
        )
        self.assertEqual(
            classify_mcp_tool_risk("get_secret_key", "fetch master credentials"),
            RiskClass.R4,
        )
        self.assertEqual(
            classify_mcp_tool_risk("revoke_access_token", "Revoke authentication tokens"),
            RiskClass.R4,
        )
        self.assertEqual(
            classify_mcp_tool_risk("sudo_elevate", "Root privilege escalation"),
            RiskClass.R4,
        )

        # Default fallback: Safe Default-Deny (R2, never R0/R1)
        self.assertEqual(
            classify_mcp_tool_risk("unrecognized_operation", "some custom action"),
            RiskClass.R2,
        )

    def test_risk_classification_overrides(self) -> None:
        """Verify per-server overrides take precedence over heuristics."""
        overrides = {
            "read_weather": RiskClass.R3,
            "exec_bash": RiskClass.R4,
            "custom_tool": RiskClass.R1,
        }
        self.assertEqual(
            classify_mcp_tool_risk("read_weather", "Query weather report", overrides=overrides),
            RiskClass.R3,
        )
        self.assertEqual(
            classify_mcp_tool_risk("exec_bash", "Execute command", overrides=overrides),
            RiskClass.R4,
        )
        self.assertEqual(
            classify_mcp_tool_risk("custom_tool", "Custom action", overrides=overrides),
            RiskClass.R1,
        )

    def test_stdio_handshake_and_tool_discovery(self) -> None:
        """Verify handshake initialization and tool listing via StdioMCPTransport."""
        transport = StdioMCPTransport(
            command=[sys.executable, "-c", MOCK_SERVER_CODE]
        )
        self.addCleanup(transport.close)

        adapter = MCPClientAdapter(server_id="mock-stdio-server", transport=transport)
        self.addCleanup(adapter.close)

        # 1. Initialize
        init_result = adapter.initialize(timeout=10.0)
        self.assertTrue(adapter.initialized)
        self.assertEqual(adapter.protocol_version, "2024-11-05")
        self.assertEqual(adapter.server_info.get("name"), "mock-mcp-server")
        self.assertEqual(adapter.server_info.get("version"), "1.0.0")

        # 2. Discover tools
        tools = adapter.discover_tools(timeout=10.0)
        self.assertEqual(len(tools), 6)
        tool_names = {t.name: t for t in tools}

        self.assertIn("read_weather", tool_names)
        self.assertEqual(tool_names["read_weather"].risk_class, RiskClass.R1)
        self.assertEqual(tool_names["read_weather"].server_id, "mock-stdio-server")
        self.assertIn("city", tool_names["read_weather"].input_schema.get("properties", {}))

        self.assertIn("save_note", tool_names)
        self.assertEqual(tool_names["save_note"].risk_class, RiskClass.R2)

        self.assertIn("exec_bash", tool_names)
        self.assertEqual(tool_names["exec_bash"].risk_class, RiskClass.R3)

        self.assertIn("admin_wipe_system", tool_names)
        self.assertEqual(tool_names["admin_wipe_system"].risk_class, RiskClass.R4)

        self.assertIn("slow_delay", tool_names)
        self.assertIn("failing_tool", tool_names)

        # 3. Manifest data verification
        manifest = adapter.get_manifest_data()
        self.assertEqual(manifest["server_id"], "mock-stdio-server")
        self.assertEqual(len(manifest["tools"]), 6)
        first_tool = manifest["tools"][0]
        self.assertIn("name", first_tool)
        self.assertIn("risk_class", first_tool)
        self.assertIn("input_schema", first_tool)

    def test_stdio_tool_execution(self) -> None:
        """Verify successful tool execution and error handling via tools/call."""
        transport = StdioMCPTransport(
            command=[sys.executable, "-c", MOCK_SERVER_CODE]
        )
        self.addCleanup(transport.close)

        adapter = MCPClientAdapter(server_id="mock-stdio-server", transport=transport)
        self.addCleanup(adapter.close)

        # Auto-initializes on first discovery or call
        adapter.discover_tools(timeout=10.0)

        # 1. Success call
        call_res = adapter.call_tool("read_weather", {"city": "San Francisco"}, timeout=10.0)
        self.assertTrue(call_res["ok"])
        self.assertIn("Weather in San Francisco: 72°F, Sunny", call_res["output"])
        self.assertIsInstance(call_res["content"], list)

        # 2. Execution failure call (isError: true)
        fail_res = adapter.call_tool("failing_tool", {}, timeout=10.0)
        self.assertFalse(fail_res["ok"])
        self.assertIn("Disk quota exceeded", fail_res["output"])

        # 3. Method not found / unknown tool error
        with self.assertRaises(ValueError):
            adapter.call_tool("non_existent_tool", {}, timeout=10.0)

    def test_stdio_timeout_and_graceful_teardown(self) -> None:
        """Verify request timeout raises TimeoutError and close terminates subprocess cleanly."""
        transport = StdioMCPTransport(
            command=[sys.executable, "-c", MOCK_SERVER_CODE]
        )
        self.addCleanup(transport.close)

        adapter = MCPClientAdapter(server_id="mock-stdio-server", transport=transport)
        self.addCleanup(adapter.close)

        adapter.initialize(timeout=10.0)

        # Call slow delay with 0.2s timeout when delay is 2.0s
        t0 = time.monotonic()
        with self.assertRaises(TimeoutError):
            adapter.call_tool("slow_delay", {"delay": 2.0}, timeout=0.2)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 1.5)

        # Graceful teardown
        proc = transport.process
        self.assertIsNone(proc.poll(), "Process should be running before close")

        adapter.close()
        # Process should be terminated
        self.assertIsNotNone(proc.poll(), "Process must be terminated after close")
        self.assertFalse(adapter.initialized)

        # Calls after close raise ConnectionError
        with self.assertRaises(ConnectionError):
            transport.send_request("initialize", {}, timeout=1.0)

    def test_http_sse_transport_end_to_end(self) -> None:
        """Verify HttpSSEMCPTransport handles JSON-RPC POST requests and errors hermetically."""
        class MockResponse:
            def __init__(self, data: bytes) -> None:
                self._data = data

            def read(self) -> bytes:
                return self._data

            def __enter__(self) -> MockResponse:
                return self

            def __exit__(self, *args: object) -> None:
                pass

        def fake_urlopen(req: urllib.request.Request, timeout: float = 30.0) -> MockResponse:
            body = req.data.decode("utf-8") if isinstance(req.data, bytes) else str(req.data)
            payload = json.loads(body)
            req_id = payload.get("id")
            method = payload.get("method")

            if req_id is None:
                # Notification
                return MockResponse(b"")

            if method == "initialize":
                resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "mock-http-server", "version": "1.0.0"},
                    },
                }
            elif method == "tools/list":
                resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"tools": []},
                }
            elif method == "unknown_method":
                resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": "Method not found"},
                }
            else:
                resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32600, "message": "Invalid request"},
                }

            return MockResponse(json.dumps(resp).encode("utf-8"))

        endpoint_url = "http://127.0.0.1:8080/mcp"
        transport = HttpSSEMCPTransport(endpoint_url=endpoint_url)
        self.addCleanup(transport.close)

        adapter = MCPClientAdapter(server_id="mock-http-server", transport=transport)
        self.addCleanup(adapter.close)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            # Initialize
            res = adapter.initialize(timeout=5.0)
            self.assertEqual(res.get("protocolVersion"), "2024-11-05")
            self.assertEqual(adapter.server_info.get("name"), "mock-http-server")

            # Discover tools
            tools = adapter.discover_tools(timeout=5.0)
            self.assertEqual(len(tools), 0)

            # Notification
            transport.send_notification("notifications/test", {"test": True})

            # Unknown method returns JSON-RPC error
            with self.assertRaises(ValueError):
                transport.send_request("unknown_method", {}, timeout=5.0)

        # Close transport
        adapter.close()
        with self.assertRaises(ConnectionError):
            transport.send_request("initialize", {}, timeout=1.0)


if __name__ == "__main__":
    unittest.main()
