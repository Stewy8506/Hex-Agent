"""4-Phase Context Compactor & Prompt Cache Locking Core.

Provides deterministic stale tool output demotion, atomic tool-pair boundary
protection, middle-turn summarization, and immutable prompt-cache prefixing
matching production agent standards.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any, Callable

# Non-imitable elision markers preventing model reproduction
HEX_COMPRESSION_MARKER_PREFIX = "⟪HEX-CONTEXT-COMPRESSION:"
HEX_COMPRESSION_MARKER_TEMPLATE = (
    HEX_COMPRESSION_MARKER_PREFIX
    + " {omitted:,} of {total:,} chars omitted here by HEX context compactor. "
    "This is NOT part of the original tool call and must never be reproduced in new "
    "output — always write full, untruncated content.⟫"
)

CHARS_PER_TOKEN = 4
DEFAULT_KEEP_TOOL_ROUNDS = 4
DEFAULT_MIN_DEMOTE_CHARS = 1000
DEFAULT_HEAD_CHARS = 200
DEFAULT_TAIL_CHARS = 200

# Pattern to recognize already demoted recovery stubs
_DEMOTED_STUB_RE = re.compile(
    r"\[(?:[^\s\]]+|tool)\s+output demoted at compaction\s*—"
)

# Pattern to extract XML observations: <observation tool="...">...</observation>
_XML_OBSERVATION_RE = re.compile(
    r"<observation(?:\s+tool=[\"'](?P<tool_name>[^\"']+)[\"'])?>(?P<body>.*?)</observation>",
    re.DOTALL | re.IGNORECASE,
)

# Pattern to detect tool failure / error outputs
_ERROR_INDICATORS = (
    "Error:",
    "Traceback (most recent call last):",
    "Exception:",
    "FAILED",
    "Command failed",
)


def estimate_tokens_rough(text: str) -> int:
    """Rough token estimate: approx. 4 chars per token with ceiling for short texts.

    Returns 0 for empty or null text, and at least 1 token for non-empty text.
    """
    if not text:
        return 0
    text_str = str(text)
    if not text_str:
        return 0
    return max(1, (len(text_str) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN)


def estimate_message_tokens(msg: dict[str, Any]) -> int:
    """Estimate token consumption of a single message dictionary.

    Accounts for role framing overhead (~4 tokens), content payload,
    and serialized tool call representations.
    """
    if not isinstance(msg, dict):
        return estimate_tokens_rough(str(msg))

    tokens = 4  # Per-message envelope framing overhead

    role = msg.get("role")
    if role:
        tokens += estimate_tokens_rough(str(role))

    name = msg.get("name")
    if name:
        tokens += estimate_tokens_rough(str(name))

    content = msg.get("content")
    if isinstance(content, str):
        tokens += estimate_tokens_rough(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                b_type = block.get("type", "")
                if b_type == "text" or "text" in block:
                    tokens += estimate_tokens_rough(str(block.get("text", "")))
                elif b_type in ("image", "image_url") or "image_url" in block:
                    tokens += 85  # Standard low-res/metadata image cost
                else:
                    tokens += estimate_tokens_rough(str(block))
            else:
                tokens += estimate_tokens_rough(str(block))
    elif content is not None:
        tokens += estimate_tokens_rough(str(content))

    # Tool calls overhead
    tool_calls = msg.get("tool_calls")
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            if isinstance(tc, dict):
                fn = tc.get("function") or {}
                fn_name = tc.get("name") or fn.get("name") or ""
                fn_args = tc.get("arguments") or fn.get("arguments") or ""
                tc_repr = f"{tc.get('id', '')}:{fn_name}:{fn_args}"
                tokens += estimate_tokens_rough(tc_repr) + 4
            else:
                tokens += estimate_tokens_rough(str(tc))

    return tokens


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """Estimate total tokens across a list of message dictionaries."""
    if not messages:
        return 0
    return sum(estimate_message_tokens(m) for m in messages) + 3  # Assistant prompt prime


def _elision_marker(omitted: int, total: int) -> str:
    """Render the non-imitable elision marker with per-instance counts."""
    return f"{HEX_COMPRESSION_MARKER_PREFIX} {omitted:,} of {total:,} chars omitted.⟫"


def elide(text: str, limit: int) -> str:
    """Cap ``text`` at ``limit`` characters, inserting an elision marker."""
    if len(text) <= limit:
        return text
    total = len(text)
    sample_marker = _elision_marker(omitted=total, total=total)
    head_len = limit - len(sample_marker) - 1
    if head_len <= 0:
        return sample_marker
    kept = text[:head_len].rstrip()
    omitted = total - len(kept)
    return kept + " " + _elision_marker(omitted=omitted, total=total)


def elide_middle(
    text: str,
    head_chars: int = DEFAULT_HEAD_CHARS,
    tail_chars: int = DEFAULT_TAIL_CHARS,
) -> str:
    """Keep head and tail characters from text, eliding the middle with counts."""
    if len(text) <= head_chars + tail_chars:
        return text
    total = len(text)
    head = text[:head_chars].rstrip()
    tail = text[total - tail_chars:].lstrip()
    omitted_chars = total - len(head) - len(tail)
    middle_chunk = text[len(head): total - len(tail)]
    omitted_lines = middle_chunk.count("\n")
    marker = (
        f"{HEX_COMPRESSION_MARKER_PREFIX} {omitted_chars:,} chars "
        f"({omitted_lines} lines) omitted⟫"
    )
    return f"{head}\n{marker}\n{tail}"


def is_demoted_stub(content: str) -> bool:
    """Check if content is already a demoted recovery stub."""
    if not isinstance(content, str):
        return False
    return bool(_DEMOTED_STUB_RE.search(content))


def _is_tool_error(msg: dict[str, Any], content: str) -> bool:
    """Detect whether a tool execution resulted in an error."""
    if msg.get("is_error") is True:
        return True
    if msg.get("status") in ("error", "failed"):
        return True
    first_lines = "\n".join(content.splitlines()[:5])
    return any(indicator in first_lines for indicator in _ERROR_INDICATORS)


def _is_tool_result_message(msg: dict[str, Any]) -> bool:
    """Detect whether a message represents a tool result or observation."""
    if not isinstance(msg, dict):
        return False
    role = msg.get("role")
    if role == "tool":
        return True
    if role == "user":
        content = msg.get("content")
        if isinstance(content, str) and ("<observation" in content or "<tool_result" in content):
            return True
        if msg.get("name") == "observation":
            return True
    return False


def _extract_tool_name(msg: dict[str, Any], content: str) -> str:
    """Extract tool name from tool message or XML observation."""
    name = msg.get("name") or msg.get("tool_name")
    if name:
        return str(name)
    if isinstance(content, str):
        match = _XML_OBSERVATION_RE.search(content)
        if match and match.group("tool_name"):
            return match.group("tool_name")
    return "tool"


def demote_stale_tail_tools(
    messages: list[dict[str, Any]],
    tail_start: int = 0,
    session_id: str = "",
    keep_tool_rounds: int = DEFAULT_KEEP_TOOL_ROUNDS,
    min_demote_chars: int = DEFAULT_MIN_DEMOTE_CHARS,
    head_chars: int = DEFAULT_HEAD_CHARS,
    tail_chars: int = DEFAULT_TAIL_CHARS,
) -> tuple[list[dict[str, Any]], int]:
    """Demote bulky tool execution outputs older than the newest `keep_tool_rounds` rounds.

    Recent tool execution rounds (tail) are protected from demotion to prevent
    breaking active ReAct reasoning loops. Tool errors are preserved intact.

    Returns:
        tuple[list[dict[str, Any]], int]: (new_messages, demoted_count)
    """
    if not messages:
        return [], 0

    # 1. Identify tool execution rounds scanning backward from the end
    protected_indices: set[int] = set()
    rounds_seen = 0
    current_parent_key: Any = object()

    # Build quick lookup for assistant message preceding each index
    for i in range(len(messages) - 1, tail_start - 1, -1):
        msg = messages[i]
        if not _is_tool_result_message(msg):
            continue

        # Determine parent group key (parent assistant index or bare index)
        parent_key: Any = f"bare_{i}"
        for j in range(i - 1, -1, -1):
            cand = messages[j]
            cand_role = cand.get("role")
            if cand_role == "assistant":
                # Check if cand issued tool calls
                cand_calls = cand.get("tool_calls")
                if isinstance(cand_calls, list) and cand_calls:
                    parent_key = f"asst_{j}"
                    break
                cand_content = cand.get("content", "")
                if isinstance(cand_content, str) and "<tool_call" in cand_content:
                    parent_key = f"asst_{j}"
                    break
            elif cand_role != "tool":
                # Stopped at non-tool message that isn't assistant
                break

        if parent_key != current_parent_key:
            rounds_seen += 1
            current_parent_key = parent_key

        if rounds_seen > keep_tool_rounds:
            # Reached beyond protected recent tail
            pass
        else:
            protected_indices.add(i)

    # 2. Iterate through messages and demote unprotected bulky tool outputs
    result: list[dict[str, Any]] = list(messages)
    demoted_count = 0

    for idx in range(tail_start, len(messages)):
        msg = messages[idx]
        if idx in protected_indices or not _is_tool_result_message(msg):
            continue

        content = msg.get("content")
        if not isinstance(content, str):
            continue

        if len(content) < min_demote_chars or is_demoted_stub(content):
            continue

        if _is_tool_error(msg, content):
            continue

        tool_name = _extract_tool_name(msg, content)

        # Check if wrapped in XML observation
        xml_match = _XML_OBSERVATION_RE.search(content)
        if xml_match:
            body = xml_match.group("body").strip()
            if len(body) < min_demote_chars or _is_tool_error(msg, body):
                continue
            total_chars = len(body)
            head = body[:head_chars].rstrip()
            tail = body[total_chars - tail_chars:].lstrip()
            omitted_chars = total_chars - len(head) - len(tail)
            omitted_lines = body[len(head): total_chars - len(tail)].count("\n")
            marker = f"{HEX_COMPRESSION_MARKER_PREFIX} {omitted_chars} chars omitted⟫"
            stub = (
                f"[{tool_name} output demoted at compaction — {omitted_chars:,} chars "
                f"({omitted_lines} lines) omitted; head and tail preserved]\n"
                f"{head}\n{marker}\n{tail}"
            )
            new_content = f"<observation tool=\"{tool_name}\">\n{stub}\n</observation>"
        else:
            total_chars = len(content)
            head = content[:head_chars].rstrip()
            tail = content[total_chars - tail_chars:].lstrip()
            omitted_chars = total_chars - len(head) - len(tail)
            omitted_lines = content[len(head): total_chars - len(tail)].count("\n")
            marker = f"{HEX_COMPRESSION_MARKER_PREFIX} {omitted_chars} chars omitted⟫"
            stub = (
                f"[{tool_name} output demoted at compaction — {omitted_chars:,} chars "
                f"({omitted_lines} lines) omitted; head and tail preserved]\n"
                f"{head}\n{marker}\n{tail}"
            )
            new_content = stub

        new_msg = dict(msg)
        new_msg["content"] = new_content
        result[idx] = new_msg
        demoted_count += 1

    return result, demoted_count


# ==============================================================================
# Phase 2: Atomic Tool-Pair Boundary Protection & Alignment
# ==============================================================================

def find_tool_call_groups(messages: list[dict[str, Any]]) -> list[tuple[int, int]]:
    """Return list of (start_idx, end_idx) spans representing atomic call-and-response clusters.

    A group begins with an assistant message initiating tool calls (either via
    structured ``tool_calls`` or XML ``<tool_call>`` tags) and encompasses all
    corresponding tool result or observation messages that follow it.
    """
    groups: list[tuple[int, int]] = []
    n = len(messages)
    i = 0
    while i < n:
        msg = messages[i]
        role = msg.get("role")
        if role == "assistant":
            # Check structured tool calls
            tool_calls = msg.get("tool_calls")
            if isinstance(tool_calls, list) and tool_calls:
                call_ids = {
                    str(tc.get("id") or tc.get("call_id") or "")
                    for tc in tool_calls
                    if isinstance(tc, dict)
                }
                call_ids.discard("")
                j = i + 1
                last_tool_idx = i
                while j < n and messages[j].get("role") == "tool":
                    tc_id = str(messages[j].get("tool_call_id") or "")
                    if not call_ids or tc_id in call_ids:
                        last_tool_idx = j
                    j += 1
                if last_tool_idx > i:
                    groups.append((i, last_tool_idx))
                    i = last_tool_idx + 1
                    continue

            # Check XML tool calls
            content = msg.get("content")
            if isinstance(content, str) and ("<tool_call" in content or "</tool_call>" in content):
                j = i + 1
                last_tool_idx = i
                while j < n:
                    cand = messages[j]
                    cand_role = cand.get("role")
                    cand_content = cand.get("content", "")
                    if cand_role == "tool" or (
                        cand_role == "user"
                        and (
                            isinstance(cand_content, str)
                            and ("<observation" in cand_content or "<tool_result" in cand_content)
                            or cand.get("name") == "observation"
                        )
                    ):
                        last_tool_idx = j
                        j += 1
                    else:
                        break
                if last_tool_idx > i:
                    groups.append((i, last_tool_idx))
                    i = last_tool_idx + 1
                    continue
        i += 1
    return groups


def align_boundary_forward(messages: list[dict[str, Any]], cut_idx: int) -> int:
    """Push a compaction cut index forward so tool groups are never split.

    If ``cut_idx`` falls inside any tool call group, advances ``cut_idx`` past
    the group's end index (``end_idx + 1``) so all matching tool results remain
    in the prior segment. Also advances past any orphaned tool messages at the boundary.
    """
    if cut_idx <= 0:
        return 0
    if cut_idx >= len(messages):
        return len(messages)

    groups = find_tool_call_groups(messages)
    for start_idx, end_idx in groups:
        if start_idx < cut_idx <= end_idx:
            cut_idx = end_idx + 1
            break

    # Advance past any subsequent or orphaned tool messages
    while cut_idx < len(messages) and _is_tool_result_message(messages[cut_idx]):
        cut_idx += 1

    return min(cut_idx, len(messages))


def align_boundary_backward(messages: list[dict[str, Any]], cut_idx: int) -> int:
    """Pull a compaction cut index backward so tool groups are never split.

    If ``cut_idx`` falls inside any tool call group, retreats ``cut_idx`` to
    the group's start index (``start_idx``) so the initiating assistant call and all
    its results remain together in the subsequent segment.
    """
    if cut_idx <= 0:
        return 0
    if cut_idx >= len(messages):
        return len(messages)

    groups = find_tool_call_groups(messages)
    for start_idx, end_idx in groups:
        if start_idx < cut_idx <= end_idx:
            cut_idx = start_idx
            break

    # If landed on a tool message (e.g. without assistant), scan backward
    while cut_idx > 0 and _is_tool_result_message(messages[cut_idx]):
        cut_idx -= 1

    return max(0, cut_idx)


def validate_atomic_boundaries(messages: list[dict[str, Any]]) -> bool:
    """Validate that tool calls and tool results are atomically paired without orphans.

    Returns False if:
      - Any tool result message exists without a preceding assistant tool call.
      - Any assistant tool call is severed from its corresponding tool results.
      - Any tool_call_id in a tool result does not match the preceding assistant's tool_calls.
      - Any tool call declared in an assistant message has no matching tool result.
    """
    if not messages:
        return True

    n = len(messages)
    i = 0
    while i < n:
        msg = messages[i]
        role = msg.get("role")

        # 1. Orphaned tool result at top or unexpected position
        if role == "tool":
            return False

        # 2. XML observation without preceding assistant XML tool call
        if role == "user":
            content = msg.get("content", "")
            if isinstance(content, str) and ("<observation" in content or "<tool_result" in content):
                return False
            if msg.get("name") == "observation":
                return False

        # 3. Assistant message with structured tool calls
        if role == "assistant":
            tool_calls = msg.get("tool_calls")
            if isinstance(tool_calls, list) and tool_calls:
                expected_ids = [
                    str(tc.get("id") or tc.get("call_id") or "")
                    for tc in tool_calls
                    if isinstance(tc, dict)
                ]
                expected_set = set(expected_ids)
                expected_set.discard("")

                # Check following messages
                j = i + 1
                received_ids: list[str] = []
                while j < n and messages[j].get("role") == "tool":
                    t_id = str(messages[j].get("tool_call_id") or "")
                    received_ids.append(t_id)
                    j += 1

                # If no tool results followed, or if any expected id is missing
                if not received_ids:
                    return False

                if expected_set:
                    # Every expected call must have a matching response
                    if set(received_ids) != expected_set:
                        return False
                else:
                    if len(received_ids) != len(tool_calls):
                        return False

                i = j
                continue

            # Check XML tool calls
            content = msg.get("content", "")
            if isinstance(content, str) and ("<tool_call" in content or "</tool_call>" in content):
                j = i + 1
                obs_count = 0
                while j < n:
                    cand = messages[j]
                    cand_role = cand.get("role")
                    cand_content = cand.get("content", "")
                    if cand_role == "tool" or (
                        cand_role == "user"
                        and (
                            isinstance(cand_content, str)
                            and ("<observation" in cand_content or "<tool_result" in cand_content)
                            or cand.get("name") == "observation"
                        )
                    ):
                        obs_count += 1
                        j += 1
                    else:
                        break
                if obs_count == 0:
                    return False
                i = j
                continue

        i += 1

    return True


# ==============================================================================
# Phase 3: Middle-Turn Summarizer & ContextCompactor Core Engine
# ==============================================================================

SUMMARY_HEADER = "## CONTEXT COMPACTION SUMMARY"
SUMMARY_END_MARKER = "--- END OF CONTEXT SUMMARY ---"

_INFLIGHT_TASK_REPLAY_HEADER = (
    "[STILL IN PROGRESS — this is the active request, restated after the "
    "compaction boundary because it was not finished yet. Continue it; do not "
    "start over.]"
)


def is_context_summary_message(msg: dict[str, Any]) -> bool:
    """Detect whether a message represents a compacted summary block."""
    if not isinstance(msg, dict):
        return False
    content = msg.get("content", "")
    if isinstance(content, str):
        return (
            SUMMARY_HEADER in content
            or "[CONTEXT COMPACTION" in content
            or SUMMARY_END_MARKER in content
        )
    return False


@dataclass
class CompactionConfig:
    """Configuration governing context window compaction behavior."""

    max_context_tokens: int = 100_000
    trigger_threshold: float = 0.8
    protect_first_n: int = 1
    protect_last_n: int = 6
    tail_token_budget: int = 25_000
    keep_tool_rounds: int = 4
    min_demote_chars: int = 1000
    pin_task_turn: bool = False


@dataclass
class CompactionResult:
    """Telemetry and outcome of a context compaction execution."""

    compacted_messages: list[dict[str, Any]]
    original_tokens: int
    compacted_tokens: int
    compression_ratio: float
    phase1_demoted_tools: int
    phase3_summarized_turns: int
    used_fallback: bool


class ContextCompactor:
    """4-Phase Context Compactor Engine.

    Compacts conversational history while protecting the static system head
    and active tail to stay within context budgets and maximize KV cache hits.
    """

    def __init__(
        self,
        config: CompactionConfig | None = None,
        model_caller: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config or CompactionConfig()
        self.model_caller = model_caller
        self.compression_count: int = 0

    def _effective_protect_first_n(
        self, messages: list[dict[str, Any]] | None = None
    ) -> int:
        """Return effective protect_first_n, decaying to 0 after first compaction.

        If pin_task_turn is set, does not decay.
        """
        if self.config.pin_task_turn:
            return self.config.protect_first_n
        if self.compression_count >= 1:
            return 0
        if messages and len(messages) > 1:
            # Check if an earlier turn is already a compaction summary
            for m in messages[1: min(len(messages), 4)]:
                if is_context_summary_message(m):
                    return 0
        return self.config.protect_first_n

    def _protect_head_size(self, messages: list[dict[str, Any]]) -> int:
        """Head messages to protect: system message (if present) plus decaying protect_first_n."""
        head = 1 if messages and messages[0].get("role") == "system" else 0
        return head + self._effective_protect_first_n(messages)

    def _find_tail_cut(self, messages: list[dict[str, Any]], compress_start: int) -> int:
        """Calculate compress_end backward from tail using token budget and protect_last_n."""
        n = len(messages)
        if n <= compress_start:
            return compress_start

        available = n - compress_start
        if available <= 1:
            return compress_start

        # Scale tail token budget if max_context_tokens is small
        effective_tail_budget = min(
            self.config.tail_token_budget,
            max(100, int(self.config.max_context_tokens * 0.4)),
        )

        tokens_acc = 0
        cut_idx = n
        for i in range(n - 1, compress_start - 1, -1):
            msg_tok = estimate_message_tokens(messages[i])
            tail_count = n - i
            # Stop walking back if we have met protect_last_n and adding this message exceeds budget,
            # OR if adding this message would leave less than 1 message in the middle.
            if (
                tail_count >= self.config.protect_last_n
                and tokens_acc + msg_tok > effective_tail_budget
            ) or (i <= compress_start):
                cut_idx = i + 1 if i > compress_start else compress_start + 1
                break
            tokens_acc += msg_tok
            cut_idx = i

        cut_idx = min(max(compress_start, cut_idx), n)
        aligned = align_boundary_backward(messages, cut_idx)
        if aligned <= compress_start:
            aligned = align_boundary_forward(messages, cut_idx)
            if aligned >= n:
                aligned = cut_idx

        return min(max(compress_start, aligned), n)

    def _plan_compaction_window(
        self, messages: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], int, int, int]:
        """Phases 1-2 of compaction: returns (working, demoted_count, compress_start, compress_end)."""
        working, demoted_count = demote_stale_tail_tools(
            messages,
            keep_tool_rounds=self.config.keep_tool_rounds,
            min_demote_chars=self.config.min_demote_chars,
        )
        head_size = self._protect_head_size(working)
        compress_start = align_boundary_forward(working, head_size)
        compress_end = self._find_tail_cut(working, compress_start)
        return working, demoted_count, compress_start, compress_end

    def _extract_user_text(self, msg: dict[str, Any]) -> str:
        """Extract user request text, stripping any prior in-flight replay header."""
        content = msg.get("content", "")
        if isinstance(content, list):
            parts = []
            for b in content:
                if isinstance(b, dict) and (b.get("type") == "text" or "text" in b):
                    parts.append(str(b.get("text", "")))
                elif isinstance(b, str):
                    parts.append(b)
            content = " ".join(parts)
        text = str(content).strip()
        if _INFLIGHT_TASK_REPLAY_HEADER in text:
            text = text.rsplit(_INFLIGHT_TASK_REPLAY_HEADER, 1)[1].strip()
        return text

    def _generate_deterministic_summary(self, turns: list[dict[str, Any]]) -> str:
        """Generate a clean, deterministic markdown summary of turns without an LLM call."""
        user_intents: list[str] = []
        actions_taken: list[str] = []
        findings_errors: list[str] = []
        working_state: list[str] = []

        for msg in turns:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    b.get("text", "") if isinstance(b, dict) else str(b)
                    for b in content
                )
            content_str = str(content).strip()

            if role == "user":
                if (
                    "<observation" not in content_str
                    and "<tool_result" not in content_str
                    and msg.get("name") != "observation"
                ):
                    clean = content_str.split("\n")[0][:120].strip()
                    if clean and clean not in user_intents:
                        user_intents.append(clean)
                else:
                    match = _XML_OBSERVATION_RE.search(content_str)
                    tool_n = match.group("tool_name") if match and match.group("tool_name") else "tool"
                    body = match.group("body").strip() if match else content_str
                    if _is_tool_error(msg, body):
                        findings_errors.append(f"{tool_n} error: {body[:100]}...")
                    else:
                        findings_errors.append(f"{tool_n} observed: {body[:80]}...")
            elif role == "assistant":
                tool_calls = msg.get("tool_calls")
                if isinstance(tool_calls, list):
                    for tc in tool_calls:
                        if isinstance(tc, dict):
                            fn = tc.get("function") or {}
                            fn_name = tc.get("name") or fn.get("name") or "tool"
                            actions_taken.append(f"Executed {fn_name}")
                for m in re.finditer(r"<tool_call>(.*?)</tool_call>", content_str, re.DOTALL):
                    try:
                        tc_dict = json.loads(m.group(1))
                        t_name = tc_dict.get("name", "tool")
                        actions_taken.append(f"Executed {t_name}")
                    except Exception:
                        actions_taken.append("Executed tool call")
                if content_str and not content_str.startswith("<tool_call"):
                    first_line = content_str.split("\n")[0][:100].strip()
                    if first_line and not first_line.startswith("<"):
                        working_state.append(first_line)
            elif role == "tool":
                t_name = msg.get("name") or "tool"
                if _is_tool_error(msg, content_str):
                    findings_errors.append(f"{t_name} error: {content_str[:100]}...")
                else:
                    findings_errors.append(f"{t_name} result: {content_str[:80]}...")

        lines = [
            SUMMARY_HEADER,
            "Earlier turns were compacted into this structured summary reference.",
            "",
            "### Active Goals & User Intent",
        ]
        if user_intents:
            for ui in user_intents[:5]:
                lines.append(f"- {ui}")
        else:
            lines.append("- Execute task instructions from previous turns.")

        lines.extend(["", "### Actions Taken & Files Modified"])
        if actions_taken:
            for act in actions_taken[:8]:
                lines.append(f"- {act}")
        else:
            lines.append("- Processed multi-turn conversational context.")

        lines.extend(["", "### Key Findings, Errors & Tool Results"])
        if findings_errors:
            for fe in findings_errors[:8]:
                lines.append(f"- {fe}")
        else:
            lines.append("- Intermediate tool operations executed successfully.")

        lines.extend(["", "### Current Working State"])
        if working_state:
            lines.append(f"- {working_state[-1]}")
        else:
            lines.append(f"- Compacted {len(turns)} intermediate turns.")

        lines.extend(["", SUMMARY_END_MARKER])
        return "\n".join(lines)

    def _generate_model_summary(self, turns: list[dict[str, Any]]) -> str | None:
        """Invoke configured model_caller to produce structured compaction summary."""
        if not self.model_caller or not callable(self.model_caller):
            return None

        transcript_parts: list[str] = []
        for m in turns:
            r = m.get("role", "unknown")
            c = m.get("content", "")
            if isinstance(c, list):
                c = str(c)
            transcript_parts.append(f"[{r.upper()}]: {str(c)[:500]}")
        transcript_text = "\n".join(transcript_parts)

        prompt = (
            "You are a context compaction engine. Summarize the following middle turns of a conversation between a user and an AI assistant.\n"
            "Produce a concise, factual summary strictly adhering to the following structure:\n\n"
            f"{SUMMARY_HEADER}\n"
            "### Active Goals & User Intent\n"
            "<key goals>\n\n"
            "### Actions Taken & Files Modified\n"
            "<actions>\n\n"
            "### Key Findings, Errors & Tool Results\n"
            "<findings>\n\n"
            "### Current Working State\n"
            "<state>\n\n"
            f"{SUMMARY_END_MARKER}\n\n"
            f"Conversation to summarize:\n{transcript_text}"
        )

        try:
            res = None
            try:
                res = self.model_caller([{"role": "user", "content": prompt}])
            except TypeError:
                res = self.model_caller(prompt)
            text = ""
            if isinstance(res, str):
                text = res
            elif isinstance(res, dict):
                text = str(res.get("text") or res.get("content") or "")
            elif hasattr(res, "text"):
                text = str(res.text)
            elif hasattr(res, "content"):
                text = str(res.content)
            text = text.strip()
            if not text or len(text) < 20:
                return None
            if SUMMARY_HEADER not in text:
                text = f"{SUMMARY_HEADER}\n{text}"
            if SUMMARY_END_MARKER not in text:
                text = f"{text}\n\n{SUMMARY_END_MARKER}"
            return text
        except Exception:
            return None

    def should_compact(self, messages: list[dict[str, Any]]) -> bool:
        """Check whether messages exceed compaction trigger token budget."""
        tokens = estimate_messages_tokens(messages)
        threshold = int(self.config.max_context_tokens * self.config.trigger_threshold)
        return tokens >= threshold

    def compact_messages(
        self, messages: list[dict[str, Any]], force: bool = True
    ) -> CompactionResult:
        """Run full context compaction pipeline over `messages`.

        1. Phase 1: Demote bulky stale tool execution outputs.
        2. Phase 2: Compute atomic tool-pair cut boundaries.
        3. Phase 3: Summarize middle turns (model-assisted or deterministic fallback)
           and restate in-flight tasks.
        4. Anti-thrashing validation: if compacted >= original, revert.
        """
        if not messages:
            return CompactionResult(
                compacted_messages=[],
                original_tokens=0,
                compacted_tokens=0,
                compression_ratio=1.0,
                phase1_demoted_tools=0,
                phase3_summarized_turns=0,
                used_fallback=False,
            )

        original_tokens = estimate_messages_tokens(messages)

        if not force and not self.should_compact(messages):
            return CompactionResult(
                compacted_messages=list(messages),
                original_tokens=original_tokens,
                compacted_tokens=original_tokens,
                compression_ratio=1.0,
                phase1_demoted_tools=0,
                phase3_summarized_turns=0,
                used_fallback=False,
            )

        # 1-2. Plan window and demote stale tools
        working, demoted_count, compress_start, compress_end = (
            self._plan_compaction_window(messages)
        )

        middle_turns = working[compress_start:compress_end]
        phase3_turns = len(middle_turns)
        used_fallback = False

        if phase3_turns > 0:
            summary_text = self._generate_model_summary(middle_turns)
            if not summary_text:
                summary_text = self._generate_deterministic_summary(middle_turns)
                used_fallback = True

            # In-flight task restatement: check if tail has active user prompt
            tail_has_user = any(
                m.get("role") == "user" and not _is_tool_result_message(m)
                for m in working[compress_end:]
            )
            inflight_task_text = ""
            if not tail_has_user:
                for m in reversed(working[:compress_end]):
                    if m.get("role") == "user" and not _is_tool_result_message(m):
                        inflight_task_text = self._extract_user_text(m)
                        break

            head = list(working[:compress_start])
            tail = list(working[compress_end:])

            last_head_role = head[-1].get("role") if head else None
            summary_role = "assistant" if last_head_role == "user" else "user"

            if inflight_task_text:
                if summary_role == "user":
                    carrier_content = (
                        f"{summary_text}\n\n"
                        f"{_INFLIGHT_TASK_REPLAY_HEADER}\n"
                        f"{inflight_task_text}"
                    )
                    compacted = head + [{"role": "user", "content": carrier_content}] + tail
                else:
                    compacted = (
                        head
                        + [{"role": "assistant", "content": summary_text}]
                        + [{"role": "user", "content": f"{_INFLIGHT_TASK_REPLAY_HEADER}\n{inflight_task_text}"}]
                        + tail
                    )
            else:
                compacted = head + [{"role": summary_role, "content": summary_text}] + tail
        else:
            compacted = working

        compacted_tokens = estimate_messages_tokens(compacted)

        # Anti-thrashing checks: if compaction did not reduce tokens, revert to original
        if compacted_tokens >= original_tokens:
            return CompactionResult(
                compacted_messages=list(messages),
                original_tokens=original_tokens,
                compacted_tokens=original_tokens,
                compression_ratio=1.0,
                phase1_demoted_tools=demoted_count,
                phase3_summarized_turns=0,
                used_fallback=used_fallback,
            )

        self.compression_count += 1
        ratio = compacted_tokens / max(1, original_tokens)

        return CompactionResult(
            compacted_messages=compacted,
            original_tokens=original_tokens,
            compacted_tokens=compacted_tokens,
            compression_ratio=ratio,
            phase1_demoted_tools=demoted_count,
            phase3_summarized_turns=phase3_turns,
            used_fallback=used_fallback,
        )


# ==============================================================================
# Phase 4: Immutable Prompt-Cache Prefixing & Provider Cache Breakpoints
# ==============================================================================

def stabilize_prompt_cache_prefix(
    messages: list[dict[str, Any]],
    system_prompt: str,
    tools_schema: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Enforce byte-for-byte immutable prompt-cache prefix for high KV hit rates.

    Ensures that the system prompt message occupies index 0 with canonical,
    deterministic text, dynamic runtime stamps are excluded from the static prefix,
    and subsequent conversational turns are cleanly preserved.
    """
    clean_messages: list[dict[str, Any]] = []

    # 1. Canonical system message at index 0
    canonical_system = str(system_prompt).strip()
    clean_messages.append({"role": "system", "content": canonical_system})

    # 2. Add non-system messages from input
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "system":
            # Strip redundant internal system messages to avoid cache churn
            continue
        clean_messages.append(dict(msg))

    return clean_messages


