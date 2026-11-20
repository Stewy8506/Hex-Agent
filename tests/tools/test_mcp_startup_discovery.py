"""B57: live-registry tool discovery and config-driven MCP startup registration.

Covers:
- ToolGateway.live_tool_definitions() advertises the live authorized registry
  (built-ins + dynamically registered MCP tools), not the static catalog.
- Dynamic MCP tools carry their discovered input schema and risk class.
- The static classmethod catalog is preserved for existing callers.
- register_configured_mcp_servers() builds stdio/HTTP transports from config,
  reuses the scanned registration path, and reports per-server failures without
  aborting the remaining servers.
- load_mcp_servers_config() merges global/project config.yaml mcp_servers.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness_core.storage.db import LedgerStore
from harness_core.gateway import (
    TOOL_DEFINITIONS,
    ToolGateway,
    _build_mcp_transport,
    register_configured_mcp_servers,
)
from harness_core.mcp_client import MCPClientAdapter
from harness_core.config_loader import load_mcp_servers_config
from harness_core.core.models import RiskClass


class _FakeMCPTransport:
    """In-process MCP transport stub (no subprocess) for hermetic discovery tests."""

    def __init__(self, tools=None, call_text="docs-result", fail_list=False):
        self._tools = tools if tools is not None else [
            {
                "name": "lookup_docs",
                "description": "Query architectural documentation and knowledge base",
                "inputSchema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            }
        ]
        self._call_text = call_text
        self._fail_list = fail_list
        self.calls = []
        self.closed = False

    def send_request(self, method, params=None, timeout=30.0):
        params = params or {}
        if method == "initialize":
            return {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-mcp", "version": "1.0.0"},
            }
        if method == "tools/list":
            if self._fail_list:
                raise ConnectionError("fake server refused tools/list")
            return {"tools": self._tools}
        if method == "tools/call":
            self.calls.append((params.get("name"), params.get("arguments")))
            return {"content": [{"type": "text", "text": self._call_text}], "isError": False}
        raise ValueError(f"unexpected method: {method}")

    def send_notification(self, method, params=None):
        pass

    def close(self):
        self.closed = True


class TestLiveToolDefinitions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hex_b57_")
        self.ledger = LedgerStore(Path(self.tmp) / "ledger.db")
        self.gateway = ToolGateway(self.ledger)

    def tearDown(self):
        self.ledger.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_live_registry_matches_static_catalog(self):
        live = self.gateway.live_tool_definitions()
        self.assertEqual(len(live), len(TOOL_DEFINITIONS))
        self.assertEqual([d["id"] for d in live], [d["id"] for d in TOOL_DEFINITIONS])
        self.assertIn("read_file", [d["id"] for d in live])

    def test_dynamic_mcp_tool_is_advertised_with_schema(self):
        adapter = MCPClientAdapter("fake_docs", _FakeMCPTransport())
        registered = self.gateway.register_mcp_server(adapter, enforce_scan=False)
        self.assertIn("fake_docs__lookup_docs", registered)

        live = self.gateway.live_tool_definitions()
        ids = [d["id"] for d in live]
        self.assertIn("fake_docs__lookup_docs", ids)
        self.assertIn("lookup_docs", ids)  # bare alias also advertised
        self.assertIn("read_file", ids)    # built-ins preserved

        dyn = next(d for d in live if d["id"] == "fake_docs__lookup_docs")
        self.assertEqual(dyn["parameters"]["required"], ["query"])
        self.assertEqual(dyn["risk_class"], RiskClass.R1.value)
        self.assertEqual(dyn["description"], "Query architectural documentation and knowledge base")

    def test_unregistered_tool_is_not_advertised(self):
        # Discovery must reflect the live registry: a tool that is not registered
        # cannot be promised to clients (invocation would reject it).
        del self.gateway.tools["read_file"]
        ids = [d["id"] for d in self.gateway.live_tool_definitions()]
        self.assertNotIn("read_file", ids)

    def test_static_classmethod_catalog_is_unchanged(self):
        adapter = MCPClientAdapter("fake_docs", _FakeMCPTransport())
        self.gateway.register_mcp_server(adapter, enforce_scan=False)
        static = ToolGateway.list_tool_definitions()
        self.assertEqual(len(static), len(TOOL_DEFINITIONS))
        self.assertNotIn("fake_docs__lookup_docs", [d["id"] for d in static])


class TestBuildMCPTransport(unittest.TestCase):
    def test_stdio_transport_from_command_list(self):
        with patch("harness_core.gateway.StdioMCPTransport") as Stdio:
            _build_mcp_transport({"command": ["python", "-m", "srv"], "cwd": ".", "env": {"A": 1}})
        Stdio.assert_called_once_with(command=["python", "-m", "srv"], cwd=".", env={"A": "1"})

    def test_stdio_transport_from_command_string(self):
        with patch("harness_core.gateway.StdioMCPTransport") as Stdio:
            _build_mcp_transport({"command": "python -m srv"})
        Stdio.assert_called_once_with(command=["python", "-m", "srv"], cwd=None, env=None)

    def test_http_transport_from_url(self):
        with patch("harness_core.gateway.HttpSSEMCPTransport") as Http:
            _build_mcp_transport({"url": "http://localhost:9000/mcp", "headers": {"X-Token": "abc"}})
        Http.assert_called_once_with(endpoint_url="http://localhost:9000/mcp", headers={"X-Token": "abc"})

    def test_invalid_config_raises(self):
        with self.assertRaises(ValueError):
            _build_mcp_transport({"note": "no command or url"})
        with self.assertRaises(ValueError):
            _build_mcp_transport({"command": []})
        with self.assertRaises(ValueError):
            _build_mcp_transport({"url": "http://x", "headers": "not-a-mapping"})
        with self.assertRaises(ValueError):
            _build_mcp_transport({"command": ["x"], "env": "not-a-mapping"})


class TestConfigDrivenMCPStartup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hex_b57_cfg_")
        self.ledger = LedgerStore(Path(self.tmp) / "ledger.db")
        self.gateway = ToolGateway(self.ledger)

    def tearDown(self):
        self.ledger.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_registers_configured_servers(self):
        def fake_build(spec):
            return _FakeMCPTransport()

        with patch("harness_core.gateway._build_mcp_transport", side_effect=fake_build):
            res = register_configured_mcp_servers(
                self.gateway,
                {"docs": {"command": ["python", "-m", "docs"]}},
                enforce_scan=False,
            )
        self.assertIn("docs", res["registered"])
        self.assertIn("docs__lookup_docs", res["registered"]["docs"])
        self.assertEqual(res["failed"], {})
        self.assertIn("docs__lookup_docs", self.gateway.tools)
        self.assertIn("docs__lookup_docs", [d["id"] for d in self.gateway.live_tool_definitions()])

    def test_failing_server_is_reported_and_does_not_abort_others(self):
        def fake_build(spec):
            if "command" not in spec and "url" not in spec:
                raise ValueError("MCP server config needs a 'command' (stdio) or 'url' (HTTP)")
            return _FakeMCPTransport()

        with patch("harness_core.gateway._build_mcp_transport", side_effect=fake_build):
            res = register_configured_mcp_servers(
                self.gateway,
                {"bad": {"note": "missing"}, "good": {"command": ["x"]}},
                enforce_scan=False,
            )
        self.assertIn("bad", res["failed"])
        self.assertIn("good", res["registered"])
        self.assertIn("good__lookup_docs", self.gateway.tools)

    def test_discovery_failure_is_reported_not_silent(self):
        transport = _FakeMCPTransport(fail_list=True)
        with patch("harness_core.gateway._build_mcp_transport", return_value=transport):
            res = register_configured_mcp_servers(
                self.gateway, {"broken": {"command": ["x"]}}, enforce_scan=False
            )
        self.assertIn("broken", res["failed"])
        self.assertEqual(res["registered"], {})
        self.assertNotIn("broken__lookup_docs", self.gateway.tools)
        self.assertTrue(transport.closed)  # failed transport closed, no orphaned child

    def test_empty_config_is_noop(self):
        res = register_configured_mcp_servers(self.gateway, {})
        self.assertEqual(res, {"registered": {}, "failed": {}})


class TestLoadMcpServersConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hex_b57_yaml_")
        self.global_dir = Path(self.tmp) / "global"
        self.workspace = Path(self.tmp) / "ws"
        (self.global_dir).mkdir(parents=True, exist_ok=True)
        (self.workspace / ".hex").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, path: Path, text: str):
        path.write_text(text, encoding="utf-8")

    def test_empty_when_unconfigured(self):
        self.assertEqual(load_mcp_servers_config(self.global_dir, self.workspace), {})

    def test_global_and_project_merge(self):
        self._write(self.global_dir / "config.yaml",
                    "mcp_servers:\n  docs:\n    command: ['python', '-m', 'docs']\n  shared:\n    url: http://global\n")
        self._write(self.workspace / ".hex" / "config.yaml",
                    "mcp_servers:\n  shared:\n    url: http://project\n  extra:\n    command: ['x']\n")
        cfg = load_mcp_servers_config(self.global_dir, self.workspace)
        self.assertEqual(set(cfg), {"docs", "shared", "extra"})
        self.assertEqual(cfg["shared"]["url"], "http://project")  # project wins
        self.assertEqual(cfg["docs"]["command"], ["python", "-m", "docs"])

    def test_malformed_config_is_skipped(self):
        self._write(self.global_dir / "config.yaml", "mcp_servers: [this is : bad yaml\n")
        self._write(self.workspace / ".hex" / "config.yaml",
                    "mcp_servers:\n  ok:\n    command: ['x']\n")
        cfg = load_mcp_servers_config(self.global_dir, self.workspace)
        self.assertEqual(set(cfg), {"ok"})


if __name__ == "__main__":
    unittest.main()