"""Unit tests for Hermes-parity model metadata and context window resolution."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness_core.models.model_metadata import (
    DEFAULT_CONTEXT_LENGTHS,
    fetch_models_dev,
    format_context_tokens,
    get_model_context_length,
    lookup_models_dev_context,
)
from harness_core.client.tui_launcher import build_tui_cmd_args


class TestModelMetadata(unittest.TestCase):
    def test_longest_prefix_matching(self) -> None:
        self.assertEqual(get_model_context_length("gemma-4-31b-it"), 256_000)
        self.assertEqual(get_model_context_length("gemma-4-26b-a4b-it"), 256_000)
        self.assertEqual(get_model_context_length("gemini-2.5-pro"), 1_048_576)
        self.assertEqual(get_model_context_length("gemini-2.5-flash"), 1_048_576)
        self.assertEqual(get_model_context_length("gemini-3.8-flash-tiered"), 1_048_576)
        self.assertEqual(get_model_context_length("claude-3-5-sonnet-20241022"), 200_000)
        self.assertEqual(get_model_context_length("claude-sonnet-4-6"), 1_000_000)
        self.assertEqual(get_model_context_length("claude-sonnet-4.6"), 1_000_000)
        self.assertEqual(get_model_context_length("gpt-4o"), 128_000)
        self.assertEqual(get_model_context_length("deepseek-chat"), 1_000_000)
        self.assertEqual(get_model_context_length("meta-llama/llama-3.3-70b-instruct"), 131_072)
        self.assertEqual(get_model_context_length("grok-4.6"), 500_000)
        self.assertEqual(get_model_context_length("kimi-k3"), 1_048_576)
        self.assertEqual(get_model_context_length("solar-mini"), 32_768)
        self.assertEqual(get_model_context_length("unknown-custom-model"), 256_000)

    def test_explicit_config_override(self) -> None:
        self.assertEqual(get_model_context_length("gemma-4-31b-it", config_context_length=65_536), 65_536)
        self.assertEqual(get_model_context_length("gemini-2.5-pro", config_context_length=500_000), 500_000)

    def test_format_context_tokens(self) -> None:
        self.assertEqual(format_context_tokens(256_000), "256k")
        self.assertEqual(format_context_tokens(200_000), "200k")
        self.assertEqual(format_context_tokens(128_000), "128k")
        self.assertEqual(format_context_tokens(1_000_000), "1M")
        self.assertEqual(format_context_tokens(1_048_576), "1.0M")
        self.assertEqual(format_context_tokens(2_097_152), "2.1M")

    @patch("harness_core.models.model_metadata.fetch_models_dev")
    def test_models_dev_lookup(self, mock_fetch: MagicMock) -> None:
        mock_fetch.return_value = {
            "google": {
                "models": {
                    "custom-gemma-special": {
                        "limit": {"context": 512_000}
                    }
                }
            }
        }
        self.assertEqual(lookup_models_dev_context("google", "custom-gemma-special"), 512_000)
        self.assertEqual(get_model_context_length("custom-gemma-special", provider="google"), 512_000)

    def test_tui_launcher_args_with_max_tokens(self) -> None:
        args = MagicMock()
        args.check = False
        args.project = "test-proj"
        args.workspace = "/tmp/test"
        args.model = "gemma-4-31b-it"
        args.provider = "gemini"
        args.skin = None
        args.cow = "OverlayFS"
        args.session = None
        args.max_tokens = None
        args.context_window = None

        cmd = build_tui_cmd_args(args, Path("/usr/local/bin/hex-tui"))
        self.assertIn("--model", cmd)
        self.assertIn("gemma-4-31b-it", cmd)
        self.assertIn("--max-tokens", cmd)
        # Either 262144 (from live models.dev cache) or 256000 (from static canonical table)
        self.assertTrue("256000" in cmd or "262144" in cmd)


if __name__ == "__main__":
    unittest.main()
