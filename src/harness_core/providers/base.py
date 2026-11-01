"""Base interfaces and typed data contracts for pluggable model providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ModelUsage:
    """Token consumption and financial accounting for a model generation call."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    cached_input_tokens: int = 0

    def __post_init__(self) -> None:
        if self.total_tokens == 0 and (self.input_tokens > 0 or self.output_tokens > 0):
            self.total_tokens = self.input_tokens + self.output_tokens
        self.cost_usd = round(float(self.cost_usd), 6)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def add(self, other: ModelUsage) -> ModelUsage:
        return ModelUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            cost_usd=round(self.cost_usd + other.cost_usd, 6),
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
        )


@dataclass
class ModelResponse:
    """Standardized response from any model provider."""

    text: str
    model_id: str
    provider_id: str
    usage: ModelUsage
    latency_ms: float = 0.0
    finish_reason: str = "stop"
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "model_id": self.model_id,
            "provider_id": self.provider_id,
            "usage": self.usage.to_dict(),
            "latency_ms": round(self.latency_ms, 2),
            "finish_reason": self.finish_reason,
            "raw": self.raw,
        }


@dataclass
class ProviderCapabilities:
    """Capabilities and architectural limits of a model provider."""

    supports_system_prompt: bool = True
    supports_streaming: bool = False
    max_context_window: int = 32768
    quantization: str | None = None
    local_unified_memory: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ModelProviderError(Exception):
    """Base exception for model provider operations."""


class PolicyBlockedError(ModelProviderError):
    """Raised when privacy policy or data classification forbids execution on the target tier."""


class BudgetExceededError(ModelProviderError):
    """Raised when task cost ceiling, output token limit, or request count is breached."""


class ProviderUnavailableError(ModelProviderError):
    """Raised when a required backend or runtime is inaccessible."""


class ModelProvider(ABC):
    """Abstract base class for all Tier-1 and Tier-2 model providers."""

    provider_id: str
    model_id: str

    @abstractmethod
    def generate(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
        stop_sequences: list[str] | None = None,
        **kwargs: Any,
    ) -> ModelResponse:
        """Generate text from the underlying model backend."""

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if the backend runtime/endpoint is available for requests."""

    @abstractmethod
    def get_capabilities(self) -> ProviderCapabilities:
        """Return provider architectural capabilities and limits."""
