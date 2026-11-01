"""Unit and integration test suite for Discord Bot Gateway and GatewayManager.

Tests:
1. DiscordConfig parsing from environment and operator whitelisting.
2. Unauthorized user, guild, and channel access dropping with security audit logging.
3. Interaction handling: PING/PONG, slash commands (/task, /status, /diff, /cancel, /help).
4. Interactive button callbacks (hex:approve, hex:reject, hex:diff, hex:cancel) with in-place embed updates.
5. Outbound approval request cards with ActionRow button components.
6. GatewayManager lifecycle management, concurrent threads, signal handling, and health reporting.
7. CLI integration for `hex gateway start` and `hex gateway status`.
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness_core.cli import main
from harness_core.gateways.discord_bot import (
    DiscordBotGateway,
    DiscordConfig,
    HttpDiscordTransport,
    MockDiscordTransport,
)
from harness_core.gateways.manager import GatewayManager
from harness_core.gateways.telegram_bot import (
    MockTelegramTransport,
    TelegramBotGateway,
    TelegramConfig,
)
from harness_core.models import PlanNode, TaskState, TaskSubmitRequest
from harness_core.orchestrator import HarnessOrchestrator


class TestDiscordConfigAndWhitelisting(unittest.TestCase):
    def setUp(self) -> None:
        logging.getLogger("harness_core.gateways.discord_bot").disabled = False
        self.tmp_dir = tempfile.mkdtemp(prefix="dc_test_")
        self.orch = HarnessOrchestrator(
            ":memory:",
            default_workspace=Path(self.tmp_dir) / "workspace",
        )

    def tearDown(self) -> None:
        if hasattr(self, "orch") and getattr(self.orch, "_temp_mem_dir", None):
            self.orch._temp_mem_dir.cleanup()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_discord_config_from_env(self) -> None:
        env = {
            "HEX_DISCORD_BOT_TOKEN": "dc_bot_secret_123",
            "HEX_DISCORD_APPLICATION_ID": "app_987654",
            "HEX_DISCORD_PUBLIC_KEY": "pub_abcdef",
            "HEX_DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/123/xyz",
            "HEX_DISCORD_ALLOWED_USERS": "u1, u2, u3",
            "HEX_DISCORD_ALLOWED_GUILDS": "g1, g2",
            "HEX_DISCORD_ALLOWED_CHANNELS": "c1, c2",
            "HEX_DISCORD_DRY_RUN": "true",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = DiscordConfig.from_env()
            self.assertEqual(cfg.bot_token, "dc_bot_secret_123")
            self.assertEqual(cfg.application_id, "app_987654")
            self.assertEqual(cfg.public_key, "pub_abcdef")
            self.assertEqual(cfg.webhook_url, "https://discord.com/api/webhooks/123/xyz")
            self.assertEqual(cfg.allowed_user_ids, {"u1", "u2", "u3"})
            self.assertEqual(cfg.allowed_guild_ids, {"g1", "g2"})
            self.assertEqual(cfg.allowed_channel_ids, {"c1", "c2"})
            self.assertTrue(cfg.dry_run)

    def test_unauthorized_user_interaction_rejected(self) -> None:
        cfg = DiscordConfig(
            bot_token="mock_token",
            allowed_user_ids={"u_operator"},
            allowed_guild_ids={"g_primary"},
            allowed_channel_ids={"c_ops"},
        )
        transport = MockDiscordTransport()
        gw = DiscordBotGateway(self.orch, cfg, transport=transport)

        # Authorized operator
        ok, reason = gw.check_access("u_operator", "g_primary", "c_ops")
        self.assertTrue(ok)
        self.assertEqual(reason, "Authorized")

        # Unauthorized user
        with self.assertLogs("harness_core.gateways.discord_bot", level="WARNING") as cm:
            ok_fail, reason_fail = gw.check_access("u_intruder", "g_primary", "c_ops")
            self.assertFalse(ok_fail)
            self.assertEqual(reason_fail, "Unauthorized operator")
            self.assertTrue(
                any("SECURITY_AUDIT: Dropped unauthorized Discord interaction" in log for log in cm.output)
            )

        # Handle interaction from unauthorized user
        bad_interaction = {
            "type": 2,
            "user": {"id": "u_intruder"},
            "guild_id": "g_primary",
            "channel_id": "c_ops",
            "data": {"name": "help"},
        }
        res = gw.handle_interaction(bad_interaction)
        self.assertEqual(res["type"], 4)
        self.assertEqual(res["data"]["flags"], 64)
        self.assertIn("Unauthorized operator", res["data"]["content"])

    def test_unauthorized_guild_rejected(self) -> None:
        cfg = DiscordConfig(
            bot_token="mock_token",
            allowed_user_ids={"u_operator"},
            allowed_guild_ids={"g_primary"},
        )
        gw = DiscordBotGateway(self.orch, cfg, transport=MockDiscordTransport())

        with self.assertLogs("harness_core.gateways.discord_bot", level="WARNING") as cm:
            ok, reason = gw.check_access("u_operator", "g_rogue", None)
            self.assertFalse(ok)
            self.assertEqual(reason, "Unauthorized guild")
            self.assertTrue(
                any("unauthorized guild" in log for log in cm.output)
            )

    def test_unauthorized_channel_rejected(self) -> None:
        cfg = DiscordConfig(
            bot_token="mock_token",
            allowed_user_ids={"u_operator"},
            allowed_channel_ids={"c_ops"},
        )
        gw = DiscordBotGateway(self.orch, cfg, transport=MockDiscordTransport())

        with self.assertLogs("harness_core.gateways.discord_bot", level="WARNING") as cm:
            ok, reason = gw.check_access("u_operator", None, "c_random")
            self.assertFalse(ok)
            self.assertEqual(reason, "Unauthorized channel")
            self.assertTrue(
                any("unauthorized channel" in log for log in cm.output)
            )


class TestDiscordInteractionsAndCommands(unittest.TestCase):
    def setUp(self) -> None:
        logging.getLogger("harness_core.gateways.discord_bot").disabled = False
        self.tmp_dir = tempfile.mkdtemp(prefix="dc_cmd_test_")
        self.ws_dir = Path(self.tmp_dir) / "workspace"
        self.ws_dir.mkdir(parents=True, exist_ok=True)
        (self.ws_dir / "main.py").write_text("print('initial')\n")
        self.orch = HarnessOrchestrator(
            ":memory:",
            default_workspace=self.ws_dir,
        )
        self.cfg = DiscordConfig(
            bot_token="mock_token",
            allowed_user_ids={"u_operator"},
        )
        self.transport = MockDiscordTransport()
        self.gw = DiscordBotGateway(self.orch, self.cfg, transport=self.transport)

    def tearDown(self) -> None:
        if hasattr(self, "orch") and getattr(self.orch, "_temp_mem_dir", None):
            self.orch._temp_mem_dir.cleanup()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_ping_pong_interaction(self) -> None:
        interaction = {"type": 1, "id": "int_ping", "token": "tok_ping"}
        res = self.gw.handle_interaction(interaction)
        self.assertEqual(res["type"], 1)
        self.assertEqual(len(self.transport.interaction_responses), 1)
        self.assertEqual(self.transport.interaction_responses[0]["response"]["type"], 1)

    def test_help_command_interaction(self) -> None:
        interaction = {
            "type": 2,
            "id": "int_help",
            "token": "tok_help",
            "user": {"id": "u_operator"},
            "data": {"name": "help"},
        }
        res = self.gw.handle_interaction(interaction)
        self.assertEqual(res["type"], 4)
        content = res["data"]["content"]
        self.assertIn("HEX Autonomous AI Workbench", content)
        self.assertIn("/task", content)
        self.assertIn("/status", content)
        self.assertIn("/diff", content)
        self.assertIn("/cancel", content)

    def test_task_command_interaction(self) -> None:
        interaction = {
            "type": 2,
            "id": "int_task",
            "token": "tok_task",
            "user": {"id": "u_operator"},
            "data": {
                "name": "task",
                "options": [{"name": "goal", "value": "Build authentication gateway"}],
            },
        }
        res = self.gw.handle_interaction(interaction)
        self.assertEqual(res["type"], 4)
        self.assertIn("Task Submitted", res["data"]["content"])
        self.assertIn("Build authentication gateway", res["data"]["content"])
        self.assertIn("embeds", res["data"])
        self.assertEqual(len(res["data"]["embeds"]), 1)

        tasks = self.orch.ledger.list_tasks()
        self.assertTrue(any(t["goal"] == "Build authentication gateway" for t in tasks))

    def test_task_command_missing_goal(self) -> None:
        interaction = {
            "type": 2,
            "user": {"id": "u_operator"},
            "data": {"name": "task", "options": []},
        }
        res = self.gw.handle_interaction(interaction)
        self.assertEqual(res["type"], 4)
        self.assertEqual(res["data"]["flags"], 64)
        self.assertIn("Please specify a goal", res["data"]["content"])

    def test_status_command_recent_and_specific(self) -> None:
        # Submit task
        req = TaskSubmitRequest(goal="Status command test", workspace_root=str(self.ws_dir))
        task = self.orch.submit_and_run(req)
        task_id = task["task_id"]

        # 1. /status without options (recent tasks list)
        int_recent = {
            "type": 2,
            "user": {"id": "u_operator"},
            "data": {"name": "status"},
        }
        res_recent = self.gw.handle_interaction(int_recent)
        self.assertEqual(res_recent["type"], 4)
        self.assertIn("Recent HEX Tasks", res_recent["data"]["content"])
        self.assertIn(task_id, res_recent["data"]["content"])

        # 2. /status with task_id option
        int_specific = {
            "type": 2,
            "user": {"id": "u_operator"},
            "data": {
                "name": "status",
                "options": [{"name": "task_id", "value": task_id}],
            },
        }
        res_specific = self.gw.handle_interaction(int_specific)
        self.assertEqual(res_specific["type"], 4)
        self.assertIn(f"Task Status: `{task_id}`", res_specific["data"]["content"])

    def test_diff_command_interaction(self) -> None:
        req = TaskSubmitRequest(
            goal="Diff command test",
            workspace_root=str(self.ws_dir),
            steps=[
                PlanNode(
                    step_id="step_pause",
                    title="Privileged step",
                    tool_id="git_push_remote",
                    args={"remote": "origin"},
                    risk_class="R3",
                ).to_dict(),
            ],
        )
        task = self.orch.submit_and_run(req)
        task_id = task["task_id"]

        attempts = self.orch.ledger.list_attempts(task_id)
        self.assertTrue(len(attempts) > 0)
        attempt_ws = Path(attempts[-1]["workspace_path"])
        (attempt_ws / "feature.py").write_text("def new_feature(): return 42\n", encoding="utf-8")

        int_diff = {
            "type": 2,
            "user": {"id": "u_operator"},
            "data": {
                "name": "diff",
                "options": [{"name": "task_id", "value": task_id}],
            },
        }
        res_diff = self.gw.handle_interaction(int_diff)
        self.assertEqual(res_diff["type"], 4)
        self.assertIn("```diff", res_diff["data"]["content"])
        self.assertIn("+def new_feature(): return 42", res_diff["data"]["content"])

    def test_cancel_command_interaction(self) -> None:
        req = TaskSubmitRequest(
            goal="Task to cancel",
            workspace_root=str(self.ws_dir),
            steps=[
                PlanNode(
                    step_id="step_pause",
                    title="Privileged step",
                    tool_id="git_push_remote",
                    args={"remote": "origin"},
                    risk_class="R3",
                ).to_dict(),
            ],
        )
        task = self.orch.submit_and_run(req)
        task_id = task["task_id"]
        self.assertEqual(task["status"], TaskState.WAITING_APPROVAL.value)

        int_cancel = {
            "type": 2,
            "user": {"id": "u_operator"},
            "data": {
                "name": "cancel",
                "options": [{"name": "task_id", "value": task_id}],
            },
        }
        res_cancel = self.gw.handle_interaction(int_cancel)
        self.assertEqual(res_cancel["type"], 4)
        self.assertIn("canceled and uncommitted CoW workspace rolled back", res_cancel["data"]["content"])

        canceled_task = self.orch.ledger.get_task(task_id)
        self.assertEqual(canceled_task["status"], TaskState.CANCELED.value)



class TestDiscordInteractiveButtons(unittest.TestCase):
    def setUp(self) -> None:
        logging.getLogger("harness_core.gateways.discord_bot").disabled = False
        self.tmp_dir = tempfile.mkdtemp(prefix="dc_btn_test_")
        self.ws_dir = Path(self.tmp_dir) / "workspace"
        self.ws_dir.mkdir(parents=True, exist_ok=True)
        (self.ws_dir / "index.ts").write_text("console.log('init');\n")

        self.orch = HarnessOrchestrator(
            ":memory:",
            default_workspace=self.ws_dir,
        )
        self.cfg = DiscordConfig(
            bot_token="mock_token",
            allowed_user_ids={"u_operator"},
        )
        self.transport = MockDiscordTransport()
        self.gw = DiscordBotGateway(self.orch, self.cfg, transport=self.transport)

    def tearDown(self) -> None:
        if hasattr(self, "orch") and getattr(self.orch, "_temp_mem_dir", None):
            self.orch._temp_mem_dir.cleanup()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _create_pending_approval_task(self) -> tuple[dict, str]:
        req = TaskSubmitRequest(
            goal="Deploy privileged microservice",
            workspace_root=str(self.ws_dir),
            steps=[
                PlanNode(
                    step_id="step_pre",
                    title="Audit files",
                    tool_id="list_files",
                    args={},
                    risk_class="R0",
                ).to_dict(),
                PlanNode(
                    step_id="step_deploy",
                    title="Deploy infrastructure",
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

    def test_send_approval_request_action_row(self) -> None:
        task, apr_id = self._create_pending_approval_task()
        approval_rec = self.orch.ledger.get_approval(apr_id)

        msg = self.gw.send_approval_request(approval_rec, channel_id="c_alerts")
        self.assertIn("components", msg)
        self.assertEqual(len(msg["components"]), 1)
        action_row = msg["components"][0]
        self.assertEqual(action_row["type"], 1)

        buttons = action_row["components"]
        self.assertEqual(len(buttons), 4)
        self.assertEqual(buttons[0]["custom_id"], f"hex:approve:{apr_id}")
        self.assertEqual(buttons[0]["label"], "Approve")
        self.assertEqual(buttons[1]["custom_id"], f"hex:reject:{apr_id}")
        self.assertEqual(buttons[1]["label"], "Reject")
        self.assertEqual(buttons[2]["custom_id"], f"hex:diff:{apr_id}")
        self.assertEqual(buttons[2]["label"], "View Diff")
        self.assertEqual(buttons[3]["custom_id"], f"hex:cancel:{apr_id}")
        self.assertEqual(buttons[3]["label"], "Cancel")

    def test_approve_button_resumes_task(self) -> None:
        task, apr_id = self._create_pending_approval_task()
        task_id = task["task_id"]

        interaction = {
            "type": 3,
            "id": "int_btn_app",
            "token": "tok_btn_app",
            "user": {"id": "u_operator"},
            "data": {"custom_id": f"hex:approve:{apr_id}", "component_type": 2},
            "message": {
                "embeds": [
                    {
                        "title": f"🚨 Approval Required: {apr_id}",
                        "color": 0xFFA500,
                        "fields": [{"name": "Task ID", "value": task_id}],
                    }
                ]
            },
        }

        res = self.gw.handle_interaction(interaction)
        # Type 7 = UPDATE_MESSAGE
        self.assertEqual(res["type"], 7)
        self.assertIn("APPROVED", res["data"]["content"])
        self.assertEqual(res["data"]["components"], [])
        self.assertEqual(res["data"]["embeds"][0]["color"], 0x00FF00)

        # Verify ledger updated
        updated_apr = self.orch.ledger.get_approval(apr_id)
        self.assertEqual(updated_apr["decision"], "APPROVED")
        self.assertEqual(updated_apr["decided_by"], "discord:u_operator")

    def test_reject_button_records_rejection(self) -> None:
        task, apr_id = self._create_pending_approval_task()

        interaction = {
            "type": 3,
            "id": "int_btn_rej",
            "token": "tok_btn_rej",
            "user": {"id": "u_operator"},
            "data": {"custom_id": f"hex:reject:{apr_id}", "component_type": 2},
            "message": {
                "embeds": [
                    {
                        "title": f"🚨 Approval Required: {apr_id}",
                        "color": 0xFFA500,
                    }
                ]
            },
        }

        res = self.gw.handle_interaction(interaction)
        self.assertEqual(res["type"], 7)
        self.assertIn("REJECTED", res["data"]["content"])
        self.assertEqual(res["data"]["components"], [])
        self.assertEqual(res["data"]["embeds"][0]["color"], 0xFF0000)

        updated_apr = self.orch.ledger.get_approval(apr_id)
        self.assertEqual(updated_apr["decision"], "REJECTED")
        self.assertEqual(updated_apr["decided_by"], "discord:u_operator")

    def test_diff_button_returns_ephemeral_diff(self) -> None:
        task, apr_id = self._create_pending_approval_task()

        interaction = {
            "type": 3,
            "id": "int_btn_diff",
            "token": "tok_btn_diff",
            "user": {"id": "u_operator"},
            "data": {"custom_id": f"hex:diff:{apr_id}", "component_type": 2},
        }

        res = self.gw.handle_interaction(interaction)
        # Type 4 = CHANNEL_MESSAGE_WITH_SOURCE
        self.assertEqual(res["type"], 4)
        self.assertEqual(res["data"]["flags"], 64)
        self.assertIn("content", res["data"])

    def test_cancel_button_cancels_task(self) -> None:
        task, apr_id = self._create_pending_approval_task()
        task_id = task["task_id"]

        interaction = {
            "type": 3,
            "id": "int_btn_can",
            "token": "tok_btn_can",
            "user": {"id": "u_operator"},
            "data": {"custom_id": f"hex:cancel:{apr_id}", "component_type": 2},
            "message": {
                "embeds": [{"title": f"🚨 Approval {apr_id}", "color": 0xFFA500}]
            },
        }

        res = self.gw.handle_interaction(interaction)
        self.assertEqual(res["type"], 7)
        self.assertIn("canceled", res["data"]["content"])
        self.assertEqual(res["data"]["components"], [])
        self.assertEqual(res["data"]["embeds"][0]["color"], 0x808080)

        canceled_task = self.orch.ledger.get_task(task_id)
        self.assertEqual(canceled_task["status"], TaskState.CANCELED.value)


class TestGatewayManagerAndSupervision(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="mgr_test_")
        self.orch = HarnessOrchestrator(
            ":memory:",
            default_workspace=Path(self.tmp_dir) / "workspace",
        )
        self.tg_transport = MockTelegramTransport()
        self.tg_gw = TelegramBotGateway(
            self.orch,
            TelegramConfig(bot_token="tg_mock", allowed_user_ids={"u_tg"}),
            transport=self.tg_transport,
        )
        self.dc_transport = MockDiscordTransport()
        self.dc_gw = DiscordBotGateway(
            self.orch,
            DiscordConfig(bot_token="dc_mock", allowed_user_ids={"u_dc"}),
            transport=self.dc_transport,
        )
        self.mgr = GatewayManager(
            self.orch,
            telegram_gateway=self.tg_gw,
            discord_gateway=self.dc_gw,
        )

    def tearDown(self) -> None:
        if self.mgr.is_running:
            self.mgr.stop()
        if hasattr(self, "orch") and getattr(self.orch, "_temp_mem_dir", None):
            self.orch._temp_mem_dir.cleanup()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_gateway_manager_start_and_stop(self) -> None:
        self.assertFalse(self.mgr.is_running)
        self.mgr.start(daemon=True)
        self.assertTrue(self.mgr.is_running)

        stat = self.mgr.get_status()
        self.assertEqual(stat["status"], "running")
        self.assertGreaterEqual(stat["uptime_seconds"], 0.0)

        # Stop manager
        self.mgr.stop(timeout=2.0)
        self.assertFalse(self.mgr.is_running)
        stat_stopped = self.mgr.get_status()
        self.assertEqual(stat_stopped["status"], "stopped")

    def test_gateway_manager_health_status(self) -> None:
        # Submit a task so orchestrator has tasks
        req = TaskSubmitRequest(goal="Manager test task", workspace_root=self.tmp_dir)
        self.orch.submit_and_run(req)

        stat = self.mgr.get_status()
        self.assertIn("status", stat)
        self.assertIn("gateways", stat)
        self.assertEqual(len(stat["gateways"]), 2)
        gateway_names = [g["name"] for g in stat["gateways"]]
        self.assertIn("telegram", gateway_names)
        self.assertIn("discord", gateway_names)

        self.assertIn("orchestrator_tasks", stat)
        self.assertGreaterEqual(stat["orchestrator_tasks"]["total"], 1)

    def test_gateway_manager_poll_once(self) -> None:
        # Enqueue telegram update
        update = {
            "update_id": 501,
            "message": {
                "message_id": 99,
                "from": {"id": "u_tg"},
                "chat": {"id": 100},
                "text": "/help",
            },
        }
        self.tg_transport.enqueue_updates([update])

        counts = self.mgr.poll_once()
        self.assertEqual(counts["telegram"], 1)
        self.assertEqual(self.mgr.update_counts["telegram"], 1)


class TestCLIGatewayIntegration(unittest.TestCase):
    def test_cli_gateway_status(self) -> None:
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            code = main(["--data-dir", ":memory:", "gateway", "status"])
        self.assertEqual(code, 0)
        output = buf.getvalue()
        data = json.loads(output)
        self.assertEqual(data["status"], "stopped")
        self.assertIn("gateways", data)

    def test_cli_gateway_start_once(self) -> None:
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            code = main(["--data-dir", ":memory:", "gateway", "start", "--once"])
        self.assertEqual(code, 0)
        output = buf.getvalue()
        data = json.loads(output)
        self.assertEqual(data["status"], "polled_once")
        self.assertIn("manager", data)


if __name__ == "__main__":
    unittest.main()
