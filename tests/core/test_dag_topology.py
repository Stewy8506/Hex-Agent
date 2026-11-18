"""B06/B09 regression: DAG topology validation and replan-budget attention.

B06: cyclic or dangling dependencies must be rejected when a plan is saved, and
execution must never fall back to an out-of-order node.
B09: continued divergence after the replan budget is exhausted must stop the
task for attention instead of silently continuing.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from harness_core.core.models import TaskState, TaskSubmitRequest, select_runnable_step
from harness_core.core.orchestrator import HarnessOrchestrator
from harness_core.storage.db import LedgerStore, validate_dag_topology


def node(step_id: str, depends_on: list[str] | None = None) -> dict:
    return {"step_id": step_id, "tool_id": "list_files", "depends_on": depends_on or []}


class TestDAGTopologyValidation(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="harness_dag_topology_"))
        self.ledger = LedgerStore(self.tmp_dir / "ledger.db")

    def tearDown(self) -> None:
        self.ledger.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_valid_topology_is_accepted(self) -> None:
        validate_dag_topology([node("a"), node("b", ["a"]), node("c", ["a", "b"])])
        task = self.ledger.submit_task({"goal": "valid plan", "execution_mode": "standard"})
        plan = self.ledger.save_plan(task["task_id"], [node("a"), node("b", ["a"])])
        self.assertEqual(plan["revision"], 1)

    def test_cycle_is_rejected_at_save(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self.ledger.save_plan(
                "tsk_cycle", [node("a", ["c"]), node("b", ["a"]), node("c", ["b"])]
            )
        self.assertIn("Cyclic DAG dependency", str(ctx.exception))
        self.assertIsNone(self.ledger.get_latest_plan("tsk_cycle"))

    def test_dangling_dependency_is_rejected_at_save(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self.ledger.save_plan("tsk_dangling", [node("a", ["missing"])])
        self.assertIn("unknown step", str(ctx.exception))

    def test_duplicate_step_ids_are_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self.ledger.save_plan("tsk_dupe", [node("a"), node("a")])
        self.assertIn("Duplicate DAG step_id", str(ctx.exception))

    def test_self_dependency_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self.ledger.save_plan("tsk_self", [node("a", ["a"])])
        self.assertIn("depends on itself", str(ctx.exception))

    def test_empty_plan_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.ledger.save_plan("tsk_empty", [])

    def test_select_runnable_step_never_returns_out_of_order_node(self) -> None:
        cyclic = [node("a", ["b"]), node("b", ["a"])]
        runnable, unresolved = select_runnable_step(cyclic, set())
        self.assertIsNone(runnable)
        self.assertEqual(unresolved, {"a": ["b"], "b": ["a"]})

        # An independent node is still chosen when one exists.
        with_independent = cyclic + [node("c")]
        runnable, unresolved = select_runnable_step(with_independent, set())
        self.assertEqual(runnable["step_id"], "c")
        self.assertEqual(unresolved, {})

    def test_completed_dependency_unblocks_successor(self) -> None:
        nodes = [node("a"), node("b", ["a"])]
        runnable, _ = select_runnable_step(nodes, {"a"})
        self.assertEqual(runnable["step_id"], "b")
        runnable, _ = select_runnable_step(nodes, {"a", "b"})
        self.assertIsNone(runnable)


class TestReplanBudgetAttention(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="harness_replan_budget_"))
        self.source_ws = self.tmp_dir / "source_repo"
        self.source_ws.mkdir(parents=True)
        self.orch = HarnessOrchestrator(
            data_dir=self.tmp_dir / "harness_data",
            default_workspace=self.source_ws,
        )

    def tearDown(self) -> None:
        self.orch.shutdown()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_replan_budget_exhaustion_pauses_for_attention(self) -> None:
        req = TaskSubmitRequest(
            goal="Diverging pipeline with no replan budget",
            execution_mode="standard",
            workspace_root=str(self.source_ws),
            constraints={"max_replans": 0},
            steps=[
                {
                    "step_id": "step_1_write",
                    "title": "Write undeclared side effect",
                    "tool_id": "write_file",
                    "args": {"path": "out.txt", "content": "X"},
                    "risk_class": "R1",
                    "side_effects": [],  # Undeclared -> divergence -> replan required
                }
            ],
        )
        task = self.orch.submit_and_run(req)

        self.assertEqual(task["status"], TaskState.NEEDS_ATTENTION.value)
        reason_codes = [e["reason_code"] for e in self.orch.ledger.list_events(task["task_id"])]
        self.assertIn("REPLAN_BUDGET_EXHAUSTED", reason_codes)


if __name__ == "__main__":
    unittest.main()
