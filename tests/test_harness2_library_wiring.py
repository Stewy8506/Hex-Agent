"""Unit and integration test suite for Google Research Harness² Experience Library wiring.

Tests:
1. Library initialization and root path management.
2. Experience recording validation, file persistence, tree generation, and index rebuild.
3. Pre-task experience retrieval, formatting, and max_entries truncation.
4. Multi-domain isolation ensuring zero leakage between distinct domains.
5. End-to-end orchestrator flow: task completion triggers experience recording and
   subsequent repair prompts receive injected domain experience.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from harness_core.models import TaskBudget, TaskState, TaskSubmitRequest
from harness_core.orchestrator import HarnessOrchestrator
from harness_core.providers.base import ModelResponse, ModelUsage
from harness_core.router import RouteDecision


class TestHarness2LibraryWiring(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="harness2_lib_test_"))
        self.source_ws = self.tmp_dir / "workspace"
        self.source_ws.mkdir(parents=True, exist_ok=True)
        self.data_dir = self.tmp_dir / "harness_data"
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Test 1: Library Initialization and Path Scoping
    # ------------------------------------------------------------------

    def test_library_initialization_defaults_and_custom_root(self) -> None:
        # Default in-memory profile
        orch_mem = HarnessOrchestrator(":memory:")
        self.assertTrue(orch_mem.library_root.is_dir())
        self.assertEqual(orch_mem.library_root.name, "harness2_library")

        # Custom library root
        custom_root = self.tmp_dir / "custom_lib_store"
        orch_custom = HarnessOrchestrator(
            data_dir=self.data_dir,
            default_workspace=self.source_ws,
            library_root=custom_root,
        )
        self.assertEqual(orch_custom.library_root, custom_root.resolve())
        self.assertTrue(custom_root.is_dir())

        # Domain caching and instantiation
        lib_backend = orch_custom.get_domain_library("backend")
        self.assertEqual(lib_backend.domain, "backend")
        self.assertEqual(lib_backend.dir.name, "backend")

        # Caching returns same instance
        lib_backend_cached = orch_custom.get_domain_library("backend")
        self.assertIs(lib_backend, lib_backend_cached)

        # Blank/None normalizes to "general"
        lib_general = orch_custom.get_domain_library("   ")
        self.assertEqual(lib_general.domain, "general")
        self.assertIs(lib_general, orch_custom.get_domain_library(None))  # type: ignore[arg-type]

    # ------------------------------------------------------------------
    # Test 2: Experience Recording Validation, Persistence & Index Rebuild
    # ------------------------------------------------------------------

    def test_record_task_experience_validation_and_persistence(self) -> None:
        orch = HarnessOrchestrator(
            data_dir=self.data_dir,
            default_workspace=self.source_ws,
        )

        # 1. Invalid payloads rejected
        self.assertIsNone(
            orch.record_task_experience(
                "task-bad-1",
                "backend",
                experience={"cue": "only cue"},  # Missing lesson & evidence
            )
        )
        self.assertIsNone(
            orch.record_task_experience(
                "task-bad-2",
                "backend",
                experience={"cue": "", "lesson": "lesson", "evidence": "ev"},  # Empty cue
            )
        )

        # Domain library should still have 0 entries
        self.assertEqual(orch.get_domain_experience_summary("backend")["total_entries"], 0)

        # 2. Valid experience recording
        exp_payload = {
            "cue": "when connecting to SQLite WAL",
            "lesson": "set busy_timeout to at least 5000ms",
            "evidence": "zero database locked errors across 100 concurrent threads",
        }
        entry_path = orch.record_task_experience(
            task_id="task-db-001",
            domain="database",
            experience=exp_payload,
            why="Optimal concurrency configuration proven under high load",
            label="sqlite-wal-concurrency",
            run_id="run-test-1",
        )

        self.assertIsNotNone(entry_path)
        self.assertTrue(entry_path.is_dir())
        self.assertTrue((entry_path / "EXPERIENCE.md").is_file())
        self.assertTrue((entry_path / "harness").is_dir())

        # Check EXPERIENCE.md contents
        exp_text = (entry_path / "EXPERIENCE.md").read_text(encoding="utf-8")
        self.assertIn("# task-db-001", exp_text)
        self.assertIn("sqlite-wal-concurrency", exp_text)
        self.assertIn("Optimal concurrency configuration", exp_text)
        self.assertIn("- **cue**: when connecting to SQLite WAL", exp_text)
        self.assertIn("- **lesson**: set busy_timeout to at least 5000ms", exp_text)
        self.assertIn("- **evidence**: zero database locked errors", exp_text)

        # Check harness tree files were populated with always-on files
        harness_dir = entry_path / "harness"
        self.assertTrue((harness_dir / "systemprompt.md").is_file())
        self.assertTrue((harness_dir / "guardrails.md").is_file())
        self.assertTrue((harness_dir / "LongTermMEMORY.md").is_file())
        self.assertTrue((harness_dir / "tool_descriptions" / "tool_guidance.md").is_file())

        # Check INDEX.md rebuild
        lib = orch.get_domain_library("database")
        self.assertTrue(lib.index.is_file())
        index_text = lib.index.read_text(encoding="utf-8")
        self.assertIn("# Library — database", index_text)
        self.assertIn("task-db-001", index_text)
        self.assertIn("when connecting to SQLite WAL", index_text)

        # Summary helper check
        summary = orch.get_domain_experience_summary("database")
        self.assertEqual(summary["total_entries"], 1)
        self.assertEqual(summary["entries"], ["task-db-001"])
        self.assertTrue(summary["has_context"])

    # ------------------------------------------------------------------
    # Test 3: Pre-Task Experience Retrieval
    # ------------------------------------------------------------------

    def test_pre_task_experience_retrieval(self) -> None:
        orch = HarnessOrchestrator(":memory:")

        # Empty domain returns empty string
        self.assertEqual(orch.retrieve_domain_experience("networking"), "")

        # Record first entry
        orch.record_task_experience(
            task_id="task-net-01",
            domain="networking",
            experience={
                "cue": "when receiving HTTP 429",
                "lesson": "apply exponential backoff with jitter",
                "evidence": "recovered in 3 retries",
            },
        )
        # Record second entry
        orch.record_task_experience(
            task_id="task-net-02",
            domain="networking",
            experience={
                "cue": "when DNS resolution times out",
                "lesson": "fallback to secondary DNS resolver",
                "evidence": "100% resolution success",
            },
        )

        ctx = orch.retrieve_domain_experience("networking", max_entries=5)
        self.assertIn("### Proven Domain Lessons (Google Research Harness² Library: networking)", ctx)
        self.assertIn("- **Cue**: when receiving HTTP 429", ctx)
        self.assertIn("**Lesson**: apply exponential backoff with jitter", ctx)
        self.assertIn("- **Cue**: when DNS resolution times out", ctx)
        self.assertIn("**Lesson**: fallback to secondary DNS resolver", ctx)

        # Truncation check with max_entries=1 (retrieves latest entry)
        ctx_single = orch.retrieve_domain_experience("networking", max_entries=1)
        self.assertIn("DNS resolution times out", ctx_single)
        self.assertNotIn("HTTP 429", ctx_single)

    # ------------------------------------------------------------------
    # Test 4: Multi-Domain Isolation
    # ------------------------------------------------------------------

    def test_multi_domain_isolation(self) -> None:
        orch = HarnessOrchestrator(":memory:")

        # Record domain A
        orch.record_task_experience(
            task_id="task-sec-1",
            domain="security",
            experience={
                "cue": "when handling user tokens",
                "lesson": "scrub credentials before serialization",
                "evidence": "zero leakage in audit scan",
            },
        )
        # Record domain B
        orch.record_task_experience(
            task_id="task-ui-1",
            domain="ui_components",
            experience={
                "cue": "when rendering side-by-side diffs",
                "lesson": "wrap lines to terminal width - 4",
                "evidence": "no horizontal overflows on 80-col terminals",
            },
        )

        sec_ctx = orch.retrieve_domain_experience("security")
        ui_ctx = orch.retrieve_domain_experience("ui_components")

        # Strict isolation: security contains only security
        self.assertIn("scrub credentials", sec_ctx)
        self.assertNotIn("side-by-side diffs", sec_ctx)

        # Strict isolation: UI contains only UI
        self.assertIn("side-by-side diffs", ui_ctx)
        self.assertNotIn("scrub credentials", ui_ctx)

        # Verify disk directories are separate
        lib_sec = orch.get_domain_library("security")
        lib_ui = orch.get_domain_library("ui_components")
        self.assertNotEqual(lib_sec.dir, lib_ui.dir)
        self.assertEqual(lib_sec.entries(), ["task-sec-1"])
        self.assertEqual(lib_ui.entries(), ["task-ui-1"])

    # ------------------------------------------------------------------
    # Test 5: End-to-End Orchestrator Flow and Context Injection into Repair
    # ------------------------------------------------------------------

    def test_end_to_end_task_completion_records_experience_and_informs_repair(self) -> None:
        # 1. Setup clean workspace with a verifiable script
        (self.source_ws / "math_ops.py").write_text(
            "def multiply(a: int, b: int) -> int:\n    return a * b\n",
            encoding="utf-8",
        )
        (self.source_ws / "test_math_ops.py").write_text(
            "import unittest\n"
            "from math_ops import multiply\n\n"
            "class TestMathOps(unittest.TestCase):\n"
            "    def test_mul(self):\n"
            "        self.assertEqual(multiply(3, 4), 12)\n\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n",
            encoding="utf-8",
        )

        orch = HarnessOrchestrator(
            data_dir=self.data_dir,
            default_workspace=self.source_ws,
        )

        # Submit task with experience payload in constraints
        req = TaskSubmitRequest(
            goal="Verify math operations package",
            execution_mode="standard",
            workspace_root=str(self.source_ws),
            constraints={
                "domain": "arithmetic",
                "experience": {
                    "cue": "when verifying pure math functions",
                    "lesson": "unit test edge cases including zero and negatives",
                    "evidence": "100% test coverage with 0 failures",
                },
                "experience_why": "Rigorous algebraic testing protocol established",
                "experience_label": "algebra-suite",
            },
            steps=[
                {
                    "step_id": "step_1",
                    "tool_id": "read_file",
                    "args": {"path": "math_ops.py"},
                    "risk_class": "R0",
                }
            ],
        )

        task = orch.submit_and_run(req)
        self.assertEqual(task["status"], TaskState.SUCCEEDED.value)

        # Verify event was hash-chained in the ledger
        events = orch.ledger.list_events(task["task_id"])
        h2_events = [e for e in events if e["event_type"] == "HARNESS2_EXPERIENCE_RECORDED"]
        self.assertEqual(len(h2_events), 1)
        h2_ev = h2_events[0]
        self.assertEqual(h2_ev["reason_code"], "EXPERIENCE_STORED")
        payload = h2_ev["payload"]
        self.assertEqual(payload["domain"], "arithmetic")
        self.assertEqual(payload["cue"], "when verifying pure math functions")

        # Verify library persisted on disk
        lib = orch.get_domain_library("arithmetic")
        self.assertEqual(lib.entries(), [task["task_id"]])

        # 2. Verify subsequent repair prompt in the same domain receives the domain experience
        # Create a second task with a deliberate bug in the same domain
        (self.source_ws / "broken_calc.py").write_text(
            "def divide(a: int, b: int) -> float:\n    return a + b  # Bug: + instead of /\n",
            encoding="utf-8",
        )
        (self.source_ws / "test_broken_calc.py").write_text(
            "import unittest\n"
            "from broken_calc import divide\n\n"
            "class TestBrokenCalc(unittest.TestCase):\n"
            "    def test_div(self):\n"
            "        self.assertEqual(divide(10, 2), 5.0)\n\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n",
            encoding="utf-8",
        )

        captured_context_prefixes: list[str] = []

        def mock_execute_routed_request(prompt: str, **kwargs: Any) -> tuple[RouteDecision, ModelResponse]:
            cacheable_prefix = kwargs.get("cacheable_prefix", "")
            captured_context_prefixes.append(cacheable_prefix)
            # Return valid repair patch
            patch_json = (
                '[\n  {"tool_id": "write_file", "args": {"path": "broken_calc.py", '
                '"content": "def divide(a: int, b: int) -> float:\\n    return a / b\\n"}}\n]'
            )
            resp = ModelResponse(
                text=patch_json,
                model_id="local_test_model",
                provider_id="local",
                usage=ModelUsage(input_tokens=50, output_tokens=30),
            )
            decision = RouteDecision(
                tier="tier1_local_m4",
                provider="local",
                model_id="local_test_model",
                reason="test",
                raw_tokens_estimated=100,
                compressed_tokens_estimated=50,
                token_savings_pct=50.0,
                compressed_context="",
            )
            return decision, resp

        with patch.object(orch.router, "execute_routed_request", side_effect=mock_execute_routed_request):
            repair_req = TaskSubmitRequest(
                goal="Fix divide calculation bug in broken_calc.py",
                execution_mode="standard",
                workspace_root=str(self.source_ws),
                constraints={"domain": "arithmetic"},
                budget={"max_retries_per_step": 2, "max_cost_usd": 1.0},
                steps=[
                    {
                        "step_id": "step_div_inspect",
                        "tool_id": "read_file",
                        "args": {"path": "broken_calc.py"},
                        "risk_class": "R0",
                    }
                ],
            )
            repair_task = orch.submit_and_run(repair_req)
            self.assertEqual(repair_task["status"], TaskState.SUCCEEDED.value)

        # Verify that the router was called and received the domain lessons in its context prefix
        self.assertGreater(len(captured_context_prefixes), 0)
        prefix = captured_context_prefixes[0]
        self.assertIn("### Proven Domain Lessons (Google Research Harness² Library: arithmetic)", prefix)
        self.assertIn("when verifying pure math functions", prefix)
        self.assertIn("unit test edge cases including zero and negatives", prefix)


if __name__ == "__main__":
    unittest.main()
