"""Tests for ProjectRegistry, workspace isolation, daily project switcher, and hex setup/onboard."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness_core.cli import main
from harness_core.onboard import init_project
from harness_core.orchestrator import HarnessOrchestrator
from harness_core.project_registry import ProjectRecord, ProjectRegistry, compute_project_id
from harness_core.providers.cloud_provider import CloudModelProvider, CloudProviderType, ProviderUnavailableError
from harness_core.tui import HexConsole


class TestProjectRegistryPersistence(unittest.TestCase):
    """Verifies ProjectRegistry storage, lookups, atomic writes, and ordering."""

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.reg_file = self.tmp_dir / "projects.json"
        self.registry = ProjectRegistry(self.reg_file)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_register_and_list_sorted_by_last_active(self) -> None:
        p1 = self.tmp_dir / "proj1"
        p2 = self.tmp_dir / "proj2"
        p3 = self.tmp_dir / "proj3"
        p1.mkdir()
        p2.mkdir()
        p3.mkdir()

        rec1 = self.registry.register_project(p1, name="proj-1", stack="python")
        rec2 = self.registry.register_project(p2, name="proj-2", stack="node")
        rec3 = self.registry.register_project(p3, name="proj-3", stack="rust")

        # Mutate last_active timestamps manually to verify sorting
        rec1.last_active = "2026-01-01T00:00:00Z"
        rec2.last_active = "2026-03-01T00:00:00Z"
        rec3.last_active = "2026-02-01T00:00:00Z"
        self.registry._save()

        projects = self.registry.list_projects()
        self.assertEqual(len(projects), 3)
        self.assertEqual([p.name for p in projects], ["proj-2", "proj-3", "proj-1"])

    def test_get_project_lookup_variants(self) -> None:
        p = self.tmp_dir / "my-awesome-app"
        p.mkdir()
        rec = self.registry.register_project(p, name="MyAwesomeApp", stack="python")

        # 1. By exact name
        self.assertEqual(self.registry.get_project("MyAwesomeApp"), rec)
        # 2. By case-insensitive name
        self.assertEqual(self.registry.get_project("myawesomeapp"), rec)
        # 3. By exact path
        self.assertEqual(self.registry.get_project(str(p)), rec)
        # 4. By resolved path
        self.assertEqual(self.registry.get_project(str(p.resolve())), rec)
        # 5. Nonexistent returns None
        self.assertIsNone(self.registry.get_project("nonexistent"))

    def test_touch_active_updates_timestamp_and_count(self) -> None:
        p = self.tmp_dir / "app"
        p.mkdir()
        rec = self.registry.register_project(p, name="app")
        old_time = rec.last_active
        self.assertEqual(rec.task_count, 0)

        self.registry.touch_active(p, increment_tasks=True)
        updated = self.registry.get_project("app")
        self.assertIsNotNone(updated)
        self.assertEqual(updated.task_count, 1)
        self.assertGreaterEqual(updated.last_active, old_time)

    def test_atomic_json_persistence(self) -> None:
        p = self.tmp_dir / "app"
        p.mkdir()
        self.registry.register_project(p, name="persisted-app", stack="go")

        self.assertTrue(self.reg_file.is_file())
        data = json.loads(self.reg_file.read_text(encoding="utf-8"))
        self.assertIn("projects", data)
        self.assertEqual(len(data["projects"]), 1)
        self.assertEqual(data["projects"][0]["name"], "persisted-app")
        self.assertEqual(data["projects"][0]["stack"], "go")

        # Re-load from fresh instance
        fresh = ProjectRegistry(self.reg_file)
        self.assertEqual(len(fresh.list_projects()), 1)
        self.assertEqual(fresh.list_projects()[0].name, "persisted-app")


class TestProjectDetectionAndIsolation(unittest.TestCase):
    """Verifies enclosing project detection and folder-scoped context isolation."""

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.reg_file = self.tmp_dir / "projects.json"
        self.registry = ProjectRegistry(self.reg_file)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_detect_enclosing_project_from_deep_subfolder(self) -> None:
        repo_root = self.tmp_dir / "my-repo"
        deep_folder = repo_root / "src" / "deep" / "nested" / "pkg"
        deep_folder.mkdir(parents=True)
        (repo_root / ".hex").mkdir()

        detected = self.registry.detect_enclosing_project(deep_folder)
        self.assertIsNotNone(detected)
        self.assertEqual(Path(detected.path).resolve(), repo_root.resolve())

    def test_detect_enclosing_project_with_git_dir(self) -> None:
        repo_root = self.tmp_dir / "git-repo"
        deep_folder = repo_root / "lib" / "sub"
        deep_folder.mkdir(parents=True)
        (repo_root / ".git").mkdir()

        detected = self.registry.detect_enclosing_project(deep_folder)
        self.assertIsNotNone(detected)
        self.assertEqual(Path(detected.path).resolve(), repo_root.resolve())

    def test_detect_enclosing_project_non_repo_returns_none(self) -> None:
        plain_folder = self.tmp_dir / "plain_dir"
        plain_folder.mkdir()
        detected = self.registry.detect_enclosing_project(plain_folder)
        self.assertIsNone(detected)

    def test_folder_scoped_context_isolation(self) -> None:
        proj_a = self.tmp_dir / "project_a"
        proj_b = self.tmp_dir / "project_b"
        proj_a.mkdir()
        proj_b.mkdir()

        rec_a = self.registry.register_project(proj_a, name="ProjectA")
        rec_b = self.registry.register_project(proj_b, name="ProjectB")

        # Verify isolated .hex structures in Project A
        hex_a = proj_a / ".hex"
        self.assertTrue((hex_a / "config.yaml").is_file())
        self.assertTrue((hex_a / "reflections" / "LESSONS.md").is_file())
        self.assertTrue((hex_a / "data").is_dir())
        self.assertIn("Project Reflections & Work Memory: ProjectA", (hex_a / "reflections" / "LESSONS.md").read_text())

        # Verify isolated .hex structures in Project B
        hex_b = proj_b / ".hex"
        self.assertTrue((hex_b / "config.yaml").is_file())
        self.assertTrue((hex_b / "reflections" / "LESSONS.md").is_file())
        self.assertTrue((hex_b / "data").is_dir())
        self.assertIn("Project Reflections & Work Memory: ProjectB", (hex_b / "reflections" / "LESSONS.md").read_text())

        # Modifying A does not touch B
        (hex_a / "data" / "task_a.db").write_text("dummy database A")
        self.assertFalse((hex_b / "data" / "task_a.db").exists())


class TestProjectCLICommands(unittest.TestCase):
    """Verifies `hex project` CLI subcommands (list, add, use, switch)."""

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.reg_file = self.tmp_dir / "projects.json"
        os.environ["HEX_PROJECTS_REGISTRY"] = str(self.reg_file)

    def tearDown(self) -> None:
        os.environ.pop("HEX_PROJECTS_REGISTRY", None)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_project_list_empty(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["project", "list", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buf.getvalue()), [])

    def test_project_add_and_list(self) -> None:
        target = self.tmp_dir / "my-service"
        target.mkdir()

        buf_add = io.StringIO()
        with redirect_stdout(buf_add):
            code = main(["project", "add", str(target), "--name", "my-service", "--json"])
        self.assertEqual(code, 0)
        rec = json.loads(buf_add.getvalue())
        self.assertEqual(rec["name"], "my-service")

        buf_list = io.StringIO()
        with redirect_stdout(buf_list):
            code_list = main(["project", "list"])
        self.assertEqual(code_list, 0)
        out = buf_list.getvalue()
        self.assertIn("my-service", out)
        self.assertIn("Registered Projects (1):", out)

    def test_project_use_and_switch(self) -> None:
        target = self.tmp_dir / "api-hub"
        target.mkdir()
        main(["project", "add", str(target), "--name", "api-hub", "--json"])

        buf_use = io.StringIO()
        with redirect_stdout(buf_use):
            code = main(["project", "use", "api-hub", "--json"])
        self.assertEqual(code, 0)
        rec = json.loads(buf_use.getvalue())
        self.assertEqual(rec["name"], "api-hub")

        buf_switch = io.StringIO()
        with redirect_stdout(buf_switch):
            code_sw = main(["project", "switch", "api-hub"])
        self.assertEqual(code_sw, 0)
        self.assertIn("Active project set to 'api-hub'", buf_switch.getvalue())

    def test_project_use_nonexistent_fails(self) -> None:
        buf_err = io.StringIO()
        code = main(["project", "use", "nonexistent-project-xyz", "--json"])
        self.assertEqual(code, 1)

    def test_project_prune_cli(self) -> None:
        target = self.tmp_dir / "temp-to-delete"
        target.mkdir()
        main(["project", "add", str(target), "--name", "temp-del", "--json"])

        # Delete directory so it becomes stale
        shutil.rmtree(target)

        buf_prune = io.StringIO()
        with redirect_stdout(buf_prune):
            code_prune = main(["project", "prune", "--json"])
        self.assertEqual(code_prune, 0)
        payload = json.loads(buf_prune.getvalue())
        self.assertEqual(payload["count"], 1)
        self.assertIn(str(target.resolve()), payload["pruned"])


class TestDailyProjectSwitcher(unittest.TestCase):
    """Verifies interactive project picker when launching HEX outside a project."""

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.reg_file = self.tmp_dir / "projects.json"
        os.environ["HEX_PROJECTS_REGISTRY"] = str(self.reg_file)
        self.registry = ProjectRegistry(self.reg_file)

    def tearDown(self) -> None:
        os.environ.pop("HEX_PROJECTS_REGISTRY", None)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    @patch("harness_core.config_loader.has_active_credentials", return_value=True)
    @patch("sys.stdin.isatty", return_value=True)
    @patch("harness_core.ui_selector.select_option", return_value=0)
    @patch("harness_core.tui.HexConsole.start", return_value=0)
    def test_daily_switcher_prompts_outside_project(self, mock_tui_start, mock_select, mock_isatty, mock_has_creds) -> None:
        p = self.tmp_dir / "workspace-1"
        p.mkdir()
        self.registry.register_project(p, name="workspace-1", stack="python")

        outside_dir = self.tmp_dir / "empty_dir"
        outside_dir.mkdir()

        # Run main() while CWD is outside any project
        orig_cwd = os.getcwd()
        os.environ["HEX_FORCE_INTERACTIVE"] = "1"
        try:
            os.chdir(outside_dir)
            code = main(["chat", "--skin", "mono"])
            self.assertEqual(code, 0)
            # select_option must be called with "What project do you want to work on today?"
            self.assertTrue(mock_select.called)
            prompt_arg = mock_select.call_args[0][0]
            self.assertIn("What project do you want to work on today?", prompt_arg)
        finally:
            os.environ.pop("HEX_FORCE_INTERACTIVE", None)
            os.chdir(orig_cwd)

    @patch("sys.stdin.isatty", return_value=True)
    @patch("harness_core.ui_selector.select_option")
    @patch("harness_core.tui.HexConsole.start", return_value=0)
    def test_daily_switcher_bypassed_when_inside_project(self, mock_tui_start, mock_select, mock_isatty) -> None:
        proj_dir = self.tmp_dir / "enclosing-repo"
        proj_dir.mkdir()
        (proj_dir / ".git").mkdir()

        orig_cwd = os.getcwd()
        try:
            os.chdir(proj_dir)
            code = main(["chat", "--skin", "mono", "--quiet"])
            self.assertEqual(code, 0)
            # Inside an enclosing project, select_option should NOT be prompted
            self.assertFalse(mock_select.called)
        finally:
            os.chdir(orig_cwd)


class TestTUIProjectSlashCommand(unittest.TestCase):
    """Verifies TUI `/project` slash command inspecting and switching workspaces."""

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.reg_file = self.tmp_dir / "projects.json"
        os.environ["HEX_PROJECTS_REGISTRY"] = str(self.reg_file)
        self.registry = ProjectRegistry(self.reg_file)

        self.proj_a = self.tmp_dir / "alpha"
        self.proj_b = self.tmp_dir / "beta"
        self.proj_a.mkdir()
        self.proj_b.mkdir()
        self.registry.register_project(self.proj_a, name="Alpha", stack="python")
        self.registry.register_project(self.proj_b, name="Beta", stack="rust")

        self.orch = HarnessOrchestrator(data_dir=self.tmp_dir / "data", default_workspace=self.proj_a)
        self.console = HexConsole(self.orch, workspace_root=self.proj_a, quiet=True)

    def tearDown(self) -> None:
        os.environ.pop("HEX_PROJECTS_REGISTRY", None)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_cmd_project_active_card(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.console._cmd_project("")
        out = buf.getvalue()
        self.assertIn("Active Project Workspace:", out)
        self.assertIn("Alpha", out)
        self.assertIn("python", out)

    def test_cmd_project_list(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.console._cmd_project("list")
        out = buf.getvalue()
        self.assertIn("Registered Project Workspaces (2):", out)
        self.assertIn("Alpha", out)
        self.assertIn("Beta", out)

    def test_cmd_project_switch(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.console._cmd_project("switch Beta")
        out = buf.getvalue()
        self.assertIn("Switched active project to 'Beta'", out)
        self.assertEqual(self.console.workspace_root.resolve(), self.proj_b.resolve())
        self.assertEqual(self.console.project_id, compute_project_id(self.proj_b))


class TestGlobalHexSetupAndOnboardParity(unittest.TestCase):
    """Verifies global `hex setup` and backwards-compatible `hex onboard` alias."""

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.data_dir = self.tmp_dir / "data"
        self.config_dir = self.tmp_dir / "config"
        self.data_dir.mkdir()
        self.config_dir.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_cli_setup_subcommand(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["--data-dir", str(self.data_dir), "setup", "--config-dir", str(self.config_dir), "--defaults"])
        self.assertEqual(code, 0)
        self.assertTrue((self.config_dir / "config.yaml").is_file())
        self.assertTrue((self.config_dir / "credentials.env").is_file())

    def test_cli_onboard_alias_subcommand(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["--data-dir", str(self.data_dir), "onboard", "--config-dir", str(self.config_dir), "--defaults"])
        self.assertEqual(code, 0)
        self.assertTrue((self.config_dir / "config.yaml").is_file())

    def test_cloud_provider_error_mentions_hex_setup(self) -> None:
        prov = CloudModelProvider(provider_type=CloudProviderType.ANTHROPIC, api_key="")
        with self.assertRaises(ProviderUnavailableError) as ctx:
            prov._build_request("test prompt", system_prompt=None, max_tokens=100, temperature=0.7, stop_sequences=None)
        self.assertIn("Run 'hex setup'", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
