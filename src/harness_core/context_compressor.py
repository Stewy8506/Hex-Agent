"""4-Phase Context Compactor & Prompt Cache Locking Core.

Provides deterministic stale tool output demotion, atomic tool-pair boundary
protection, middle-turn summarization, and immutable prompt-cache prefixing
matching production agent standards.
"""

from __future__ import annotations

import json
import re
from typing import Any

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
