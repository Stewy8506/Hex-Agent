"""Unit tests for hex uninstall and hex daemon uninstall."""

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness_core.cli import main
from harness_core.daemon import DaemonConfig, DaemonInstaller


class TestUninstall(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_daemon_uninstall_not_installed(self) -> None:
        cfg = DaemonConfig(service_name="test_nonexistent_service", data_dir=str(self.tmp_path))
        installer = DaemonInstaller(cfg)
        res = installer.uninstall_daemon(dry_run=False)
        self.assertEqual(res["status"], "not_installed")
        self.assertFalse(res["unloaded"])
        self.assertFalse(res["removed"])

    def test_daemon_uninstall_installed_dry_run_and_real(self) -> None:
        fake_plist = self.tmp_path / "com.test.daemon.plist"
        fake_plist.write_text("<plist></plist>", encoding="utf-8")

        cfg = DaemonConfig(service_name="com.test.daemon", data_dir=str(self.tmp_path))
        installer = DaemonInstaller(cfg)

        with patch.object(
            installer,
            "get_daemon_status",
            return_value={"platform": "launchd", "installed": True, "service_path": str(fake_plist), "active": True},
        ):
            # Test dry-run
            dry_res = installer.uninstall_daemon(dry_run=True)
            self.assertEqual(dry_res["status"], "dry_run")
            self.assertTrue(fake_plist.is_file())

            # Test real uninstall
            real_res = installer.uninstall_daemon(dry_run=False)
            self.assertEqual(real_res["status"], "uninstalled")
            self.assertTrue(real_res["removed"])
            self.assertFalse(fake_plist.is_file())

    def test_cli_daemon_uninstall(self) -> None:
        out = io.StringIO()
        with patch("sys.stdout", out):
            code = main(["daemon", "uninstall", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn('"status": "not_installed"', out.getvalue())

    def test_cli_uninstall_dry_run_keep_data(self) -> None:
        out = io.StringIO()
        with patch("sys.stdout", out):
            code = main(["uninstall", "--dry-run", "--yes", "--keep-data"])
        self.assertEqual(code, 0)
        output = out.getvalue()
        self.assertIn("HEX Uninstaller", output)
        self.assertIn("Dry-run completed", output)

    def test_cli_uninstall_purge_logic(self) -> None:
        fake_hex = self.tmp_path / ".hex"
        fake_hex.mkdir(parents=True, exist_ok=True)
        (fake_hex / "config.yaml").write_text("model: test", encoding="utf-8")

        with patch("pathlib.Path.home", return_value=self.tmp_path):
            out = io.StringIO()
            with patch("sys.stdout", out):
                code = main(["uninstall", "--purge", "--yes"])
            self.assertEqual(code, 0)
            self.assertFalse(fake_hex.is_dir())
            output = out.getvalue()
            self.assertIn("Purged data directory", output)


if __name__ == "__main__":
    unittest.main()
