"""Comprehensive Integration Test Suite for Polyglot LSP Gateway & Autonomous Repair (tests/test_lsp_gateway_and_repair.py).

Tests:
1. ToolGateway tool registration and execution for lsp_diagnostics, lsp_definition, lsp_references, lsp_hover (R1).
2. ToolGateway graceful fallback when no language server binary is installed.
3. Pre-flight compiler diagnostic gate (_run_compiler_preflight) detecting syntax/type errors.
4. Autonomous repair loop short-circuiting test runner upon compiler errors and recording LSP_COMPILER_PREFLIGHT_FAILED in ledger.
5. CLI `hex lsp status`, `hex lsp diagnostics`, `hex lsp definition`, `hex lsp hover` commands.
6. TUI `/lsp` slash command and help catalog integration.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from harness_core.cli import main as cli_main
from harness_core.db import LedgerStore
from harness_core.gateway import ToolGateway
from harness_core.lsp.client import path_to_uri
from harness_core.lsp.manager import LSPManager
from harness_core.lsp.registry import LanguageServerRegistry, LanguageServerSpec
from harness_core.models import RiskClass, TaskState, TaskSubmitRequest
from harness_core.orchestrator import HarnessOrchestrator
from harness_core.tui import HexConsole

MOCK_LSP_SERVER_CODE = """
import sys, json

def send_msg(payload):
    data = json.dumps(payload).encode('utf-8')
    header = f"Content-Length: {len(data)}\\r\\n\\r\\n".encode('ascii')
    sys.stdout.buffer.write(header + data)
    sys.stdout.buffer.flush()

while True:
    content_length = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            sys.exit(0)
        line_clean = line.strip()
        if not line_clean:
            if content_length is not None:
                break
            continue
        if line.lower().startswith(b"content-length:"):
            parts = line.split(b":", 1)
            content_length = int(parts[1].strip())

    if content_length is None:
        continue

    body = b""
    while len(body) < content_length:
        chunk = sys.stdin.buffer.read(content_length - len(body))
        if not chunk:
            sys.exit(0)
        body += chunk

    msg = json.loads(body.decode('utf-8'))
    req_id = msg.get("id")
    method = msg.get("method")

    if method == "initialize":
        send_msg({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "capabilities": {
                    "textDocumentSync": 1,
                    "hoverProvider": True,
                    "definitionProvider": True,
                    "referencesProvider": True
                }
            }
        })
    elif method == "textDocument/didOpen":
        params = msg.get("params", {})
        doc = params.get("textDocument", {})
        uri = doc.get("uri", "")
        text = doc.get("text", "")
        # Publish diagnostic if text has 'syntax_error' or unconditionally for test
        has_error = "syntax_error" in text or "error" in text or len(text) > 0
        diags = []
        if has_error:
            diags.append({
                "range": {
                    "start": {"line": 2, "character": 4},
                    "end": {"line": 2, "character": 12}
                },
                "severity": 1,
                "message": "Type mismatch: expected int, got str",
                "source": "mock-compiler",
                "code": "E101"
            })
            diags.append({
                "range": {
                    "start": {"line": 5, "character": 0},
                    "end": {"line": 5, "character": 5}
                },
                "severity": 2,
                "message": "Unused import 'math'",
                "source": "mock-compiler",
                "code": "W202"
            })
        send_msg({
            "jsonrpc": "2.0",
            "method": "textDocument/publishDiagnostics",
            "params": {
                "uri": uri,
                "diagnostics": diags
            }
        })
    elif method == "textDocument/hover":
        send_msg({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "contents": {"kind": "markdown", "value": "```python\\nmy_function(x: int) -> str\\n```\\nCompute transformed string."}
            }
        })
    elif method == "textDocument/definition":
        uri = msg.get("params", {}).get("textDocument", {}).get("uri", "")
        send_msg({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": [
                {
                    "uri": uri,
                    "range": {
                        "start": {"line": 10, "character": 4},
                        "end": {"line": 10, "character": 15}
                    }
                }
            ]
        })
    elif method == "textDocument/references":
        uri = msg.get("params", {}).get("textDocument", {}).get("uri", "")
        send_msg({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": [
                {
                    "uri": uri,
                    "range": {
                        "start": {"line": 25, "character": 8},
                        "end": {"line": 25, "character": 19}
                    }
                }
            ]
        })
    elif method == "shutdown":
        send_msg({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": None
        })
    elif method == "exit":
        sys.exit(0)
