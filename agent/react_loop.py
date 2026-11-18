"""Continuous Multi-Turn ReAct Loop Engine Core.

Implements autonomous tool-calling loops with iterative thought/action/observation
cycles, policy validation, structured tool parsing, ledger recording, and clean
termination conditions.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import inspect
import json
from pathlib import Path
import random
import re
import socket
import time
from typing import Any, Callable
import uuid

from harness_core.cognition.context_compressor import (
    CHARS_PER_TOKEN,
    CompactionConfig,
    CompactionResult,
    ContextCompactor,
    elide_middle,
    estimate_message_tokens,
    estimate_messages_tokens,
    sanitize_tool_pairs,
)


@dataclass
class ReActConfig:
    """Configuration parameters for the ReAct execution loop."""

    max_turns: int = 30
    temperature: float = 0.2
    system_prompt: str = ""
    budget_tokens: int = 100_000
    stop_sequences: list[str] = field(default_factory=list)
    max_retries: int = 3
    initial_retry_delay: float = 0.5
    backoff_factor: float = 2.0
    retry_jitter: float = 0.1
    on_event: Callable[[str, dict[str, Any]], None] | None = None
    moa_config: Any | None = None


@dataclass
class ToolCallRequest:
    """Structured request to invoke a specific tool."""

    call_id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCallResult:
    """Execution output from a single tool invocation."""

    call_id: str
    name: str
    output: Any
    is_error: bool = False
    duration_ms: float = 0.0


@dataclass
class ReActTurn:
    """A single reasoning and execution turn in the ReAct loop."""

    turn_index: int
    thought: str
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    tool_results: list[ToolCallResult] = field(default_factory=list)
    latency_ms: float = 0.0
    moa_result: Any | None = None

    def __contains__(self, key: Any) -> bool:
        return hasattr(self, str(key))

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            raise IndexError(f"ReActTurn does not support integer index {key}")
        if not hasattr(self, str(key)):
            raise KeyError(key)
        return getattr(self, str(key))

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_index": self.turn_index,
            "thought": self.thought,
            "tool_calls": [
                {"call_id": c.call_id, "name": c.name, "arguments": c.arguments}
                for c in self.tool_calls
            ],
            "tool_results": [
                {
                    "call_id": r.call_id,
                    "name": r.name,
                    "output": r.output,
                    "is_error": r.is_error,
                    "duration_ms": r.duration_ms,
                }
                for r in self.tool_results
            ],
            "latency_ms": self.latency_ms,
            "moa_result": (
                self.moa_result.to_dict()
                if hasattr(self.moa_result, "to_dict")
                else self.moa_result
            ),
        }


@dataclass
class ReActSessionResult:
    """Aggregate result of an entire ReAct loop session."""

    success: bool
    final_answer: str
    total_turns: int
    turns: list[ReActTurn] = field(default_factory=list)
    termination_reason: str = "finish"  # "finish", "max_turns", "error", "no_tool_calls"
    compaction_result: Any | None = None
    retry_count: int = 0
    friction_info: dict[str, Any] = field(default_factory=dict)


def _safe_json_loads(raw: str) -> Any:
    """Load JSON with fallback recovery for common Python-like formatting."""
    clean = raw.strip()
    try:
        return json.loads(clean)
    except Exception:
        pass
    # Quick normalization for Python booleans/None
    normalized = re.sub(r"\bTrue\b", "true", clean)
    normalized = re.sub(r"\bFalse\b", "false", normalized)
    normalized = re.sub(r"\bNone\b", "null", normalized)
    return json.loads(normalized)


def _find_json_objects(text: str) -> list[tuple[int, int, str]]:
    """Scan text and locate all balanced JSON objects accounting for strings and escapes."""
    results: list[tuple[int, int, str]] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "{":
            start = i
            depth = 1
            in_str = False
            escape = False
            i += 1
            while i < n and depth > 0:
                c = text[i]
                if escape:
                    escape = False
                elif c == "\\":
                    escape = True
                elif c == '"':
                    in_str = not in_str
                elif not in_str:
                    if c == "{":
                        depth += 1
                    elif c == "}":
                        depth -= 1
                i += 1
            if depth == 0:
                results.append((start, i, text[start:i]))
                continue
        i += 1
    return results


class ReActLoop:
    """Continuous Multi-Turn ReAct tool-calling engine.

    Cycles through model reasoning, structured tool invocation, observation
    recording, and completion verification until resolution or turn exhaustion.
    """

    def __init__(
        self,
        model_caller: Callable[..., Any] | None = None,
        gateway: Any | None = None,
        workspace_root: Path | str | None = None,
        config: ReActConfig | None = None,
        ledger: Any | None = None,
        context_compactor: ContextCompactor | None = None,
        session_id: str | None = None,
        guardrails_manager: Any | None = None,
        title_synthesizer: Any | None = None,
        approval_lease_manager: Any | None = None,
    ) -> None:
        self.model_caller = model_caller
        self.gateway = gateway
        self.workspace_root = Path(workspace_root).resolve() if workspace_root else None
        self.config = config or ReActConfig()
        self.ledger = ledger
        self.context_compactor = context_compactor
        self.session_id = session_id or f"session_{uuid.uuid4().hex[:12]}"
        if title_synthesizer is not None:
            self.title_synthesizer = title_synthesizer
        else:
            try:
                from harness_core.cognition.title_synthesizer import SessionTitleSynthesizer
                self.title_synthesizer = SessionTitleSynthesizer(model_caller=self.model_caller, ledger=self.ledger)
            except Exception:
                self.title_synthesizer = None
        self.messages: list[dict[str, Any]] = []
        self.turns: list[ReActTurn] = []
        self.last_compaction_result: Any | None = None
        self.retry_count: int = 0
        self.turn_repair_count: int = 0
        self.repair_attempts: int = 0
        self.compiler_errors_count: int = 0
        self.friction_detected: bool = False
        self._friction_reasons: list[str] = []
        self._failed_tool_sig_counts: dict[str, int] = {}
        self._gateway_task_ids: dict[str, str] = {}
        self._current_turn_index: int = 0
        self._current_tool_seq: int = 0
        if guardrails_manager is not None:
            self.guardrails_manager = guardrails_manager
        elif self.workspace_root:
            from harness_core.cognition.memory import GuardrailsManager
            self.guardrails_manager = GuardrailsManager(workspace_root=self.workspace_root)
        else:
            self.guardrails_manager = None
        self._consecutive_tool_failures: dict[str, int] = {}
        try:
            from harness_core.cognition.repetition_sentry import ActionLoopDetector, RepetitionSentry
            self.action_loop_detector = ActionLoopDetector()
            self.repetition_sentry = RepetitionSentry()
        except Exception:
            self.action_loop_detector = None
            self.repetition_sentry = None
        self.moa_config = getattr(self.config, "moa_config", None)
        self.moa = None
        if self.moa_config is not None and getattr(self.moa_config, "enabled", True):
            try:
                from harness_core.core.moa import MixtureOfAgents
                self.moa = MixtureOfAgents(self.moa_config, model_caller=self.model_caller)
            except Exception:
                self.moa = None
        if approval_lease_manager is not None:
            self.approval_lease_manager = approval_lease_manager
        elif hasattr(self.gateway, "approval_lease_manager") and getattr(self.gateway, "approval_lease_manager", None):
            self.approval_lease_manager = getattr(self.gateway, "approval_lease_manager")
        else:
            try:
                from harness_core.core.approval_lease import ApprovalLeaseManager
                self.approval_lease_manager = ApprovalLeaseManager(ledger=self.ledger)
            except Exception:
                self.approval_lease_manager = None


    def _check_and_trigger_friction(
        self, reason: str, details: dict[str, Any] | None = None
    ) -> None:
        """Record friction detection and emit lifecycle event."""
        if reason not in self._friction_reasons:
            self._friction_reasons.append(reason)
        if not self.friction_detected:
            self.friction_detected = True
            payload = {
                "session_id": self.session_id,
                "reason": reason,
                "retry_count": self.retry_count,
                "turn_repair_count": self.turn_repair_count,
                "repair_attempts": self.repair_attempts,
                "compiler_errors_count": self.compiler_errors_count,
                "details": details or {},
            }
            self._emit_event("friction_detected", payload)

    def record_repair_attempt(self, attempt: int | None = None) -> None:
        """Record a repair attempt in the loop and check friction threshold."""
        if attempt is not None:
            self.repair_attempts = attempt
        else:
            self.repair_attempts += 1
        if self.repair_attempts >= 3:
            self._check_and_trigger_friction(
                f"repair_attempts_threshold_reached (repair_attempts={self.repair_attempts})",
                {"repair_attempts": self.repair_attempts},
            )

    # -------------------------------------------------------------------------
    # Response and Tool Parsing
    # -------------------------------------------------------------------------

    @staticmethod
    def _extract_response_text(response: Any) -> str:
        """Extract plain string text from various model provider response formats."""
        if isinstance(response, str):
            return response
        if isinstance(response, dict):
            if "text" in response:
                return str(response["text"])
            if "content" in response:
                return str(response["content"])
            return json.dumps(response, default=str)
        if hasattr(response, "text"):
            return str(response.text)
        if hasattr(response, "content"):
            return str(response.content)
        return str(response)

    @classmethod
    def parse_tool_calls(cls, response: Any) -> tuple[str, list[ToolCallRequest]]:
        """Parse thought and structured tool call requests from model output.

        Supports:
          1. Native dictionary responses with "tool_calls", "tool", or "name".
          2. XML/tag blocks: <tool_call>{"name": ..., "arguments": ...}</tool_call>
          3. Markdown code blocks: ```json {"tool_calls": [...]} ```
          4. Top-level raw JSON strings.
          5. Embedded JSON tool invocation payloads.
        """
        # Case 1: Dict payload
        if isinstance(response, dict):
            thought = str(response.get("thought") or response.get("reasoning") or response.get("text") or "")
            # Check for "tool_calls" list
            if "tool_calls" in response and isinstance(response["tool_calls"], list):
                calls: list[ToolCallRequest] = []
                for idx, tc in enumerate(response["tool_calls"]):
                    name = tc.get("name") or tc.get("tool") or tc.get("function", {}).get("name")
                    args = tc.get("arguments") or tc.get("args") or tc.get("function", {}).get("arguments", {})
                    if isinstance(args, str):
                        try:
                            args = _safe_json_loads(args)
                        except Exception:
                            args = {"raw": args}
                    cid = tc.get("call_id") or tc.get("id") or f"call_{uuid.uuid4().hex[:8]}_{idx}"
                    if name:
                        calls.append(ToolCallRequest(call_id=str(cid), name=str(name), arguments=args or {}))
                return thought, calls

            # Check for direct single tool
            if "tool" in response or ("name" in response and ("args" in response or "arguments" in response)):
                name = response.get("name") or response.get("tool")
                args = response.get("arguments") or response.get("args") or {}
                if isinstance(args, str):
                    try:
                        args = _safe_json_loads(args)
                    except Exception:
                        args = {"raw": args}
                cid = response.get("call_id") or response.get("id") or f"call_{uuid.uuid4().hex[:8]}"
                if name:
                    return thought, [ToolCallRequest(call_id=str(cid), name=str(name), arguments=args or {})]

        text = cls._extract_response_text(response)
        requests: list[ToolCallRequest] = []

        # Case 2: XML tag blocks: <tool_call(?:\s+name="...")?>...</tool_call>
        xml_matches = list(
            re.finditer(
                r"<tool_call(?:\s+name=[\"'](?P<tag_name>[^\"']+)[\"'])?>(?P<body>.*?)</tool_call>",
                text,
                flags=re.DOTALL | re.IGNORECASE,
            )
        )
        if xml_matches:
            for idx, match in enumerate(xml_matches):
                tag_name = match.group("tag_name")
                body = match.group("body").strip()
                cid = f"call_{uuid.uuid4().hex[:8]}_{idx}"
                try:
                    parsed = _safe_json_loads(body)
                    if isinstance(parsed, dict):
                        name = parsed.get("name") or parsed.get("tool") or tag_name
                        # If args explicitly present, use them; otherwise if tag_name exists or parsed is dict of args
                        if "arguments" in parsed or "args" in parsed:
                            args = parsed.get("arguments") or parsed.get("args") or {}
                        elif tag_name or ("name" not in parsed and "tool" not in parsed):
                            args = parsed
                        else:
                            args = {}

                        if isinstance(args, str):
                            try:
                                args = _safe_json_loads(args)
                            except Exception:
                                args = {"raw": args}
                        if name:
                            requests.append(ToolCallRequest(call_id=cid, name=str(name), arguments=args or {}))
                    elif isinstance(parsed, list):
                        for j, item in enumerate(parsed):
                            if isinstance(item, dict):
                                name = item.get("name") or item.get("tool") or tag_name
                                args = item.get("arguments") or item.get("args") or {}
                                if isinstance(args, str):
                                    try:
                                        args = _safe_json_loads(args)
                                    except Exception:
                                        args = {"raw": args}
                                if name:
                                    requests.append(ToolCallRequest(call_id=f"{cid}_{j}", name=str(name), arguments=args or {}))
                except Exception:
                    # Check for nested <name>...</name> and <arguments>...</arguments>
                    name_m = re.search(r"<name>(.*?)</name>", body, re.DOTALL | re.IGNORECASE)
                    args_m = re.search(r"<arguments>(.*?)</arguments>", body, re.DOTALL | re.IGNORECASE)
                    name = name_m.group(1).strip() if name_m else tag_name
                    if name:
                        args: dict[str, Any] = {}
                        if args_m:
                            try:
                                args = _safe_json_loads(args_m.group(1).strip())
                            except Exception:
                                args = {"raw": args_m.group(1).strip()}
                        requests.append(ToolCallRequest(call_id=cid, name=str(name), arguments=args))

            if requests:
                # Thought is text outside all <tool_call> tags
                thought = re.sub(
                    r"<tool_call(?:\s+name=[\"'][^\"']+[\"'])?>.*?</tool_call>",
                    "",
                    text,
                    flags=re.DOTALL | re.IGNORECASE,
                ).strip()
                thought_match = re.search(r"<thought>(.*?)</thought>", thought, re.DOTALL | re.IGNORECASE)
                if thought_match:
                    thought = thought_match.group(1).strip()
                return thought, requests

        # Case 3: Markdown code blocks with JSON
        code_blocks = re.findall(r"```(?:json)?\s*([\s\S]*?)\s*```", text, flags=re.IGNORECASE)
        for idx, block in enumerate(code_blocks):
            try:
                parsed = _safe_json_loads(block.strip())
                block_requests: list[ToolCallRequest] = []
                if isinstance(parsed, dict):
                    if "tool_calls" in parsed and isinstance(parsed["tool_calls"], list):
                        for j, tc in enumerate(parsed["tool_calls"]):
                            name = tc.get("name") or tc.get("tool")
                            args = tc.get("arguments") or tc.get("args") or {}
                            if isinstance(args, str):
                                try:
                                    args = _safe_json_loads(args)
                                except Exception:
                                    args = {"raw": args}
                            cid = tc.get("call_id") or tc.get("id") or f"call_{uuid.uuid4().hex[:8]}_{idx}_{j}"
                            if name:
                                block_requests.append(ToolCallRequest(call_id=str(cid), name=str(name), arguments=args or {}))
                    elif "tool" in parsed or ("name" in parsed and ("args" in parsed or "arguments" in parsed)):
                        name = parsed.get("name") or parsed.get("tool")
                        args = parsed.get("arguments") or parsed.get("args") or {}
                        if isinstance(args, str):
                            try:
                                args = _safe_json_loads(args)
                            except Exception:
                                args = {"raw": args}
                        cid = parsed.get("call_id") or parsed.get("id") or f"call_{uuid.uuid4().hex[:8]}_{idx}"
                        if name:
                            block_requests.append(ToolCallRequest(call_id=str(cid), name=str(name), arguments=args or {}))
                elif isinstance(parsed, list):
                    for j, item in enumerate(parsed):
                        if isinstance(item, dict) and ("name" in item or "tool" in item):
                            name = item.get("name") or item.get("tool")
                            args = item.get("arguments") or item.get("args") or {}
                            if isinstance(args, str):
                                try:
                                    args = _safe_json_loads(args)
                                except Exception:
                                    args = {"raw": args}
                            cid = item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:8]}_{idx}_{j}"
                            if name:
                                block_requests.append(ToolCallRequest(call_id=str(cid), name=str(name), arguments=args or {}))

                if block_requests:
                    requests.extend(block_requests)
            except Exception:
                continue

        if requests:
            thought = re.sub(r"```(?:json)?\s*[\s\S]*?\s*```", "", text, flags=re.IGNORECASE).strip()
            thought_match = re.search(r"<thought>(.*?)</thought>", thought, re.DOTALL | re.IGNORECASE)
            if thought_match:
                thought = thought_match.group(1).strip()
            return thought, requests

        # Case 4: Raw JSON string or embedded JSON object
        stripped = text.strip()
        if (stripped.startswith("{") and stripped.endswith("}")) or (stripped.startswith("[") and stripped.endswith("]")):
            try:
                parsed = _safe_json_loads(stripped)
                if isinstance(parsed, dict):
                    thought = str(parsed.get("thought") or parsed.get("reasoning") or "")
                    if "tool_calls" in parsed and isinstance(parsed["tool_calls"], list):
                        calls = []
                        for j, tc in enumerate(parsed["tool_calls"]):
                            name = tc.get("name") or tc.get("tool")
                            args = tc.get("arguments") or tc.get("args") or {}
                            if isinstance(args, str):
                                try:
                                    args = _safe_json_loads(args)
                                except Exception:
                                    args = {"raw": args}
                            cid = tc.get("call_id") or tc.get("id") or f"call_{uuid.uuid4().hex[:8]}_{j}"
                            if name:
                                calls.append(ToolCallRequest(call_id=str(cid), name=str(name), arguments=args or {}))
                        return thought, calls
                    if "tool" in parsed or ("name" in parsed and ("args" in parsed or "arguments" in parsed)):
                        name = parsed.get("name") or parsed.get("tool")
                        args = parsed.get("arguments") or parsed.get("args") or {}
                        if isinstance(args, str):
                            try:
                                args = _safe_json_loads(args)
                            except Exception:
                                args = {"raw": args}
                        cid = parsed.get("call_id") or parsed.get("id") or f"call_{uuid.uuid4().hex[:8]}"
                        if name:
                            return thought, [ToolCallRequest(call_id=str(cid), name=str(name), arguments=args or {})]
                elif isinstance(parsed, list):
                    calls = []
                    for j, item in enumerate(parsed):
                        if isinstance(item, dict) and ("name" in item or "tool" in item):
                            name = item.get("name") or item.get("tool")
                            args = item.get("arguments") or item.get("args") or {}
                            if isinstance(args, str):
                                try:
                                    args = _safe_json_loads(args)
                                except Exception:
                                    args = {"raw": args}
                            cid = item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:8]}_{j}"
                            if name:
                                calls.append(ToolCallRequest(call_id=str(cid), name=str(name), arguments=args or {}))
                    if calls:
                        return "", calls
            except Exception:
                pass

        # Case 5: Embedded JSON search in text with balanced brace scanning
        json_objs = _find_json_objects(text)
        for start_idx, end_idx, candidate in json_objs:
            try:
                parsed = _safe_json_loads(candidate)
                if isinstance(parsed, dict) and ("tool" in parsed or ("name" in parsed and ("args" in parsed or "arguments" in parsed))):
                    name = parsed.get("name") or parsed.get("tool")
                    args = parsed.get("arguments") or parsed.get("args") or {}
                    if isinstance(args, str):
                        try:
                            args = _safe_json_loads(args)
                        except Exception:
                            args = {"raw": args}
                    cid = parsed.get("call_id") or parsed.get("id") or f"call_{uuid.uuid4().hex[:8]}"
                    if name:
                        thought = (text[:start_idx] + " " + text[end_idx:]).strip()
                        return thought, [ToolCallRequest(call_id=str(cid), name=str(name), arguments=args or {})]
            except Exception:
                continue

        # Case 6: No tool calls emitted
        thought = text.strip()
        thought_match = re.search(r"<thought>(.*?)</thought>", thought, re.DOTALL | re.IGNORECASE)
        if thought_match:
            thought = thought_match.group(1).strip()
        return thought, []

    # -------------------------------------------------------------------------
    # Model Invocation & Tool Execution
    # -------------------------------------------------------------------------

    @staticmethod
    def _is_transient_error(exc: Exception) -> bool:
        """Check if an exception represents a transient failure eligible for retry."""
        # Non-transient standard exceptions
        if isinstance(exc, (ValueError, TypeError, KeyError, AttributeError, SyntaxError)):
            return False

        try:
            from harness_core.providers.base import BudgetExceededError, PolicyBlockedError
            if isinstance(exc, (BudgetExceededError, PolicyBlockedError)):
                return False
        except Exception:
            pass

        # Timeout & connection errors
        if isinstance(exc, (TimeoutError, ConnectionError)):
            return True
        try:
            if isinstance(exc, socket.timeout):
                return True
        except Exception:
            pass

        # Explicit HTTP status code attribute (e.g. urllib.error.HTTPError, custom exceptions)
        status = (
            getattr(exc, "code", None)
            or getattr(exc, "status_code", None)
            or getattr(exc, "status", None)
            or getattr(exc, "http_status", None)
        )
        if isinstance(status, int):
            if status in (429, 502, 503, 504, 529):
                return True
            if status in (400, 401, 403, 404):
                return False

        err_str = str(exc).lower()

        # Explicit non-transient markers
        non_transient_markers = (
            "400 bad request",
            "http 400",
            "401 unauthorized",
            "http 401",
            "403 forbidden",
            "http 403",
            "404 not found",
            "http 404",
        )
        if any(m in err_str for m in non_transient_markers):
            return False

        # Explicit transient markers
        transient_markers = (
            "429",
            "503",
            "502",
            "504",
            "529",
            "rate limit",
            "ratelimit",
            "too many requests",
            "overloaded",
            "timeout",
            "timed out",
            "service unavailable",
            "temporarily unavailable",
            "connection reset",
            "connection refused",
            "socket error",
        )
        return any(m in err_str for m in transient_markers)

    def _invoke_caller(self, messages: list[dict[str, Any]]) -> Any:
        """Invoke configured model_caller trying multiple parameter signatures."""
        if self.model_caller is None:
            raise RuntimeError("No model_caller configured for ReActLoop")

        res: Any = None
        # Try passing messages list directly
        try:
            res = self.model_caller(messages)
        except TypeError:
            # Try keyword argument messages=messages
            try:
                res = self.model_caller(messages=messages)
            except TypeError:
                # Fallback to string prompt formatted from messages
                prompt_str = self._format_messages_to_prompt(messages)
                res = self.model_caller(prompt_str)

        # Repetition Sentry content-sanity check for runaway repetition
        try:
            from harness_core.cognition.repetition_sentry import (
                REPETITION_LOOP_INTERRUPTED,
                generate_steering_prompt,
                is_runaway_repetition,
            )
            raw_text = self._extract_response_text(res)
            if raw_text and is_runaway_repetition(raw_text):
                steering = generate_steering_prompt("token_repetition", {})
                return f"{REPETITION_LOOP_INTERRUPTED}\n{steering}"
        except Exception:
            pass

        return res

    def _call_model(self, messages: list[dict[str, Any]]) -> Any:
        """Invoke configured model_caller with transient error retry resilience."""
        max_retries = getattr(self.config, "max_retries", 3)
        initial_delay = getattr(self.config, "initial_retry_delay", 0.5)
        backoff_factor = getattr(self.config, "backoff_factor", 2.0)
        jitter = getattr(self.config, "retry_jitter", 0.1)

        attempt = 0
        while True:
            try:
                return self._invoke_caller(messages)
            except Exception as exc:
                if not self._is_transient_error(exc) or attempt >= max_retries:
                    raise
                self.retry_count += 1
                if self.retry_count >= 3:
                    self._check_and_trigger_friction(
                        f"retry_count_threshold_reached (retry_count={self.retry_count})",
                        {"retry_count": self.retry_count},
                    )
                delay = initial_delay * (backoff_factor ** attempt) + random.uniform(0, jitter)
                time.sleep(delay)
                attempt += 1

    @staticmethod
    def _format_messages_to_prompt(messages: list[dict[str, Any]]) -> str:
        """Convert a list of structured chat messages to a plaintext prompt string."""
        lines: list[str] = []
        for msg in messages:
            role = str(msg.get("role", "user")).upper()
            content = str(msg.get("content", ""))
            if role == "TOOL":
                tool_name = msg.get("name", "tool")
                lines.append(f"[TOOL ({tool_name})]\n{content}")
            else:
                lines.append(f"[{role}]\n{content}")
        return "\n\n".join(lines)

    def _execute_tool_call(self, call: ToolCallRequest) -> ToolCallResult:
        """Execute a single tool call through the gateway and record execution metrics."""
        # KV-cache prefix defense: assert system prompt immutability
        initial_sys_prompt = getattr(self.config, "system_prompt", None)
        initial_msg0 = (
            dict(self.messages[0])
            if (getattr(self, "messages", None) and self.messages and self.messages[0].get("role") == "system")
            else None
        )

        try:
            return self._execute_tool_call_inner(call)
        finally:
            assert getattr(self.config, "system_prompt", None) == initial_sys_prompt, (
                "System prompt KV-cache prefix was mutated during tool execution!"
            )
            if initial_msg0 is not None and getattr(self, "messages", None) and self.messages:
                assert self.messages[0] == initial_msg0, (
                    "Initial system message in messages[0] was mutated during tool execution!"
                )

    _execute_tool = _execute_tool_call

    def _execute_tool_call_inner(self, call: ToolCallRequest) -> ToolCallResult:
        """Internal execution routine for a tool call."""
        start_t = time.perf_counter()

        # Check temporal approval lease for auto-approval
        lease_mgr = getattr(self, "approval_lease_manager", None) or (
            getattr(self.gateway, "approval_lease_manager", None) if self.gateway else None
        )
        if lease_mgr is not None and hasattr(lease_mgr, "check_lease"):
            try:
                has_lease, lease_id = lease_mgr.check_lease(call.name, call.arguments)
                if has_lease:
                    setattr(call, "policy_decision", "LEASE_APPROVED")
            except Exception:
                pass
        if not self.gateway:
            return ToolCallResult(
                call_id=call.call_id,
                name=call.name,
                output="Error: No ToolGateway configured",
                is_error=True,
                duration_ms=0.0,
            )

        workspace_dir = Path(self.workspace_root) if self.workspace_root else Path.cwd()

        if self.session_id and hasattr(self.gateway, "_active_session_key"):
            try:
                self.gateway._active_session_key = self.session_id
            except Exception:
                pass

        if call.name == "code_kernel" and isinstance(call.arguments, dict):
            if "owner" not in call.arguments and "session_id" not in call.arguments and self.session_id:
                call.arguments["owner"] = self.session_id

        # Mandatory gateway boundary (B02): the standard ToolGateway is the only
        # execution path for registered tools — policy, approval, and journaling
        # all live in invoke(). Direct handler dispatch below is reserved for
        # non-standard gateway doubles that expose no policy engine.
        from harness_core.gateway import ToolGateway

        if isinstance(self.gateway, ToolGateway):
            return self._invoke_via_gateway(call, workspace_dir, start_t)

        # Gateway with .tools dictionary (standard HEX ToolGateway)
        if hasattr(self.gateway, "tools") and isinstance(self.gateway.tools, dict):
            if call.name in self.gateway.tools:
                spec = self.gateway.tools[call.name]
                handler = getattr(spec, "handler", None)
                if not callable(handler) and isinstance(spec, dict):
                    handler = spec.get("handler")
                elif not callable(handler) and callable(spec):
                    handler = spec

                if not callable(handler):
                    duration_ms = (time.perf_counter() - start_t) * 1000
                    return ToolCallResult(
                        call_id=call.call_id,
                        name=call.name,
                        output=f"Error: Tool '{call.name}' has no callable handler",
                        is_error=True,
                        duration_ms=duration_ms,
                    )

                try:
                    # Check parameters: some handlers take (workspace_dir, args), others take (args)
                    sig = inspect.signature(handler)
                    if len(sig.parameters) == 1:
                        result = handler(call.arguments)
                    else:
                        result = handler(workspace_dir, call.arguments)

                    duration_ms = (time.perf_counter() - start_t) * 1000
                    is_error = False
                    if isinstance(result, dict):
                        if result.get("ok") is False:
                            is_error = True
                        elif "error" in result and bool(result["error"]):
                            is_error = True
                        elif result.get("status") in ("error", "DENIED"):
                            is_error = True
                        elif result.get("status") == "failed" and call.name not in (
                            "read_terminal", "start_process", "kill_process", "list_processes"
                        ):
                            is_error = True
                    return ToolCallResult(
                        call_id=call.call_id,
                        name=call.name,
                        output=result,
                        is_error=is_error,
                        duration_ms=duration_ms,
                    )
                except Exception as e:
                    duration_ms = (time.perf_counter() - start_t) * 1000
                    return ToolCallResult(
                        call_id=call.call_id,
                        name=call.name,
                        output=f"Error: {e}",
                        is_error=True,
                        duration_ms=duration_ms,
                    )
            else:
                duration_ms = (time.perf_counter() - start_t) * 1000
                return ToolCallResult(
                    call_id=call.call_id,
                    name=call.name,
                    output=f"Error: Tool '{call.name}' not found in gateway",
                    is_error=True,
                    duration_ms=duration_ms,
                )

        # Fallback for gateways exposing .execute(name, arguments)
        if hasattr(self.gateway, "execute") and callable(self.gateway.execute):
            try:
                result = self.gateway.execute(call.name, call.arguments)
                duration_ms = (time.perf_counter() - start_t) * 1000
                is_error = False
                if isinstance(result, dict):
                    if result.get("ok") is False:
                        is_error = True
                    elif "error" in result and bool(result["error"]):
                        is_error = True
                    elif result.get("status") in ("error", "DENIED"):
                        is_error = True
                    elif result.get("status") == "failed" and call.name not in (
                        "read_terminal", "start_process", "kill_process", "list_processes"
                    ):
                        is_error = True
                return ToolCallResult(
                    call_id=call.call_id,
                    name=call.name,
                    output=result,
                    is_error=is_error,
                    duration_ms=duration_ms,
                )
            except Exception as e:
                duration_ms = (time.perf_counter() - start_t) * 1000
                return ToolCallResult(
                    call_id=call.call_id,
                    name=call.name,
                    output=f"Error: {e}",
                    is_error=True,
                    duration_ms=duration_ms,
                )

        # Fallback for gateways exposing .invoke(name, arguments, ...)
        if hasattr(self.gateway, "invoke") and callable(self.gateway.invoke):
            try:
                sig = inspect.signature(self.gateway.invoke)
                if len(sig.parameters) >= 3:
                    result = self.gateway.invoke(call.name, call.arguments, None)
                elif len(sig.parameters) == 2:
                    result = self.gateway.invoke(call.name, call.arguments)
                else:
                    result = self.gateway.invoke(call.name)

                duration_ms = (time.perf_counter() - start_t) * 1000
                is_error = False
                if isinstance(result, dict):
                    if result.get("ok") is False:
                        is_error = True
                    elif "error" in result and bool(result["error"]):
                        is_error = True
                    elif result.get("status") in ("error", "DENIED"):
                        is_error = True
                    elif result.get("status") == "failed" and call.name not in (
                        "read_terminal", "start_process", "kill_process", "list_processes"
                    ):
                        is_error = True
                return ToolCallResult(
                    call_id=call.call_id,
                    name=call.name,
                    output=result,
                    is_error=is_error,
                    duration_ms=duration_ms,
                )
            except Exception as e:
                duration_ms = (time.perf_counter() - start_t) * 1000
                return ToolCallResult(
                    call_id=call.call_id,
                    name=call.name,
                    output=f"Error: {e}",
                    is_error=True,
                    duration_ms=duration_ms,
                )

        duration_ms = (time.perf_counter() - start_t) * 1000
        return ToolCallResult(
            call_id=call.call_id,
            name=call.name,
            output=f"Error: Tool '{call.name}' not found in gateway",
            is_error=True,
            duration_ms=duration_ms,
        )

    # -------------------------------------------------------------------------
    # Mandatory Gateway Dispatch (B02)
    # -------------------------------------------------------------------------

    def _resolve_gateway_task_id(self, workspace_dir: Path) -> str:
        """Map the active ReAct task onto a ledger task the gateway can policy-check."""
        tid = getattr(self, "active_task_id", None) or self.session_id
        cached = self._gateway_task_ids.get(tid)
        if cached:
            return cached
        ledger = self.gateway.ledger
        resolved = tid
        if ledger.get_task(tid) is None:
            record = ledger.submit_task({
                "goal": getattr(self, "_active_prompt", None) or f"ReAct session {self.session_id}",
                "execution_mode": "react_loop",
                "workspace_root": str(workspace_dir),
            })
            resolved = record["task_id"]
        self._gateway_task_ids[tid] = resolved
        return resolved

    def _invoke_via_gateway(
        self, call: ToolCallRequest, workspace_dir: Path, start_t: float
    ) -> ToolCallResult:
        """Execute a tool call exclusively through ToolGateway.invoke."""
        from harness_core.gateway import ApprovalRequiredInterrupt, PolicyBlockedError

        task_id = self._resolve_gateway_task_id(workspace_dir)
        try:
            result = self.gateway.invoke(
                task_id=task_id,
                attempt_id="att_react",
                step_id=f"turn_{self._current_turn_index}",
                sequence_in_step=self._current_tool_seq,
                tool_id=call.name,
                args=call.arguments,
                workspace_dir=workspace_dir,
            )
        except PolicyBlockedError as exc:
            return ToolCallResult(
                call_id=call.call_id,
                name=call.name,
                output=f"Denied by gateway policy: {exc}",
                is_error=True,
                duration_ms=(time.perf_counter() - start_t) * 1000,
            )
        except ApprovalRequiredInterrupt as exc:
            approval_id = exc.approval_record.get("approval_id", "unknown")
            return ToolCallResult(
                call_id=call.call_id,
                name=call.name,
                output=(
                    f"Tool '{call.name}' requires owner approval before execution "
                    f"(approval_id={approval_id}); no side effects were performed."
                ),
                is_error=True,
                duration_ms=(time.perf_counter() - start_t) * 1000,
            )
        except Exception as exc:
            return ToolCallResult(
                call_id=call.call_id,
                name=call.name,
                output=f"Error: {exc}",
                is_error=True,
                duration_ms=(time.perf_counter() - start_t) * 1000,
            )

        is_error = False
        if isinstance(result, dict):
            if result.get("ok") is False:
                is_error = True
            elif "error" in result and bool(result["error"]):
                is_error = True
            elif result.get("status") in ("error", "DENIED"):
                is_error = True
            elif result.get("status") == "failed" and call.name not in (
                "read_terminal", "start_process", "kill_process", "list_processes"
            ):
                is_error = True
        return ToolCallResult(
            call_id=call.call_id,
            name=call.name,
            output=result,
            is_error=is_error,
            duration_ms=(time.perf_counter() - start_t) * 1000,
        )

    # -------------------------------------------------------------------------
    # Ledger Step Recording
    # -------------------------------------------------------------------------

    def _record_tool_call_in_ledger(
        self,
        task_id: str,
        turn_index: int,
        sequence_in_turn: int,
        call: ToolCallRequest,
    ) -> None:
        """Record step / tool call initiation in the ledger store if present."""
        if not self.ledger:
            return

        if hasattr(self.ledger, "record_step") and callable(self.ledger.record_step):
            try:
                self.ledger.record_step(
                    task_id=task_id,
                    step_id=f"turn_{turn_index}",
                    tool_name=call.name,
                    arguments=call.arguments,
                )
            except Exception:
                pass

        if hasattr(self.ledger, "record_tool_call") and callable(self.ledger.record_tool_call):
            try:
                self.ledger.record_tool_call(
                    call_id=call.call_id,
                    task_id=task_id,
                    tool_name=call.name,
                    args=call.arguments,
                    step_id=f"turn_{turn_index}",
                )
            except Exception:
                pass
        elif hasattr(self.ledger, "begin_tool_call") and callable(self.ledger.begin_tool_call):
            policy_decision = "ALLOW"
            lease_mgr = getattr(self, "approval_lease_manager", None) or (
                getattr(self.gateway, "approval_lease_manager", None) if self.gateway else None
            )
            if lease_mgr is not None and hasattr(lease_mgr, "check_lease"):
                try:
                    has_lease, lease_id = lease_mgr.check_lease(call.name, call.arguments)
                    if has_lease:
                        policy_decision = "LEASE_APPROVED"
                except Exception:
                    pass

            try:
                self.ledger.begin_tool_call(
                    task_id=task_id,
                    attempt_id="att_1",
                    step_id=f"turn_{turn_index}",
                    sequence_in_step=sequence_in_turn,
                    tool_id=call.name,
                    risk_class="R1",
                    canonical_args_hash="",
                    arguments_redacted=call.arguments,
                    policy_decision=policy_decision,
                    idempotency_key=f"idem_{call.call_id}",
                )
            except Exception:
                pass

    def _seal_tool_call_in_ledger(
        self,
        task_id: str,
        turn_index: int,
        sequence_in_turn: int,
        call: ToolCallRequest,
        result: ToolCallResult,
    ) -> None:
        """Seal step / tool call completion in the ledger store if present."""
        if not self.ledger:
            return

        if hasattr(self.ledger, "seal_step") and callable(self.ledger.seal_step):
            try:
                self.ledger.seal_step(
                    task_id=task_id,
                    step_id=f"turn_{turn_index}",
                    tool_name=call.name,
                    output=result.output,
                    is_error=result.is_error,
                )
            except Exception:
                pass

        if hasattr(self.ledger, "seal_tool_call") and callable(self.ledger.seal_tool_call):
            try:
                payload = result.output if isinstance(result.output, dict) else {"output": result.output}
                status = "FAILED" if result.is_error else "SEALED"
                self.ledger.seal_tool_call(
                    call.call_id,
                    payload,
                    status=status,
                )
            except Exception:
                pass

    def _record_moa_in_ledger(self, task_id: str, turn_index: int, moa_result: Any) -> None:
        """Record MoA synthesis step and token metrics in the ledger store."""
        if not self.ledger or not task_id:
            return

        step_id = f"moa_turn_{turn_index}"
        if hasattr(self.ledger, "record_step") and callable(self.ledger.record_step):
            try:
                proposers_list = [
                    getattr(p, "model", str(p))
                    for p in getattr(moa_result, "proposer_outputs", [])
                ]
                self.ledger.record_step(
                    task_id=task_id,
                    step_id=step_id,
                    tool_name="mixture_of_agents",
                    arguments={
                        "aggregator": getattr(moa_result, "aggregator_model", ""),
                        "proposers": proposers_list,
                        "fallback_used": getattr(moa_result, "fallback_used", False),
                    },
                )
            except Exception:
                pass

        if hasattr(self.ledger, "seal_step") and callable(self.ledger.seal_step):
            try:
                self.ledger.seal_step(
                    task_id=task_id,
                    step_id=step_id,
                    tool_name="mixture_of_agents",
                    output={
                        "total_tokens": getattr(moa_result, "total_tokens", 0),
                        "latency_ms": getattr(moa_result, "total_latency_ms", 0.0),
                        "proposer_count": len(getattr(moa_result, "proposer_outputs", [])),
                    },
                    is_error=False,
                )
            except Exception:
                pass

        if hasattr(self.ledger, "record_event") and callable(self.ledger.record_event):
            try:
                self.ledger.record_event(
                    task_id=task_id,
                    event_type="MOA_SYNTHESIS_COMPLETED",
                    reason_code="MOA_SYNTHESIS_SUCCESS",
                    payload={
                        "turn_index": turn_index,
                        "total_tokens": getattr(moa_result, "total_tokens", 0),
                        "total_latency_ms": getattr(moa_result, "total_latency_ms", 0.0),
                        "aggregator": getattr(moa_result, "aggregator_model", ""),
                        "proposers_count": len(getattr(moa_result, "proposer_outputs", [])),
                    },
                )
            except Exception:
                pass
        elif hasattr(self.ledger, "record_task_event") and callable(self.ledger.record_task_event):
            try:
                self.ledger.record_task_event(
                    task_id=task_id,
                    event_type="MOA_SYNTHESIS_COMPLETED",
                    reason_code="MOA_SYNTHESIS_SUCCESS",
                    payload={
                        "turn_index": turn_index,
                        "total_tokens": getattr(moa_result, "total_tokens", 0),
                        "total_latency_ms": getattr(moa_result, "total_latency_ms", 0.0),
                        "aggregator": getattr(moa_result, "aggregator_model", ""),
                        "proposers_count": len(getattr(moa_result, "proposer_outputs", [])),
                    },
                )
            except Exception:
                pass

    def _trigger_title_synthesis_if_first_turn(self, turn_index: int, task_id: str, prompt: str) -> None:
        """Schedule background LLM title synthesis after turn 1 completion."""
        if turn_index == 1 and self.title_synthesizer and self.ledger and task_id:
            try:
                self.title_synthesizer.synthesize_async(task_id=task_id, prompt=prompt)
            except Exception:
                pass

    def _emit_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Emit a lifecycle event through on_event callback if configured."""
        if event_type == "turn_complete" and data.get("turn_index") == 1:
            task_id = getattr(self, "active_task_id", None) or getattr(self, "session_id", None) or ""
            prompt = getattr(self, "_active_prompt", "")
            if task_id and prompt:
                self._trigger_title_synthesis_if_first_turn(1, task_id, prompt)

        cb = getattr(self.config, "on_event", None)
        if cb and callable(cb):
            try:
                cb(event_type, data)
            except Exception:
                pass

    def _handle_tool_failure_guardrail(
        self, call: ToolCallRequest, tool_res: ToolCallResult
    ) -> None:
        """Capture deterministic repeat failures and record actionable [AVOID] invariants."""
        sig = ""
        if self.guardrails_manager:
            sig = self.guardrails_manager.extract_error_signature(tool_res.output)
        else:
            raw = str(tool_res.output or "")
            clean = re.sub(r"^(?:Error:\s*)+", "", raw.strip(), flags=re.IGNORECASE)
            sig = clean.split("\n")[0].strip()[:120]

        if not sig:
            return

        try:
            from harness_core.cognition.memory import is_transient_error
            if is_transient_error(sig):
                return
        except Exception:
            pass

        sig_lower = sig.lower()
        if (
            "syntaxerror" in sig_lower
            or "compiler" in sig_lower
            or "diagnostic" in sig_lower
            or "pyright" in sig_lower
            or "rust-analyzer" in sig_lower
            or "compilation error" in sig_lower
            or "preflight" in sig_lower
            or tool_res.name in ("lsp_diagnostics", "check_syntax")
        ):
            self.compiler_errors_count += 1
            if self.compiler_errors_count >= 2:
                self._check_and_trigger_friction(
                    f"repeated_compiler_errors (>= 2): {sig}",
                    {"tool": tool_res.name, "error": sig, "count": self.compiler_errors_count},
                )

        key = f"{tool_res.name}:{sig}"
        count = self._consecutive_tool_failures.get(key, 0) + 1
        self._consecutive_tool_failures[key] = count
        self._failed_tool_sig_counts[key] = self._failed_tool_sig_counts.get(key, 0) + 1

        if count >= 2:
            self._check_and_trigger_friction(
                f"repeat_tool_failure: {tool_res.name} failed >= 2 times with: {sig}",
                {"tool": tool_res.name, "error": sig, "count": count},
            )
            if self.guardrails_manager:
                arg_summary = ""
                if isinstance(call.arguments, dict):
                    arg_val = (
                        call.arguments.get("command")
                        or call.arguments.get("path")
                        or call.arguments.get("file_path")
                        or call.arguments.get("target")
                        or ""
                    )
                    if arg_val:
                        arg_summary = str(arg_val)[:60]

                if arg_summary:
                    rule = f"- [AVOID] Do not run '{arg_summary}' with {tool_res.name} — repeatedly fails with: {sig}"
                else:
                    rule = f"- [AVOID] Do not repeat failed invocation of {tool_res.name} — error: {sig}"

                self.guardrails_manager.record_avoid_invariant(rule)

    def _reset_tool_failures_on_success(self, tool_name: str) -> None:
        """Reset consecutive error count when tool executes successfully."""
        prefix = f"{tool_name}:"
        for k, count in list(self._consecutive_tool_failures.items()):
            if k.startswith(prefix) and count >= 2:
                self._check_and_trigger_friction(
                    f"tool_repeat_failure_resolved: {tool_name} failed >= 2 times before succeeding",
                    {"tool": tool_name, "prior_failures": count},
                )
        to_del = [k for k in self._consecutive_tool_failures if k.startswith(prefix)]
        for k in to_del:
            self._consecutive_tool_failures.pop(k, None)

    def record_rejected_diff(self, file_path: str, reason: str = "") -> None:
        """Record a negative guardrail when a proposed diff or edit is rejected by operator."""
        if not self.guardrails_manager:
            return
        desc = f" — reason: {reason}" if reason else ""
        rule = f"- [AVOID] Rejected change on {file_path}{desc}. Propose an alternative approach."
        self.guardrails_manager.record_avoid_invariant(rule)

    def record_rollback(self, snapshot_id: str, reason: str = "") -> None:
        """Record a negative guardrail when an execution state is rolled back."""
        if not self.guardrails_manager:
            return
        desc = f" — {reason}" if reason else ""
        rule = f"- [AVOID] Rolled back workspace from {snapshot_id}{desc}. Do not re-attempt identical modification."
        self.guardrails_manager.record_avoid_invariant(rule)

    def rewind_to(self, turn_spec: int | str, task_id: str | None = None) -> list[dict[str, Any]]:
        """Rewind dialogue history, internal turn state, and ledger tracking to specified turn.
        
        Args:
            turn_spec: Relative offset (-N) or absolute turn index/ID.
            task_id: Optional task identifier, defaults to active task/session.
            
        Returns:
            Truncated list of active dialogue messages.
        """
        from harness_core.storage.timeline_rewind import TimelineRewindManager

        tid = task_id or getattr(self, "active_task_id", None) or getattr(self, "session_id", None) or "task_react"
        mgr = TimelineRewindManager(ledger=self.ledger)
        result = mgr.rewind(task_id=tid, target=turn_spec, messages=self.messages)
        self.messages = result.live_messages

        if hasattr(self, "turns"):
            self.turns = [t for t in self.turns if getattr(t, "turn_index", 0) <= result.target_turn]

        return self.messages

    def _apply_preflight_session_hygiene(self, budget_tokens: int) -> None:
        """Run initial sanitization and pre-compaction pass on oversized inputs before Turn 1."""
        if not self.messages:
            return

        initial_tokens = estimate_messages_tokens(self.messages)

        # 1. Sanitize tool pairs and broken boundaries
        self.messages = sanitize_tool_pairs(self.messages)

        # 2. Wire compactor if needed
        if self.context_compactor is None:
            self.context_compactor = ContextCompactor(
                CompactionConfig(
                    max_context_tokens=budget_tokens,
                    trigger_threshold=0.8,
                ),
                model_caller=self.model_caller,
            )

        # 3. If multi-message history, attempt standard compaction pass first
        if len(self.messages) > 2:
            comp_res = self.context_compactor.compact_messages(self.messages, budget_tokens=budget_tokens, force=True)
            if comp_res.compacted_tokens < comp_res.original_tokens:
                self.messages = comp_res.compacted_messages
                self.last_compaction_result = comp_res

        # 4. If still exceeding 85% of budget, pre-compact oversized messages
        current_tokens = estimate_messages_tokens(self.messages)
        threshold_tokens = int(budget_tokens * 0.85)

        if current_tokens >= threshold_tokens:
            safe_target_tokens = max(100, int(budget_tokens * 0.65))
            per_msg_budget = max(50, safe_target_tokens // max(1, len(self.messages)))
            for i, msg in enumerate(self.messages):
                msg_tok = estimate_message_tokens(msg)
                if msg_tok > per_msg_budget:
                    content = msg.get("content", "")
                    if isinstance(content, str) and len(content) > per_msg_budget * CHARS_PER_TOKEN:
                        allowed_chars = max(200, per_msg_budget * CHARS_PER_TOKEN)
                        half = allowed_chars // 2
                        new_content = elide_middle(content, head_chars=half, tail_chars=half)
                        self.messages[i] = dict(msg, content=new_content)
                    elif isinstance(content, list):
                        new_content_blocks = []
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                txt = str(block.get("text", ""))
                                if len(txt) > per_msg_budget * CHARS_PER_TOKEN:
                                    allowed_chars = max(200, per_msg_budget * CHARS_PER_TOKEN)
                                    half = allowed_chars // 2
                                    block = dict(block, text=elide_middle(txt, head_chars=half, tail_chars=half))
                            new_content_blocks.append(block)
                        self.messages[i] = dict(msg, content=new_content_blocks)

            compacted_tokens = estimate_messages_tokens(self.messages)
            self.last_compaction_result = CompactionResult(
                compacted_messages=list(self.messages),
                original_tokens=initial_tokens,
                compacted_tokens=compacted_tokens,
                compression_ratio=compacted_tokens / max(1, initial_tokens),
                phase1_demoted_tools=0,
                phase3_summarized_turns=0,
                used_fallback=True,
            )

    # -------------------------------------------------------------------------
    # Execution Loop
    # -------------------------------------------------------------------------

    def run(
        self,
        prompt: str,
        task_id: str = "task_react",
        initial_messages: list[dict[str, Any]] | None = None,
        session_id: str | None = None,
    ) -> ReActSessionResult:
        """Execute the multi-turn ReAct loop synchronously."""
        if session_id:
            self.session_id = session_id
        self.active_task_id = task_id
        self._active_prompt = prompt
        self.retry_count = 0
        self.turn_repair_count = 0
        self.repair_attempts = 0
        self.compiler_errors_count = 0
        self.friction_detected = False
        self._friction_reasons = []
        self._failed_tool_sig_counts = {}
        self.messages = []
        if self.config.system_prompt:
            self.messages.append({"role": "system", "content": self.config.system_prompt})
        if initial_messages:
            self.messages.extend(initial_messages)
        self.messages.append({"role": "user", "content": prompt})

        # Pre-Flight Gateway Session Hygiene (Before Turn 1)
        budget = getattr(self.config, "budget_tokens", 100_000) or 100_000
        initial_tokens = estimate_messages_tokens(self.messages)
        if initial_tokens >= int(budget * 0.85):
            self._apply_preflight_session_hygiene(budget)

        # Stage 1: Instant Title derivation in ledger (Phase 37)
        if self.title_synthesizer and self.ledger and task_id:
            try:
                self.title_synthesizer.ensure_instant_title(task_id, prompt)
            except Exception:
                pass

        self.turns = []
        success = False
        final_answer = ""
        termination_reason = "max_turns"

        for turn_index in range(1, self.config.max_turns + 1):
            turn_start = time.perf_counter()
            self._emit_event("turn_start", {"turn_index": turn_index})

            # 0. Context compaction check
            compactor = self.context_compactor
            if compactor is None and getattr(self.config, "budget_tokens", 0) > 0:
                compactor = ContextCompactor(
                    CompactionConfig(
                        max_context_tokens=self.config.budget_tokens,
                        trigger_threshold=0.8,
                    ),
                    model_caller=self.model_caller,
                )
                self.context_compactor = compactor

            if compactor is not None:
                current_tokens = estimate_messages_tokens(self.messages)
                threshold = int(self.config.budget_tokens * 0.8)
                if current_tokens >= threshold or compactor.should_compact(self.messages):
                    comp_res = compactor.compact_messages(self.messages, force=True)
                    if comp_res.compacted_tokens < comp_res.original_tokens:
                        self.messages = comp_res.compacted_messages
                        self.last_compaction_result = comp_res

            # 1. Call model
            turn_moa_res = None
            try:
                if self.moa is not None and getattr(self.moa.config, "enabled", True):
                    self.moa.model_caller = self.model_caller
                    current_prompt = self._format_messages_to_prompt(self.messages)
                    turn_moa_res = self.moa.synthesize(current_prompt, messages=self.messages)
                    raw_text = turn_moa_res.synthesized_text
                    response = raw_text
                    self._record_moa_in_ledger(task_id, turn_index, turn_moa_res)
                else:
                    response = self._call_model(self.messages)
                    raw_text = self._extract_response_text(response)
            except Exception as e:
                turn_latency = (time.perf_counter() - turn_start) * 1000
                self.turns.append(
                    ReActTurn(
                        turn_index=turn_index,
                        thought=f"Model execution error: {e}",
                        latency_ms=turn_latency,
                    )
                )
                self._emit_event("turn_complete", {"turn_index": turn_index, "latency_ms": turn_latency})
                if self.retry_count >= 3 or self.turn_repair_count >= 3 or self.repair_attempts >= 3:
                    self._check_and_trigger_friction("friction_threshold_reached")
                f_info = {
                    "friction_detected": self.friction_detected,
                    "retry_count": self.retry_count,
                    "turn_repair_count": self.turn_repair_count,
                    "repair_attempts": self.repair_attempts,
                    "compiler_errors_count": self.compiler_errors_count,
                    "reasons": list(self._friction_reasons),
                    "consecutive_tool_failures": dict(self._consecutive_tool_failures),
                }
                return ReActSessionResult(
                    success=False,
                    final_answer=f"Error: {e}",
                    total_turns=len(self.turns),
                    turns=self.turns,
                    termination_reason="error",
                    compaction_result=self.last_compaction_result,
                    retry_count=self.retry_count,
                    friction_info=f_info,
                )

            from harness_core.cognition.repetition_sentry import (
                generate_steering_prompt,
                strip_thinking_tags,
            )

            raw_text = self._extract_response_text(response)
            cleaned_raw_text, extracted_reasoning = strip_thinking_tags(raw_text)
            thought, tool_calls = self.parse_tool_calls(response)
            if extracted_reasoning and not thought:
                thought = extracted_reasoning
            elif thought:
                thought, _ = strip_thinking_tags(thought)
            self._emit_event("thought", {"thought": thought})
            turn_latency = (time.perf_counter() - turn_start) * 1000

            # 2. Check for explicit <finish> tag
            finish_match = re.search(r"<finish>(.*?)</finish>", raw_text, re.DOTALL | re.IGNORECASE)
            if finish_match:
                final_answer, _ = strip_thinking_tags(finish_match.group(1).strip())
                success = True
                termination_reason = "finish"
                self.turns.append(
                    ReActTurn(
                        turn_index=turn_index,
                        thought=thought,
                        latency_ms=turn_latency,
                        moa_result=turn_moa_res,
                    )
                )
                self._emit_event("turn_complete", {"turn_index": turn_index, "latency_ms": turn_latency})
                break

            if "<finish>" in raw_text.lower():
                cleaned = re.sub(r"</?finish>", "", raw_text, flags=re.IGNORECASE).strip()
                final_answer, _ = strip_thinking_tags(cleaned)
                success = True
                termination_reason = "finish"
                self.turns.append(
                    ReActTurn(
                        turn_index=turn_index,
                        thought=thought,
                        latency_ms=turn_latency,
                        moa_result=turn_moa_res,
                    )
                )
                self._emit_event("turn_complete", {"turn_index": turn_index, "latency_ms": turn_latency})
                break

            # 3. Check for natural final answer (no tool calls emitted)
            if not tool_calls:
                final_answer = thought if thought else cleaned_raw_text.strip()
                final_answer, _ = strip_thinking_tags(final_answer)
                success = True
                termination_reason = "no_tool_calls"
                self.turns.append(
                    ReActTurn(
                        turn_index=turn_index,
                        thought=thought,
                        latency_ms=turn_latency,
                        moa_result=turn_moa_res,
                    )
                )
                self._emit_event("turn_complete", {"turn_index": turn_index, "latency_ms": turn_latency})
                break

            # 4. Tool calls present: record turn and execute each tool call
            self.messages.append({"role": "assistant", "content": cleaned_raw_text})
            tool_results: list[ToolCallResult] = []

            for seq, call in enumerate(tool_calls):
                # Emit tool_start event
                self._emit_event("tool_start", {
                    "call_id": call.call_id,
                    "name": call.name,
                    "arguments": call.arguments,
                })

                # Evaluate tool call against ActionLoopDetector
                is_thrashing = False
                loop_type = ""
                loop_details: dict[str, Any] = {}
                if getattr(self, "action_loop_detector", None) is not None:
                    is_thrashing, loop_type, loop_details = self.action_loop_detector.check_thrashing(
                        call.name, call.arguments
                    )

                if is_thrashing:
                    steering_msg = generate_steering_prompt(loop_type, loop_details)
                    tool_res = ToolCallResult(
                        call_id=call.call_id,
                        name=call.name,
                        output=steering_msg,
                        is_error=True,
                        duration_ms=0.0,
                    )
                    self.action_loop_detector.record_action(
                        call.name,
                        call.arguments,
                        is_error=True,
                        output=steering_msg,
                    )
                    self.retry_count += 1
                else:
                    # Ledger step begin
                    self._record_tool_call_in_ledger(task_id, turn_index, seq, call)

                    # Execute tool
                    self._current_turn_index = turn_index
                    self._current_tool_seq = seq
                    tool_res = self._execute_tool_call(call)

                    # Ledger step seal
                    self._seal_tool_call_in_ledger(task_id, turn_index, seq, call, tool_res)

                    if getattr(self, "action_loop_detector", None) is not None:
                        self.action_loop_detector.record_action(
                            call.name,
                            call.arguments,
                            is_error=tool_res.is_error,
                            output=tool_res.output,
                        )

                tool_results.append(tool_res)

                # Emit tool_end event
                self._emit_event("tool_end", {
                    "call_id": tool_res.call_id,
                    "name": tool_res.name,
                    "is_error": tool_res.is_error,
                    "duration_ms": tool_res.duration_ms,
                })

                if tool_res.is_error:
                    self._handle_tool_failure_guardrail(call, tool_res)
                else:
                    self._reset_tool_failures_on_success(tool_res.name)

                # Format observation back into message history
                if tool_res.name == "code_kernel" and isinstance(tool_res.output, dict):
                    out_str = str(tool_res.output.get("output", ""))
                    warn_str = str(tool_res.output.get("warning", ""))
                    parts = []
                    if out_str:
                        parts.append(out_str)
                    elif not tool_res.is_error:
                        parts.append("(execution completed with no output)")
                    if warn_str:
                        parts.append(f"[Warning: {warn_str}]")
                    obs_payload = "\n\n".join(parts) if parts else ""
                else:
                    obs_payload = (
                        tool_res.output
                        if isinstance(tool_res.output, str)
                        else json.dumps(tool_res.output, default=str, ensure_ascii=False)
                    )
                if tool_res.is_error:
                    avoid_hint = ""
                    if self.guardrails_manager is not None:
                        try:
                            rules = self.guardrails_manager.list_guardrails()
                            matched_rules = [r for r in rules if call.name in r][:3]
                            if matched_rules:
                                avoid_hint = "\n[Active Guardrails - DO NOT Repeat]\n" + "\n".join(matched_rules)
                        except Exception:
                            pass
                    obs_content = f"<observation tool=\"{tool_res.name}\">\nError: {obs_payload}{avoid_hint}\n</observation>"
                else:
                    obs_content = f"<observation tool=\"{tool_res.name}\">\n{obs_payload}\n</observation>"

                self.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_res.call_id,
                        "name": tool_res.name,
                        "tool_name": tool_res.name,
                        "call_id": tool_res.call_id,
                        "content": obs_payload,
                        "observation": obs_content,
                    }
                )

            turn = ReActTurn(
                turn_index=turn_index,
                thought=thought,
                tool_calls=tool_calls,
                tool_results=tool_results,
                latency_ms=(time.perf_counter() - turn_start) * 1000,
                moa_result=turn_moa_res,
            )
            self.turns.append(turn)
            self._emit_event("turn_complete", {"turn_index": turn_index, "latency_ms": turn.latency_ms})

            if any(r.is_error for r in tool_results):
                self.turn_repair_count += 1
                if self.turn_repair_count >= 3:
                    self._check_and_trigger_friction(
                        f"turn_repair_count_threshold_reached (turn_repair_count={self.turn_repair_count})",
                        {"turn_repair_count": self.turn_repair_count},
                    )

        # Max turns exhausted
        if termination_reason == "max_turns" and not success:
            final_answer = self.turns[-1].thought if self.turns else ""

        if self.retry_count >= 3 or self.turn_repair_count >= 3 or self.repair_attempts >= 3:
            self._check_and_trigger_friction("friction_threshold_reached")

        friction_info = {
            "friction_detected": self.friction_detected,
            "retry_count": self.retry_count,
            "turn_repair_count": self.turn_repair_count,
            "repair_attempts": self.repair_attempts,
            "compiler_errors_count": self.compiler_errors_count,
            "reasons": list(self._friction_reasons),
            "consecutive_tool_failures": dict(self._consecutive_tool_failures),
        }

        return ReActSessionResult(
            success=success,
            final_answer=final_answer,
            total_turns=len(self.turns),
            turns=self.turns,
            termination_reason=termination_reason,
            compaction_result=self.last_compaction_result,
            retry_count=self.retry_count,
            friction_info=friction_info,
        )

    async def run_async(
        self,
        prompt: str,
        task_id: str = "task_react",
        initial_messages: list[dict[str, Any]] | None = None,
        session_id: str | None = None,
    ) -> ReActSessionResult:
        """Execute the multi-turn ReAct loop asynchronously."""
        # Run sync run in default loop executor to support asyncio call sites
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.run, prompt, task_id, initial_messages, session_id)
