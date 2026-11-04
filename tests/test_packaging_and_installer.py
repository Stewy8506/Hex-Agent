"""Unit and integration tests for HEX packaging, vendor resolution, and installer script."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from harness_core.db import LedgerStore
from harness_core.vendor_paths import (
    get_graphify_dir,
    get_harness2_dir,
    get_skillspector_dir,
    get_vendor_dir,
)


class TestPackagingAndVendorResolution(unittest.TestCase):
    """Tests that vendor resolution, schema loading, and packaging artifacts work robustly."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_vendor_dir_env_var_override(self) -> None:
        """HEX_VENDOR_DIR overrides all other vendor path lookups."""
        custom_vendor = (self.temp_path / "custom_vendor").resolve()
        custom_vendor.mkdir()
        (custom_vendor / "graphify").mkdir()
        (custom_vendor / "skillspector").mkdir()
        (custom_vendor / "harness2").mkdir()

        with patch.dict(os.environ, {"HEX_VENDOR_DIR": str(custom_vendor)}):
            resolved = get_vendor_dir()
            self.assertEqual(resolved, custom_vendor)
            self.assertEqual(get_graphify_dir(), custom_vendor / "graphify")
            self.assertEqual(get_harness2_dir(), custom_vendor / "harness2")

    def test_vendor_dir_repo_fallback(self) -> None:
        """In repo checkout, vendor directory resolves to workspace repo root."""
        with patch.dict(os.environ, {}, clear=False):
            if "HEX_VENDOR_DIR" in os.environ:
                del os.environ["HEX_VENDOR_DIR"]
            if "HARNESS_VENDOR_DIR" in os.environ:
                del os.environ["HARNESS_VENDOR_DIR"]

            vendor = get_vendor_dir()
            self.assertTrue(vendor.is_dir(), f"Vendor dir should be valid: {vendor}")
            self.assertTrue((vendor / "graphify").is_dir() or (vendor / "skillspector").is_dir())

    def test_db_schema_auto_discovery_in_fresh_environment(self) -> None:
        """LedgerStore successfully initializes SQLite schema from packaged migrations in an empty dir."""
        isolated_db = self.temp_path / "isolated_store" / "ledger.db"
        self.assertFalse(isolated_db.exists())

        # Initialize without passing explicit schema path
        store = LedgerStore(isolated_db)
        self.assertTrue(isolated_db.is_file())

        # Verify key tables were automatically created by _init_schema
        cursor = store.conn.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = {row[0] for row in cursor.fetchall()}

        self.assertIn("tasks", tables)
        self.assertIn("workers", tables)
        self.assertIn("tool_calls", tables)
        self.assertIn("approvals", tables)
        self.assertIn("skills", tables)

    def test_installer_script_bash_syntax(self) -> None:
        """install.sh is valid POSIX bash with no syntax errors."""
        installer_path = Path(__file__).resolve().parents[1] / "install.sh"
        self.assertTrue(installer_path.is_file(), "install.sh must exist in repository root")

        # Run bash -n (syntax check without executing)
        res = subprocess.run(["bash", "-n", str(installer_path)], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"install.sh syntax error: {res.stderr}")

        content = installer_path.read_text(encoding="utf-8")
        self.assertIn("HEX AI WORKBENCH", content)
        self.assertIn("onboard --defaults", content)

    def test_built_wheel_structure(self) -> None:
        """Wheel in dist/ contains all required package data (migrations, skills, console scripts)."""
        dist_dir = Path(__file__).resolve().parents[1] / "dist"
        wheels = list(dist_dir.glob("hex_ai-*.whl"))
        self.assertTrue(len(wheels) > 0, "At least one built wheel must be present in dist/")

        latest_wheel = sorted(wheels, key=lambda p: p.stat().st_mtime)[-1]
        with zipfile.ZipFile(latest_wheel, "r") as zf:
            namelist = zf.namelist()

            # Verify core modules
            self.assertIn("harness_core/__init__.py", namelist)
            self.assertIn("harness_core/cli.py", namelist)
            self.assertIn("harness_core/tui.py", namelist)
            self.assertIn("harness_core/vendor_paths.py", namelist)

            # Verify packaged initial schema
            self.assertIn("harness_core/migrations/0001_initial_schema.sql", namelist)

            # Verify packaged built-in skills
            has_skills = any(name.startswith("harness_core/skills/") and name.endswith("SKILL.md") for name in namelist)
            self.assertTrue(has_skills, f"Expected built-in skills in wheel, found: {namelist}")

            # Verify entry points declare 'hex' and 'harness'
            entry_point_files = [n for n in namelist if n.endswith("entry_points.txt")]
            self.assertTrue(len(entry_point_files) > 0)
            entry_points_text = zf.read(entry_point_files[0]).decode("utf-8")
            self.assertIn("hex = harness_core.cli:main", entry_points_text)
            self.assertIn("harness = harness_core.cli:main", entry_points_text)


if __name__ == "__main__":
    unittest.main()
