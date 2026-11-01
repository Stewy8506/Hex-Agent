"""Unit and integration test suite for Telegram Bot Gateway.

Tests:
1. Configuration from environment and strict operator whitelisting.
2. Unauthorized user and chat dropping with security audit logging.
3. Slash command routing (/start, /help, /status, /diff, /cancel, /task).
4. Conversational natural language task delegation.
5. Interactive inline keyboard callbacks (approve, reject, diff, cancel).
6. In-place message edits, callback query answering, and CoW workspace diffs.
7. End-to-end task lifecycle with R3 approval gating and resumption.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness_core.gateways.telegram_bot import (
    HttpTelegramTransport,
    MockTelegramTransport,
    TelegramBotGateway,
    TelegramConfig,
)
from harness_core.models import PlanNode, TaskState, TaskSubmitRequest
from harness_core.orchestrator import HarnessOrchestrator


class TestTelegramConfigAndWhitelisting(unittest.TestCase):
    def setUp(self) -> None:
        logging.getLogger("harness_core.gateways.telegram_bot").disabled = False
        self.tmp_dir = tempfile.mkdtemp(prefix="tg_test_")
        self.orch = HarnessOrchestrator(
            ":memory:",
            default_workspace=Path(self.tmp_dir) / "workspace",
        )

    def tearDown(self) -> None:
        if hasattr(self, "orch") and getattr(self.orch, "_temp_mem_dir", None):
            self.orch._temp_mem_dir.cleanup()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_telegram_config_from_env(self) -> None:
        env = {
            "HEX_TELEGRAM_BOT_TOKEN": "123456:BOT_TOKEN_XYZ",
            "HEX_TELEGRAM_ALLOWED_USERS": "1001, 1002, 1003",
            "HEX_TELEGRAM_ALLOWED_CHATS": "2001, 2002",
            "HEX_TELEGRAM_POLL_INTERVAL": "2.5",
            "HEX_TELEGRAM_POLL_TIMEOUT": "45",
            "HEX_TELEGRAM_WEBHOOK_URL": "https://example.com/tg_webhook",
            "HEX_TELEGRAM_BASE_API_URL": "https://custom-telegram-proxy.org",
            "HEX_TELEGRAM_DRY_RUN": "true",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = TelegramConfig.from_env()
            self.assertEqual(cfg.bot_token, "123456:BOT_TOKEN_XYZ")
            self.assertEqual(cfg.allowed_user_ids, {"1001", "1002", "1003"})
            self.assertEqual(cfg.allowed_chat_ids, {"2001", "2002"})
            self.assertEqual(cfg.poll_interval, 2.5)
            self.assertEqual(cfg.poll_timeout, 45)
            self.assertEqual(cfg.webhook_url, "https://example.com/tg_webhook")
            self.assertEqual(cfg.base_api_url, "https://custom-telegram-proxy.org")
            self.assertTrue(cfg.dry_run)

    def test_unauthorized_user_dropped_with_audit_log(self) -> None:
        cfg = TelegramConfig(
            bot_token="mock_token",
            allowed_user_ids={"1001"},
            allowed_chat_ids={"2001"},
        )
        transport = MockTelegramTransport()
        gw = TelegramBotGateway(self.orch, cfg, transport=transport)

        # Authorized operator
        ok, reason = gw.check_access(1001, 2001)
        self.assertTrue(ok)
        self.assertEqual(reason, "Authorized")

        # Unauthorized operator
        with self.assertLogs("harness_core.gateways.telegram_bot", level="WARNING") as cm:
            ok_fail, reason_fail = gw.check_access(9999, 2001)
            self.assertFalse(ok_fail)
            self.assertEqual(reason_fail, "Unauthorized operator")
            self.assertTrue(
                any("SECURITY_AUDIT: Dropped unauthorized Telegram update" in log for log in cm.output)
            )

        # Message from unauthorized user sent via handle_message
        msg = {
            "from": {"id": 9999},
            "chat": {"id": 2001},
            "text": "/task unauthorized task",
        }
        res = gw.handle_message(msg)
        self.assertFalse(res["ok"])
        self.assertIn("Unauthorized", res["text"])

        # Ensure no task was submitted in orchestrator ledger
        tasks = self.orch.ledger.list_tasks()
        self.assertEqual(len(tasks), 0)

        # Verify refusal message was sent via transport
        self.assertEqual(len(transport.sent_messages), 1)
        self.assertIn("Unauthorized operator", transport.sent_messages[0]["text"])

    def test_unauthorized_chat_dropped(self) -> None:
        cfg = TelegramConfig(
            bot_token="mock_token",
            allowed_user_ids={"1001"},
            allowed_chat_ids={"2001"},
        )
        transport = MockTelegramTransport()
        gw = TelegramBotGateway(self.orch, cfg, transport=transport)

        # Authorized user sending in unauthorized group chat
        with self.assertLogs("harness_core.gateways.telegram_bot", level="WARNING") as cm:
            ok, reason = gw.check_access(1001, 9999)
            self.assertFalse(ok)
            self.assertEqual(reason, "Unauthorized chat")
            self.assertTrue(any("chat unauthorized" in log for log in cm.output))


class TestTelegramCommandsAndTaskDelegation(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="tg_cmd_test_")
        self.ws_dir = Path(self.tmp_dir) / "workspace"
        self.ws_dir.mkdir(parents=True, exist_ok=True)
        self.orch = HarnessOrchestrator(
            ":memory:",
            default_workspace=self.ws_dir,
        )
        self.cfg = TelegramConfig(
            bot_token="mock_token",
            allowed_user_ids={"1001"},
            allowed_chat_ids={"2001"},
        )
        self.transport = MockTelegramTransport()
        self.gw = TelegramBotGateway(self.orch, self.cfg, transport=self.transport)

    def tearDown(self) -> None:
        if hasattr(self, "orch") and getattr(self.orch, "_temp_mem_dir", None):
            self.orch._temp_mem_dir.cleanup()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_help_and_start_commands(self) -> None:
        for cmd in ("/start", "/help"):
            msg = {"from": {"id": 1001}, "chat": {"id": 2001}, "text": cmd}
            res = self.gw.handle_message(msg)
            self.assertTrue(res["ok"])
            self.assertEqual(res["action"], "help")
            self.assertIn("HEX Autonomous AI Workbench", res["text"])
            self.assertIn("/task", res["text"])
            self.assertIn("/status", res["text"])
            self.assertIn("/diff", res["text"])
            self.assertIn("/cancel", res["text"])

    def test_task_submission_via_slash_command(self) -> None:
        msg = {
            "from": {"id": 1001},
            "chat": {"id": 2001},
            "text": "/task Build authentication middleware",
        }
        res = self.gw.handle_message(msg)
        self.assertTrue(res["ok"])
        self.assertEqual(res["action"], "task_submitted")
        task_id = res["task_id"]
        self.assertTrue(task_id.startswith("tsk_"))

        # Check ledger record
        task = self.orch.ledger.get_task(task_id)
        self.assertIsNotNone(task)
        self.assertEqual(task["goal"], "Build authentication middleware")

        # Verify confirmation message sent
        sent = self.transport.sent_messages[-1]
        self.assertIn(task_id, sent["text"])
        self.assertIn("Task Submitted", sent["text"])

    def test_conversational_task_submission(self) -> None:
        # Natural language text not beginning with /
        prompt = "Fix race condition in background queue worker"
        msg = {"from": {"id": 1001}, "chat": {"id": 2001}, "text": prompt}
        res = self.gw.handle_message(msg)
        self.assertTrue(res["ok"])
        self.assertEqual(res["action"], "task_submitted")
        task_id = res["task_id"]

        task = self.orch.ledger.get_task(task_id)
        self.assertIsNotNone(task)
        self.assertEqual(task["goal"], prompt)

    def test_status_command(self) -> None:
        # Test /status with no tasks
        msg_empty = {"from": {"id": 1001}, "chat": {"id": 2001}, "text": "/status"}
        res_empty = self.gw.handle_message(msg_empty)
        self.assertTrue(res_empty["ok"])
        self.assertIn("No recent tasks found", res_empty["text"])

        # Submit task
        self.gw.handle_message({
            "from": {"id": 1001},
            "chat": {"id": 2001},
            "text": "/task Refactor logger",
        })
        tasks = self.orch.ledger.list_tasks(limit=1)
        task_id = tasks[0]["task_id"]

        # Test /status (list mode)
        res_list = self.gw.handle_message({
            "from": {"id": 1001},
            "chat": {"id": 2001},
            "text": "/status",
        })
        self.assertTrue(res_list["ok"])
        self.assertIn(task_id, res_list["text"])
        self.assertIn("Refactor logger", res_list["text"])

        # Test /status <task_id> (specific task)
        res_specific = self.gw.handle_message({
            "from": {"id": 1001},
            "chat": {"id": 2001},
            "text": f"/status {task_id}",
        })
        self.assertTrue(res_specific["ok"])
        self.assertIn(task_id, res_specific["text"])
        self.assertIn("Task Status", res_specific["text"])

    def test_diff_command(self) -> None:
        # Submit a task with steps that modify workspace
        test_file = self.ws_dir / "app.py"
        test_file.write_text("print('v1')\n", encoding="utf-8")

        req = TaskSubmitRequest(
            goal="Update app to v2",
            workspace_root=str(self.ws_dir),
            steps=[
                PlanNode(
                    step_id="step_1",
                    title="Update file",
                    tool_id="list_files",
                    args={},
                    risk_class="R0",
                ).to_dict(),
            ],
        )
        task = self.orch.submit_and_run(req)
        task_id = task["task_id"]

        # Modify file in attempt workspace if available
        attempts = self.orch.ledger.list_attempts(task_id)
        if attempts:
            attempt_ws = Path(attempts[-1]["workspace_path"])
            (attempt_ws / "app.py").write_text("print('v2 modified')\n", encoding="utf-8")

        res = self.gw.handle_message({
            "from": {"id": 1001},
            "chat": {"id": 2001},
            "text": f"/diff {task_id}",
        })
        self.assertTrue(res["ok"])
        self.assertEqual(res["action"], "diff")
        self.assertIn(task_id, res["text"])

    def test_cancel_command(self) -> None:
        # Submit task with pending approval so it's active
        req = TaskSubmitRequest(
            goal="Long running task to cancel",
            workspace_root=str(self.ws_dir),
            steps=[
                PlanNode(
                    step_id="step_pause",
                    title="Dangerous step",
                    tool_id="git_push_remote",
                    args={"remote": "origin"},
                    risk_class="R3",
                ).to_dict(),
            ],
        )
        task = self.orch.submit_and_run(req)
        task_id = task["task_id"]
        self.assertEqual(task["status"], TaskState.WAITING_APPROVAL.value)

        # Cancel via slash command
        res = self.gw.handle_message({
            "from": {"id": 1001},
            "chat": {"id": 2001},
            "text": f"/cancel {task_id}",
        })
        self.assertTrue(res["ok"])
        self.assertEqual(res["action"], "cancel")
        self.assertIn("Task Canceled", res["text"])

        # Verify ledger status
        updated_task = self.orch.ledger.get_task(task_id)
        self.assertEqual(updated_task["status"], TaskState.CANCELED.value)

    def test_polling_update_processing(self) -> None:
        # Enqueue updates in mock transport
        self.transport.enqueue_updates([
            {
                "update_id": 1,
                "message": {
                    "from": {"id": 1001},
                    "chat": {"id": 2001},
                    "text": "/help",
                },
            },
            {
                "update_id": 2,
                "message": {
                    "from": {"id": 1001},
                    "chat": {"id": 2001},
                    "text": "/task Auto test polling",
                },
            },
        ])

        count = self.gw.poll_once()
        self.assertEqual(count, 2)
        self.assertEqual(self.gw._last_update_id, 2)
        self.assertGreaterEqual(len(self.transport.sent_messages), 2)


class TestTelegramInteractiveCallbacks(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="tg_cb_test_")
        self.ws_dir = Path(self.tmp_dir) / "workspace"
        self.ws_dir.mkdir(parents=True, exist_ok=True)
        self.orch = HarnessOrchestrator(
            ":memory:",
            default_workspace=self.ws_dir,
        )
        self.cfg = TelegramConfig(
            bot_token="mock_token",
            allowed_user_ids={"1001"},
            allowed_chat_ids={"2001"},
        )
        self.transport = MockTelegramTransport()
        self.gw = TelegramBotGateway(self.orch, self.cfg, transport=self.transport)

    def tearDown(self) -> None:
        if hasattr(self, "orch") and getattr(self.orch, "_temp_mem_dir", None):
            self.orch._temp_mem_dir.cleanup()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _create_pending_approval_task(self) -> tuple[dict, str]:
        req = TaskSubmitRequest(
            goal="Execute gated production deployment",
            workspace_root=str(self.ws_dir),
            steps=[
                PlanNode(
                    step_id="step_pre",
                    title="Pre check",
                    tool_id="list_files",
                    args={},
                    risk_class="R0",
                ).to_dict(),
                PlanNode(
                    step_id="step_deploy",
                    title="Deploy to prod",
                    tool_id="git_push_remote",
                    args={"remote": "origin", "branch": "prod"},
                    risk_class="R3",
                    depends_on=["step_pre"],
                ).to_dict(),
            ],
        )
        task = self.orch.submit_and_run(req)
        self.assertEqual(task["status"], TaskState.WAITING_APPROVAL.value)
        approvals = self.orch.ledger.list_approvals(task_id=task["task_id"], decision="PENDING")
        self.assertEqual(len(approvals), 1)
        return task, approvals[0]["approval_id"]

    def test_send_approval_request_formats_inline_keyboard(self) -> None:
        task, apr_id = self._create_pending_approval_task()
        approval_rec = self.orch.ledger.get_approval(apr_id)

        res = self.gw.send_approval_request(approval_rec, chat_id=2001)
        self.assertTrue(res["ok"])

        sent = self.transport.sent_messages[-1]
        self.assertEqual(sent["chat_id"], 2001)
        self.assertIn("🚨 *HEX Approval Request (R3)*", sent["text"])
        self.assertIn(task["task_id"], sent["text"])
        self.assertIn("git_push_remote", sent["text"])

        # Check inline keyboard layout
        inline_kb = sent["reply_markup"]["inline_keyboard"]
        self.assertEqual(len(inline_kb), 2)
        row0 = inline_kb[0]
        row1 = inline_kb[1]
        self.assertEqual(row0[0]["text"], "✅ Approve")
        self.assertEqual(row0[0]["callback_data"], f"approve:{apr_id}")
        self.assertEqual(row0[1]["text"], "❌ Reject")
        self.assertEqual(row0[1]["callback_data"], f"reject:{apr_id}")
        self.assertEqual(row1[0]["text"], "🔍 View Diff")
        self.assertEqual(row1[0]["callback_data"], f"diff:{apr_id}")
        self.assertEqual(row1[1]["text"], "⏹ Cancel Task")
        self.assertEqual(row1[1]["callback_data"], f"cancel:{apr_id}")

    def test_approval_callback_approve_resumes_task(self) -> None:
        task, apr_id = self._create_pending_approval_task()
        approval_rec = self.orch.ledger.get_approval(apr_id)
        self.gw.send_approval_request(approval_rec, chat_id=2001)

        cb_query = {
            "id": "cb_1001",
            "from": {"id": 1001},
            "message": {
                "message_id": 555,
                "chat": {"id": 2001},
                "text": "🚨 HEX Approval Request (R3)",
            },
            "data": f"approve:{apr_id}",
        }
        res = self.gw.handle_callback_query(cb_query)
        self.assertTrue(res["ok"])
        self.assertEqual(res["action"], "approve")

        # 1. Verify ledger decision updated to APPROVED
        updated_apr = self.orch.ledger.get_approval(apr_id)
        self.assertEqual(updated_apr["decision"], "APPROVED")
        self.assertEqual(updated_apr["decided_by"], "tg:1001")

        # 2. Verify orch resumed task and task succeeded
        final_task = self.orch.ledger.get_task(task["task_id"])
        self.assertEqual(final_task["status"], TaskState.SUCCEEDED.value)

        # 3. Verify message was edited in-place with green confirmation badge
        self.assertEqual(len(self.transport.edited_messages), 1)
        edited = self.transport.edited_messages[0]
        self.assertEqual(edited["message_id"], 555)
        self.assertIn("✅ *APPROVED by @1001*", edited["text"])
        self.assertEqual(edited["reply_markup"], {"inline_keyboard": []})

        # 4. Verify answerCallbackQuery was called
        self.assertEqual(len(self.transport.answered_callbacks), 1)
        ans = self.transport.answered_callbacks[0]
        self.assertEqual(ans["callback_query_id"], "cb_1001")
        self.assertIn("Approval recorded! Task resumed.", ans["text"])

    def test_approval_callback_reject(self) -> None:
        task, apr_id = self._create_pending_approval_task()

        cb_query = {
            "id": "cb_1002",
            "from": {"id": 1001},
            "message": {
                "message_id": 556,
                "chat": {"id": 2001},
                "text": "🚨 HEX Approval Request (R3)",
            },
            "data": f"reject:{apr_id}",
        }
        res = self.gw.handle_callback_query(cb_query)
        self.assertTrue(res["ok"])
        self.assertEqual(res["action"], "reject")

        # 1. Verify ledger decision updated to REJECTED
        updated_apr = self.orch.ledger.get_approval(apr_id)
        self.assertEqual(updated_apr["decision"], "REJECTED")
        self.assertEqual(updated_apr["decided_by"], "tg:1001")

        # 2. Verify message was edited in-place with red rejection badge
        self.assertEqual(len(self.transport.edited_messages), 1)
        edited = self.transport.edited_messages[0]
        self.assertEqual(edited["message_id"], 556)
        self.assertIn("❌ *REJECTED by @1001*", edited["text"])

        # 3. Verify answerCallbackQuery was called
        self.assertEqual(len(self.transport.answered_callbacks), 1)
        ans = self.transport.answered_callbacks[0]
        self.assertEqual(ans["callback_query_id"], "cb_1002")
        self.assertIn("Action rejected.", ans["text"])

    def test_approval_callback_diff(self) -> None:
        task, apr_id = self._create_pending_approval_task()

        cb_query = {
            "id": "cb_1003",
            "from": {"id": 1001},
            "message": {
                "message_id": 557,
                "chat": {"id": 2001},
                "text": "🚨 HEX Approval Request (R3)",
            },
            "data": f"diff:{apr_id}",
        }
        res = self.gw.handle_callback_query(cb_query)
        self.assertTrue(res["ok"])
        self.assertEqual(res["action"], "diff")

        # Verify a new message with diff preview was sent
        self.assertEqual(len(self.transport.sent_messages), 1)
        sent = self.transport.sent_messages[0]
        self.assertIn("Workspace Diff", sent["text"])

        # Verify callback acknowledged
        self.assertEqual(len(self.transport.answered_callbacks), 1)
        self.assertEqual(self.transport.answered_callbacks[0]["callback_query_id"], "cb_1003")

    def test_approval_callback_cancel(self) -> None:
        task, apr_id = self._create_pending_approval_task()

        cb_query = {
            "id": "cb_1004",
            "from": {"id": 1001},
            "message": {
                "message_id": 558,
                "chat": {"id": 2001},
                "text": "🚨 HEX Approval Request (R3)",
            },
            "data": f"cancel:{apr_id}",
        }
        res = self.gw.handle_callback_query(cb_query)
        self.assertTrue(res["ok"])
        self.assertEqual(res["action"], "cancel")

        # Verify task is canceled in ledger
        updated_task = self.orch.ledger.get_task(task["task_id"])
        self.assertEqual(updated_task["status"], TaskState.CANCELED.value)

        # Verify message edited in-place with cancellation badge
        self.assertEqual(len(self.transport.edited_messages), 1)
        edited = self.transport.edited_messages[0]
        self.assertEqual(edited["message_id"], 558)
        self.assertIn("⏹ *CANCELED by @1001*", edited["text"])

        # Verify callback acknowledged
        self.assertEqual(len(self.transport.answered_callbacks), 1)
        self.assertEqual(self.transport.answered_callbacks[0]["callback_query_id"], "cb_1004")

    def test_unauthorized_callback_rejected(self) -> None:
        task, apr_id = self._create_pending_approval_task()

        cb_query = {
            "id": "cb_attacker",
            "from": {"id": 9999},  # unauthorized user
            "message": {
                "message_id": 559,
                "chat": {"id": 2001},
                "text": "🚨 HEX Approval Request (R3)",
            },
            "data": f"approve:{apr_id}",
        }
        res = self.gw.handle_callback_query(cb_query)
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "unauthorized")

        # Verify approval is still PENDING
        apr = self.orch.ledger.get_approval(apr_id)
        self.assertEqual(apr["decision"], "PENDING")

        # Verify alert answered to user
        self.assertEqual(len(self.transport.answered_callbacks), 1)
        ans = self.transport.answered_callbacks[0]
        self.assertEqual(ans["callback_query_id"], "cb_attacker")
        self.assertTrue(ans["show_alert"])


class TestTelegramE2EWorkflow(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="tg_e2e_test_")
        self.ws_dir = Path(self.tmp_dir) / "workspace"
        self.ws_dir.mkdir(parents=True, exist_ok=True)
        self.orch = HarnessOrchestrator(
            ":memory:",
            default_workspace=self.ws_dir,
        )
        self.cfg = TelegramConfig(
            bot_token="mock_token",
            allowed_user_ids={"1001"},
            allowed_chat_ids={"2001"},
        )
        self.transport = MockTelegramTransport()
        self.gw = TelegramBotGateway(self.orch, self.cfg, transport=self.transport)

    def tearDown(self) -> None:
        if hasattr(self, "orch") and getattr(self.orch, "_temp_mem_dir", None):
            self.orch._temp_mem_dir.cleanup()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_e2e_telegram_task_lifecycle(self) -> None:
        # Step 1: Operator submits prompt through Telegram message with R3 gated step
        user_message = {
            "from": {"id": 1001},
            "chat": {"id": 2001},
            "text": "Deploy production release",
            "steps": [
                PlanNode(
                    step_id="step_pre",
                    title="Pre check",
                    tool_id="list_files",
                    args={},
                    risk_class="R0",
                ).to_dict(),
                PlanNode(
                    step_id="step_deploy",
                    title="Deploy to prod",
                    tool_id="git_push_remote",
                    args={"remote": "origin", "branch": "prod"},
                    risk_class="R3",
                    depends_on=["step_pre"],
                ).to_dict(),
            ],
        }
        sub_res = self.gw.handle_message(user_message)
        self.assertTrue(sub_res["ok"])
        task_id = sub_res["task_id"]

        # Step 2: Task created and paused in WAITING_APPROVAL
        task = self.orch.ledger.get_task(task_id)
        self.assertIsNotNone(task)
        self.assertEqual(task["status"], TaskState.WAITING_APPROVAL.value)

        # Step 3: Approval request card with inline buttons was dispatched automatically
        approvals = self.orch.ledger.list_approvals(task_id=task_id, decision="PENDING")
        self.assertEqual(len(approvals), 1)
        apr_id = approvals[0]["approval_id"]

        approval_card = next(
            m for m in self.transport.sent_messages if "🚨 *HEX Approval Request" in m.get("text", "")
        )
        card_msg_id = approval_card["message_id"]
        self.assertEqual(approval_card["chat_id"], 2001)

        # Step 4: Operator clicks [✅ Approve]
        approve_cb = {
            "id": "cb_flow_1",
            "from": {"id": 1001},
            "message": {
                "message_id": card_msg_id,
                "chat": {"id": 2001},
                "text": approval_card["text"],
            },
            "data": f"approve:{apr_id}",
        }
        cb_res = self.gw.handle_callback_query(approve_cb)
        self.assertTrue(cb_res["ok"])

        # Step 5: Verify approval record updated, message edited in-place, task completed, and broadcast sent
        updated_apr = self.orch.ledger.get_approval(apr_id)
        self.assertEqual(updated_apr["decision"], "APPROVED")
        self.assertEqual(updated_apr["decided_by"], "tg:1001")

        final_task = self.orch.ledger.get_task(task_id)
        self.assertEqual(final_task["status"], TaskState.SUCCEEDED.value)

        edited_card = self.transport.edited_messages[-1]
        self.assertEqual(edited_card["message_id"], card_msg_id)
        self.assertIn("APPROVED", edited_card["text"])
        self.assertEqual(len(edited_card["reply_markup"]["inline_keyboard"]), 0)

        # Completion card was broadcast
        completion_card = next(
            m for m in self.transport.sent_messages if "🏁 *Task Completed Successfully" in m.get("text", "")
        )
        self.assertIn(task_id, completion_card["text"])


if __name__ == "__main__":
    unittest.main()
