"""Unit and integration test suite for remote approval webhooks and interactive decision endpoints.

Tests:
1. WebhookConfig initialization and environment variable parsing.
2. Telegram & Discord payload formatters, secret scrubbing, and length truncation.
3. HTTP delivery and error handling (HTTPError, URLError, timeouts).
4. Interactive decision endpoints (/v1/approvals/{id}:decide) with HTML/JSON content negotiation.
5. End-to-end R3 tool approval-resume workflow with dry-run notifications.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness_core.models import PlanNode, TaskState, TaskSubmitRequest
from harness_core.orchestrator import HarnessOrchestrator
from harness_core.server import HarnessAPIApp
from harness_core.webhooks import (
    WebhookConfig,
    WebhookNotifier,
    format_decision_broadcast,
    format_discord_approval,
    format_telegram_approval,
)


class TestWebhookConfig(unittest.TestCase):
    def test_default_config(self) -> None:
        cfg = WebhookConfig()
        self.assertIsNone(cfg.telegram_bot_token)
        self.assertIsNone(cfg.telegram_chat_id)
        self.assertIsNone(cfg.discord_webhook_url)
        self.assertEqual(cfg.public_base_url, "http://127.0.0.1:8420")
        self.assertFalse(cfg.dry_run)
        self.assertEqual(cfg.timeout, 5.0)

    def test_from_env_parsing(self) -> None:
        env = {
            "HARNESS_TELEGRAM_BOT_TOKEN": "123456:ABC-DEF",
            "HARNESS_TELEGRAM_CHAT_ID": "-100123456789",
            "HARNESS_DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/987/xyz",
            "HARNESS_PUBLIC_BASE_URL": "https://harness.mycorp.internal",
            "HARNESS_WEBHOOK_DRY_RUN": "true",
            "HARNESS_WEBHOOK_TIMEOUT": "8.5",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = WebhookConfig.from_env()
            self.assertEqual(cfg.telegram_bot_token, "123456:ABC-DEF")
            self.assertEqual(cfg.telegram_chat_id, "-100123456789")
            self.assertEqual(cfg.discord_webhook_url, "https://discord.com/api/webhooks/987/xyz")
            self.assertEqual(cfg.public_base_url, "https://harness.mycorp.internal")
            self.assertTrue(cfg.dry_run)
            self.assertEqual(cfg.timeout, 8.5)


class TestWebhookFormattersAndScrubbing(unittest.TestCase):
    def setUp(self) -> None:
        self.base_url = "https://ops.example.com:8420"
        self.sample_record = {
            "approval_id": "apr_abc123",
            "task_id": "tsk_xyz789",
            "step_id": "step_deploy",
            "action_type": "git_push",
            "action_params": {
                "branch": "production",
                "remote": "origin",
                "api_key": "sk-proj-testkey1234567890",
                "auth_token": "ghp_1234567890abcdef1234567890abcdef",
            },
            "blast_radius_summary": "Push to main branch with token=ghp_1234567890abcdef1234567890abcdef",
            "risk_class": "R3",
        }

    def test_telegram_formatting_and_secret_scrubbing(self) -> None:
        payload = format_telegram_approval(self.sample_record, self.base_url)
        text = payload["text"]

        # Assert header and fields present
        self.assertIn("🚨 *HEX Approval Request (R3)*", text)
        self.assertIn("tsk_xyz789", text)
        self.assertIn("step_deploy", text)
        self.assertIn("git_push", text)
        self.assertIn("R3", text)

        # Assert secrets redacted
        self.assertIn("[REDACTED_SECRET]", text)
        self.assertNotIn("sk-proj-testkey1234567890", text)
        self.assertNotIn("ghp_1234567890abcdef1234567890abcdef", text)

        # Assert decision links
        approve_link = f"{self.base_url}/v1/approvals/apr_abc123:decide?decision=approve"
        reject_link = f"{self.base_url}/v1/approvals/apr_abc123:decide?decision=reject"
        self.assertIn(approve_link, text)
        self.assertIn(reject_link, text)

        # Assert inline keyboard buttons
        keyboard = payload.get("reply_markup", {}).get("inline_keyboard", [])
        self.assertEqual(len(keyboard), 1)
        self.assertEqual(keyboard[0][0]["text"], "✅ Approve")
        self.assertEqual(keyboard[0][0]["url"], approve_link)
        self.assertEqual(keyboard[0][1]["text"], "❌ Reject")
        self.assertEqual(keyboard[0][1]["url"], reject_link)

    def test_discord_formatting_and_color_coding(self) -> None:
        # Test R3 orange
        r3_payload = format_discord_approval(self.sample_record, self.base_url)
        r3_embed = r3_payload["embeds"][0]
        self.assertEqual(r3_embed["color"], 0xFF9900)
        self.assertIn("git_push", r3_embed["title"])

        # Test R4 red
        r4_rec = dict(self.sample_record)
        r4_rec["risk_class"] = "R4"
        r4_payload = format_discord_approval(r4_rec, self.base_url)
        r4_embed = r4_payload["embeds"][0]
        self.assertEqual(r4_embed["color"], 0xFF0000)

        # Assert secrets redacted in Discord embed
        raw_embed_str = json.dumps(r3_payload)
        self.assertIn("[REDACTED_SECRET]", raw_embed_str)
        self.assertNotIn("sk-proj-testkey1234567890", raw_embed_str)
        self.assertNotIn("ghp_1234567890abcdef1234567890abcdef", raw_embed_str)

    def test_oversized_parameters_truncation(self) -> None:
        huge_record = dict(self.sample_record)
        huge_record["action_params"] = {"large_blob": "A" * 5000}
        tg_payload = format_telegram_approval(huge_record, self.base_url)
        dc_payload = format_discord_approval(huge_record, self.base_url)

        # Verify snippet is capped
        self.assertIn("...", tg_payload["text"])
        dc_params_field = next(f for f in dc_payload["embeds"][0]["fields"] if f["name"] == "Parameters")
        self.assertIn("...", dc_params_field["value"])

    def test_decision_broadcast_formatter(self) -> None:
        broadcast = format_decision_broadcast(self.sample_record, "APPROVED", comment="Looks safe to push")
        tg = broadcast["telegram"]
        dc = broadcast["discord"]

        self.assertIn("APPROVED", tg["text"])
        self.assertIn("Looks safe to push", tg["text"])
        self.assertIn("APPROVED", dc["content"])
        self.assertEqual(dc["embeds"][0]["color"], 0x00FF00)


class TestWebhookHttpDelivery(unittest.TestCase):
    def setUp(self) -> None:
        self.config = WebhookConfig(
            telegram_bot_token="test_bot_token",
            telegram_chat_id="123456",
            discord_webhook_url="https://discord.com/api/webhooks/mock/123",
            public_base_url="http://localhost:8420",
            dry_run=False,
            timeout=3.0,
        )
        self.notifier = WebhookNotifier(self.config)
        self.record = {
            "approval_id": "apr_1",
            "task_id": "t1",
            "step_id": "s1",
            "action_type": "git_push",
            "action_params": {"ref": "main"},
            "blast_radius_summary": "Pushing code",
            "risk_class": "R3",
        }

    @patch("urllib.request.urlopen")
    def test_successful_http_dispatch(self, mock_urlopen: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"ok": true}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        res = self.notifier.send_approval_request(self.record)

        self.assertEqual(mock_urlopen.call_count, 2)
        self.assertEqual(res["telegram"]["status"], 200)
        self.assertEqual(res["discord"]["status"], 200)
        self.assertEqual(len(self.notifier.sent_payloads), 2)

        # Verify URL calls
        req1 = mock_urlopen.call_args_list[0][0][0]
        self.assertEqual(req1.full_url, "https://api.telegram.org/bottest_bot_token/sendMessage")
        self.assertEqual(req1.get_method(), "POST")

        req2 = mock_urlopen.call_args_list[1][0][0]
        self.assertEqual(req2.full_url, "https://discord.com/api/webhooks/mock/123")
        self.assertEqual(req2.get_method(), "POST")

    @patch("urllib.request.urlopen")
    def test_http_error_handling(self, mock_urlopen: MagicMock) -> None:
        err = urllib.error.HTTPError(
            url="https://api.telegram.org/bottest_bot_token/sendMessage",
            code=500,
            msg="Internal Server Error",
            hdrs={},
            fp=io.BytesIO(b'{"description": "fatal"}'),
        )
        mock_urlopen.side_effect = err

        # Should not raise exception
        res = self.notifier.send_approval_request(self.record)
        self.assertEqual(res["telegram"]["status"], 500)
        self.assertEqual(res["discord"]["status"], 500)

    @patch("urllib.request.urlopen")
    def test_timeout_and_network_error_handling(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        res = self.notifier.send_approval_request(self.record)
        self.assertEqual(res["telegram"]["status"], 500)
        self.assertIn("Connection refused", res["telegram"]["error"])


class TestInteractiveDecisionEndpoint(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.notifier = WebhookNotifier(WebhookConfig(dry_run=True))
        self.orch = HarnessOrchestrator(self.temp_dir, webhook_notifier=self.notifier)
        self.app = HarnessAPIApp(self.orch)

        task = self.orch.ledger.submit_task({"goal": "deploy app"})
        self.task_id = task["task_id"]
        self.approval = self.orch.ledger.create_approval(
            task_id=self.task_id,
            attempt_id="att_1",
            step_id="step_deploy",
            action_type="git_push",
            action_params={"remote": "origin", "branch": "main"},
            blast_radius_summary="Deploying to production",
        )
        self.apr_id = self.approval["approval_id"]

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_get_decision_approve_html(self) -> None:
        status, body, ct = self.app.handle_request(
            "GET",
            f"/v1/approvals/{self.apr_id}:decide?decision=approve&comment=All+tests+green",
            headers={"Accept": "text/html"},
        )
        self.assertEqual(status, 200)
        self.assertIn("text/html", ct)
        self.assertIn("badge-approved", str(body))
        self.assertIn("APPROVED", str(body))
        self.assertIn(self.apr_id, str(body))
        self.assertIn(self.task_id, str(body))
        self.assertIn("All tests green", str(body))

        # Check ledger record
        record = self.orch.ledger.list_approvals(task_id=self.task_id)[0]
        self.assertEqual(record["decision"], "APPROVED")
        self.assertEqual(record["comment"], "All tests green")

    def test_get_decision_reject_html(self) -> None:
        status, body, ct = self.app.handle_request(
            "GET",
            f"/v1/approvals/{self.apr_id}:decide?decision=reject&comment=Security+audit+required",
            headers={"Accept": "text/html"},
        )
        self.assertEqual(status, 200)
        self.assertIn("text/html", ct)
        self.assertIn("badge-rejected", str(body))
        self.assertIn("REJECTED", str(body))
        self.assertIn("Security audit required", str(body))

        record = self.orch.ledger.list_approvals(task_id=self.task_id)[0]
        self.assertEqual(record["decision"], "REJECTED")

    def test_post_decision_json(self) -> None:
        status, body, ct = self.app.handle_request(
            "POST",
            f"/v1/approvals/{self.apr_id}:decide",
            body={"decision": "allow", "comment": "approved via api", "auto_resume": False},
            headers={"Accept": "application/json"},
        )
        self.assertEqual(status, 200)
        self.assertIn("application/json", ct)
        self.assertIsInstance(body, dict)
        self.assertEqual(body.get("decision"), "APPROVED")
        self.assertEqual(body.get("comment"), "approved via api")

    def test_nonexistent_approval_returns_404(self) -> None:
        status, body, _ = self.app.handle_request(
            "GET",
            "/v1/approvals/apr_nonexistent:decide?decision=approve",
        )
        self.assertEqual(status, 404)

    def test_invalid_decision_returns_400(self) -> None:
        status, body, _ = self.app.handle_request(
            "POST",
            f"/v1/approvals/{self.apr_id}:decide",
            body={"decision": "maybe"},
        )
        self.assertEqual(status, 400)


class TestEndToEndApprovalWorkflow(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.notifier = WebhookNotifier(WebhookConfig(dry_run=True))
        self.orch = HarnessOrchestrator(self.temp_dir, webhook_notifier=self.notifier)
        self.app = HarnessAPIApp(self.orch)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_r3_action_pauses_dispatches_webhook_and_resumes(self) -> None:
        # Submit a task with an R3 tool requiring approval
        req = TaskSubmitRequest(
            goal="Deploy release to production",
            steps=[
                PlanNode(
                    step_id="step_inspect",
                    title="Inspect repo files",
                    tool_id="list_files",
                    args={},
                    risk_class="R0",
                ).to_dict(),
                PlanNode(
                    step_id="step_deploy",
                    title="Push production tag",
                    tool_id="git_push_remote",
                    args={"remote": "origin", "branch": "release/v1.0"},
                    risk_class="R3",
                    depends_on=["step_inspect"],
                ).to_dict(),
            ],
        )

        task = self.orch.submit_and_run(req)
        # Task should pause in WAITING_APPROVAL
        self.assertEqual(task["status"], TaskState.WAITING_APPROVAL.value)

        # Verify outbound webhooks were recorded in sent_payloads
        self.assertGreaterEqual(len(self.notifier.sent_payloads), 2)
        tg_payload = next(p for p in self.notifier.sent_payloads if p["target"] == "telegram")
        dc_payload = next(p for p in self.notifier.sent_payloads if p["target"] == "discord")

        self.assertIn("🚨 *HEX Approval Request (R3)*", tg_payload["payload"]["text"])
        self.assertIn("git_push_remote", tg_payload["payload"]["text"])
        self.assertIn("release/v1.0", tg_payload["payload"]["text"])

        # Fetch the pending approval record
        approvals = self.orch.ledger.list_approvals(task_id=task["task_id"], decision="PENDING")
        self.assertEqual(len(approvals), 1)
        apr_id = approvals[0]["approval_id"]

        # Simulate user clicking Approve from Telegram/Discord link
        status, html_page, ct = self.app.handle_request(
            "GET",
            f"/v1/approvals/{apr_id}:decide?decision=approve&comment=Approved+by+oncall",
            headers={"Accept": "text/html"},
        )
        self.assertEqual(status, 200)
        self.assertIn("text/html", ct)
        self.assertIn("APPROVED", str(html_page))

        # Verify task resumed automatically and succeeded
        final_task = self.orch.ledger.get_task(task["task_id"])
        self.assertIsNotNone(final_task)
        self.assertEqual(final_task["status"], TaskState.SUCCEEDED.value)

        # Verify decision broadcast was also captured
        tg_bcasts = [p for p in self.notifier.sent_payloads if p["target"] == "telegram_decision"]
        self.assertEqual(len(tg_bcasts), 1)
        self.assertIn("APPROVED", tg_bcasts[0]["payload"]["text"])
        self.assertIn("Approved by oncall", tg_bcasts[0]["payload"]["text"])


if __name__ == "__main__":
    unittest.main()
