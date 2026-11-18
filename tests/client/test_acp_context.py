"""B41 regression: follow-up turns keep tool results and withdraw stale approvals.

ACP rebuilt model context from the last ten turns' prompt/response text only,
so tool outcomes and reasoning disappeared from later turns; pending approvals
were memory-only and a reloaded session left the client waiting forever.
"""

from __future__ import annotations

import threading
import unittest

from harness_core.client.acp import build_conversation_history
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


if __name__ == "__main__":
    unittest.main()
