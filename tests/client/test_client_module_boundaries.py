"""Contracts for command dispatch, shared terminal rendering, and bundled skills."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness_core.client import cli, tui
from harness_core.client.commands import antigravity, projects
from harness_core.client.terminal import ansi, review


class TestCommandDispatchBoundaries(unittest.TestCase):
    def test_commands_dispatch_before_orchestrator_and_interactive_bootstrap(self):
        cases = (
            (["project"], projects, "run_project_command"),
            (["project", "switch", "workspace-id", "--json"], projects, "run_project_command"),
            (["project", "clean", "--json"], projects, "run_project_command"),
            (["auth", "antigravity", "--status"], antigravity, "run_auth_command"),
            (["quota", "--json"], antigravity, "run_quota_command"),
        )
        for argv, module, handler_name in cases:
            with self.subTest(argv=argv), \
                 patch("harness_core.config_loader.load_hex_environment") as load_env, \
                 patch.object(cli, "HarnessOrchestrator", side_effect=AssertionError("constructed orchestrator")), \
                 patch.object(cli, "_maybe_first_run_setup", side_effect=AssertionError("interactive bootstrap")), \
                 patch.object(module, handler_name, return_value=17) as handler:
                self.assertEqual(cli.main(argv), 17)
                load_env.assert_called_once()
                handler.assert_called_once()
                self.assertEqual(handler.call_args.args[0].cmd, argv[0])
                if argv[0] == "project" and len(argv) > 1:
                    self.assertEqual(handler.call_args.args[0].project_cmd, argv[1])

    def test_manual_auth_preserves_provider_patch_and_safe_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth_file = Path(tmp) / "auth.json"
            output = io.StringIO()
            with patch("harness_core.config_loader.load_hex_environment"), \
                 patch("harness_core.providers.antigravity_provider.AntigravityOAuthManager") as manager_type, \
                 patch.object(cli, "HarnessOrchestrator", side_effect=AssertionError("constructed orchestrator")), \
                 redirect_stdout(output):
                manager = manager_type.return_value
                manager.save_credential.return_value = auth_file
                self.assertEqual(cli.main([
                    "auth", "antigravity", "--token", "manual-access",
                    "--refresh-token", "manual-refresh", "--project-id", "test-project",
                    "--auth-file", str(auth_file),
                ]), 0)
                manager_type.assert_called_once_with(auth_file=str(auth_file))
                credential = manager.save_credential.call_args.args[0]
                self.assertEqual(credential.access_token, "manual-access")
                self.assertEqual(credential.refresh_token, "manual-refresh")
                self.assertEqual(credential.source, "cli_manual")
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["status"], "saved")
            self.assertTrue(payload["has_access_token"])
            self.assertTrue(payload["has_refresh_token"])
            self.assertEqual(payload["project_id"], "test-project")
            self.assertNotIn("manual-access", output.getvalue())
            self.assertNotIn("manual-refresh", output.getvalue())

    def test_quota_success_and_failure_preserve_provider_patch(self):
        for available in (True, False):
            with self.subTest(available=available), \
                 patch("harness_core.config_loader.load_hex_environment"), \
                 patch("harness_core.providers.antigravity_provider.AntigravityOAuthManager") as manager_type, \
                 patch("harness_core.providers.antigravity_provider.AntigravityModelProvider") as provider_type, \
                 patch.object(cli, "HarnessOrchestrator", side_effect=AssertionError("constructed orchestrator")):
                provider = provider_type.return_value
                if available:
                    provider.fetch_quota_status.return_value.to_dict.return_value = {"remaining": 42}
                else:
                    provider.fetch_quota_status.side_effect = RuntimeError("offline quota")
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(cli.main(["quota"]), 0 if available else 1)
                provider_type.assert_called_once_with(oauth_manager=manager_type.return_value)
                self.assertEqual(json.loads(output.getvalue()),
                                 {"remaining": 42} if available else
                                 {"status": "unavailable", "error": "offline quota"})


class TestTerminalModuleBoundaries(unittest.TestCase):
    def test_tui_uses_shared_terminal_definitions(self):
        ansi_names = (
            "ANSI_RESET", "ANSI_BOLD", "ANSI_DIM", "ANSI_ITALIC", "ANSI_RED",
            "ANSI_GREEN", "ANSI_YELLOW", "ANSI_BLUE", "ANSI_MAGENTA", "ANSI_CYAN",
            "ANSI_WHITE", "ANSI_ESCAPE_RE", "fg_rgb", "bg_rgb", "colorize_rgb",
            "strip_ansi", "_pad_visible", "_truncate_visible",
        )
        review_names = (
            "RISK_BADGES", "render_unified_diff", "render_side_by_side_diff",
            "render_blast_radius_tree",
        )
        for module, names in ((ansi, ansi_names), (review, review_names)):
            for name in names:
                with self.subTest(name=name):
                    self.assertIs(getattr(tui, name), getattr(module, name))

    def test_splash_counts_skills_from_shared_resource_locator(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            bundle_dir = workspace / "bundles"
            bundle_dir.mkdir()
            skill_count = sum(len(group[1]) for group in tui.BUILTIN_SKILL_GROUPS) + 3
            for index in range(skill_count):
                skill_dir = bundle_dir / str(index)
                skill_dir.mkdir()
                (skill_dir / "SKILL.md").write_text("# Test skill\n", encoding="utf-8")
            result = MagicMock(returncode=0, stdout="testsha\n")
            with patch("harness_core.resources.bundled_skills_dir", return_value=bundle_dir) as locator, \
                 patch.object(tui.subprocess, "run", return_value=result):
                rendered = tui.render_hex_splash(workspace_root=workspace, enable_color=False)
            locator.assert_called_once_with()
            self.assertIn(f"{skill_count} skills", ansi.strip_ansi(rendered))
