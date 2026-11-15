"""Unit tests for Model Reasoning Effort Control & Wire Clamping Engine."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness_core.config_loader import (
    _MODEL_SELECTION_ENV_KEYS,
    load_hex_environment,
    load_reasoning_effort,
)
from harness_core.onboard import update_config_yaml_model
from harness_core.providers.cloud_provider import CloudModelProvider, CloudProviderType
from harness_core.reasoning_effort import (
    ANTHROPIC_EFFORTS,
    DEEPSEEK_EFFORTS,
    EFFORT_LADDER,
    GEMINI_EFFORTS,
    OPENAI_EFFORTS,
    clamp_effort,
    effort_to_anthropic_thinking,
    effort_to_gemini_thinking,
    get_provider_supported_efforts,
    model_supports_reasoning,
    resolve_reasoning_effort,
)
from harness_core.router import TwoTierModelRouter
from harness_core.tui import HexConsole


class TestReasoningEffortLadderAndVocabularies(unittest.TestCase):
    """Test canonical ladder, provider vocabularies, and provider mapping."""

    def test_canonical_ladder_contents(self) -> None:
        self.assertEqual(
            EFFORT_LADDER,
            ("none", "minimal", "low", "medium", "high", "xhigh", "max"),
        )
        self.assertEqual(len(EFFORT_LADDER), 7)

    def test_provider_vocabulary_sets(self) -> None:
        self.assertEqual(OPENAI_EFFORTS, ("low", "medium", "high"))
        self.assertEqual(ANTHROPIC_EFFORTS, ("none", "low", "medium", "high", "max"))
        self.assertEqual(GEMINI_EFFORTS, ("none", "low", "medium", "high"))
        self.assertEqual(DEEPSEEK_EFFORTS, ("none", "low", "medium", "high", "max"))

    def test_get_provider_supported_efforts(self) -> None:
        self.assertEqual(get_provider_supported_efforts("openai"), OPENAI_EFFORTS)
        self.assertEqual(get_provider_supported_efforts("azure"), OPENAI_EFFORTS)
        self.assertEqual(get_provider_supported_efforts("anthropic"), ANTHROPIC_EFFORTS)
        self.assertEqual(get_provider_supported_efforts("claude"), ANTHROPIC_EFFORTS)
        self.assertEqual(get_provider_supported_efforts("gemini"), GEMINI_EFFORTS)
        self.assertEqual(get_provider_supported_efforts("antigravity"), GEMINI_EFFORTS)
        self.assertEqual(get_provider_supported_efforts("deepseek"), DEEPSEEK_EFFORTS)


class TestReasoningEffortClamping(unittest.TestCase):
    """Test nearest-level clamping engine preventing HTTP 400 Bad Request errors."""

    def test_openai_clamping(self) -> None:
        # OpenAI supports ("low", "medium", "high")
        # Exact matches
        self.assertEqual(clamp_effort("low", OPENAI_EFFORTS), "low")
        self.assertEqual(clamp_effort("medium", OPENAI_EFFORTS), "medium")
        self.assertEqual(clamp_effort("high", OPENAI_EFFORTS), "high")

        # Nearest weaker
        self.assertEqual(clamp_effort("xhigh", OPENAI_EFFORTS), "high")
        self.assertEqual(clamp_effort("max", OPENAI_EFFORTS), "high")

        # No weaker exists -> lowest supported level
        self.assertEqual(clamp_effort("minimal", OPENAI_EFFORTS), "low")
        self.assertEqual(clamp_effort("none", OPENAI_EFFORTS), "low")

    def test_anthropic_clamping(self) -> None:
        # Anthropic supports ("none", "low", "medium", "high", "max")
        self.assertEqual(clamp_effort("none", ANTHROPIC_EFFORTS), "none")
        self.assertEqual(clamp_effort("low", ANTHROPIC_EFFORTS), "low")
        self.assertEqual(clamp_effort("medium", ANTHROPIC_EFFORTS), "medium")
        self.assertEqual(clamp_effort("high", ANTHROPIC_EFFORTS), "high")
        self.assertEqual(clamp_effort("max", ANTHROPIC_EFFORTS), "max")

        # "minimal" -> weaker is "none"
        self.assertEqual(clamp_effort("minimal", ANTHROPIC_EFFORTS), "none")
        # "xhigh" -> weaker is "high"
        self.assertEqual(clamp_effort("xhigh", ANTHROPIC_EFFORTS), "high")

    def test_gemini_clamping(self) -> None:
        # Gemini supports ("none", "low", "medium", "high")
        self.assertEqual(clamp_effort("none", GEMINI_EFFORTS), "none")
        self.assertEqual(clamp_effort("low", GEMINI_EFFORTS), "low")
        self.assertEqual(clamp_effort("medium", GEMINI_EFFORTS), "medium")
        self.assertEqual(clamp_effort("high", GEMINI_EFFORTS), "high")

        # "minimal" -> weaker is "none"
        self.assertEqual(clamp_effort("minimal", GEMINI_EFFORTS), "none")
        # "xhigh" -> weaker is "high"
        self.assertEqual(clamp_effort("xhigh", GEMINI_EFFORTS), "high")
        # "max" -> weaker is "high"
        self.assertEqual(clamp_effort("max", GEMINI_EFFORTS), "high")

    def test_deepseek_clamping(self) -> None:
        # DeepSeek supports ("none", "low", "medium", "high", "max")
        self.assertEqual(clamp_effort("max", DEEPSEEK_EFFORTS), "max")
        self.assertEqual(clamp_effort("xhigh", DEEPSEEK_EFFORTS), "high")
        self.assertEqual(clamp_effort("minimal", DEEPSEEK_EFFORTS), "none")

    def test_case_insensitivity_and_aliases(self) -> None:
        self.assertEqual(clamp_effort("HIGH", OPENAI_EFFORTS), "high")
        self.assertEqual(clamp_effort("  Medium  ", OPENAI_EFFORTS), "medium")
        self.assertEqual(clamp_effort("off", ANTHROPIC_EFFORTS), "none")
        self.assertEqual(clamp_effort("disabled", GEMINI_EFFORTS), "none")
        self.assertEqual(clamp_effort("default", OPENAI_EFFORTS), "medium")
        self.assertEqual(clamp_effort("maximum", DEEPSEEK_EFFORTS), "max")


class TestAnthropicThinkingTranslation(unittest.TestCase):
    """Test translating reasoning efforts to Anthropic Claude 3.7 thinking payloads."""

    def test_anthropic_effort_budgets(self) -> None:
        self.assertEqual(
            effort_to_anthropic_thinking("low"),
            {"type": "enabled", "budget_tokens": 2048},
        )
        self.assertEqual(
            effort_to_anthropic_thinking("medium"),
            {"type": "enabled", "budget_tokens": 8192},
        )
        self.assertEqual(
            effort_to_anthropic_thinking("high"),
            {"type": "enabled", "budget_tokens": 16384},
        )
        self.assertEqual(
            effort_to_anthropic_thinking("max"),
            {"type": "enabled", "budget_tokens": 32768},
        )

    def test_anthropic_none_disables_thinking(self) -> None:
        self.assertIsNone(effort_to_anthropic_thinking("none"))
        self.assertIsNone(effort_to_anthropic_thinking("off"))
        self.assertIsNone(effort_to_anthropic_thinking("disabled"))
        self.assertIsNone(effort_to_anthropic_thinking(""))
        self.assertIsNone(effort_to_anthropic_thinking(None))  # type: ignore[arg-type]

    def test_anthropic_xhigh_clamped_to_high(self) -> None:
        self.assertEqual(
            effort_to_anthropic_thinking("xhigh"),
            {"type": "enabled", "budget_tokens": 16384},
        )


class TestGeminiThinkingTranslation(unittest.TestCase):
    """Test translating reasoning efforts to Gemini thinkingConfig payloads."""

    def test_gemini_effort_budgets(self) -> None:
        self.assertEqual(
            effort_to_gemini_thinking("low"),
            {"thinkingBudget": 2048},
        )
        self.assertEqual(
            effort_to_gemini_thinking("medium"),
            {"thinkingBudget": 8192},
        )
        self.assertEqual(
            effort_to_gemini_thinking("high"),
            {"thinkingBudget": 16384},
        )

    def test_gemini_none_budget_zero(self) -> None:
        # None sets thinkingBudget to 0 to disable thinking on Gemini 2.5 Flash
        self.assertEqual(effort_to_gemini_thinking("none"), {"thinkingBudget": 0})
        self.assertEqual(effort_to_gemini_thinking("off"), {"thinkingBudget": 0})
        self.assertEqual(effort_to_gemini_thinking("disabled"), {"thinkingBudget": 0})

    def test_gemini_max_clamped_to_high(self) -> None:
        self.assertEqual(
            effort_to_gemini_thinking("max"),
            {"thinkingBudget": 16384},
        )


class TestModelSupportsReasoning(unittest.TestCase):
    """Test detection of models supporting extended thinking / reasoning."""

    def test_openai_models(self) -> None:
        self.assertTrue(model_supports_reasoning("openai", "o1"))
        self.assertTrue(model_supports_reasoning("openai", "o1-mini"))
        self.assertTrue(model_supports_reasoning("openai", "o1-preview"))
        self.assertTrue(model_supports_reasoning("openai", "o3"))
        self.assertTrue(model_supports_reasoning("openai", "o3-mini"))
        self.assertTrue(model_supports_reasoning("openai", "gpt-5.6-preview"))
        self.assertFalse(model_supports_reasoning("openai", "gpt-4o"))
        self.assertFalse(model_supports_reasoning("openai", "gpt-4o-mini"))

    def test_anthropic_models(self) -> None:
        self.assertTrue(model_supports_reasoning("anthropic", "claude-3-7-sonnet-20250219"))
        self.assertTrue(model_supports_reasoning("anthropic", "claude-3.7-sonnet"))
        self.assertFalse(model_supports_reasoning("anthropic", "claude-3-5-sonnet-20241022"))
        self.assertFalse(model_supports_reasoning("anthropic", "claude-3-5-haiku-20241022"))

    def test_gemini_models(self) -> None:
        self.assertTrue(model_supports_reasoning("gemini", "gemini-2.5-pro"))
        self.assertTrue(model_supports_reasoning("gemini", "gemini-2.5-flash"))
        self.assertTrue(model_supports_reasoning("gemini", "gemini-2.0-flash-thinking-exp"))
        self.assertFalse(model_supports_reasoning("gemini", "gemini-2.0-flash"))
        self.assertFalse(model_supports_reasoning("gemini", "gemini-1.5-pro"))

    def test_antigravity_models(self) -> None:
        self.assertTrue(model_supports_reasoning("antigravity", "claude-opus-4-6-thinking"))
        self.assertTrue(model_supports_reasoning("antigravity", "gemini-2.5-pro"))
        self.assertTrue(model_supports_reasoning("antigravity", "gemini-2.5-flash"))

    def test_deepseek_models(self) -> None:
        self.assertTrue(model_supports_reasoning("deepseek", "deepseek-reasoner"))
        self.assertTrue(model_supports_reasoning("openrouter", "deepseek/deepseek-r1"))
        self.assertFalse(model_supports_reasoning("deepseek", "deepseek-chat"))


class TestResolveReasoningEffort(unittest.TestCase):
    """Test resolution of active reasoning effort."""

    def test_defaults_to_medium_for_reasoning_models(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(resolve_reasoning_effort("openai", "o3-mini"), "medium")
            self.assertEqual(resolve_reasoning_effort("anthropic", "claude-3-7-sonnet-20250219"), "medium")
            self.assertEqual(resolve_reasoning_effort("gemini", "gemini-2.5-pro"), "medium")

    def test_defaults_to_none_for_non_reasoning_models(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(resolve_reasoning_effort("openai", "gpt-4o"), "none")
            self.assertEqual(resolve_reasoning_effort("anthropic", "claude-3-5-sonnet-20241022"), "none")

    def test_explicit_effort_overrides_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(resolve_reasoning_effort("openai", "o3-mini", explicit_effort="high"), "high")
            self.assertEqual(resolve_reasoning_effort("openai", "gpt-4o", explicit_effort="low"), "low")

    def test_environment_variable_overrides_default(self) -> None:
        with patch.dict(os.environ, {"HEX_REASONING_EFFORT": "xhigh"}):
            self.assertEqual(resolve_reasoning_effort("openai", "o3-mini"), "xhigh")


class TestRouterPayloadFormatting(unittest.TestCase):
    """Test router wire payload formatting across OpenAI, Anthropic, Gemini, and Antigravity."""

    def setUp(self) -> None:
        self.router = TwoTierModelRouter(cloud_provider_type="openai", cloud_model_id="o3-mini")

    def test_format_payload_openai_o_series(self) -> None:
        raw_payload = {"model": "o3-mini", "messages": [{"role": "user", "content": "solve"}]}
        formatted = self.router._format_cloud_payload("openai", "o3-mini", raw_payload, effort="high")
        self.assertEqual(formatted.get("reasoning_effort"), "high")

    def test_format_payload_openai_non_reasoning(self) -> None:
        raw_payload = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hello"}]}
        formatted = self.router._format_cloud_payload("openai", "gpt-4o", raw_payload, effort="high")
        self.assertNotIn("reasoning_effort", formatted)

    def test_format_payload_anthropic_claude_37(self) -> None:
        raw_payload = {"model": "claude-3-7-sonnet-20250219", "messages": [{"role": "user", "content": "analyze"}]}
        formatted = self.router._format_cloud_payload("anthropic", "claude-3-7-sonnet-20250219", raw_payload, effort="high")
        self.assertEqual(formatted.get("thinking"), {"type": "enabled", "budget_tokens": 16384})

    def test_format_payload_anthropic_non_reasoning(self) -> None:
        raw_payload = {"model": "claude-3-5-sonnet-20241022", "messages": [{"role": "user", "content": "analyze"}]}
        formatted = self.router._format_cloud_payload("anthropic", "claude-3-5-sonnet-20241022", raw_payload, effort="high")
        self.assertNotIn("thinking", formatted)

    def test_format_payload_gemini_25(self) -> None:
        raw_payload = {
            "contents": [{"role": "user", "parts": [{"text": "query"}]}],
            "generationConfig": {"maxOutputTokens": 4096},
        }
        formatted = self.router._format_cloud_payload("gemini", "gemini-2.5-pro", raw_payload, effort="medium")
        self.assertIn("thinkingConfig", formatted.get("generationConfig", {}))
        self.assertEqual(formatted["generationConfig"]["thinkingConfig"], {"thinkingBudget": 8192})

    def test_router_set_reasoning_effort(self) -> None:
        self.router.set_reasoning_effort("max")
        self.assertEqual(self.router.reasoning_effort, "max")


class TestProviderRequestWireGeneration(unittest.TestCase):
    """Test actual wire payload generation in CloudModelProvider and AntigravityModelProvider."""

    def test_openai_wire_request_includes_reasoning_effort(self) -> None:
        provider = CloudModelProvider(
            provider_type=CloudProviderType.OPENAI,
            model_id="o3-mini",
            api_key="mock-key",
            reasoning_effort="high",
        )
        url, headers, body_bytes = provider._build_request("Test reasoning prompt")
        body = json.loads(body_bytes.decode("utf-8"))
        self.assertEqual(body.get("reasoning_effort"), "high")

    def test_anthropic_wire_request_includes_thinking(self) -> None:
        provider = CloudModelProvider(
            provider_type=CloudProviderType.ANTHROPIC,
            model_id="claude-3-7-sonnet-20250219",
            api_key="mock-key",
            reasoning_effort="medium",
        )
        url, headers, body_bytes = provider._build_request("Test reasoning prompt")
        body = json.loads(body_bytes.decode("utf-8"))
        self.assertEqual(body.get("thinking"), {"type": "enabled", "budget_tokens": 8192})

    def test_gemini_wire_request_includes_thinking_config(self) -> None:
        provider = CloudModelProvider(
            provider_type=CloudProviderType.GEMINI,
            model_id="gemini-2.5-pro",
            api_key="mock-key",
            reasoning_effort="low",
        )
        url, headers, body_bytes = provider._build_request("Test reasoning prompt")
        body = json.loads(body_bytes.decode("utf-8"))
        gen_cfg = body.get("generationConfig", {})
        self.assertEqual(gen_cfg.get("thinkingConfig"), {"thinkingBudget": 2048})


class TestConfigLoaderReasoningEffort(unittest.TestCase):
    """Test parsing and hydrating reasoning effort in config_loader."""

    def test_model_selection_env_keys_includes_reasoning_effort(self) -> None:
        self.assertIn("HEX_REASONING_EFFORT", _MODEL_SELECTION_ENV_KEYS)

    def test_load_hex_environment_parses_reasoning_effort(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg_dir = Path(td)
            cfg_path = cfg_dir / "config.yaml"
            cfg_path.write_text(
                """version: "1.0"
