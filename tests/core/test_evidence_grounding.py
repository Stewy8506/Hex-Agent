"""B67 regression tests: evidence-grounded lessons + published DAG-learned memory.

Proves:
1. ReAct friction lessons carry evidence harvested from the session's turns:
   failed tool outputs (error_traces), files written (modified_files), and the
   last applied patch content (final_patch).
2. The DAG coding path writes post-success learning into the REAL workspace:
   - episodic work-memory docs land in source_ws/.hex/memory/ (not the CoW
     attempt clone, whose .hex tree is excluded from publish_delta);
   - friction reflection runs against source_ws with the failure digest and
     the applied repair patch as evidence.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness_core.core.models import TaskSubmitRequest
from harness_core.core.orchestrator import HarnessOrchestrator
from harness_core.core.react_loop import (
    ReActSessionResult,
    ReActTurn,
    ToolCallRequest,
    ToolCallResult,
)


class TestExtractReactEvidence(unittest.TestCase):
    def _harvest(self, turns):
        from harness_core.core.orchestrator import _extract_react_evidence

        return _extract_react_evidence(turns)

    def test_harvests_error_traces_from_failed_tools(self) -> None:
        turns = [
            ReActTurn(
                turn_index=0,
                thought="run tests",
                tool_calls=[ToolCallRequest(call_id="c1", name="run_tests", arguments={})],
                tool_results=[
                    ToolCallResult(
                        call_id="c1", name="run_tests", output="AssertionError: 2 != 5", is_error=True
                    )
                ],
            )
        ]
        ev = self._harvest(turns)
        self.assertEqual(ev["error_traces"], ["run_tests: AssertionError: 2 != 5"])

    def test_errored_write_file_is_not_a_modified_file(self) -> None:
        turns = [
            ReActTurn(
                turn_index=0,
                thought="write",
                tool_calls=[
                    ToolCallRequest(
                        call_id="c1",
                        name="write_file",
                        arguments={"path": "calc.py", "content": "broken"},
                    )
                ],
                tool_results=[
                    ToolCallResult(call_id="c1", name="write_file", output="denied", is_error=True)
                ],
            )
        ]
        ev = self._harvest(turns)
        self.assertEqual(ev["modified_files"], [])
        self.assertEqual(ev["final_patch"], "")

    def test_last_successful_write_wins_as_final_patch(self) -> None:
        turns = [
            ReActTurn(
                turn_index=0,
                thought="first write",
                tool_calls=[
                    ToolCallRequest(
                        call_id="c1",
                        name="write_file",
                        arguments={"path": "a.txt", "content": "draft one"},
                    )
                ],
                tool_results=[ToolCallResult(call_id="c1", name="write_file", output="ok")],
            ),
            ReActTurn(
                turn_index=1,
                thought="second write",
                tool_calls=[
                    ToolCallRequest(
                        call_id="c2",
                        name="write_file",
                        arguments={"path": "b.txt", "content": "final content"},
                    )
                ],
                tool_results=[ToolCallResult(call_id="c2", name="write_file", output="ok")],
            ),
        ]
        ev = self._harvest(turns)
        self.assertEqual(ev["modified_files"], ["a.txt", "b.txt"])
        self.assertEqual(ev["final_patch"], "final content")

    def test_caps_error_traces_and_modified_files(self) -> None:
        err_turns = [
            ReActTurn(
                turn_index=i,
                thought="fail",
                tool_calls=[ToolCallRequest(call_id=f"e{i}", name="run_tests", arguments={})],
                tool_results=[
                    ToolCallResult(call_id=f"e{i}", name="run_tests", output=f"boom {i}", is_error=True)
                ],
            )
            for i in range(7)
        ]
        write_turns = [
            ReActTurn(
                turn_index=100 + i,
                thought="write",
                tool_calls=[
                    ToolCallRequest(
                        call_id=f"w{i}",
                        name="write_file",
                        arguments={"path": f"f{i}.txt", "content": f"c{i}"},
                    )
                ],
                tool_results=[ToolCallResult(call_id=f"w{i}", name="write_file", output="ok")],
            )
            for i in range(25)
        ]
        ev = self._harvest(err_turns + write_turns)
        self.assertEqual(len(ev["error_traces"]), 5)
        self.assertEqual(len(ev["modified_files"]), 20)


class _FakeEvidenceLoop:
    """ReActLoop stand-in whose session contains failing + successful tool turns."""

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs

    def run(
        self,
        prompt: str | None = None,
        task_id: str | None = None,
        initial_messages: object = None,
    ) -> ReActSessionResult:
        turns = [
            ReActTurn(
                turn_index=0,
                thought="run tests",
                tool_calls=[ToolCallRequest(call_id="c1", name="run_tests", arguments={})],
                tool_results=[
                    ToolCallResult(
                        call_id="c1", name="run_tests", output="AssertionError: 2 != 5", is_error=True
                    )
                ],
            ),
            ReActTurn(
                turn_index=1,
                thought="write fix",
                tool_calls=[
                    ToolCallRequest(
                        call_id="c2",
                        name="write_file",
                        arguments={"path": "notes.txt", "content": "patch body v2"},
                    )
                ],
                tool_results=[ToolCallResult(call_id="c2", name="write_file", output="ok")],
            ),
        ]
        return ReActSessionResult(
            success=True,
            final_answer="Wrote notes.txt",
            total_turns=2,
            turns=turns,
            termination_reason="finish",
            retry_count=3,
            friction_info={
                "friction_detected": True,
                "retry_count": 3,
                "turn_repair_count": 1,
                "repair_attempts": 0,
                "reasons": ["repeated_tool_failure"],
            },
        )


class TestReActReflectionEvidenceWiring(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="harness_react_evidence_test_"))
        self.source_ws = self.tmp_dir / "source_repo"
        self.source_ws.mkdir(parents=True, exist_ok=True)
        self.data_dir = self.tmp_dir / "harness_data"
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        if hasattr(self, "orch") and self.orch is not None:
            self.orch.shutdown()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_agent_loop_reflection_receives_harvested_evidence(self) -> None:
        orch = HarnessOrchestrator(
            data_dir=self.data_dir,
            default_workspace=self.source_ws,
        )
        with patch.object(
            HarnessOrchestrator, "_handle_post_task_reflection", return_value={"status": "synthesized"}
        ) as mock_reflect, patch("harness_core.core.orchestrator.ReActLoop", _FakeEvidenceLoop):
            orch.run_agent_loop(
                goal="fix failing tests and write notes.txt",
                workspace_root=self.source_ws,
                demo_mode=False,
            )

        mock_reflect.assert_called_once()
        kwargs = mock_reflect.call_args.kwargs
        self.assertEqual(kwargs["error_traces"], ["run_tests: AssertionError: 2 != 5"])
        self.assertEqual(kwargs["modified_files"], ["notes.txt"])
        self.assertEqual(kwargs["final_patch"], "patch body v2")
        self.assertEqual(Path(kwargs["workspace_root"]).resolve(), self.source_ws.resolve())


class TestDagEvidenceAndWorkspaceRouting(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="harness_dag_evidence_test_"))
        self.source_ws = self.tmp_dir / "source_repo"
        self.source_ws.mkdir(parents=True, exist_ok=True)
        self.data_dir = self.tmp_dir / "harness_data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
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

    def tearDown(self) -> None:
        if hasattr(self, "orch") and self.orch is not None:
            self.orch.shutdown()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_reflection_runs_in_source_workspace_with_failure_and_patch_evidence(self) -> None:
        repair_call_count = 0

        def three_attempt_repair_generator(
            task_id: str,
            attempt_ws: Path,
            failure_digest: str,
            blast_report: object,
            modified_files: list[str],
        ) -> list[dict[str, object]]:
            nonlocal repair_call_count
            repair_call_count += 1
            if repair_call_count == 1:
                content = "def add(a: int, b: int) -> int:\n    return a * b\n"
            elif repair_call_count == 2:
                content = "def add(a: int, b: int) -> int:\n    return a - b - 1\n"
            else:
                content = "def add(a: int, b: int) -> int:\n    return a + b\n"
            return [{"tool_id": "write_file", "args": {"path": "calculator.py", "content": content}}]

        orch = HarnessOrchestrator(
            data_dir=self.data_dir,
            default_workspace=self.source_ws,
            repair_patch_generator=three_attempt_repair_generator,
        )

        with patch.object(
            HarnessOrchestrator, "_handle_post_task_reflection", return_value={"status": "synthesized"}
        ) as mock_reflect:
            req = TaskSubmitRequest(
                goal="Fix addition calculation bug in calculator.py after repeated friction",
                execution_mode="standard",
                workspace_root=str(self.source_ws),
                budget={"max_retries_per_step": 3, "max_cost_usd": 2.0},
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

        self.assertEqual(task["status"], "SUCCEEDED")
        self.assertEqual(repair_call_count, 3, "Reflection gate requires three repair attempts")

        mock_reflect.assert_called_once()
        kwargs = mock_reflect.call_args.kwargs
        self.assertEqual(
            Path(kwargs["workspace_root"]).resolve(),
            self.source_ws.resolve(),
            "Lessons must be written into the real workspace, not the CoW attempt clone",
        )
        self.assertTrue(kwargs["error_traces"], "Failure digest must be passed as evidence")
        self.assertIn("AssertionError", kwargs["error_traces"][0])
        self.assertEqual(kwargs["modified_files"], ["calculator.py"])
        self.assertIn("return a + b", kwargs["final_patch"])

    def test_episodic_work_memory_publishes_to_source_workspace(self) -> None:
        def one_shot_repair_generator(*args: object, **kwargs: object) -> list[dict[str, object]]:
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
            repair_patch_generator=one_shot_repair_generator,
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
        self.assertEqual(task["status"], "SUCCEEDED")

        mem_docs = list((self.source_ws / ".hex" / "memory").glob("*.md"))
        self.assertTrue(
            mem_docs,
            "Episodic work-memory outcome must be published into the real workspace",
        )
        combined = "\n".join(p.read_text(encoding="utf-8") for p in mem_docs)
        self.assertIn("work_memory", combined)
        self.assertIn("Task", combined)


if __name__ == "__main__":
    unittest.main()
