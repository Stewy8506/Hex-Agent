"""F06 tests: one command catalog governs every surface, and first-run
onboarding is reachable from the actual default (native) launch route.

The shared catalog lives at hex_cli/command_catalog.json.
These tests fail if ACP dispatch, console help/completion, or the launch
flow drifts away from it.
"""

from __future__ import annotations

import inspect
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness_core.client import cli
from harness_core.client.acp import ACPServer
from harness_core.client.auto_setup import EnvironmentSnapshot
from harness_core.client.command_catalog import (
    SURFACES,
    completion_names,
    load_catalog,
    render_help,
)
from harness_core.client.tui import HexConsole


class TestCatalogShape(unittest.TestCase):
    def test_entries_are_well_formed(self) -> None:
        catalog = load_catalog()
        self.assertTrue(catalog)
        names = [c.name for c in catalog]
        self.assertEqual(len(names), len(set(names)), "duplicate command names")
        for spec in catalog:
            self.assertTrue(spec.name.startswith("/"), spec.name)
            self.assertTrue(spec.usage.startswith("/"), spec.usage)
            self.assertTrue(spec.description, spec.name)
            self.assertEqual(set(spec.surfaces), set(SURFACES), spec.name)
            for status in spec.surfaces.values():
                self.assertIn(status, ("supported", "unavailable"), spec.name)

    def test_tui_commands_are_local_or_forwardable(self) -> None:
        for spec in load_catalog():
            if spec.supported_on("tui"):
                self.assertTrue(
                    spec.tui_local or spec.supported_on("acp"),
                    f"{spec.name} is offered by the TUI but neither local nor ACP-supported",
                )


class TestAcpSurfaceMatchesCatalog(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.server = ACPServer(orchestrator=object(), workspace_root=self._tmp.name)

    def test_every_acp_command_resolves_and_none_are_undeclared(self) -> None:
        handlers = self.server._command_handlers()
        acp_specs = [c for c in load_catalog() if c.supported_on("acp")]
        declared = {n for c in acp_specs for n in c.all_names()}

        # No handler exists for a command the catalog does not declare.
        self.assertTrue(
            set(handlers) <= declared,
            f"undeclared ACP handlers: {set(handlers) - declared}",
        )

        # Every declared name/alias dispatches through the real request path.
        # Handlers are stubbed so resolution is proven without executing ops.
        stubbed = {key: (lambda sess, args: "ok") for key in handlers}
        with mock.patch.object(self.server, "_command_handlers", return_value=stubbed):
            for spec in acp_specs:
                for name in spec.all_names():
                    resp = self.server._handle_session_command(
                        {"sessionId": "ses_cat", "command": name}
                    )
                    self.assertFalse(resp["isError"], f"{name} did not dispatch: {resp['output']}")
                    self.assertEqual(resp["output"], "ok", name)

    def test_help_output_is_the_catalog(self) -> None:
        sess = self.server.sessions.get("ses_help")
        if sess is None:
            from harness_core.client.acp import ACPSession

            sess = ACPSession(session_id="ses_help", workspace_root=self._tmp.name)
        self.assertEqual(self.server._cmd_help(sess, ""), render_help("acp"))


class TestConsoleSurfaceMatchesCatalog(unittest.TestCase):
    def test_completion_list_is_the_catalog(self) -> None:
        self.assertEqual(HexConsole.SLASH_COMMANDS, completion_names("console"))

    def test_help_renders_the_catalog(self) -> None:
        text = render_help("console")
        for spec in load_catalog():
            if spec.supported_on("console"):
                self.assertIn(spec.usage, text)

    def test_dispatch_handles_every_console_command(self) -> None:
        source = inspect.getsource(HexConsole.handle_slash_command)
        for spec in load_catalog():
            if spec.supported_on("console"):
                self.assertIn(
                    f'"{spec.name}"',
                    source,
                    f"{spec.name} is declared console-supported but not dispatched",
                )


class TestFirstRunLaunchRoute(unittest.TestCase):
    """First-run setup must run before the native launcher on the real route.

    The test-runner seam is patched False so `hex` takes the same launch_rust
    branch a user gets; launch_hex_tui is recorded (never executed) and must
    observe an already-written configuration.
    """

    def setUp(self) -> None:
        self._home = tempfile.TemporaryDirectory()
        self._ws = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        self.addCleanup(self._ws.cleanup)
        self.cfg_dir = Path(self._home.name) / ".hex"
        self.launch_calls: list[bool] = []

        env = mock.patch.dict(
            os.environ,
            {"HOME": self._home.name, "HEX_TESTING": "1", "HEX_FORCE_INTERACTIVE": "1"},
        )
        env.start()
        self.addCleanup(env.stop)
        self._old_cwd = os.getcwd()
        os.chdir(self._ws.name)
        self.addCleanup(os.chdir, self._old_cwd)

        snapshot = EnvironmentSnapshot(
            os_name="Fixture OS",
            has_git=True,
            has_python=True,
            gemini_key="fixture-key-not-real",
        )
        self._patchers: list[mock._patch] = []

        def start(p: mock._patch):
            started = p.start()
            self._patchers.append(p)
            return started

        self.select_mock = start(
            mock.patch("harness_core.client.ui_selector.select_option", return_value=0)
        )
        self.launch_mock = start(
            mock.patch(
                "harness_core.client.tui_launcher.launch_hex_tui",
                side_effect=self._record_launch,
            )
        )
        start(mock.patch.object(cli, "_is_test_runner", return_value=False))
        start(
            mock.patch(
                "harness_core.config_loader.has_active_credentials", return_value=False
            )
        )
        start(
            mock.patch(
                "harness_core.client.auto_setup.detect_environment",
                return_value=snapshot,
            )
        )
        self.addCleanup(self._stop_patchers)

    def _stop_patchers(self) -> None:
        for p in self._patchers:
            p.stop()

    def _record_launch(self, args, repo_root) -> int:
        self.launch_calls.append((self.cfg_dir / "config.yaml").is_file())
        return 0

    def test_default_launch_runs_setup_before_native_tui(self) -> None:
        rc = cli.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(self.launch_calls, [True], "native launch must see written config")
        self.assertTrue((self.cfg_dir / "config.yaml").is_file())
        self.assertTrue((self.cfg_dir / "credentials.env").is_file())
        self.select_mock.assert_called_once()
        self.launch_mock.assert_called_once()

    def test_tui_subcommand_runs_setup_before_native_tui(self) -> None:
        rc = cli.main(["tui"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.launch_calls, [True])
        self.assertTrue((self.cfg_dir / "config.yaml").is_file())
        self.select_mock.assert_called_once()

    def test_configured_launch_skips_setup(self) -> None:
        with mock.patch(
            "harness_core.config_loader.has_active_credentials", return_value=True
        ):
            rc = cli.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(self.launch_calls, [False], "no setup should have written config")
        self.select_mock.assert_not_called()
        self.launch_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()