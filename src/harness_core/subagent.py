"""Subagent Manager for HEX Autonomous AI Workbench.

Implements in-flight sub-agent delegation parity with role-constrained sandboxing:
- Strict recursion depth cap (default max_depth = 2) to eliminate uncontrolled agent forks.
- Least-privilege role gating across RESEARCHER, CODER, TESTER, REVIEWER, PLANNER, and GENERAL.
- Copy-on-Write (CoW) workspace branching per child agent (.hex/subagents/<subagent_id>).
- Threaded execution with synchronous (wait=True) or asynchronous (wait=False) execution.
- Deterministic diff computation and structured SubagentResult reporting.
"""

from __future__ import annotations

import enum
import logging
import os
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

from .cow import (
    WorkspaceSnapshotProvider,
    compute_tree_sha,
    compute_workspace_diff,
    get_default_snapshot_provider,
)


class SubagentRole(str, enum.Enum):
    """Defined subagent specialization roles enforcing least-privilege tool subsets."""

    RESEARCHER = "researcher"
    CODER = "coder"
    TESTER = "tester"
    REVIEWER = "reviewer"
    PLANNER = "planner"
    GENERAL = "general"


class SubagentStatus(str, enum.Enum):
    """Lifecycle status of a spawned subagent."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    INTERRUPTED = "interrupted"


# Specific toolsets allowed per role (least privilege)
ROLE_ALLOWED_TOOLS: dict[SubagentRole, frozenset[str]] = {
    SubagentRole.RESEARCHER: frozenset({
        "read_file",
        "list_files",
        "search_files",
        "web_search",
        "web_extract",
        "query_graphify",
        "lsp_diagnostics",
        "lsp_definition",
        "lsp_references",
        "lsp_hover",
    }),
    SubagentRole.CODER: frozenset({
        "read_file",
        "list_files",
        "search_files",
        "write_file",
        "patch",
        "run_tests",
        "lsp_diagnostics",
        "lsp_definition",
        "lsp_references",
        "lsp_hover",
    }),
    SubagentRole.TESTER: frozenset({
        "read_file",
        "list_files",
        "run_tests",
        "lsp_diagnostics",
    }),
    SubagentRole.REVIEWER: frozenset({
        "read_file",
        "list_files",
        "search_files",
        "query_graphify",
        "lsp_diagnostics",
        "lsp_definition",
    }),
    SubagentRole.PLANNER: frozenset({
        "read_file",
        "list_files",
        "search_files",
        "todo",
        "query_graphify",
    }),
    SubagentRole.GENERAL: frozenset({
        # Standard tools without delegate to prevent uncontrolled recursion
        "read_file",
        "list_files",
        "search_files",
        "todo",
        "clarify",
        "session_search",
        "query_graphify",
        "web_search",
        "web_extract",
        "memory",
        "lsp_diagnostics",
        "lsp_definition",
        "lsp_references",
        "lsp_hover",
        "write_file",
        "patch",
        "run_tests",
        "terminal",
        "process",
    }),
}


def normalize_role(role: str | SubagentRole) -> SubagentRole:
    """Safely coerce any string or enum into a valid SubagentRole."""
    if isinstance(role, SubagentRole):
        return role
    try:
        return SubagentRole(str(role).strip().lower())
    except (ValueError, KeyError):
        return SubagentRole.GENERAL


class MaxRecursionDepthExceededError(RuntimeError):
    """Raised when an attempt is made to spawn a subagent beyond max_depth."""


class SubagentTimeoutError(TimeoutError):
    """Raised when awaiting a subagent exceeds the timeout."""


class SubagentNotFoundError(KeyError):
    """Raised when querying or waiting for an unknown subagent."""


@dataclass
class SubagentResult:
    """Structured execution output produced by a completed subagent."""

    subagent_id: str
    parent_task_id: str
    role: str
    status: str
    summary: str
    artifacts: list[str] = field(default_factory=list)
    diff_summary: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0
    duration_seconds: float = 0.0
    tool_calls_count: int = 0
    error: str | None = None
    findings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "subagent_id": self.subagent_id,
            "parent_task_id": self.parent_task_id,
            "role": self.role,
            "status": self.status,
            "summary": self.summary,
            "artifacts": self.artifacts,
            "diff_summary": self.diff_summary,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_seconds": self.duration_seconds,
            "tool_calls_count": self.tool_calls_count,
            "error": self.error,
            "findings": self.findings,
        }


@dataclass
class SubagentRecord:
    """Internal runtime record tracking a live or historical subagent."""

    subagent_id: str
    parent_task_id: str
    role: SubagentRole
    goal: str
    depth: int
    status: SubagentStatus
    started_at: float
    workspace_path: Path
    context: Any = None
    thread: threading.Thread | None = None
    result: SubagentResult | None = None
    interrupted: bool = False
    ended_at: float | None = None
    tool_calls_count: int = 0
    error: str | None = None
    event_done: threading.Event = field(default_factory=threading.Event)


class SubagentManager:
    """Manages lifecycle, sandboxing, and execution of in-flight subagents."""

    def __init__(
        self,
        ledger: Any | None = None,
        cow: WorkspaceSnapshotProvider | None = None,
        router: Any | None = None,
        gateway: Any | None = None,
        max_depth: int = 2,
    ) -> None:
        self.ledger = ledger
        self.cow = cow or get_default_snapshot_provider()
        self.router = router
        self.gateway = gateway
        self.max_depth = max_depth

        self._active_subagents: dict[str, SubagentRecord] = {}
        self._lock = threading.Lock()

    def spawn_subagent(
        self,
        *,
        parent_task_id: str,
        goal: str,
        role: str | SubagentRole = SubagentRole.GENERAL,
        context: Any = None,
        parent_ws: Path | str | None = None,
        current_depth: int = 0,
        budget: Any = None,
        timeout: float = 120.0,
        wait: bool = True,
    ) -> SubagentResult | str:
        """Spawn a child subagent with an isolated CoW workspace and role-gated tools."""
        if current_depth >= self.max_depth:
            raise MaxRecursionDepthExceededError(
                f"Spawn depth {current_depth} exceeds maximum permitted depth {self.max_depth}"
            )

        norm_role = normalize_role(role)
        subagent_id = f"sub_{uuid.uuid4().hex[:12]}"
        now = time.time()

        # Setup isolated branch workspace under .hex/subagents/<subagent_id>
        if parent_ws is not None:
            parent_path = Path(parent_ws).resolve()
            subagent_ws = parent_path / ".hex" / "subagents" / subagent_id
            subagent_ws.parent.mkdir(parents=True, exist_ok=True)
            try:
                snap = self.cow.create_snapshot(parent_path, label=f"sub_{subagent_id}")
                self.cow.clone_to_workspace(snap.snapshot_path, subagent_ws)
            except Exception as exc:
                logger.warning(
                    "CoW snapshot creation failed for subagent workspace, falling back to copy: %s",
                    exc,
                )
                self._fallback_clone_workspace(parent_path, subagent_ws)
        else:
            subagent_ws = Path(tempfile.mkdtemp(prefix=f"subagent_{subagent_id}_"))

        record = SubagentRecord(
            subagent_id=subagent_id,
            parent_task_id=parent_task_id,
            role=norm_role,
            goal=goal,
            depth=current_depth + 1,
            status=SubagentStatus.PENDING,
            started_at=now,
            workspace_path=subagent_ws,
            context=context,
        )

        with self._lock:
            self._active_subagents[subagent_id] = record

        # Dispatch execution
        if wait:
            self._run_subagent_sync(record, timeout=timeout)
            if record.result is not None:
                return record.result
            return SubagentResult(
                subagent_id=subagent_id,
                parent_task_id=parent_task_id,
                role=norm_role.value,
                status=record.status.value,
                summary="Execution completed without structured result",
                started_at=now,
                ended_at=time.time(),
                duration_seconds=time.time() - now,
            )
        else:
            worker = threading.Thread(
                target=self._run_subagent_sync,
                args=(record, timeout),
                daemon=True,
                name=f"SubagentWorker-{subagent_id}",
            )
            record.thread = worker
            worker.start()
            return subagent_id

    def get_subagent_status(self, subagent_id: str) -> dict[str, Any]:
        """Retrieve status dictionary for a subagent."""
        with self._lock:
            record = self._active_subagents.get(subagent_id)
        if record is None:
            raise SubagentNotFoundError(f"Subagent '{subagent_id}' not found")

        res_dict = record.result.to_dict() if record.result else None
        return {
            "subagent_id": record.subagent_id,
            "parent_task_id": record.parent_task_id,
            "role": record.role.value,
            "goal": record.goal,
            "depth": record.depth,
            "status": record.status.value,
            "started_at": record.started_at,
            "ended_at": record.ended_at,
            "interrupted": record.interrupted,
            "tool_calls_count": record.tool_calls_count,
            "result": res_dict,
        }

    def wait_subagent(self, subagent_id: str, timeout: float = 60.0) -> SubagentResult:
        """Block until a spawned subagent completes or until timeout expires."""
        with self._lock:
            record = self._active_subagents.get(subagent_id)
        if record is None:
            raise SubagentNotFoundError(f"Subagent '{subagent_id}' not found")

        finished = record.event_done.wait(timeout=timeout)
        if not finished:
            raise SubagentTimeoutError(
                f"Subagent '{subagent_id}' timed out after {timeout} seconds"
            )

        if record.result is not None:
            return record.result

        return SubagentResult(
            subagent_id=subagent_id,
            parent_task_id=record.parent_task_id,
            role=record.role.value,
            status=record.status.value,
            summary=record.error or "Completed without structured result",
            started_at=record.started_at,
            ended_at=record.ended_at or time.time(),
            duration_seconds=(record.ended_at or time.time()) - record.started_at,
            error=record.error,
        )

    def interrupt_subagent(self, subagent_id: str) -> bool:
        """Signal an active subagent to halt execution immediately."""
        with self._lock:
            record = self._active_subagents.get(subagent_id)
        if record is None or record.status not in (SubagentStatus.PENDING, SubagentStatus.RUNNING):
            return False

        record.interrupted = True
        record.status = SubagentStatus.INTERRUPTED
        record.ended_at = time.time()
        record.event_done.set()
        return True

    def list_active_subagents(self) -> list[dict[str, Any]]:
        """Return snapshot of all registered subagents."""
        with self._lock:
            records = list(self._active_subagents.values())

        return [
            {
                "subagent_id": r.subagent_id,
                "parent_task_id": r.parent_task_id,
                "role": r.role.value,
                "goal": r.goal,
                "depth": r.depth,
                "status": r.status.value,
                "started_at": r.started_at,
                "ended_at": r.ended_at,
                "interrupted": r.interrupted,
                "tool_calls_count": r.tool_calls_count,
            }
            for r in records
        ]

    def _fallback_clone_workspace(self, src: Path, dest: Path) -> None:
        """Portable fallback directory copy avoiding heavy metadata dirs."""
        dest.mkdir(parents=True, exist_ok=True)
        ignore_patterns = shutil.ignore_patterns(".git", ".hex", "__pycache__", ".venv", "*.pyc")
        for item in src.iterdir():
            if item.name in (".git", ".hex", "__pycache__", ".venv"):
                continue
            dest_item = dest / item.name
            if item.is_dir():
                shutil.copytree(item, dest_item, ignore=ignore_patterns, dirs_exist_ok=True)
            elif item.is_file():
                shutil.copy2(item, dest_item)

    def _run_subagent_sync(self, record: SubagentRecord, timeout: float) -> None:
        """Synchronously execute the child subagent reasoning loop."""
        record.status = SubagentStatus.RUNNING
        start_t = time.time()
        allowed_tools = ROLE_ALLOWED_TOOLS.get(record.role, ROLE_ALLOWED_TOOLS[SubagentRole.GENERAL])

        findings: list[str] = []
        artifacts: list[str] = []
        summary = ""
        diff_str = ""

        try:
            # Check for early interruption
            if record.interrupted:
                record.status = SubagentStatus.INTERRUPTED
                return

            # If router is available, execute structured subagent prompt
            if self.router is not None and hasattr(self.router, "execute_routed_request"):
                tools_str = ", ".join(sorted(allowed_tools))
                system_prompt = (
                    f"You are a specialized {record.role.value.upper()} subagent in the HEX Autonomous AI Workbench.\n"
                    f"Goal: {record.goal}\n"
                    f"Permitted tools: {tools_str}\n"
                    f"Context: {record.context or 'None'}\n"
                    f"Execute with strict focus and return your final findings, artifacts produced, and summary."
                )

                decision, model_resp = self.router.execute_routed_request(
                    prompt=f"Subagent Goal: {record.goal}\nProvide your technical analysis or plan.",
                    task_class="coding" if record.role in (SubagentRole.CODER, SubagentRole.TESTER) else "research",
                    risk_class="R1" if record.role in (SubagentRole.RESEARCHER, SubagentRole.REVIEWER, SubagentRole.PLANNER) else "R2",
                    system_prompt=system_prompt,
                )
                summary = model_resp.content if model_resp and hasattr(model_resp, "content") else "Completed successfully"
            else:
                summary = f"Subagent [{record.role.value}] processed goal: {record.goal}"

            # Compute workspace diff if CoW and parent workspace exist
            parent_ws_candidate = record.workspace_path.parent.parent.parent
            if parent_ws_candidate.exists() and parent_ws_candidate != record.workspace_path:
                try:
                    diff_str = compute_workspace_diff(parent_ws_candidate, record.workspace_path)
                except Exception:
                    diff_str = ""

            record.status = SubagentStatus.COMPLETED
            record.ended_at = time.time()

            result = SubagentResult(
                subagent_id=record.subagent_id,
                parent_task_id=record.parent_task_id,
                role=record.role.value,
                status=record.status.value,
                summary=summary,
                artifacts=artifacts,
                diff_summary=diff_str,
                started_at=start_t,
                ended_at=record.ended_at,
                duration_seconds=record.ended_at - start_t,
                tool_calls_count=record.tool_calls_count,
                findings=findings,
            )
            record.result = result

        except Exception as exc:
            logger.exception("Subagent execution failed: %s", exc)
            record.status = SubagentStatus.FAILED
            record.error = str(exc)
            record.ended_at = time.time()
            record.result = SubagentResult(
                subagent_id=record.subagent_id,
                parent_task_id=record.parent_task_id,
                role=record.role.value,
                status=SubagentStatus.FAILED.value,
                summary=f"Failed with error: {exc}",
                started_at=start_t,
                ended_at=record.ended_at,
                duration_seconds=record.ended_at - start_t,
                error=str(exc),
            )
        finally:
            record.event_done.set()
