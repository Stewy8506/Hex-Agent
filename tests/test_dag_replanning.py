"""Unit and integration tests for Dynamic DAG Replanning and Selective Subgraph Invalidation.

Validates:
1. Selective subgraph invalidation preserving independent completed steps.
2. Hash-chained PLAN_REVISED task events linking sequentially without breaks.
3. Dynamic blast-radius test pinning via Active Graphify.
4. End-to-end task execution with dynamic DAG replanning reaching SUCCEEDED.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from harness_core.db import LedgerStore, compute_event_hash, canonical_json
from harness_core.models import PlanNode, TaskState, TaskSubmitRequest
from harness_core.orchestrator import HarnessOrchestrator


class TestDAGReplanning(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="harness_replan_test_"))
        self.source_ws = self.tmp_dir / "source_repo"
        self.source_ws.mkdir(parents=True, exist_ok=True)
        self.data_dir = self.tmp_dir / "harness_data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.orch = HarnessOrchestrator(
            data_dir=self.data_dir,
            default_workspace=self.source_ws,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Test 1: Selective Subgraph Invalidation with Independent Step Preservation
    # ------------------------------------------------------------------

    def test_selective_subgraph_invalidation_and_preservation(self) -> None:
        req = TaskSubmitRequest(
            goal="Build multi-component architecture",
            execution_mode="standard",
            workspace_root=str(self.source_ws),
        )
        task = self.orch.submit_task(req, auto_execute=False)
        task_id = task["task_id"]

        # Construct 4-step DAG:
        # Step 1: inspect_repo (root, completed)
        # Step 2A: write_core_service (depends on Step 1, completed)
        # Step 2B: write_unrelated_logger (depends on Step 1, independent of 2A, completed)
        # Step 3: integrate_service (depends on Step 2A, pending)
        step_1 = PlanNode(
            step_id="step_1_inspect",
            title="Inspect repo files",
            tool_id="list_files",
            args={},
            risk_class="R0",
            status="COMPLETED",
        )
        step_2A = PlanNode(
            step_id="step_2A_write_core_service",
            title="Write core service",
            tool_id="write_file",
            args={"path": "service.py"},
            depends_on=["step_1_inspect"],
            risk_class="R1",
            status="COMPLETED",
            side_effects=[],
        )
        step_2B = PlanNode(
            step_id="step_2B_write_unrelated_logger",
            title="Write unrelated logger",
            tool_id="write_file",
            args={"path": "logger.py"},
            depends_on=["step_1_inspect"],
            risk_class="R1",
            status="COMPLETED",
            side_effects=["logger.py"],
        )
        step_3 = PlanNode(
            step_id="step_3_integrate_service",
            title="Integrate service",
            tool_id="write_file",
            args={"path": "integration.py"},
            depends_on=["step_2A_write_core_service"],
            risk_class="R1",
            status="PENDING",
        )

        initial_plan = self.orch.ledger.save_plan(
            task_id,
            [step_1, step_2A, step_2B, step_3],
            rationale="Initial multi-step plan",
        )
        self.assertEqual(initial_plan["revision"], 1)
        self.assertIsNone(initial_plan["supersedes_revision"])

        # Simulate side-effect mutation at Step 2A (writing service.py)
        (self.source_ws / "service.py").write_text("SERVICE_V2 = True\n", encoding="utf-8")

        # Invoke replan_dag
        new_plan = self.orch.replan_dag(
            task_id=task_id,
            current_plan=initial_plan,
            mutated_step_id="step_2A_write_core_service",
            actual_modified_files=["service.py"],
            attempt_ws=self.source_ws,
        )

        # Verify revision increment
        self.assertEqual(new_plan["revision"], 2)
        self.assertEqual(new_plan["supersedes_revision"], 1)

        # Verify selective invalidation and preservation in DAG
        node_map = {n["step_id"]: n for n in new_plan["dag"]}
        # a. Step 3 is marked INVALIDATED
        self.assertEqual(node_map["step_3_integrate_service"]["status"], "INVALIDATED")
        # b. Step 1 and Step 2B remain COMPLETED and preserved
        self.assertEqual(node_map["step_1_inspect"]["status"], "COMPLETED")
        self.assertEqual(node_map["step_2B_write_unrelated_logger"]["status"], "COMPLETED")
        # Step 2A also completed
        self.assertEqual(node_map["step_2A_write_core_service"]["status"], "COMPLETED")

    # ------------------------------------------------------------------
    # Test 2: Hash-Chained Task Event for Plan Revisions
    # ------------------------------------------------------------------

    def test_hash_chained_task_event_for_plan_revisions(self) -> None:
        req = TaskSubmitRequest(
            goal="Test plan revision hashing",
            execution_mode="standard",
            workspace_root=str(self.source_ws),
        )
        task = self.orch.submit_task(req, auto_execute=False)
        task_id = task["task_id"]

        step_1 = PlanNode(
            step_id="step_1",
            title="Step 1",
            tool_id="list_files",
            status="COMPLETED",
        )
        step_2A = PlanNode(
            step_id="step_2A",
            title="Step 2A",
            tool_id="write_file",
            depends_on=["step_1"],
            status="COMPLETED",
        )
        step_2B = PlanNode(
            step_id="step_2B",
            title="Step 2B",
            tool_id="write_file",
            depends_on=["step_1"],
            status="COMPLETED",
        )
        step_3 = PlanNode(
            step_id="step_3",
            title="Step 3",
            tool_id="write_file",
            depends_on=["step_2A"],
            status="PENDING",
        )

        plan = self.orch.ledger.save_plan(task_id, [step_1, step_2A, step_2B, step_3])
        (self.source_ws / "mod.py").write_text("x = 1\n", encoding="utf-8")

        new_plan = self.orch.replan_dag(
            task_id=task_id,
            current_plan=plan,
            mutated_step_id="step_2A",
            actual_modified_files=["mod.py"],
            attempt_ws=self.source_ws,
        )

        events = self.orch.ledger.list_events(task_id)
        replan_events = [e for e in events if e["event_type"] == "PLAN_REVISED"]
        self.assertEqual(len(replan_events), 1)

        rev_event = replan_events[0]
        self.assertEqual(rev_event["reason_code"], "SELECTIVE_SUBGRAPH_INVALIDATION")
        payload = rev_event["payload"]
        self.assertEqual(payload["mutated_step_id"], "step_2A")
        self.assertIn("step_3", payload["invalidated_step_ids"])
        self.assertIn("step_1", payload["preserved_step_ids"])
        self.assertIn("step_2B", payload["preserved_step_ids"])
        self.assertEqual(payload["new_revision"], 2)

        # Cryptographic hash-chain continuity check across all events
        prev_sha = "0" * 64
        for ev in events:
            expected_sha = compute_event_hash(
                prev_hash=prev_sha,
                task_id=task_id,
                seq_no=ev["sequence_no"],
                event_type=ev["event_type"],
                prior_state=ev["prior_state"],
                next_state=ev["next_state"],
                reason_code=ev["reason_code"],
                workspace_tree_sha=ev["workspace_tree_sha"],
                payload_str=canonical_json(ev["payload"]),
                occurred_at=ev["occurred_at"],
            )
            self.assertEqual(
                ev["event_sha256"],
                expected_sha,
                f"Broken hash chain at sequence {ev['sequence_no']}",
            )
            prev_sha = ev["event_sha256"]

    # ------------------------------------------------------------------
    # Test 3: Dynamic Test Pinning from Active Graphify
    # ------------------------------------------------------------------

    def test_dynamic_test_pinning_from_active_graphify(self) -> None:
        # Create core_lib.py and test_core_callers.py in workspace
        (self.source_ws / "core_lib.py").write_text(
            "def calculate_total(a: int, b: int) -> int:\n"
            "    return a + b\n",
            encoding="utf-8",
        )
        (self.source_ws / "test_core_callers.py").write_text(
            "import unittest\n"
            "from core_lib import calculate_total\n\n"
            "class TestCoreCallers(unittest.TestCase):\n"
            "    def test_calc(self):\n"
            "        self.assertEqual(calculate_total(10, 20), 30)\n\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n",
            encoding="utf-8",
        )

        req = TaskSubmitRequest(
            goal="Refactor core lib with dynamic test pinning",
            execution_mode="standard",
            workspace_root=str(self.source_ws),
        )
        task = self.orch.submit_task(req, auto_execute=False)
        task_id = task["task_id"]

        step_1 = PlanNode(
            step_id="step_1_inspect",
            title="Inspect repo",
            tool_id="list_files",
            status="COMPLETED",
        )
        step_2 = PlanNode(
            step_id="step_2_modify_core",
            title="Modify core lib",
            tool_id="write_file",
            args={"path": "core_lib.py"},
            depends_on=["step_1_inspect"],
            status="COMPLETED",
            side_effects=[],
        )

        plan = self.orch.ledger.save_plan(
            task_id,
            [step_1, step_2],
            blast_radius_checks=[],
            rationale="Initial plan with zero pinned tests",
        )
        self.assertEqual(plan["blast_radius_checks"], [])

        # Step 2 modifies core_lib.py
        (self.source_ws / "core_lib.py").write_text(
            "def calculate_total(a: int, b: int) -> int:\n"
            "    # Optimized implementation\n"
            "    return (a + b)\n",
            encoding="utf-8",
        )

        # replan_dag triggers Active Graphify analysis
        new_plan = self.orch.replan_dag(
            task_id=task_id,
            current_plan=plan,
            mutated_step_id="step_2_modify_core",
            actual_modified_files=["core_lib.py"],
            attempt_ws=self.source_ws,
        )

        # Verify test_core_callers.py was dynamically discovered and pinned
        pinned = new_plan.get("blast_radius_checks") or []
        self.assertIn("test_core_callers.py", pinned)

        # Verify proof service executes dynamically pinned checks
        snap = self.orch.cow.create_snapshot(self.source_ws, label=f"base_{task_id}")
        attempt_ws = self.orch.workspaces_root / f"ws_{task_id}_test"
        self.orch.cow.clone_to_workspace(snap.snapshot_path, attempt_ws)
        ver_outcome = self.orch.proof_service.run_verification_and_build_proof(
            task_id=task_id,
            attempt_id="att_test_pinning",
            base_snapshot_path=snap.snapshot_path,
            workspace_dir=attempt_ws,
            modified_files=["core_lib.py"],
            run_tests=True,
        )
        self.assertTrue(ver_outcome["passed"])
        ver_results = ver_outcome.get("verification_results") or {}
        self.assertIn("test_core_callers.py", ver_results.get("pinned_blast_radius_targets", []))

    # ------------------------------------------------------------------
    # Test 4: End-to-End Task Execution with Dynamic Replanning
    # ------------------------------------------------------------------

    def test_e2e_task_execution_with_dynamic_replanning(self) -> None:
        # Create initial test in workspace
        (self.source_ws / "test_pipeline.py").write_text(
            "import unittest\n"
            "from pathlib import Path\n\n"
            "class TestPipeline(unittest.TestCase):\n"
            "    def test_integrated_output(self):\n"
            "        data_p = Path('pipeline_output.txt')\n"
            "        self.assertTrue(data_p.is_file())\n"
            "        self.assertEqual(data_p.read_text(encoding='utf-8').strip(), 'PIPELINE_OK')\n\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n",
            encoding="utf-8",
        )

        # Task steps:
        # Step 1: list files
        # Step 2: write core_data.txt without declaring side_effects (triggers unexpected side-effects)
        # Step 3: depends on step 2, writes pipeline_output.txt
        req = TaskSubmitRequest(
            goal="Execute multi-step pipeline with dynamic replanning",
            execution_mode="standard",
            workspace_root=str(self.source_ws),
            steps=[
                {
                    "step_id": "step_1_inspect",
                    "title": "Inspect workspace",
                    "tool_id": "list_files",
                    "args": {},
                    "risk_class": "R0",
                },
                {
                    "step_id": "step_2_core",
                    "title": "Write core data",
                    "tool_id": "write_file",
                    "args": {"path": "core_data.txt", "content": "CORE_V1"},
                    "depends_on": ["step_1_inspect"],
                    "risk_class": "R1",
                    "side_effects": [],  # Declared empty -> triggers unexpected side-effect replan
                },
                {
                    "step_id": "step_3_finish",
                    "title": "Write pipeline output",
                    "tool_id": "write_file",
                    "args": {"path": "pipeline_output.txt", "content": "PIPELINE_OK"},
                    "depends_on": ["step_2_core"],
                    "risk_class": "R1",
                    "side_effects": ["pipeline_output.txt"],
                },
            ],
        )

        task = self.orch.submit_and_run(req)
        self.assertEqual(task["status"], TaskState.SUCCEEDED.value)

        # Verify that dynamic replanning occurred: plan revision >= 2
        latest_plan = self.orch.ledger.get_latest_plan(task["task_id"])
        self.assertIsNotNone(latest_plan)
        self.assertGreaterEqual(latest_plan["revision"], 2)
        self.assertIsNotNone(latest_plan["supersedes_revision"])

        # Verify PLAN_REVISED event was recorded in ledger
        events = self.orch.ledger.list_events(task["task_id"])
        event_types = [e["event_type"] for e in events]
        self.assertIn("PLAN_REVISED", event_types)

        # Verify final checkpoint recorded latest plan revision
        latest_chk = self.orch.ledger.get_latest_checkpoint(task["task_id"])
        self.assertIsNotNone(latest_chk)
        self.assertEqual(latest_chk["plan_revision"], latest_plan["revision"])
        self.assertIn("step_1_inspect", latest_chk["completed_steps"])
        self.assertIn("step_2_core", latest_chk["completed_steps"])
        self.assertIn("step_3_finish", latest_chk["completed_steps"])

    # ------------------------------------------------------------------
    # Test 5: Selective Invalidation with Multi-Branch Dependency Tree
    # ------------------------------------------------------------------

    def test_multi_branch_selective_invalidation(self) -> None:
        req = TaskSubmitRequest(
            goal="Branching DAG test",
            execution_mode="standard",
            workspace_root=str(self.source_ws),
        )
        task = self.orch.submit_task(req, auto_execute=False)
        task_id = task["task_id"]

        # Branch structure:
        # root -> branch_A_1 -> branch_A_2 (downstream of A)
        # root -> branch_B_1 -> branch_B_2 (downstream of B)
        nodes = [
            PlanNode(step_id="root", title="Root", tool_id="list_files", status="COMPLETED"),
            PlanNode(step_id="branch_A_1", title="A1", tool_id="write_file", depends_on=["root"], status="COMPLETED"),
            PlanNode(step_id="branch_A_2", title="A2", tool_id="write_file", depends_on=["branch_A_1"], status="COMPLETED"),
            PlanNode(step_id="branch_B_1", title="B1", tool_id="write_file", depends_on=["root"], status="COMPLETED"),
            PlanNode(step_id="branch_B_2", title="B2", tool_id="write_file", depends_on=["branch_B_1"], status="COMPLETED"),
        ]
        plan = self.orch.ledger.save_plan(task_id, nodes)

        (self.source_ws / "mod_a.py").write_text("A = 1\n", encoding="utf-8")

        # Mutate branch_A_1
        new_plan = self.orch.replan_dag(
            task_id=task_id,
            current_plan=plan,
            mutated_step_id="branch_A_1",
            actual_modified_files=["mod_a.py"],
            attempt_ws=self.source_ws,
        )

        n_map = {n["step_id"]: n for n in new_plan["dag"]}
        # Only branch_A_2 should be INVALIDATED
        self.assertEqual(n_map["branch_A_2"]["status"], "INVALIDATED")
        # Root and entire B branch should remain COMPLETED
        self.assertEqual(n_map["root"]["status"], "COMPLETED")
        self.assertEqual(n_map["branch_B_1"]["status"], "COMPLETED")
        self.assertEqual(n_map["branch_B_2"]["status"], "COMPLETED")
        self.assertEqual(n_map["branch_A_1"]["status"], "COMPLETED")


if __name__ == "__main__":
    unittest.main()
