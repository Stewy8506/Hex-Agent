"""Integration and unit tests for native Rust Ratatui Cockpit launcher and CLI integration."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from harness_core.client import cli, tui_launcher
from harness_core.client.cli import build_parser, main
from harness_core.client.tui_launcher import (
    build_hex_tui,
    build_tui_cmd_args,
    find_hex_tui_binary,
    find_or_build_hex_tui,
    find_repo_root,
    launch_hex_tui,
)


class TestNativeProcessHandoff(unittest.TestCase):
    def test_launcher_handoff_and_subprocess_share_arguments_and_environment(self):
        root = Path("/fixture/checkout")
        binary = root / "hex-tui"
        for platform, handoff, check in (
            ("posix", True, False), ("posix", False, False),
            ("posix", True, True), ("nt", True, False),
        ):
            for in_venv in (False, True):
                with self.subTest(platform=platform, handoff=handoff, check=check, in_venv=in_venv):
                    args = argparse.Namespace(
                        check=check, project="alpha", workspace="/fixture/workspace",
                        model="chosen-model", max_tokens=12345, skin="mono", session="session-1",
                    )
                    execve = MagicMock(side_effect=SystemExit(41))
                    launcher_os = SimpleNamespace(name=platform, environ=os.environ, execve=execve)
                    with patch.dict(os.environ, {
                        "HEX_PHASE1_CREDENTIAL": "keep-me", "HEX_PYTHON_BIN": "stale-python",
                        "HEX_REPO_ROOT": "stale-root", "VIRTUAL_ENV": "inherited-env",
                    }, clear=True), \
                         patch.object(tui_launcher, "os", launcher_os), \
                         patch.object(sys, "executable", "/fixture/python"), \
                         patch.object(sys, "base_prefix", "/base"), \
                         patch.object(sys, "prefix", "/venv" if in_venv else "/base"), \
                         patch.object(tui_launcher, "find_hex_tui_binary", return_value=binary), \
                         patch.object(tui_launcher.subprocess, "run") as run:
                        run.return_value.returncode = -15
                        should_exec = platform == "posix" and handoff and not check
                        if should_exec:
                            with self.assertRaises(SystemExit) as exit_info:
                                launch_hex_tui(args, root, process_handoff=handoff)
                            self.assertEqual(exit_info.exception.code, 41)
                            execve.assert_called_once()
                            self.assertEqual(execve.call_args.args[0], str(binary))
                            cmd, env = execve.call_args.args[1:]
                            run.assert_not_called()
                        else:
                            self.assertEqual(launch_hex_tui(args, root, process_handoff=handoff), -15)
                            execve.assert_not_called()
                            run.assert_called_once()
                            cmd, env = run.call_args.args[0], run.call_args.kwargs["env"]
                            self.assertFalse(run.call_args.kwargs["check"])
                        self.assertEqual(cmd, build_tui_cmd_args(args, binary))
                        self.assertEqual(env["HEX_PHASE1_CREDENTIAL"], "keep-me")
                        self.assertEqual(env["HEX_PYTHON_BIN"], "/fixture/python")
                        self.assertEqual(env["HEX_REPO_ROOT"], str(root))
                        self.assertEqual(env["VIRTUAL_ENV"], "/venv" if in_venv else "inherited-env")
                        self.assertEqual(os.environ["HEX_PYTHON_BIN"], "stale-python")
                        self.assertEqual(os.environ["HEX_REPO_ROOT"], "stale-root")

    def test_only_real_interactive_posix_entrypoint_opts_into_handoff(self):
        for use_entrypoint, argv, platform, stdin_tty, stdout_tty, testing in (
            (True, ["tui"], "posix", True, True, False),
            (True, [], "posix", True, True, False),
            (True, ["chat"], "posix", True, True, False),
            (True, ["tui", "--check"], "posix", True, True, False),
            (True, ["tui"], "posix", False, True, False),
            (True, ["tui"], "posix", True, False, False),
            (True, ["tui"], "nt", True, True, False),
            (True, ["tui"], "posix", True, True, True),
            (False, ["tui"], "posix", True, True, False),
            (False, None, "posix", True, True, False),
        ):
            with self.subTest(entrypoint=use_entrypoint, argv=argv, platform=platform,
                              stdin_tty=stdin_tty, stdout_tty=stdout_tty, testing=testing):
                cli_os = SimpleNamespace(name=platform, environ=os.environ)
                with patch.object(cli, "os", cli_os), \
                     patch.object(sys, "argv", ["hex", *(argv if argv is not None else ["tui"])]), \
                     patch.object(sys.stdin, "isatty", return_value=stdin_tty), \
                     patch.object(sys.stdout, "isatty", return_value=stdout_tty), \
                     patch.object(cli, "_is_test_runner", return_value=testing), \
                     patch.object(cli, "_maybe_first_run_setup"), \
                     patch("harness_core.config_loader.load_hex_environment"), \
                     patch("harness_core.storage.project_registry.ProjectRegistry", side_effect=AssertionError("registry construction")), \
                     patch("harness_core.core.orchestrator.HarnessOrchestrator", side_effect=AssertionError("backend construction")), \
                     patch.object(tui_launcher, "launch_hex_tui", return_value=29) as launch:
                    code = cli.entrypoint() if use_entrypoint else cli.main(argv)
                    self.assertEqual(code, 29)
                    launch.assert_called_once()
                    expected = use_entrypoint and platform == "posix" and stdin_tty and stdout_tty and not testing and "--check" not in (argv or [])
                    self.assertEqual(launch.call_args.kwargs, {"process_handoff": True} if expected else {})

    @unittest.skipUnless(os.name == "posix", "POSIX interactive handoff")
    def test_module_routing_uses_real_entrypoint(self):
        script = '''
import runpy
import sys
from harness_core import config_loader
from harness_core.client import tui_launcher
config_loader.load_hex_environment = lambda **kwargs: None
config_loader.has_active_credentials = lambda: True
sys.stdin.isatty = lambda: True
sys.stdout.isatty = lambda: True
sys.argv = ["hex", "tui"]
def launch(args, root, **kwargs):
    assert kwargs == {"process_handoff": True}, kwargs
    return 31
tui_launcher.launch_hex_tui = launch
runpy.run_module("harness_core.client.cli", run_name="__main__")
'''
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env.pop("HEX_TESTING", None)
            env["HOME"] = tmp
            result = subprocess.run(
                [sys.executable, "-c", script], cwd=tmp, env=env,
                capture_output=True, text=True, timeout=60,
            )
        self.assertEqual(result.returncode, 31, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")

    def test_missing_native_and_failed_exec_keep_legacy_fallback(self):
        for error in (FileNotFoundError("native missing"), OSError("exec failed")):
            with self.subTest(error=error), \
                 patch.object(sys, "argv", ["hex", "chat", "--ratatui", "--quiet"]), \
                 patch.object(sys.stdin, "isatty", return_value=True), \
                 patch.object(sys.stdout, "isatty", return_value=True), \
                 patch.object(cli, "_is_test_runner", return_value=False), \
                 patch.object(cli, "_maybe_first_run_setup"), \
                 patch("harness_core.config_loader.load_hex_environment"), \
                 patch.object(tui_launcher, "launch_hex_tui", side_effect=error), \
                 patch("harness_core.core.orchestrator.HarnessOrchestrator") as constructor, \
                 patch("harness_core.storage.project_registry.ProjectRegistry") as registry, \
                 patch("harness_core.client.tui.HexConsole") as console:
                registry.return_value.detect_enclosing_project.return_value = None
                console.return_value.start.return_value = 7
                self.assertEqual(cli.entrypoint(), 7)
                constructor.assert_called_once()
                console.assert_called_once()

    @unittest.skipUnless(os.name == "posix", "POSIX exec and signal semantics")
    def test_real_exec_keeps_pid_environment_and_exit_signal_status(self):
        script = '''
import argparse
import os
import sys
from pathlib import Path
from harness_core.client import tui_launcher
root = Path.cwd()
parent_pid = os.getpid()
child = (
    "import json, os, signal, sys; "
    f"assert os.getpid() == {parent_pid}; "
    "assert os.environ['HEX_PHASE1_CREDENTIAL'] == 'inherited'; "
    f"assert os.environ['HEX_PYTHON_BIN'] == {sys.executable!r}; "
    f"assert os.environ['HEX_REPO_ROOT'] == {str(root)!r}; "
    "print(json.dumps({'pid_preserved': True}), flush=True); "
    + ("sys.exit(37)" if sys.argv[1] == "exit" else "os.kill(os.getpid(), signal.SIGTERM)")
)
tui_launcher.find_hex_tui_binary = lambda *args, **kwargs: Path(sys.executable)
tui_launcher.build_tui_cmd_args = lambda *args: [sys.executable, "-c", child]
tui_launcher.launch_hex_tui(argparse.Namespace(check=False), root, process_handoff=True)
raise AssertionError("exec returned")
'''
        with tempfile.TemporaryDirectory() as tmp:
            for outcome, expected in (("exit", 37), ("signal", -15)):
                with self.subTest(outcome=outcome):
                    env = os.environ.copy()
                    env.update(HOME=tmp, HEX_PHASE1_CREDENTIAL="inherited")
                    result = subprocess.run(
                        [sys.executable, "-c", script, outcome], cwd=tmp, env=env,
                        capture_output=True, text=True, timeout=60,
                    )
                    self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                    self.assertEqual(result.stdout.strip(), '{"pid_preserved": true}')

    def test_keyboard_interrupt_return_contract_is_preserved(self):
        for use_entrypoint in (False, True):
            with self.subTest(entrypoint=use_entrypoint), \
                 patch.object(cli, "_main_impl", side_effect=KeyboardInterrupt), \
                 patch.object(sys, "stdout"):
                self.assertEqual(cli.entrypoint() if use_entrypoint else cli.main([]), 130)


class TestRustTuiLauncher(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Ensure hex-tui binary is built for testing
        cls.repo_root = find_repo_root()
        cls.bin_path = find_or_build_hex_tui(cls.repo_root)
        assert cls.bin_path.is_file(), f"Binary not found: {cls.bin_path}"

    def test_find_repo_root(self):
        root = find_repo_root()
        self.assertTrue((root / "Cargo.toml").is_file())
        self.assertTrue((root / "crates" / "hex-tui").is_dir())

    def test_find_hex_tui_binary(self):
        found = find_hex_tui_binary(self.repo_root)
        self.assertIsNotNone(found)
        self.assertTrue(found.is_file())
        self.assertTrue(os.access(found, os.X_OK))

    def test_env_var_override(self):
        with tempfile.NamedTemporaryFile(suffix="-dummy", delete=False) as tf:
            tf.write(b"#!/bin/sh\nexit 0\n")
            dummy_path = Path(tf.name)
        dummy_path.chmod(0o755)

        try:
            with patch.dict(os.environ, {"HEX_TUI_BIN": str(dummy_path)}):
                resolved = find_hex_tui_binary(self.repo_root)
                self.assertEqual(resolved, dummy_path.resolve())
        finally:
            if dummy_path.exists():
                dummy_path.unlink()

    def test_build_tui_cmd_args(self):
        parser = build_parser()
        args = parser.parse_args([
            "tui",
            "--check",
            "--project", "alpha",
            "--workspace", "/tmp/ws",
            "--model", "claude-3-5-sonnet",
            "--cow", "Btrfs",
            "--session", "ses_test_123",
        ])

        dummy_bin = Path("/usr/local/bin/hex-tui")
        cmd = build_tui_cmd_args(args, dummy_bin)

        self.assertEqual(cmd[0], str(dummy_bin))
        self.assertIn("--check", cmd)
        self.assertIn("--project", cmd)
        self.assertEqual(cmd[cmd.index("--project") + 1], "alpha")
        self.assertIn("--workspace", cmd)
        self.assertEqual(cmd[cmd.index("--workspace") + 1], "/tmp/ws")
        self.assertIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "claude-3-5-sonnet")
        self.assertIn("--cow", cmd)
        self.assertEqual(cmd[cmd.index("--cow") + 1], "Btrfs")
        self.assertIn("--session", cmd)
        self.assertEqual(cmd[cmd.index("--session") + 1], "ses_test_123")

    def test_stale_project_config_does_not_shadow_global_model(self):
        with tempfile.TemporaryDirectory() as ws_dir, tempfile.TemporaryDirectory() as home_dir:
            ws, home = Path(ws_dir), Path(home_dir)
            (ws / ".hex").mkdir()
            (ws / ".hex" / "config.yaml").write_text(
                'model:\n  cloud_provider: "antigravity"\n  cloud_model_id: "gemini-3.8-flash-tiered"\n',
                encoding="utf-8",
            )
            (home / ".hex").mkdir()
            (home / ".hex" / "config.yaml").write_text(
                'model:\n  cloud_provider: "openrouter"\n'
                '  cloud_model_id: "nvidia/nemotron-3-ultra-550b-a55b:free"\n',
                encoding="utf-8",
            )
            args = argparse.Namespace(model=None, provider=None, workspace=str(ws))
            with patch.object(Path, "home", return_value=home):
                cmd = build_tui_cmd_args(args, Path("/usr/local/bin/hex-tui"))
            self.assertIn("--model", cmd)
            self.assertEqual(
                cmd[cmd.index("--model") + 1],
                "nvidia/nemotron-3-ultra-550b-a55b:free",
                "a project config without project_override must not defeat the global model",
            )

    def test_project_override_true_does_shadow_global_model(self):
        with tempfile.TemporaryDirectory() as ws_dir, tempfile.TemporaryDirectory() as home_dir:
            ws, home = Path(ws_dir), Path(home_dir)
            (ws / ".hex").mkdir()
            (ws / ".hex" / "config.yaml").write_text(
                'model:\n  project_override: true\n'
                '  cloud_provider: "antigravity"\n  cloud_model_id: "gemini-3.8-flash-tiered"\n',
                encoding="utf-8",
            )
            (home / ".hex").mkdir()
            (home / ".hex" / "config.yaml").write_text(
                'model:\n  cloud_provider: "openrouter"\n'
                '  cloud_model_id: "nvidia/nemotron-3-ultra-550b-a55b:free"\n',
                encoding="utf-8",
            )
            args = argparse.Namespace(model=None, provider=None, workspace=str(ws))
            with patch.object(Path, "home", return_value=home):
                cmd = build_tui_cmd_args(args, Path("/usr/local/bin/hex-tui"))
            self.assertEqual(cmd[cmd.index("--model") + 1], "gemini-3.8-flash-tiered")

    def test_direct_headless_check_launch(self):
        args = argparse.Namespace(
            check=True,
            project="default",
            workspace=None,
            model="gemini-2.5-pro",
            cow="OverlayFS",
            session=None,
            build=False,
            release=False,
        )
        code = launch_hex_tui(args, self.repo_root)
        self.assertEqual(code, 0)

    def test_cli_tui_subcommand_check(self):
        exit_code = main(["tui", "--check"])
        self.assertEqual(exit_code, 0)

    def test_cli_chat_ratatui_flag_check(self):
        exit_code = main(["chat", "--ratatui", "--check"])
        self.assertEqual(exit_code, 0)

    def test_cli_parser_has_tui_and_ratatui(self):
        parser = build_parser()

        # Check 'tui' parser
        tui_args = parser.parse_args(["tui", "--check", "--release"])
        self.assertEqual(tui_args.cmd, "tui")
        self.assertTrue(tui_args.check)
        self.assertTrue(tui_args.release)

        # Check 'chat --ratatui'
        chat_args = parser.parse_args(["chat", "--ratatui"])
        self.assertEqual(chat_args.cmd, "chat")
        self.assertTrue(chat_args.ratatui)


if __name__ == "__main__":
    unittest.main()
