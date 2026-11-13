"""Unit and integration test suite for OrchestratorBenchmarkAdapter and sweep runner.

Tests:
1. Protocol conformance against Google Research Harness² BenchmarkAdapter protocol.
2. Rollout execution: isolated run directories, workspace sandboxing, and trajectory formatting.
3. Multi-rollout Propose-Probe-Compose recursion sweeps (run_parallel and run_sequential).
4. CLI subcommand hex harness2-eval (summary table and structured JSON outputs).
5. Regression safety and re-exports in proof.py.
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from harness_core.vendor_paths import get_harness2_dir

# Ensure cloned vendor/harness2 is on sys.path
VENDOR_HARNESS2 = get_harness2_dir()
if str(VENDOR_HARNESS2) not in sys.path and VENDOR_HARNESS2.is_dir():
    sys.path.insert(0, str(VENDOR_HARNESS2))

from harness2.core.spec import BenchmarkAdapter, BenchSpec, RunView
from harness_core.cli import main as cli_main
from harness_core.harness2_adapter import (
    LocalRouterProposer,
    OrchestratorBenchmarkAdapter,
    run_orchestrator_sweep,
)
from harness_core.orchestrator import HarnessOrchestrator
from harness_core.proof import OrchestratorBenchmarkAdapter as ProofBenchmarkAdapter


class TestHarness2BenchmarkAdapter(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="h2_bench_test_"))
        self.workspace_dir = self.tmp_dir / "workspace"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = self.tmp_dir / "harness_data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.runs_root = self.tmp_dir / "benchmark_runs"
        self.runs_root.mkdir(parents=True, exist_ok=True)

        self.orchestrator = HarnessOrchestrator(
            data_dir=self.data_dir,
            default_workspace=self.workspace_dir,
        )
        self.adapter = OrchestratorBenchmarkAdapter(
            self.orchestrator,
            runs_root=self.runs_root,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # 1. Protocol Conformance Tests
    # ------------------------------------------------------------------

    def test_protocol_conformance(self) -> None:
        """Verify OrchestratorBenchmarkAdapter satisfies the BenchmarkAdapter protocol."""
        try:
            self.assertTrue(
                issubclass(OrchestratorBenchmarkAdapter, BenchmarkAdapter),
                "OrchestratorBenchmarkAdapter must satisfy issubclass check against BenchmarkAdapter",
            )
            self.assertTrue(
                isinstance(self.adapter, BenchmarkAdapter),
                "Adapter instance must satisfy isinstance check against BenchmarkAdapter",
            )
        except TypeError:
            # Python 3.11 Protocol with non-method data members does not support runtime issubclass/isinstance checks
            pass

        required_methods = [
            "tasks",
            "domain_of",
            "task_prompt",
            "task_files",
            "execute",
            "delivered",
            "trajectory",
            "deliverable_files",
            "deliverable_text",
            "base_harness",
            "validate_tree",
            "score",
        ]
        for m in required_methods:
            self.assertTrue(hasattr(self.adapter, m), f"Adapter missing method: {m}")
            self.assertTrue(callable(getattr(self.adapter, m)), f"Attribute {m} is not callable")

        self.assertTrue(hasattr(self.adapter, "spec"))
        self.assertIsInstance(self.adapter.spec, BenchSpec)
        self.assertTrue(hasattr(self.adapter, "substrate"))

    def test_task_registry_and_specification_methods(self) -> None:
        """Verify task listing, prompt resolution, and domain classification."""
        tasks = self.adapter.tasks()
        self.assertIn("demo-1", tasks)
        self.assertIn("ledger-1", tasks)
        self.assertIn("bench-calc", tasks)

        # Domain classification
        self.assertEqual(self.adapter.domain_of("demo-1"), "software_engineering")
        self.assertEqual(self.adapter.domain_of("ledger-1"), "accounting")
        self.assertEqual(self.adapter.domain_of("unknown-task"), "software_engineering")

        # Task prompt
        prompt = self.adapter.task_prompt("demo-1")
        self.assertTrue(len(prompt) > 0)

        # Dynamic task registration
        self.adapter.register_task(
            task_id="custom-api-task",
            prompt="Build a REST API endpoint /healthz",
            domain="backend_engineering",
        )
        self.assertIn("custom-api-task", self.adapter.tasks())
        self.assertEqual(self.adapter.domain_of("custom-api-task"), "backend_engineering")
        self.assertEqual(self.adapter.task_prompt("custom-api-task"), "Build a REST API endpoint /healthz")

    def test_tree_validation(self) -> None:
        """Verify harness tree validation detects missing components."""
        surface = self.adapter.substrate.surface()
        valid_tree = {name: "# Valid" for name in surface.always_on}
        self.adapter.validate_tree(valid_tree)

        # Violation: missing systemprompt.md
        with self.assertRaises(ValueError) as ctx:
            self.adapter.validate_tree({})
        self.assertIn("systemprompt.md", str(ctx.exception))

    # ------------------------------------------------------------------
    # 2. Rollout Execution and Workspace Sandboxing Tests
    # ------------------------------------------------------------------

    def test_rollout_execution_and_deliverables(self) -> None:
        """Verify isolated rollout directory, trajectory log, and deliverables."""
        run_id = "test_rollout_001"
        run_dir = self.adapter.execute("demo-1", None, run_id)

        self.assertTrue(run_dir.is_dir())
        self.assertEqual(run_dir.name, run_id)

        # Dedicated workspace inside run_dir
        ws_dir = run_dir / "workspace"
        self.assertTrue(ws_dir.is_dir())

        # Trajectory file exists and adheres to grammar
        traj_file = run_dir / "trajectory.md"
        self.assertTrue(traj_file.is_file())
        traj_content = traj_file.read_text(encoding="utf-8")
        self.assertIn("[ACTION]", traj_content)
        self.assertIn("[OBSERVATION]", traj_content)

        # Deliverables
        self.assertTrue(self.adapter.delivered(run_dir))
        deliv_text = self.adapter.deliverable_text(run_dir)
        self.assertTrue(len(deliv_text) > 0)
        deliv_files = self.adapter.deliverable_files(run_dir)
        self.assertIn("answer.txt", deliv_files)

        # Trajectory truncation
        short_traj = self.adapter.trajectory(run_dir, obs_trunc=25)
        self.assertIn("[OBSERVATION TRUNCATED", short_traj)

        # Scoring
        score_dict = self.adapter.score(run_dir, "demo-1")
        self.assertIsNotNone(score_dict)
        self.assertTrue(score_dict["passed"])
        self.assertEqual(score_dict["score"], 1.0)

    def test_ledger_task_rollout_execution(self) -> None:
        """Verify execution of script-based accounting task (ledger-1)."""
        run_id = "test_ledger_001"
        run_dir = self.adapter.execute("ledger-1", None, run_id)

        self.assertTrue(run_dir.is_dir())
        ans_file = run_dir / "answer.txt"
        self.assertTrue(ans_file.is_file())
        ans_text = ans_file.read_text(encoding="utf-8")
        self.assertIn("Total:", ans_text)

        traj = self.adapter.trajectory(run_dir)
        self.assertIn("scripts/summarize.py", traj)
        self.assertTrue(self.adapter.delivered(run_dir))

    # ------------------------------------------------------------------
    # 3. Multi-Rollout Sweep (run_orchestrator_sweep) Tests
    # ------------------------------------------------------------------

    def test_run_orchestrator_sweep_parallel(self) -> None:
        """Verify parallel recursion sweep (Propose-Probe-Compose width k=1)."""
        sweep_work = self.runs_root / "work_parallel"
        res = run_orchestrator_sweep(
            adapter=self.adapter,
            task_id="ledger-1",
            mode="parallel",
            k=1,
            work_dir=sweep_work,
        )

        self.assertTrue(res.ok)
        self.assertEqual(res.mode, "agg")
        self.assertEqual(res.task_id, "ledger-1")
        self.assertEqual(res.k, 1)
        self.assertIn("agg1", res.finals)
        self.assertEqual(len(res.stages), 3)  # inv1, dom1, agg1

        # Check serialization
        res_dict = res.to_dict()
        self.assertEqual(res_dict["ok"], True)
        self.assertEqual(res_dict["finals"], ["agg1"])
        self.assertEqual(len(res_dict["stages"]), 3)

    def test_run_orchestrator_sweep_sequential(self) -> None:
        """Verify sequential recursion sweep (Propose-Probe-Compose depth k=1)."""
        sweep_work = self.runs_root / "work_sequential"
        res = run_orchestrator_sweep(
            adapter=self.adapter,
            task_id="ledger-1",
            mode="sequential",
            k=1,
            work_dir=sweep_work,
        )

        self.assertTrue(res.ok)
        self.assertEqual(res.mode, "seq")
        self.assertEqual(res.task_id, "ledger-1")
        self.assertIn("c1", res.finals)
        self.assertEqual(len(res.stages), 3)

    def test_run_orchestrator_sweep_invalid_mode_raises(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            run_orchestrator_sweep(self.adapter, "demo-1", mode="quantum_branch")
        self.assertIn("Unknown sweep mode", str(ctx.exception))

    # ------------------------------------------------------------------
    # 4. CLI Subcommand Tests (hex harness2-eval)
    # ------------------------------------------------------------------

    def test_cli_harness2_eval_help(self) -> None:
        """Verify hex harness2-eval --help exits cleanly."""
        with self.assertRaises(SystemExit) as cm:
            cli_main(["harness2-eval", "--help"])
        self.assertEqual(cm.exception.code, 0)

    def test_cli_harness2_eval_json_execution(self) -> None:
        """Verify hex harness2-eval --json returns valid structured JSON."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            ret = cli_main([
                "--data-dir", str(self.data_dir),
                "harness2-eval",
                "--task-id", "ledger-1",
                "--runs-root", str(self.runs_root / "cli_runs"),
                "--k", "1",
                "--mode", "parallel",
                "--json",
            ])

        self.assertEqual(ret, 0)
        output = buf.getvalue().strip()
        data = json.loads(output)
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("task_id"), "ledger-1")
        self.assertEqual(data.get("finals"), ["agg1"])

    def test_cli_harness2_eval_summary_table(self) -> None:
        """Verify hex harness2-eval human-readable summary output."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            ret = cli_main([
                "--data-dir", str(self.data_dir),
                "harness2-eval",
                "--task-id", "ledger-1",
                "--runs-root", str(self.runs_root / "cli_table_runs"),
                "--k", "1",
                "--mode", "parallel",
            ])

        self.assertEqual(ret, 0)
        output = buf.getvalue()
        self.assertIn("Google Research Harness² Sweep Evaluation", output)
        self.assertIn("SUCCESS", output)
        self.assertIn("Winning Finals: agg1", output)

    # ------------------------------------------------------------------
    # 5. Regression Safety & Proof Re-export Tests
    # ------------------------------------------------------------------

    def test_proof_reexport(self) -> None:
        """Verify OrchestratorBenchmarkAdapter is re-exported from harness_core.proof."""
        self.assertIs(ProofBenchmarkAdapter, OrchestratorBenchmarkAdapter)


if __name__ == "__main__":
    unittest.main()
