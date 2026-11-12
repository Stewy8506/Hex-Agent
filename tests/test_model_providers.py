"""Unit and contract tests for ModelProvider base protocols, LocalMLXModelProvider, and Tier1LogCompressor."""

from __future__ import annotations

import json
import platform
import sys
import unittest
from unittest.mock import MagicMock, patch

from harness_core.models import TaskBudget
from harness_core.providers.base import (
    BudgetExceededError,
    ModelProvider,
    ModelProviderError,
    ModelResponse,
    ModelUsage,
    PolicyBlockedError,
    ProviderCapabilities,
    ProviderUnavailableError,
)
from harness_core.providers.cloud_provider import (
    CloudModelProvider,
    CloudProviderType,
    ModelPricing,
    TaskBudgetTracker,
    calculate_cost_usd,
)
from harness_core.providers.mlx_provider import (
    LocalMLXModelProvider,
    Tier1LogCompressor,
)


class TestModelProviderBase(unittest.TestCase):
    """Test base dataclasses, protocols, and exceptions."""

    def test_model_usage_arithmetic_and_serialization(self) -> None:
        u1 = ModelUsage(input_tokens=100, output_tokens=50, cost_usd=0.002)
        u2 = ModelUsage(input_tokens=200, output_tokens=80, cost_usd=0.003)
        self.assertEqual(u1.total_tokens, 150)
        self.assertEqual(u2.total_tokens, 280)

        summed = u1.add(u2)
        self.assertEqual(summed.input_tokens, 300)
        self.assertEqual(summed.output_tokens, 130)
        self.assertEqual(summed.total_tokens, 430)
        self.assertAlmostEqual(summed.cost_usd, 0.005, places=5)

        data = summed.to_dict()
        self.assertEqual(data["input_tokens"], 300)
        self.assertEqual(data["output_tokens"], 130)
        self.assertEqual(data["total_tokens"], 430)
        self.assertEqual(data["cost_usd"], 0.005)

    def test_model_response_serialization(self) -> None:
        usage = ModelUsage(input_tokens=25, output_tokens=15, cost_usd=0.0)
        resp = ModelResponse(
            text="Synthetic generation output",
            model_id="test-model",
            provider_id="test-provider",
            usage=usage,
            latency_ms=15.42,
            finish_reason="stop",
            raw={"custom": "field"},
        )
        data = resp.to_dict()
        self.assertEqual(data["text"], "Synthetic generation output")
        self.assertEqual(data["model_id"], "test-model")
        self.assertEqual(data["provider_id"], "test-provider")
        self.assertEqual(data["latency_ms"], 15.42)
        self.assertEqual(data["finish_reason"], "stop")
        self.assertEqual(data["usage"]["total_tokens"], 40)
        self.assertEqual(data["raw"], {"custom": "field"})

    def test_provider_capabilities(self) -> None:
        caps = ProviderCapabilities(
            supports_system_prompt=True,
            supports_streaming=False,
            max_context_window=32768,
            quantization="4bit",
            local_unified_memory=True,
        )
        data = caps.to_dict()
        self.assertTrue(data["supports_system_prompt"])
        self.assertFalse(data["supports_streaming"])
        self.assertEqual(data["max_context_window"], 32768)
        self.assertEqual(data["quantization"], "4bit")
        self.assertTrue(data["local_unified_memory"])

    def test_custom_exceptions_inheritance(self) -> None:
        self.assertTrue(issubclass(PolicyBlockedError, ModelProviderError))
        self.assertTrue(issubclass(BudgetExceededError, ModelProviderError))
        self.assertTrue(issubclass(ProviderUnavailableError, ModelProviderError))