def compute_cache_signature(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    prefix_turns: int = 2,
) -> str:
    """Compute deterministic SHA-256 hex digest of the static prompt-cache prefix.

    Covers the sorted canonical tool schemas and the first `prefix_turns`
    messages (typically system instructions and initial task turn).
    """
    hasher = hashlib.sha256()

    # Tool schemas deterministic serialization
    if tools:
        canonical_tools = json.dumps(tools, sort_keys=True, separators=(",", ":"))
        hasher.update(canonical_tools.encode("utf-8"))

    # First `prefix_turns` messages
    prefix_slice = messages[:prefix_turns]
    for msg in prefix_slice:
        # Canonical message representation ignoring ephemeral internal keys
        msg_copy = {k: v for k, v in msg.items() if not k.startswith("_")}
        canonical_msg = json.dumps(msg_copy, sort_keys=True, separators=(",", ":"))
        hasher.update(canonical_msg.encode("utf-8"))

    return hasher.hexdigest()


def apply_cache_control_breakpoints(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    provider: str = "anthropic",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
    """Inject provider-specific ephemeral cache control breakpoints.

    For Anthropic: injects `"cache_control": {"type": "ephemeral"}` onto:
      1. The last tool definition in `tools` (caches tools schema).
      2. The system prompt message (caches static system instructions).
      3. The latest context summary checkpoint in middle history (if present).
      4. The second-to-last turn in the active tail (caches conversation prefix).
    Respects Anthropic maximum of 4 cache breakpoints per request.

    For Gemini and OpenAI: preserves exact byte stability without injecting breakpoints.
    """
    tagged_msgs: list[dict[str, Any]] = [dict(m) for m in messages]
    tagged_tools: list[dict[str, Any]] | None = [dict(t) for t in tools] if tools is not None else None

    prov = provider.lower()
    if "anthropic" not in prov and "claude" not in prov:
        return tagged_msgs, tagged_tools

    breakpoints_used = 0
    max_breakpoints = 4

    # 1. Last tool definition
    if tagged_tools and len(tagged_tools) > 0 and breakpoints_used < max_breakpoints:
        tagged_tools[-1] = dict(tagged_tools[-1])
        tagged_tools[-1]["cache_control"] = {"type": "ephemeral"}
        breakpoints_used += 1

    # 2. System prompt message (index 0)
    if tagged_msgs and tagged_msgs[0].get("role") == "system" and breakpoints_used < max_breakpoints:
        tagged_msgs[0] = dict(tagged_msgs[0])
        tagged_msgs[0]["cache_control"] = {"type": "ephemeral"}
        breakpoints_used += 1

    # 3. Latest context summary checkpoint in middle history
    summary_idx = -1
    for idx in range(len(tagged_msgs) - 1, 0, -1):
        if is_context_summary_message(tagged_msgs[idx]):
            summary_idx = idx
            break

    if summary_idx > 0 and breakpoints_used < max_breakpoints:
        tagged_msgs[summary_idx] = dict(tagged_msgs[summary_idx])
        tagged_msgs[summary_idx]["cache_control"] = {"type": "ephemeral"}
        breakpoints_used += 1

    # 4. Second-to-last turn in active tail (or last turn if >= 2 messages and not already tagged)
    if len(tagged_msgs) >= 2 and breakpoints_used < max_breakpoints:
        tail_target_idx = len(tagged_msgs) - 2 if len(tagged_msgs) > 2 else len(tagged_msgs) - 1
        if tail_target_idx != summary_idx and tail_target_idx != 0:
            tagged_msgs[tail_target_idx] = dict(tagged_msgs[tail_target_idx])
            tagged_msgs[tail_target_idx]["cache_control"] = {"type": "ephemeral"}
            breakpoints_used += 1

    return tagged_msgs, tagged_tools

