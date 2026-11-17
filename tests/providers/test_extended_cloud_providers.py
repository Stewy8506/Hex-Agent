"""Unit tests for the extended CloudModelProvider protocol handling (OpenAI Chat, Anthropic Messages, Gemini REST)."""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import MagicMock, patch

from harness_core.providers.cloud_provider import (
    CloudModelProvider,
    CloudProviderType,
    ProviderUnavailableError,
    fetch_available_models,
)


class TestExtendedCloudProviders(unittest.TestCase):
    """Test suite verifying request building and response parsing across all transports."""

    def test_openai_chat_request_and_response(self) -> None:
        """Verify OpenAI Chat completions request building and response parsing for OpenRouter / DeepSeek / Groq."""
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test-key"}):
            provider = CloudModelProvider(
                provider_type="openrouter",
                model_id="deepseek/deepseek-r1",
            )
            self.assertEqual(provider.model_id, "deepseek/deepseek-r1")
            self.assertEqual(provider.base_url, "https://openrouter.ai/api/v1")

            url, headers, body_bytes = provider._build_request(
                clean_prompt="Write a Python hello world function.",
                system_prompt="You are an expert engineer.",
                temperature=0.2,
                max_tokens=500,
                stop_sequences=None,
            )
            self.assertEqual(url, "https://openrouter.ai/api/v1/chat/completions")
            # Headers are normalized lowercase in internal builder
            norm_headers = {k.lower(): v for k, v in headers.items()}
            self.assertEqual(norm_headers["authorization"], "Bearer sk-or-test-key")
            self.assertEqual(norm_headers["content-type"], "application/json")

            payload = json.loads(body_bytes.decode("utf-8"))
            self.assertEqual(payload["model"], "deepseek/deepseek-r1")
            self.assertEqual(payload["temperature"], 0.2)
            self.assertEqual(payload["max_tokens"], 500)
            self.assertEqual(len(payload["messages"]), 2)
            self.assertEqual(payload["messages"][0]["role"], "system")
            self.assertEqual(payload["messages"][1]["role"], "user")

            mock_response_data = {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "def hello():\n    return 'Hello, World!'",
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 15,
                    "completion_tokens": 25,
                    "total_tokens": 40,
                },
            }

            text, in_tok, out_tok, cached_tok, finish = provider._parse_response(
                mock_response_data, prompt="Write a Python hello world function."
            )
            self.assertIn("def hello()", text)
            self.assertEqual(in_tok, 15)
            self.assertEqual(out_tok, 25)

    def test_anthropic_messages_request_and_response(self) -> None:
        """Verify Anthropic Messages request format for Anthropic / MiniMax."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test-key"}):
            provider = CloudModelProvider(
                provider_type="anthropic",
                model_id="claude-3-5-sonnet-20241022",
            )
            url, headers, body_bytes = provider._build_request(
                clean_prompt="Refactor this code.",
                system_prompt="Concise refactoring instructions.",
                temperature=0.0,
                max_tokens=1000,
                stop_sequences=None,
            )
            self.assertEqual(url, "https://api.anthropic.com/v1/messages")
            norm_headers = {k.lower(): v for k, v in headers.items()}
            self.assertEqual(norm_headers["x-api-key"], "sk-ant-test-key")
            self.assertEqual(norm_headers["anthropic-version"], "2023-06-01")

            payload = json.loads(body_bytes.decode("utf-8"))
            self.assertEqual(payload["model"], "claude-3-5-sonnet-20241022")
            self.assertEqual(payload["system"], "Concise refactoring instructions.")
            self.assertEqual(payload["messages"], [{"role": "user", "content": "Refactor this code."}])

            mock_response_data = {
                "content": [{"type": "text", "text": "// Refactored solution"}],
                "usage": {
                    "input_tokens": 30,
                    "output_tokens": 45,
                },
            }

            text, in_tok, out_tok, cached_tok, finish = provider._parse_response(
                mock_response_data, prompt="Refactor this code."
            )
            self.assertEqual(text, "// Refactored solution")
            self.assertEqual(in_tok, 30)
            self.assertEqual(out_tok, 45)

    def test_custom_endpoint_override(self) -> None:
        """Verify that a custom endpoint with base_url overrides default endpoint."""
        with patch.dict(os.environ, {"HEX_CUSTOM_API_KEY": "sk-proxy-key"}):
            provider = CloudModelProvider(
                provider_type="custom",
                model_id="meta-llama/Llama-3.3-70B-Instruct",
                base_url="http://10.0.0.5:8000/v1",
            )
            url, headers, body_bytes = provider._build_request(
                clean_prompt="Hello server",
                system_prompt=None,
                temperature=0.7,
                max_tokens=256,
                stop_sequences=None,
            )
            self.assertEqual(url, "http://10.0.0.5:8000/v1/chat/completions")
            norm_headers = {k.lower(): v for k, v in headers.items()}
            self.assertEqual(norm_headers["authorization"], "Bearer sk-proxy-key")

    def test_fetch_available_models_network_failure(self) -> None:
        """Verify fetch_available_models raises ProviderUnavailableError on network failure."""
        with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")):
            with self.assertRaises(ProviderUnavailableError):
                fetch_available_models("groq", api_key="test-key")

    def test_missing_api_key_raises_unavailable(self) -> None:
        """Verify unconfigured provider raises ProviderUnavailableError on generation."""
        with patch.dict(os.environ, {}, clear=True):
            provider = CloudModelProvider(
                provider_type="deepseek",
                model_id="deepseek-chat",
            )
            with self.assertRaises(ProviderUnavailableError):
                provider.generate(prompt="Test prompt")


if __name__ == "__main__":
    unittest.main()
