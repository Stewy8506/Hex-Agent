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
import uuid
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
    build_gemini_contents_and_system,
    extract_gemini_tool_calls,
    format_gemini_tools_and_config,
    normalize_tool_definitions,
    parse_tool_args_dict,
    sanitize_messages_secrets,
)
from .registry import (
    PROVIDER_REGISTRY,
    ProviderSpec,
    get_provider_spec,
    list_provider_ids,
)


class CloudProviderType(str, Enum):
    """Supported cloud model provider backends."""

    ANTIGRAVITY = "antigravity"
    OPENROUTER = "openrouter"
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GEMINI = "gemini"
    DEEPSEEK = "deepseek"
    XAI = "xai"
    GROQ = "groq"
    MISTRAL = "mistral"
    TOGETHER = "together"
    CEREBRAS = "cerebras"
    FIREWORKS = "fireworks"
    NVIDIA = "nvidia"
    ALIBABA = "alibaba"
    MINIMAX = "minimax"
    KIMI = "kimi"
    ZAI = "zai"
    AI_GATEWAY = "ai-gateway"
    HUGGINGFACE = "huggingface"
    AZURE = "azure"
    BEDROCK = "bedrock"
    VERTEX = "vertex"
    LOCAL = "local"
    OLLAMA = "ollama"
    LMSTUDIO = "lmstudio"
    CUSTOM = "custom"


