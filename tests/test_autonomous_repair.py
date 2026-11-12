"""Autonomous Repair Benchmark and Retry Escalation Test Suite (Phase 05 Plan 01).

Proves:
1. Multi-Turn Autonomous Repair Happy Path:
   - Initial verification failure captured by Tier-1 log compressor.
   - Blast-radius checks and compressed digest used to formulate patch.
   - Patch applied inside isolated CoW attempt workspace.
   - Re-verification passes, Proof-Carrying Artifact generated, and changes synced to source.
2. Retry Exhaustion Escalation:
   - Max retries bounded by task budget (e.g. max_retries_per_step = 2).
   - Forensic CoW snapshot and checkpoint preserved for operator inspection.
   - Task safely escalates to NEEDS_ATTENTION with REPAIR_RETRIES_EXHAUSTED.
   - Base source workspace remains untouched.
3. Multi-Failure Compound Repair:
   - Multiple failing modules repaired across iterative turns until full suite passes.
4. Budget Limit Escalation:
   - Hard cost ceiling triggers immediate escalation to NEEDS_ATTENTION with BUDGET_EXHAUSTED.
5. Injected Runtime BudgetExceededError Escalation:
   - Dynamic budget breach during patch generation freezes state and preserves forensic snapshot.
6. Patch Parser & Prompt Formulator:
   - Verifies resilient parsing of JSON tool actions and python code block fallbacks.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from harness_core.models import TaskBudget, TaskState, TaskSubmitRequest
from harness_core.orchestrator import HarnessOrchestrator
from harness_core.providers.base import BudgetExceededError, ModelResponse, ModelUsage
from harness_core.router import RouteDecision


class TestAutonomousRepairBenchmark(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="harness_repair_test_"))
        self.source_ws = self.tmp_dir / "source_repo"
        self.source_ws.mkdir(parents=True, exist_ok=True)
        self.data_dir = self.tmp_dir / "harness_data"
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        if hasattr(self, "orch") and self.orch is not None:
            self.orch.shutdown()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _setup_calculator_repo(self) -> None:
        """Create a broken calculator implementation and corresponding unit test."""
        (self.source_ws / "calculator.py").write_text(
            "def add(a: int, b: int) -> int:\n"
            "    return a - b  # Intentional bug: minus instead of plus\n",
            encoding="utf-8",
        )
        (self.source_ws / "test_calculator.py").write_text(
            "import unittest\n"
            "from calculator import add\n\n"
            "class TestCalculator(unittest.TestCase):\n"
            "    def test_add(self):\n"
            "        self.assertEqual(add(2, 3), 5)\n\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n",
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Test 1: Multi-Turn Autonomous Repair Happy Path
    # ------------------------------------------------------------------

    def test_multi_turn_autonomous_repair_happy_path(self) -> None:
        self._setup_calculator_repo()

        captured_digests: list[str] = []

        def deterministic_repair_generator(
            task_id: str,
            attempt_ws: Path,
            failure_digest: str,
            blast_report: Any,
            modified_files: list[str],
        ) -> list[dict[str, Any]]:
            captured_digests.append(failure_digest)
            # Fix the bug by replacing calculator.py content
            return [
                {
                    "tool_id": "write_file",
                    "args": {
                        "path": "calculator.py",
                        "content": "def add(a: int, b: int) -> int:\n    return a + b\n",
                    },
                }
            ]

        orch = HarnessOrchestrator(
            data_dir=self.data_dir,
            default_workspace=self.source_ws,
            repair_patch_generator=deterministic_repair_generator,
        )

        req = TaskSubmitRequest(
            goal="Fix addition calculation bug in calculator.py",
            execution_mode="standard",
            workspace_root=str(self.source_ws),
            budget={"max_retries_per_step": 2, "max_cost_usd": 2.0},
            steps=[
                {
                    "step_id": "step_1_inspect",
                    "tool_id": "read_file",
                    "args": {"path": "calculator.py"},
                    "risk_class": "R0",
                }
            ],
        )

        task = orch.submit_and_run(req)
        self.assertEqual(task["status"], TaskState.SUCCEEDED.value)

        # 1. Verify failure digest captured assertion error and file
        self.assertEqual(len(captured_digests), 1)
        digest = captured_digests[0]
        self.assertIn("AssertionError", digest)
        self.assertIn("test_calculator.py", digest)

        # 2. Verify state transition event chain in ledger
        events = orch.ledger.list_events(task["task_id"])
        event_types = [e["event_type"] for e in events]
        reason_codes = [e["reason_code"] for e in events]

        self.assertIn("STATE_VERIFYING", event_types)
        self.assertIn("AUTONOMOUS_REPAIR_ATTEMPT", reason_codes)
        self.assertIn("REPAIR_VERIFICATION_ATTEMPT", reason_codes)
        self.assertIn("PROOF_VERIFIED", reason_codes)

        # 3. Verify final payload indicates 1 successful repair attempt
        latest_task = orch.ledger.get_task(task["task_id"])
        self.assertIsNotNone(latest_task)
        self.assertEqual(latest_task["status"], TaskState.SUCCEEDED.value)

        # 4. Verify base workspace was synchronized with the working fix
        fixed_content = (self.source_ws / "calculator.py").read_text(encoding="utf-8")
        self.assertIn("return a + b", fixed_content)
        self.assertNotIn("return a - b", fixed_content)

        # 5. Verify Proof-Carrying Artifact bundle in CAS
        latest_proof = orch.ledger.get_proof_bundle(task["task_id"])
        self.assertIsNotNone(latest_proof)
        self.assertTrue(latest_proof["verification_results"]["passed"])
        self.assertIn("+    return a + b", latest_proof["git_diff_text"])

        cas_file = orch.proof_service.cas_dir / f"{latest_proof['proof_id']}.json"
        self.assertTrue(cas_file.is_file(), "Proof bundle must be saved to CAS")

    # ------------------------------------------------------------------
    # Test 2: Retry Exhaustion Escalation to NEEDS_ATTENTION
    # ------------------------------------------------------------------

    def test_retry_exhaustion_escalates_to_needs_attention(self) -> None:
        self._setup_calculator_repo()
        initial_base_code = (self.source_ws / "calculator.py").read_text(encoding="utf-8")

        repair_call_count = 0

        def non_working_repair_generator(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            nonlocal repair_call_count
            repair_call_count += 1
            # Return an invalid patch that still fails tests
            return [
                {
                    "tool_id": "write_file",
                    "args": {
                        "path": "calculator.py",
                        "content": f"def add(a: int, b: int) -> int:\n    return a * b  # Attempt {repair_call_count} still wrong\n",
                    },
                }
            ]

        orch = HarnessOrchestrator(
            data_dir=self.data_dir,
            default_workspace=self.source_ws,
            repair_patch_generator=non_working_repair_generator,
        )

        req = TaskSubmitRequest(
            goal="Attempt repair with invalid fixes",
            execution_mode="standard",
            workspace_root=str(self.source_ws),
            budget={"max_retries_per_step": 2, "max_cost_usd": 2.0},
            steps=[
                {
                    "step_id": "step_1_inspect",
                    "tool_id": "read_file",
                    "args": {"path": "calculator.py"},
                    "risk_class": "R0",
                }
            ],
        )

        task = orch.submit_and_run(req)

        # 1. Verify task transitioned to NEEDS_ATTENTION
        self.assertEqual(task["status"], TaskState.NEEDS_ATTENTION.value)
        self.assertEqual(repair_call_count, 2, "Must execute exactly max_retries_per_step (2)")

        # 2. Verify reason code and terminal reason
        latest_task = orch.ledger.get_task(task["task_id"])
        self.assertIsNotNone(latest_task)
        self.assertEqual(latest_task["status"], TaskState.NEEDS_ATTENTION.value)
        self.assertIn("REPAIR_RETRIES_EXHAUSTED", latest_task["terminal_reason"])

        # 3. Verify forensic CoW snapshot path exists on disk
        events = orch.ledger.list_events(task["task_id"])
        attention_event = [e for e in events if e["next_state"] == TaskState.NEEDS_ATTENTION.value][-1]
        payload = attention_event["payload"]

        self.assertEqual(payload["repair_attempts"], 2)
        self.assertEqual(payload["max_retries"], 2)
        forensic_path = Path(payload["forensic_snapshot_path"])
        self.assertTrue(forensic_path.is_dir(), "Forensic CoW snapshot directory must be preserved")
        self.assertTrue((forensic_path / "calculator.py").is_file())

        # 4. Verify base workspace remained completely untouched and uncorrupted
        current_base_code = (self.source_ws / "calculator.py").read_text(encoding="utf-8")
        self.assertEqual(initial_base_code, current_base_code)

    # ------------------------------------------------------------------
    # Test 3: Multi-Failure Compound Repair Across Multiple Turns
    # ------------------------------------------------------------------

    def test_multi_failure_compound_repair(self) -> None:
        # Create two broken files and tests
        (self.source_ws / "math_ops.py").write_text(
            "def multiply(a: int, b: int) -> int:\n"
            "    return a + b  # Bug: addition instead of multiplication\n",
            encoding="utf-8",
        )
        (self.source_ws / "test_math_ops.py").write_text(
            "import unittest\n"
            "from math_ops import multiply\n\n"
            "class TestMath(unittest.TestCase):\n"
            "    def test_multiply(self):\n"
            "        self.assertEqual(multiply(2, 3), 6)\n\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n",
            encoding="utf-8",
        )
        (self.source_ws / "string_ops.py").write_text(
            "def shout(s: str) -> str:\n"
            "    return s.lower()  # Bug: lower instead of upper\n",
            encoding="utf-8",
        )
        (self.source_ws / "test_string_ops.py").write_text(
            "import unittest\n"
            "from string_ops import shout\n\n"
            "class TestString(unittest.TestCase):\n"
            "    def test_shout(self):\n"
            "        self.assertEqual(shout('hello'), 'HELLO')\n\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n",
            encoding="utf-8",
        )

        turn_count = 0

        def multi_turn_repair_generator(
            task_id: str,
            attempt_ws: Path,
            failure_digest: str,
            blast_report: Any,
            modified_files: list[str],
        ) -> list[dict[str, Any]]:
            nonlocal turn_count
            turn_count += 1
            # Turn 1: Fix math_ops.py
            if turn_count == 1:
                return [
                    {
                        "tool_id": "write_file",
                        "args": {
                            "path": "math_ops.py",
                            "content": "def multiply(a: int, b: int) -> int:\n    return a * b\n",
                        },
                    }
                ]
            # Turn 2: Fix string_ops.py
            return [
                {
                    "tool_id": "write_file",
                    "args": {
                        "path": "string_ops.py",
                        "content": "def shout(s: str) -> str:\n    return s.upper()\n",
                    },
                }
            ]

        orch = HarnessOrchestrator(
            data_dir=self.data_dir,
            default_workspace=self.source_ws,
            repair_patch_generator=multi_turn_repair_generator,
        )

        req = TaskSubmitRequest(
            goal="Fix compound defects across multiple modules",
            execution_mode="standard",
            workspace_root=str(self.source_ws),
            budget={"max_retries_per_step": 3, "max_cost_usd": 2.0},
            steps=[
                {
                    "step_id": "step_1_init_math",
                    "tool_id": "write_file",
                    "args": {
                        "path": "math_ops.py",
                        "content": "def multiply(a: int, b: int) -> int:\n    return a + b  # Bug: addition instead of multiplication\n",
                    },
                    "risk_class": "R2",
                },
                {
                    "step_id": "step_2_init_string",
                    "tool_id": "write_file",
                    "args": {
                        "path": "string_ops.py",
                        "content": "def shout(s: str) -> str:\n    return s.lower()  # Bug: lower instead of upper\n",
                    },
                    "risk_class": "R2",
                },
            ],
        )

        task = orch.submit_and_run(req)
        self.assertEqual(task["status"], TaskState.SUCCEEDED.value)
        self.assertEqual(turn_count, 2, "Compound repair should complete in 2 turns")

        # Verify both fixes synced to base workspace
        self.assertIn("return a * b", (self.source_ws / "math_ops.py").read_text(encoding="utf-8"))
        self.assertIn("return s.upper()", (self.source_ws / "string_ops.py").read_text(encoding="utf-8"))

    # ------------------------------------------------------------------
    # Test 4: Hard Budget Limit Escalation
    # ------------------------------------------------------------------

    def test_budget_limit_escalation_to_needs_attention(self) -> None:
        self._setup_calculator_repo()
        initial_base_code = (self.source_ws / "calculator.py").read_text(encoding="utf-8")

        generator_called = False

        def counting_repair_generator(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            nonlocal generator_called
            generator_called = True
            return []

        orch = HarnessOrchestrator(
            data_dir=self.data_dir,
            default_workspace=self.source_ws,
            repair_patch_generator=counting_repair_generator,
        )

        # Set ultra-low budget ceiling (0.0001 USD)
        req = TaskSubmitRequest(
            goal="Test hard cost ceiling breach",
            execution_mode="standard",
            workspace_root=str(self.source_ws),
            budget={"max_cost_usd": 0.0001, "max_retries_per_step": 3},
            steps=[
                {
                    "step_id": "step_1_inspect",
                    "tool_id": "read_file",
                    "args": {"path": "calculator.py"},
                    "risk_class": "R0",
                }
            ],
        )

        task = orch.submit_and_run(req)

        # 1. Verify task transitioned to NEEDS_ATTENTION with BUDGET_EXHAUSTED
        self.assertEqual(task["status"], TaskState.NEEDS_ATTENTION.value)
        self.assertFalse(generator_called, "Must not execute repair generator when budget is exhausted")

        events = orch.ledger.list_events(task["task_id"])
        attention_event = [e for e in events if e["next_state"] == TaskState.NEEDS_ATTENTION.value][-1]
        self.assertEqual(attention_event["reason_code"], "BUDGET_EXHAUSTED")

        # 2. Verify forensic snapshot created
        forensic_path = Path(attention_event["payload"]["forensic_snapshot_path"])
        self.assertTrue(forensic_path.is_dir())

        # 3. Base workspace untouched
        self.assertEqual(initial_base_code, (self.source_ws / "calculator.py").read_text(encoding="utf-8"))

    # ------------------------------------------------------------------
    # Test 5: Dynamic BudgetExceededError Injected During Patch Generation
    # ------------------------------------------------------------------

    def test_injected_budget_exceeded_error_escalation(self) -> None:
        self._setup_calculator_repo()

        def raising_repair_generator(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            raise BudgetExceededError("Simulated token count limit breach")

        orch = HarnessOrchestrator(
            data_dir=self.data_dir,
            default_workspace=self.source_ws,
            repair_patch_generator=raising_repair_generator,
        )

        req = TaskSubmitRequest(
            goal="Test dynamic budget error during repair",
            execution_mode="standard",
            workspace_root=str(self.source_ws),
            budget={"max_retries_per_step": 2, "max_cost_usd": 2.0},
            steps=[
                {
                    "step_id": "step_1_inspect",
                    "tool_id": "read_file",
                    "args": {"path": "calculator.py"},
                    "risk_class": "R0",
                }
            ],
        )

        task = orch.submit_and_run(req)
        self.assertEqual(task["status"], TaskState.NEEDS_ATTENTION.value)

        events = orch.ledger.list_events(task["task_id"])
        attention_event = [e for e in events if e["next_state"] == TaskState.NEEDS_ATTENTION.value][-1]
        self.assertEqual(attention_event["reason_code"], "BUDGET_EXHAUSTED")
        self.assertEqual(attention_event["payload"]["repair_attempts"], 1)

    # ------------------------------------------------------------------
    # Test 6: Default Router Patch Generation & Resilient Parsing
    # ------------------------------------------------------------------

    def test_router_patch_generation_and_fallback_parsing(self) -> None:
        self._setup_calculator_repo()

        orch = HarnessOrchestrator(
            data_dir=self.data_dir,
            default_workspace=self.source_ws,
        )

        # 1. Test markdown JSON parsing
        json_resp = (
            "Here is the fix:\n```json\n"
            '[{"tool_id": "write_file", "args": {"path": "calculator.py", "content": "def add(a, b): return a + b\\n"}}]\n'
            "```\nEnjoy!"
        )
        ops = orch._parse_patch_operations(json_resp, ["calculator.py"])
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["tool_id"], "write_file")
        self.assertIn("return a + b", ops[0]["args"]["content"])

        # 2. Test markdown python code block fallback parsing
        py_resp = "Here is the corrected python file:\n```python\ndef add(a: int, b: int) -> int:\n    return a + b\n```"
        fallback_ops = orch._parse_patch_operations(py_resp, ["calculator.py"])
        self.assertEqual(len(fallback_ops), 1)
        self.assertEqual(fallback_ops[0]["tool_id"], "write_file")
        self.assertEqual(fallback_ops[0]["args"]["path"], "calculator.py")
        self.assertIn("return a + b", fallback_ops[0]["args"]["content"])


if __name__ == "__main__":
    unittest.main()
