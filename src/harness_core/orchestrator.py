"""Core Autonomous Harness Orchestrator (PRD Sections 6.1–6.8, 8.2, 9, 13).

Coordinates:
- Task intake, policy validation, and state machine transitions (`SUBMITTED` -> `SUCCEEDED`).
- `R0/R1` Fast-Path Express Lane (`<2s` synchronous bypass with full call journaling & proof).
- Standard DAG Planning & Execution inside isolated APFS/CoW workspace snapshots.
- Mid-step crash recovery: restores exact CoW filesystem snapshot and replays sealed tool calls in 0 ms.
- Speculative Execution (`fork` / `SPECULATING`): runs $N$ parallel candidate repair branches on
  isolated CoW clones, verifies each deterministically, promotes the winning branch, and discards losers.
- Cooperative Pause, Resume, and Cancellation with automatic CoW rollback.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .cow import APFSAndPortableCoWProvider
from .db import LedgerStore
from .gateway import ApprovalRequiredInterrupt, PolicyBlockedError, ToolGateway
from .graphify import ActiveGraphifyAdapter
from .models import PlanNode, TaskState, TaskSubmitRequest
from .proof import Harness2ControlledAdapter, VerificationAndProofService
from .router import GovernedMemoryService, TwoTierModelRouter
from .skillspector import SkillSpectorScanner


class HarnessOrchestrator:
    """End-to-end orchestrator wiring Ledger, CoW, Gateway, Graphify, SkillSpector, and Proof services."""

    def __init__(self, data_dir: Path | str, default_workspace: Path | str | None = None):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.default_workspace = Path(default_workspace or self.data_dir / "default_project").resolve()
        self.default_workspace.mkdir(parents=True, exist_ok=True)

        self.ledger = LedgerStore(self.data_dir / "ledger.db")
        self.cow = APFSAndPortableCoWProvider(self.data_dir / "cow_snapshots")
        self.workspaces_root = self.data_dir / "active_workspaces"
        self.workspaces_root.mkdir(parents=True, exist_ok=True)

        self.graphify = ActiveGraphifyAdapter(self.default_workspace)
        self.gateway = ToolGateway(self.ledger, self.graphify)
        self.scanner = SkillSpectorScanner(self.ledger)
        self.router = TwoTierModelRouter()
        self.memory = GovernedMemoryService(self.ledger)
        self.proof_service = VerificationAndProofService(
            self.ledger,
            self.cow,
            self.gateway,
            self.graphify,
            self.data_dir / "cas_artifacts",
        )
        self.harness2_adapter = Harness2ControlledAdapter(
            self.ledger,
            self.scanner,
            self.data_dir / "harness_revisions",
        )

        # Register default local MacBook Air M4 worker
        self.default_worker_id = "wrk_m4_air_local"
        self.ledger.register_worker(
            worker_id=self.default_worker_id,
            machine_class="m4_air",
            cow_backend=self.cow.backend_name,
            capabilities={"apfs_clone": True, "mlx": True, "sandbox": True},
            local_llm_models=[self.router.local_model_id],
            trust_level="trusted_local",
        )

    # ------------------------------------------------------------------
    # Task Submission & Execution Entrypoints
    # ------------------------------------------------------------------

    def submit_and_run(self, req: TaskSubmitRequest, auto_execute: bool = True) -> dict[str, Any]:
        task = self.ledger.submit_task(req)
        if task["status"] != TaskState.SUBMITTED.value:
            return task
        task_id = task["task_id"]

        # Policy validation check (FR-003)
        if not req.goal or not req.goal.strip():
            return self.ledger.transition_task(
                task_id,
                TaskState.REJECTED,
                reason_code="EMPTY_GOAL",
                terminal_reason="Task goal cannot be empty",
            )

        constraints = task.get("constraints") or {}
        if constraints.get("disallowed_policy"):
            return self.ledger.transition_task(
                task_id,
                TaskState.REJECTED,
                reason_code="POLICY_VIOLATION",
                terminal_reason="Task violates configured security policy",
            )

        # Verify pinned skill integrity via SkillSpector if task invokes a skill (FR-039b, FR-039e)
        skill_id = constraints.get("skill_id")
        if skill_id:
            try:
                self.scanner.verify_skill_runtime_integrity(skill_id)
            except Exception as exc:
                return self.ledger.transition_task(
                    task_id,
                    TaskState.REJECTED,
                    reason_code="SKILLSPECTOR_BLOCK",
                    terminal_reason=str(exc),
                )

        self.ledger.transition_task(task_id, TaskState.VALIDATING, reason_code="POLICY_VALIDATED")
        self.ledger.transition_task(task_id, TaskState.QUEUED, reason_code="ENQUEUED")

        if auto_execute:
            return self.execute_task(task_id)
        return self.ledger.get_task(task_id)  # type: ignore[return-value]

    def execute_task(self, task_id: str, worker_id: str | None = None) -> dict[str, Any]:
        task = self.ledger.get_task(task_id)
        if not task:
            raise KeyError(f"Task not found: {task_id}")

        wrk = worker_id or self.default_worker_id
        constraints = task.get("constraints") or {}
        source_ws = Path(constraints.get("workspace_root") or self.default_workspace).resolve()
        source_ws.mkdir(parents=True, exist_ok=True)

        # Fast-Path Express Lane (FR-001a): bypasses DAG planning ceremony for R0/R1 tasks
        if task["execution_mode"] == "fast_path":
            return self._execute_fast_path(task, source_ws, wrk)

        return self._execute_standard_dag(task, source_ws, wrk)

    def _execute_fast_path(self, task: dict[str, Any], source_ws: Path, worker_id: str) -> dict[str, Any]:
        task_id = task["task_id"]
        base_tree_sha = self.cow.compute_tree_sha(source_ws)
        lease_info = self.ledger.acquire_lease_and_attempt(
            task_id,
            worker_id,
            base_tree_sha=base_tree_sha,
            workspace_path=str(source_ws),
        )
        attempt_id = lease_info["attempt_id"]
        fencing_token = lease_info["fencing_token"]

        self.ledger.transition_task(
            task_id,
            TaskState.RUNNING,
            reason_code="FAST_PATH_BYPASS",
            attempt_id=attempt_id,
            workspace_tree_sha=base_tree_sha,
            fencing_token=fencing_token,
            payload={"lane": "R0_R1_FAST_PATH"},
        )

        steps = (task.get("constraints") or {}).get("requested_steps") or [
            {"step_id": "fp_list", "tool_id": "list_files", "args": {}, "risk_class": "R0"},
            {"step_id": "fp_graph", "tool_id": "query_graphify", "args": {}, "risk_class": "R1"},
        ]

        try:
            for idx, st in enumerate(steps, start=1):
                self.gateway.invoke(
                    task_id=task_id,
                    attempt_id=attempt_id,
                    step_id=st.get("step_id", f"fp_{idx}"),
                    sequence_in_step=idx,
                    tool_id=st["tool_id"],
                    args=st.get("args", {}),
                    workspace_dir=source_ws,
                )
        except PolicyBlockedError as exc:
            self.ledger.update_attempt(attempt_id, status="FAILED", failure_class="POLICY_BLOCKED")
            return self.ledger.transition_task(
                task_id,
                TaskState.FAILED,
                reason_code="FAST_PATH_POLICY_BLOCKED",
                attempt_id=attempt_id,
                fencing_token=fencing_token,
                terminal_reason=str(exc),
            )

        self.ledger.transition_task(
            task_id,
            TaskState.VERIFYING,
            reason_code="FAST_PATH_VERIFY",
            attempt_id=attempt_id,
            fencing_token=fencing_token,
        )
        self.proof_service.run_verification_and_build_proof(
            task_id=task_id,
            attempt_id=attempt_id,
            base_snapshot_path=source_ws,
            workspace_dir=source_ws,
            modified_files=[],
            run_tests=False,
        )
        self.ledger.update_attempt(attempt_id, status="SUCCEEDED", head_tree_sha=base_tree_sha)
        return self.ledger.transition_task(
            task_id,
            TaskState.SUCCEEDED,
            reason_code="FAST_PATH_VERIFIED",
            attempt_id=attempt_id,
            workspace_tree_sha=base_tree_sha,
            fencing_token=fencing_token,
        )

    def _execute_standard_dag(self, task: dict[str, Any], source_ws: Path, worker_id: str) -> dict[str, Any]:
        task_id = task["task_id"]
        constraints = task.get("constraints") or {}

        # Check if resuming from an existing valid checkpoint & CoW snapshot (FR-013)
        latest_chk = self.ledger.get_latest_checkpoint(task_id)
        if latest_chk and Path(latest_chk["snapshot_path"]).is_dir():
            base_snap_path = Path(latest_chk["snapshot_path"])
            base_tree_sha = latest_chk["workspace_tree_sha"]
            completed_step_ids = set(latest_chk.get("completed_steps") or [])
        else:
            base_snap = self.cow.create_snapshot(source_ws, label=f"base_{task_id}")
            base_snap_path = base_snap.snapshot_path
            base_tree_sha = base_snap.workspace_tree_sha
            completed_step_ids = set()

        # Create or reuse Plan DAG
        plan = self.ledger.get_latest_plan(task_id)
        if not plan:
            if task["status"] in (TaskState.QUEUED.value, TaskState.NEEDS_ATTENTION.value):
                self.ledger.transition_task(
                    task_id,
                    TaskState.PLANNING,
                    reason_code="BUILDING_DAG",
                    workspace_tree_sha=base_tree_sha,
                )
            req_steps = constraints.get("requested_steps") or [
                PlanNode(
                    step_id="step_1_inspect",
                    title="Inspect repository files",
                    tool_id="list_files",
                    args={},
                    risk_class="R0",
                ).to_dict(),
                PlanNode(
                    step_id="step_2_graph",
                    title="Build AST & call graph via Graphify",
                    tool_id="query_graphify",
                    args={},
                    depends_on=["step_1_inspect"],
                    risk_class="R1",
                ).to_dict(),
            ]
            plan = self.ledger.save_plan(task_id, req_steps, rationale=f"DAG plan for: {task['goal']}")
            self.ledger.transition_task(
                task_id,
                TaskState.READY,
                reason_code="DAG_READY",
                workspace_tree_sha=base_tree_sha,
            )

        # Clone CoW snapshot into an isolated attempt working directory
        attempt_ws = self.workspaces_root / f"ws_{task_id}_{len(self.ledger.list_attempts(task_id)) + 1}"
        self.cow.clone_to_workspace(base_snap_path, attempt_ws)

        lease_info = self.ledger.acquire_lease_and_attempt(
            task_id,
            worker_id,
            base_tree_sha=base_tree_sha,
            workspace_path=str(attempt_ws),
        )
        attempt_id = lease_info["attempt_id"]
        fencing_token = lease_info["fencing_token"]

        cur_task = self.ledger.get_task(task_id)
        assert cur_task is not None
        if cur_task["status"] != TaskState.RUNNING.value:
            self.ledger.transition_task(
                task_id,
                TaskState.RUNNING,
                reason_code="WORKER_LEASE_CLAIMED",
                attempt_id=attempt_id,
                workspace_tree_sha=base_tree_sha,
                fencing_token=fencing_token,
            )

        dag_nodes: list[dict[str, Any]] = list(plan.get("dag") or [])
        modified_files: list[str] = []

        try:
            for idx, node in enumerate(dag_nodes, start=1):
                step_id = node.get("step_id", f"step_{idx}")
                if step_id in completed_step_ids and not node.get("has_unsealed_calls"):
                    continue

                if "code_mode_ops" in node:
                    self.gateway.execute_code_mode_composition(
                        task_id=task_id,
                        attempt_id=attempt_id,
                        step_id=step_id,
                        workspace_dir=attempt_ws,
                        operations=node["code_mode_ops"],
                    )
                    for op in node["code_mode_ops"]:
                        if op.get("tool_id") == "write_file" and "path" in op.get("args", {}):
                            modified_files.append(str(op["args"]["path"]))
                else:
                    tool_id = node["tool_id"]
                    args = node.get("args", {})
                    self.gateway.invoke(
                        task_id=task_id,
                        attempt_id=attempt_id,
                        step_id=step_id,
                        sequence_in_step=1,
                        tool_id=tool_id,
                        args=args,
                        workspace_dir=attempt_ws,
                    )
                    if tool_id == "write_file" and "path" in args:
                        modified_files.append(str(args["path"]))

                if node.get("simulate_crash_after_step"):
                    raise RuntimeError(f"Injected worker crash after step '{step_id}'")

                # Seal step checkpoint and CoW snapshot after each completed step
                completed_step_ids.add(step_id)
                pending_ids = [n.get("step_id", "") for n in dag_nodes if n.get("step_id", "") not in completed_step_ids]
                step_snap = self.cow.create_snapshot(attempt_ws, label=f"{task_id}_{step_id}")
                self.ledger.create_checkpoint(
                    task_id=task_id,
                    attempt_id=attempt_id,
                    plan_revision=plan["revision"],
                    completed_steps=sorted(completed_step_ids),
                    pending_steps=pending_ids,
                    workspace_tree_sha=step_snap.workspace_tree_sha,
                    snapshot_path=str(step_snap.snapshot_path),
                )

        except ApprovalRequiredInterrupt as apr_int:
            # Seal CoW checkpoint and pause in WAITING_APPROVAL
            chk_snap = self.cow.create_snapshot(attempt_ws, label=f"{task_id}_wait_approval")
            self.ledger.create_checkpoint(
                task_id=task_id,
                attempt_id=attempt_id,
                plan_revision=plan["revision"],
                completed_steps=sorted(completed_step_ids),
                pending_steps=[n.get("step_id", "") for n in dag_nodes if n.get("step_id", "") not in completed_step_ids],
                workspace_tree_sha=chk_snap.workspace_tree_sha,
                snapshot_path=str(chk_snap.snapshot_path),
            )
            self.ledger.update_attempt(
                attempt_id,
                status="PAUSED_APPROVAL",
                head_tree_sha=chk_snap.workspace_tree_sha,
            )
            return self.ledger.transition_task(
                task_id,
                TaskState.WAITING_APPROVAL,
                reason_code="R3_APPROVAL_REQUIRED",
                attempt_id=attempt_id,
                workspace_tree_sha=chk_snap.workspace_tree_sha,
                fencing_token=fencing_token,
                payload={"approval_id": apr_int.approval_record["approval_id"]},
            )

        except PolicyBlockedError as pol_err:
            # Roll back uncommitted CoW changes to last valid snapshot
            reverted_sha = self.cow.rollback_workspace(attempt_ws, base_snap_path)
            self.ledger.update_attempt(
                attempt_id,
                status="FAILED",
                head_tree_sha=reverted_sha,
                failure_class="POLICY_BLOCKED",
            )
            return self.ledger.transition_task(
                task_id,
                TaskState.FAILED,
                reason_code="POLICY_DENIED",
                attempt_id=attempt_id,
                workspace_tree_sha=reverted_sha,
                fencing_token=fencing_token,
                terminal_reason=str(pol_err),
            )

        except Exception as crash_exc:
            # Roll back dirty uncommitted files in attempt_ws to latest valid snapshot (FR-012a)
            latest_valid = self.ledger.get_latest_checkpoint(task_id)
            snap_to_restore = Path(latest_valid["snapshot_path"]) if latest_valid else base_snap_path
            reverted_sha = self.cow.rollback_workspace(attempt_ws, snap_to_restore)
            self.ledger.update_attempt(
                attempt_id,
                status="FAILED",
                head_tree_sha=reverted_sha,
                failure_class="STEP_INTERRUPTED",
            )
            return self.ledger.transition_task(
                task_id,
                TaskState.RETRY_WAIT,
                reason_code="STEP_CRASH_COW_REVERTED",
                attempt_id=attempt_id,
                workspace_tree_sha=reverted_sha,
                fencing_token=fencing_token,
                payload={"error": str(crash_exc), "restored_snapshot": str(snap_to_restore)},
            )

        # Transition to VERIFYING and generate Proof-Carrying Artifact Bundle (FR-018, FR-022)
        head_sha = self.cow.compute_tree_sha(attempt_ws)
        self.ledger.transition_task(
            task_id,
            TaskState.VERIFYING,
            reason_code="EXECUTION_COMPLETE_VERIFYING",
            attempt_id=attempt_id,
            workspace_tree_sha=head_sha,
            fencing_token=fencing_token,
        )

        ver_outcome = self.proof_service.run_verification_and_build_proof(
            task_id=task_id,
            attempt_id=attempt_id,
            base_snapshot_path=base_snap_path,
            workspace_dir=attempt_ws,
            modified_files=modified_files,
            run_tests=True,
        )

        if not ver_outcome["passed"]:
            self.ledger.update_attempt(
                attempt_id,
                status="FAILED",
                head_tree_sha=head_sha,
                failure_class="VERIFICATION_FAILED",
            )
            return self.ledger.transition_task(
                task_id,
                TaskState.NEEDS_ATTENTION,
                reason_code="VERIFICATION_CHECKS_FAILED",
                attempt_id=attempt_id,
                workspace_tree_sha=head_sha,
                fencing_token=fencing_token,
                payload={"verification": ver_outcome["verification_results"]},
            )

        # Sync verified workspace back to source_ws if R2 modifications succeeded
        if modified_files:
            self.cow.clone_to_workspace(attempt_ws, source_ws)

        self.ledger.update_attempt(attempt_id, status="SUCCEEDED", head_tree_sha=head_sha)
        # Record episodic memory summary (FR-032)
        self.memory.write_memory(
            scope="episodic",
            project_id=task.get("project_id") or "default",
            subject=f"Task {task_id} succeeded",
            content=f"Goal '{task['goal']}' verified with proof {ver_outcome['proof']['proof_id']} and tree SHA {head_sha[:12]}.",
            provenance={"task_id": task_id, "proof_id": ver_outcome["proof"]["proof_id"]},
        )

        return self.ledger.transition_task(
            task_id,
            TaskState.SUCCEEDED,
            reason_code="PROOF_VERIFIED",
            attempt_id=attempt_id,
            workspace_tree_sha=head_sha,
            fencing_token=fencing_token,
            payload={"proof_id": ver_outcome["proof"]["proof_id"]},
        )

    # ------------------------------------------------------------------
    # Pause, Resume, Cancel, and Speculative Branching (`fork`)
    # ------------------------------------------------------------------

    def pause_task(self, task_id: str, actor: str = "owner") -> dict[str, Any]:
        task = self.ledger.get_task(task_id)
        if not task:
            raise KeyError(f"Task not found: {task_id}")
        return self.ledger.transition_task(
            task_id,
            TaskState.PAUSED,
            reason_code="COOPERATIVE_PAUSE_REQUESTED",
            actor=actor,
        )

    def resume_task(self, task_id: str, worker_id: str | None = None) -> dict[str, Any]:
        task = self.ledger.get_task(task_id)
        if not task:
            raise KeyError(f"Task not found: {task_id}")
        if task["status"] in (
            TaskState.PAUSED.value,
            TaskState.RETRY_WAIT.value,
            TaskState.WAITING_APPROVAL.value,
            TaskState.NEEDS_ATTENTION.value,
        ):
            self.ledger.transition_task(
                task_id,
                TaskState.QUEUED,
                reason_code="RESUMED_FROM_CHECKPOINT",
            )
        return self.execute_task(task_id, worker_id=worker_id)

    def cancel_task(self, task_id: str, actor: str = "owner") -> dict[str, Any]:
        """FR-016: Cooperative cancel + CoW rollback of uncommitted workspace layer."""
        task = self.ledger.get_task(task_id)
        if not task:
            raise KeyError(f"Task not found: {task_id}")
        chk = self.ledger.get_latest_checkpoint(task_id)
        attempts = self.ledger.list_attempts(task_id)
        reverted_sha = task.get("active_workspace_tree_sha")
        if attempts and chk and Path(chk["snapshot_path"]).is_dir():
            last_ws = attempts[-1].get("workspace_path")
            if last_ws and Path(last_ws).is_dir():
                reverted_sha = self.cow.rollback_workspace(last_ws, chk["snapshot_path"])
        return self.ledger.transition_task(
            task_id,
            TaskState.CANCELED,
            reason_code="CANCELED_AND_COW_ROLLED_BACK",
            actor=actor,
            workspace_tree_sha=reverted_sha,
            terminal_reason="Canceled by operator; uncommitted CoW layer rolled back cleanly",
            payload={"clean_cancellation": True},
        )

    def fork_speculative_branches(
        self,
        task_id: str,
        branch_strategies: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """FR-021a & Stage 4 Acceptance: Forks a checkpoint's CoW snapshot into parallel
        speculative `attempt_id` branches, runs deterministic verification on each in isolation,
        commits the passing branch, and garbage-collects the losing CoW snapshot(s).
        """
        task = self.ledger.get_task(task_id)
        if not task:
            raise KeyError(f"Task not found: {task_id}")

        constraints = task.get("constraints") or {}
        source_ws = Path(constraints.get("workspace_root") or self.default_workspace).resolve()
        chk = self.ledger.get_latest_checkpoint(task_id)
        if chk and Path(chk["snapshot_path"]).is_dir():
            base_snap_path = Path(chk["snapshot_path"])
            base_sha = chk["workspace_tree_sha"]
        else:
            snap = self.cow.create_snapshot(source_ws, label=f"fork_base_{task_id}")
            base_snap_path = snap.snapshot_path
            base_sha = snap.workspace_tree_sha

        if task["status"] in (TaskState.SUBMITTED.value, TaskState.VALIDATING.value, TaskState.QUEUED.value, TaskState.READY.value, TaskState.NEEDS_ATTENTION.value):
            if task["status"] == TaskState.QUEUED.value:
                self.ledger.transition_task(task_id, TaskState.READY, reason_code="PREPARE_SPECULATION")
            self.ledger.transition_task(task_id, TaskState.SPECULATING, reason_code="FORKED_SPECULATIVE_BRANCHES")
        elif task["status"] == TaskState.RUNNING.value:
            self.ledger.transition_task(task_id, TaskState.SPECULATING, reason_code="FORKED_SPECULATIVE_BRANCHES")

        def _run_single_branch(idx: int, strategy: dict[str, Any]) -> dict[str, Any]:
            label = strategy.get("label", f"branch_{idx}")
            branch_ws = self.workspaces_root / f"spec_{task_id}_{label}"
            self.cow.clone_to_workspace(base_snap_path, branch_ws)

            lease = self.ledger.acquire_lease_and_attempt(
                task_id,
                self.default_worker_id,
                base_tree_sha=base_sha,
                workspace_path=str(branch_ws),
                parent_attempt_id=chk["attempt_id"] if chk else None,
                branch_label=label,
            )
            att_id = lease["attempt_id"]
            mod_files: list[str] = []

            for s_idx, step in enumerate(strategy.get("steps", []), start=1):
                self.gateway.invoke(
                    task_id=task_id,
                    attempt_id=att_id,
                    step_id=f"{label}_s{s_idx}",
                    sequence_in_step=s_idx,
                    tool_id=step["tool_id"],
                    args=step.get("args", {}),
                    workspace_dir=branch_ws,
                )
                if step["tool_id"] == "write_file" and "path" in step.get("args", {}):
                    mod_files.append(str(step["args"]["path"]))

            ver = self.proof_service.run_verification_and_build_proof(
                task_id=task_id,
                attempt_id=att_id,
                base_snapshot_path=base_snap_path,
                workspace_dir=branch_ws,
                modified_files=mod_files,
                run_tests=True,
            )
            head_sha = self.cow.compute_tree_sha(branch_ws)
            return {
                "branch_label": label,
                "attempt_id": att_id,
                "workspace_path": str(branch_ws),
                "head_tree_sha": head_sha,
                "passed": ver["passed"],
                "proof": ver["proof"],
                "verification_results": ver["verification_results"],
            }

        with ThreadPoolExecutor(max_workers=max(1, len(branch_strategies))) as pool:
            outcomes = list(
                pool.map(lambda item: _run_single_branch(item[0], item[1]), enumerate(branch_strategies, start=1))
            )

        self.ledger.transition_task(task_id, TaskState.VERIFYING, reason_code="EVALUATING_SPECULATIVE_BRANCHES")

        winner = next((o for o in outcomes if o["passed"]), None)
        discarded: list[str] = []
        for o in outcomes:
            if winner and o["attempt_id"] == winner["attempt_id"]:
                self.ledger.update_attempt(o["attempt_id"], status="SUCCEEDED", head_tree_sha=o["head_tree_sha"])
            else:
                self.ledger.update_attempt(
                    o["attempt_id"],
                    status="DISCARDED",
                    head_tree_sha=o["head_tree_sha"],
                    failure_class="SPECULATIVE_LOSER",
                )
                # Cleanly garbage-collect losing branch CoW workspace
                self.cow.discard_snapshot(o["workspace_path"])
                import shutil
                shutil.rmtree(o["workspace_path"], ignore_errors=True)
                discarded.append(o["branch_label"])

        if winner:
            self.cow.clone_to_workspace(winner["workspace_path"], source_ws)
            final_task = self.ledger.transition_task(
                task_id,
                TaskState.SUCCEEDED,
                reason_code="SPECULATIVE_WINNER_PROMOTED",
                attempt_id=winner["attempt_id"],
                workspace_tree_sha=winner["head_tree_sha"],
                payload={
                    "winning_branch": winner["branch_label"],
                    "winning_attempt_id": winner["attempt_id"],
                    "discarded_branches": discarded,
                    "proof_id": winner["proof"]["proof_id"],
                },
            )
        else:
            final_task = self.ledger.transition_task(
                task_id,
                TaskState.NEEDS_ATTENTION,
                reason_code="ALL_SPECULATIVE_BRANCHES_FAILED",
                payload={"discarded_branches": discarded},
            )

        return {
            "task": final_task,
            "winning_branch": winner["branch_label"] if winner else None,
            "branches": outcomes,
            "discarded_branches": discarded,
        }
