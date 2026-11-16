"""Degenerate Loop / Repetition Sentry & Thinking Tag Scrubber.

Intercepts token repetition, tool call thrashing, and cyclical ping-pong action loops
with authoritative steering prompt injections, and scrubs reasoning/thinking tags cleanly.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import hashlib
import json
import math
import re
import time
from typing import Any

# Repetition thresholds
MIN_FRAGMENT_LENGTH = 400
_REPEAT_WINDOW = 60
_MIN_REPEAT_COUNT = 5
_DOMINANCE_RATIO = 0.5
_RUNAWAY_DISTINCT_LINE_RATIO = 0.5
_MAX_ANCHOR_SAMPLES = 32
_MAX_ANCHOR_MATCHES = 8

REPETITION_LOOP_INTERRUPTED = "[the reply degenerated into a repetition loop and was interrupted]"

# Thinking tag definitions
THINK_TAG_NAMES: tuple[str, ...] = (
    "think",
    "thinking",
    "reasoning",
    "thought",
    "REASONING_SCRATCHPAD",
)

_THINK_CLOSED_RE = re.compile(
    r"<(" + "|".join(re.escape(t) for t in THINK_TAG_NAMES) + r")>(.*?)</\1>",
    re.DOTALL | re.IGNORECASE,
)
_THINK_UNCLOSED_RE = re.compile(
    r"<(" + "|".join(re.escape(t) for t in THINK_TAG_NAMES) + r")>(.*)$",
    re.DOTALL | re.IGNORECASE,
)


def _line_repetition_dominated(text: str, n: int, dominance_ratio: float = _DOMINANCE_RATIO) -> bool:
    """True when a single normalized line covers at least half the fragment via repeats."""
    lines = [line.strip() for line in text.splitlines()]
    counts = Counter(line for line in lines if line)
    return any(c >= _MIN_REPEAT_COUNT and (c * len(line)) >= (n * dominance_ratio) for line, c in counts.items())


def _window_count_dominated(text: str, n: int, dominance_ratio: float = _DOMINANCE_RATIO, window: int = _REPEAT_WINDOW) -> bool:
    """True when one 60-char sliding window recurs often enough to cover at least half of text."""
    if n < window:
        return False
    needed = max(_MIN_REPEAT_COUNT, math.ceil(n * dominance_ratio / window))
    counts: dict[str, int] = {}
    for i in range(n - window + 1):
        key = text[i : i + window]
        c = counts.get(key, 0) + 1
        if c >= needed:
            return True
        counts[key] = c
    return False


def _expand_run(text: str, n: int, start: int, period: int, matched: int) -> tuple[int, int]:
    """Expand one known equal window to the [left, right) bounds of its exact periodic run."""
    left = start
    while left > 0 and text[left - 1] == text[left - 1 + period]:
        left -= 1

    right = start + matched
    while right + period < n and text[right] == text[right + period]:
        right += 1
    return left, right + period


def _periodic_run_dominated(text: str, n: int, dominance_ratio: float = _DOMINANCE_RATIO, window: int = _REPEAT_WINDOW) -> bool:
    """Detect a dominant exact periodic run from evenly spaced anchors."""
    max_start = n - window
    if max_start < 1:
        return False

    sample_step = max(1, (max_start + _MAX_ANCHOR_SAMPLES - 2) // (_MAX_ANCHOR_SAMPLES - 1))
    sample_starts = list(range(0, max_start + 1, sample_step))
    if sample_starts[-1] != max_start:
        sample_starts.append(max_start)

    rejected: list[tuple[int, int, int]] = []
    for start in sample_starts:
        anchor = text[start : start + window]
        search_from = start + 1
        for _ in range(_MAX_ANCHOR_MATCHES):
            match = text.find(anchor, search_from)
            if match < 0:
                break
            period = match - start
            search_from = match + 1
            if any(lo <= start < hi and period % p == 0 for lo, hi, p in rejected):
                continue
            left, right = _expand_run(text, n, start, period, window)
            if (right - left) >= (_MIN_REPEAT_COUNT * period) and (right - left) >= (n * dominance_ratio):
                return True
            rejected.append((left, right, period))
    return False


def is_repetition_dominated(
    text: str,
    dominance_ratio: float = _DOMINANCE_RATIO,
    min_fragment_length: int = MIN_FRAGMENT_LENGTH,
) -> bool:
    """True when a repetition loop covers at least the dominance_ratio of text."""
    if not isinstance(text, str):
        return False
    n = len(text)
    if n < min_fragment_length:
        return False

    if _line_repetition_dominated(text, n, dominance_ratio):
        return True

    return _window_count_dominated(text, n, dominance_ratio) or _periodic_run_dominated(text, n, dominance_ratio)


def is_runaway_repetition(
    text: str,
    dominance_ratio: float = _DOMINANCE_RATIO,
    min_fragment_length: int = MIN_FRAGMENT_LENGTH,
) -> bool:
    """Stricter than is_repetition_dominated: filters legitimate multiline structures.

    A legitimately repetitive but correct reply (distinct SQL batch rows, structured tables)
    has many distinct lines. Runaway repetition re-emits identical lines so distinct line ratio <= 0.5.
    """
    if not is_repetition_dominated(text, dominance_ratio, min_fragment_length):
        return False
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < _MIN_REPEAT_COUNT:
        return True
    distinct_ratio = len(set(lines)) / len(lines)
    return distinct_ratio <= _RUNAWAY_DISTINCT_LINE_RATIO


def strip_thinking_tags(text: str) -> tuple[str, str | None]:
    """Extract contents of thinking tags (<think>...</think>) and strip them from output text.

    Supports complete blocks (<think>...</think>) and unclosed streaming tails (<think>...$).
    Returns (cleaned_text, reasoning_content). If no reasoning tags are present, reasoning_content is None.
    """
    if not text or not isinstance(text, str):
        return text or "", None

    reasoning_parts: list[str] = []

    def _replace_closed(match: re.Match[str]) -> str:
        inner = match.group(2).strip()
        if inner:
            reasoning_parts.append(inner)
        return ""

    cleaned = _THINK_CLOSED_RE.sub(_replace_closed, text)

    unclosed_match = _THINK_UNCLOSED_RE.search(cleaned)
    if unclosed_match:
        inner = unclosed_match.group(2).strip()
        if inner:
            reasoning_parts.append(inner)
        cleaned = cleaned[: unclosed_match.start()]

    cleaned = cleaned.strip()
    reasoning = "\n\n".join(reasoning_parts).strip() if reasoning_parts else None
    return cleaned, reasoning


@dataclass
class ActionRecord:
    """Record of a single tool invocation for loop detection."""

    tool_name: str
    args_hash: str
    is_error: bool
    output_summary: str
    timestamp: float = field(default_factory=time.time)


class ActionLoopDetector:
    """Detects tool call thrashing and repeating action cycles across turns."""

    def __init__(self, repeat_threshold: int = 3, cycle_threshold: int = 2) -> None:
        self.repeat_threshold = repeat_threshold
        self.cycle_threshold = cycle_threshold
        self.history: list[ActionRecord] = []

    def _hash_arguments(self, arguments: dict[str, Any] | None) -> str:
        """Compute deterministic hash of tool call arguments."""
        if not arguments:
            return "empty"
        try:
            norm = json.dumps(arguments, sort_keys=True, default=str)
        except Exception:
            norm = str(sorted(arguments.items()))
        return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]

    def record_action(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        is_error: bool = False,
        output: Any = "",
    ) -> None:
        """Record an executed action into the recent invocation window."""
        args_hash = self._hash_arguments(arguments)
        summary = str(output)[:100].strip() if output else ""
        self.history.append(
            ActionRecord(
                tool_name=tool_name,
                args_hash=args_hash,
                is_error=is_error,
                output_summary=summary,
            )
        )
        if len(self.history) > 20:
            self.history = self.history[-20:]

    def check_thrashing(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
    ) -> tuple[bool, str, dict[str, Any]]:
        """Evaluate if an incoming tool call constitutes degenerate thrashing.

        Returns (is_thrashing, loop_type, details).
        """
        args_hash = self._hash_arguments(arguments)

        # 1. Identical call repetition: Same tool and args repeated >= repeat_threshold times
        required_prior = self.repeat_threshold - 1
        if len(self.history) >= required_prior:
            tail = self.history[-required_prior:]
            if all(rec.tool_name == tool_name and rec.args_hash == args_hash for rec in tail):
                # Thrashing if prior attempts resulted in error or empty results
                if all(rec.is_error or not rec.output_summary for rec in tail):
                    return (
                        True,
                        "identical_call_repetition",
                        {
                            "tool_name": tool_name,
                            "count": self.repeat_threshold,
                            "arguments": arguments,
                        },
                    )

        # 2. Ping-pong cycle detection: Repeating sequence of 2-3 tools without state progress
        candidate_call = (tool_name, args_hash)
        full_seq = [(rec.tool_name, rec.args_hash) for rec in self.history] + [candidate_call]

        for period in (2, 3):
            needed_len = period * self.cycle_threshold
            if len(full_seq) >= needed_len:
                tail_window = full_seq[-needed_len:]
                unit = tail_window[:period]
                expected = unit * self.cycle_threshold
                if tail_window == expected:
                    cycle_tools = [t[0] for t in unit]
                    return (
                        True,
                        "ping_pong_cycle",
                        {
                            "cycle": cycle_tools,
                            "tool_name": tool_name,
                            "period": period,
                        },
                    )

        return False, "", {}

    def reset(self) -> None:
        """Clear the invocation history."""
        self.history.clear()


def generate_steering_prompt(loop_type: str, details: dict[str, Any]) -> str:
    """Generate an authoritative out-of-band system steering injection."""
    tool_name = details.get("tool_name", "the tool")
    if loop_type == "identical_call_repetition":
        return (
            f"[SYSTEM STEERING: Degenerate action loop detected on {tool_name}. "
            "Repeating this exact operation will not succeed. Halt immediately "
            "and adopt an alternative approach or report the blocker.]"
        )
    elif loop_type == "ping_pong_cycle":
        cycle_tools = " -> ".join(details.get("cycle", [tool_name]))
        return (
            f"[SYSTEM STEERING: Cyclical ping-pong action loop detected ({cycle_tools}). "
            "The workflow is oscillating between tools without state progression. "
            "Halt immediately and re-evaluate strategy or ask user clarification.]"
        )
    elif loop_type == "token_repetition":
        return (
            "[SYSTEM STEERING: Degenerate token repetition loop detected. "
            "Model output degenerated into repetitive fragments and was interrupted. "
            "Formulate a direct, non-repetitive response.]"
        )
    return (
        f"[SYSTEM STEERING: Degenerate loop detected on {tool_name}. "
        "Halt repeating actions and select an alternative strategy.]"
    )


class RepetitionSentry:
    """High-level cognition guard combining token repetition, action thrashing, and thinking scrubbing."""

    def __init__(self, repeat_threshold: int = 3, cycle_threshold: int = 2) -> None:
        self.action_detector = ActionLoopDetector(
            repeat_threshold=repeat_threshold,
            cycle_threshold=cycle_threshold,
        )

    def check_text_repetition(self, text: str) -> bool:
        """Check if output text exhibits runaway repetition."""
        return is_runaway_repetition(text)

    def strip_thinking(self, text: str) -> tuple[str, str | None]:
        """Strip thinking tags and extract reasoning."""
        return strip_thinking_tags(text)

    def generate_steering(self, loop_type: str, details: dict[str, Any]) -> str:
        """Generate out-of-band steering prompt."""
        return generate_steering_prompt(loop_type, details)

    def filter_model_output(self, text: str) -> tuple[str, str | None, bool]:
        """Filter model output: check repetition, scrub thinking tags.

        Returns (scrubbed_text, reasoning_content, is_repetition_interrupted).
        """
        if self.check_text_repetition(text):
            return REPETITION_LOOP_INTERRUPTED, None, True

        scrubbed, reasoning = self.strip_thinking(text)
        return scrubbed, reasoning, False
