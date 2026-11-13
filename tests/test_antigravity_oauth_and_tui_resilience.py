"""Unit and Integration Tests for Antigravity OAuth, Model Selection, and TUI Resilience."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness_core.onboard import _probe_antigravity_token, run_onboard_wizard
from harness_core.providers.antigravity_provider import AntigravityCredential, AntigravityModelProvider
from harness_core.providers.base import ProviderUnavailableError
from harness_core.tui import HexConsole


class TestAntigravityOAuthAndTuiResilience(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_env = dict(os.environ)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cfg_dir = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        os.environ.clear()
        os.environ.update(self._orig_env)

    def test_probe_antigravity_token_empty(self) -> None:
        self.assertFalse(_probe_antigravity_token(""))
        self.assertFalse(_probe_antigravity_token("   "))

    @patch("urllib.request.urlopen")
    def test_probe_antigravity_token_success(self, mock_urlopen: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_resp
        self.assertTrue(_probe_antigravity_token("valid_token_xyz"))

    @patch("urllib.request.urlopen")
    def test_probe_antigravity_token_failure(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = Exception("HTTP 401 Unauthorized")
        self.assertFalse(_probe_antigravity_token("expired_token_123"))

    @patch("harness_core.providers.antigravity_provider.AntigravityOAuthManager.resolve_credential")
    @patch("harness_core.onboard.fetch_available_models", return_value=[("gemini-2.5-pro", "Gemini 2.5 Pro"), ("gemini-2.5-flash", "Gemini 2.5 Flash")])
    @patch("harness_core.onboard.prompt_secret")
    @patch("harness_core.onboard.select_option")
    @patch("harness_core.onboard._probe_antigravity_token", return_value=True)
    def test_onboard_wizard_antigravity_model_selection(
        self,
        mock_probe: MagicMock,
        mock_select: MagicMock,
        mock_prompt: MagicMock,
        mock_fetch: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        mock_resolve.return_value = AntigravityCredential(
            access_token="ya29.existing_token_abc",
            refresh_token="1//existing_refresh_abc",
            expires_at_ms=9999999999000,
        )
        # Step 1: Antigravity provider (0)
        # Step 1a: Auth -> Keep (0)
        # Step 1b: Model selection -> gemini-2.5-flash (1)
        # Step 1c: Context compressor -> (0)
        # Step 2: Search -> Disabled (3)
        # Step 3: Messaging -> Skip (0)
        mock_select.side_effect = [0, 0, 1, 0, 3, 0]

        result = run_onboard_wizard(
            config_dir=self.cfg_dir,
            non_interactive=False,
            defaults_only=False,
        )

        self.assertEqual(result["cloud_provider"], "antigravity")
        self.assertEqual(result["cloud_model_id"], "gemini-2.5-flash")
        self.assertEqual(result["web_search_provider"], "none")

        # Verify config written
        cfg_text = (self.cfg_dir / "config.yaml").read_text(encoding="utf-8")
        self.assertIn('cloud_model_id: "gemini-2.5-flash"', cfg_text)

    @patch("harness_core.providers.antigravity_provider.AntigravityOAuthManager.resolve_credential", return_value=None)
    @patch("harness_core.onboard.fetch_available_models", return_value=[("gemini-2.5-pro", "Gemini 2.5 Pro")])
    @patch("builtins.input", return_value="http://localhost:8420/?code=auth_code_999")
    @patch("webbrowser.open")
    @patch("harness_core.providers.antigravity_provider.AntigravityOAuthManager.exchange_code_for_tokens")
    @patch("harness_core.onboard.prompt_secret")
    @patch("harness_core.onboard.select_option")
    @patch("harness_core.onboard._probe_antigravity_token", return_value=False)
    def test_onboard_wizard_triggers_browser_oauth_when_unauthenticated(
        self,
        mock_probe: MagicMock,
        mock_select: MagicMock,
        mock_prompt: MagicMock,
        mock_exchange: MagicMock,
        mock_browser: MagicMock,
        mock_input: MagicMock,
        mock_fetch: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        mock_cred = AntigravityCredential(
            access_token="new_browser_access_token_777",
            refresh_token="new_refresh_token_777",
            expires_at_ms=9999999999000,
        )
        mock_exchange.return_value = mock_cred

        # Step 1: Antigravity (0)
        # Step 1a: Auth option: Browser sign-in (0)
        # Step 1b: Model -> gemini-2.5-pro (0)
        # Step 1c: Context compressor -> (0)
        # Step 2: Search -> DuckDuckGo (0)
        # Step 3: Messaging -> Skip (0)
        mock_select.side_effect = [0, 0, 0, 0, 0, 0]

        result = run_onboard_wizard(
            config_dir=self.cfg_dir,
            non_interactive=False,
            defaults_only=False,
        )

        self.assertEqual(result["cloud_provider"], "antigravity")
        self.assertEqual(result["cloud_model_id"], "gemini-2.5-pro")
        mock_exchange.assert_called_once()
        mock_browser.assert_called_once()

    def test_tui_handle_user_prompt_catches_auth_error_gracefully(self) -> None:
        # Mock orchestrator that raises ProviderUnavailableError (HTTP 401)
        mock_orch = MagicMock()
        mock_orch.submit_and_run.side_effect = ProviderUnavailableError(
            "Google Antigravity returned HTTP 401: UNAUTHENTICATED"
        )
        mock_orch.default_workspace = str(self.cfg_dir)

        console = HexConsole(orchestrator=mock_orch, skin_name="amber")

        # Test that calling handle_user_prompt does not crash and prints error card
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            console.handle_user_prompt("hello")

        out = buf.getvalue()
        self.assertIn("Model Provider Error", out)
        self.assertIn("UNAUTHENTICATED", out)
        self.assertIn("hex auth antigravity", out)

    def test_antigravity_provider_refreshes_token_on_401(self) -> None:
        initial_cred = AntigravityCredential(
            access_token="stale_token_111",
            refresh_token="valid_refresh_token_222",
            expires_at_ms=9999999999000,
            project_id="test_proj",
        )

        refreshed_cred = AntigravityCredential(
            access_token="refreshed_token_333",
            refresh_token="valid_refresh_token_222",
            expires_at_ms=9999999999000,
            project_id="test_proj",
        )

        provider = AntigravityModelProvider(
            access_token=initial_cred.access_token,
            refresh_token=initial_cred.refresh_token,
            project_id="test_proj",
            model_id="gemini-2.5-pro",
        )
        provider.oauth_manager._from_hex_auth_file = MagicMock(return_value=None)
        provider.oauth_manager.refresh_access_token = MagicMock(return_value=refreshed_cred)
        provider.oauth_manager.save_credential = MagicMock()

        # Mock _execute_http: first call returns 401, second call returns 200 with response
        success_response = json.dumps({
            "response": {
                "candidates": [{
                    "content": {
                        "role": "model",
                        "parts": [{"text": "Hello from refreshed Gemini!"}],
                    },
                    "finishReason": "STOP",
                }]
            }
        }).encode("utf-8")

        call_count = 0

        def fake_execute(url: str, headers: dict, body: bytes, **kwargs) -> tuple[int, bytes]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return 401, b'{"error": {"code": 401, "message": "UNAUTHENTICATED"}}'
            return 200, success_response

        provider._execute_http = MagicMock(side_effect=fake_execute)

        result = provider.generate("test prompt")
        self.assertEqual(result.text, "Hello from refreshed Gemini!")
        self.assertEqual(call_count, 2)
        provider.oauth_manager.refresh_access_token.assert_called_once()
        provider.oauth_manager.save_credential.assert_called_once_with(refreshed_cred)


if __name__ == "__main__":
    unittest.main()
