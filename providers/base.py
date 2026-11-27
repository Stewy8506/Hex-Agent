"""Base interfaces and typed data contracts for pluggable model providers."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any
import uuid


def _sanitize_value_strings(
    value: Any,
    *,
    redactor: Any,
    preserve_signature: bool = False,
) -> Any:
    """Recursively redact secrets in string payloads while preserving thinking signatures."""
    if isinstance(value, str):
        return str(redactor(value))
    if isinstance(value, list):
        return [
            _sanitize_value_strings(item, redactor=redactor, preserve_signature=False)
            for item in value
        ]
    if isinstance(value, dict):
        allow_sig = preserve_signature
        sanitized: dict[Any, Any] = {}
        for k, v in value.items():
            if k == "signature" and allow_sig and isinstance(v, str):
                sanitized[k] = v
            elif k == "thinking_block" and preserve_signature and isinstance(v, dict):
                sanitized[k] = _sanitize_value_strings(
                    v, redactor=redactor, preserve_signature=True
                )
            else:
                sanitized[k] = _sanitize_value_strings(
                    v, redactor=redactor, preserve_signature=False
                )
        return sanitized
    return value


def sanitize_messages_secrets(messages: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """Return a copy of structured chat messages with secret patterns redacted from string payloads."""
    if messages is None:
        return None
    from harness_core.gateway import redact_secrets

    sanitized_messages: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        is_assistant = str(msg.get("role", "user")).lower() == "assistant"
        sanitized = _sanitize_value_strings(
            dict(msg),
            redactor=redact_secrets,
            preserve_signature=is_assistant,
        )
        raw_content = msg.get("content")
        clean_content = sanitized.get("content")
        if (
            is_assistant
            and isinstance(raw_content, list)
            and isinstance(clean_content, list)
        ):
            for raw_block, clean_block in zip(raw_content, clean_content):
                if (
                    isinstance(raw_block, dict)
                    and raw_block.get("type") == "thinking"
                    and isinstance(raw_block.get("signature"), str)
                    and isinstance(clean_block, dict)
                ):
                    clean_block["signature"] = raw_block["signature"]
        sanitized_messages.append(sanitized)
    return sanitized_messages


def normalize_tool_definitions(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Normalize gateway or OpenAI tool schemas into canonical {name, description, parameters} dicts."""
    normalized: list[dict[str, Any]] = []
    if not tools:
        return normalized
    for entry in tools:
        if not isinstance(entry, dict):
            continue
        fn = entry.get("function") if entry.get("type") == "function" and isinstance(entry.get("function"), dict) else entry
        name = str(fn.get("name") or fn.get("id") or "").strip()
        if not name:
            continue
        description = str(fn.get("description") or "").strip()
        params = fn.get("parameters") or fn.get("input_schema") or {"type": "object", "properties": {}}
        normalized.append({
            "name": name,
            "description": description,
            "parameters": params if isinstance(params, dict) else {"type": "object", "properties": {}},
        })
    return normalized


def parse_tool_args_dict(raw_args: Any) -> dict[str, Any]:
    """Coerce dict or JSON-encoded tool arguments into a dictionary."""
    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str):
        text = raw_args.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {"raw": raw_args}
    return {}


