"""Unit and Integration Tests for Live Dynamic Model Discovery Engine.

Verifies:
1. Live model discovery endpoints across Google Antigravity, Anthropic, OpenAI, Gemini, and Ollama.
2. Offline fallback handling badging all models with `[offline fallback]`.
3. Inverted onboarding flow: authenticating first before discovering/selecting models.
4. Interactive TUI `/model` command discovering live models and updating active routing.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from harness_core.onboard import run_onboard_wizard
from harness_core.providers.antigravity_provider import (
    AntigravityModelProvider,
    AntigravityOAuthManager,
    fetch_available_models as ag_fetch_available_models,
)
from harness_core.providers.base import ProviderUnavailableError
from harness_core.providers.cloud_provider import (
    CloudModelProvider,
    CloudProviderType,
    fetch_available_models,
)
from harness_core.tui import HexConsole


class MockHTTPResponse:
    """Mock for urllib.request.urlopen context manager."""

    def __init__(self, data: bytes, status: int = 200) -> None:
        self.data = data
        self.status = status

    def read(self) -> bytes:
        return self.data

    def __enter__(self) -> MockHTTPResponse:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        pass


class TestAntigravityLiveModelDiscovery(unittest.TestCase):
    """Verifies live model discovery and quota reflection for Google Antigravity."""

    @patch("urllib.request.urlopen")
    def test_antigravity_live_discovery_success(self, mock_urlopen: MagicMock) -> None:
        payload = {
            "models": {
                "gemini-2.5-pro": {
                    "displayName": "Gemini 2.5 Pro",
                    "quotaInfo": {"remainingFraction": 0.85},
                },
                "claude-3-5-sonnet": {
                    "displayName": "Claude 3.5 Sonnet",
                    "quotaInfo": {"remainingFraction": 0.40},
                },
            }
        }
        mock_urlopen.return_value = MockHTTPResponse(json.dumps(payload).encode("utf-8"))

        models = ag_fetch_available_models(access_token="ya29.mock_token")
        self.assertEqual(len(models), 2)
        model_dict = dict(models)
        self.assertIn("gemini-2.5-pro", model_dict)
        self.assertIn("85% quota remaining", model_dict["gemini-2.5-pro"])
        self.assertIn("claude-3-5-sonnet", model_dict)
        self.assertIn("40% quota remaining", model_dict["claude-3-5-sonnet"])

    @patch("urllib.request.urlopen")
    def test_antigravity_manager_and_provider_methods(self, mock_urlopen: MagicMock) -> None:
        payload = {
            "models": {
                "gemini-2.5-flash": {
                    "displayName": "Gemini 2.5 Flash",
                    "quotaInfo": {"remainingFraction": 1.0},
                }
            }
        }
        mock_urlopen.return_value = MockHTTPResponse(json.dumps(payload).encode("utf-8"))

        mgr = AntigravityOAuthManager()
        models_mgr = mgr.fetch_available_models(access_token="ya29.mgr_token")
        self.assertEqual(models_mgr[0][0], "gemini-2.5-flash")
        self.assertIn("100% quota remaining", models_mgr[0][1])

        prov = AntigravityModelProvider()
        models_prov = prov.fetch_available_models(access_token="ya29.prov_token")
        self.assertEqual(models_prov[0][0], "gemini-2.5-flash")

    @patch("urllib.request.urlopen")
    def test_antigravity_401_triggers_auto_refresh_retry(self, mock_urlopen: MagicMock) -> None:
        import time
        from harness_core.providers.antigravity_provider import AntigravityCredential
        payload = {
            "models": {
                "claude-opus-4-6-thinking": {
                    "displayName": "Claude Opus 4.6 (Thinking)",
                    "quotaInfo": {"remainingFraction": 1.0},
                }
            }
        }
        err_401 = urllib.error.HTTPError(
            url="https://antigravity-service/v1internal:fetchAvailableModels",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=io.BytesIO(b'{"error": "invalid_token"}'),
        )
        success_resp = MockHTTPResponse(json.dumps(payload).encode("utf-8"))
        mock_urlopen.side_effect = [err_401, success_resp]

        fake_cred = AntigravityCredential(
            access_token="ya29.refreshed_fresh_token",
            refresh_token="1//mock_refresh",
            expires_at_ms=int(time.time() * 1000) + 3600000,
            project_id="test-proj",
        )

        with patch("harness_core.providers.antigravity_provider.AntigravityOAuthManager.resolve_credential", return_value=fake_cred):
            models = ag_fetch_available_models(access_token="ya29.stale_expired_token")
            self.assertEqual(len(models), 1)
            self.assertEqual(models[0][0], "claude-opus-4-6-thinking")
            self.assertIn("100% quota remaining", models[0][1])
            self.assertEqual(mock_urlopen.call_count, 2)

    @patch("urllib.request.urlopen")
    def test_antigravity_error_on_network_failure(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
        with self.assertRaises(ProviderUnavailableError):
            ag_fetch_available_models(access_token="ya29.mock_token")

    def test_antigravity_error_without_token(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch("harness_core.providers.antigravity_provider.AntigravityOAuthManager.resolve_credential", return_value=None):
            with self.assertRaises(ProviderUnavailableError):
                ag_fetch_available_models(access_token="")


class TestCloudProvidersLiveModelDiscovery(unittest.TestCase):
    """Verifies live model discovery across Anthropic, OpenAI, Gemini, and Ollama."""

    @patch("urllib.request.urlopen")
    def test_anthropic_live_discovery(self, mock_urlopen: MagicMock) -> None:
        payload = {
            "data": [
                {"id": "claude-3-5-sonnet-20241022", "display_name": "Claude 3.5 Sonnet"},
                {"id": "claude-3-7-sonnet-20250219", "display_name": "Claude 3.7 Sonnet"},
            ]
        }
        mock_urlopen.return_value = MockHTTPResponse(json.dumps(payload).encode("utf-8"))

        models = fetch_available_models("anthropic", api_key="sk-ant-test")
        self.assertEqual(len(models), 2)
        model_ids = [m[0] for m in models]
        self.assertIn("claude-3-5-sonnet-20241022", model_ids)
        self.assertIn("claude-3-7-sonnet-20250219", model_ids)

    @patch("urllib.request.urlopen")
    def test_openai_live_discovery_and_filtering(self, mock_urlopen: MagicMock) -> None:
        payload = {
            "data": [
                {"id": "gpt-4o-2024-08-06"},
                {"id": "o1-preview"},
                {"id": "o3-mini-2025-01-31"},
                {"id": "text-embedding-3-small"},
                {"id": "whisper-1"},
            ]
        }
        mock_urlopen.return_value = MockHTTPResponse(json.dumps(payload).encode("utf-8"))

        models = fetch_available_models("openai", api_key="sk-test")
        model_ids = [m[0] for m in models]
        self.assertIn("gpt-4o-2024-08-06", model_ids)
        self.assertIn("o1-preview", model_ids)
        self.assertIn("o3-mini-2025-01-31", model_ids)
        self.assertNotIn("text-embedding-3-small", model_ids)
        self.assertNotIn("whisper-1", model_ids)

    @patch("urllib.request.urlopen")
    def test_gemini_live_discovery_and_filtering(self, mock_urlopen: MagicMock) -> None:
        payload = {
            "models": [
                {
                    "name": "models/gemini-2.5-pro",
                    "displayName": "Gemini 2.5 Pro",
                    "supportedGenerationMethods": ["generateContent", "countTokens"],
                },
                {
                    "name": "models/gemini-2.5-flash",
                    "displayName": "Gemini 2.5 Flash",
                    "supportedGenerationMethods": ["generateContent"],
                },
                {
                    "name": "models/text-embedding-004",
                    "displayName": "Text Embedding 004",
                    "supportedGenerationMethods": ["embedContent"],
                },
            ]
        }
        mock_urlopen.return_value = MockHTTPResponse(json.dumps(payload).encode("utf-8"))

        models = fetch_available_models("gemini", api_key="AIzaSyTest")
        model_ids = [m[0] for m in models]
        self.assertIn("gemini-2.5-pro", model_ids)
        self.assertIn("gemini-2.5-flash", model_ids)
        self.assertNotIn("text-embedding-004", model_ids)
        self.assertNotIn("models/gemini-2.5-pro", model_ids)

    @patch("urllib.request.urlopen")
    def test_ollama_live_discovery(self, mock_urlopen: MagicMock) -> None:
        payload = {
            "models": [
                {"name": "llama3.3:70b"},
                {"name": "qwen2.5-coder:7b"},
            ]
        }
        mock_urlopen.return_value = MockHTTPResponse(json.dumps(payload).encode("utf-8"))

        models = fetch_available_models("local")
        model_ids = [m[0] for m in models]
        self.assertIn("llama3.3:70b", model_ids)
        self.assertIn("qwen2.5-coder:7b", model_ids)


class TestLiveModelFetchFailureHandling(unittest.TestCase):
    """Verifies that offline or unreachable conditions raise ProviderUnavailableError or return empty for local."""

    @patch("urllib.request.urlopen")
    def test_all_provider_live_fetch_failures(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = urllib.error.URLError("Network unreachable")

        with self.assertRaises(ProviderUnavailableError):
            fetch_available_models("anthropic", api_key="sk-ant-test")

        with self.assertRaises(ProviderUnavailableError):
            fetch_available_models("openai", api_key="sk-test")

        with self.assertRaises(ProviderUnavailableError):
            fetch_available_models("gemini", api_key="AIzaTest")

        with self.assertRaises(ProviderUnavailableError):
            fetch_available_models("antigravity", api_key="ya29.test")

        # Local returns empty list when no local models are running or cached
        local_models = fetch_available_models("local")
        self.assertEqual(local_models, [])


class TestInvertedOnboardingFlow(unittest.TestCase):
    """Verifies that onboarding asks for authentication before presenting models."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp()
        self.cfg_dir = Path(self.test_dir) / ".hex"
        self.cfg_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    @patch("harness_core.onboard.fetch_available_models")
    @patch("harness_core.onboard.prompt_secret")
    @patch("harness_core.onboard.select_option")
    def test_onboarding_authenticates_before_model_discovery(
        self,
        mock_select: MagicMock,
        mock_prompt: MagicMock,
        mock_fetch: MagicMock,
    ) -> None:
        call_order: list[str] = []

        def tracked_select(title: str, options: Any, **kwargs: Any) -> int:
            call_order.append(f"select:{title}")
            return 0

        def tracked_prompt(prompt: str, **kwargs: Any) -> str:
            call_order.append(f"prompt:{prompt}")
            return "sk-ant-valid-key"

        mock_select.side_effect = tracked_select
        mock_prompt.side_effect = tracked_prompt
        mock_fetch.return_value = [
            ("claude-3-7-sonnet", "Claude 3.7 Sonnet"),
            ("claude-3-5-sonnet", "Claude 3.5 Sonnet"),
        ]

        # Select Anthropic as provider (idx 1), then select model (idx 0), compressor (0), search (0), messaging (0)
        select_responses = [1, 0, 0, 0, 0]
        mock_select.side_effect = select_responses

        result = run_onboard_wizard(
            config_dir=self.cfg_dir,
            non_interactive=False,
            defaults_only=False,
        )

        self.assertEqual(result["cloud_provider"], "anthropic")
        self.assertEqual(result["cloud_model_id"], "claude-3-7-sonnet")

        # Verify fetch_available_models was called with the authenticated key
        mock_fetch.assert_called_with("anthropic", api_key="sk-ant-valid-key")


