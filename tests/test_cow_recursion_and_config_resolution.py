"""Tests for CoW Snapshot Recursion Prevention and Model Configuration Resolution.

Validates:
1. CoW snapshot creation never recurses into `.hex`, `.venv`, `.git`, or `.harness_data`.
2. Snapshots created inside the project tree (.hex/data/cow_snapshots) never duplicate .hex.
3. Proof diff service excludes IGNORE_DIRS including .hex.
4. TwoTierModelRouter dynamically resolves HEX_CLOUD_MODEL_ID and HEX_CLOUD_PROVIDER from environment.
5. Config loader correctly parses and cascades config.yaml into environment variables.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from harness_core.config_loader import load_hex_environment, parse_config_yaml
from harness_core.cow import (
    IGNORE_DIRS,
    APFSAndPortableCoWProvider,
    _cow_copy_tree,
    compute_tree_sha,
)
from harness_core.proof import VerificationAndProofService
from harness_core.router import TwoTierModelRouter


class TestCoWRecursionPrevention(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.ws = self.root / "my_project"
        self.ws.mkdir()

        # Add regular project files
        (self.ws / "app.py").write_text("print('hello')", encoding="utf-8")
        src_dir = self.ws / "src"
        src_dir.mkdir()
        (src_dir / "lib.py").write_text("def fn(): return 1", encoding="utf-8")

        # Add ignored directories that should NEVER be cloned into snapshots
        (self.ws / ".hex").mkdir()
        (self.ws / ".hex" / "config.yaml").write_text("test: 1", encoding="utf-8")
        (self.ws / ".venv").mkdir()
        (self.ws / ".venv" / "bin").mkdir()
        (self.ws / ".venv" / "bin" / "python").write_text("#!/bin/sh", encoding="utf-8")
        (self.ws / ".git").mkdir()
        (self.ws / ".git" / "HEAD").write_text("ref: refs/heads/main", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_ignore_dirs_contains_hex_and_venv(self):
        self.assertIn(".hex", IGNORE_DIRS)
        self.assertIn(".venv", IGNORE_DIRS)
        self.assertIn(".git", IGNORE_DIRS)
        self.assertIn(".harness_data", IGNORE_DIRS)

    def test_cow_copy_tree_skips_ignored_dirs(self):
        dst = self.root / "snapshot_dst"
        _cow_copy_tree(self.ws, dst, apfs_supported=True)

        # Valid source files must exist
        self.assertTrue((dst / "app.py").is_file())
        self.assertTrue((dst / "src" / "lib.py").is_file())

        # Ignored directories must NOT exist in snapshot
        self.assertFalse((dst / ".hex").exists())
        self.assertFalse((dst / ".venv").exists())
        self.assertFalse((dst / ".git").exists())

    def test_cow_snapshot_inside_workspace_does_not_recurse(self):
        # Place snapshots root INSIDE .hex within the workspace (the real-world scenario)
        snapshots_root = self.ws / ".hex" / "data" / "cow_snapshots"
        snapshots_root.mkdir(parents=True)

        provider = APFSAndPortableCoWProvider(snapshots_root=snapshots_root)
        meta = provider.create_snapshot(self.ws, label="test_snap")

        self.assertTrue(meta.snapshot_path.is_dir())
        self.assertTrue((meta.snapshot_path / "app.py").is_file())
        self.assertTrue((meta.snapshot_path / "src" / "lib.py").is_file())

        # The snapshot must NOT have cloned .hex into itself
        self.assertFalse((meta.snapshot_path / ".hex").exists())
        self.assertFalse((meta.snapshot_path / ".venv").exists())


class TestProofDiffExclusion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.base = self.root / "base"
        self.head = self.root / "head"
        self.base.mkdir()
        self.head.mkdir()

        (self.base / "main.py").write_text("v1", encoding="utf-8")
        (self.head / "main.py").write_text("v2", encoding="utf-8")

        # Add .hex difference which should be ignored
        (self.base / ".hex").mkdir()
        (self.head / ".hex").mkdir()
        (self.base / ".hex" / "cache.bin").write_text("base_cache", encoding="utf-8")
        (self.head / ".hex" / "cache.bin").write_text("head_cache_huge", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_compute_git_diff_ignores_hex(self):
        svc = VerificationAndProofService(
            ledger=MagicMock(),
            cow=MagicMock(),
            gateway=MagicMock(),
            graphify=MagicMock(),
            cas_dir=self.root / "cas",
        )
        diff_sha, diff_text = svc.compute_git_diff(self.base, self.head)
        self.assertIn("main.py", diff_text)
        self.assertNotIn("cache.bin", diff_text)
        self.assertNotIn("head_cache_huge", diff_text)


class TestModelConfigurationResolution(unittest.TestCase):
    def setUp(self):
        self._orig_env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._orig_env)

    def test_two_tier_model_router_reads_env_model_id(self):
        os.environ["HEX_CLOUD_MODEL_ID"] = "claude-opus-4-6-thinking"
        os.environ["HEX_CLOUD_PROVIDER"] = "antigravity"

        router = TwoTierModelRouter()
        self.assertEqual(router.cloud_model_id, "claude-opus-4-6-thinking")

    def test_two_tier_model_router_reads_local_model_from_env(self):
        os.environ["HEX_LOCAL_MODEL_ID"] = "mlx-community/Llama-3.2-3B-Instruct-4bit"
        router = TwoTierModelRouter()
        self.assertEqual(router.local_model_id, "mlx-community/Llama-3.2-3B-Instruct-4bit")

    def test_load_hex_environment_prioritizes_config(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            cfg_dir = Path(tmp.name)
            cfg_file = cfg_dir / "config.yaml"
            cfg_file.write_text(
                """
model:
  profile: "custom_hybrid"
  cloud_provider: "antigravity"
  cloud_model_id: "claude-opus-4-6-thinking"
  local_model_id: "mlx-community/Llama-3.2-3B-Instruct-4bit"
  compressor_mode: "cloud"
""",
                encoding="utf-8",
            )
            # Remove any existing env var to test load
            os.environ.pop("HEX_CLOUD_MODEL_ID", None)
            os.environ.pop("HEX_CLOUD_PROVIDER", None)
            os.environ.pop("HEX_LOCAL_MODEL_ID", None)
            os.environ.pop("HEX_MODEL_PROFILE", None)

            load_hex_environment(config_dir=cfg_dir, workspace_root=cfg_dir, override=True)
            self.assertEqual(os.environ.get("HEX_CLOUD_MODEL_ID"), "claude-opus-4-6-thinking")
            self.assertEqual(os.environ.get("HEX_CLOUD_PROVIDER"), "antigravity")
            self.assertEqual(os.environ.get("HEX_LOCAL_MODEL_ID"), "mlx-community/Llama-3.2-3B-Instruct-4bit")
            self.assertEqual(os.environ.get("HEX_MODEL_PROFILE"), "custom_hybrid")
        finally:
            tmp.cleanup()

    def test_detect_default_local_model_non_darwin(self):
        with unittest.mock.patch("sys.platform", "linux"):
            from harness_core.router import _detect_default_local_model
            os.environ.pop("HEX_LOCAL_MODEL_ID", None)
            os.environ.pop("HARNESS_LOCAL_MODEL_ID", None)
            detected = _detect_default_local_model()
            self.assertEqual(detected, "local_deterministic_compressor")


if __name__ == "__main__":
    unittest.main()
