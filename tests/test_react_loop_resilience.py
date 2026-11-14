"""Unit and resilience tests for ReActLoop native tool schema and transient retry backoff,

and CloudModelProvider Anthropic thinking block signature preservation and suppression.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock
import urllib.error

from harness_core.gateway import RiskClass, ToolGateway, ToolSpec
from harness_core.providers.base import ProviderUnavailableError
from harness_core.providers.cloud_provider import CloudModelProvider, CloudProviderType
from harness_core.react_loop import ReActConfig, ReActLoop, ToolCallRequest


class TestReActLoopResilience(unittest.TestCase):
    """Test suite for ReAct loop transient retry resilience and native tool formatting."""

    def test_tool_result_native_format(self) -> None:
        """Verify ReActLoop execution appends messages with role: 'tool', tool_call_id, name, and valid content."""
        turn = 0

        def mock_model(messages: list[dict]) -> str:
            nonlocal turn
            if turn == 0:
                turn += 1
                return '<thought>Need to read status</thought>\n<tool_call>{"name": "get_status", "arguments": {}}</tool_call>'
            return "<finish>Status verified</finish>"

        mock_gateway = MagicMock()
        mock_gateway.tools = {
            "get_status": ToolSpec(
                tool_id="get_status",
                description="Get status",
                risk_class=RiskClass.R0,
                handler=lambda ws, args: {"status": "healthy", "code": 200},
            )
        }

        loop = ReActLoop(
            model_caller=mock_model,
            gateway=mock_gateway,
            config=ReActConfig(max_turns=3),
        )

        session = loop.run("Check system health")
        self.assertTrue(session.success)
        self.assertEqual(session.termination_reason, "finish")

        # Verify messages history has native role: "tool" message
        tool_messages = [m for m in loop.messages if m.get("role") == "tool"]
        self.assertEqual(len(tool_messages), 1)

        tool_msg = tool_messages[0]
        self.assertEqual(tool_msg["role"], "tool")
        self.assertEqual(tool_msg["name"], "get_status")
        self.assertTrue(tool_msg.get("tool_call_id"))
        self.assertIn('"status": "healthy"', tool_msg["content"])
        self.assertIn('<observation tool="get_status">', tool_msg["observation"])

    def test_transient_retry_429_success(self) -> None:
        """Mock caller raising HTTP 429 on attempts 1 and 2, then returning <finish>Done</finish> on attempt 3.

        Verify session succeeds and retry count is 2.
        """
        attempts = 0

        def failing_model(messages: list[dict]) -> str:
            nonlocal attempts
            attempts += 1
            if attempts <= 2:
                raise urllib.error.HTTPError(
                    url="https://api.anthropic.com/v1/messages",
                    code=429,
                    msg="Too Many Requests",
                    hdrs={},
                    fp=io.BytesIO(b"Rate limit exceeded"),
                )
            return "<finish>Done</finish>"

        fast_config = ReActConfig(
            max_turns=3,
            max_retries=3,
            initial_retry_delay=0.001,
            backoff_factor=1.5,
            retry_jitter=0.001,
        )

        loop = ReActLoop(
            model_caller=failing_model,
            config=fast_config,
        )

        session = loop.run("Perform operation")
        self.assertTrue(session.success)
        self.assertEqual(session.final_answer, "Done")
        self.assertEqual(session.retry_count, 2)
        self.assertEqual(attempts, 3)

    def test_transient_retry_503_and_timeout(self) -> None:
        """Mock caller raising HTTP 503 and TimeoutError before succeeding."""
        attempts = 0

        def flaky_model(messages: list[dict]) -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise urllib.error.HTTPError(
                    url="https://api.openai.com/v1/chat/completions",
                    code=503,
                    msg="Service Unavailable",
                    hdrs={},
                    fp=io.BytesIO(b"Service Unavailable"),
                )
            if attempts == 2:
                raise TimeoutError("Model upstream connection timed out after 30s")
            return "<finish>Recovered from outage</finish>"

        fast_config = ReActConfig(
            max_turns=3,
            max_retries=3,
            initial_retry_delay=0.001,
            backoff_factor=1.5,
            retry_jitter=0.001,
        )

        loop = ReActLoop(
            model_caller=flaky_model,
            config=fast_config,
        )

        session = loop.run("Recover from downtime")
        self.assertTrue(session.success)
        self.assertEqual(session.final_answer, "Recovered from outage")
        self.assertEqual(session.retry_count, 2)
        self.assertEqual(attempts, 3)

    def test_retry_exhaustion_reports_error(self) -> None:
        """Mock caller failing with 429 continuously. Verify loop terminates with termination_reason='error', success=False."""
        attempts = 0

        def perpetually_throttled(messages: list[dict]) -> str:
            nonlocal attempts
            attempts += 1
            raise ProviderUnavailableError("Cloud provider 'anthropic' returned HTTP 429: Rate limit exceeded")

        fast_config = ReActConfig(
            max_turns=3,
            max_retries=3,
            initial_retry_delay=0.001,
            backoff_factor=1.5,
            retry_jitter=0.001,
        )

        loop = ReActLoop(
            model_caller=perpetually_throttled,
            config=fast_config,
        )

        session = loop.run("Exhaust retries")
        self.assertFalse(session.success)
        self.assertEqual(session.termination_reason, "error")
        self.assertIn("Rate limit exceeded", session.final_answer)
        # Initial attempt + 3 retries = 4 calls total
        self.assertEqual(attempts, 4)
        self.assertEqual(session.retry_count, 3)

    def test_non_transient_no_retry(self) -> None:
        """Mock caller raising ValueError or HTTP 400. Verify caller was called exactly once with no retries."""
        # Case A: ValueError
        attempts_a = 0

        def bad_value_model(messages: list[dict]) -> str:
            nonlocal attempts_a
            attempts_a += 1
            raise ValueError("Malformed prompt arguments")

        fast_config = ReActConfig(
            max_turns=3,
            max_retries=3,
            initial_retry_delay=0.001,
            backoff_factor=1.5,
            retry_jitter=0.001,
        )

        loop = ReActLoop(model_caller=bad_value_model, config=fast_config)
        session = loop.run("Trigger value error")
        self.assertFalse(session.success)
        self.assertEqual(session.termination_reason, "error")
        self.assertEqual(attempts_a, 1)
        self.assertEqual(session.retry_count, 0)

        # Case B: HTTP 400 Bad Request
        attempts_b = 0

        def bad_request_model(messages: list[dict]) -> str:
            nonlocal attempts_b
            attempts_b += 1
            raise urllib.error.HTTPError(
                url="https://api.anthropic.com/v1/messages",
                code=400,
                msg="Bad Request: Invalid parameter",
                hdrs={},
                fp=io.BytesIO(b"Bad Request"),
            )

        loop_b = ReActLoop(model_caller=bad_request_model, config=fast_config)
        session_b = loop_b.run("Trigger bad request")
        self.assertFalse(session_b.success)
        self.assertEqual(session_b.termination_reason, "error")
        self.assertEqual(attempts_b, 1)
        self.assertEqual(session_b.retry_count, 0)


class TestAnthropicThinkingAndToolTranslation(unittest.TestCase):
    """Test suite for Anthropic thinking block preservation/suppression and tool_result translation."""

    def setUp(self) -> None:
        self.provider = CloudModelProvider(
            provider_type=CloudProviderType.ANTHROPIC,
            model_id="claude-3-7-sonnet-20250219",
            api_key="mock-anthropic-key",
        )

    def test_anthropic_thinking_signature_preserved(self) -> None:
        """Verify _build_request preserves valid thinking blocks with signatures when formatting messages for Anthropic."""
        valid_sig = "EJ48fk_valid_cryptographic_signature_Anthropic_frontier_2025"
        messages = [
            {"role": "user", "content": "Analyze algorithm complexity"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "Analyzing time complexity using master theorem...",
                        "signature": valid_sig,
                    },
                    {
                        "type": "text",
                        "text": "The time complexity is O(N log N).",
                    },
                ],
            },
            {"role": "user", "content": "What about space complexity?"},
        ]

        url, headers, body_bytes = self.provider._build_request(
            messages=messages,
            max_tokens=2048,
            temperature=0.0,
            stop_sequences=None,
        )

        payload = json.loads(body_bytes.decode("utf-8"))
        self.assertIn("messages", payload)
        asst_msg = payload["messages"][1]
        self.assertEqual(asst_msg["role"], "assistant")

        content_blocks = asst_msg["content"]
        self.assertEqual(len(content_blocks), 2)

        thinking_block = content_blocks[0]
        self.assertEqual(thinking_block["type"], "thinking")
        self.assertEqual(thinking_block["thinking"], "Analyzing time complexity using master theorem...")
        self.assertEqual(thinking_block["signature"], valid_sig)

        text_block = content_blocks[1]
        self.assertEqual(text_block["type"], "text")
        self.assertEqual(text_block["text"], "The time complexity is O(N log N).")

    def test_anthropic_thinking_signature_suppressed_when_missing(self) -> None:
        """Verify _build_request strips thinking blocks when signature is missing or empty, avoiding HTTP 400 errors."""
        # Test 1: Empty signature string
        messages_empty_sig = [
            {"role": "user", "content": "Hello"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "Unsigned thoughts with empty signature...",
                        "signature": "",
                    },
                    {
                        "type": "text",
                        "text": "Hello there!",
                    },
                ],
            },
            {"role": "user", "content": "Next question"},
        ]

        url, headers, body_bytes = self.provider._build_request(
            messages=messages_empty_sig,
            max_tokens=1024,
            temperature=0.0,
            stop_sequences=None,
        )

        payload = json.loads(body_bytes.decode("utf-8"))
        asst_msg = payload["messages"][1]
        self.assertEqual(asst_msg["role"], "assistant")
        content_blocks = asst_msg["content"]
        # Thinking block must be stripped
        for block in content_blocks:
            if isinstance(block, dict):
                self.assertNotEqual(block.get("type"), "thinking")

        # Test 2: Missing signature field altogether
        messages_no_sig = [
            {"role": "user", "content": "Solve math"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "Corrupted thinking missing signature field entirely",
                    },
                    {
                        "type": "text",
                        "text": "Result is 42",
                    },
                ],
            },
        ]

        _, _, body_bytes_no_sig = self.provider._build_request(
            messages=messages_no_sig,
            max_tokens=1024,
            temperature=0.0,
            stop_sequences=None,
        )

        payload_no_sig = json.loads(body_bytes_no_sig.decode("utf-8"))
        asst_blocks = payload_no_sig["messages"][1]["content"]
        for block in asst_blocks:
            if isinstance(block, dict):
                self.assertNotEqual(block.get("type"), "thinking")

    def test_anthropic_tool_message_translation(self) -> None:
        """Verify role: 'tool' messages are converted to Anthropic tool_result content blocks."""
        messages = [
            {"role": "user", "content": "Compute hash"},
            {"role": "assistant", "content": "Calling hash tool"},
            {
                "role": "tool",
                "tool_call_id": "call_sha256_xyz789",
                "name": "sha256",
                "content": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                "observation": '<observation tool="sha256">e3b0c442...</observation>',
            },
        ]

        url, headers, body_bytes = self.provider._build_request(
            messages=messages,
            max_tokens=1024,
            temperature=0.0,
            stop_sequences=None,
        )

        payload = json.loads(body_bytes.decode("utf-8"))
        self.assertEqual(len(payload["messages"]), 3)
        tool_msg = payload["messages"][2]

        # Anthropic requires tool results wrapped in role: "user" with type: "tool_result"
        self.assertEqual(tool_msg["role"], "user")
        self.assertIsInstance(tool_msg["content"], list)
        self.assertEqual(len(tool_msg["content"]), 1)

        result_block = tool_msg["content"][0]
        self.assertEqual(result_block["type"], "tool_result")
        self.assertEqual(result_block["tool_use_id"], "call_sha256_xyz789")
        self.assertEqual(
            result_block["content"],
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )

    def test_anthropic_parse_response_retains_thinking_signature(self) -> None:
        """Verify _parse_response indexes thinking blocks and cryptographic signature in response raw dictionary."""
        valid_sig = "sig_cryptographic_test_valid_signature_12345"
        raw_data = {
            "id": "msg_01XFDUDY",
            "type": "message",
            "role": "assistant",
            "content": [
                {
                    "type": "thinking",
                    "thinking": "Detailed internal reasoning before answering...",
                    "signature": valid_sig,
                },
                {
                    "type": "text",
                    "text": "Final calibrated output.",
                },
            ],
            "model": "claude-3-7-sonnet-20250219",
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 50,
                "output_tokens": 120,
            },
        }

        text, in_tokens, out_tokens, cached_tokens, finish_reason = self.provider._parse_response(
            raw_data, "Test prompt"
        )

        self.assertEqual(text, "Final calibrated output.")
        self.assertEqual(in_tokens, 50)
        self.assertEqual(out_tokens, 120)
        self.assertEqual(finish_reason, "end_turn")

        # Verify raw_data retains thinking block and signature
        self.assertIn("thinking_blocks", raw_data)
        self.assertEqual(len(raw_data["thinking_blocks"]), 1)
        self.assertEqual(raw_data["thinking_blocks"][0]["signature"], valid_sig)
        self.assertEqual(raw_data["signature"], valid_sig)
        self.assertEqual(raw_data["thinking"], "Detailed internal reasoning before answering...")


if __name__ == "__main__":
    unittest.main()
