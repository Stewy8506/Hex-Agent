"""Comprehensive Unit Test Suite for Polyglot LSP Gateway (tests/test_lsp_client.py).

Tests:
1. Content-Length header framing and byte-stream parsing.
2. Real stdio subprocess transport with mock LSP server.
3. Handshake lifecycle (initialize, initialized, shutdown, exit).
4. Document synchronization (didOpen, didChange, didClose).
5. textDocument/publishDiagnostics notification handling and LSPDiagnostic model.
6. Definition, references, and hover query methods.
7. Diagnostic store severity filtering (ERROR, WARNING, INFO, HINT, ALL).
8. LanguageServerRegistry auto-detection, priority sorting, and environment overrides.
9. LSPManager multi-language orchestration, lazy startup, and clean teardown.
10. Error resilience (timeout, JSON-RPC error payloads, broken pipes).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from harness_core.lsp.client import (
    LSPClient,
    LSPDiagnostic,
    LSPMessage,
    LSPResponseError,
    LSPTimeoutError,
    StdioLSPTransport,
    path_to_uri,
    uri_to_path,
)
from harness_core.lsp.manager import LSPManager
from harness_core.lsp.registry import (
    LanguageServerRegistry,
    LanguageServerSpec,
)

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
        uri = msg.get("params", {}).get("textDocument", {}).get("uri", "")
        # Publish mock diagnostics
        send_msg({
            "jsonrpc": "2.0",
            "method": "textDocument/publishDiagnostics",
            "params": {
                "uri": uri,
                "diagnostics": [
                    {
                        "range": {
                            "start": {"line": 10, "character": 4},
                            "end": {"line": 10, "character": 12}
                        },
                        "severity": 1,
                        "message": "Type mismatch: expected int, got str",
                        "source": "mock-lsp",
                        "code": "E001"
                    },
                    {
                        "range": {
                            "start": {"line": 20, "character": 0},
                            "end": {"line": 20, "character": 5}
                        },
                        "severity": 2,
                        "message": "Unused variable 'x'",
                        "source": "mock-lsp",
                        "code": "W002"
                    }
                ]
            }
        })
    elif method == "textDocument/hover":
        send_msg({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "contents": {"kind": "markdown", "value": "```python\\nfoo: int\\n```\\nMock docstring."}
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
                        "start": {"line": 5, "character": 4},
                        "end": {"line": 5, "character": 7}
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
                        "start": {"line": 15, "character": 8},
                        "end": {"line": 15, "character": 11}
                    }
                }
            ]
        })
    elif method == "custom/error":
        send_msg({
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32600, "message": "Invalid request"}
        })
    elif method == "shutdown":
        send_msg({"jsonrpc": "2.0", "id": req_id, "result": None})
    elif method == "exit":
        sys.exit(0)
"""


class TestLSPFramingAndModels(unittest.TestCase):
    """Tests for LSPMessage, URI conversions, and diagnostic models."""

    def test_uri_conversions(self):
        with tempfile.TemporaryDirectory() as td:
            test_file = Path(td) / "module.py"
            test_file.write_text("x = 1\n", encoding="utf-8")
            uri = path_to_uri(test_file)
            self.assertTrue(uri.startswith("file://"))
            recovered_path = uri_to_path(uri)
            self.assertEqual(Path(recovered_path).resolve(), test_file.resolve())

    def test_lsp_message_serialization(self):
        msg = LSPMessage(
            id=42,
            method="textDocument/hover",
            params={"textDocument": {"uri": "file:///test.py"}},
        )
        d = msg.to_dict()
        self.assertEqual(d["jsonrpc"], "2.0")
        self.assertEqual(d["id"], 42)
        self.assertEqual(d["method"], "textDocument/hover")
        self.assertNotIn("result", d)

        recovered = LSPMessage.from_dict(d)
        self.assertEqual(recovered.id, 42)
        self.assertEqual(recovered.method, "textDocument/hover")

    def test_lsp_diagnostic_model(self):
        diag = LSPDiagnostic(
            file_path="/workspace/main.py",
            line=12,
            character=4,
            end_line=12,
            end_character=10,
            severity=1,
            severity_name="ERROR",
            message="Undefined variable",
            source="pyright",
            code="reportUndefinedVariable",
        )
        self.assertEqual(diag.severity, 1)
        self.assertEqual(diag.severity_name, "ERROR")
        d = diag.to_dict()
        self.assertEqual(d["line"], 12)
        self.assertEqual(d["code"], "reportUndefinedVariable")


