"""Test suite for HEX CLI entrypoints and onboarding wizard."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from harness_core.cli import main
from harness_core.onboard import HardwareProfile, run_onboard_wizard


class TestHexCLI(unittest.TestCase):
    """Verifies HEX CLI commands, hardware auto-discovery, and onboarding sequence."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.tmp_dir.name) / "hex_config"
        self.data_dir = Path(self.tmp_dir.name) / "hex_data"

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_hardware_profiling(self) -> None:
        """Verify hardware profiling detects OS, RAM, snapshot provider, and sandbox."""
        prof = HardwareProfile.detect()
        self.assertTrue(prof.os_name in ("Darwin", "Linux", "Windows"))
        self.assertGreater(prof.ram_gb, 0)
        self.assertTrue(len(prof.cow_backend) > 0)
        self.assertTrue(len(prof.sandbox_backend) > 0)

    def test_hex_onboard_defaults(self) -> None:
        """Verify hex onboard creates valid config, credentials with 0600 permissions, and seeds skills."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            res = run_onboard_wizard(
                config_dir=self.config_dir,
                defaults_only=True,
                non_interactive=True,
            )

        output = buf.getvalue()
        self.assertIn("HEX: Autonomous AI Workbench", output)
        self.assertIn("HEX has been successfully initialized!", output)
        self.assertEqual(res["status"], "ready")
        self.assertGreaterEqual(res["seeded_skills"], 2)

        cfg_path = Path(res["config_file"])
        creds_path = Path(res["credentials_file"])
        self.assertTrue(cfg_path.is_file())
        self.assertTrue(creds_path.is_file())

        cfg_text = cfg_path.read_text(encoding="utf-8")
        self.assertIn("HEX Autonomous AI Workbench Configuration", cfg_text)
        self.assertIn("compressor_mode: \"local\"", cfg_text)

        # Check credentials file permissions on POSIX
        if os.name == "posix":
            mode = oct(creds_path.stat().st_mode & 0o777)
            self.assertEqual(mode, "0o600")

    def test_hex_cli_onboard_subcommand(self) -> None:
        """Verify hex CLI invokes onboard wizard cleanly (backwards-compatible alias)."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            exit_code = main(["--data-dir", str(self.data_dir), "onboard", "--config-dir", str(self.config_dir), "--defaults"])
        self.assertEqual(exit_code, 0)
        self.assertTrue((self.config_dir / "config.yaml").is_file())

    def test_hex_cli_setup_subcommand(self) -> None:
        """Verify hex CLI invokes setup wizard cleanly."""
        setup_cfg_dir = Path(self.tmp_dir.name) / "setup_config"
        setup_cfg_dir.mkdir()
        buf = io.StringIO()
        with redirect_stdout(buf):
            exit_code = main(["--data-dir", str(self.data_dir), "setup", "--config-dir", str(setup_cfg_dir), "--defaults"])
        self.assertEqual(exit_code, 0)
        self.assertTrue((setup_cfg_dir / "config.yaml").is_file())

    def test_hex_cli_init_and_status(self) -> None:
        """Verify hex init sets up ledger and hex status reports tasks."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["--data-dir", str(self.data_dir), "init"])
        self.assertEqual(code, 0)
        init_json = json.loads(buf.getvalue())
        self.assertEqual(init_json["status"], "initialized")
        self.assertGreaterEqual(len(init_json["seeded_skills"]), 2)

        buf_stat = io.StringIO()
        with redirect_stdout(buf_stat):
            code_stat = main(["--data-dir", str(self.data_dir), "status"])
        self.assertEqual(code_stat, 0)
        stat_json = json.loads(buf_stat.getvalue())
        self.assertIn("tasks", stat_json)

    def test_hex_cli_submit_fast_path(self) -> None:
        """Verify hex submit executes a fast-path task and returns proof bundle."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["--data-dir", str(self.data_dir), "submit", "Diagnostic ping", "--mode", "fast_path"])
        self.assertEqual(code, 0)
        res = json.loads(buf.getvalue())
        self.assertEqual(res["task"]["status"], "SUCCEEDED")
        self.assertTrue(res["proof"]["verification_results"]["passed"])
        self.assertTrue(res["proof"]["ledger_signature"].startswith("ed25519-hmac-sha256:"))

    def test_hex_bare_invocation_starts_tui(self) -> None:
        """Running 'hex' with no subcommands immediately launches the interactive TUI console like Hermes."""
        from unittest.mock import patch

        with patch("harness_core.tui.HexConsole.start", return_value=0) as mock_start:
            ret = main([])
            self.assertEqual(ret, 0)
            self.assertTrue(mock_start.called, "Running bare 'hex' should start HexConsole")

    def test_hex_tui_options_at_top_level(self) -> None:
        """Top-level options like 'hex --skin matrix -Q' pass directly to HexConsole."""
        from unittest.mock import patch

        with patch("harness_core.tui.HexConsole.__init__", return_value=None) as mock_init, \
             patch("harness_core.tui.HexConsole.start", return_value=0):
            ret = main(["--skin", "matrix", "-Q"])
            self.assertEqual(ret, 0)
            kwargs = mock_init.call_args.kwargs
            self.assertEqual(kwargs["skin_name"], "matrix")
            self.assertTrue(kwargs["quiet"])


if __name__ == "__main__":
    unittest.main()