def fetch_available_models(
    provider_type: CloudProviderType | str,
    api_key: str | None = None,
    base_url: str | None = None,
    timeout: float = 3.0,
) -> list[tuple[str, str]]:
    """Discover available models from live provider API or curated provider fallback."""
    raw_type = getattr(provider_type, "value", provider_type)
    norm = str(raw_type).lower().strip()
    spec = get_provider_spec(norm)

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
        key = (api_key or os.getenv("HEX_GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
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

    elif norm in ("local", "ollama", "lmstudio"):
        results: list[tuple[str, str]] = []
        # 1. Probe local Ollama if running
        ollama_base = os.getenv("OLLAMA_HOST") or "http://localhost:11434"
        ollama_url = f"{ollama_base.rstrip('/')}/api/tags"
        headers = {"User-Agent": "HEX/0.1.0"}
        req = urllib.request.Request(ollama_url, headers=headers, method="GET")
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

    # -------------------------------------------------------------------------
    # Generic / OpenAI-compatible / Registry-driven live discovery
    # -------------------------------------------------------------------------
    if spec:
        resolved_key = (api_key or spec.resolve_api_key() or "").strip()
        target_base = (base_url or spec.resolve_base_url()).rstrip("/")
        models_url = f"{target_base}/models" if not target_base.endswith("/models") else target_base

        if resolved_key or not spec.requires_api_key:
            headers = {"User-Agent": "HEX/0.1.0"}
            if resolved_key:
                headers["Authorization"] = f"Bearer {resolved_key}"
            headers.update(spec.extra_headers)

            req = urllib.request.Request(models_url, headers=headers, method="GET")
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    if resp.status == 200:
                        data = json.loads(resp.read().decode("utf-8"))
                        items = data.get("data") or data.get("models")
                        if isinstance(items, list) and items:
                            results = []
                            for m in items:
                                if isinstance(m, dict) and "id" in m:
                                    mid = str(m["id"])
                                    disp = str(m.get("name") or m.get("display_name") or mid)
                                    results.append((mid, f"{disp} ({spec.name})"))
                            if results:
                                results.sort(key=lambda x: (x[1].lower(), x[0].lower()))
                                return results
            except Exception as exc:
                raise ProviderUnavailableError(f"Failed to fetch models for '{norm}': {exc}") from exc

    raise ProviderUnavailableError(f"No models found or available for provider '{norm}'.")

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
        base_url: str | None = None,
        budget_tracker: TaskBudgetTracker | None = None,
        http_transport: Callable[[str, dict[str, str], bytes], tuple[int, bytes]] | None = None,
        enable_prompt_caching: bool = False,
        oauth_manager: AntigravityOAuthManager | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        if isinstance(provider_type, CloudProviderType):
            self.provider_type = provider_type
        else:
            norm_p = str(provider_type).lower().strip()
            if norm_p in ("google-antigravity", "google_antigravity", "agy"):
                norm_p = "antigravity"
            try:
                self.provider_type = CloudProviderType(norm_p)
            except ValueError:
                self.provider_type = CloudProviderType.CUSTOM

        self.spec = get_provider_spec(self.provider_type.value)
        self.base_url = (base_url or (self.spec.resolve_base_url() if self.spec else "")).rstrip("/")

        if model_id:
            self.model_id = model_id
        else:
            resolved_env_model = os.getenv("HEX_CLOUD_MODEL_ID") or os.getenv("HARNESS_CLOUD_MODEL_ID")
            if resolved_env_model:
                self.model_id = resolved_env_model
            elif self.spec and self.spec.default_model:
                self.model_id = self.spec.default_model
            else:
                try:
                    disc = fetch_available_models(self.provider_type, api_key=api_key, base_url=self.base_url)
                    self.model_id = disc[0][0] if disc else ""
                except Exception:
                    self.model_id = ""

        self.budget_tracker = budget_tracker or TaskBudgetTracker()
        self.http_transport = http_transport
        self.enable_prompt_caching = enable_prompt_caching
        self.reasoning_effort = reasoning_effort or os.getenv("HEX_REASONING_EFFORT")
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
                reasoning_effort=self.reasoning_effort,
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
        if self.spec:
            resolved = self.spec.resolve_api_key()
            if resolved is not None:
                return resolved
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
        from harness_core.models.model_metadata import get_model_context_length

        max_ctx = get_model_context_length(
            self.model_id,
            provider=self.provider_name,
            base_url=self.base_url,
        )
        return ProviderCapabilities(
            supports_system_prompt=True,
            supports_streaming=False,
            max_context_window=max_ctx,
            quantization=None,
            local_unified_memory=False,
        )

    def generate(
        self,
        prompt: str = "",
        *,
        system_prompt: str | None = None,
        cacheable_prefix: str | None = None,
        enable_prompt_caching: bool | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        stop_sequences: list[str] | None = None,
        data_classification: str = "internal",
        budget: Any = None,
        messages: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        reasoning_effort: str | None = None,
        **kwargs: Any,
    ) -> ModelResponse:
        """Generate response from frontier cloud provider with privacy, prefix caching, and budget enforcement."""
        eff_effort = reasoning_effort or self.reasoning_effort or os.getenv("HEX_REASONING_EFFORT")
        clean_messages = sanitize_messages_secrets(messages)
        if self._antigravity_delegate is not None:
            self._antigravity_delegate.model_id = AntigravityModelProvider._normalize_model_id(self.model_id)
            if hasattr(self._antigravity_delegate, "reasoning_effort") and eff_effort:
                self._antigravity_delegate.reasoning_effort = eff_effort
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
                messages=clean_messages,
                tools=tools,
                tool_choice=tool_choice,
                reasoning_effort=eff_effort,
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

        # Sanitize prompts and structured messages with secret redaction before any payload dispatch
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
            messages=clean_messages,
            tools=tools,
            tool_choice=tool_choice,
            reasoning_effort=eff_effort,
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
        extracted_tool_calls = self._extract_tool_calls(data)
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
            tool_calls=extracted_tool_calls,
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

    @staticmethod
    def _normalize_tool_definitions(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        return normalize_tool_definitions(tools)

    @staticmethod
    def _normalize_openai_messages(raw_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Normalize chat messages (including assistant tool_calls and tool results) for OpenAI chat."""
        out: list[dict[str, Any]] = []
        for msg in raw_messages:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role", "user")).lower()
            if role == "tool":
                cid = msg.get("tool_call_id") or msg.get("tool_use_id") or msg.get("call_id") or ""
                content = msg.get("content")
                if content is None:
                    content = msg.get("observation") or ""
                out.append({
                    "role": "tool",
                    "tool_call_id": str(cid),
                    "content": str(content),
                })
                continue
            if role == "assistant" and isinstance(msg.get("tool_calls"), list) and msg["tool_calls"]:
                norm_calls: list[dict[str, Any]] = []
                for idx, tc in enumerate(msg["tool_calls"]):
                    if not isinstance(tc, dict):
                        continue
                    if tc.get("type") == "function" and isinstance(tc.get("function"), dict):
                        fn = tc["function"]
                        tc_name = str(fn.get("name") or "")
                        tc_args = fn.get("arguments", "{}")
                        if not isinstance(tc_args, str):
                            tc_args = json.dumps(tc_args, ensure_ascii=False)
                    else:
                        tc_name = str(tc.get("name") or tc.get("tool") or "")
                        raw_args = tc.get("arguments") or tc.get("args") or {}
                        tc_args = raw_args if isinstance(raw_args, str) else json.dumps(raw_args, ensure_ascii=False)
                    if tc_name:
                        cid = str(tc.get("id") or tc.get("call_id") or f"call_{idx}")
                        norm_calls.append({
                            "id": cid,
                            "type": "function",
                            "function": {"name": tc_name, "arguments": tc_args},
                        })
                assistant_msg: dict[str, Any] = {"role": "assistant", "content": msg.get("content") or ""}
                if norm_calls:
                    assistant_msg["tool_calls"] = norm_calls
                out.append(assistant_msg)
                continue
            out.append(dict(msg))
        return out

    @staticmethod
    def _parse_tool_args_dict(raw_args: Any) -> dict[str, Any]:
        return parse_tool_args_dict(raw_args)

    @staticmethod
    def _normalize_anthropic_tool_choice(tool_choice: Any) -> Any:
        if isinstance(tool_choice, str):
            norm = tool_choice.strip().lower()
            if norm in ("required", "any"):
                return {"type": "any"}
            if norm in ("auto", "none"):
                return {"type": norm}
            return {"type": "tool", "name": tool_choice}
        return tool_choice

    def _build_request(
        self,
        clean_prompt: str | list[dict[str, Any]] = "",
        *,
        system_prompt: str | None = None,
        cacheable_prefix: str | None = None,
        enable_prompt_caching: bool = False,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        stop_sequences: list[str] | None = None,
        messages: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        reasoning_effort: str | None = None,
    ) -> tuple[str, dict[str, str], bytes]:
        if (
            (self.api_key is None or not str(self.api_key).strip())
            and self.http_transport is None
            and (not self.spec or self.spec.requires_api_key)
        ):
            raise ProviderUnavailableError(
                f"No API key provided for {self.provider_type.value}. Run 'hex setup' or set the appropriate environment variable."
            )

        messages_to_use: list[dict[str, Any]] | None = (
            sanitize_messages_secrets(messages)
            if messages is not None
            else (sanitize_messages_secrets(clean_prompt) if isinstance(clean_prompt, list) else None)
        )
        if messages_to_use is None and isinstance(clean_prompt, str):
            s = clean_prompt.strip()
            if s.startswith("[") and s.endswith("]"):
                try:
                    parsed = json.loads(s)
                    if isinstance(parsed, list) and all(isinstance(x, dict) and "role" in x for x in parsed):
                        messages_to_use = sanitize_messages_secrets(parsed)
                except Exception:
                    pass

        transport = (
            self.spec.transport
            if self.spec
            else ("anthropic_messages" if self.provider_type == CloudProviderType.ANTHROPIC else "openai_chat")
        )

        norm_tools = self._normalize_tool_definitions(tools)

        if self.provider_type == CloudProviderType.GEMINI:
            api_key = self.api_key or "mock-key"
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model_id}:generateContent?key={api_key}"
            headers = {"content-type": "application/json"}
            contents, eff_system = build_gemini_contents_and_system(
                messages_to_use,
                str(clean_prompt),
                cacheable_prefix=cacheable_prefix,
                system_prompt=system_prompt,
            )
            system_prompt = eff_system
            payload: dict[str, Any] = {
                "contents": contents,
                "generationConfig": {
                    "maxOutputTokens": max_tokens,
                    "temperature": temperature,
                },
            }
            gem_tools, gem_tool_cfg = format_gemini_tools_and_config(norm_tools, tool_choice)
            if gem_tools:
                payload["tools"] = gem_tools
            if gem_tool_cfg:
                payload["toolConfig"] = gem_tool_cfg
            if system_prompt:
                if "gemma" in str(self.model_id).lower():
                    # Gemma models do not support systemInstruction / Developer Instruction on Gemini API
                    if "contents" in payload and payload["contents"]:
                        c0 = payload["contents"][0]
                        if "parts" in c0 and c0["parts"]:
                            c0["parts"][0]["text"] = f"{system_prompt}\n\n{c0['parts'][0]['text']}"
                        else:
                            c0["parts"] = [{"text": system_prompt}]
                    else:
                        payload["contents"] = [{"role": "user", "parts": [{"text": system_prompt}]}]
                else:
                    payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}
            if stop_sequences:
                payload["generationConfig"]["stopSequences"] = stop_sequences
            eff = reasoning_effort or self.reasoning_effort or os.getenv("HEX_REASONING_EFFORT")
            if eff:
                from harness_core.core.reasoning_effort import effort_to_gemini_thinking, model_supports_reasoning
                if model_supports_reasoning("gemini", self.model_id):
                    th_cfg = effort_to_gemini_thinking(eff)
                    if th_cfg is not None:
                        payload["generationConfig"]["thinkingConfig"] = th_cfg
            return url, headers, json.dumps(payload).encode("utf-8")

        elif transport == "anthropic_messages" or self.provider_type == CloudProviderType.ANTHROPIC:
            base = self.base_url or "https://api.anthropic.com/v1"
            url = base if base.endswith("/messages") else f"{base}/messages"
            headers = {
                "x-api-key": self.api_key or "mock-key",
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            if enable_prompt_caching:
                headers["anthropic-beta"] = "prompt-caching-2024-07-31"
            if self.spec and self.spec.extra_headers:
                headers.update(self.spec.extra_headers)

            if messages_to_use is not None:
                formatted_messages: list[dict[str, Any]] = []
                extracted_system: list[str] = []

                for msg in messages_to_use:
                    if not isinstance(msg, dict):
                        continue
                    m_role = str(msg.get("role", "user")).lower()
                    if m_role == "system":
                        extracted_system.append(str(msg.get("content", "")))
                        continue
                    elif m_role == "tool":
                        call_id = msg.get("tool_call_id") or msg.get("tool_use_id") or msg.get("id") or ""
                        tool_content = str(msg.get("content", ""))
                        tool_block = {
                            "type": "tool_result",
                            "tool_use_id": str(call_id),
                            "content": tool_content,
                        }
                        if formatted_messages and formatted_messages[-1].get("role") == "user" and isinstance(formatted_messages[-1].get("content"), list):
                            formatted_messages[-1]["content"].append(tool_block)
                        else:
                            formatted_messages.append({"role": "user", "content": [tool_block]})
                    elif m_role == "assistant":
                        raw_content = msg.get("content")
                        raw_tcs = msg.get("tool_calls") if isinstance(msg.get("tool_calls"), list) else []
                        if isinstance(raw_content, list):
                            new_blocks: list[dict[str, Any]] = []
                            for block in raw_content:
                                if isinstance(block, dict) and block.get("type") == "thinking":
                                    sig = block.get("signature")
                                    if sig and isinstance(sig, str) and sig.strip():
                                        new_blocks.append({
                                            "type": "thinking",
                                            "thinking": block.get("thinking", ""),
                                            "signature": sig.strip(),
                                        })
                                    # Lacks signature or empty signature -> suppress/strip to prevent HTTP 400
                                elif isinstance(block, dict):
                                    new_blocks.append(block)
                                else:
                                    new_blocks.append({"type": "text", "text": str(block)})
                            formatted_messages.append({"role": "assistant", "content": new_blocks})
                        else:
                            sig = msg.get("signature") or (
                                msg.get("thinking_block", {}).get("signature")
                                if isinstance(msg.get("thinking_block"), dict)
                                else None
                            )
                            thinking_val = msg.get("thinking") or (
                                msg.get("thinking_block", {}).get("thinking")
                                if isinstance(msg.get("thinking_block"), dict)
                                else None
                            )
                            if raw_tcs or thinking_val is not None:
                                blocks: list[dict[str, Any]] = []
                                if thinking_val is not None and sig and isinstance(sig, str) and sig.strip():
                                    blocks.append({
                                        "type": "thinking",
                                        "thinking": str(thinking_val),
                                        "signature": sig.strip(),
                                    })
                                if raw_content or (thinking_val is not None and not raw_tcs):
                                    blocks.append({
                                        "type": "text",
                                        "text": str(raw_content or ""),
                                    })
                                for idx, tc in enumerate(raw_tcs):
                                    if not isinstance(tc, dict):
                                        continue
                                    fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
                                    tc_name = str(fn.get("name") or tc.get("tool") or "")
                                    tc_args = self._parse_tool_args_dict(fn.get("arguments") or tc.get("args") or {})
                                    if tc_name:
                                        blocks.append({
                                            "type": "tool_use",
                                            "id": str(tc.get("id") or tc.get("call_id") or f"call_{idx}"),
                                            "name": tc_name,
                                            "input": tc_args,
                                        })
                                formatted_messages.append({"role": "assistant", "content": blocks})
                            else:
                                formatted_messages.append({
                                    "role": "assistant",
                                    "content": str(raw_content or ""),
                                })
                    else:
                        # User message
                        formatted_messages.append({
                            "role": "user",
                            "content": msg.get("content", ""),
                        })

                eff_system = system_prompt or ("\n\n".join(extracted_system) if extracted_system else None)
                payload: dict[str, Any] = {
                    "model": self.model_id,
                    "messages": formatted_messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                }
                if eff_system:
                    if enable_prompt_caching:
                        payload["system"] = [
                            {
                                "type": "text",
                                "text": eff_system,
                                "cache_control": {"type": "ephemeral"},
                            }
                        ]
                    else:
                        payload["system"] = eff_system
                if stop_sequences:
                    payload["stop_sequences"] = stop_sequences
                if norm_tools:
                    payload["tools"] = [
                        {
                            "name": t["name"],
                            "description": t["description"],
                            "input_schema": t["parameters"],
                        }
                        for t in norm_tools
                    ]
                    if tool_choice is not None:
                        payload["tool_choice"] = self._normalize_anthropic_tool_choice(tool_choice)
                eff = reasoning_effort or self.reasoning_effort or os.getenv("HEX_REASONING_EFFORT")
                if eff:
                    from harness_core.core.reasoning_effort import effort_to_anthropic_thinking, model_supports_reasoning
                    if model_supports_reasoning("anthropic", self.model_id):
                        th = effort_to_anthropic_thinking(eff)
                        if th is not None:
                            payload["thinking"] = th
                return url, headers, json.dumps(payload).encode("utf-8")

            if enable_prompt_caching and cacheable_prefix:
                user_content: Any = [
                    {
                        "type": "text",
                        "text": cacheable_prefix,
                        "cache_control": {"type": "ephemeral"},
                    },
                    {
                        "type": "text",
                        "text": str(clean_prompt),
                    },
                ]
            else:
                user_content = str(clean_prompt)

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
            if norm_tools:
                payload["tools"] = [
                    {
                        "name": t["name"],
                        "description": t["description"],
                        "input_schema": t["parameters"],
                    }
                    for t in norm_tools
                ]
                if tool_choice is not None:
                    payload["tool_choice"] = self._normalize_anthropic_tool_choice(tool_choice)
            eff = reasoning_effort or self.reasoning_effort or os.getenv("HEX_REASONING_EFFORT")
            if eff:
                from harness_core.core.reasoning_effort import effort_to_anthropic_thinking, model_supports_reasoning
                if model_supports_reasoning("anthropic", self.model_id):
                    th = effort_to_anthropic_thinking(eff)
                    if th is not None:
                        payload["thinking"] = th
            return url, headers, json.dumps(payload).encode("utf-8")

        else:
            # Default to standard OpenAI-compatible chat completions transport
            base = self.base_url or "https://api.openai.com/v1"
            url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
            headers = {
                "content-type": "application/json",
            }
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            if self.spec and self.spec.extra_headers:
                headers.update(self.spec.extra_headers)

            if messages_to_use is not None:
                openai_messages: list[dict[str, Any]] = []
                if system_prompt and not any(m.get("role") == "system" for m in messages_to_use if isinstance(m, dict)):
                    openai_messages.append({"role": "system", "content": system_prompt})
                for msg in messages_to_use:
                    if not isinstance(msg, dict):
                        continue
                    m_role = str(msg.get("role", "user")).lower()
                    if m_role == "tool":
                        call_id = msg.get("tool_call_id") or msg.get("tool_use_id") or msg.get("id") or ""
                        tool_item: dict[str, Any] = {
                            "role": "tool",
                            "tool_call_id": str(call_id),
                            "content": str(msg.get("content", "")),
                        }
                        if msg.get("name"):
                            tool_item["name"] = str(msg["name"])
                        openai_messages.append(tool_item)
                    else:
                        openai_messages.append(dict(msg))
                payload = {
                    "model": self.model_id,
                    "messages": openai_messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                }
                if stop_sequences:
                    payload["stop"] = stop_sequences
                if "messages" in payload and isinstance(payload["messages"], list):
                    payload["messages"] = self._normalize_openai_messages(payload["messages"])
                if norm_tools:
                    payload["tools"] = [
                        {
                            "type": "function",
                            "function": {
                                "name": t["name"],
                                "description": t["description"],
                                "parameters": t["parameters"],
                            },
                        }
                        for t in norm_tools
                    ]
                    if tool_choice is not None:
                        payload["tool_choice"] = tool_choice
                eff = reasoning_effort or self.reasoning_effort or os.getenv("HEX_REASONING_EFFORT")
                if eff:
                    from harness_core.core.reasoning_effort import OPENAI_EFFORTS, clamp_effort, model_supports_reasoning
                    if model_supports_reasoning(self.provider_type.value, self.model_id):
                        payload["reasoning_effort"] = clamp_effort(eff, OPENAI_EFFORTS)
                return url, headers, json.dumps(payload).encode("utf-8")

            # Order strictly as stable (system) -> context (cacheable_prefix) -> volatile (prompt)
            messages_list: list[dict[str, str]] = []
            if system_prompt:
                messages_list.append({"role": "system", "content": system_prompt})
            if cacheable_prefix:
                messages_list.append({"role": "user", "content": cacheable_prefix})
            messages_list.append({"role": "user", "content": str(clean_prompt)})
            payload = {
                "model": self.model_id,
                "messages": messages_list,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
            if stop_sequences:
                payload["stop"] = stop_sequences
            if norm_tools:
                payload["tools"] = [
                    {
                        "type": "function",
                        "function": {
                            "name": t["name"],
                            "description": t["description"],
                            "parameters": t["parameters"],
                        },
                    }
                    for t in norm_tools
                ]
                if tool_choice is not None:
                    payload["tool_choice"] = tool_choice
            eff = reasoning_effort or self.reasoning_effort or os.getenv("HEX_REASONING_EFFORT")
            if eff:
                from harness_core.core.reasoning_effort import OPENAI_EFFORTS, clamp_effort, model_supports_reasoning
                if model_supports_reasoning(self.provider_type.value, self.model_id):
                    payload["reasoning_effort"] = clamp_effort(eff, OPENAI_EFFORTS)
            return url, headers, json.dumps(payload).encode("utf-8")

    def _extract_tool_calls(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract normalized native tool calls from Gemini, Anthropic, or OpenAI responses."""
        calls: list[dict[str, Any]] = []
        transport = (
            self.spec.transport
            if self.spec
            else ("anthropic_messages" if self.provider_type == CloudProviderType.ANTHROPIC else "openai_chat")
        )

        if self.provider_type == CloudProviderType.GEMINI:
            return extract_gemini_tool_calls(data.get("candidates", []))

        if transport == "anthropic_messages" or self.provider_type == CloudProviderType.ANTHROPIC:
            content = data.get("content", [])
            if isinstance(content, list):
                for idx, item in enumerate(content):
                    if isinstance(item, dict) and item.get("type") == "tool_use" and item.get("name"):
                        calls.append({
                            "id": str(item.get("id") or f"call_{uuid.uuid4().hex[:8]}_{idx}"),
                            "name": str(item["name"]),
                            "arguments": self._parse_tool_args_dict(item.get("input") or {}),
                        })
            return calls

        choices = data.get("choices", [])
        if choices and isinstance(choices, list) and isinstance(choices[0], dict):
            msg = choices[0].get("message", {})
            if isinstance(msg, dict):
                raw_tcs = msg.get("tool_calls")
                if isinstance(raw_tcs, list):
                    for idx, tc in enumerate(raw_tcs):
                        if not isinstance(tc, dict):
                            continue
                        fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
                        tc_name = fn.get("name") or tc.get("tool")
                        if not tc_name:
                            continue
                        calls.append({
                            "id": str(tc.get("id") or tc.get("call_id") or f"call_{uuid.uuid4().hex[:8]}_{idx}"),
                            "name": str(tc_name),
                            "arguments": self._parse_tool_args_dict(fn.get("arguments") or tc.get("args") or {}),
                        })
                elif isinstance(msg.get("function_call"), dict) and msg["function_call"].get("name"):
                    fc = msg["function_call"]
                    calls.append({
                        "id": f"call_{uuid.uuid4().hex[:8]}_0",
                        "name": str(fc["name"]),
                        "arguments": self._parse_tool_args_dict(fc.get("arguments") or {}),
                    })
        return calls

    def _parse_response(
        self, data: dict[str, Any], prompt: str
    ) -> tuple[str, int, int, int, str]:
        text = ""
        in_tokens = 0
        out_tokens = 0
        cached_tokens = 0
        finish_reason = "stop"

        transport = (
            self.spec.transport
            if self.spec
            else ("anthropic_messages" if self.provider_type == CloudProviderType.ANTHROPIC else "openai_chat")
        )

        if self.provider_type == CloudProviderType.GEMINI:
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

        elif transport == "anthropic_messages" or self.provider_type == CloudProviderType.ANTHROPIC:
            thinking_blocks: list[dict[str, Any]] = []
            if "content" in data and isinstance(data["content"], list):
                for item in data["content"]:
                    if isinstance(item, dict):
                        if item.get("type") == "text":
                            text += item.get("text", "")
                        elif item.get("type") == "thinking":
                            thinking_blocks.append(item)
                    elif isinstance(item, str):
                        text += item
            elif "text" in data:
                text = str(data["text"])

            if thinking_blocks:
                data["thinking_blocks"] = thinking_blocks
                data["thinking"] = thinking_blocks[0].get("thinking", "")
                data["signature"] = thinking_blocks[0].get("signature", "")

            usage_dict = data.get("usage", {})
            raw_in = int(usage_dict.get("input_tokens", 0))
            cache_read = int(usage_dict.get("cache_read_input_tokens", 0))
            cache_create = int(usage_dict.get("cache_creation_input_tokens", 0))
            cached_tokens = cache_read
            in_tokens = raw_in + cache_read + cache_create
            out_tokens = int(usage_dict.get("output_tokens", 0))
            finish_reason = data.get("stop_reason") or "stop"

        else:
            # Default to standard OpenAI-compatible response parsing
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

        # Fallback heuristic token count if provider didn't return usage
        if in_tokens <= 0:
            in_tokens = max(1, len(prompt) // 4)
        if out_tokens <= 0:
            out_tokens = max(1, len(text) // 4)

        return text, in_tokens, out_tokens, cached_tokens, finish_reason
