"""Unit tests for the 21 ToolGateway tools (including patch, search_files, web_extract, terminal, todo, clarify, memory, process, session_search)."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from harness_core.db import LedgerStore
from harness_core.gateway import PolicyBlockedError, ToolGateway
from harness_core.models import RiskClass, TaskSubmitRequest


class TestGatewayTools(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.workspace_dir = Path(self.temp_dir) / "workspace"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = Path(self.temp_dir) / "ledger.db"
        self.ledger = LedgerStore(self.db_path)
        self.gateway = ToolGateway(self.ledger)

    def tearDown(self) -> None:
        self.ledger.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_total_tool_count_is_26(self) -> None:
        """Verify exactly 26 tools are registered in the gateway."""
        self.assertEqual(len(self.gateway.tools), 26)
        expected_tools = {
            "read_file",
            "list_files",
            "search_files",
            "todo",
            "clarify",
            "session_search",
            "query_graphify",
            "web_search",
            "web_extract",
            "memory",
            "lsp_diagnostics",
            "lsp_definition",
            "lsp_references",
            "lsp_hover",
            "write_file",
            "patch",
            "run_tests",
            "terminal",
            "process",
            "git_push_remote",
            "modify_system_policy",
            "delegate",
            "subagent_status",
            "subagent_wait",
            "browser",
            "image_inspect",
        }
        self.assertEqual(set(self.gateway.tools.keys()), expected_tools)

    def test_subagent_tools_execution(self) -> None:
        """Test delegate, subagent_status, and subagent_wait through the gateway."""
        # 1. Delegate tool call
        del_res = self.gateway._tool_delegate(
            self.workspace_dir,
            {"goal": "Research caching strategies", "role": "researcher", "wait": True},
        )
        self.assertTrue(del_res["ok"])
        subagent_id = del_res["result"]["subagent_id"]
        self.assertEqual(del_res["result"]["role"], "researcher")

        # 2. Subagent status tool call
        stat_res = self.gateway._tool_subagent_status(
            self.workspace_dir,
            {"subagent_id": subagent_id},
        )
        self.assertTrue(stat_res["ok"])
        self.assertEqual(stat_res["subagent"]["subagent_id"], subagent_id)

        # 3. Subagent wait tool call
        wait_res = self.gateway._tool_subagent_wait(
            self.workspace_dir,
            {"subagent_id": subagent_id, "timeout": 5.0},
        )
        self.assertTrue(wait_res["ok"])
        self.assertEqual(wait_res["result"]["subagent_id"], subagent_id)

    def test_patch_search_replace(self) -> None:
        """Test patching a file via search and replace blocks."""
        test_file = self.workspace_dir / "sample.py"
        test_file.write_text("def hello():\n    return 'world'\n", encoding="utf-8")

        res = self.gateway.tools["patch"].handler(
            self.workspace_dir,
            {
                "path": "sample.py",
                "find": "return 'world'",
                "replace": "return 'hex'",
            },
        )
        self.assertTrue(res["ok"])
        self.assertIn("def hello():\n    return 'hex'\n", test_file.read_text(encoding="utf-8"))

    def test_patch_unified_diff(self) -> None:
        """Test patching a file via a unified diff format."""
        test_file = self.workspace_dir / "calc.py"
        test_file.write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")

        diff = (
            "--- a/calc.py\n"
            "+++ b/calc.py\n"
            "@@ -1,3 +1,3 @@\n"
            " a = 1\n"
            "-b = 2\n"
            "+b = 20\n"
            " c = 3\n"
        )
        res = self.gateway.tools["patch"].handler(
            self.workspace_dir,
            {"path": "calc.py", "diff": diff},
        )
        self.assertTrue(res["ok"])
        self.assertEqual(test_file.read_text(encoding="utf-8"), "a = 1\nb = 20\nc = 3\n")

    def test_patch_path_traversal_blocked(self) -> None:
        """Test that patch rejects attempts to escape workspace."""
        with self.assertRaises(PolicyBlockedError):
            self.gateway.tools["patch"].handler(
                self.workspace_dir,
                {"path": "../escape.txt", "find": "a", "replace": "b"},
            )

    def test_search_files(self) -> None:
        """Test search_files with substring, regex, and file filtering."""
        (self.workspace_dir / "src").mkdir()
        (self.workspace_dir / "src" / "alpha.py").write_text(
            "def calculate_total():\n    return 42\n", encoding="utf-8"
        )
        (self.workspace_dir / "src" / "beta.txt").write_text(
            "calculate_total is a helper function\n", encoding="utf-8"
        )

        # 1. Search in all files
        res = self.gateway.tools["search_files"].handler(
            self.workspace_dir,
            {"query": "calculate_total"},
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["total_matches"], 2)

        # 2. Filter by glob
        res_glob = self.gateway.tools["search_files"].handler(
            self.workspace_dir,
            {"query": "calculate_total", "file_pattern": "*.py"},
        )
        self.assertTrue(res_glob["ok"])
        self.assertEqual(res_glob["total_matches"], 1)
        self.assertEqual(res_glob["matches"][0]["path"], "src/alpha.py")

        # 3. Regex match
        res_re = self.gateway.tools["search_files"].handler(
            self.workspace_dir,
            {"query": r"return\s+\d+"},
        )
        self.assertTrue(res_re["ok"])
        self.assertEqual(res_re["total_matches"], 1)

    def test_web_extract_url_validation_and_mock(self) -> None:
        """Test web_extract rejects invalid URLs and extracts text properly."""
        # Non-http/https URL
        res_invalid = self.gateway.tools["web_extract"].handler(
            self.workspace_dir,
            {"url": "ftp://example.com/test"},
        )
        self.assertFalse(res_invalid["ok"])

        # Mocked HTTP request
        mock_html = b"<html><head><title>Test</title></head><body><h1>HEX Agent</h1><p>Running with token sk-abcdef1234567890!</p></body></html>"
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = mock_html
            mock_resp.headers.get_content_charset.return_value = "utf-8"
            mock_urlopen.return_value.__enter__.return_value = mock_resp

            res = self.gateway.tools["web_extract"].handler(
                self.workspace_dir,
                {"url": "https://example.com/docs"},
            )
            self.assertTrue(res["ok"])
            self.assertIn("HEX Agent", res["content"])
            self.assertIn("[REDACTED_SECRET]", res["content"])
            self.assertNotIn("sk-abcdef1234567890", res["content"])

    def test_terminal(self) -> None:
        """Test executing terminal command in sandbox with output and secret redaction."""
        res = self.gateway.tools["terminal"].handler(
            self.workspace_dir,
            {"command": "echo 'hello from terminal token: sk-secret123456789'"},
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["exit_code"], 0)
        self.assertIn("hello from terminal", res["stdout"])
        self.assertIn("[REDACTED_SECRET]", res["stdout"])
        self.assertNotIn("sk-secret123456789", res["stdout"])

    def test_todo_lifecycle(self) -> None:
        """Test todo add, read, update, and clear operations."""
        todo_tool = self.gateway.tools["todo"].handler

        # 1. Add tasks
        res_add = todo_tool(
            self.workspace_dir,
            {"action": "add", "item": "Refactor auth controller"},
        )
        self.assertTrue(res_add["ok"])
        self.assertEqual(len(res_add["todos"]), 1)
        self.assertEqual(res_add["todos"][0]["status"], "pending")

        # 2. Update task
        res_update = todo_tool(
            self.workspace_dir,
            {"action": "update", "index": 0, "status": "completed"},
        )
        self.assertTrue(res_update["ok"])
        self.assertEqual(res_update["todos"][0]["status"], "completed")

        # 3. Read tasks
        res_read = todo_tool(self.workspace_dir, {"action": "read"})
        self.assertTrue(res_read["ok"])
        self.assertEqual(len(res_read["todos"]), 1)

        # 4. Clear tasks
        res_clear = todo_tool(self.workspace_dir, {"action": "clear"})
        self.assertTrue(res_clear["ok"])
        self.assertEqual(len(res_clear["todos"]), 0)

    def test_clarify(self) -> None:
        """Test clarify structured question prompting."""
        res = self.gateway.tools["clarify"].handler(
            self.workspace_dir,
            {
                "question": "Which database engine should be used?",
                "options": ["PostgreSQL", "SQLite"],
            },
        )
        self.assertTrue(res["ok"])
        self.assertTrue(res["clarification_needed"])
        self.assertEqual(res["options"], ["PostgreSQL", "SQLite"])

    def test_memory_crud(self) -> None:
        """Test memory set, get, list, and delete operations."""
        mem_tool = self.gateway.tools["memory"].handler

        # 1. Set key
        res_set = mem_tool(
            self.workspace_dir,
            {
                "action": "set",
                "key": "framework_choice",
                "value": "FastAPI with asyncpg",
            },
        )
        self.assertTrue(res_set["ok"])

        # 2. Get key
        res_get = mem_tool(
            self.workspace_dir,
            {"action": "get", "key": "framework_choice"},
        )
        self.assertTrue(res_get["ok"])
        self.assertEqual(res_get["memory"]["value"], "FastAPI with asyncpg")

        # 3. List
        res_list = mem_tool(self.workspace_dir, {"action": "list"})
        self.assertTrue(res_list["ok"])
        self.assertIn("framework_choice", res_list["memories"])

        # 4. Delete
        res_del = mem_tool(
            self.workspace_dir,
            {"action": "delete", "key": "framework_choice"},
        )
        self.assertTrue(res_del["ok"])
        self.assertTrue(res_del["deleted"])

    def test_process_list_and_protected_kill(self) -> None:
        """Test process listing and rejection of killing protected system PIDs."""
        proc_tool = self.gateway.tools["process"].handler

        # List procs
        res_list = proc_tool(self.workspace_dir, {"action": "list"})
        self.assertTrue(res_list["ok"])
        self.assertIsInstance(res_list["processes"], list)

        # Attempt to kill PID 1 must be blocked
        with self.assertRaises(PolicyBlockedError):
            proc_tool(self.workspace_dir, {"action": "kill", "pid": 1})

        # Attempt to kill self PID must be blocked
        with self.assertRaises(PolicyBlockedError):
            proc_tool(self.workspace_dir, {"action": "kill", "pid": os.getpid()})

    def test_session_search(self) -> None:
        """Test session_search finds matching tasks in the ledger."""
        # Create a sample task in ledger
        self.ledger.submit_task(
            TaskSubmitRequest(
                goal="Write stripe webhook endpoint in payment/webhook.py",
                workspace_root=str(self.workspace_dir),
            )
        )

        res = self.gateway.tools["session_search"].handler(
            self.workspace_dir,
            {"query": "stripe webhook"},
        )
        self.assertTrue(res["ok"])
        self.assertGreaterEqual(res["count"], 1)

    def test_invoke_via_gateway_with_policy_and_memoization(self) -> None:
        """Test end-to-end invocation of a new R0 tool (search_files) via gateway.invoke."""
        # Create task in ledger
        task_info = self.ledger.submit_task(
            TaskSubmitRequest(
                goal="Find files containing keyword",
                workspace_root=str(self.workspace_dir),
            )
        )
        task_id = task_info["task_id"]

        # Write test file
        (self.workspace_dir / "hello.py").write_text("print('hex-engine')\n", encoding="utf-8")

        # 1. First execution
        res1 = self.gateway.invoke(
            task_id=task_id,
            attempt_id="att-1",
            step_id="step-1",
            sequence_in_step=0,
            tool_id="search_files",
            args={"query": "hex-engine"},
            workspace_dir=self.workspace_dir,
        )
        self.assertTrue(res1["ok"])
        self.assertEqual(res1["total_matches"], 1)
        self.assertFalse(res1.get("_replayed", False))

        # 2. Replay with identical parameters triggers 0ms deterministic memoization
        res2 = self.gateway.invoke(
            task_id=task_id,
            attempt_id="att-1",
            step_id="step-1",
            sequence_in_step=0,
            tool_id="search_files",
            args={"query": "hex-engine"},
            workspace_dir=self.workspace_dir,
        )
        self.assertTrue(res2["ok"])
        self.assertTrue(res2.get("_replayed", False))
        self.assertIn("_replayed_from_call_id", res2)


if __name__ == "__main__":
    unittest.main()