def build_gemini_contents_and_system(
    messages: list[dict[str, Any]] | None,
    clean_prompt: str,
    cacheable_prefix: str | None = None,
    system_prompt: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Translate structured chat messages (including tool calls/results) into Gemini contents + systemInstruction."""
    if messages is not None:
        contents: list[dict[str, Any]] = []
        extracted_sys: list[str] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            raw_role = str(msg.get("role", "user")).lower()
            if raw_role == "system":
                sys_txt = str(msg.get("content", "")).strip()
                if sys_txt:
                    extracted_sys.append(sys_txt)
                continue
            if raw_role == "tool":
                tool_name = str(msg.get("name") or msg.get("tool_name") or "tool")
                contents.append({
                    "role": "user",
                    "parts": [{
                        "functionResponse": {
                            "name": tool_name,
                            "response": {"result": str(msg.get("content", ""))},
                        }
                    }],
                })
                continue
            m_role = "model" if raw_role == "assistant" else "user"
            m_parts: list[dict[str, Any]] = []
            m_text = str(msg.get("content") or "")
            if m_text:
                m_parts.append({"text": m_text})
            if m_role == "model" and isinstance(msg.get("tool_calls"), list):
                for tc in msg["tool_calls"]:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
                    tc_name = str(fn.get("name") or tc.get("tool") or "")
                    tc_args = parse_tool_args_dict(fn.get("arguments") or tc.get("args") or {})
                    if tc_name:
                        m_parts.append({"functionCall": {"name": tc_name, "args": tc_args}})
            if not m_parts:
                m_parts.append({"text": ""})
            contents.append({"role": m_role, "parts": m_parts})
        if cacheable_prefix:
            contents.insert(0, {"role": "user", "parts": [{"text": cacheable_prefix}]})
        eff_system = system_prompt or ("\n\n".join(extracted_sys) if extracted_sys else None)
        return contents, eff_system

    parts: list[dict[str, Any]] = []
    if cacheable_prefix:
        parts.append({"text": cacheable_prefix})
    parts.append({"text": str(clean_prompt)})
    return [{"role": "user", "parts": parts}], system_prompt


def format_gemini_tools_and_config(
    norm_tools: list[dict[str, Any]],
    tool_choice: Any | None = None,
) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    """Build Gemini tools (functionDeclarations) and toolConfig (functionCallingConfig)."""
    tools_payload: list[dict[str, Any]] | None = None
    if norm_tools:
        tools_payload = [{
            "functionDeclarations": [
                {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["parameters"],
                }
                for t in norm_tools
            ]
        }]

    tool_cfg: dict[str, Any] | None = None
    if tool_choice is not None:
        if isinstance(tool_choice, str):
            mode_norm = tool_choice.strip().lower()
            if mode_norm in ("required", "any"):
                tool_cfg = {"functionCallingConfig": {"mode": "ANY"}}
            elif mode_norm == "none":
                tool_cfg = {"functionCallingConfig": {"mode": "NONE"}}
            elif mode_norm == "auto":
                tool_cfg = {"functionCallingConfig": {"mode": "AUTO"}}
        elif isinstance(tool_choice, dict):
            tc_type = str(tool_choice.get("type") or "").strip().lower()
            fn_obj = tool_choice.get("function") if isinstance(tool_choice.get("function"), dict) else {}
            fn_name = fn_obj.get("name") or tool_choice.get("name")
            if fn_name:
                tool_cfg = {
                    "functionCallingConfig": {
                        "mode": "ANY",
                        "allowedFunctionNames": [str(fn_name)],
                    }
                }
            elif tc_type in ("any", "required"):
                tool_cfg = {"functionCallingConfig": {"mode": "ANY"}}
            elif tc_type == "none":
                tool_cfg = {"functionCallingConfig": {"mode": "NONE"}}
            elif tc_type == "auto":
                tool_cfg = {"functionCallingConfig": {"mode": "AUTO"}}
    return tools_payload, tool_cfg


def extract_gemini_tool_calls(candidates: Any) -> list[dict[str, Any]]:
    """Extract normalized native tool calls from Gemini/Vertex candidate parts."""
    calls: list[dict[str, Any]] = []
    if not (isinstance(candidates, list) and candidates):
        return calls
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        content = cand.get("content") or {}
        if not isinstance(content, dict):
            continue
        parts = content.get("parts") or []
        if not isinstance(parts, list):
            continue
        for idx, part in enumerate(parts):
            if not isinstance(part, dict):
                continue
            fc = part.get("functionCall")
            if isinstance(fc, dict) and fc.get("name"):
                calls.append({
                    "id": f"call_{uuid.uuid4().hex[:8]}_{idx}",
                    "name": str(fc["name"]),
                    "arguments": parse_tool_args_dict(fc.get("args") or {}),
                })
    return calls


def format_messages_for_text_prompt(
    messages: list[dict[str, Any]],
    *,
    system_prompt: str | None = None,
    tools: list[dict[str, Any]] | None = None,
) -> str:
    """Serialize structured messages and optional tool schemas into a readable text prompt."""
    lines: list[str] = []
    if system_prompt:
        lines.append(f"[SYSTEM]\n{system_prompt}")
    if tools:
        tool_summaries = [
            f"- {t['name']}: {t['description']} (params: {json.dumps(t['parameters'], ensure_ascii=False)})"
            for t in normalize_tool_definitions(tools)
        ]
        if tool_summaries:
            lines.append("[TOOLS]\n" + "\n".join(tool_summaries))
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role", "user")).upper()
        content = str(msg.get("content") or "")
        if role == "TOOL":
            t_name = str(msg.get("name") or msg.get("tool_name") or "tool")
            lines.append(f"[TOOL ({t_name})]\n{content}")
        elif role == "ASSISTANT" and isinstance(msg.get("tool_calls"), list) and msg["tool_calls"]:
            tc_strs: list[str] = []
            for tc in msg["tool_calls"]:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
                t_name = fn.get("name") or tc.get("tool") or ""
                t_args = fn.get("arguments") or tc.get("args") or {}
                tc_strs.append(f'<tool_call>{{"name": "{t_name}", "arguments": {json.dumps(parse_tool_args_dict(t_args), ensure_ascii=False)}}}</tool_call>')
            combined = "\n".join(part for part in (content, "\n".join(tc_strs)) if part)
            lines.append(f"[ASSISTANT]\n{combined}")
        else:
            lines.append(f"[{role}]\n{content}")
    return "\n\n".join(lines)


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
    tool_calls: list[dict[str, Any]] = field(default_factory=list)

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