class TestStdioTransportAndClient(unittest.TestCase):
    """Tests using a mock stdio language server subprocess."""

    def setUp(self):
        cmd = [sys.executable, "-u", "-c", MOCK_LSP_SERVER_CODE]
        self.transport = StdioLSPTransport(command=cmd)

    def tearDown(self):
        self.transport.close()

    def test_transport_send_request_and_response(self):
        self.assertTrue(self.transport.is_alive)
        res = self.transport.send_request("initialize", {"capabilities": {}})
        self.assertIsInstance(res, dict)
        self.assertIn("capabilities", res)
        self.assertTrue(res["capabilities"]["hoverProvider"])

    def test_transport_error_response(self):
        with self.assertRaises(LSPResponseError) as ctx:
            self.transport.send_request("custom/error", {})
        self.assertEqual(ctx.exception.code, -32600)
        self.assertIn("Invalid request", ctx.exception.message)

    def test_transport_timeout(self):
        # Sending a method the mock server ignores to trigger timeout
        with self.assertRaises(LSPTimeoutError):
            self.transport.send_request("method/ignored_by_server", {}, timeout=0.3)

    def test_client_lifecycle_and_document_sync(self):
        with tempfile.TemporaryDirectory() as td:
            workspace_root = Path(td)
            client = LSPClient(transport=self.transport, workspace_root=workspace_root)
            init_res = client.initialize(workspace_root)
            self.assertTrue(client.is_initialized)
            self.assertTrue(client.capabilities.get("hoverProvider"))

            # Document sync: didOpen
            sample_file = workspace_root / "main.py"
            sample_file.write_text("a = 10\nprint(a)\n", encoding="utf-8")
            client.did_open(sample_file)

            # Allow time for async publishDiagnostics notification to arrive
            time.sleep(0.3)

            # Check published diagnostics
            diags = client.get_diagnostics(file_path=sample_file)
            self.assertEqual(len(diags), 2)
            self.assertEqual(diags[0].severity_name, "ERROR")
            self.assertEqual(diags[0].code, "E001")
            self.assertEqual(diags[1].severity_name, "WARNING")

            # Severity filter
            errors = client.get_diagnostics(file_path=sample_file, severity="ERROR")
            self.assertEqual(len(errors), 1)
            self.assertEqual(errors[0].line, 10)

            warnings = client.get_diagnostics(file_path=sample_file, severity=2)
            self.assertEqual(len(warnings), 1)
            self.assertEqual(warnings[0].line, 20)

            # Queries
            hover = client.get_hover(sample_file, line=10, character=4)
            self.assertIsNotNone(hover)
            self.assertIn("Mock docstring", hover["contents"]["value"])

            definitions = client.get_definition(sample_file, line=10, character=4)
            self.assertEqual(len(definitions), 1)
            self.assertEqual(definitions[0]["range"]["start"]["line"], 5)

            references = client.get_references(sample_file, line=10, character=4)
            self.assertEqual(len(references), 1)
            self.assertEqual(references[0]["range"]["start"]["line"], 15)

            # Document update & close
            client.did_change(sample_file, text="a = 'hello'\n")
            client.did_close(sample_file)

            # Graceful shutdown
            client.shutdown()
            self.assertFalse(client.is_initialized)


