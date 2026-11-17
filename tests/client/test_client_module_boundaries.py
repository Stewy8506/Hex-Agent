"""Contracts for command dispatch, shared terminal rendering, and bundled skills."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness_core.client import cli, tui
from harness_core.client.commands import antigravity, projects
from harness_core.client.terminal import ansi, review


class TestFreshInterpreterCliBoundaries(unittest.TestCase):
    def test_lightweight_routes_do_not_import_execution_stack(self):
        script = r'''
import io
import os
import sys
from pathlib import Path
from unittest.mock import patch

forbidden = (
    "harness_core.core.orchestrator", "harness_core.core.models",
    "harness_core.core.router", "harness_core.gateway", "harness_core.graphify",
    "harness_core.skills.executor", "harness_core.client.server",
    "harness_core.client.tui", "harness_core.storage.project_registry", "networkx",
)
attempts = []
class ImportBoundary:
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + ".") for name in forbidden):
            attempts.append(fullname)
            raise AssertionError("execution import: " + fullname)
sys.meta_path.insert(0, ImportBoundary())
from harness_core.client import cli
scenario = sys.argv[1]
if scenario == "import":
    assert cli.build_parser().prog == "hex"
elif scenario in ("help", "version"):
    try:
        cli.main(["--" + scenario])
    except SystemExit as exc:
        assert exc.code == 0
    else:
        raise AssertionError("argparse did not exit")
elif scenario == "native":
    from harness_core.client import tui_launcher
    with patch.object(tui_launcher, "find_hex_tui_binary", return_value=Path("/native/hex-tui")), \
         patch.object(tui_launcher.subprocess, "run") as run:
        run.return_value.returncode = 23
        assert cli.main(["tui", "--check", "--workspace", os.getcwd()]) == 23
        assert "--check" in run.call_args.args[0]
elif scenario == "acp-stdio":
    request = '{"jsonrpc":"2.0","id":1,"method":"initialize"}\n'
    with patch.object(sys, "stdin", io.StringIO(request)):
        assert cli.main(["acp"]) == 0
else:
    assert cli.main(["acp", "--" + scenario.removeprefix("acp-")]) == 0
assert not attempts, attempts
assert not any(name in sys.modules for name in forbidden)
'''
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            for scenario in ("import", "help", "version", "native", "acp-check", "acp-setup", "acp-stdio"):
                with self.subTest(scenario=scenario):
                    env = os.environ.copy()
                    env.pop("HEX_FORCE_INTERACTIVE", None)
                    env.update(HOME=str(home), HEX_TESTING="1", HEX_DATA_DIR=str(home / "data"))
                    result = subprocess.run(
                        [sys.executable, "-c", script, scenario], cwd=home, env=env,
                        capture_output=True, text=True, timeout=60,
                    )
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    if scenario == "acp-stdio":
                        response = json.loads(result.stdout)
                        self.assertEqual(response["id"], 1)
                        self.assertIn("result", response)
                        self.assertEqual(result.stderr, "")


class TestAcpCliBoundaries(unittest.TestCase):
    def test_stdio_constructs_only_the_acp_owned_backend_lazily(self):
        from harness_core.client.acp import ACPServer

        with tempfile.TemporaryDirectory() as tmp, \
             patch("harness_core.config_loader.load_hex_environment"), \
             patch("harness_core.storage.project_registry.ProjectRegistry", side_effect=AssertionError("registry construction")), \
             patch.object(cli, "_maybe_first_run_setup", side_effect=AssertionError("interactive setup")), \
             patch("harness_core.core.orchestrator.HarnessOrchestrator") as constructor, \
             patch.object(tui, "render_hex_splash", return_value="splash"), \
             patch.object(tui, "_resolve_provider_label", return_value="fixture"):
            workspace = Path(tmp).resolve()
            constructor.return_value.gateway.tools = []

            def run_stdio(server):
                self.assertEqual(server.workspace_root, workspace)
                constructor.assert_not_called()
                response = server.handle_request({"id": 1, "method": "initialize"})
                self.assertIn("result", response)
                constructor.assert_not_called()
                for req_id in (2, 3):
                    response = server.handle_request({"id": req_id, "method": "session/new"})
                    self.assertIn("result", response)
                constructor.assert_called_once_with(
                    data_dir=workspace / ".hex" / "data", default_workspace=workspace,
                )

            with patch.object(ACPServer, "run_stdio", autospec=True, side_effect=run_stdio) as run:
                # These never configured the effective ACP backend, even before
                # the CLI's unused orchestrator was removed.
                self.assertEqual(cli.main([
                    "--data-dir", str(workspace / "unused"), "--model", "unused-model",
                    "--provider", "local", "acp", "--workspace", str(workspace),
                ]), 0)
                run.assert_called_once()
            self.assertFalse((workspace / "unused").exists())

    def test_acp_preserves_environment_workspace_and_check_setup_precedence(self):
        from harness_core.client.acp import ACPServer

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            home, project, nested, explicit = (root / name for name in ("home", "project", "project/nested", "explicit"))
            for directory in (home, project, nested, explicit):
                directory.mkdir(parents=True, exist_ok=True)
            for directory, token in ((home, "global"), (project, "enclosing"), (nested, "cwd"), (explicit, "explicit")):
                (directory / ".hex").mkdir()
                (directory / ".hex" / "credentials.env").write_text(f"HEX_PHASE1_TOKEN={token}\n", encoding="utf-8")
            # A nested cwd without a project marker still supplies the initial
            # cwd environment; the enclosing project supplies the second load.
            (nested / ".hex" / "credentials.env").unlink()
            (nested / ".hex").rmdir()
            (home / ".hex" / "config.yaml").write_text(
                'model:\n  cloud_model_id: global-model\n  cloud_base_url: https://configured.invalid\n',
                encoding="utf-8",
            )
            (project / ".hex" / "config.yaml").write_text(
                'model:\n  cloud_model_id: stale-project-model\n', encoding="utf-8",
            )
            (explicit / ".hex" / "config.yaml").write_text(
                'model:\n  project_override: true\n  cloud_model_id: explicit-model\n', encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True), \
                 patch.object(Path, "home", return_value=home), \
                 patch.object(Path, "cwd", return_value=nested), \
                 patch("harness_core.storage.project_registry.ProjectRegistry", side_effect=AssertionError("registry construction")), \
                 patch("harness_core.core.orchestrator.HarnessOrchestrator", side_effect=AssertionError("backend construction")), \
                 patch.object(ACPServer, "run_stdio", autospec=True) as run:
                for workspace in (None, explicit):
                    with self.subTest(workspace=workspace):
                        argv = ["--base-url", "https://cli.invalid/", "acp"]
                        if workspace:
                            argv += ["--workspace", str(workspace)]
                        self.assertEqual(cli.main(argv), 0)
                        self.assertEqual(run.call_args.args[0].workspace_root, workspace or nested)
                        self.assertEqual(os.environ["HEX_PHASE1_TOKEN"], "explicit" if workspace else "enclosing")
                        self.assertEqual(os.environ["HEX_CLOUD_MODEL_ID"], "explicit-model" if workspace else "global-model")
                        self.assertEqual(os.environ["HEX_CLOUD_BASE_URL"], "https://cli.invalid")
                run.reset_mock()
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(cli.main(["acp", "--check", "--setup"]), 0)
                self.assertIn("ACP Health Check", output.getvalue())
                self.assertIn(str(nested), output.getvalue())
                self.assertNotIn("Integration Snippets", output.getvalue())
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(["acp", "--setup"]), 0)
                run.assert_not_called()

    def test_acp_no_enclosing_project_keeps_checkout_environment_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            with patch.object(Path, "cwd", return_value=workspace), \
                 patch("harness_core.config_loader.load_hex_environment") as load_env, \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["acp", "--check"]), 0)
            from tests import REPO_ROOT
            self.assertEqual(load_env.call_args.kwargs, {"workspace_root": REPO_ROOT, "override": True})


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
                 patch("harness_core.core.orchestrator.HarnessOrchestrator", side_effect=AssertionError("constructed orchestrator")), \
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
                 patch("harness_core.core.orchestrator.HarnessOrchestrator", side_effect=AssertionError("constructed orchestrator")), \
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
                 patch("harness_core.core.orchestrator.HarnessOrchestrator", side_effect=AssertionError("constructed orchestrator")):
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
