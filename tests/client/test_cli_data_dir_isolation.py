"""CLI test runs must never append tasks to a real workspace ledger.

`hex run` resolves its workspace from the source checkout, not the cwd, so a
test invoking the CLI used to bind `<repo>/.hex/data/ledger.db` and leave a
permanent row in the user's resume history.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness_core.client import cli
from harness_core.core import orchestrator


from tests import REPO_ROOT


def _run_capturing_data_dir(argv: list[str]) -> tuple[int, Path | None]:
    captured: dict[str, Path] = {}
    real_orchestrator = orchestrator.HarnessOrchestrator

    def spy(data_dir, *args, **kwargs):
        captured["data_dir"] = Path(str(data_dir)).resolve()
        return real_orchestrator(data_dir, *args, **kwargs)

    out = io.StringIO()
    with patch.object(orchestrator, "HarnessOrchestrator", spy), patch("sys.stdout", out):
        code = cli.main(argv)
    return code, captured.get("data_dir")


class TestCliDataDirIsolation(unittest.TestCase):
    def test_dry_run_binds_scratch_ledger_outside_the_checkout(self):
        with patch.dict(os.environ, {"HEX_TESTING": "1"}):
            code, data_dir = _run_capturing_data_dir([
                "run",
                "Isolation probe: never record me in a real ledger",
                "--mode", "fast_path",
                "--dry-run",
            ])
            self.assertEqual(code, 0)
            self.assertIsNotNone(data_dir)
            self.assertEqual(data_dir, Path(cli._test_data_dir()).resolve())
            self.assertNotIn(REPO_ROOT, data_dir.parents)
            self.assertFalse((REPO_ROOT / ".hex" / "data") == data_dir)
            self.assertFalse((REPO_ROOT / ".harness_data") == data_dir)

    def test_explicit_data_dir_overrides_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            chosen = Path(tmp) / "chosen_ledger"
            code, data_dir = _run_capturing_data_dir([
                "--data-dir", str(chosen),
                "run",
                "Isolation probe: explicit dir",
                "--mode", "fast_path",
                "--dry-run",
            ])
            self.assertEqual(code, 0)
            self.assertEqual(data_dir, chosen.resolve())

    def test_env_data_dir_overrides_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            chosen = Path(tmp) / "env_ledger"
            with patch.dict(os.environ, {"HEX_DATA_DIR": str(chosen), "HEX_TESTING": "1"}):
                code, data_dir = _run_capturing_data_dir([
                    "run",
                    "Isolation probe: env dir",
                    "--mode", "fast_path",
                    "--dry-run",
                ])
            self.assertEqual(code, 0)
            self.assertEqual(data_dir, chosen.resolve())


if __name__ == "__main__":
    unittest.main()