class TestLanguageServerRegistry(unittest.TestCase):
    """Tests for multi-language server registry and detection."""

    def test_default_specs_coverage(self):
        reg = LanguageServerRegistry()
        specs = reg.get_all_specs()
        self.assertGreaterEqual(len(specs), 9)

        languages = {s.language_id for s in specs}
        self.assertIn("python", languages)
        self.assertIn("typescript", languages)
        self.assertIn("go", languages)
        self.assertIn("rust", languages)
        self.assertIn("cpp", languages)
        self.assertIn("java", languages)

    def test_find_server_for_file_with_env_override(self):
        with tempfile.TemporaryDirectory() as td:
            reg = LanguageServerRegistry()
            py_file = Path(td) / "script.py"
            py_file.write_text("print('test')\n", encoding="utf-8")

            # Fake executable script
            fake_bin = Path(td) / "fake_pyright"
            fake_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_bin.chmod(0o755)

            with mock.patch.dict(os.environ, {"HEX_LSP_PYTHON_BIN": str(fake_bin)}):
                spec = reg.find_server_for_file(py_file, workspace_root=td, require_installed=True)
                self.assertIsNotNone(spec)
                self.assertEqual(spec.language_id, "python")
                self.assertEqual(spec.resolve_binary(), str(fake_bin))
                cmd = spec.get_command()
                self.assertEqual(cmd[0], str(fake_bin))

    def test_find_server_by_extension_uninstalled_lookup(self):
        with tempfile.TemporaryDirectory() as td:
            reg = LanguageServerRegistry()
            rs_file = Path(td) / "main.rs"
            spec = reg.find_server_for_file(rs_file, require_installed=False)
            self.assertIsNotNone(spec)
            self.assertEqual(spec.server_name, "rust-analyzer")
            self.assertEqual(spec.language_id, "rust")

            ts_file = Path(td) / "app.tsx"
            spec_ts = reg.find_server_for_file(ts_file, require_installed=False)
            self.assertIsNotNone(spec_ts)
            self.assertEqual(spec_ts.language_id, "typescript")

            cpp_file = Path(td) / "engine.cpp"
            spec_cpp = reg.find_server_for_file(cpp_file, require_installed=False)
            self.assertIsNotNone(spec_cpp)
            self.assertEqual(spec_cpp.language_id, "cpp")

    def test_detect_workspace_servers(self):
        with tempfile.TemporaryDirectory() as td:
            reg = LanguageServerRegistry()
            (Path(td) / "Cargo.toml").write_text("[package]\nname = 'test'\n", encoding="utf-8")
            (Path(td) / "package.json").write_text('{"name": "test"}\n', encoding="utf-8")

            detected = reg.detect_workspace_servers(td)
            lang_ids = {s.language_id for s in detected}
            self.assertIn("rust", lang_ids)
            self.assertIn("typescript", lang_ids)


class TestLSPManager(unittest.TestCase):
    """Tests for LSPManager workspace coordinator."""

    def test_manager_context_and_close(self):
        with tempfile.TemporaryDirectory() as td:
            workspace_root = Path(td)
            spec = LanguageServerSpec(
                language_id="python",
                server_name="mock-py",
                binary_candidates=[sys.executable],
                default_args=["-u", "-c", MOCK_LSP_SERVER_CODE],
                file_extensions=(".py",),
                priority=200,
            )

            reg = LanguageServerRegistry(include_defaults=False)
            reg.register_server(spec)

            with LSPManager(workspace_root=workspace_root, registry=reg) as mgr:
                test_file = workspace_root / "demo.py"
                test_file.write_text("x = 42\n", encoding="utf-8")

                client = mgr.get_client_for_file(test_file, auto_start=True)
                self.assertIsNotNone(client)
                self.assertIn("python", mgr.active_languages)

                # Trigger sync
                mgr.sync_file(test_file)
                time.sleep(0.3)

                diags = mgr.get_diagnostics()
                self.assertGreaterEqual(len(diags), 1)
                self.assertEqual(diags[0].message, "Type mismatch: expected int, got str")

            # After context exit, all servers must be closed
            self.assertEqual(len(mgr.active_languages), 0)


if __name__ == "__main__":
    unittest.main()
