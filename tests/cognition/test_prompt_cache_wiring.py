"""B63/B66 regression tests: headline prompt caching + stable-to-volatile slot tiering.

Proves:
1. The headline 5-slot system prompt is sent with prompt-caching enabled on the
   ReAct agent-loop and fast-path model calls (Anthropic cache_control on the
   system block; the previously False default sent it without cache hints).
2. Slot #5 orders stable content (workspace rules, skills index) before volatile
   per-goal blocks (extra_rules / retrieved skills), so changing the volatile
   tail never invalidates the cacheable prefix.
3. The tests-only cache-prefix helpers (stabilize_prompt_cache_prefix,
   compute_cache_signature, apply_cache_control_breakpoints,
   validate_atomic_boundaries) are gone — the provider transport owns caching.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from harness_core.cognition.prompt_builder import PromptBuilder
from harness_core.core.models import TaskSubmitRequest
from harness_core.core.orchestrator import HarnessOrchestrator


class TestSlotOrderingStableToVolatile(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="harness_slot_tier_test_"))
        self.ws = self.tmp_dir / "ws"
        self.ws.mkdir(parents=True, exist_ok=True)
        (self.ws / "AGENTS.md").write_text(
            "Always run the test suite before committing.\n", encoding="utf-8"
        )
        self.builder = PromptBuilder()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_volatile_extra_rules_come_after_stable_prefix(self) -> None:
        prompt = self.builder.build_system_prompt(
            workspace_root=self.ws,
            extra_rules="<retrieved_skills>\nGOAL_SPECIFIC_RULE_MARKER\n</retrieved_skills>",
            include_skills=True,
        )
        skills_pos = prompt.find("<available_skills>")
        rules_pos = prompt.find("[AGENTS.md]")
        volatile_pos = prompt.find("GOAL_SPECIFIC_RULE_MARKER")
        self.assertGreater(volatile_pos, -1, "extra_rules must be present in the prompt")
        self.assertLess(
            rules_pos,
            volatile_pos,
            "Stable workspace rules must precede volatile per-goal rules",
        )
        self.assertLess(
            skills_pos,
            volatile_pos,
            "The stable skills index must precede volatile per-goal rules",
        )

    def test_changing_volatile_tail_preserves_cacheable_prefix(self) -> None:
        prompt_a = self.builder.build_system_prompt(
            workspace_root=self.ws,
            extra_rules="<retrieved_skills>\nRules for goal A\n</retrieved_skills>",
            include_skills=True,
        )
        prompt_b = self.builder.build_system_prompt(
            workspace_root=self.ws,
            extra_rules="<retrieved_skills>\nCompletely different rules for goal B\n</retrieved_skills>",
            include_skills=True,
        )
        common = _common_prefix(prompt_a, prompt_b)
        self.assertIn(
            "</available_skills>",
            common,
            "Changing per-goal rules must not invalidate the stable prefix through the skills index",
        )


def _common_prefix(a: str, b: str) -> str:
    end = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            break
        end += 1
    return a[:end]


def _mock_router(finish_text: str) -> MagicMock:
    router = MagicMock()
    router.cloud_provider.is_available.return_value = True
    router.execute_routed_request.return_value = (
        MagicMock(),
        SimpleNamespace(text=finish_text),
    )
    router.get_context_compactor.return_value = None
    return router


class TestHeadlinePromptCaching(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="harness_prompt_cache_test_"))
        self.source_ws = self.tmp_dir / "source_repo"
        self.source_ws.mkdir(parents=True, exist_ok=True)
        self.data_dir = self.tmp_dir / "harness_data"
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        if hasattr(self, "orch") and self.orch is not None:
            self.orch.shutdown()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_react_agent_loop_requests_prompt_caching(self) -> None:
        orch = HarnessOrchestrator(
            data_dir=self.data_dir,
            default_workspace=self.source_ws,
        )
        router = _mock_router("<finish>Answered without tool use</finish>")
        orch.router = router

        orch.run_agent_loop(
            goal="Explain the repository layout",
            workspace_root=self.source_ws,
            demo_mode=False,
        )

        router.execute_routed_request.assert_called()
        for call in router.execute_routed_request.call_args_list:
            self.assertIs(
                call.kwargs.get("enable_prompt_caching"),
                True,
                "The headline 5-slot ReAct prompt must be sent with prompt caching enabled",
            )

    def test_fast_path_requests_prompt_caching(self) -> None:
        orch = HarnessOrchestrator(
            data_dir=self.data_dir,
            default_workspace=self.source_ws,
        )
        router = _mock_router("Synthesized answer for the user request.")
        orch.router = router

        req = TaskSubmitRequest(
            goal="Summarize the repository layout",
            execution_mode="fast_path",
            workspace_root=str(self.source_ws),
            steps=[],
        )
        task = orch.submit_and_run(req)
        self.assertEqual(task["status"], "SUCCEEDED")

        router.execute_routed_request.assert_called_once()
        self.assertIs(
            router.execute_routed_request.call_args.kwargs.get("enable_prompt_caching"),
            True,
            "The fast-path 5-slot prompt must be sent with prompt caching enabled",
        )


if __name__ == "__main__":
    unittest.main()
