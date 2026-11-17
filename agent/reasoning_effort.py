"""First-Class Model Reasoning Effort Control & Wire Clamping Engine.

Provides full parity with Hermes Agent reasoning controls:
1. Canonical reasoning ladder: none, minimal, low, medium, high, xhigh, max.
2. Provider-specific vocabulary clamping (OpenAI, Anthropic, Gemini, DeepSeek).
3. Wire translation to Anthropic Claude 3.7 thinking tokens and Gemini thinkingConfig.
4. Model capability detection for extended thinking / reasoning models.
"""

from __future__ import annotations

import os
from typing import Any, Sequence

# Canonical reasoning effort ladder across HEX
EFFORT_LADDER: tuple[str, ...] = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)

# Provider-specific wire vocabulary sets
OPENAI_EFFORTS: tuple[str, ...] = ("low", "medium", "high")
ANTHROPIC_EFFORTS: tuple[str, ...] = ("none", "low", "medium", "high", "max")
GEMINI_EFFORTS: tuple[str, ...] = ("none", "low", "medium", "high")
DEEPSEEK_EFFORTS: tuple[str, ...] = ("none", "low", "medium", "high", "max")

# Canonical aliases and normalizations
EFFORT_ALIASES: dict[str, str] = {
    "off": "none",
    "disabled": "none",
    "false": "none",
    "0": "none",
    "no": "none",
    "min": "minimal",
    "med": "medium",
    "default": "medium",
    "standard": "medium",
    "hi": "high",
    "extra-high": "xhigh",
    "extra_high": "xhigh",
    "maximum": "max",
    "full": "max",
}

# Anthropic thinking token budget mapping
ANTHROPIC_THINKING_BUDGETS: dict[str, int] = {
    "minimal": 1024,
    "low": 2048,
    "medium": 8192,
    "high": 16384,
    "xhigh": 24576,
    "max": 32768,
}

# Gemini thinkingConfig token budget mapping
GEMINI_THINKING_BUDGETS: dict[str, int] = {
    "none": 0,
    "minimal": 512,
    "low": 2048,
    "medium": 8192,
    "high": 16384,
    "xhigh": 24576,
    "max": 32768,
}


def get_provider_supported_efforts(provider: str) -> tuple[str, ...]:
    """Return the wire vocabulary of reasoning efforts supported by the provider."""
    p = (provider or "").strip().lower()
    if p in ("openai", "azure", "o1", "o3"):
        return OPENAI_EFFORTS
    if p in ("anthropic", "claude"):
        return ANTHROPIC_EFFORTS
    if p in ("gemini", "google", "antigravity", "google-antigravity", "agy"):
        return GEMINI_EFFORTS
    if p in ("deepseek",):
        return DEEPSEEK_EFFORTS
    return OPENAI_EFFORTS


def clamp_effort(effort: str, supported: Sequence[str]) -> str:
    """Safely clamp an effort level to the supported vocabulary of a provider.

    Returns verbatim if supported; otherwise selects the nearest weaker supported level
    (or lowest if none weaker).
    """
    if not supported:
        return effort or "none"

    clean = (effort or "none").strip().lower()
    clean = EFFORT_ALIASES.get(clean, clean)

    # Check exact case-insensitive match against supported
    sup_map = {s.lower(): s for s in supported}
    if clean in sup_map:
        return sup_map[clean]

    ladder_indices = {lvl: idx for idx, lvl in enumerate(EFFORT_LADDER)}
    supported_ranked = sorted(
        [s for s in supported if s.lower() in ladder_indices],
        key=lambda s: ladder_indices[s.lower()],
    )
    if not supported_ranked:
        return supported[0]

    effort_idx = ladder_indices.get(clean, ladder_indices.get("medium", 3))
    weaker = [s for s in supported_ranked if ladder_indices[s.lower()] < effort_idx]
    if weaker:
        return weaker[-1]

    return supported_ranked[0]


def effort_to_anthropic_thinking(effort: str) -> dict[str, Any] | None:
    """Translate canonical reasoning effort into Anthropic Claude 3.7 thinking payload.

    Maps:
      - low -> 2,048 budget tokens
      - medium -> 8,192 budget tokens
      - high -> 16,384 budget tokens
      - max -> 32,768 budget tokens
    Returns None for "none".
    """
    if not effort:
        return None

    clean = str(effort).strip().lower()
    clean = EFFORT_ALIASES.get(clean, clean)
    if clean in ("none", "off", "disabled", "0", "false"):
        return None

    clamped = clamp_effort(clean, ANTHROPIC_EFFORTS)
    if clamped == "none":
        return None

    budget = ANTHROPIC_THINKING_BUDGETS.get(clamped, ANTHROPIC_THINKING_BUDGETS.get(clean, 8192))
    return {"type": "enabled", "budget_tokens": budget}


def effort_to_gemini_thinking(effort: str) -> dict[str, Any] | None:
    """Translate canonical reasoning effort into Gemini thinkingConfig wire payload.

    Maps to thinkingConfig budget tokens:
      - none -> {"thinkingBudget": 0} (explicitly disable thinking)
      - low -> {"thinkingBudget": 2048}
      - medium -> {"thinkingBudget": 8192}
      - high -> {"thinkingBudget": 16384}
    """
    if effort is None:
        return None

    clean = str(effort).strip().lower()
    if not clean:
        return None

    clean = EFFORT_ALIASES.get(clean, clean)
    if clean in ("none", "off", "disabled", "0", "false"):
        return {"thinkingBudget": 0}

    clamped = clamp_effort(clean, GEMINI_EFFORTS)
    budget = GEMINI_THINKING_BUDGETS.get(clamped, GEMINI_THINKING_BUDGETS.get(clean, 8192))
    return {"thinkingBudget": budget}


def model_supports_reasoning(provider: str, model_id: str) -> bool:
    """Determine whether the specified model and provider support extended reasoning / thinking."""
    p = (provider or "").strip().lower()
    m = (model_id or "").strip().lower()
    if not m:
        return False

    # Check model identifier features first regardless of provider wrapper
    if any(k in m for k in ("thinking", "reasoner", "deepseek-r1")):
        return True
    if m.startswith(("o1", "o3")) or "/o1" in m or "/o3" in m:
        return True
    if "claude-3-7" in m or "claude-3.7" in m or "claude-4" in m:
        return True
    if "gemini-2.5" in m:
        return True

    # Provider-specific logic
    if p in ("openai", "azure"):
        return m.startswith(("o1", "o3")) or "reasoning" in m or "gpt-5" in m
    if p == "anthropic":
        return "claude-3-7" in m or "claude-3.7" in m or "thinking" in m
    if p in ("gemini", "google"):
        return "2.5" in m or "thinking" in m or "2.0-flash-thinking" in m
    if p in ("antigravity", "google-antigravity", "agy"):
        return (
            "thinking" in m
            or "2.5" in m
            or "claude-3-7" in m
            or "claude-3.7" in m
            or m.startswith(("o1", "o3"))
        )
    if p == "deepseek":
        return "reasoner" in m or "r1" in m

    return False


def resolve_reasoning_effort(
    provider: str,
    model_id: str,
    explicit_effort: str | None = None,
) -> str:
    """Resolve active reasoning effort with automatic default based on model capability."""
    env_effort = os.getenv("HEX_REASONING_EFFORT")
    candidate = explicit_effort or env_effort
    if candidate:
        clean = candidate.strip().lower()
        return EFFORT_ALIASES.get(clean, clean)

    if model_supports_reasoning(provider, model_id):
        return "medium"

    return "none"
