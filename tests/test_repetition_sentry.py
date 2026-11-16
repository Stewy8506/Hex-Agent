"""Unit and integration tests for Repetition Sentry and Thinking Tag Scrubber."""

from __future__ import annotations

import time
import pytest

from harness_core.cognition.repetition_sentry import (
    MIN_FRAGMENT_LENGTH,
    REPETITION_LOOP_INTERRUPTED,
    ActionLoopDetector,
    RepetitionSentry,
    generate_steering_prompt,
    is_repetition_dominated,
    is_runaway_repetition,
    strip_thinking_tags,
)
from harness_core.core.react_loop import ReActConfig, ReActLoop, ToolCallResult


class TestRepetitionSentry:
    """Tests for token repetition and n-gram dominance detection."""

    def test_line_repetition_dominated(self):
        """Verifies that line-echo repetition covering >=50% of the text is detected."""
        echo_line = "SELECT * FROM users WHERE status = 'active';"
        # 20 identical lines is 880+ chars
        repetitive_text = (echo_line + "\n") * 20
        assert len(repetitive_text) >= MIN_FRAGMENT_LENGTH
        assert is_repetition_dominated(repetitive_text) is True
        assert is_runaway_repetition(repetitive_text) is True

    def test_window_repetition_without_newlines(self):
        """Verifies that 60-character sliding window repeats without newlines are flagged."""
        phrase = "The quick brown fox jumps over the lazy dog and lands softly on the grass. "
        repeated = phrase * 15
        assert len(repeated) >= MIN_FRAGMENT_LENGTH
        assert is_repetition_dominated(repeated) is True
        assert is_runaway_repetition(repeated) is True

    def test_legitimate_batch_inserts_not_flagged(self):
        """Legitimate batch inserts with distinct rows must not trigger runaway repetition."""
        # Distinct lines share prefix 'INSERT INTO table VALUES (...)' but each line has distinct data
        rows = [
            f"INSERT INTO items (id, name, created_at, price) VALUES ({i}, 'item_{i:04d}', '2026-11-16T12:00:00Z', {i * 1.5:.2f});"
            for i in range(50)
        ]
        text = "\n".join(rows)
        assert len(text) >= MIN_FRAGMENT_LENGTH
        # Even if window counts might see shared prefix, distinct line ratio is 1.0 > 0.5
        assert is_runaway_repetition(text) is False

    def test_legitimate_markdown_table_not_flagged(self):
        """Legitimate markdown table with distinct row data is not flagged as runaway repetition."""
        header = "| User ID | Name | Role | Department |\n|---|---|---|---|\n"
        rows = [f"| usr_{i:03d} | Employee {i} | Engineer | Core Platform |" for i in range(30)]
        text = header + "\n".join(rows)
        assert len(text) >= MIN_FRAGMENT_LENGTH
        assert is_runaway_repetition(text) is False

    def test_short_fragments_fail_open(self):
        """Short fragments below MIN_FRAGMENT_LENGTH are not flagged."""
        short_text = "echo echo echo echo echo echo echo echo"
        assert is_repetition_dominated(short_text) is False
        assert is_runaway_repetition(short_text) is False


class TestActionLoopDetector:
    """Tests for ActionLoopDetector tool call thrashing and cycle detection."""

    def test_identical_call_repetition_detected(self):
        """Detects identical tool and arguments repeating >=3 consecutive times with failure/empty output."""
        detector = ActionLoopDetector(repeat_threshold=3)

        # First call fails
        detector.record_action("read_file", {"path": "/invalid/path.py"}, is_error=True, output="FileNotFoundError")
        is_loop, _, _ = detector.check_thrashing("read_file", {"path": "/invalid/path.py"})
        assert not is_loop

        # Second call fails
        detector.record_action("read_file", {"path": "/invalid/path.py"}, is_error=True, output="FileNotFoundError")
        # Third incoming call with identical tool and args should trip thrashing!
        is_loop, loop_type, details = detector.check_thrashing("read_file", {"path": "/invalid/path.py"})
        assert is_loop
        assert loop_type == "identical_call_repetition"
        assert details["tool_name"] == "read_file"

    def test_successful_calls_do_not_trip_identical_repetition(self):
        """Consecutive calls that succeed with content do not trip thrashing (e.g. paginated reads)."""
        detector = ActionLoopDetector(repeat_threshold=3)
        detector.record_action("read_file", {"path": "/valid/data.log"}, is_error=False, output="line 1..100")
        detector.record_action("read_file", {"path": "/valid/data.log"}, is_error=False, output="line 101..200")
        is_loop, _, _ = detector.check_thrashing("read_file", {"path": "/valid/data.log"})
        assert not is_loop

    def test_ping_pong_action_cycle_detected(self):
        """Detects alternating ping-pong tool cycles (A -> B -> A -> B) without progression."""
        detector = ActionLoopDetector(cycle_threshold=2)

        # Cycle: git_status -> run_tests -> git_status -> run_tests
        detector.record_action("git_status", {})
        detector.record_action("run_tests", {})
        detector.record_action("git_status", {})

        # Incoming call: run_tests closes the 2nd cycle of (git_status, run_tests)
        is_loop, loop_type, details = detector.check_thrashing("run_tests", {})
        assert is_loop
        assert loop_type == "ping_pong_cycle"
        assert details["cycle"] == ["git_status", "run_tests"]

    def test_generate_steering_prompt_authoritative(self):
        """Verifies authoritative steering prompts for each loop type."""
        p1 = generate_steering_prompt("identical_call_repetition", {"tool_name": "compile_code"})
        assert "Degenerate action loop detected on compile_code" in p1
        assert "Repeating this exact operation will not succeed" in p1

        p2 = generate_steering_prompt("ping_pong_cycle", {"cycle": ["toolA", "toolB"]})
        assert "Cyclical ping-pong action loop detected" in p2
        assert "toolA -> toolB" in p2

        p3 = generate_steering_prompt("token_repetition", {})
        assert "Degenerate token repetition loop detected" in p3