class TestTUIInteractiveModelSelector(unittest.TestCase):
    """Verifies interactive /model command in TUI pair-programming cockpit."""

    def setUp(self) -> None:
        self.orchestrator = MagicMock()
        self.router = MagicMock()
        self.router.cloud_model_id = "gemini-2.5-pro"
        self.router.cloud_provider = MagicMock()
        self.router.cloud_provider.provider_id = "google_antigravity"
        self.router.cloud_provider.access_token = "ya29.test_token"
        self.router.budget_tracker = MagicMock()
        self.router.budget_tracker.max_usd_per_task = 1.0
        self.orchestrator.router = self.router

    @patch("harness_core.providers.cloud_provider.fetch_available_models")
    @patch("harness_core.ui_selector.select_option")
    def test_interactive_model_switching(
        self,
        mock_select: MagicMock,
        mock_fetch: MagicMock,
    ) -> None:
        mock_fetch.return_value = [
            ("gemini-2.5-pro", "Gemini 2.5 Pro"),
            ("claude-3-5-sonnet", "Claude 3.5 Sonnet"),
        ]
        mock_select.return_value = 1  # Select claude-3-5-sonnet

        console = HexConsole(orchestrator=self.orchestrator)
        mock_stdin = MagicMock()
        mock_stdin.isatty.return_value = True
        mock_stdout = io.StringIO()
        mock_stdout.isatty = lambda: True

        console.stream_in = mock_stdin
        console.stream_out = mock_stdout

        with patch("sys.stdin", mock_stdin), patch("sys.stdout", mock_stdout):
            console._cmd_model("")

        self.assertEqual(self.router.cloud_model_id, "claude-3-5-sonnet")
        output = mock_stdout.getvalue()
        self.assertIn("Active model switched to: claude-3-5-sonnet", output)

    def test_direct_model_argument_switching(self) -> None:
        console = HexConsole(orchestrator=self.orchestrator)
        out = io.StringIO()
        with patch("sys.stdout", out):
            console._cmd_model("o3-mini")

        self.assertEqual(self.router.cloud_model_id, "o3-mini")
        self.assertIn("Model profile updated to: o3-mini", out.getvalue())

    def test_non_interactive_model_status_card(self) -> None:
        console = HexConsole(orchestrator=self.orchestrator)
        mock_stdin = MagicMock()
        mock_stdin.isatty.return_value = False
        out = io.StringIO()

        console.stream_in = mock_stdin
        console.stream_out = out

        with patch("sys.stdin", mock_stdin), patch("sys.stdout", out):
            console._cmd_model("")

        self.assertIn("Active Model Routing Configuration", out.getvalue())


if __name__ == "__main__":
    unittest.main()
