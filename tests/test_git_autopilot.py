"""Unit and integration test suite for Git Autopilot Engine & CLI Workflows (PRD Section 8, Pillar 4)."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness_core.cli import main as cli_main
from harness_core.git_autopilot import (
    GitAutopilot,
    GitAutopilotConfig,
    GitHubTransport,
    MockGitHubTransport,
    SubprocessGitHubTransport,
)


class TestGitAutopilotConfig(unittest.TestCase):
    """Verifies GitAutopilotConfig defaults and environment variable parsing."""

    def test_default_config_values(self):
        cfg = GitAutopilotConfig()
        self.assertEqual(cfg.author_name, "hex-bot")
        self.assertEqual(cfg.author_email, "hex-bot@users.noreply.github.com")
        self.assertEqual(cfg.branch_prefix, "hex/")
        self.assertEqual(cfg.default_base_branch, "main")
        self.assertIsNone(cfg.github_token)
        self.assertIsNone(cfg.repo_slug)
        self.assertFalse(cfg.dry_run)

    def test_from_env_with_custom_values(self):
        env = {
            "HEX_GITHUB_TOKEN": "hex-secret-token",
            "HEX_GIT_AUTHOR_NAME": "custom-bot",
            "HEX_GIT_AUTHOR_EMAIL": "custom@hex.ai",
            "HEX_GIT_BRANCH_PREFIX": "autopilot/",
            "HEX_GITHUB_REPOSITORY": "org/workbench",
            "HEX_GIT_DEFAULT_BASE": "master",
            "HEX_GIT_DRY_RUN": "true",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = GitAutopilotConfig.from_env()
            self.assertEqual(cfg.github_token, "hex-secret-token")
            self.assertEqual(cfg.author_name, "custom-bot")
            self.assertEqual(cfg.author_email, "custom@hex.ai")
            self.assertEqual(cfg.branch_prefix, "autopilot/")
            self.assertEqual(cfg.repo_slug, "org/workbench")
            self.assertEqual(cfg.default_base_branch, "master")
            self.assertTrue(cfg.dry_run)

    def test_from_env_fallback_to_standard_github_env_vars(self):
        env = {
            "GITHUB_TOKEN": "standard-gh-token",
            "GITHUB_REPOSITORY": "octocat/Hello-World",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = GitAutopilotConfig.from_env()
            self.assertEqual(cfg.github_token, "standard-gh-token")
            self.assertEqual(cfg.repo_slug, "octocat/Hello-World")
            self.assertEqual(cfg.branch_prefix, "hex/")
            self.assertFalse(cfg.dry_run)


class TestSlugifyAndBranchNaming(unittest.TestCase):
    """Verifies slug generation and branch naming sanitization."""

    def test_slugify_standard_goals(self):
        self.assertEqual(
            GitAutopilot.slugify("Fix auth token bug"),
            "fix-auth-token-bug",
        )
        self.assertEqual(
            GitAutopilot.slugify("Add 2FA & Multi-Tenant Support!"),
            "add-2fa-multi-tenant-support",
        )

    def test_slugify_truncation(self):
        long_goal = "Refactor database connection pool and ensure zero leaked connections during failover"
        slug = GitAutopilot.slugify(long_goal, max_length=30)
        self.assertLessEqual(len(slug), 30)
        self.assertFalse(slug.endswith("-"))
        self.assertEqual(slug, "refactor-database-connection")

    def test_slugify_fallback(self):
        self.assertEqual(GitAutopilot.slugify(""), "task")
        self.assertEqual(GitAutopilot.slugify("!@#$%^&*()"), "task")

    def test_create_task_branch_naming(self):
        cfg = GitAutopilotConfig(dry_run=True, branch_prefix="hex/")
        auto = GitAutopilot(".", config=cfg)

        b1 = auto.create_task_branch("task_8829_abc", "Add user authentication")
        self.assertEqual(b1, "hex/task-8829abc-add-user-authentication")

        b2 = auto.create_task_branch("t-99", "Update UI colors")
        self.assertEqual(b2, "hex/task-t99-update-ui-colors")

        b3 = auto.create_task_branch("task-1234", "Fix bug")
        self.assertEqual(b3, "hex/task-1234-fix-bug")


class TestConventionalCommits(unittest.TestCase):
    """Verifies conventional commit message formatting and dry-run commit behavior."""

    def test_commit_dry_run_formatting(self):
        cfg = GitAutopilotConfig(dry_run=True)
        auto = GitAutopilot(".", config=cfg)

        sha = auto.commit_task_changes(
            task_id="task_12345678_abcd",
            scope="auth",
            description="Implement JWT validation middleware",
        )
        self.assertTrue(sha.startswith("mock-sha-"))
        self.assertIn("12345678", sha)


class TestPRMarkdownSummaryGeneration(unittest.TestCase):
    """Verifies rich Pull Request summary generation with AST impact and Proof tables."""

    def setUp(self):
        self.cfg = GitAutopilotConfig(dry_run=True)
        self.auto = GitAutopilot(".", config=self.cfg)

    def test_generate_pr_summary_full_bundle(self):
        proof_bundle = {
            "git_diff_sha": "diff-sha-12345",
            "workspace_tree_sha": "tree-sha-67890",
            "runtime_digest": "digest-darwin-arm64",
            "ledger_signature": "ed25519-hmac-sha256:abcdef0123456789abcdef0123456789",
            "verification_results": {
                "passed": True,
                "exit_code": 0,
                "pinned_blast_radius_targets": ["tests/test_billing.py"],
            },
        }
        ast_impact = {
            "modified_symbols": ["compute_tax", "process_order"],
            "hop1_dependents": ["invoice_total", "checkout"],
            "impacted_tests": ["tests/test_billing.py"],
        }

        body = self.auto.generate_pr_summary(
            task_id="tsk_9921",
            goal="Refactor sales tax calculation logic",
            proof_bundle=proof_bundle,
            ast_impact=ast_impact,
        )

        self.assertIn("## 🤖 HEX Automated Pull Request", body)
        self.assertIn("> Refactor sales tax calculation logic", body)
        self.assertIn("### 🔍 Active Graphify AST Impact Matrix", body)
        self.assertIn("| `compute_tax` | Modified Symbol | `invoice_total`, `checkout` | R1 |", body)
        self.assertIn("| `tests/test_billing.py` | Impacted Test | Pinned Verification | R0 |", body)
        self.assertIn("### 🛡️ Proof-Carrying Artifact Verification", body)
        self.assertIn("✅ Passed", body)
        self.assertIn("`diff-sha-12345`", body)
        self.assertIn("`tree-sha-67890`", body)
        self.assertIn("`digest-darwin-arm64`", body)
        self.assertIn("`tests/test_billing.py`", body)
        self.assertIn("Task ID: `tsk_9921`", body)

    def test_generate_pr_summary_zero_blast_radius_fallback(self):
        body = self.auto.generate_pr_summary(
            task_id="tsk_read_only",
            goal="Inspect documentation",
            proof_bundle={
                "git_diff_sha": "diff-empty",
                "workspace_tree_sha": "tree-clean",
                "verification_results": {"passed": True, "exit_code": 0},
            },
            ast_impact=None,
        )
        self.assertIn("Active Graphify AST Impact Matrix", body)
        self.assertIn("zero AST blast radius", body)
        self.assertIn("Proof-Carrying Artifact Verification", body)


class TestLocalGitRepositoryIntegration(unittest.TestCase):
    """Verifies real local Git branch creation, file modification, and conventional commits."""

    def test_real_git_workflow(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            ws = Path(tmp_dir)

            # Initialize local git repository
            subprocess.run(["git", "init", "-b", "main"], cwd=ws, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.name", "Hex Tester"], cwd=ws, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.email", "tester@hex.ai"], cwd=ws, capture_output=True, check=True)

            # Create initial file & commit on main
            readme = ws / "README.md"
            readme.write_text("# Test Repo\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=ws, capture_output=True, check=True)
            subprocess.run(["git", "commit", "-m", "chore: initial commit"], cwd=ws, capture_output=True, check=True)

            # Initialize GitAutopilot with dry_run=False
            cfg = GitAutopilotConfig(
                author_name="hex-worker-bot",
                author_email="bot@hex.local",
                branch_prefix="hex/",
                dry_run=False,
            )
            transport = MockGitHubTransport()
            autopilot = GitAutopilot(workspace_root=ws, config=cfg, transport=transport)

            # 1. Create task branch
            branch = autopilot.create_task_branch("task_42", "Add payment processing")
            self.assertEqual(branch, "hex/task-42-add-payment-processing")

            # Check that git branch is active
            cur_branch = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=ws,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            self.assertEqual(cur_branch, "hex/task-42-add-payment-processing")

            # 2. Modify files and commit
            payment_file = ws / "payment.py"
            payment_file.write_text("def process_payment(val): return True\n", encoding="utf-8")

            commit_sha = autopilot.commit_task_changes(
                task_id="task_42",
                scope="payment",
                description="implement payment processor",
            )
            self.assertEqual(len(commit_sha), 40)

            # Verify commit details in git log
            log_out = subprocess.run(
                ["git", "log", "-1", "--pretty=format:%an <%ae>%n%s%n%b"],
                cwd=ws,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

            self.assertIn("hex-worker-bot <bot@hex.local>", log_out)
            self.assertIn("feat(payment): implement payment processor", log_out)
            self.assertIn("Task-ID: task_42", log_out)
            self.assertIn("Automated-By: HEX Autonomous Workbench", log_out)

            # 3. Create pull request via transport
            pr_res = autopilot.create_pull_request(
                branch=branch,
                title="feat(payment): implement payment processor",
                body="PR body description",
            )
            self.assertTrue(pr_res["ok"])
            self.assertEqual(pr_res["number"], 101)
            self.assertEqual(len(transport.created_prs), 1)
            self.assertEqual(transport.created_prs[0]["head"], branch)


class TestGitHubTransports(unittest.TestCase):
    """Verifies MockGitHubTransport and SubprocessGitHubTransport behavior."""

    def test_mock_transport_operations(self):
        transport = MockGitHubTransport(default_pr_number=202)
        pr = transport.create_pull_request(
            repo_slug="owner/repo",
            title="feat: add feature",
            body="body",
            head_branch="hex/feature",
            base_branch="main",
        )
        self.assertEqual(pr["number"], 202)
        self.assertIn("https://github.com/owner/repo/pull/202", pr["html_url"])
        self.assertEqual(len(transport.created_prs), 1)

        comment = transport.post_issue_comment("owner/repo", 202, "Automated comment")
        self.assertTrue(comment["ok"])
        self.assertEqual(len(transport.comments), 1)

        fetched = transport.get_pull_request("owner/repo", 202)
        self.assertEqual(fetched["number"], 202)
        self.assertEqual(len(transport.fetched_prs), 1)

    def test_subprocess_transport_fallback_when_gh_missing_and_no_token(self):
        with patch("shutil.which", return_value=None):
            transport = SubprocessGitHubTransport(token=None)
            res = transport.create_pull_request(
                repo_slug="owner/repo",
                title="feat: test",
                body="body",
                head_branch="head",
                base_branch="main",
            )
            self.assertFalse(res["ok"])
            self.assertEqual(res["status"], "failed")

    def test_subprocess_transport_with_gh_cli_mock(self):
        with patch("shutil.which", return_value="/usr/local/bin/gh"):
            transport = SubprocessGitHubTransport(token="test-token")
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stdout = "https://github.com/my-org/my-repo/pull/77\n"

            with patch("subprocess.run", return_value=mock_proc) as mock_run:
                pr = transport.create_pull_request(
                    repo_slug="my-org/my-repo",
                    title="feat: test",
                    body="body",
                    head_branch="hex/test",
                    base_branch="main",
                )
                self.assertTrue(pr["ok"])
                self.assertEqual(pr["number"], 77)
                self.assertEqual(pr["transport"], "gh_cli")
                mock_run.assert_called_once()
                cmd = mock_run.call_args[0][0]
                self.assertIn("gh", cmd)
                self.assertIn("create", cmd)
                self.assertIn("--head", cmd)
                self.assertIn("hex/test", cmd)


class TestCLIIntegrationWorkflows(unittest.TestCase):
    """Verifies `hex run` and `hex exec` CLI commands with auto-branching and PR creation."""

    def test_cli_run_dry_run_with_pr(self):
        out = io.StringIO()
        with patch("sys.stdout", out):
            code = cli_main([
                "run",
                "Automated diagnostic task",
                "--mode", "fast_path",
                "--dry-run",
                "--pr",
            ])
        self.assertEqual(code, 0)
        output = json.loads(out.getvalue())
        self.assertEqual(output["status"], "completed")
        self.assertTrue(output["branch"].startswith("hex/task-"))
        self.assertTrue(output["commit"].startswith("mock-sha-"))
        self.assertIn("pull_request", output)
        self.assertEqual(output["pull_request"]["number"], 101)
        self.assertTrue(output["pull_request"]["ok"])

    def test_cli_exec_alias_dry_run_branch_only(self):
        out = io.StringIO()
        with patch("sys.stdout", out):
            code = cli_main([
                "exec",
                "Add simple test helper",
                "--mode", "fast_path",
                "--dry-run",
                "--branch", "custom-feature-branch",
            ])
        self.assertEqual(code, 0)
        output = json.loads(out.getvalue())
        self.assertEqual(output["status"], "completed")
        self.assertEqual(output["branch"], "custom-feature-branch")
        self.assertTrue(output["commit"].startswith("mock-sha-"))
        self.assertNotIn("pull_request", output)

    def test_cli_run_failure_prevents_pr(self):
        mock_task = {
            "task_id": "tsk_fail_1",
            "status": "FAILED",
            "state": "FAILED",
            "error_message": "Deterministic test verification failed",
        }

        out = io.StringIO()
        with patch("harness_core.orchestrator.HarnessOrchestrator.submit_and_run", return_value=mock_task):
            with patch("sys.stdout", out):
                code = cli_main([
                    "run",
                    "Failing task",
                    "--mode", "fast_path",
                    "--dry-run",
                    "--pr",
                ])

        self.assertEqual(code, 1)
        output = json.loads(out.getvalue())
        self.assertEqual(output["status"], "failed")
        self.assertIn("error", output)
        self.assertNotIn("branch", output)
        self.assertNotIn("pull_request", output)


if __name__ == "__main__":
    unittest.main()