class TestThinkingTagScrubber:
    """Tests for strip_thinking_tags functionality."""

    def test_closed_thinking_tags_scrubbed(self):
        """Strips complete closed thinking tags and extracts reasoning."""
        raw = "<think>Let me analyze the problem first. It requires sorting.</think>The solution is QuickSort."
        cleaned, reasoning = strip_thinking_tags(raw)
        assert cleaned == "The solution is QuickSort."
        assert reasoning == "Let me analyze the problem first. It requires sorting."

    def test_case_insensitive_and_alternate_tags(self):
        """Strips uppercase and alternate reasoning tag names."""
        raw1 = "<THINK>Thinking deeply...</THINK>Final answer."
        c1, r1 = strip_thinking_tags(raw1)
        assert c1 == "Final answer."
        assert r1 == "Thinking deeply..."

        raw2 = "<reasoning>Internal chain of thought</reasoning>Done."
        c2, r2 = strip_thinking_tags(raw2)
        assert c2 == "Done."
        assert r2 == "Internal chain of thought"

        raw3 = "<thought>Pondering</thought>Clean."
        c3, r3 = strip_thinking_tags(raw3)
        assert c3 == "Clean."
        assert r3 == "Pondering"

    def test_unclosed_streaming_tail_scrubbed(self):
        """Strips unclosed thinking block at streaming tail."""
        raw = "Partial answer so far. <think>Still reasoning about step 3..."
        cleaned, reasoning = strip_thinking_tags(raw)
        assert cleaned == "Partial answer so far."
        assert reasoning == "Still reasoning about step 3..."

    def test_text_without_thinking_tags_untouched(self):
        """Text without tags returns unchanged with None reasoning."""
        raw = "Normal assistant response with `code` and explanation."
        cleaned, reasoning = strip_thinking_tags(raw)
        assert cleaned == raw
        assert reasoning is None


class TestReActLoopRepetitionIntegration:
    """Integration tests for ReActLoop with RepetitionSentry."""

    def test_react_loop_intercepts_runaway_token_repetition(self):
        """ReActLoop intercepts runaway token repetition in model caller."""
        repeated_sentence = "Repeating output that causes a runaway loop over and over again.\n"
        looping_reply = repeated_sentence * 25

        def looping_caller(messages):
            return looping_reply

        cfg = ReActConfig(max_turns=2)
        loop = ReActLoop(model_caller=looping_caller, config=cfg)
        res = loop.run(prompt="Tell me a story")

        # The loop must catch the repetition and not deliver the raw loop
        assert REPETITION_LOOP_INTERRUPTED in res.final_answer or "Degenerate token repetition loop detected" in res.final_answer

    def test_react_loop_intercepts_action_thrashing_with_steering(self):
        """ReActLoop detects tool call thrashing, skips execution, and injects steering prompt."""
        turn_count = 0

        def thrashing_caller(messages):
            nonlocal turn_count
            turn_count += 1
            # Model repeatedly attempts same failed tool call
            return '<tool_call>{"name": "failing_tool", "arguments": {"param": 123}}</tool_call>'

        executed_tool_calls = 0

        def mock_tool_runner(name, args):
            nonlocal executed_tool_calls
            executed_tool_calls += 1
            return ToolCallResult(call_id="call_1", name=name, output="Error: Not found", is_error=True)

        cfg = ReActConfig(max_turns=5)
        loop = ReActLoop(model_caller=thrashing_caller, config=cfg)
        loop._execute_tool_call = lambda call: mock_tool_runner(call.name, call.arguments)

        res = loop.run(prompt="Run tool")

        # By turn 3, action loop detector triggers on identical tool calls
        # After thrashing is triggered, the real tool execution is skipped
        assert executed_tool_calls <= 3
        # Check messages history for injected SYSTEM STEERING prompt
        has_steering = any(
            "SYSTEM STEERING: Degenerate action loop detected" in str(msg.get("content", ""))
            or "SYSTEM STEERING: Degenerate action loop detected" in str(msg.get("observation", ""))
            for msg in loop.messages
        )
        assert has_steering

    def test_react_loop_scrubs_thinking_tags_from_history(self):
        """ReActLoop ensures thinking tags are never saved to assistant messages or final answer."""
        def thinking_caller(messages):
            return "<think>Private internal reasoning</think><finish>User-facing result</finish>"

        cfg = ReActConfig(max_turns=2)
        loop = ReActLoop(model_caller=thinking_caller, config=cfg)
        res = loop.run(prompt="Compute answer")

        assert res.success
        assert res.final_answer == "User-facing result"
        assert "<think>" not in res.final_answer
        assert "Private internal reasoning" not in res.final_answer
        for msg in loop.messages:
            if msg.get("role") == "assistant":
                assert "<think>" not in msg.get("content", "")
                assert "</think>" not in msg.get("content", "")
