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

import collections
import json
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
import tempfile
from pathlib import Path
from typing import Any, Callable

from .cow import APFSAndPortableCoWProvider, WorkspaceSnapshotProvider, get_default_snapshot_provider
from .db import LedgerStore, utcnow_iso
from .gateway import ApprovalRequiredInterrupt, PolicyBlockedError, ToolGateway
from .graphify import ActiveGraphifyAdapter
from .models import PlanNode, TaskBudget, TaskState, TaskSubmitRequest
from .proof import Harness2ControlledAdapter, VerificationAndProofService
from .providers.base import BudgetExceededError
from .router import GovernedMemoryService, TwoTierModelRouter
from .sandbox import ExecutionSandbox, get_default_sandbox
from .skillspector import SkillSpectorScanner
from .webhooks import WebhookConfig, WebhookNotifier


class HarnessOrchestrator:
    """End-to-end orchestrator wiring Ledger, CoW, Gateway, Graphify, SkillSpector, and Proof services."""

    def __init__(
        self,
        data_dir: Path | str,
        default_workspace: Path | str | None = None,
        cow: WorkspaceSnapshotProvider | None = None,
        sandbox: ExecutionSandbox | None = None,
        repair_patch_generator: Callable[..., list[dict[str, Any]]] | None = None,
        webhook_notifier: WebhookNotifier | None = None,
    ):
        if str(data_dir) == ":memory:":
            self._temp_mem_dir = tempfile.TemporaryDirectory(prefix="harness_mem_")
            self.data_dir = Path(self._temp_mem_dir.name).resolve()
        else:
            self._temp_mem_dir = None
            self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.default_workspace = Path(default_workspace or self.data_dir / "default_project").resolve()
        self.default_workspace.mkdir(parents=True, exist_ok=True)
        self.repair_patch_generator = repair_patch_generator
        self.webhook_notifier = (
            webhook_notifier if webhook_notifier is not None else WebhookNotifier(WebhookConfig.from_env())
        )

        self.ledger = LedgerStore(self.data_dir / "ledger.db")
        self.cow = cow or get_default_snapshot_provider(self.data_dir / "cow_snapshots")
        self.workspaces_root = self.data_dir / "active_workspaces"
        self.workspaces_root.mkdir(parents=True, exist_ok=True)

        self.sandbox = sandbox or get_default_sandbox(self.workspaces_root)
        self.graphify = ActiveGraphifyAdapter(self.default_workspace)
        self.gateway = ToolGateway(
            self.ledger,
            self.graphify,
            sandbox=self.sandbox,
            notifier=self.webhook_notifier,
        )
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
            capabilities={
                "apfs_clone": True,
                "mlx": True,
                "sandbox": True,
                "sandbox_backend": self.sandbox.backend_name,
            },
            local_llm_models=[self.router.local_model_id],
            trust_level="trusted_local",
        )

    # ------------------------------------------------------------------
    # Task Submission & Execution Entrypoints
    # ------------------------------------------------------------------

    def submit_task(self, req: TaskSubmitRequest, auto_execute: bool = False) -> dict[str, Any]:
        return self.submit_and_run(req, auto_execute=auto_execute)

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

    def execute_task(
        self,
        task_id: str,
        worker_id: str | None = None,
        lease_info: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        task = self.ledger.get_task(task_id)
        if not task:
            raise KeyError(f"Task not found: {task_id}")

        wrk = worker_id or self.default_worker_id
        constraints = task.get("constraints") or {}
        source_ws = Path(constraints.get("workspace_root") or self.default_workspace).resolve()
        source_ws.mkdir(parents=True, exist_ok=True)

        # Fast-Path Express Lane (FR-001a): bypasses DAG planning ceremony for R0/R1 tasks
        if task["execution_mode"] == "fast_path":
            return self._execute_fast_path(task, source_ws, wrk, lease_info=lease_info)

        return self._execute_standard_dag(task, source_ws, wrk, lease_info=lease_info)

    def _execute_fast_path(
        self,
        task: dict[str, Any],
        source_ws: Path,
        worker_id: str,
        lease_info: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        task_id = task["task_id"]
        base_tree_sha = self.cow.compute_tree_sha(source_ws)
        if lease_info is None:
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

    def generate_repair_patch(
        self,
        task_id: str,
        attempt_ws: Path,
        failure_digest: str,
        blast_report: Any,
        modified_files: list[str],
    ) -> list[dict[str, Any]]:
        """Generates concrete repair patch operations using model router or injected generator."""
        if self.repair_patch_generator is not None:
            try:
                return self.repair_patch_generator(
                    task_id=task_id,
                    attempt_ws=attempt_ws,
                    failure_digest=failure_digest,
                    blast_report=blast_report,
                    modified_files=modified_files,
                )
            except TypeError:
                try:
                    return self.repair_patch_generator(
                        task_id, attempt_ws, failure_digest, blast_report, modified_files
                    )
                except TypeError:
                    return self.repair_patch_generator(failure_digest)

        # Formulate repair prompt with failure context, blast radius, and failing code
        blast_dict = blast_report.to_dict() if hasattr(blast_report, "to_dict") else {}
        impacted_symbols = blast_dict.get("modified_symbols", []) + blast_dict.get("hop1_dependents", [])
        blast_warning = blast_dict.get("warning_message", "No AST blast warnings")

        # Read context for modified or failing files
        target_files = list(modified_files)
        for match in re.findall(r"([\w./\-]+\.py)", failure_digest):
            clean_p = match.strip("'\"")
            if clean_p not in target_files:
                target_files.append(clean_p)

        file_contexts: list[str] = []
        for file_rel in target_files:
            file_path = attempt_ws / file_rel
            if file_path.is_file():
                try:
                    file_contexts.append(f"--- File: {file_rel} ---\n{file_path.read_text(encoding='utf-8')}")
                except Exception:
                    pass

        code_context_str = "\n\n".join(file_contexts) if file_contexts else "No existing code files found."

        # Ordered stable -> context -> volatile tiers for prompt prefix caching
        stable_system_prompt = (
            "You are an autonomous software repair agent.\n"
            "A verification test failed during execution. Fix the failing code.\n"
            "Instructions:\n"
            "Provide the repair patch as a JSON list of file write operations:\n"
            "```json\n"
            '[\n  {"tool_id": "write_file", "args": {"path": "<relative_path>", "content": "<file_content>"}}\n]\n'
            "```\n"
            "Provide only valid code in the content."
        )
        cacheable_context_prefix = (
            f"[AST Blast Radius Report]\nWarning: {blast_warning}\nImpacted Symbols: {impacted_symbols}\n\n"
            f"[Source Code Context]\n{code_context_str}"
        )
        volatile_failure_prompt = f"[Failure Digest]\n{failure_digest}"

        task = self.ledger.get_task(task_id) or {}
        budget_dict = task.get("budget") or {}
        task_budget = TaskBudget.from_dict(budget_dict)

        decision, model_response = self.router.execute_routed_request(
            prompt=volatile_failure_prompt,
            system_prompt=stable_system_prompt,
            cacheable_prefix=cacheable_context_prefix,
            enable_prompt_caching=True,
            task_class="repair",
            risk_class="R2",
            budget=task_budget,
        )

        response_text = model_response.text.strip()
        return self._parse_patch_operations(response_text, target_files)

    def _parse_patch_operations(self, text: str, fallback_files: list[str]) -> list[dict[str, Any]]:
        """Parses model response into concrete write_file tool operations."""
        json_matches = re.findall(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
        candidates = json_matches if json_matches else [text]

        for cand in candidates:
            cand = cand.strip()
            start_bracket = cand.find("[")
            end_bracket = cand.rfind("]")
            if start_bracket != -1 and end_bracket > start_bracket:
                cand_sub = cand[start_bracket : end_bracket + 1]
                try:
                    parsed = json.loads(cand_sub)
                    if isinstance(parsed, list) and parsed:
                        ops = []
                        for item in parsed:
                            if isinstance(item, dict):
                                if "tool_id" in item and "args" in item:
                                    ops.append(item)
                                elif "path" in item:
                                    ops.append({
                                        "tool_id": item.get("tool_id", "write_file"),
                                        "args": {
                                            "path": item["path"],
                                            "content": item.get("content", ""),
                                        },
                                    })
                        if ops:
                            return ops
                except json.JSONDecodeError:
                    pass

        py_matches = re.findall(r"```(?:python)?\s*(.*?)\s*```", text, re.DOTALL)
        if py_matches and fallback_files:
            return [{
                "tool_id": "write_file",
                "args": {
                    "path": fallback_files[0],
                    "content": py_matches[0],
                },
            }]

        return []

    def replan_dag(
        self,
        task_id: str,
        current_plan: dict[str, Any],
        mutated_step_id: str,
        actual_modified_files: list[str],
        attempt_ws: Path | str,
        *,
        completed_step_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        """Dynamically replans active DAG and selectively invalidates downstream dependent steps (FR-012b).

        1. Analyzes downstream reachability from mutated_step_id and file overlaps.
        2. Selectively invalidates downstream steps while preserving independent completed steps.
        3. Pins newly impacted tests discovered by Active Graphify to blast_radius_checks.
        4. Saves monotonically incremented plan revision and logs hash-chained PLAN_REVISED event.
        """
        attempt_ws_path = Path(attempt_ws)
        raw_dag = current_plan.get("dag") or current_plan.get("steps") or []
        dag_nodes: list[dict[str, Any]] = [
            n.to_dict() if hasattr(n, "to_dict") else dict(n)
            for n in raw_dag
        ]

        # 1. Construct directed child adjacency graph: parent_step -> list of dependent_steps
        children_map: dict[str, list[str]] = collections.defaultdict(list)
        for node in dag_nodes:
            sid = node.get("step_id")
            for dep in node.get("depends_on") or []:
                children_map[dep].append(sid)

        # 2. Downstream reachability traversal (BFS) from mutated_step_id
        downstream_ids: set[str] = set()
        queue: collections.deque[str] = collections.deque(children_map.get(mutated_step_id, []))
        while queue:
            curr = queue.popleft()
            if curr not in downstream_ids:
                downstream_ids.add(curr)
                for child in children_map.get(curr, []):
                    if child not in downstream_ids:
                        queue.append(child)

        # 3. Dynamic Blast-Radius Test Pinning via Active Graphify
        impacted_tests: list[str] = []
        if attempt_ws_path.is_dir() and actual_modified_files:
            try:
                blast_report = self.graphify.compute_blast_radius(attempt_ws_path, actual_modified_files)
                impacted_tests = list(blast_report.impacted_tests or [])
            except Exception:
                pass

        # Check if any step's input files, targets, or side effects overlap with actual_modified_files or impacted_tests
        norm_modified = {p.replace("\\", "/").lstrip("./") for p in actual_modified_files}
        norm_tests = {p.replace("\\", "/").lstrip("./") for p in impacted_tests}

        directly_affected: set[str] = set()
        for node in dag_nodes:
            sid = node.get("step_id")
            if not sid or sid == mutated_step_id:
                continue
            step_files: set[str] = set()
            args = node.get("args") or {}
            for k in ("path", "target", "file", "filename"):
                if k in args and isinstance(args[k], str):
                    step_files.add(args[k].replace("\\", "/").lstrip("./"))
            for k in ("targets", "inputs", "files", "side_effects"):
                val = args.get(k) or node.get(k)
                if isinstance(val, list):
                    for item in val:
                        if isinstance(item, str):
                            step_files.add(item.replace("\\", "/").lstrip("./"))
            if (step_files & norm_modified) or (step_files & norm_tests):
                directly_affected.add(sid)

        for aff_id in directly_affected:
            if aff_id not in downstream_ids:
                downstream_ids.add(aff_id)
                q: collections.deque[str] = collections.deque(children_map.get(aff_id, []))
                while q:
                    curr = q.popleft()
                    if curr not in downstream_ids:
                        downstream_ids.add(curr)
                        for child in children_map.get(curr, []):
                            if child not in downstream_ids:
                                q.append(child)

        # 4. Selective Invalidation:
        # Mark downstream steps as INVALIDATED and remove from completed_step_ids;
        # Preserve independent completed steps as COMPLETED.
        if completed_step_ids is None:
            completed_set = {
                n.get("step_id") for n in dag_nodes if n.get("status") == "COMPLETED"
            }
            completed_set.add(mutated_step_id)
        else:
            completed_set = completed_step_ids
            completed_set.add(mutated_step_id)

        for node in dag_nodes:
            sid = node.get("step_id")
            if sid in downstream_ids:
                node["status"] = "INVALIDATED"
                completed_set.discard(sid)
            elif sid in completed_set or sid == mutated_step_id or node.get("status") == "COMPLETED":
                node["status"] = "COMPLETED"
                completed_set.add(sid)
            else:
                node["status"] = node.get("status", "PENDING")

        preserved_ids = sorted(completed_set - downstream_ids)

        # 5. Merge blast radius test checks
        existing_checks = list(current_plan.get("blast_radius_checks") or [])
        combined_checks = sorted(set(existing_checks) | set(impacted_tests))

        # 6. Save monotonic plan revision
        new_plan = self.ledger.save_plan(
            task_id,
            dag_nodes,
            blast_radius_checks=combined_checks,
            rationale=f"Dynamic replan triggered by step {mutated_step_id}",
        )

        # 7. Record hash-chained PLAN_REVISED task event
        tree_sha = None
        if attempt_ws_path.is_dir():
            try:
                tree_sha = self.cow.compute_tree_sha(attempt_ws_path)
            except Exception:
                pass

        self.ledger.record_event(
            task_id,
            event_type="PLAN_REVISED",
            reason_code="SELECTIVE_SUBGRAPH_INVALIDATION",
            workspace_tree_sha=tree_sha,
            payload={
                "mutated_step_id": mutated_step_id,
                "invalidated_step_ids": sorted(downstream_ids),
                "preserved_step_ids": sorted(preserved_ids),
                "pinned_blast_radius_tests": combined_checks,
                "new_revision": new_plan["revision"],
            },
        )

        return new_plan

    def export_workspace_delta(self, task_id: str, attempt_ws: Path | str) -> dict[str, Any]:
        """Exports workspace delta into a deterministic CAS tar.gz archive and records artifact in ledger."""
        ws_path = Path(attempt_ws).resolve()
        tree_sha = self.cow.compute_tree_sha(ws_path)
        cas_dir = self.data_dir / "cas"
        cas_dir.mkdir(parents=True, exist_ok=True)
        tmp_archive = cas_dir / f"tmp_{uuid.uuid4().hex}.tar.gz"
        archive_sha = self.cow.export_delta_archive(ws_path, tmp_archive)
        dest_archive = cas_dir / f"{archive_sha}.tar.gz"
        if dest_archive.exists():
            tmp_archive.unlink(missing_ok=True)
        else:
            tmp_archive.rename(dest_archive)

        artifact = self.ledger.record_artifact(
            task_id=task_id,
            kind="cow_delta_archive",
            uri=str(dest_archive),
            sha256=archive_sha,
            size_bytes=dest_archive.stat().st_size,
            media_type="application/gzip",
            sensitivity="internal",
        )
        now = utcnow_iso()
        with self.ledger._lock:
            if self.ledger.backend_type == "postgres":
                self.ledger._run_sync(
                    self.ledger._execute_pg,
                    "UPDATE tasks SET active_workspace_tree_sha = $1, updated_at = $2 WHERE task_id = $3",
                    tree_sha,
                    now,
                    task_id,
                )
            else:
                self.ledger.conn.execute(
                    "UPDATE tasks SET active_workspace_tree_sha = ?, updated_at = ? WHERE task_id = ?",
                    (tree_sha, now, task_id),
                )
                self.ledger.conn.commit()

        return {
            "artifact": artifact,
            "artifact_id": artifact["artifact_id"],
            "archive_path": str(dest_archive),
            "archive_sha": archive_sha,
            "workspace_tree_sha": tree_sha,
        }

    def reconstruct_workspace_from_delta(self, task_id: str, target_ws: Path | str) -> str:
        """Reconstructs workspace directory from the latest CAS delta archive and verifies Merkle tree SHA."""
        target_path = Path(target_ws).resolve()
        task = self.ledger.get_task(task_id)
        if not task:
            raise KeyError(f"Task not found: {task_id}")

        artifacts = [a for a in self.ledger.list_artifacts(task_id) if a.get("kind") == "cow_delta_archive"]
        if not artifacts:
            chk = self.ledger.get_latest_checkpoint(task_id)
            if chk and chk.get("artifact_uri") and Path(chk["artifact_uri"]).is_file():
                archive_path = Path(chk["artifact_uri"]).resolve()
            else:
                raise FileNotFoundError(f"No CoW delta archive artifact found for task {task_id}")
        else:
            archive_path = Path(artifacts[-1]["uri"]).resolve()

        if not archive_path.is_file():
            raise FileNotFoundError(f"CoW delta archive file missing on disk: {archive_path}")

        reconstructed_sha = self.cow.import_delta_archive(archive_path, target_path)
        expected_sha = task.get("active_workspace_tree_sha")
        if expected_sha and reconstructed_sha != expected_sha:
            raise ValueError(
                f"Tree SHA mismatch during workspace reconstruction: expected {expected_sha}, got {reconstructed_sha}"
            )
        return reconstructed_sha

    def _execute_standard_dag(
        self,
        task: dict[str, Any],
        source_ws: Path,
        worker_id: str,
        lease_info: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        task_id = task["task_id"]
        constraints = task.get("constraints") or {}

        attempt_ws = self.workspaces_root / f"ws_{task_id}_{len(self.ledger.list_attempts(task_id)) + 1}"
        latest_chk = self.ledger.get_latest_checkpoint(task_id)
        delta_artifacts = [a for a in self.ledger.list_artifacts(task_id) if a.get("kind") == "cow_delta_archive"]

        # Check if resuming from an existing CAS delta archive or valid local checkpoint (FR-012/FR-013)
        if delta_artifacts and Path(delta_artifacts[-1]["uri"]).is_file():
            latest_delta = delta_artifacts[-1]
            archive_path = Path(latest_delta["uri"]).resolve()
            reconstructed_tree_sha = self.cow.import_delta_archive(archive_path, attempt_ws)
            expected_sha = task.get("active_workspace_tree_sha") or (latest_chk.get("workspace_tree_sha") if latest_chk else None)
            if expected_sha and reconstructed_tree_sha != expected_sha:
                raise ValueError(
                    f"Tree SHA mismatch during workspace reconstruction: expected {expected_sha}, got {reconstructed_tree_sha}"
                )
            base_tree_sha = reconstructed_tree_sha
            base_snap = self.cow.create_snapshot(attempt_ws, label=f"reconstructed_{task_id}")
            base_snap_path = base_snap.snapshot_path
            completed_step_ids = set(latest_chk.get("completed_steps") or []) if latest_chk else set()
        elif latest_chk and Path(latest_chk["snapshot_path"]).is_dir():
            base_snap_path = Path(latest_chk["snapshot_path"])
            base_tree_sha = latest_chk["workspace_tree_sha"]
            completed_step_ids = set(latest_chk.get("completed_steps") or [])
            self.cow.clone_to_workspace(base_snap_path, attempt_ws)
        else:
            base_snap = self.cow.create_snapshot(source_ws, label=f"base_{task_id}")
            base_snap_path = base_snap.snapshot_path
            base_tree_sha = base_snap.workspace_tree_sha
            completed_step_ids = set()
            self.cow.clone_to_workspace(base_snap_path, attempt_ws)

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

        if lease_info is None:
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
        for n in dag_nodes:
            if n.get("status") == "COMPLETED":
                completed_step_ids.add(n.get("step_id"))
            elif n.get("status") == "INVALIDATED":
                completed_step_ids.discard(n.get("step_id"))

        modified_files: list[str] = []
        replan_count = 0
        max_replans = 20

        try:
            while True:
                # Find the next runnable node: not yet completed, with all depends_on completed
                runnable_node = None
                for n in dag_nodes:
                    sid = n.get("step_id")
                    if sid in completed_step_ids and not n.get("has_unsealed_calls"):
                        continue
                    deps = n.get("depends_on") or []
                    if all(d in completed_step_ids for d in deps):
                        runnable_node = n
                        break

                if runnable_node is None:
                    pending = [n for n in dag_nodes if n.get("step_id") not in completed_step_ids]
                    if not pending:
                        break  # All nodes executed successfully
                    runnable_node = pending[0]

                node = runnable_node
                step_id = node.get("step_id")

                step_modified_files: list[str] = []
                if "code_mode_ops" in node:
                    self.gateway.execute_code_mode_composition(
                        task_id=task_id,
                        attempt_id=attempt_id,
                        step_id=step_id,
                        workspace_dir=attempt_ws,
                        operations=node["code_mode_ops"],
                        fencing_token=fencing_token,
                    )
                    for op in node["code_mode_ops"]:
                        if op.get("tool_id") == "write_file" and "path" in op.get("args", {}):
                            p = str(op["args"]["path"])
                            step_modified_files.append(p)
                            if p not in modified_files:
                                modified_files.append(p)
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
                        fencing_token=fencing_token,
                    )
                    if tool_id == "write_file" and "path" in args:
                        p = str(args["path"])
                        step_modified_files.append(p)
                        if p not in modified_files:
                            modified_files.append(p)

                if "actual_modified_files" in node:
                    for f in node["actual_modified_files"]:
                        p = str(f)
                        if p not in step_modified_files:
                            step_modified_files.append(p)
                        if p not in modified_files:
                            modified_files.append(p)

                if node.get("simulate_crash_after_step"):
                    raise RuntimeError(f"Injected worker crash after step '{step_id}'")

                # Seal step checkpoint and CoW snapshot after each completed step
                completed_step_ids.add(step_id)
                node["status"] = "COMPLETED"

                # Check for unexpected side-effects
                declared = {
                    p.replace("\\", "/").lstrip("./")
                    for p in (node.get("side_effects") or [])
                }
                actual = {
                    p.replace("\\", "/").lstrip("./")
                    for p in step_modified_files
                }
                unexpected_side_effects = bool(actual - declared)
                explicit_replan = bool(node.get("trigger_replan") or node.get("args", {}).get("trigger_replan"))

                # Check if Active Graphify discovers new downstream tests/callers not yet pinned
                blast_new_callers = False
                if step_modified_files and attempt_ws.is_dir():
                    try:
                        b_rep = self.graphify.compute_blast_radius(attempt_ws, step_modified_files)
                        existing_pins = set(plan.get("blast_radius_checks") or [])
                        if set(b_rep.impacted_tests) - existing_pins:
                            blast_new_callers = True
                    except Exception:
                        pass

                if unexpected_side_effects or explicit_replan or blast_new_callers:
                    if replan_count < max_replans:
                        replan_count += 1
                        plan = self.replan_dag(
                            task_id=task_id,
                            current_plan=plan,
                            mutated_step_id=step_id,
                            actual_modified_files=list(step_modified_files),
                            attempt_ws=attempt_ws,
                            completed_step_ids=completed_step_ids,
                        )
                        dag_nodes = list(plan.get("dag") or [])

                pending_ids = [n.get("step_id", "") for n in dag_nodes if n.get("step_id", "") not in completed_step_ids]
                step_snap = self.cow.create_snapshot(attempt_ws, label=f"{task_id}_{step_id}")
                delta_info = self.export_workspace_delta(task_id, attempt_ws)
                self.ledger.create_checkpoint(
                    task_id=task_id,
                    attempt_id=attempt_id,
                    plan_revision=plan["revision"],
                    completed_steps=sorted(completed_step_ids),
                    pending_steps=pending_ids,
                    workspace_tree_sha=step_snap.workspace_tree_sha,
                    snapshot_path=str(step_snap.snapshot_path),
                    artifact_uri=delta_info.get("archive_path"),
                )

        except ApprovalRequiredInterrupt as apr_int:
            # Seal CoW checkpoint and pause in WAITING_APPROVAL
            chk_snap = self.cow.create_snapshot(attempt_ws, label=f"{task_id}_wait_approval")
            delta_info = self.export_workspace_delta(task_id, attempt_ws)
            self.ledger.create_checkpoint(
                task_id=task_id,
                attempt_id=attempt_id,
                plan_revision=plan["revision"],
                completed_steps=sorted(completed_step_ids),
                pending_steps=[n.get("step_id", "") for n in dag_nodes if n.get("step_id", "") not in completed_step_ids],
                workspace_tree_sha=chk_snap.workspace_tree_sha,
                snapshot_path=str(chk_snap.snapshot_path),
                artifact_uri=delta_info.get("archive_path"),
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

        repair_attempt = 0
        task_budget = TaskBudget.from_dict(task.get("budget"))
        constraints = task.get("constraints") or {}
        max_repair_retries = int(
            constraints.get("max_repair_attempts")
            or task_budget.max_retries_per_step
            or 2
        )
        budget_exceeded = False

        while not ver_outcome["passed"] and repair_attempt < max_repair_retries:
            # Check budget limits
            tracker = getattr(self.router.cloud_provider, "budget_tracker", None)
            if tracker is not None:
                tracker.sync_with_task_budget(task_budget)
                if tracker.total_cost_usd >= tracker.max_cost_usd:
                    budget_exceeded = True
                    break
            if task_budget.max_cost_usd is not None and task_budget.max_cost_usd <= 0.0001:
                budget_exceeded = True
                break

            repair_attempt += 1

            raw_output = (
                ver_outcome.get("verification_results", {}).get("test_output_excerpt", "")
            )
            comp_result = self.router.compress_verbose_log(raw_output)
            failure_digest = comp_result.get("compressed_text", raw_output)

            blast_report = self.graphify.compute_blast_radius(attempt_ws, modified_files)

            # Transition state machine: VERIFYING -> RUNNING
            curr_sha = self.cow.compute_tree_sha(attempt_ws)
            self.ledger.transition_task(
                task_id,
                TaskState.RUNNING,
                reason_code="AUTONOMOUS_REPAIR_ATTEMPT",
                attempt_id=attempt_id,
                workspace_tree_sha=curr_sha,
                fencing_token=fencing_token,
                payload={
                    "repair_attempt": repair_attempt,
                    "max_retries": max_repair_retries,
                    "failure_digest": failure_digest[:500],
                },
            )

            try:
                patch_ops = self.generate_repair_patch(
                    task_id=task_id,
                    attempt_ws=attempt_ws,
                    failure_digest=failure_digest,
                    blast_report=blast_report,
                    modified_files=modified_files,
                )
            except BudgetExceededError:
                budget_exceeded = True
                break

            # Apply repair patch operations to attempt_ws
            if patch_ops:
                repair_step_id = f"repair_{repair_attempt}"
                normalized_ops: list[dict[str, Any]] = []
                for op in patch_ops:
                    if "tool_id" in op and "args" in op:
                        normalized_ops.append(op)
                    elif "path" in op:
                        normalized_ops.append({
                            "tool_id": op.get("tool_id", "write_file"),
                            "args": {
                                "path": op["path"],
                                "content": op.get("content", ""),
                            },
                        })
                    elif "tool" in op:
                        normalized_ops.append({
                            "tool_id": op["tool"],
                            "args": op.get("args", {}),
                        })

                if normalized_ops:
                    try:
                        self.gateway.execute_code_mode_composition(
                            task_id=task_id,
                            attempt_id=attempt_id,
                            step_id=repair_step_id,
                            workspace_dir=attempt_ws,
                            operations=normalized_ops,
                        )
                        for op in normalized_ops:
                            if op.get("tool_id") == "write_file" and "path" in op.get("args", {}):
                                file_p = str(op["args"]["path"])
                                if file_p not in modified_files:
                                    modified_files.append(file_p)
                    except BudgetExceededError:
                        budget_exceeded = True
                        break

            # Transition state machine: RUNNING -> VERIFYING
            curr_sha = self.cow.compute_tree_sha(attempt_ws)
            self.ledger.transition_task(
                task_id,
                TaskState.VERIFYING,
                reason_code="REPAIR_VERIFICATION_ATTEMPT",
                attempt_id=attempt_id,
                workspace_tree_sha=curr_sha,
                fencing_token=fencing_token,
                payload={"repair_attempt": repair_attempt},
            )

            # Re-execute verification
            ver_outcome = self.proof_service.run_verification_and_build_proof(
                task_id=task_id,
                attempt_id=attempt_id,
                base_snapshot_path=base_snap_path,
                workspace_dir=attempt_ws,
                modified_files=modified_files,
                run_tests=True,
            )

        if not ver_outcome["passed"]:
            forensic_snap = self.cow.create_snapshot(
                attempt_ws, label=f"{task_id}_repair_exhausted_att{repair_attempt}"
            )
            forensic_sha = forensic_snap.workspace_tree_sha
            forensic_path = str(forensic_snap.snapshot_path)

            pending_steps = [
                n.get("step_id", "")
                for n in dag_nodes
                if n.get("step_id", "") not in completed_step_ids
            ]
            delta_info = self.export_workspace_delta(task_id, attempt_ws)
            self.ledger.create_checkpoint(
                task_id=task_id,
                attempt_id=attempt_id,
                plan_revision=plan["revision"],
                completed_steps=sorted(completed_step_ids),
                pending_steps=pending_steps,
                workspace_tree_sha=forensic_sha,
                snapshot_path=forensic_path,
                artifact_uri=delta_info.get("archive_path"),
            )

            if budget_exceeded:
                failure_class = "BUDGET_EXCEEDED"
                reason_code = "BUDGET_EXHAUSTED"
            else:
                failure_class = "REPAIR_RETRIES_EXHAUSTED"
                reason_code = "REPAIR_RETRIES_EXHAUSTED"

            self.ledger.update_attempt(
                attempt_id,
                status="FAILED",
                head_tree_sha=forensic_sha,
                failure_class=failure_class,
            )
            return self.ledger.transition_task(
                task_id,
                TaskState.NEEDS_ATTENTION,
                reason_code=reason_code,
                attempt_id=attempt_id,
                workspace_tree_sha=forensic_sha,
                fencing_token=fencing_token,
                terminal_reason=f"Autonomous repair failed after {repair_attempt} attempt(s): {failure_class}",
                payload={
                    "repair_attempts": repair_attempt,
                    "max_retries": max_repair_retries,
                    "forensic_snapshot_path": forensic_path,
                    "verification_results": ver_outcome["verification_results"],
                    "modified_files": modified_files,
                },
            )

        # Sync verified workspace back to source_ws if R2 modifications succeeded
        if modified_files:
            self.cow.clone_to_workspace(attempt_ws, source_ws)

        head_sha = self.cow.compute_tree_sha(attempt_ws)
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
            payload={
                "proof_id": ver_outcome["proof"]["proof_id"],
                "repair_attempts": repair_attempt,
            },
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
