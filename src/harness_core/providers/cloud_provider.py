"""Governed Cloud Frontier Model Provider with multi-backend dispatch,

Google Antigravity OAuth quota routing, prompt prefix caching (stable -> context -> volatile),
optional cloud log compression, per-task budget tracking, and strict privacy gating.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from harness_core.gateway import redact_secrets

from .antigravity_provider import (
    AntigravityCredential,
    AntigravityModelProvider,
    AntigravityOAuthManager,
    AntigravityQuotaSnapshot,
    fetch_available_models as ag_fetch_available_models,
)
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

    ANTIGRAVITY = "antigravity"
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GEMINI = "gemini"
    LOCAL = "local"
    OLLAMA = "ollama"


def fetch_available_models(
    provider_type: CloudProviderType | str,
    api_key: str | None = None,
    timeout: float = 3.0,
) -> list[tuple[str, str]]:
    """Discover available models from live provider API without hardcoded fallbacks."""
    raw_type = getattr(provider_type, "value", provider_type)
    norm = str(raw_type).lower().strip()

    if norm in ("antigravity", "google-antigravity", "cloudcode"):
        return ag_fetch_available_models(access_token=api_key, timeout=timeout)

    elif norm == "anthropic":
        key = (api_key or os.getenv("HEX_ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_API_KEY") or "").strip()
        if not key:
            raise ProviderUnavailableError("No Anthropic API key found. Set HEX_ANTHROPIC_API_KEY to live-fetch models.")
        url = "https://api.anthropic.com/v1/models"
        headers = {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "User-Agent": "HEX/0.1.0",
        }
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    items = data.get("data")
                    if isinstance(items, list) and items:
                        results = []
                        for m in items:
                            if isinstance(m, dict) and "id" in m:
                                mid = str(m["id"])
                                disp = str(m.get("display_name") or mid)
                                results.append((mid, disp))
                        if results:
                            results.sort(key=lambda x: (x[1].lower(), x[0].lower()))
                            return results
            raise ProviderUnavailableError(f"Anthropic returned HTTP status {resp.status}.")
        except Exception as exc:
            if isinstance(exc, ProviderUnavailableError):
                raise
            raise ProviderUnavailableError(f"Failed to live-fetch Anthropic models: {exc}") from exc

    elif norm == "openai":
        key = (api_key or os.getenv("HEX_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()
        if not key:
            raise ProviderUnavailableError("No OpenAI API key found. Set HEX_OPENAI_API_KEY to live-fetch models.")
        url = "https://api.openai.com/v1/models"
        headers = {
            "Authorization": f"Bearer {key}",
            "User-Agent": "HEX/0.1.0",
        }
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    items = data.get("data")
                    if isinstance(items, list) and items:
                        allowed_pats = ("gpt-4o", "gpt-4.5", "o1", "o3-mini", "gpt-4-turbo")
                        results = []
                        for m in items:
                            if isinstance(m, dict) and "id" in m:
                                mid = str(m["id"])
                                if any(p in mid for p in allowed_pats):
                                    results.append((mid, mid))
                        if results:
                            results.sort(key=lambda x: x[0])
                            return results
            raise ProviderUnavailableError(f"OpenAI returned HTTP status {resp.status}.")
        except Exception as exc:
            if isinstance(exc, ProviderUnavailableError):
                raise
            raise ProviderUnavailableError(f"Failed to live-fetch OpenAI models: {exc}") from exc

    elif norm in ("gemini", "google"):
        key = (api_key or os.getenv("HEX_GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY") or "").strip()
        if not key:
            raise ProviderUnavailableError("No Gemini API key found. Set HEX_GEMINI_API_KEY to live-fetch models.")
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={key}"
        headers = {"User-Agent": "HEX/0.1.0"}
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    items = data.get("models")
                    if isinstance(items, list) and items:
                        results = []
                        for m in items:
                            if not isinstance(m, dict):
                                continue
                            methods = m.get("supportedGenerationMethods") or []
                            if "generateContent" in methods:
                                raw_name = str(m.get("name", ""))
                                clean_id = raw_name.removeprefix("models/").strip()
                                if clean_id:
                                    disp = m.get("displayName")
                                    label = f"{clean_id} ({disp})" if disp and disp != clean_id else clean_id
                                    results.append((clean_id, label))
                        if results:
                            results.sort(key=lambda x: x[0])
                            return results
            raise ProviderUnavailableError(f"Gemini returned HTTP status {resp.status}.")
        except Exception as exc:
            if isinstance(exc, ProviderUnavailableError):
                raise
            raise ProviderUnavailableError(f"Failed to live-fetch Gemini models: {exc}") from exc

    elif norm in ("local", "ollama"):
        results: list[tuple[str, str]] = []
        # 1. Probe local Ollama if running
        url = "http://localhost:11434/api/tags"
        headers = {"User-Agent": "HEX/0.1.0"}
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=min(timeout, 1.5)) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    items = data.get("models")
                    if isinstance(items, list) and items:
                        for m in items:
                            if isinstance(m, dict) and "name" in m:
                                name = str(m["name"])
                                results.append((name, f"{name} (Ollama)"))
        except Exception:
            pass

        # 2. Check HuggingFace hub cache
        hf_cache = Path.home() / ".cache" / "huggingface" / "hub"
        if hf_cache.is_dir():
            try:
                for entry in hf_cache.iterdir():
                    if entry.is_dir() and entry.name.startswith("models--"):
                        raw_id = entry.name.removeprefix("models--").replace("--", "/")
                        label = f"{raw_id} (HuggingFace Hub)"
                        if (raw_id, label) not in results:
                            results.append((raw_id, label))
            except Exception:
                pass

        # 3. Check LM Studio cache
        lms_cache = Path.home() / ".cache" / "lm-studio" / "models"
        if lms_cache.is_dir():
            try:
                for author_dir in lms_cache.iterdir():
                    if author_dir.is_dir():
                        for m_dir in author_dir.iterdir():
                            if m_dir.is_dir():
                                mid = f"{author_dir.name}/{m_dir.name}"
                                label = f"{mid} (LM Studio)"
                                if (mid, label) not in results:
                                    results.append((mid, label))
            except Exception:
                pass

        results.sort(key=lambda x: x[0].lower())
        return results

    return []


class ModelPricing:
    """Standard pricing lookup per 1M tokens (USD)."""

    RATES: dict[str, tuple[float, float]] = {
        # model_pattern: (input_usd_per_1m, output_usd_per_1m)
        "antigravity": (0.00, 0.00),
        "claude-3-5-sonnet": (3.00, 15.00),
        "claude-3.5-sonnet": (3.00, 15.00),
        "gpt-4o": (2.50, 10.00),
        "gemini-1.5-pro": (1.25, 5.00),
        "gemini-2.5-pro": (1.25, 5.00),
        "gemini-2.5-flash": (0.10, 0.40),
        "gemini-3.5-pro": (1.50, 6.00),
        "gemini-3.8-flash": (0.10, 0.40),
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


def calculate_cost_usd(
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
) -> float:
    """Calculate total USD cost for a model call given input, cached input (90% discount), and output token counts."""
    input_rate, output_rate = ModelPricing.get_rates(model_id)
    cached = max(0, min(int(cached_input_tokens), int(input_tokens)))
    uncached = max(0, int(input_tokens) - cached)
    effective_input_cost = uncached * input_rate + cached * (input_rate * 0.10)
    cost = (effective_input_cost + int(output_tokens) * output_rate) / 1_000_000.0
    return round(cost, 6)


@dataclass
class TaskBudgetTracker:
    """Cumulative token and USD cost accounting with hard ceilings."""

    total_cost_usd: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cached_input_tokens: int = 0
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

    def record_usage(
        self,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        cached_input_tokens: int = 0,
    ) -> None:
        """Record token consumption and cost; enforce hard budget ceilings."""
        self.request_count += 1
        self.total_input_tokens += int(input_tokens)
        self.total_output_tokens += int(output_tokens)
        self.total_cached_input_tokens += int(cached_input_tokens)
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

    Google Antigravity OAuth quota support, prompt prefix caching (stable -> context -> volatile),
    optional cloud log compression, per-task budget tracking, and strict privacy gating.
    """

    provider_id: str = "cloud_frontier"

    @staticmethod
    def fetch_available_models(
        provider_type: CloudProviderType | str,
        api_key: str | None = None,
        timeout: float = 3.0,
    ) -> list[tuple[str, str]]:
        return fetch_available_models(provider_type, api_key=api_key, timeout=timeout)

    def __init__(
        self,
        *,
        provider_type: CloudProviderType | str = CloudProviderType.GEMINI,
        model_id: str | None = None,
        api_key: str | None = None,
        budget_tracker: TaskBudgetTracker | None = None,
        http_transport: Callable[[str, dict[str, str], bytes], tuple[int, bytes]] | None = None,
        enable_prompt_caching: bool = False,
        oauth_manager: AntigravityOAuthManager | None = None,
    ) -> None:
        if isinstance(provider_type, CloudProviderType):
            self.provider_type = provider_type
        else:
            norm_p = str(provider_type).lower().strip()
            if norm_p in ("google-antigravity", "google_antigravity", "agy"):
                norm_p = "antigravity"
            self.provider_type = CloudProviderType(norm_p)

        if model_id:
            self.model_id = model_id
        else:
            resolved_env_model = os.getenv("HEX_CLOUD_MODEL_ID") or os.getenv("HARNESS_CLOUD_MODEL_ID")
            if resolved_env_model:
                self.model_id = resolved_env_model
            else:
                try:
                    disc = fetch_available_models(self.provider_type, api_key=api_key)
                    self.model_id = disc[0][0] if disc else ""
                except Exception:
                    self.model_id = ""

        self.budget_tracker = budget_tracker or TaskBudgetTracker()
        self.http_transport = http_transport
        self.enable_prompt_caching = enable_prompt_caching
        self._antigravity_delegate: AntigravityModelProvider | None = None

        if self.provider_type == CloudProviderType.ANTIGRAVITY:
            self.provider_id = "google_antigravity"
            self._antigravity_delegate = AntigravityModelProvider(
                model_id=self.model_id,
                oauth_manager=oauth_manager,
                access_token=api_key,
                budget_tracker=self.budget_tracker,
                http_transport=http_transport,
                enable_prompt_caching=enable_prompt_caching,
            )
            self.api_key = api_key or self._resolve_env_api_key()
        else:
            self.provider_id = self.provider_type.value
            self.api_key = api_key or self._resolve_env_api_key()

    def _resolve_env_api_key(self) -> str | None:
        if self.provider_type == CloudProviderType.ANTIGRAVITY:
            if self._antigravity_delegate is not None:
                cred = self._antigravity_delegate._get_active_credential()
                if cred and cred.access_token:
                    return cred.access_token
            return (
                os.getenv("HEX_ANTIGRAVITY_TOKEN")
                or os.getenv("ANTIGRAVITY_OAUTH_TOKEN")
                or os.getenv("ANTIGRAVITY_ACCESS_TOKEN")
            )
        if self.provider_type == CloudProviderType.ANTHROPIC:
            return os.getenv("ANTHROPIC_API_KEY") or os.getenv("HEX_ANTHROPIC_API_KEY")
        elif self.provider_type == CloudProviderType.OPENAI:
            return os.getenv("OPENAI_API_KEY") or os.getenv("HEX_OPENAI_API_KEY")
        elif self.provider_type == CloudProviderType.GEMINI:
            return os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or os.getenv("HEX_GEMINI_API_KEY")
        return None

    def is_available(self) -> bool:
        if self._antigravity_delegate is not None:
            return self._antigravity_delegate.is_available()
        return bool(self.api_key) or (self.http_transport is not None)

    def get_capabilities(self) -> ProviderCapabilities:
        if self._antigravity_delegate is not None:
            return self._antigravity_delegate.get_capabilities()
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
        cacheable_prefix: str | None = None,
        enable_prompt_caching: bool | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        stop_sequences: list[str] | None = None,
        data_classification: str = "internal",
        budget: Any = None,
        **kwargs: Any,
    ) -> ModelResponse:
        """Generate response from frontier cloud provider with privacy, prefix caching, and budget enforcement."""
        if self._antigravity_delegate is not None:
            self._antigravity_delegate.model_id = AntigravityModelProvider._normalize_model_id(self.model_id)
            return self._antigravity_delegate.generate(
                prompt,
                system_prompt=system_prompt,
                cacheable_prefix=cacheable_prefix,
                enable_prompt_caching=enable_prompt_caching,
                max_tokens=max_tokens,
                temperature=temperature,
                stop_sequences=stop_sequences,
                data_classification=data_classification,
                budget=budget,
                **kwargs,
            )

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
        clean_prefix = str(redact_secrets(cacheable_prefix)) if cacheable_prefix else None
        use_caching = (
            enable_prompt_caching
            if enable_prompt_caching is not None
            else (self.enable_prompt_caching or clean_prefix is not None)
        )

        # Build backend request with stable -> context -> volatile ordering
        url, headers, req_body_bytes = self._build_request(
            clean_prompt,
            system_prompt=clean_system_prompt,
            cacheable_prefix=clean_prefix,
            enable_prompt_caching=use_caching,
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

        text, in_tokens, out_tokens, cached_tokens, finish_reason = self._parse_response(data, clean_prompt)
        cost_usd = calculate_cost_usd(
            self.model_id,
            in_tokens,
            out_tokens,
            cached_input_tokens=cached_tokens,
        )

        # Record usage and check post-call ceilings
        self.budget_tracker.record_usage(
            in_tokens,
            out_tokens,
            cost_usd,
            cached_input_tokens=cached_tokens,
        )

        usage = ModelUsage(
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            total_tokens=in_tokens + out_tokens,
            cost_usd=cost_usd,
            cached_input_tokens=cached_tokens,
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

    def compress_log(
        self,
        raw_log: str,
        max_chars: int = 2400,
        data_classification: str = "internal",
        budget: Any = None,
    ) -> dict[str, Any]:
        """Compress verbose test/build logs using this CloudModelProvider (with deterministic pre-filter)."""
        if self._antigravity_delegate is not None:
            return self._antigravity_delegate.compress_log(
                raw_log,
                max_chars=max_chars,
                data_classification=data_classification,
                budget=budget,
            )

        if data_classification in ("sensitive", "secret"):
            raise PolicyBlockedError(
                f"Cloud log compression forbidden for data classification '{data_classification}'."
            )

        from .mlx_provider import Tier1LogCompressor

        local_pre = Tier1LogCompressor(mlx_available=False).compress(raw_log, max_chars=max_chars)
        sanitized = str(redact_secrets(raw_log))
        raw_tokens = max(1, len(sanitized) // 4)

        if not sanitized.strip() or not self.is_available():
            local_pre["engine"] = f"cloud_{self.provider_type.value}_fallback"
            return local_pre

        system_prompt = (
            "Distill the test/build failure log into a concise failure digest preserving exact "
            "failing file paths, line numbers, traceback frames, and assertion errors."
        )
        resp = self.generate(
            prompt=local_pre["compressed_text"],
            system_prompt=system_prompt,
            enable_prompt_caching=True,
            max_tokens=512,
            temperature=0.0,
            data_classification=data_classification,
            budget=budget,
        )
        combined = (
            f"[Tier-1 Cloud ({self.model_id}) Compressed Failure Digest]\n"
            f"{local_pre['compressed_text']}\n{resp.text.strip()}"
        )
        if len(combined) > max_chars:
            combined = combined[: max_chars - 35] + "\n...[truncated by cloud compressor]"
        comp_tokens = max(1, len(combined) // 4)
        savings_pct = round(max(0.0, (1.0 - (comp_tokens / raw_tokens)) * 100.0), 2)
        return {
            "compressed_text": combined,
            "raw_tokens": raw_tokens,
            "compressed_tokens": comp_tokens,
            "token_savings_pct": savings_pct,
            "failing_files": local_pre["failing_files"],
            "failing_assertions": local_pre["failing_assertions"],
            "engine": f"cloud_{self.provider_type.value}",
            "compressor_model": self.model_id,
            "cost_usd": resp.usage.cost_usd,
        }

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
        cacheable_prefix: str | None = None,
        enable_prompt_caching: bool = False,
        max_tokens: int,
        temperature: float,
        stop_sequences: list[str] | None,
    ) -> tuple[str, dict[str, str], bytes]:
        if not self.api_key and self.http_transport is None:
            raise ProviderUnavailableError(
                f"No API key provided for {self.provider_type.value}. Run 'hex setup' or set the appropriate environment variable."
            )

        if self.provider_type == CloudProviderType.ANTHROPIC:
            url = "https://api.anthropic.com/v1/messages"
            headers = {
                "x-api-key": self.api_key or "mock-key",
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            if enable_prompt_caching:
                headers["anthropic-beta"] = "prompt-caching-2024-07-31"

            if enable_prompt_caching and cacheable_prefix:
                user_content: Any = [
                    {
                        "type": "text",
                        "text": cacheable_prefix,
                        "cache_control": {"type": "ephemeral"},
                    },
                    {
                        "type": "text",
                        "text": clean_prompt,
                    },
                ]
            else:
                user_content = clean_prompt

            payload: dict[str, Any] = {
                "model": self.model_id,
                "messages": [{"role": "user", "content": user_content}],
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
            if system_prompt:
                if enable_prompt_caching:
                    payload["system"] = [
                        {
                            "type": "text",
                            "text": system_prompt,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ]
                else:
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
            # Order strictly as stable (system) -> context (cacheable_prefix) -> volatile (prompt)
            messages: list[dict[str, str]] = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            if cacheable_prefix:
                messages.append({"role": "user", "content": cacheable_prefix})
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
            parts: list[dict[str, str]] = []
            if cacheable_prefix:
                parts.append({"text": cacheable_prefix})
            parts.append({"text": clean_prompt})
            payload = {
                "contents": [{"role": "user", "parts": parts}],
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
    ) -> tuple[str, int, int, int, str]:
        text = ""
        in_tokens = 0
        out_tokens = 0
        cached_tokens = 0
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
            raw_in = int(usage_dict.get("input_tokens", 0))
            cache_read = int(usage_dict.get("cache_read_input_tokens", 0))
            cache_create = int(usage_dict.get("cache_creation_input_tokens", 0))
            cached_tokens = cache_read
            in_tokens = raw_in + cache_read + cache_create
            out_tokens = int(usage_dict.get("output_tokens", 0))
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
            in_tokens = int(usage_dict.get("prompt_tokens", 0))
            out_tokens = int(usage_dict.get("completion_tokens", 0))
            details = usage_dict.get("prompt_tokens_details") or {}
            if isinstance(details, dict):
                cached_tokens = int(details.get("cached_tokens", 0))

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
            in_tokens = int(usage_meta.get("promptTokenCount", 0))
            out_tokens = int(usage_meta.get("candidatesTokenCount", 0))
            cached_tokens = int(usage_meta.get("cachedContentTokenCount", 0))

        # Fallback heuristic token count if provider didn't return usage
        if in_tokens <= 0:
            in_tokens = max(1, len(prompt) // 4)
        if out_tokens <= 0:
            out_tokens = max(1, len(text) // 4)

        return text, in_tokens, out_tokens, cached_tokens, finish_reason
