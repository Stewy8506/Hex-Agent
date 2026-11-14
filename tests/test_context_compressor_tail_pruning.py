"""Unit tests for Context Compactor Phase 1 & Phase 2.

Covers token estimation, elision markers, stale tail tool demotion,
and atomic tool-call boundary protection and alignment.
"""

from __future__ import annotations

import unittest
from typing import Any

from harness_core.context_compressor import (
    HEX_COMPRESSION_MARKER_PREFIX,
    align_boundary_backward,
    align_boundary_forward,
    demote_stale_tail_tools,
    elide,
    elide_middle,
    estimate_message_tokens,
    estimate_messages_tokens,
    estimate_tokens_rough,
    find_tool_call_groups,
    is_demoted_stub,
    validate_atomic_boundaries,
)


class TestContextCompressorTailPruning(unittest.TestCase):
    """Test suite for Phase 1 (stale tool demotion) and Phase 2 (boundary alignment)."""

    def test_token_estimation(self):
        """Verify token counting on plain strings, multimodal structures, and tool calls."""
        # Plain text
        self.assertEqual(estimate_tokens_rough(""), 0)
        self.assertEqual(estimate_tokens_rough(None), 0)
        self.assertEqual(estimate_tokens_rough("hi"), 1)
        self.assertEqual(estimate_tokens_rough("hello world"), 3)
        self.assertEqual(estimate_tokens_rough("a" * 400), 100)

        # Message dictionary
        user_msg = {"role": "user", "content": "How do I implement quicksort?"}
        tokens = estimate_message_tokens(user_msg)
        self.assertGreater(tokens, 8)

        # Multimodal content blocks
        multi_msg = {
            "role": "user",
            "content": [
                {"type": "text", "text": "Look at this screenshot:"},
                {"type": "image", "image_url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg..."},
            ],
        }
        multi_tokens = estimate_message_tokens(multi_msg)
        self.assertGreaterEqual(multi_tokens, 85)

        # Tool calls in assistant message
        asst_msg = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_123",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "main.py"}'},
                }
            ],
        }
        asst_tokens = estimate_message_tokens(asst_msg)
        self.assertGreater(asst_tokens, 10)

        # Total messages estimation
        msgs = [user_msg, asst_msg]
        total_tokens = estimate_messages_tokens(msgs)
        self.assertGreater(total_tokens, tokens + asst_tokens)

    def test_elision_marker_and_middle(self):
        """Verify omitted character and line count accuracy and marker format."""
        short_text = "Short text"
        self.assertEqual(elide(short_text, 50), short_text)

        long_text = "Line 1\nLine 2\nLine 3\nLine 4\n" * 20
        elided = elide(long_text, 60)
        self.assertIn(HEX_COMPRESSION_MARKER_PREFIX, elided)
        self.assertLessEqual(len(elided), 120)

        # Elide middle
        middle_elided = elide_middle(long_text, head_chars=30, tail_chars=30)
        self.assertTrue(middle_elided.startswith(long_text[:30].rstrip()))
        self.assertTrue(middle_elided.endswith(long_text[-30:].lstrip()))
        self.assertIn(HEX_COMPRESSION_MARKER_PREFIX, middle_elided)
        self.assertIn("chars", middle_elided)
        self.assertIn("lines) omitted", middle_elided)

    def test_demote_stale_tail_tools_structured(self):
        """Verify 10 rounds of structured tool calls demote first 6, keeping newest 4 intact."""
        messages: list[dict[str, Any]] = []
        for i in range(10):
            messages.append(
                {
                    "role": "assistant",
                    "content": f"Running step {i}",
                    "tool_calls": [
                        {
                            "id": f"call_{i}",
                            "type": "function",
                            "function": {"name": "fetch_logs", "arguments": f'{{"step": {i}}}'},
                        }
                    ],
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": f"call_{i}",
                    "name": "fetch_logs",
                    "content": f"[LOGS STEP {i}] " + ("log output line\n" * 120),  # > 1500 chars
                }
            )

        compacted, demoted_count = demote_stale_tail_tools(
            messages,
            keep_tool_rounds=4,
            min_demote_chars=1000,
            head_chars=150,
            tail_chars=150,
        )

        self.assertEqual(demoted_count, 6)
        self.assertEqual(len(compacted), 20)

        # First 6 tool results (indices 1, 3, 5, 7, 9, 11) must be demoted
        for r in range(6):
            tool_idx = r * 2 + 1
            content = compacted[tool_idx]["content"]
            self.assertTrue(is_demoted_stub(content), f"Round {r} should be demoted")
            self.assertIn("fetch_logs output demoted at compaction", content)
            self.assertIn(HEX_COMPRESSION_MARKER_PREFIX, content)
            self.assertIn(f"[LOGS STEP {r}]", content)  # Head preserved

        # Last 4 tool results (indices 13, 15, 17, 19) must be 100% untouched
        for r in range(6, 10):
            tool_idx = r * 2 + 1
            self.assertEqual(compacted[tool_idx]["content"], messages[tool_idx]["content"])

    def test_demote_stale_tail_tools_xml(self):
        """Verify XML ReAct turns with <observation tool="..."> truncate older turns."""
        messages: list[dict[str, Any]] = []
        for i in range(8):
            messages.append(
                {
                    "role": "assistant",
                    "content": f"<tool_call name=\"grep_code\">pattern_{i}</tool_call>",
                }
            )
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f'<observation tool="grep_code">\n'
                        f'Matches for pattern_{i}:\n'
                        + ("result line in codebase.py:42\n" * 80)
                        + f'</observation>'
                    ),
                }
            )

        compacted, demoted_count = demote_stale_tail_tools(
            messages,
            keep_tool_rounds=3,
            min_demote_chars=800,
            head_chars=100,
            tail_chars=100,
        )

        self.assertEqual(demoted_count, 5)

        # Check that older observations maintain valid XML wrapper
        for r in range(5):
            obs_idx = r * 2 + 1
            content = compacted[obs_idx]["content"]
            self.assertTrue(content.startswith('<observation tool="grep_code">'))
            self.assertTrue(content.endswith("</observation>"))
            self.assertIn("grep_code output demoted at compaction", content)
            self.assertIn(HEX_COMPRESSION_MARKER_PREFIX, content)

        # Check newest 3 remain completely untouched
        for r in range(5, 8):
            obs_idx = r * 2 + 1
            self.assertEqual(compacted[obs_idx]["content"], messages[obs_idx]["content"])

    def test_demote_stale_preserves_errors(self):
        """Verify tool errors (is_error=True or Error: prefix) are never demoted."""
        messages = [
            # Round 0: error via is_error flag
            {
                "role": "assistant",
                "tool_calls": [{"id": "c0", "function": {"name": "run_test"}}],
            },
            {
                "role": "tool",
                "tool_call_id": "c0",
                "name": "run_test",
                "is_error": True,
                "content": "AssertionError: expected 4 got 2\n" + ("trace line\n" * 100),
            },
            # Round 1: error via Error: prefix
            {
                "role": "assistant",
                "tool_calls": [{"id": "c1", "function": {"name": "run_build"}}],
            },
            {
                "role": "tool",
                "tool_call_id": "c1",
                "name": "run_build",
                "content": "Error: compilation failed with code 1\n" + ("stack dump\n" * 100),
            },
            # Round 2: normal bulky output
            {
                "role": "assistant",
                "tool_calls": [{"id": "c2", "function": {"name": "read_doc"}}],
            },
            {
                "role": "tool",
                "tool_call_id": "c2",
                "name": "read_doc",
                "content": "Documentation body:\n" + ("content line\n" * 100),
            },
            # Protected recent round (round 3)
            {
                "role": "assistant",
                "tool_calls": [{"id": "c3", "function": {"name": "final_verify"}}],
            },
            {
                "role": "tool",
                "tool_call_id": "c3",
                "name": "final_verify",
                "content": "Verification success!\n" + ("ok line\n" * 100),
            },
        ]

        compacted, demoted_count = demote_stale_tail_tools(
            messages,
            keep_tool_rounds=1,
            min_demote_chars=500,
        )

        # Only Round 2 (read_doc) should be demoted; Rounds 0 and 1 were errors!
        self.assertEqual(demoted_count, 1)
        self.assertEqual(compacted[1]["content"], messages[1]["content"])  # Round 0 untouched
        self.assertEqual(compacted[3]["content"], messages[3]["content"])  # Round 1 untouched
        self.assertTrue(is_demoted_stub(compacted[5]["content"]))          # Round 2 demoted
        self.assertEqual(compacted[7]["content"], messages[7]["content"])  # Round 3 protected tail

    def test_atomic_boundary_alignment_forward(self):
        """Test forward boundary alignment over parallel tool calls and XML calls."""
        # Parallel tool calls: 1 assistant, 3 tool results
        messages = [
            {"role": "user", "content": "Fetch multiple assets"},                       # 0
            {                                                                            # 1
                "role": "assistant",
                "content": "Running parallel queries",
                "tool_calls": [
                    {"id": "call_a", "function": {"name": "query_a"}},
                    {"id": "call_b", "function": {"name": "query_b"}},
                    {"id": "call_c", "function": {"name": "query_c"}},
                ],
            },
            {"role": "tool", "tool_call_id": "call_a", "content": "res_a"},             # 2
            {"role": "tool", "tool_call_id": "call_b", "content": "res_b"},             # 3
            {"role": "tool", "tool_call_id": "call_c", "content": "res_c"},             # 4
            {"role": "assistant", "content": "Got all responses"},                       # 5
            {"role": "user", "content": "Great, continue"},                              # 6
        ]

        groups = find_tool_call_groups(messages)
        self.assertEqual(groups, [(1, 4)])

        # Cutting at index 1 (before assistant) keeps the whole group in suffix
        self.assertEqual(align_boundary_forward(messages, 1), 1)

        # Cutting at index 2, 3, or 4 must advance to index 5 (after all tool results)
        self.assertEqual(align_boundary_forward(messages, 2), 5)
        self.assertEqual(align_boundary_forward(messages, 3), 5)
        self.assertEqual(align_boundary_forward(messages, 4), 5)

        # Cutting at index 5 or 6 is already clean
        self.assertEqual(align_boundary_forward(messages, 5), 5)
        self.assertEqual(align_boundary_forward(messages, 6), 6)

    def test_atomic_boundary_alignment_backward(self):
        """Test backward boundary alignment over parallel tool calls and XML calls."""
        messages = [
            {"role": "user", "content": "Fetch multiple assets"},                       # 0
            {                                                                            # 1
                "role": "assistant",
                "content": "Running parallel queries",
                "tool_calls": [
                    {"id": "call_a", "function": {"name": "query_a"}},
                    {"id": "call_b", "function": {"name": "query_b"}},
                    {"id": "call_c", "function": {"name": "query_c"}},
                ],
            },
            {"role": "tool", "tool_call_id": "call_a", "content": "res_a"},             # 2
            {"role": "tool", "tool_call_id": "call_b", "content": "res_b"},             # 3
            {"role": "tool", "tool_call_id": "call_c", "content": "res_c"},             # 4
            {"role": "assistant", "content": "Got all responses"},                       # 5
        ]

        # Cutting at index 1 is already at the start of the group
        self.assertEqual(align_boundary_backward(messages, 1), 1)

        # Cutting at index 2, 3, or 4 must retreat to index 1 (start of the assistant call)
        self.assertEqual(align_boundary_backward(messages, 2), 1)
        self.assertEqual(align_boundary_backward(messages, 3), 1)
        self.assertEqual(align_boundary_backward(messages, 4), 1)

        # Cutting at index 5 is after the group
        self.assertEqual(align_boundary_backward(messages, 5), 5)

    def test_validate_atomic_boundaries(self):
        """Test validation of well-formed vs severed tool calling boundaries."""
        # 1. Well-formed structured messages
        valid_structured = [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": "calling tool",
                "tool_calls": [{"id": "c1", "function": {"name": "test"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
            {"role": "assistant", "content": "done"},
        ]
        self.assertTrue(validate_atomic_boundaries(valid_structured))

        # 2. Well-formed XML messages
        valid_xml = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "<tool_call name='test'>args</tool_call>"},
            {"role": "user", "content": "<observation tool='test'>output</observation>"},
            {"role": "assistant", "content": "done"},
        ]
        self.assertTrue(validate_atomic_boundaries(valid_xml))

        # 3. Orphaned tool message at start
        self.assertFalse(validate_atomic_boundaries([{"role": "tool", "content": "orphan"}]))

        # 4. Orphaned tool message after user
        self.assertFalse(
            validate_atomic_boundaries(
                [
                    {"role": "user", "content": "hi"},
                    {"role": "tool", "tool_call_id": "c1", "content": "orphan"},
                ]
            )
        )

        # 5. Assistant with tool_calls at the end of messages (severed)
        self.assertFalse(
            validate_atomic_boundaries(
                [
                    {"role": "user", "content": "hi"},
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{"id": "c1", "function": {"name": "test"}}],
                    },
                ]
            )
        )

        # 6. Assistant tool calls severed by subsequent user message
        self.assertFalse(
            validate_atomic_boundaries(
                [
                    {"role": "user", "content": "hi"},
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{"id": "c1", "function": {"name": "test"}}],
                    },
                    {"role": "user", "content": "interrupting user message"},
                ]
            )
        )

        # 7. Incomplete parallel tool calls (missing c2)
        self.assertFalse(
            validate_atomic_boundaries(
                [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {"id": "c1", "function": {"name": "t1"}},
                            {"id": "c2", "function": {"name": "t2"}},
                        ],
                    },
                    {"role": "tool", "tool_call_id": "c1", "content": "ok1"},
                    {"role": "user", "content": "next"},
                ]
            )
        )

        # 8. Orphaned XML observation
        self.assertFalse(
            validate_atomic_boundaries(
                [
                    {"role": "user", "content": "<observation tool='t'>orphan</observation>"},
                ]
            )
        )

    def test_sanitize_tool_pairs(self):
        """Verify sanitize_tool_pairs removes orphan tool results and unresolved tool_calls."""
        from harness_core.context_compressor import sanitize_tool_pairs

        dirty_messages = [
            # 1. Orphan tool result at very beginning (should be dropped)
            {"role": "tool", "tool_call_id": "stray_0", "content": "stray"},
            # 2. Assistant with 2 calls, but only c1 responded
            {
                "role": "assistant",
                "content": "Running tools",
                "tool_calls": [
                    {"id": "c1", "function": {"name": "tool_1"}},
                    {"id": "c2", "function": {"name": "tool_2"}},
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "result 1"},
            # 3. Orphan tool result for non-existent call c99
            {"role": "tool", "tool_call_id": "c99", "content": "stray result"},
            # 4. Normal user message
            {"role": "user", "content": "continue"},
        ]

        cleaned = sanitize_tool_pairs(dirty_messages)
        # Should drop stray_0 and c99, and remove c2 from tool_calls
        self.assertEqual(len(cleaned), 3)
        self.assertEqual(cleaned[0]["role"], "assistant")
        self.assertEqual(len(cleaned[0]["tool_calls"]), 1)
        self.assertEqual(cleaned[0]["tool_calls"][0]["id"], "c1")
        self.assertEqual(cleaned[1]["role"], "tool")
        self.assertEqual(cleaned[1]["tool_call_id"], "c1")
        self.assertEqual(cleaned[2]["role"], "user")
        self.assertTrue(validate_atomic_boundaries(cleaned))

    def test_strip_historical_media(self):
        """Verify strip_historical_media replaces base64 images before tail_start."""
        from harness_core.context_compressor import strip_historical_media

        messages = [
            {"role": "user", "content": "inspect screen"},
            # Historical image
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Screenshot 1:"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="}},
                ],
            },
            {"role": "assistant", "content": "I see the button"},
            # Recent tail image (should remain untouched)
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"data": "recent_base64_blob"}},
                ],
            },
        ]

        stripped, count = strip_historical_media(messages, tail_start=3)
        self.assertEqual(count, 1)
        # First image replaced with text placeholder
        self.assertEqual(stripped[1]["content"][1]["type"], "text")
        self.assertIn("historical image omitted", stripped[1]["content"][1]["text"])
        # Tail image untouched
        self.assertEqual(stripped[3]["content"][0]["type"], "image")


if __name__ == "__main__":
    unittest.main()