class TestLocalMLXModelProvider(unittest.TestCase):
    """Test LocalMLXModelProvider hardware discovery, inference, and fallback."""

    def test_apple_silicon_detection(self) -> None:
        provider = LocalMLXModelProvider()

        with patch("sys.platform", "darwin"), patch("platform.machine", return_value="arm64"):
            self.assertTrue(provider.is_apple_silicon())

        with patch("sys.platform", "darwin"), patch("platform.machine", return_value="aarch64"):
            self.assertTrue(provider.is_apple_silicon())

        with patch("sys.platform", "linux"), patch("platform.machine", return_value="x86_64"):
            self.assertFalse(provider.is_apple_silicon())

        with patch("sys.platform", "win32"), patch("platform.machine", return_value="AMD64"):
            self.assertFalse(provider.is_apple_silicon())

        with patch("sys.platform", "darwin"), patch("platform.machine", return_value="x86_64"):
            self.assertFalse(provider.is_apple_silicon())

    def test_mlx_availability_missing_dependency(self) -> None:
        provider = LocalMLXModelProvider()
        with patch.object(provider, "is_apple_silicon", return_value=True):
            with patch("importlib.util.find_spec", return_value=None):
                self.assertFalse(provider.is_mlx_available())

    def test_is_available_requires_loaded_model(self) -> None:
        provider = LocalMLXModelProvider()
        self.assertFalse(provider.is_available())

    def test_capabilities(self) -> None:
        provider = LocalMLXModelProvider(model_id="mlx-community/Qwen2.5-Coder-7B-Instruct-4bit")
        caps = provider.get_capabilities()
        self.assertEqual(caps.quantization, "4bit")
        self.assertEqual(caps.max_context_window, 32768)
        self.assertTrue(caps.supports_system_prompt)

    def test_generate_fallback_when_mlx_absent(self) -> None:
        provider = LocalMLXModelProvider()
        with patch.object(provider, "is_mlx_available", return_value=False):
            resp = provider.generate(
                prompt="Triage compilation error in main.py",
                system_prompt="You are an autonomous triage agent.",
            )
            self.assertIsInstance(resp, ModelResponse)
            self.assertIn("not loaded or weights are not downloaded", resp.text)
            self.assertEqual(resp.usage.cost_usd, 0.0)
            self.assertEqual(resp.provider_id, "local_mlx")
            self.assertEqual(resp.finish_reason, "stop")
            self.assertTrue(resp.raw.get("fallback"))
            self.assertFalse(resp.raw.get("mlx_available"))

    def test_generate_with_mocked_mlx(self) -> None:
        provider = LocalMLXModelProvider()
        mock_model = MagicMock()
        mock_tokenizer = MagicMock()
        mock_load = MagicMock(return_value=(mock_model, mock_tokenizer))
        mock_mlx_gen = MagicMock(return_value="Synthesized MLX explanation for failure.")

        with patch.object(provider, "is_mlx_available", return_value=True):
            with patch.dict(
                "sys.modules",
                {"mlx_lm": MagicMock(load=mock_load, generate=mock_mlx_gen)},
            ):
                resp = provider.generate(
                    prompt="Explain why test_math failed",
                    system_prompt="System instructions",
                    max_tokens=256,
                )
                self.assertEqual(resp.text, "Synthesized MLX explanation for failure.")
                self.assertEqual(resp.usage.cost_usd, 0.0)
                self.assertEqual(resp.provider_id, "local_mlx")
                self.assertTrue(resp.raw.get("mlx"))


