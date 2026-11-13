"""Integration and unit tests for native Rust Ratatui Cockpit launcher and CLI integration."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness_core.cli import build_parser, main
from harness_core.tui_launcher import (
    build_hex_tui,
    build_tui_cmd_args,
    find_hex_tui_binary,
    find_or_build_hex_tui,
    find_repo_root,
    launch_hex_tui,
)


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
