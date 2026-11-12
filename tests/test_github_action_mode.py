"""Comprehensive test suite for GitHub Action Mode & Webhook Bot Responder (PRD Section 8, Pillar 4)."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness_core.cli import main as cli_main
from harness_core.git_autopilot import MockGitHubTransport
from harness_core.github_action import (
    GitHubActionConfig,
    GitHubActionRunner,
    TriggerMatch,
)


class TestGitHubActionConfig(unittest.TestCase):
    """Verifies GitHubActionConfig parsing, defaults, and environment resolution."""

    def test_default_config_values(self):
        cfg = GitHubActionConfig()
        self.assertEqual(cfg.event_name, "")
        self.assertEqual(cfg.event_path, "")
        self.assertIsNone(cfg.token)
        self.assertEqual(cfg.repository, "")
        self.assertIsNone(cfg.ref)
        self.assertIsNone(cfg.sha)
        self.assertEqual(cfg.bot_names, {"@hex-bot", "/hex", "@hex"})
        self.assertFalse(cfg.dry_run)

    def test_from_env_with_standard_github_action_vars(self):
        env = {
            "GITHUB_EVENT_NAME": "issue_comment",
            "GITHUB_EVENT_PATH": "/github/workflow/event.json",
            "GITHUB_TOKEN": "ghp_standardtoken1234567890",
            "GITHUB_REPOSITORY": "octocat/Hello-World",
            "GITHUB_REF": "refs/heads/feature",
            "GITHUB_SHA": "abc123def456",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = GitHubActionConfig.from_env()
            self.assertEqual(cfg.event_name, "issue_comment")
            self.assertEqual(cfg.event_path, "/github/workflow/event.json")
            self.assertEqual(cfg.token, "ghp_standardtoken1234567890")
            self.assertEqual(cfg.repository, "octocat/Hello-World")
            self.assertEqual(cfg.ref, "refs/heads/feature")
            self.assertEqual(cfg.sha, "abc123def456")
            self.assertFalse(cfg.dry_run)

    def test_from_env_hex_overrides_and_dry_run(self):
        env = {
            "HEX_GITHUB_TOKEN": "hex-custom-token",
            "HEX_GITHUB_REPOSITORY": "my-org/my-repo",
            "HEX_BOT_NAMES": "@bot, /bot-fix, @assistant",
            "HEX_DRY_RUN": "true",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = GitHubActionConfig.from_env()
            self.assertEqual(cfg.token, "hex-custom-token")
            self.assertEqual(cfg.repository, "my-org/my-repo")
            self.assertEqual(cfg.bot_names, {"@bot", "/bot-fix", "@assistant"})
            self.assertTrue(cfg.dry_run)


class TestTriggerDetection(unittest.TestCase):
    """Verifies multi-event payload parsing and bot trigger extraction."""

    def setUp(self):
        self.config = GitHubActionConfig(
            repository="owner/repo",
            dry_run=True,
            bot_names={"@hex-bot", "/hex", "@hex"},
        )
        self.runner = GitHubActionRunner(config=self.config)

    def test_issue_comment_mention_on_issue(self):
        payload = {
            "issue": {"number": 101},
            "comment": {
                "body": "Hey team, @hex-bot fix the authentication bug in login.py",
                "user": {"login": "alice"},
            },
        }
        match = self.runner.detect_trigger("issue_comment", payload)
        self.assertIsNotNone(match)
        self.assertTrue(match.matched)
        self.assertEqual(match.trigger_type, "mention")
        self.assertEqual(match.command, "fix")
        self.assertEqual(match.instruction, "the authentication bug in login.py")
        self.assertEqual(match.author, "alice")
        self.assertEqual(match.issue_number, 101)
        self.assertIsNone(match.pr_number)

    def test_issue_comment_slash_command_on_pull_request(self):
        payload = {
            "issue": {
                "number": 202,
                "pull_request": {"url": "https://api.github.com/repos/owner/repo/pulls/202"},
            },
            "comment": {
                "body": "/hex explain why this test is failing",
                "user": {"login": "bob"},
            },
        }
        match = self.runner.detect_trigger("issue_comment", payload)
        self.assertIsNotNone(match)
        self.assertEqual(match.trigger_type, "slash_command")
        self.assertEqual(match.command, "explain")
        self.assertEqual(match.instruction, "why this test is failing")
        self.assertEqual(match.author, "bob")
        self.assertEqual(match.issue_number, 202)
        self.assertEqual(match.pr_number, 202)

    def test_issue_comment_custom_command(self):
        payload = {
            "issue": {"number": 55},
            "comment": {
                "body": "@hex refactor the database query layer",
                "user": {"login": "carol"},
            },
        }
        match = self.runner.detect_trigger("issue_comment", payload)
        self.assertIsNotNone(match)
        self.assertEqual(match.trigger_type, "mention")
        self.assertEqual(match.command, "custom")
        self.assertEqual(match.instruction, "refactor the database query layer")

    def test_issue_comment_without_bot_mention_skipped(self):
        payload = {
            "issue": {"number": 12},
            "comment": {
                "body": "LGTM! Ready to merge when CI finishes.",
                "user": {"login": "reviewer"},
            },
        }
        match = self.runner.detect_trigger("issue_comment", payload)
        self.assertIsNone(match)

    def test_pull_request_opened_with_trigger_in_body(self):
        payload = {
            "action": "opened",
            "pull_request": {
                "number": 42,
                "title": "Add user profile endpoints",
                "body": "Please review this. @hex-bot review security implications",
                "user": {"login": "dave"},
                "head": {"ref": "feature/profiles", "sha": "1234567890abcdef"},
            },
        }
        match = self.runner.detect_trigger("pull_request", payload)
        self.assertIsNotNone(match)
        self.assertEqual(match.trigger_type, "mention")
        self.assertEqual(match.command, "review")
        self.assertEqual(match.instruction, "security implications")
        self.assertEqual(match.pr_number, 42)
        self.assertEqual(match.branch, "feature/profiles")
        self.assertEqual(match.head_sha, "1234567890abcdef")

    def test_pull_request_labeled_event(self):
        payload = {
            "action": "labeled",
            "label": {"name": "hex:fix"},
            "pull_request": {
                "number": 73,
                "title": "Fix memory leak in buffer pool",
                "user": {"login": "maintainer"},
                "head": {"ref": "patch/buffer-leak"},
            },
        }
        match = self.runner.detect_trigger("pull_request", payload)
        self.assertIsNotNone(match)
        self.assertEqual(match.trigger_type, "label")
        self.assertEqual(match.command, "fix")
        self.assertEqual(match.instruction, "Fix memory leak in buffer pool")
        self.assertEqual(match.pr_number, 73)

    def test_issues_labeled_event(self):
        payload = {
            "action": "labeled",
            "label": {"name": "hex"},
            "issue": {
                "number": 88,
                "title": "Broken route handler in /healthz",
                "user": {"login": "reporter"},
            },
        }
        match = self.runner.detect_trigger("issues", payload)
        self.assertIsNotNone(match)
        self.assertEqual(match.trigger_type, "label")
        self.assertEqual(match.command, "run")
        self.assertEqual(match.instruction, "Broken route handler in /healthz")
        self.assertEqual(match.issue_number, 88)

    def test_workflow_dispatch_event(self):
        payload = {
            "inputs": {
                "goal": "Run regression audit suite across all packages",
                "command": "run",
            },
            "sender": {"login": "ci-runner"},
            "ref": "refs/heads/main",
            "sha": "999888777",
        }
        match = self.runner.detect_trigger("workflow_dispatch", payload)
        self.assertIsNotNone(match)
        self.assertEqual(match.trigger_type, "dispatch")
        self.assertEqual(match.command, "run")
        self.assertEqual(match.instruction, "Run regression audit suite across all packages")
        self.assertEqual(match.author, "ci-runner")
        self.assertEqual(match.branch, "refs/heads/main")

    def test_workflow_dispatch_empty_inputs_skipped(self):
        payload = {"inputs": {}}
        match = self.runner.detect_trigger("workflow_dispatch", payload)
        self.assertIsNone(match)

    def test_unhandled_event_returns_none(self):
        payload = {"ref": "refs/heads/main", "commits": []}
        match = self.runner.detect_trigger("push", payload)
        self.assertIsNone(match)

    def test_parse_event_payload_safe_handling(self):
        # Dict pass-through
        self.assertEqual(self.runner.parse_event_payload({"key": "val"}), {"key": "val"})
        # Valid JSON string
        self.assertEqual(self.runner.parse_event_payload('{"num": 42}'), {"num": 42})
        # Valid JSON bytes
        self.assertEqual(self.runner.parse_event_payload(b'{"bytes": true}'), {"bytes": True})
        # Malformed JSON
        self.assertEqual(self.runner.parse_event_payload("not valid json"), {})


class TestActionExecutionLifecycle(unittest.TestCase):
    """Verifies end-to-end task execution, CoW isolation, and rich comment posting."""

    def setUp(self):
        self.mock_transport = MockGitHubTransport()
        self.config = GitHubActionConfig(
            repository="owner/repo",
            dry_run=True,
            bot_names={"@hex-bot", "/hex"},
        )
        self.runner = GitHubActionRunner(config=self.config, transport=self.mock_transport)

    def test_execute_action_successful_issue_comment(self):
        mock_orch = MagicMock()
        mock_orch.default_workspace = Path.cwd()
        mock_orch.submit_and_run.return_value = {
            "task_id": "tsk_gh_001",
            "status": "SUCCEEDED",
            "state": "SUCCEEDED",
        }

        mock_proof = {
            "task_id": "tsk_gh_001",
            "git_diff_sha": "diff_sha_abc123",
            "workspace_tree_sha": "tree_sha_def456",
            "runtime_digest": "digest_789",
            "ledger_signature": "sig_abc123456789012345678901234567890",
            "verification_results": {
                "passed": True,
                "exit_code": 0,
                "pinned_blast_radius_targets": ["tests/test_auth.py"],
            },
            "ast_impact": {
                "modified_symbols": ["auth.login", "auth.verify_token"],
                "hop1_dependents": ["api.endpoints.auth_login"],
                "impacted_tests": ["tests/test_auth.py"],
            },
        }
        mock_orch.ledger.get_proof_bundle.return_value = mock_proof

        payload = {
            "issue": {"number": 10},
            "comment": {
                "body": "@hex-bot fix the session timeout issue in auth.py",
                "user": {"login": "maintainer"},
            },
        }

        res = self.runner.execute_action(mock_orch, "issue_comment", payload)

        self.assertEqual(res["status"], "completed")
        self.assertEqual(res["task_id"], "tsk_gh_001")
        self.assertIsNotNone(res["commit"])

        # Verify comments posted via transport
        self.assertEqual(len(self.mock_transport.comments), 2)

        # 1. First comment: Acknowledgement
        ack = self.mock_transport.comments[0]
        self.assertEqual(ack["issue_number"], 10)
        self.assertIn("HEX Autopilot Activated", ack["body"])
        self.assertIn("@maintainer", ack["body"])
        self.assertIn("session timeout", ack["body"])

        # 2. Second comment: Succeeded with AST table and Proof metrics
        comp = self.mock_transport.comments[1]
        self.assertEqual(comp["issue_number"], 10)
        self.assertIn("HEX Task Succeeded", comp["body"])
        self.assertIn("Active Graphify AST Impact Matrix", comp["body"])
        self.assertIn("auth.login", comp["body"])
        self.assertIn("Proof-Carrying Artifact Verification", comp["body"])
        self.assertIn("diff_sha_abc123", comp["body"])
        self.assertIn("tree_sha_def456", comp["body"])

    def test_execute_action_task_failure_posts_error_comment(self):
        mock_orch = MagicMock()
        mock_orch.default_workspace = Path.cwd()
        mock_orch.submit_and_run.return_value = {
            "task_id": "tsk_gh_fail",
            "status": "FAILED",
            "state": "FAILED",
            "error_message": "SyntaxError: unexpected EOF while parsing",
        }
        mock_orch.ledger.get_proof_bundle.return_value = None

        payload = {
            "issue": {"number": 15},
            "comment": {
                "body": "/hex fix broken syntax",
                "user": {"login": "dev_user"},
            },
        }

        res = self.runner.execute_action(mock_orch, "issue_comment", payload)

        self.assertEqual(res["status"], "failed")
        self.assertEqual(res["task_id"], "tsk_gh_fail")
        self.assertIn("SyntaxError", res["error"])

        # Expect 2 comments: Ack followed by Failure
        self.assertEqual(len(self.mock_transport.comments), 2)
        fail_comment = self.mock_transport.comments[1]
        self.assertIn("HEX Task Execution Failed", fail_comment["body"])
        self.assertIn("SyntaxError", fail_comment["body"])
        self.assertIn("tsk_gh_fail", fail_comment["body"])

    def test_execute_action_skipped_non_bot_comment(self):
        mock_orch = MagicMock()
        payload = {
            "issue": {"number": 20},
            "comment": {
                "body": "Normal review comment without mentioning bot.",
                "user": {"login": "reviewer"},
            },
        }

        res = self.runner.execute_action(mock_orch, "issue_comment", payload)

        self.assertEqual(res["status"], "skipped")
        self.assertIn("No bot trigger detected", res["reason"])
        mock_orch.submit_and_run.assert_not_called()
        self.assertEqual(len(self.mock_transport.comments), 0)

    def test_secret_redaction_in_comments(self):
        mock_orch = MagicMock()
        mock_orch.default_workspace = Path.cwd()
        mock_orch.submit_and_run.return_value = {
            "task_id": "tsk_sec_01",
            "status": "SUCCEEDED",
        }
        mock_orch.ledger.get_proof_bundle.return_value = {
            "git_diff_sha": "diff_sha",
            "workspace_tree_sha": "tree_sha",
            "verification_results": {"passed": True},
        }

        payload = {
            "issue": {"number": 99},
            "comment": {
                "body": "@hex-bot fix bug with token sk-abcdef1234567890",
                "user": {"login": "dev"},
            },
        }

        self.runner.execute_action(mock_orch, "issue_comment", payload)

        for comment in self.mock_transport.comments:
            self.assertNotIn("sk-abcdef1234567890", comment["body"])
            self.assertIn("[REDACTED_SECRET]", comment["body"])


class TestCLIIntegration(unittest.TestCase):
    """Verifies `hex gh-action` CLI invocation with event file and flags."""

    def test_cli_gh_action_skipped_when_no_trigger(self):
        out = io.StringIO()
        with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False) as tf:
            json.dump({
                "comment": {"body": "Just an ordinary comment"},
                "issue": {"number": 1},
            }, tf)
            event_path = tf.name

        try:
            with patch("sys.stdout", out):
                code = cli_main(["gh-action", "--event-path", event_path, "--dry-run"])
            self.assertEqual(code, 0)
            data = json.loads(out.getvalue())
            self.assertEqual(data["status"], "skipped")
        finally:
            Path(event_path).unlink(missing_ok=True)

    def test_cli_gh_action_successful_execution(self):
        out = io.StringIO()
        with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False) as tf:
            json.dump({
                "comment": {
                    "body": "@hex-bot fix calculation logic",
                    "user": {"login": "lead_dev"},
                },
                "issue": {"number": 77},
            }, tf)
            event_path = tf.name

        try:
            mock_task = {
                "task_id": "tsk_cli_gh_01",
                "status": "SUCCEEDED",
                "state": "SUCCEEDED",
            }
            with patch("harness_core.orchestrator.HarnessOrchestrator.submit_and_run", return_value=mock_task):
                with patch("harness_core.db.LedgerStore.get_proof_bundle", return_value={
                    "git_diff_sha": "diff_test",
                    "workspace_tree_sha": "tree_test",
                    "verification_results": {"passed": True},
                }):
                    with patch("sys.stdout", out):
                        code = cli_main(["gh-action", "--event-path", event_path, "--event-name", "issue_comment", "--dry-run"])

            self.assertEqual(code, 0)
            data = json.loads(out.getvalue())
            self.assertEqual(data["status"], "completed")
            self.assertEqual(data["task_id"], "tsk_cli_gh_01")
            self.assertEqual(data["trigger"]["command"], "fix")
        finally:
            Path(event_path).unlink(missing_ok=True)

    def test_cli_gh_action_missing_event_path_handled(self):
        out = io.StringIO()
        with patch("sys.stdout", out):
            code = cli_main(["gh-action", "--dry-run"])
        self.assertEqual(code, 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data["status"], "skipped")


if __name__ == "__main__":
    unittest.main()
