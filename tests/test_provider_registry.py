"""Unit tests for the HEX Multi-Provider Registry."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from harness_core.providers.registry import (
    CATEGORY_LABELS,
    PROVIDER_REGISTRY,
    ProviderSpec,
    get_all_provider_env_vars,
    get_provider_spec,
    list_provider_ids,
    list_providers,
)


class TestProviderRegistry(unittest.TestCase):
    """Test suite verifying the canonical provider registry completeness and functionality."""

    def test_registry_contains_minimum_providers(self) -> None:
        """Verify that all major providers (at least 25) are registered."""
        providers = list_providers()
        self.assertGreaterEqual(len(providers), 25)

        expected_slugs = {
            "antigravity",
            "openrouter",
            "anthropic",
            "openai",
            "gemini",
            "deepseek",
            "groq",
            "xai",
            "mistral",
            "together",
            "cerebras",
            "fireworks",
            "nvidia",
            "alibaba",
            "minimax",
            "kimi",
            "zai",
            "ai-gateway",
            "huggingface",
            "azure",
            "bedrock",
            "vertex",
            "ollama",
            "lmstudio",
            "local",
            "custom",
        }
        registered_slugs = set(list_provider_ids())
        missing = expected_slugs - registered_slugs
        self.assertEqual(missing, set(), f"Missing registered providers: {missing}")

    def test_provider_specs_validity(self) -> None:
        """Verify each provider spec has valid fields and transports."""
        valid_transports = {"openai_chat", "anthropic_messages", "gemini_rest", "antigravity", "local"}
        valid_categories = {"featured", "fast_cloud", "aggregator", "enterprise", "local"}

        for p in list_providers():
            self.assertTrue(p.id, "Provider id must not be empty")
            self.assertTrue(p.name, f"Provider name must not be empty for {p.id}")
            self.assertEqual(p.id, p.id.lower().strip())
            self.assertIn(p.category, valid_categories, f"Invalid category {p.category} for {p.id}")
            self.assertIn(p.transport, valid_transports, f"Invalid transport {p.transport} for {p.id}")
            if p.transport != "local":
                self.assertTrue(p.default_base_url.startswith(("http://", "https://")), f"Invalid default_base_url for {p.id}")
            self.assertTrue(p.default_model, f"default_model must not be empty for {p.id}")
            self.assertGreater(len(p.curated_models), 0, f"curated_models must not be empty for {p.id}")

            if p.requires_api_key:
                self.assertGreater(len(p.env_vars), 0, f"env_vars must not be empty for {p.id}")

    def test_category_filters(self) -> None:
        """Verify filtering providers by category."""
        featured = list_providers(category="featured")
        self.assertTrue(any(p.id == "openrouter" for p in featured))
        self.assertTrue(any(p.id == "antigravity" for p in featured))

        fast_cloud = list_providers(category="fast_cloud")
        self.assertTrue(any(p.id == "groq" for p in fast_cloud))
        self.assertTrue(any(p.id == "cerebras" for p in fast_cloud))
        self.assertTrue(any(p.id == "mistral" for p in fast_cloud))

        enterprise = list_providers(category="enterprise")
        self.assertTrue(any(p.id == "azure" for p in enterprise))
        self.assertTrue(any(p.id == "bedrock" for p in enterprise))

        aggregators = list_providers(category="aggregator")
        self.assertTrue(any(p.id == "ai-gateway" for p in aggregators))
        self.assertTrue(any(p.id == "huggingface" for p in aggregators))

        local = list_providers(category="local")
        self.assertTrue(any(p.id == "ollama" for p in local))
        self.assertTrue(any(p.id == "lmstudio" for p in local))
        self.assertTrue(any(p.id == "custom" for p in local))

    def test_api_key_resolution_priority(self) -> None:
        """Verify API key resolution follows custom creds then environment fallback chain."""
        spec = get_provider_spec("deepseek")
        self.assertIsNotNone(spec)
        assert spec is not None

        # 1. Custom creds takes top priority
        custom_creds = {"DEEPSEEK_API_KEY": "sk-custom-deepseek-123"}
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-env-deepseek-456"}):
            resolved = spec.resolve_api_key(custom_creds)
            self.assertEqual(resolved, "sk-custom-deepseek-123")

        # 2. Environment variable fallback
        with patch.dict(os.environ, {"HEX_DEEPSEEK_API_KEY": "sk-hex-deepseek-789"}, clear=True):
            resolved = spec.resolve_api_key({})
            self.assertEqual(resolved, "sk-hex-deepseek-789")

    def test_base_url_resolution(self) -> None:
        """Verify base URL resolution respects overrides."""
        spec = get_provider_spec("custom")
        self.assertIsNotNone(spec)
        assert spec is not None

        # Default
        self.assertEqual(spec.resolve_base_url({}), "http://localhost:8000/v1")

        # Overridden via custom creds
        self.assertEqual(
            spec.resolve_base_url({"OPENAI_BASE_URL": "http://192.168.1.50:8080/v1"}),
            "http://192.168.1.50:8080/v1",
        )

    def test_all_provider_env_vars_collected(self) -> None:
        """Verify get_all_provider_env_vars returns unique environment keys."""
        env_vars = get_all_provider_env_vars()
        self.assertIn("OPENROUTER_API_KEY", env_vars)
        self.assertIn("DEEPSEEK_API_KEY", env_vars)
        self.assertIn("GROQ_API_KEY", env_vars)
        self.assertIn("ANTHROPIC_API_KEY", env_vars)
        self.assertIn("OPENAI_API_KEY", env_vars)
        self.assertIn("GEMINI_API_KEY", env_vars)

    def test_cli_provider_and_base_url_flags(self) -> None:
        """Verify CLI accepts --provider and --base-url across top-level and subcommands."""
        from harness_core.cli import build_parser

        parser = build_parser()

        # Top-level flags
        args = parser.parse_args(["--provider", "groq", "--base-url", "https://custom.endpoint/v1"])
        self.assertEqual(args.provider, "groq")
        self.assertEqual(args.base_url, "https://custom.endpoint/v1")

        # Setup subcommand flags
        args_setup = parser.parse_args(["setup", "model", "--provider", "deepseek", "--model", "deepseek-chat"])
        self.assertEqual(args_setup.cmd, "setup")
        self.assertEqual(args_setup.stage, "model")
        self.assertEqual(args_setup.provider, "deepseek")
        self.assertEqual(args_setup.model, "deepseek-chat")

        # Run subcommand flags
        args_run = parser.parse_args(["run", "Optimize task", "--provider", "cerebras", "--base-url", "https://api.cerebras.ai/v1"])
        self.assertEqual(args_run.cmd, "run")
        self.assertEqual(args_run.provider, "cerebras")
        self.assertEqual(args_run.base_url, "https://api.cerebras.ai/v1")


if __name__ == "__main__":
    unittest.main()