class TestTier1LogCompressor(unittest.TestCase):
    """Test live context and log compression engine."""

    def setUp(self) -> None:
        self.compressor = Tier1LogCompressor(mlx_available=False)

    def test_synthetic_verbose_pytest_trace_token_savings(self) -> None:
        passing_lines = "\n".join(
            [f"tests/test_{i}.py .................................................... [ {int(i / 150 * 100)}%]" for i in range(150)]
        )
        failing_trace = (
            "\n=================================== FAILURES ===================================\n"
            "___________________________________ test_calc ___________________________________\n"
            "Traceback (most recent call last):\n"
            '  File "tests/test_math.py", line 42, in test_calc\n'
            "    assert result == 10\n"
            "AssertionError: expected 10 got 20\n"
            "=========================== short test summary info ============================\n"
            "FAILED tests/test_math.py::test_calc - AssertionError: expected 10 got 20\n"
            "========================= 1 failed, 149 passed in 1.23s ========================\n"
        )
        raw_log = passing_lines + failing_trace
        res = self.compressor.compress(raw_log)

        # >70% token savings requirement
        self.assertGreaterEqual(res["token_savings_pct"], 70.0)
        self.assertIn("tests/test_math.py", res["failing_files"])
        self.assertTrue(
            any("expected 10 got 20" in a or "AssertionError" in a for a in res["failing_assertions"]),
            f"Expected assertion in {res['failing_assertions']}",
        )
        self.assertIn("tests/test_math.py:42", res["compressed_text"])
        self.assertIn("test_calc", res["compressed_text"])

        # Passing noise must be stripped
        self.assertNotIn("test_0.py", res["compressed_text"])
        self.assertNotIn("test session starts", res["compressed_text"])

    def test_multi_failure_log(self) -> None:
        raw_log = (
            "FAILED tests/test_billing.py:88: test_checkout\n"
            "    assert charge_status == 'success'\n"
            "AssertionError: charge failed\n\n"
            "FAILED tests/test_auth.py:24: test_jwt\n"
            "    raise PermissionError('invalid token')\n"
            "PermissionError: invalid token\n"
        )
        res = self.compressor.compress(raw_log)

        self.assertIn("tests/test_billing.py", res["failing_files"])
        self.assertIn("tests/test_auth.py", res["failing_files"])
        self.assertIn("test_checkout", res["compressed_text"])
        self.assertIn("test_jwt", res["compressed_text"])
        self.assertIn("tests/test_billing.py:88", res["compressed_text"])
        self.assertIn("tests/test_auth.py:24", res["compressed_text"])

    def test_secret_redaction(self) -> None:
        raw_log = (
            "FAILED test_api.py:12: test_connect\n"
            "AssertionError: failed with key sk-ant-api03-abcdefghijklmnop and github token ghp_12345678901234567890\n"
        )
        res = self.compressor.compress(raw_log)
        self.assertNotIn("sk-ant-api03-abcdefghijklmnop", res["compressed_text"])
        self.assertNotIn("ghp_12345678901234567890", res["compressed_text"])
        self.assertIn("[REDACTED_SECRET]", res["compressed_text"])

    def test_short_and_empty_logs(self) -> None:
        empty_res = self.compressor.compress("")
        self.assertEqual(empty_res["failing_files"], [])
        self.assertEqual(empty_res["failing_assertions"], [])
        self.assertEqual(empty_res["token_savings_pct"], 0.0)
        self.assertIn("Empty log trace", empty_res["compressed_text"])

        short_res = self.compressor.compress("short build warning: unused variable")
        self.assertEqual(short_res["failing_files"], [])
        self.assertGreaterEqual(short_res["token_savings_pct"], 0.0)

    def test_provider_compress_log_delegation(self) -> None:
        provider = LocalMLXModelProvider()
        raw = "FAILED test_math.py:10: test_foo\n    assert 1 == 2\nAssertionError: assert 1 == 2"
        res = provider.compress_log(raw)
        self.assertIn("test_math.py", res["failing_files"])
        self.assertTrue(res["engine"].startswith("tier1_mlx"))


class TestTaskBudgetTracker(unittest.TestCase):
    """Test task budget tracking, limit enforcement, and pricing calculators."""

    def test_pricing_calculation(self) -> None:
        # Claude 3.5 Sonnet ($3.00 input, $15.00 output per 1M)
        cost_claude = calculate_cost_usd("claude-3-5-sonnet-20241022", 1000, 1000)
        self.assertAlmostEqual(cost_claude, 0.018, places=5)

        # GPT-4o ($2.50 input, $10.00 output per 1M)
        cost_gpt4o = calculate_cost_usd("gpt-4o", 10000, 1000)
        self.assertAlmostEqual(cost_gpt4o, 0.035, places=5)

        # Gemini 1.5 Pro ($1.25 input, $5.00 output per 1M)
        cost_gemini15 = calculate_cost_usd("gemini-1.5-pro", 10000, 1000)
        self.assertAlmostEqual(cost_gemini15, 0.0175, places=5)

        # Gemini 3.5 Pro ($1.50 input, $6.00 output per 1M)
        cost_gemini35 = calculate_cost_usd("gemini-3.5-pro", 10000, 1000)
        self.assertAlmostEqual(cost_gemini35, 0.021, places=5)

    def test_budget_tracker_request_limit(self) -> None:
        tracker = TaskBudgetTracker(max_requests=2)
        tracker.check_budget_before_call()
        tracker.record_usage(10, 10, 0.001)  # request 1
        tracker.check_budget_before_call()
        tracker.record_usage(10, 10, 0.001)  # request 2

        with self.assertRaises(BudgetExceededError) as ctx:
            tracker.check_budget_before_call()
        self.assertIn("request count limit exceeded", str(ctx.exception))

    def test_budget_tracker_cost_ceiling(self) -> None:
        tracker = TaskBudgetTracker(max_cost_usd=0.05)
        tracker.record_usage(100, 100, 0.03)

        with self.assertRaises(BudgetExceededError) as ctx:
            tracker.record_usage(100, 100, 0.03)
        self.assertIn("cost ceiling exceeded", str(ctx.exception))

    def test_budget_tracker_output_token_limit(self) -> None:
        tracker = TaskBudgetTracker(max_output_tokens=100)
        tracker.record_usage(50, 60, 0.001)

        with self.assertRaises(BudgetExceededError) as ctx:
            tracker.record_usage(50, 50, 0.001)
        self.assertIn("output token limit exceeded", str(ctx.exception))


