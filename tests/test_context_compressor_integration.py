"""Integration tests for 4-Phase Context Compactor & Prompt Cache Locking.

Validates end-to-end integration across ReActLoop, TwoTierModelRouter,
HarnessOrchestrator, and HexConsole TUI.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock

from harness_core.context_compressor import (
    CompactionConfig,
    CompactionResult,
    ContextCompactor,
    _INFLIGHT_TASK_REPLAY_HEADER,
    apply_cache_control_breakpoints,
    compute_cache_signature,
    estimate_messages_tokens,
    is_context_summary_message,
    stabilize_prompt_cache_prefix,
    validate_atomic_boundaries,
)
from harness_core.react_loop import ReActConfig, ReActLoop
from harness_core.router import TwoTierModelRouter
from harness_core.tui import HexConsole


class TestContextCompressorIntegration(unittest.TestCase):
    """End-to-end integration test suite for Phase 26 Context Compactor."""

    def test_react_loop_automatic_compaction(self) -> None:
        """Verify ReActLoop automatically triggers compaction when approaching budget."""
        turn_count = 0

        def mock_model_caller(messages: list[dict]) -> str:
            nonlocal turn_count
            turn_count += 1
            if turn_count < 4:
                # Issue tool call that generates lengthy output
                return '<thought>Inspecting file</thought>\n<tool_call>{"name": "read_data", "arguments": {}}</tool_call>'
            return "<finish>Task completed after multiple tool rounds</finish>"

        gateway = MagicMock()
        # Return bulky output over 1200 chars to fill tokens
        gateway.tools = {"read_data": lambda args: {"status": "ok", "data": "X" * 1500}}

        cfg = ReActConfig(
            max_turns=6,
            budget_tokens=400,  # Small budget so threshold 320 tokens is exceeded
            system_prompt="You are HEX ReAct Agent.",
        )

        loop = ReActLoop(
            model_caller=mock_model_caller,
            gateway=gateway,
            config=cfg,
        )

        res = loop.run(prompt="Analyze large data stream")

        self.assertTrue(res.success)
        self.assertIn("Task completed", res.final_answer)
        # Verify compaction occurred during the multi-turn session
        self.assertIsNotNone(loop.last_compaction_result)
        self.assertTrue(loop.last_compaction_result.compacted_tokens < loop.last_compaction_result.original_tokens)

    def test_prompt_cache_prefix_stability_across_continuous_turns(self) -> None:
        """Verify prompt cache SHA-256 signature remains 100% constant across 10 turns."""
        system_prompt = "You are HEX Autonomous AI Workbench. Strictly adhere to coding standards."
        tools = [
            {"name": "read_file", "description": "Read file contents", "parameters": {"type": "object"}},
            {"name": "write_file", "description": "Write file contents", "parameters": {"type": "object"}},
        ]

        messages = [
            {"role": "user", "content": "Initial user goal: build web app"},
            {"role": "assistant", "content": "I will examine the project structure."},
        ]

        # Base stabilized prefix
        base_messages = stabilize_prompt_cache_prefix(messages, system_prompt, tools)
        initial_sig = compute_cache_signature(base_messages, tools, prefix_turns=2)

        # Simulate 10 continuous conversational turns
        current_history = list(messages)
        for i in range(1, 11):
            current_history.append({"role": "user", "content": f"Turn {i}: follow-up instruction and detail"})
            current_history.append({"role": "assistant", "content": f"Turn {i}: executed action and observed outcome."})

            stabilized = stabilize_prompt_cache_prefix(current_history, system_prompt, tools)
            turn_sig = compute_cache_signature(stabilized, tools, prefix_turns=2)

            self.assertEqual(
                turn_sig,
                initial_sig,
                f"Cache prefix signature drifted at turn {i}!",
            )

        # Verify Anthropic ephemeral cache control breakpoints
        tagged_msgs, tagged_tools = apply_cache_control_breakpoints(
            stabilized, tools, provider="anthropic"
        )
        self.assertIsNotNone(tagged_tools)
        self.assertEqual(tagged_tools[-1].get("cache_control"), {"type": "ephemeral"})
        self.assertEqual(tagged_msgs[0].get("cache_control"), {"type": "ephemeral"})

    def test_full_four_phase_compaction_pipeline(self) -> None:
        """Verify full 4-phase compaction pipeline preserves atomic boundaries and reduces tokens."""
        # Build 15-turn conversation:
        # Head (system + 1 user prompt)
        # Middle (8 turns of tool calls and bulky tool results)
        # Tail (6 turns of recent tools and followups)
        messages: list[dict] = [
            {"role": "system", "content": "You are HEX Autonomous AI Workbench."},
            {"role": "user", "content": "Turn 0: Inspect system environment and dependencies."},
        ]

        # Middle bulky tool rounds
        for i in range(1, 9):
            messages.append({
                "role": "assistant",
                "content": f"<thought>Querying logs round {i}</thought>\n<tool_call>{{\"name\": \"run_tests\", \"arguments\": {{\"round\": {i}}}}}</tool_call>",
            })
            messages.append({
                "role": "user",
                "content": f"<observation tool=\"run_tests\">\nRound {i} output: {'A' * 1400}\n</observation>",
            })

        # Tail recent turns
        for i in range(9, 13):
            messages.append({
                "role": "assistant",
                "content": f"<thought>Active turn {i}</thought>\n<tool_call>{{\"name\": \"edit_file\", \"arguments\": {{\"path\": \"src/mod{i}.py\"}}}}</tool_call>",
            })
            messages.append({
                "role": "user",
                "content": f"<observation tool=\"edit_file\">\nFile src/mod{i}.py updated successfully.\n</observation>",
            })
        messages.append({"role": "user", "content": "Final verify: run tests on latest changes."})

        orig_tokens = estimate_messages_tokens(messages)
        compactor = ContextCompactor(
            CompactionConfig(max_context_tokens=2000, trigger_threshold=0.5)
        )

        res = compactor.compact_messages(messages, force=True)

        # 1. Stale tools demoted
        self.assertGreater(res.phase1_demoted_tools, 0)

        # 2. Middle turns summarized
        self.assertGreater(res.phase3_summarized_turns, 0)
        self.assertTrue(res.used_fallback)  # Deterministic fallback used since no model_caller passed

        # 3. Tokens significantly reduced
        self.assertLess(res.compacted_tokens, orig_tokens)
        self.assertLess(res.compression_ratio, 0.7)

        # 4. Atomic tool boundaries intact
        self.assertTrue(validate_atomic_boundaries(res.compacted_messages))

        # 5. Summary marker present
        summary_found = any(is_context_summary_message(m) for m in res.compacted_messages)
        self.assertTrue(summary_found)

    def test_inflight_task_restatement_preserves_active_objective(self) -> None:
        """Verify that in-flight user request is restated after summary boundary."""
        messages: list[dict] = [
            {"role": "system", "content": "You are HEX Autonomous AI Workbench."},
            {"role": "user", "content": "Refactor authentication module to support OAuth 2.0 PKCE flow."},
        ]
        # Middle turns where assistant works through multi-turn tools
        for i in range(1, 10):
            messages.append({
                "role": "assistant",
                "content": f"<thought>Step {i}</thought>\n<tool_call>{{\"name\": \"exec_step_{i}\", \"arguments\": {{}}}}</tool_call>",
            })
            messages.append({
                "role": "user",
                "content": f"<observation tool=\"exec_step_{i}\">\nStep {i} executed with data {'D' * 500}\n</observation>",
            })

        # Tail ends on an assistant tool round without a new user message
        messages.append({
            "role": "assistant",
            "content": "<thought>Preparing OAuth validation</thought>\n<tool_call>{\"name\": \"validate\", \"arguments\": {}}</tool_call>",
        })
        messages.append({
            "role": "user",
            "content": "<observation tool=\"validate\">Passed preliminary checks.</observation>",
        })

        compactor = ContextCompactor(CompactionConfig(max_context_tokens=1500, trigger_threshold=0.5))
        res = compactor.compact_messages(messages, force=True)

        # Verify restatement header is injected to preserve intent
        found_replay = any(
            _INFLIGHT_TASK_REPLAY_HEADER in str(m.get("content", ""))
            for m in res.compacted_messages
        )
        self.assertTrue(found_replay, "In-flight task replay header was not restated!")

        # Verify the original objective text was preserved
        found_objective = any(
            "OAuth 2.0 PKCE flow" in str(m.get("content", ""))
            for m in res.compacted_messages
        )
        self.assertTrue(found_objective, "Original user objective was lost during compaction!")

    def test_tui_compress_command_execution(self) -> None:
        """Verify TUI /compress slash command executes manual compaction and prints telemetry."""
        orchestrator = MagicMock()
        console = HexConsole(orchestrator, quiet=True)

        # Populate console messages with lengthy turns
        console.messages = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "Initial user query"},
        ]
        for i in range(12):
            console.messages.append({
                "role": "assistant",
                "content": f"Assistant response {i} explaining complex architecture in great detail " * 15,
            })
            console.messages.append({
                "role": "user",
                "content": f"User follow up {i} asking for more details and implementation steps " * 15,
            })

        orig_len = len(console.messages)
        orig_tokens = estimate_messages_tokens(console.messages)

        # Run /compress command
        console.handle_command("/compress")

        self.assertIsNotNone(console.last_compaction_result)
        self.assertLess(len(console.messages), orig_len)
        self.assertGreater(console.session_tokens_saved, 0)
        self.assertLess(console.session_compression_ratio, 1.0)

    def test_router_context_compactor_integration(self) -> None:
        """Verify TwoTierModelRouter exposes get_context_compactor and compact_context."""
        router = TwoTierModelRouter(compressor_mode="local")
        compactor = router.get_context_compactor(max_context_tokens=1000)
        self.assertIsInstance(compactor, ContextCompactor)

        # Test compact_context
        msgs = [
            {"role": "system", "content": "Sys prompt"},
        ] + [
            {"role": "user", "content": f"Turn {i}: detailed task payload " * 20}
            for i in range(12)
        ]
        compacted_msgs, res = router.compact_context(msgs, budget_tokens=800)
        self.assertIsInstance(res, CompactionResult)
        self.assertLess(res.compacted_tokens, res.original_tokens)
        self.assertEqual(compacted_msgs, res.compacted_messages)


if __name__ == "__main__":
    unittest.main()
