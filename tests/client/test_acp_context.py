"""B41 regression: follow-up turns keep tool results and withdraw stale approvals.

ACP rebuilt model context from the last ten turns' prompt/response text only,
so tool outcomes and reasoning disappeared from later turns; pending approvals
were memory-only and a reloaded session left the client waiting forever.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from harness_core.client.acp import ACPSession, ACPServer, SQLiteHistory, build_conversation_history
from harness_core.client.acp_approval import AcpApprovalBridge
from harness_core.capabilities.gateways.reaction_listener import ApprovalRequest
from harness_core.core.orchestrator import format_conversation_history


class TestConversationHistoryRebuild(unittest.TestCase):
    def test_tool_rows_and_reasoning_survive_a_follow_up_turn(self) -> None:
        history = build_conversation_history([
            {
                "prompt": "add the parser",
                "tool_calls": [
                    {
                        "tool": "write_file",
                        "event": "complete",
                        "payload": {"ok": True, "path": "parser.py"},
                    },
                    {
                        "tool": "run_command",
                        "event": "error",
                        "payload": {"ok": False, "error": "pytest failed"},
                    },
                ],
                "reasoning": "write first, then test",
                "response": "Parser added.",
            }
        ])

        self.assertEqual(
            [row["role"] for row in history],
            ["user", "tool", "tool", "reasoning", "assistant"],
        )
        self.assertEqual(history[1]["tool"], "write_file")
        self.assertEqual(history[1]["status"], "complete")
        self.assertIn("parser.py", history[1]["content"])
        self.assertEqual(history[2]["status"], "error")
        self.assertIn("pytest failed", history[2]["content"])

    def test_legacy_turn_without_tool_rows_still_works(self) -> None:
        history = build_conversation_history([
            {"prompt": "hi", "response": "hello"},
        ])
        self.assertEqual(history, [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ])

    def test_model_prompt_renders_tool_and_reasoning_rows(self) -> None:
        block = format_conversation_history([
            {"role": "user", "content": "add the parser"},
            {
                "role": "tool",
                "tool": "write_file",
                "status": "complete",
                "content": '{"ok": true, "path": "parser.py"}',
            },
            {"role": "reasoning", "content": "write first, then test"},
            {"role": "assistant", "content": "Parser added."},
        ])
        self.assertIn("Tool write_file (complete):", block)
        self.assertIn("parser.py", block)
        self.assertIn("Reasoning: write first, then test", block)


class TestPendingApprovalWithdrawal(unittest.TestCase):
    def test_reloaded_session_withdraws_its_pending_approval(self) -> None:
        sessions = {"task-1": "sess-1"}

        def session_for_task(task_id: str | None) -> str | None:
            # The task→session map is gone after a session reload.
            return sessions.get(task_id or "")

        bridge = AcpApprovalBridge(
            emit=lambda event: None,
            session_for_task=session_for_task,
            workspace_root=".",
            timeout_seconds=10.0,
        )
        decisions: list[str] = []

        def run_request() -> None:
            decisions.append(
                bridge.request_approval(
                    ApprovalRequest(
                        request_id="appr-1",
                        task_id="task-1",
                        tool_name="run_command",
                        arguments={"command": "rm -rf build"},
                    )
                ).decision
            )

        worker = threading.Thread(target=run_request, daemon=True)
        worker.start()
        for _ in range(100):
            if bridge.has_pending("appr-1"):
                break
            threading.Event().wait(0.01)

        sessions.clear()
        self.assertEqual(
            bridge.cancel_pending(
                session_id="sess-1", reason="Session reloaded; approval withdrawn."
            ),
            1,
        )
        worker.join(timeout=5.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(decisions, ["DENY"])


class TestTaskSessionReload(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name)
        self.server = ACPServer(workspace_root=self.workspace)

    def _load(self, server, params):
        return server.handle_request({"id": 22, "method": "session/load", "params": params})

    def _seed(self, sid, records):
        sess = ACPSession(sid, self.workspace / "editor", model="saved-model", history=records,
                          last_task_id="newer_task")
        self.server.sessions[sid] = sess
        self.assertTrue(self.server._persist_session(sess))
        return sess

    def test_task_reload_after_persistence_preserves_session_and_paging(self) -> None:
        records = [{"task_id": "old_task" if i in (0, 39) else f"task_{i}",
                    "prompt": f"prompt {i}", "response": "é😀" * 3000, "status": "completed"}
                   for i in range(40)]
        self._seed("saved_session", records)
        self._seed("other_session", [{"task_id": "foreign", "response": "private"}])
        restarted = ACPServer(workspace_root=self.workspace)
        cancel_pending = Mock(return_value=2)
        restarted.approval_bridge = SimpleNamespace(cancel_pending=cancel_pending)
        connect = sqlite3.connect
        with patch.object(SQLiteHistory, "__getitem__", side_effect=AssertionError("full record read")), \
             patch.object(SQLiteHistory, "has_task", side_effect=AssertionError("session scan")), \
             patch("harness_core.client.acp.sqlite3.connect", wraps=connect) as connections:
            loaded = self._load(restarted, {"taskId": "old_task", "paged": True})
            self.assertIn("result", loaded)
            by_session = self._load(restarted, {"sessionId": "saved_session", "paged": True})
        result = loaded["result"]
        self.assertEqual(result, by_session["result"])
        self.assertEqual(result["sessionId"], "saved_session")
        self.assertEqual(result["cwd"], str(self.workspace / "editor"))
        self.assertEqual(result["model"], "saved-model")
        self.assertNotIn("provider", result)
        self.assertEqual(result["pendingApprovalsWithdrawn"], 2)
        self.assertEqual(result["historyCount"], 40)
        self.assertEqual(result["nextBefore"], 8)
        self.assertEqual([turn["turnIndex"] for turn in result["history"]], list(range(8, 40)))
        self.assertEqual(result["history"][-1]["responseChars"], 6000)
        self.assertLessEqual(len(json.dumps(result).encode("utf-8")), 64 * 1024)
        self.assertEqual(set(restarted.sessions), {"saved_session"})
        self.assertIsNone(restarted._orchestrator)
        self.assertTrue(any(call.kwargs.get("uri") is True and call.args[0].endswith("?mode=ro")
                            for call in connections.call_args_list))
        cancel_pending.assert_called_with(
            session_id="saved_session",
            reason="Session reloaded; pending approval withdrawn and denied.",
        )
        full = self._load(restarted, {"taskId": "old_task", "paged": False})["result"]
        self.assertEqual(full["history"], records)
        self.assertEqual(self._load(restarted, {"sessionId": "saved_session"})["result"], full)

    def test_task_reload_uses_in_memory_records_not_last_task_id(self) -> None:
        sess = ACPSession("memory_session", self.workspace, model="memory-model",
                          history=[{"task_id": "old_task", "response": "saved"}],
                          last_task_id="newer_task")
        self.server.sessions[sess.session_id] = sess
        with patch("harness_core.client.acp.sqlite3.connect", side_effect=AssertionError("DB creation")):
            result = self._load(self.server, {"taskId": "old_task"})["result"]
            missing = self._load(self.server, {"taskId": "newer_task"})
        self.assertEqual(result["sessionId"], sess.session_id)
        self.assertEqual(result["history"], sess.history)
        self.assertEqual(result["model"], "memory-model")
        self.assertIn("Task not found", missing["error"]["message"])
        self.assertFalse(self.server._sessions_dir.exists())

    def test_missing_task_does_not_create_storage_or_session(self) -> None:
        for directory_exists in (False, True):
            with self.subTest(directory_exists=directory_exists):
                if directory_exists:
                    self.server._sessions_dir.mkdir(parents=True)
                with patch("harness_core.client.acp.sqlite3.connect", side_effect=AssertionError("DB creation")):
                    response = self._load(self.server, {"taskId": "missing", "paged": True})
                self.assertIn("Task not found", response["error"]["message"])
                self.assertFalse((self.server._sessions_dir / "history.db").exists())
                self.assertEqual(self.server.sessions, {})
                self.assertIsNone(self.server._orchestrator)

    def test_absent_persisted_task_and_orphan_mapping_fail_explicitly(self) -> None:
        self._seed("owned", [{"task_id": "known", "response": "saved"}])
        restarted = ACPServer(workspace_root=self.workspace)
        missing = self._load(restarted, {"taskId": "missing", "paged": True})
        self.assertIn("Task not found", missing["error"]["message"])
        self.server._session_path("owned").unlink()
        orphan = self._load(restarted, {"taskId": "known", "paged": True})
        self.assertIn("Session not found", orphan["error"]["message"])
        self.assertEqual(restarted.sessions, {})
        self.assertFalse(self.server._session_path("known").exists())

    def test_ambiguous_task_mapping_rejects_persisted_and_memory_matches(self) -> None:
        self._seed("first", [{"task_id": "shared", "response": "first"}])
        self._seed("second", [{"task_id": "shared", "response": "second"}])
        restarted = ACPServer(workspace_root=self.workspace)
        response = self._load(restarted, {"taskId": "shared", "paged": True})
        self.assertEqual(response["error"]["code"], -32602)
        self.assertIn("multiple ACP sessions", response["error"]["message"])
        self.assertEqual(restarted.sessions, {})
        memory = ACPServer(workspace_root=self.workspace / "memory")
        for sid in ("first", "second"):
            memory.sessions[sid] = ACPSession(sid, memory.workspace_root,
                                             history=[{"task_id": "shared"}])
        response = self._load(memory, {"taskId": "shared"})
        self.assertIn("multiple ACP sessions", response["error"]["message"])
        self.assertFalse(memory._sessions_dir.exists())
        restarted.sessions["third"] = ACPSession("third", self.workspace,
                                                history=[{"task_id": "known"}])
        self._seed("owned", [{"task_id": "known"}])
        response = self._load(restarted, {"taskId": "known"})
        self.assertIn("multiple ACP sessions", response["error"]["message"])
        self.assertEqual(set(restarted.sessions), {"third"})

    def test_task_load_strict_parameter_validation(self) -> None:
        invalid = [None, [], False, True, "task", 1, {}, {"paged": True},
                   {"sessionId": "valid", "taskId": "valid"},
                   {"sessionId": None, "taskId": "valid"},
                   {"sessionId": "valid", "taskId": None},
                   {"taskId": "valid", "unknown": True}]
        for name in ("sessionId", "taskId"):
            invalid.extend({name: value} for value in (
                None, True, False, 1, 1.5, [], {}, "", "../task", "task.json", "task/id",
                "task\\id", "task id", "é", "x" * 257, "task' OR 1=1 --",
            ))
            invalid.extend({name: "valid", "paged": value} for value in (None, 0, 1, "true", []))
        for params in invalid:
            with self.subTest(params=params):
                response = self._load(self.server, params)
                self.assertEqual(response["error"]["code"], -32602)
                self.assertEqual(response["id"], 22)
        self.assertEqual(self.server.sessions, {})
        self.assertFalse(self.server._sessions_dir.exists())


if __name__ == "__main__":
    unittest.main()
