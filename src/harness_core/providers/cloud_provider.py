"""Governed Cloud Frontier Model Provider with multi-backend dispatch,

per-task budget tracking, and strict privacy gating.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from harness_core.gateway import redact_secrets

from .base import (
    BudgetExceededError,
    ModelProvider,
    ModelProviderError,
    ModelResponse,
    ModelUsage,
    PolicyBlockedError,
    ProviderCapabilities,
    ProviderUnavailableError,
)


class CloudProviderType(str, Enum):
    """Supported cloud model provider backends."""

    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GEMINI = "gemini"


class ModelPricing:
    """Standard pricing lookup per 1M tokens (USD)."""

    RATES: dict[str, tuple[float, float]] = {
        # model_pattern: (input_usd_per_1m, output_usd_per_1m)
        "claude-3-5-sonnet": (3.00, 15.00),
        "claude-3.5-sonnet": (3.00, 15.00),
        "gpt-4o": (2.50, 10.00),
        "gemini-1.5-pro": (1.25, 5.00),
        "gemini-3.5-pro": (1.50, 6.00),
        "gemini-2.0-flash": (0.10, 0.40),
        "gemini-1.5-flash": (0.075, 0.30),
    }
    DEFAULT_RATES: tuple[float, float] = (2.00, 8.00)

    @classmethod
    def get_rates(cls, model_id: str) -> tuple[float, float]:
        norm = model_id.lower().strip()
        for pat, rates in cls.RATES.items():
            if pat in norm:
                return rates
        return cls.DEFAULT_RATES


def calculate_cost_usd(model_id: str, input_tokens: int, output_tokens: int) -> float:
    """Calculate total USD cost for a model call given input and output token counts."""
    input_rate, output_rate = ModelPricing.get_rates(model_id)
    cost = (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000.0
    return round(cost, 6)


@dataclass
class TaskBudgetTracker:
    """Cumulative token and USD cost accounting with hard ceilings."""

    total_cost_usd: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    request_count: int = 0
    max_cost_usd: float = 2.0
    max_output_tokens: int = 65536
    max_requests: int = 50

    def check_budget_before_call(self) -> None:
        """Pre-flight check before initiating a model request."""
        if self.request_count >= self.max_requests:
            raise BudgetExceededError(
                f"Task request count limit exceeded: {self.request_count} >= {self.max_requests}"
            )
        if self.total_cost_usd >= self.max_cost_usd:
            raise BudgetExceededError(
                f"Task cost ceiling exceeded: ${self.total_cost_usd:.4f} >= ${self.max_cost_usd:.2f}"
            )

    def record_usage(self, input_tokens: int, output_tokens: int, cost_usd: float) -> None:
        """Record token consumption and cost; enforce hard budget ceilings."""
        self.request_count += 1
        self.total_input_tokens += int(input_tokens)
        self.total_output_tokens += int(output_tokens)
        self.total_cost_usd = round(self.total_cost_usd + float(cost_usd), 6)

        if self.total_cost_usd > self.max_cost_usd:
            raise BudgetExceededError(
                f"Task cost ceiling exceeded: ${self.total_cost_usd:.4f} > ${self.max_cost_usd:.2f}"
            )
        if self.total_output_tokens > self.max_output_tokens:
            raise BudgetExceededError(
                f"Task output token limit exceeded: {self.total_output_tokens} > {self.max_output_tokens}"
            )

    def sync_with_task_budget(self, budget: Any) -> None:
        """Optionally synchronize limits from a TaskBudget instance."""
        if budget is not None:
            if hasattr(budget, "max_cost_usd") and budget.max_cost_usd is not None:
                self.max_cost_usd = float(budget.max_cost_usd)
            if hasattr(budget, "max_tool_calls") and budget.max_tool_calls is not None:
                self.max_requests = int(budget.max_tool_calls)


class CloudModelProvider(ModelProvider):
    """Governed Cloud Frontier Model Provider with multi-backend dispatch,

    per-task budget tracking, and strict privacy gating.
    """

    provider_id: str = "cloud_frontier"

    def __init__(
        self,
        *,
        provider_type: CloudProviderType | str = CloudProviderType.GEMINI,
        model_id: str | None = None,
        api_key: str | None = None,
        budget_tracker: TaskBudgetTracker | None = None,
        http_transport: Callable[[str, dict[str, str], bytes], tuple[int, bytes]] | None = None,
    ) -> None:
        if isinstance(provider_type, CloudProviderType):
            self.provider_type = provider_type
        else:
            self.provider_type = CloudProviderType(str(provider_type).lower())

        if model_id:
            self.model_id = model_id
        else:
            if self.provider_type == CloudProviderType.ANTHROPIC:
                self.model_id = "claude-3-5-sonnet-20241022"
            elif self.provider_type == CloudProviderType.OPENAI:
                self.model_id = "gpt-4o"
            else:
                self.model_id = "gemini-3.5-pro"

        self.api_key = api_key or self._resolve_env_api_key()
        self.budget_tracker = budget_tracker or TaskBudgetTracker()
        self.http_transport = http_transport

    def _resolve_env_api_key(self) -> str | None:
        if self.provider_type == CloudProviderType.ANTHROPIC:
            return os.getenv("ANTHROPIC_API_KEY")
        elif self.provider_type == CloudProviderType.OPENAI:
            return os.getenv("OPENAI_API_KEY")
        elif self.provider_type == CloudProviderType.GEMINI:
            return os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        return None

    def is_available(self) -> bool:
        return bool(self.api_key) or (self.http_transport is not None)

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supports_system_prompt=True,
            supports_streaming=False,
            max_context_window=200_000,
            quantization=None,
            local_unified_memory=False,
        )

    def generate(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        stop_sequences: list[str] | None = None,
        data_classification: str = "internal",
        budget: Any = None,
        **kwargs: Any,
    ) -> ModelResponse:
        """Generate response from frontier cloud provider with privacy and budget enforcement."""
        # Privacy Gate: Sensitive / secret data classification is strictly blocked from cloud egress
        if data_classification in ("sensitive", "secret"):
            raise PolicyBlockedError(
                f"Cloud egress forbidden for data classification '{data_classification}'. "
                "Data must remain within 100% local boundary."
            )

        # Budget synchronization & pre-flight check
        if budget is not None:
            self.budget_tracker.sync_with_task_budget(budget)
        self.budget_tracker.check_budget_before_call()

        # Sanitize prompts with secret redaction before any payload dispatch
        clean_prompt = str(redact_secrets(prompt))
        clean_system_prompt = str(redact_secrets(system_prompt)) if system_prompt else None

        # Build backend request
        url, headers, req_body_bytes = self._build_request(
            clean_prompt,
            system_prompt=clean_system_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            stop_sequences=stop_sequences,
        )

        t0 = time.perf_counter()
        status_code, resp_bytes = self._execute_http(url, headers, req_body_bytes)
        latency_ms = (time.perf_counter() - t0) * 1000.0

        if status_code not in (200, 201):
            err_msg = resp_bytes.decode("utf-8", errors="replace")[:500]
            raise ProviderUnavailableError(
                f"Cloud provider '{self.provider_type.value}' returned HTTP {status_code}: {err_msg}"
            )

        try:
            data = json.loads(resp_bytes.decode("utf-8"))
        except Exception as e:
            raise ModelProviderError(
                f"Failed to parse JSON response from '{self.provider_type.value}': {e}"
            ) from e

        text, in_tokens, out_tokens, finish_reason = self._parse_response(data, clean_prompt)
        cost_usd = calculate_cost_usd(self.model_id, in_tokens, out_tokens)

        # Record usage and check post-call ceilings
        self.budget_tracker.record_usage(in_tokens, out_tokens, cost_usd)

        usage = ModelUsage(
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            total_tokens=in_tokens + out_tokens,
            cost_usd=cost_usd,
        )

        return ModelResponse(
            text=text,
            model_id=self.model_id,
            provider_id=self.provider_id,
            usage=usage,
            latency_ms=latency_ms,
            finish_reason=finish_reason,
            raw=data,
        )

    def _execute_http(self, url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
        if self.http_transport is not None:
            return self.http_transport(url, headers, body)

        if not self.api_key:
            raise ProviderUnavailableError(
                f"No API key configured for cloud provider '{self.provider_type.value}'."
            )

        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30.0) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except Exception as e:
            raise ProviderUnavailableError(
                f"HTTP request to '{self.provider_type.value}' failed: {e}"
            ) from e

    def _build_request(
        self,
        clean_prompt: str,
        *,
        system_prompt: str | None,
        max_tokens: int,
        temperature: float,
        stop_sequences: list[str] | None,
    ) -> tuple[str, dict[str, str], bytes]:
        if self.provider_type == CloudProviderType.ANTHROPIC:
            url = "https://api.anthropic.com/v1/messages"
            headers = {
                "x-api-key": self.api_key or "mock-key",
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            payload: dict[str, Any] = {
                "model": self.model_id,
                "messages": [{"role": "user", "content": clean_prompt}],
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
            if system_prompt:
                payload["system"] = system_prompt
            if stop_sequences:
                payload["stop_sequences"] = stop_sequences
            return url, headers, json.dumps(payload).encode("utf-8")

        elif self.provider_type == CloudProviderType.OPENAI:
            url = "https://api.openai.com/v1/chat/completions"
            headers = {
                "Authorization": f"Bearer {self.api_key or 'mock-key'}",
                "content-type": "application/json",
            }
            messages: list[dict[str, str]] = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": clean_prompt})
            payload = {
                "model": self.model_id,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
            if stop_sequences:
                payload["stop"] = stop_sequences
            return url, headers, json.dumps(payload).encode("utf-8")

        elif self.provider_type == CloudProviderType.GEMINI:
            api_key = self.api_key or "mock-key"
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model_id}:generateContent?key={api_key}"
            headers = {"content-type": "application/json"}
            payload = {
                "contents": [{"role": "user", "parts": [{"text": clean_prompt}]}],
                "generationConfig": {
                    "maxOutputTokens": max_tokens,
                    "temperature": temperature,
                },
            }
            if system_prompt:
                payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}
            if stop_sequences:
                payload["generationConfig"]["stopSequences"] = stop_sequences
            return url, headers, json.dumps(payload).encode("utf-8")

        raise ModelProviderError(f"Unsupported cloud provider type: {self.provider_type}")

    def _parse_response(
        self, data: dict[str, Any], prompt: str
    ) -> tuple[str, int, int, str]:
        text = ""
        in_tokens = 0
        out_tokens = 0
        finish_reason = "stop"

        if self.provider_type == CloudProviderType.ANTHROPIC:
            if "content" in data and isinstance(data["content"], list):
                for item in data["content"]:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text += item.get("text", "")
                    elif isinstance(item, str):
                        text += item
            elif "text" in data:
                text = str(data["text"])
            usage_dict = data.get("usage", {})
            in_tokens = usage_dict.get("input_tokens", 0)
            out_tokens = usage_dict.get("output_tokens", 0)
            finish_reason = data.get("stop_reason") or "stop"

        elif self.provider_type == CloudProviderType.OPENAI:
            choices = data.get("choices", [])
            if choices and isinstance(choices, list) and isinstance(choices[0], dict):
                msg = choices[0].get("message", {})
                if isinstance(msg, dict):
                    text = msg.get("content", "") or ""
                else:
                    text = str(msg)
                finish_reason = choices[0].get("finish_reason") or "stop"
            elif "text" in data:
                text = str(data["text"])
            usage_dict = data.get("usage", {})
            in_tokens = usage_dict.get("prompt_tokens", 0)
            out_tokens = usage_dict.get("completion_tokens", 0)

        elif self.provider_type == CloudProviderType.GEMINI:
            candidates = data.get("candidates", [])
            if candidates and isinstance(candidates, list) and isinstance(candidates[0], dict):
                cand = candidates[0]
                content = cand.get("content", {})
                if isinstance(content, dict):
                    parts = content.get("parts", [])
                    if parts and isinstance(parts, list):
                        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
                finish_reason = str(cand.get("finishReason", "stop")).lower()
            elif "text" in data:
                text = str(data["text"])
            usage_meta = data.get("usageMetadata", {})
            in_tokens = usage_meta.get("promptTokenCount", 0)
            out_tokens = usage_meta.get("candidatesTokenCount", 0)

        # Fallback heuristic token count if provider didn't return usage
        if in_tokens <= 0:
            in_tokens = max(1, len(prompt) // 4)
        if out_tokens <= 0:
            out_tokens = max(1, len(text) // 4)

        return text, in_tokens, out_tokens, finish_reason
