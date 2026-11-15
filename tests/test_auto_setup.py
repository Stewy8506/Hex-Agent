"""Unit test suite for Autonomous Agent Self-Setup and Environment Discovery."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness_core.auto_setup import (
    EnvironmentSnapshot,
    detect_environment,
    execute_auto_setup,
    synthesize_optimal_config,
)
from harness_core.config_loader import parse_config_yaml, parse_env_file


class TestAutoSetup(unittest.TestCase):
    """Test environment discovery and optimal configuration synthesis."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_synthesize_antigravity_optimal_config(self) -> None:
        """When Antigravity IDE OAuth session exists, it is selected with included quota."""
        env = EnvironmentSnapshot(
            os_name="Darwin",
            arch="arm64",
            machine_model="Apple M4",
            is_apple_silicon=True,
            has_metal=True,
            has_antigravity_ide=True,
            antigravity_token="ya29.test_token",
            antigravity_source="ide_vscdb",
        )
        cfg = synthesize_optimal_config(env)
        self.assertEqual(cfg["model"]["cloud_provider"], "antigravity")
        self.assertEqual(cfg["model"]["cloud_model_id"], "gemini-2.5-pro")
        self.assertEqual(cfg["model"]["reasoning_effort"], "low")
        self.assertEqual(cfg["model"]["compressor_mode"], "deterministic")
        self.assertEqual(cfg["model"]["profile"], "antigravity_hybrid")
        self.assertEqual(cfg["web_search"]["provider"], "duckduckgo")

    def test_synthesize_anthropic_when_key_present(self) -> None:
        """When Anthropic API key is available, Claude 3.7 with medium reasoning is selected."""
        env = EnvironmentSnapshot(
            os_name="Linux",
            arch="x86_64",
            anthropic_key="sk-ant-test-key-1234",
            tavily_key="tvly-test-key",
            telegram_bot_token="123456:ABC-DEF",
        )
        cfg = synthesize_optimal_config(env)
        self.assertEqual(cfg["model"]["cloud_provider"], "anthropic")
        self.assertEqual(cfg["model"]["cloud_model_id"], "claude-3-7-sonnet")
        self.assertEqual(cfg["model"]["reasoning_effort"], "medium")
        self.assertEqual(cfg["web_search"]["provider"], "tavily")
        self.assertTrue(cfg["messaging"]["telegram"]["enabled"])
        self.assertEqual(cfg["messaging"]["telegram"]["bot_token"], "123456:ABC-DEF")

    def test_synthesize_local_offline_when_ollama_active(self) -> None:
        """When no cloud keys exist but local Ollama is active, offline model is selected."""
        env = EnvironmentSnapshot(
            os_name="Linux",
            arch="x86_64",
            ollama_running=True,
            ollama_models=["llama3.2:latest", "qwen2.5-coder:7b"],
        )
        cfg = synthesize_optimal_config(env)
        self.assertEqual(cfg["model"]["cloud_provider"], "ollama")
        self.assertEqual(cfg["model"]["cloud_model_id"], "llama3.2:latest")
        self.assertEqual(cfg["model"]["profile"], "local")
        self.assertEqual(cfg["model"]["compressor_mode"], "local")

    def test_execute_auto_setup_writes_clean_files(self) -> None:
        """execute_auto_setup creates config.yaml and chmod 0600 credentials.env."""
        env = EnvironmentSnapshot(
            os_name="Darwin",
            arch="arm64",
            is_apple_silicon=True,
            anthropic_key="sk-ant-live",
            discord_bot_token="discord-tok-789",
        )
        res = execute_auto_setup(config_dir=self.root, custom_env=env)
        self.assertEqual(res["status"], "success")

        cfg_file = self.root / "config.yaml"
        cred_file = self.root / "credentials.env"
        self.assertTrue(cfg_file.is_file())
        self.assertTrue(cred_file.is_file())

        parsed_cfg = parse_config_yaml(cfg_file)
        self.assertEqual(parsed_cfg["model"]["cloud_provider"], "anthropic")
        self.assertEqual(parsed_cfg["model"]["cloud_model_id"], "claude-3-7-sonnet")
        self.assertEqual(parsed_cfg["model"]["reasoning_effort"], "medium")
        self.assertEqual(parsed_cfg["messaging"]["discord_enabled"], "true")

        parsed_creds = parse_env_file(cred_file)
        self.assertEqual(parsed_creds["HEX_CLOUD_PROVIDER"], "anthropic")
        self.assertEqual(parsed_creds["ANTHROPIC_API_KEY"], "sk-ant-live")
        self.assertEqual(parsed_creds["HEX_DISCORD_BOT_TOKEN"], "discord-tok-789")

    def test_run_onboard_wizard_auto_mode(self) -> None:
        """run_onboard_wizard with auto_mode=True executes headless auto-setup."""
        from harness_core.onboard import run_onboard_wizard

        env = EnvironmentSnapshot(
            os_name="Darwin",
            arch="arm64",
            is_apple_silicon=True,
            openai_key="sk-openai-live",
        )
        with patch("harness_core.auto_setup.detect_environment", return_value=env):
            res = run_onboard_wizard(config_dir=self.root, auto_mode=True)
            self.assertEqual(res["status"], "success")
            self.assertEqual(res["synthesized"]["model"]["cloud_provider"], "openai")
            self.assertEqual(res["synthesized"]["model"]["cloud_model_id"], "gpt-4o")
            self.assertTrue((self.root / "config.yaml").is_file())
            self.assertTrue((self.root / "credentials.env").is_file())


if __name__ == "__main__":
    unittest.main()