class TestCloudModelProvider(unittest.TestCase):
    """Test CloudModelProvider dispatch, privacy gating, and outbound secret redaction."""

    def test_anthropic_payload_dispatch(self) -> None:
        captured_req: dict[str, Any] = {}

        def mock_transport(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
            captured_req["url"] = url
            captured_req["headers"] = headers
            captured_req["payload"] = json.loads(body.decode("utf-8"))
            resp = {
                "content": [{"type": "text", "text": "Anthropic Claude synthesis"}],
                "usage": {"input_tokens": 120, "output_tokens": 45},
                "stop_reason": "end_turn",
            }
            return 200, json.dumps(resp).encode("utf-8")

        provider = CloudModelProvider(
            provider_type=CloudProviderType.ANTHROPIC,
            model_id="claude-3-5-sonnet-20241022",
            api_key="sk-ant-test-key",
            http_transport=mock_transport,
        )

        resp = provider.generate(
            prompt="Analyze architectural trade-offs",
            system_prompt="You are a principal engineer.",
            max_tokens=256,
        )

        self.assertEqual(captured_req["url"], "https://api.anthropic.com/v1/messages")
        self.assertEqual(captured_req["headers"]["x-api-key"], "sk-ant-test-key")
        self.assertEqual(captured_req["headers"]["anthropic-version"], "2023-06-01")
        self.assertEqual(captured_req["payload"]["model"], "claude-3-5-sonnet-20241022")
        self.assertEqual(captured_req["payload"]["system"], "You are a principal engineer.")
        self.assertEqual(captured_req["payload"]["messages"][0]["content"], "Analyze architectural trade-offs")
        self.assertEqual(resp.text, "Anthropic Claude synthesis")
        self.assertEqual(resp.usage.input_tokens, 120)
        self.assertEqual(resp.usage.output_tokens, 45)
        self.assertGreater(resp.usage.cost_usd, 0.0)

    def test_openai_payload_dispatch(self) -> None:
        captured_req: dict[str, Any] = {}

        def mock_transport(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
            captured_req["url"] = url
            captured_req["headers"] = headers
            captured_req["payload"] = json.loads(body.decode("utf-8"))
            resp = {
                "choices": [{
                    "message": {"role": "assistant", "content": "OpenAI GPT-4o synthesis"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 80, "completion_tokens": 30},
            }
            return 200, json.dumps(resp).encode("utf-8")

        provider = CloudModelProvider(
            provider_type=CloudProviderType.OPENAI,
            model_id="gpt-4o",
            api_key="sk-openai-test-key",
            http_transport=mock_transport,
        )

        resp = provider.generate(
            prompt="Optimize SQL query execution plan",
            system_prompt="You are a database tuning expert.",
            max_tokens=512,
        )

        self.assertEqual(captured_req["url"], "https://api.openai.com/v1/chat/completions")
        self.assertEqual(captured_req["headers"]["Authorization"], "Bearer sk-openai-test-key")
        self.assertEqual(captured_req["payload"]["model"], "gpt-4o")
        self.assertEqual(captured_req["payload"]["messages"][0]["role"], "system")
        self.assertEqual(captured_req["payload"]["messages"][1]["content"], "Optimize SQL query execution plan")
        self.assertEqual(resp.text, "OpenAI GPT-4o synthesis")
        self.assertEqual(resp.usage.input_tokens, 80)
        self.assertEqual(resp.usage.output_tokens, 30)

    def test_gemini_payload_dispatch(self) -> None:
        captured_req: dict[str, Any] = {}

        def mock_transport(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
            captured_req["url"] = url
            captured_req["headers"] = headers
            captured_req["payload"] = json.loads(body.decode("utf-8"))
            resp = {
                "candidates": [{
                    "content": {
                        "parts": [{"text": "Google Gemini 3.5 Pro reasoning"}],
                        "role": "model",
                    },
                    "finishReason": "STOP",
                }],
                "usageMetadata": {
                    "promptTokenCount": 90,
                    "candidatesTokenCount": 40,
                },
            }
            return 200, json.dumps(resp).encode("utf-8")

        provider = CloudModelProvider(
            provider_type=CloudProviderType.GEMINI,
            model_id="gemini-3.5-pro",
            api_key="test-gemini-key",
            http_transport=mock_transport,
        )

        resp = provider.generate(
            prompt="Explain lock contention in Postgres",
            system_prompt="Be concise.",
            max_tokens=300,
        )

        self.assertIn("gemini-3.5-pro:generateContent", captured_req["url"])
        self.assertIn("key=test-gemini-key", captured_req["url"])
        self.assertEqual(
            captured_req["payload"]["contents"][0]["parts"][0]["text"],
            "Explain lock contention in Postgres",
        )
        self.assertEqual(resp.text, "Google Gemini 3.5 Pro reasoning")
        self.assertEqual(resp.usage.input_tokens, 90)
        self.assertEqual(resp.usage.output_tokens, 40)

    def test_privacy_gating_blocks_sensitive_and_secret(self) -> None:
        transport_called = False

        def mock_transport(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
            nonlocal transport_called
            transport_called = True
            return 200, b"{}"

        provider = CloudModelProvider(http_transport=mock_transport)

        with self.assertRaises(PolicyBlockedError) as ctx_sens:
            provider.generate("Internal customer transaction records", data_classification="sensitive")
        self.assertIn("Cloud egress forbidden for data classification 'sensitive'", str(ctx_sens.exception))

        with self.assertRaises(PolicyBlockedError) as ctx_sec:
            provider.generate("Root CA private keys", data_classification="secret")
        self.assertIn("Cloud egress forbidden for data classification 'secret'", str(ctx_sec.exception))

        self.assertFalse(transport_called, "Zero network requests must be initiated for sensitive/secret classifications")

    def test_outbound_secret_redaction(self) -> None:
        captured_payloads: list[dict[str, Any]] = []

        def mock_transport(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
            payload = json.loads(body.decode("utf-8"))
            captured_payloads.append(payload)
            resp = {
                "candidates": [{
                    "content": {"parts": [{"text": "Sanitized response"}]},
                    "finishReason": "STOP",
                }],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
            }
            return 200, json.dumps(resp).encode("utf-8")

        provider = CloudModelProvider(
            provider_type=CloudProviderType.GEMINI,
            api_key="safe-mock-key",
            http_transport=mock_transport,
        )

        raw_prompt = "Failed with token sk-ant-api03-1234567890abcdef12345 and password = 'SuperSecretPassword!'"
        raw_system = "Admin session with Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.token.signature"

        provider.generate(
            prompt=raw_prompt,
            system_prompt=raw_system,
            data_classification="internal",
        )

        self.assertEqual(len(captured_payloads), 1)
        raw_body_text = json.dumps(captured_payloads[0])
        self.assertNotIn("sk-ant-api03-1234567890abcdef12345", raw_body_text)
        self.assertNotIn("SuperSecretPassword!", raw_body_text)
        self.assertIn("[REDACTED_SECRET]", raw_body_text)

    def test_budget_ceiling_enforcement_in_generate(self) -> None:
        def mock_transport(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
            resp = {
                "candidates": [{
                    "content": {"parts": [{"text": "Simulated response"}]},
                    "finishReason": "STOP",
                }],
                "usageMetadata": {"promptTokenCount": 10000, "candidatesTokenCount": 5000},
            }
            return 200, json.dumps(resp).encode("utf-8")

        tracker = TaskBudgetTracker(max_cost_usd=0.05)
        provider = CloudModelProvider(
            provider_type=CloudProviderType.GEMINI,
            model_id="gemini-3.5-pro",
            budget_tracker=tracker,
            http_transport=mock_transport,
        )

        # 1st call: 10k in, 5k out on gemini-3.5-pro = (10000*1.5 + 5000*6.0)/1M = 0.045 USD
        resp1 = provider.generate("First query")
        self.assertAlmostEqual(resp1.usage.cost_usd, 0.045, places=5)

        # 2nd call: adding another 0.045 breaches 0.05 USD ceiling
        with self.assertRaises(BudgetExceededError) as ctx:
            provider.generate("Second query")
        self.assertIn("cost ceiling exceeded", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