model:
  cloud_provider: "anthropic"
  cloud_model_id: "claude-3-7-sonnet-20250219"
  reasoning_effort: "xhigh"
""",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                load_hex_environment(config_dir=cfg_dir, override=True)
                self.assertEqual(os.environ.get("HEX_REASONING_EFFORT"), "xhigh")

    def test_load_reasoning_effort_helper(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg_dir = Path(td)
            cfg_path = cfg_dir / "config.yaml"
            cfg_path.write_text(
                """version: "1.0"
model:
  cloud_provider: "openai"
  cloud_model_id: "o3-mini"
  reasoning_effort: "high"
""",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                eff = load_reasoning_effort(config_dir=cfg_dir)
                self.assertEqual(eff, "high")


class TestOnboardConfigYamlWiring(unittest.TestCase):
    """Test update_config_yaml_model and onboarding with reasoning effort."""

    def test_update_config_yaml_model_writes_reasoning_effort(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "config.yaml"
            success = update_config_yaml_model(
                cfg_path,
                cloud_provider="openai",
                cloud_model_id="o3-mini",
                profile="cloud",
                reasoning_effort="high",
            )
            self.assertTrue(success)
            content = cfg_path.read_text(encoding="utf-8")
            self.assertIn('reasoning_effort: "high"', content)


class TestTUIReasoningSlashCommand(unittest.TestCase):
    """Test /reasoning command in HexConsole interactive TUI."""

    def setUp(self) -> None:
        self.mock_orch = MagicMock()
        self.mock_orch.router = TwoTierModelRouter(cloud_provider_type="anthropic", cloud_model_id="claude-3-7-sonnet-20250219")
        self.console = HexConsole(self.mock_orch)

    def test_slash_commands_list_includes_reasoning(self) -> None:
        self.assertIn("/reasoning", HexConsole.SLASH_COMMANDS)

    def test_cmd_reasoning_inspect_output(self) -> None:
        f = io.StringIO()
        with patch("sys.stdout", f):
            self.console._cmd_reasoning("")
        out = f.getvalue()
        self.assertIn("Model Reasoning Effort Telemetry", out)
        self.assertIn("Active Level:", out)

    def test_cmd_reasoning_update_valid_level(self) -> None:
        f = io.StringIO()
        with patch("sys.stdout", f):
            self.console._cmd_reasoning("max")
        out = f.getvalue()
        self.assertIn("Reasoning effort updated to: max", out)
        self.assertEqual(self.mock_orch.router.reasoning_effort, "max")
        self.assertEqual(os.environ.get("HEX_REASONING_EFFORT"), "max")

    def test_cmd_reasoning_unknown_level(self) -> None:
        f = io.StringIO()
        with patch("sys.stdout", f):
            self.console._cmd_reasoning("super_ultra")
        out = f.getvalue()
        self.assertIn("Unknown reasoning effort", out)


if __name__ == "__main__":
    unittest.main()
