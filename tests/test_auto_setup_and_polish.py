"""Tests for First-Run Auto-Setup Wizard, Startup Interceptor, and Harness Polish."""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness_core.config_loader import has_active_credentials, parse_env_file
from harness_core.onboard import HardwareProfile, run_onboard_wizard
from harness_core.orchestrator import HarnessOrchestrator
from harness_core.providers.cloud_provider import (
    CloudModelProvider,
    CloudProviderType,
    ProviderUnavailableError,
)
from harness_core.tui import render_hex_splash, render_hex_splash


class TestAutoSetupAndPolish(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_env = dict(os.environ)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cfg_dir = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        os.environ.clear()
        os.environ.update(self._orig_env)

    def test_onboard_wizard_defaults_only(self) -> None:
        result = run_onboard_wizard(
            config_dir=self.cfg_dir,
            non_interactive=True,
            defaults_only=True,
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["cloud_provider"], "antigravity")
        self.assertEqual(result["web_search_provider"], "duckduckgo")

        creds_file = self.cfg_dir / "credentials.env"
        self.assertTrue(creds_file.is_file())
        creds = parse_env_file(creds_file)
        self.assertEqual(creds.get("HEX_WEB_SEARCH_PROVIDER"), "duckduckgo")
        self.assertEqual(creds.get("HEX_CLOUD_PROVIDER"), "antigravity")

    @patch("harness_core.onboard.fetch_available_models")
    @patch("harness_core.onboard.prompt_secret")
    @patch("harness_core.onboard.select_option")
    def test_onboard_wizard_interactive_anthropic_and_tavily(
        self,
        mock_select: MagicMock,
        mock_prompt: MagicMock,
        mock_fetch: MagicMock,
    ) -> None:
        mock_fetch.return_value = [("claude-3-5-sonnet", "Claude 3.5 Sonnet")]
        # Step 1: Anthropic (idx 1)
        # Step 1b: Model claude-3-5-sonnet (idx 0)
        # Step 1c: Compressor (idx 1 -> cloud)
        # Step 2: Tavily (idx 1)
        # Step 3: Skip messaging (idx 0)
        mock_select.side_effect = [1, 0, 1, 1, 0]
        mock_prompt.side_effect = ["sk-ant-test-key-999", "tvly-test-key-888"]

        result = run_onboard_wizard(
            config_dir=self.cfg_dir,
            non_interactive=False,
            defaults_only=False,
        )
        self.assertEqual(result["cloud_provider"], "anthropic")
        self.assertEqual(result["web_search_provider"], "tavily")

        creds = parse_env_file(self.cfg_dir / "credentials.env")
        self.assertEqual(creds.get("ANTHROPIC_API_KEY"), "sk-ant-test-key-999")
        self.assertEqual(creds.get("TAVILY_API_KEY"), "tvly-test-key-888")
        self.assertEqual(creds.get("HEX_CLOUD_PROVIDER"), "anthropic")

    def test_hardware_profile_detection(self) -> None:
        prof = HardwareProfile.detect()
        self.assertIn(prof.os_name, ("Darwin", "Linux", "Windows"))
        self.assertGreater(prof.ram_gb, 0.0)
        self.assertIsInstance(prof.machine_model, str)

    def test_orchestrator_dynamic_worker_id(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            orch = HarnessOrchestrator(data_dir=td, default_workspace=td)
            self.assertTrue(orch.default_worker_id.startswith("wrk_"))
            self.assertTrue(orch.default_worker_id.endswith("_local"))
            worker = orch.ledger.get_worker(orch.default_worker_id)
            self.assertIsNotNone(worker)
            self.assertIn("ram_gb", worker["capabilities"])

    def test_tui_splash_contains_dynamic_version_and_tools(self) -> None:
        splash = render_hex_splash(
            model_id="claude-3-5-sonnet",
            compressor_mode="local",
            workspace_root=Path.cwd(),
        )
        self.assertIn("HEX Autonomous AI Workbench", splash)
        self.assertIn("web_search", splash)
        self.assertNotIn("v1.3.0", splash)

    def test_cli_auto_intercept_unconfigured(self) -> None:
        from harness_core.cli import main

        with patch.object(sys, "argv", ["hex", "chat"]):
            with patch("sys.stdin.isatty", return_value=True):
                with patch("harness_core.config_loader.has_active_credentials", return_value=False):
                    with patch("harness_core.onboard.run_onboard_wizard") as mock_wizard:
                        with patch("harness_core.tui.HexConsole.start", return_value=0):
                            with patch("sys.stdout", new_callable=io.StringIO):
                                ret = main(["chat", "--data-dir", str(self.cfg_dir)])
                                self.assertEqual(ret, 0)
                                mock_wizard.assert_called_once()

    def test_cloud_provider_missing_key_raises_unavailable(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            prov = CloudModelProvider(
                provider_type=CloudProviderType.ANTHROPIC,
                model_id="claude-3-5-sonnet",
                api_key=None,
            )
            with self.assertRaises(ProviderUnavailableError):
                prov.generate("test prompt")


if __name__ == "__main__":
    unittest.main()