"""


def _create_mock_registry() -> LanguageServerRegistry:
    spec = LanguageServerSpec(
        language_id="python",
        server_name="mock-py",
        binary_candidates=[sys.executable],
        default_args=["-u", "-c", MOCK_LSP_SERVER_CODE],
        file_extensions=(".py",),
        priority=300,
    )
    reg = LanguageServerRegistry(include_defaults=False)
    reg.register_server(spec)
    return reg


class TestToolGatewayLSPTools(unittest.TestCase):
    """Verifies ToolGateway exposure of lsp_diagnostics, lsp_definition, lsp_references, lsp_hover."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.ledger = LedgerStore(":memory:")
        self.registry = _create_mock_registry()
        self.lsp_manager = LSPManager(workspace_root=self.workspace, registry=self.registry)
        self.gateway = ToolGateway(self.ledger, lsp_manager=self.lsp_manager)

    def tearDown(self):
        self.lsp_manager.close_all()
        self.temp_dir.cleanup()

    def test_lsp_tools_registered_as_r1(self):
        expected_tools = ["lsp_diagnostics", "lsp_definition", "lsp_references", "lsp_hover"]
        for tool_id in expected_tools:
            self.assertIn(tool_id, self.gateway.tools)
            spec = self.gateway.tools[tool_id]
            self.assertEqual(spec.risk_class, RiskClass.R1)

    def test_lsp_diagnostics_query(self):
        sample_file = self.workspace / "app.py"
        sample_file.write_text("x = syntax_error\n", encoding="utf-8")

        # Invoke through gateway handler
        res = self.gateway._tool_lsp_diagnostics(self.workspace, {"file_path": "app.py"})
        self.assertIn("diagnostics", res)
        self.assertEqual(res["error_count"], 1)
        self.assertEqual(res["warning_count"], 1)
        self.assertEqual(res["total_count"], 2)

        # Test severity filter
        res_err = self.gateway._tool_lsp_diagnostics(self.workspace, {"file_path": "app.py", "severity": "error"})
        self.assertEqual(res_err["error_count"], 1)
        self.assertEqual(len(res_err["diagnostics"]), 1)
        self.assertEqual(res_err["diagnostics"][0]["severity"], 1)

    def test_lsp_definition_query(self):
        sample_file = self.workspace / "calc.py"
        sample_file.write_text("def compute(): pass\n", encoding="utf-8")

        res = self.gateway._tool_lsp_definition(self.workspace, {"file_path": "calc.py", "line": 1, "character": 5})
        self.assertIn("definitions", res)
        defs = res["definitions"]
        self.assertEqual(len(defs), 1)
        # Verify 1-based indexing in response
        self.assertEqual(defs[0]["line"], 11)
        self.assertEqual(defs[0]["character"], 5)

    def test_lsp_references_query(self):
        sample_file = self.workspace / "calc.py"
        sample_file.write_text("compute()\n", encoding="utf-8")

        res = self.gateway._tool_lsp_references(self.workspace, {"file_path": "calc.py", "line": 1, "character": 1})
        self.assertIn("references", res)
        refs = res["references"]
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["line"], 26)
        self.assertEqual(refs[0]["character"], 9)

    def test_lsp_hover_query(self):
        sample_file = self.workspace / "calc.py"
        sample_file.write_text("compute()\n", encoding="utf-8")

        res = self.gateway._tool_lsp_hover(self.workspace, {"file_path": "calc.py", "line": 1, "character": 1})
        self.assertIn("hover", res)
        contents = res["hover"].get("contents", "")
        self.assertIn("my_function(x: int)", contents)

    def test_unsupported_language_graceful_fallback(self):
        # File with an extension without installed server
        res_diag = self.gateway._tool_lsp_diagnostics(self.workspace, {"file_path": "unknown.xyz"})
        self.assertEqual(res_diag.get("status"), "no_server_available")
        self.assertEqual(res_diag.get("diagnostics"), [])

        res_def = self.gateway._tool_lsp_definition(self.workspace, {"file_path": "unknown.xyz", "line": 1, "character": 1})
        self.assertEqual(res_def.get("status"), "no_server_available")
        self.assertEqual(res_def.get("definitions"), [])

        res_hov = self.gateway._tool_lsp_hover(self.workspace, {"file_path": "unknown.xyz", "line": 1, "character": 1})
        self.assertEqual(res_hov.get("status"), "no_server_available")


