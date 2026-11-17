"""Tests for unified CLI commands including hex eval runner (GAP-02)."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout

from harness_core.client.cli import main


class TestHexEvalCLI(unittest.TestCase):
    """Verifies hex eval entrypoint, suite selection, formatted table, and JSON output."""

    def test_hex_eval_cli_core_suite(self) -> None:
        """'hex eval --suite core' executes core benchmark suite and prints summary table."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            exit_code = main(["eval", "--suite", "core"])
        output = buf.getvalue()

        self.assertEqual(exit_code, 0)
        self.assertIn("HEX BENCHMARK EVALUATION RUNNER", output)
        self.assertIn("TOTAL / SUMMARY", output)
        self.assertIn("PASSED", output)
        self.assertIn("tests.providers.test_model_providers", output)
        self.assertIn("tests.storage.test_cow_providers", output)

    def test_hex_eval_cli_json_mode(self) -> None:
        """'hex eval --suite core --json' outputs structured JSON payload."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            exit_code = main(["eval", "--suite", "core", "--json"])
        output = buf.getvalue()

        self.assertEqual(exit_code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["suite"], "core")
        self.assertEqual(payload["status"], "PASSED")
        self.assertGreaterEqual(payload["total_tests"], 50)
        self.assertEqual(payload["failed"], 0)
        self.assertEqual(payload["errors"], 0)
        self.assertTrue(len(payload["modules"]) >= 4)

    def test_hex_eval_cli_invalid_suite(self) -> None:
        """'hex eval --suite nonexistent' fails gracefully."""
        buf = io.StringIO()
        # argparse choices check will print usage/error or eval_runner handles it
        try:
            with redirect_stdout(buf):
                exit_code = main(["eval", "--suite", "invalid_suite_xyz"])
            self.assertNotEqual(exit_code, 0)
        except SystemExit as exc:
            self.assertNotEqual(exc.code, 0)


if __name__ == "__main__":
    unittest.main()
