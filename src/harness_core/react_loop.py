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
import re
import time
from typing import Any, Callable
import uuid


@dataclass
class ReActConfig:
    """Configuration parameters for the ReAct execution loop."""

    max_turns: int = 30
    temperature: float = 0.2
    system_prompt: str = ""
    budget_tokens: int = 100_000
    stop_sequences: list[str] = field(default_factory=list)


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


@dataclass
class ReActSessionResult:
    """Aggregate result of an entire ReAct loop session."""

    success: bool
    final_answer: str
    total_turns: int
    turns: list[ReActTurn] = field(default_factory=list)
    termination_reason: str = "finish"  # "finish", "max_turns", "error", "no_tool_calls"


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
    ) -> None:
        self.model_caller = model_caller
        self.gateway = gateway
        self.workspace_root = Path(workspace_root).resolve() if workspace_root else None
        self.config = config or ReActConfig()
        self.ledger = ledger
        self.messages: list[dict[str, Any]] = []
        self.turns: list[ReActTurn] = []

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
            return json.dumps(response)
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
                            args = json.loads(args)
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
                        args = json.loads(args)
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
                    parsed = json.loads(body)
                    if isinstance(parsed, dict):
                        name = parsed.get("name") or parsed.get("tool") or tag_name
                        args = parsed.get("arguments") or parsed.get("args") or {}
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
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
                                        args = json.loads(args)
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
                                args = json.loads(args_m.group(1).strip())
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
                parsed = json.loads(block.strip())
                block_requests: list[ToolCallRequest] = []
                if isinstance(parsed, dict):
                    if "tool_calls" in parsed and isinstance(parsed["tool_calls"], list):
                        for j, tc in enumerate(parsed["tool_calls"]):
                            name = tc.get("name") or tc.get("tool")
                            args = tc.get("arguments") or tc.get("args") or {}
                            if isinstance(args, str):
                                try:
                                    args = json.loads(args)
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
                                args = json.loads(args)
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
                                    args = json.loads(args)
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
                parsed = json.loads(stripped)
                if isinstance(parsed, dict):
                    thought = str(parsed.get("thought") or parsed.get("reasoning") or "")
                    if "tool_calls" in parsed and isinstance(parsed["tool_calls"], list):
                        calls = []
                        for j, tc in enumerate(parsed["tool_calls"]):
                            name = tc.get("name") or tc.get("tool")
                            args = tc.get("arguments") or tc.get("args") or {}
                            if isinstance(args, str):
                                try:
                                    args = json.loads(args)
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
                                args = json.loads(args)
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
                                    args = json.loads(args)
                                except Exception:
                                    args = {"raw": args}
                            cid = item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:8]}_{j}"
                            if name:
                                calls.append(ToolCallRequest(call_id=str(cid), name=str(name), arguments=args or {}))
                    if calls:
                        return "", calls
            except Exception:
                pass

        # Case 5: Embedded JSON search in text
        json_matches = re.finditer(r"(\{[\s\S]*?\})", text)
        for match in json_matches:
            candidate = match.group(1).strip()
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict) and ("tool" in parsed or ("name" in parsed and ("args" in parsed or "arguments" in parsed))):
                    name = parsed.get("name") or parsed.get("tool")
                    args = parsed.get("arguments") or parsed.get("args") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            args = {"raw": args}
                    cid = parsed.get("call_id") or parsed.get("id") or f"call_{uuid.uuid4().hex[:8]}"
                    if name:
                        thought = (text[:match.start()] + " " + text[match.end():]).strip()
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

    def _call_model(self, messages: list[dict[str, Any]]) -> Any:
        """Invoke configured model_caller with appropriate parameter signature."""
        if self.model_caller is None:
            raise RuntimeError("No model_caller configured for ReActLoop")

        # Try passing messages list directly
        try:
            return self.model_caller(messages)
        except TypeError:
            pass

        # Try keyword argument messages=messages
        try:
            return self.model_caller(messages=messages)
        except TypeError:
            pass

        # Fallback to string prompt formatted from messages
        prompt_str = self._format_messages_to_prompt(messages)
        return self.model_caller(prompt_str)

    @staticmethod
    def _format_messages_to_prompt(messages: list[dict[str, Any]]) -> str:
        """Convert a list of structured chat messages to a plaintext prompt string."""
        lines: list[str] = []
        for msg in messages:
            role = str(msg.get("role", "user")).upper()
            content = str(msg.get("content", ""))
            lines.append(f"[{role}]\n{content}")
        return "\n\n".join(lines)

    def _execute_tool_call(self, call: ToolCallRequest) -> ToolCallResult:
        """Execute a single tool call through the gateway and record execution metrics."""
        start_t = time.perf_counter()
        if not self.gateway:
            return ToolCallResult(
                call_id=call.call_id,
                name=call.name,
                output="Error: No ToolGateway configured",
                is_error=True,
                duration_ms=0.0,
            )

        workspace_dir = Path(self.workspace_root) if self.workspace_root else Path.cwd()

        # Gateway with .tools dictionary (standard HEX ToolGateway)
        if hasattr(self.gateway, "tools") and isinstance(self.gateway.tools, dict) and call.name in self.gateway.tools:
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
                    if result.get("status") in ("error", "failed", "DENIED") or (
                        "error" in result and bool(result["error"])
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

        # Fallback for gateways exposing .execute(name, arguments)
        if hasattr(self.gateway, "execute") and callable(self.gateway.execute):
            try:
                result = self.gateway.execute(call.name, call.arguments)
                duration_ms = (time.perf_counter() - start_t) * 1000
                is_error = False
                if isinstance(result, dict) and (result.get("status") in ("error", "failed") or "error" in result):
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
                    policy_decision="ALLOW",
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

    # -------------------------------------------------------------------------
    # Execution Loop
    # -------------------------------------------------------------------------

    def run(
        self,
        prompt: str,
        task_id: str = "task_react",
        initial_messages: list[dict[str, Any]] | None = None,
    ) -> ReActSessionResult:
        """Execute the multi-turn ReAct loop synchronously."""
        self.messages = []
        if self.config.system_prompt:
            self.messages.append({"role": "system", "content": self.config.system_prompt})
        if initial_messages:
            self.messages.extend(initial_messages)
        self.messages.append({"role": "user", "content": prompt})

        self.turns = []
        success = False
        final_answer = ""
        termination_reason = "max_turns"

        for turn_index in range(1, self.config.max_turns + 1):
            turn_start = time.perf_counter()

            # 1. Call model
            try:
                response = self._call_model(self.messages)
            except Exception as e:
                turn_latency = (time.perf_counter() - turn_start) * 1000
                self.turns.append(
                    ReActTurn(
                        turn_index=turn_index,
                        thought=f"Model execution error: {e}",
                        latency_ms=turn_latency,
                    )
                )
                return ReActSessionResult(
                    success=False,
                    final_answer=f"Error: {e}",
                    total_turns=len(self.turns),
                    turns=self.turns,
                    termination_reason="error",
                )

            raw_text = self._extract_response_text(response)
            thought, tool_calls = self.parse_tool_calls(response)
            turn_latency = (time.perf_counter() - turn_start) * 1000

            # 2. Check for explicit <finish> tag
            finish_match = re.search(r"<finish>(.*?)</finish>", raw_text, re.DOTALL | re.IGNORECASE)
            if finish_match:
                final_answer = finish_match.group(1).strip()
                success = True
                termination_reason = "finish"
                self.turns.append(
                    ReActTurn(
                        turn_index=turn_index,
                        thought=thought,
                        latency_ms=turn_latency,
                    )
                )
                break

            if "<finish>" in raw_text.lower():
                cleaned = re.sub(r"</?finish>", "", raw_text, flags=re.IGNORECASE).strip()
                final_answer = cleaned
                success = True
                termination_reason = "finish"
                self.turns.append(
                    ReActTurn(
                        turn_index=turn_index,
                        thought=thought,
                        latency_ms=turn_latency,
                    )
                )
                break

            # 3. Check for natural final answer (no tool calls emitted)
            if not tool_calls:
                final_answer = thought if thought else raw_text.strip()
                success = True
                termination_reason = "no_tool_calls"
                self.turns.append(
                    ReActTurn(
                        turn_index=turn_index,
                        thought=thought,
                        latency_ms=turn_latency,
                    )
                )
                break

            # 4. Tool calls present: record turn and execute each tool call
            self.messages.append({"role": "assistant", "content": raw_text})
            tool_results: list[ToolCallResult] = []

            for seq, call in enumerate(tool_calls):
                # Ledger step begin
                self._record_tool_call_in_ledger(task_id, turn_index, seq, call)

                # Execute tool
                tool_res = self._execute_tool_call(call)
                tool_results.append(tool_res)

                # Ledger step seal
                self._seal_tool_call_in_ledger(task_id, turn_index, seq, call, tool_res)

                # Format observation back into message history
                obs_payload = (
                    tool_res.output
                    if isinstance(tool_res.output, str)
                    else json.dumps(tool_res.output, ensure_ascii=False)
                )
                if tool_res.is_error:
                    obs_content = f"<observation tool=\"{tool_res.name}\">\nError: {obs_payload}\n</observation>"
                else:
                    obs_content = f"<observation tool=\"{tool_res.name}\">\n{obs_payload}\n</observation>"

                self.messages.append(
                    {
                        "role": "user",
                        "content": obs_content,
                        "tool_name": tool_res.name,
                        "call_id": tool_res.call_id,
                    }
                )

            turn = ReActTurn(
                turn_index=turn_index,
                thought=thought,
                tool_calls=tool_calls,
                tool_results=tool_results,
                latency_ms=(time.perf_counter() - turn_start) * 1000,
            )
            self.turns.append(turn)

        # Max turns exhausted
        if termination_reason == "max_turns" and not success:
            final_answer = self.turns[-1].thought if self.turns else ""

        return ReActSessionResult(
            success=success,
            final_answer=final_answer,
            total_turns=len(self.turns),
            turns=self.turns,
            termination_reason=termination_reason,
        )

    async def run_async(
        self,
        prompt: str,
        task_id: str = "task_react",
        initial_messages: list[dict[str, Any]] | None = None,
    ) -> ReActSessionResult:
        """Execute the multi-turn ReAct loop asynchronously."""
        # Run sync run in default loop executor to support asyncio call sites
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.run, prompt, task_id, initial_messages)