class TestCompilerPreflightInterceptor(unittest.TestCase):
    """Verifies pre-flight compiler diagnostic gating in HarnessOrchestrator."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.orch = HarnessOrchestrator(
            data_dir=self.temp_dir.name,
            default_workspace=Path(self.temp_dir.name) / "project",
        )
        self.orch.default_workspace.mkdir(parents=True, exist_ok=True)
        # Inject mock LSP registry
        reg = _create_mock_registry()
        self.orch.lsp_manager = LSPManager(workspace_root=self.orch.default_workspace, registry=reg)
        self.orch.gateway.lsp_manager = self.orch.lsp_manager

    def tearDown(self):
        self.orch.lsp_manager.close_all()
        if hasattr(self, "orch") and self.orch is not None:
            self.orch.shutdown()
        self.temp_dir.cleanup()

    def test_preflight_detects_compiler_errors(self):
        test_file = self.orch.default_workspace / "bad_syntax.py"
        test_file.write_text("x = syntax_error\n", encoding="utf-8")

        result = self.orch._run_compiler_preflight(self.orch.default_workspace, ["bad_syntax.py"])
        self.assertFalse(result["passed"])
        self.assertGreaterEqual(result["error_count"], 1)
        self.assertIn("[ERROR]", result["summary"])
        self.assertIn("Type mismatch: expected int, got str", result["summary"])

    def test_preflight_passes_when_no_errors(self):
        # If no modified files or files don't exist
        result = self.orch._run_compiler_preflight(self.orch.default_workspace, [])
        self.assertTrue(result["passed"])
        self.assertEqual(result["error_count"], 0)


class TestAutonomousRepairWithLSPPreflight(unittest.TestCase):
    """Verifies end-to-end autonomous repair loop interception of compiler errors."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name) / "data"
        self.project_dir = Path(self.temp_dir.name) / "project"
        self.project_dir.mkdir(parents=True, exist_ok=True)

        reg = _create_mock_registry()
        self.orch = HarnessOrchestrator(
            data_dir=self.data_dir,
            default_workspace=self.project_dir,
        )
        self.orch.lsp_manager = LSPManager(workspace_root=self.project_dir, registry=reg)
        self.orch.gateway.lsp_manager = self.orch.lsp_manager

    def tearDown(self):
        self.orch.lsp_manager.close_all()
        if hasattr(self, "orch") and self.orch is not None:
            self.orch.shutdown()
        self.temp_dir.cleanup()

    def test_repair_short_circuits_and_logs_event(self):
        # Prepare a mock repair patch generator
        repair_called = [False]

        def mock_repair_generator(**kwargs):
            repair_called[0] = True
            # Verify failure digest passed in contains compiler diagnostic pre-flight failure
            digest = kwargs.get("failure_digest", "")
            self.assertIn("Compiler Diagnostic Pre-Flight Failed", digest)
            return [{
                "tool_id": "write_file",
                "args": {"path": "main.py", "content": "# Fixed cleanly\n"},
            }]

        self.orch.repair_patch_generator = mock_repair_generator

        # Submit task with steps creating a file with syntax error
        req = TaskSubmitRequest(
            goal="Fix compiler error in main.py",
            constraints={
                "requested_steps": [
                    {
                        "step_id": "step_write_err",
                        "title": "Write file with syntax error",
                        "tool_id": "write_file",
                        "args": {"path": "main.py", "content": "x = syntax_error\n"},
                        "risk_class": "R2",
                    }
                ],
                "max_repair_attempts": 1,
            },
        )
        res = self.orch.submit_and_run(req)
        task_id = res["task_id"]

        # Verify ledger recorded LSP_COMPILER_PREFLIGHT_FAILED event
        events = self.orch.ledger.list_events(task_id)
        preflight_events = [e for e in events if e.get("event_type") == "LSP_COMPILER_PREFLIGHT_FAILED"]
        self.assertGreaterEqual(len(preflight_events), 1)

        event = preflight_events[0]
        self.assertEqual(event.get("reason_code"), "COMPILER_SYNTAX_ERROR")
        payload = event.get("payload") or {}
        self.assertGreaterEqual(payload.get("error_count", 0), 1)
        self.assertIn("summary", payload)


class TestHexLSPCliAndTUI(unittest.TestCase):
    """Verifies CLI `hex lsp` subcommands and TUI `/lsp` slash command."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        (self.workspace / "sample.py").write_text("x = 10\n", encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_cli_lsp_status_json(self):
        f = io.StringIO()
        with mock.patch("sys.stdout", f):
            code = cli_main(["lsp", "status", "--workspace", str(self.workspace), "--json"])
        self.assertEqual(code, 0)
        output = f.getvalue()
        data = json.loads(output)
        self.assertIsInstance(data, list)
        languages = {item["language"] for item in data}
        self.assertIn("python", languages)
        self.assertIn("typescript", languages)

    def test_cli_lsp_diagnostics_no_errors(self):
        f = io.StringIO()
        with mock.patch("sys.stdout", f):
            code = cli_main(["lsp", "diagnostics", "--workspace", str(self.workspace), "--json"])
        self.assertEqual(code, 0)
        output = f.getvalue()
        data = json.loads(output)
        self.assertEqual(data["error_count"], 0)

    def test_cli_lsp_definition_and_hover(self):
        f = io.StringIO()
        with mock.patch("sys.stdout", f):
            code = cli_main(["lsp", "definition", "sample.py", "1", "1", "--workspace", str(self.workspace), "--json"])
        self.assertEqual(code, 0)
        data = json.loads(f.getvalue())
        self.assertIn("definitions", data)

        f_hov = io.StringIO()
        with mock.patch("sys.stdout", f_hov):
            code_hov = cli_main(["lsp", "hover", "sample.py", "1", "1", "--workspace", str(self.workspace), "--json"])
        self.assertEqual(code_hov, 0)
        data_hov = json.loads(f_hov.getvalue())
        self.assertIn("hover", data_hov)

    def test_tui_lsp_slash_command(self):
        orch = HarnessOrchestrator(
            data_dir=self.workspace / "data",
            default_workspace=self.workspace,
        )
        console = HexConsole(orchestrator=orch, workspace_root=self.workspace)

        # Test /lsp default
        f_lsp = io.StringIO()
        with mock.patch("sys.stdout", f_lsp):
            res = console.handle_slash_command("/lsp")
        self.assertFalse(res)
        self.assertIn("Language Server Protocol", f_lsp.getvalue())

        # Test /lsp status
        f_stat = io.StringIO()
        with mock.patch("sys.stdout", f_stat):
            res_stat = console.handle_slash_command("/lsp status")
        self.assertFalse(res_stat)
        self.assertIn("Polyglot Language Server Status", f_stat.getvalue())

        # Test /lsp diagnostics
        f_diag = io.StringIO()
        with mock.patch("sys.stdout", f_diag):
            res_diag = console.handle_slash_command("/lsp diagnostics")
        self.assertFalse(res_diag)
        self.assertIn("LSP Compiler Diagnostics", f_diag.getvalue())

        # Test /help includes /lsp
        f_help = io.StringIO()
        with mock.patch("sys.stdout", f_help):
            console.handle_slash_command("/help")
        self.assertIn("/lsp", f_help.getvalue())


if __name__ == "__main__":
    unittest.main()
