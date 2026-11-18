"""Subagent Manager for HEX Agent.

Implements in-flight sub-agent delegation parity with role-constrained sandboxing:
- Strict, server-owned recursion depth cap (default max_depth = 2).
- Least-privilege role gating across RESEARCHER, CODER, TESTER, REVIEWER, PLANNER, and GENERAL,
  enforced before dispatch (not only described in the prompt).
- A bounded child tool loop that executes role-permitted tools through the shared
  gateway, so policy, journaling and replay still apply.
- Bounded spawn concurrency, deadlines and tool-call budgets.
- Copy-on-Write (CoW) workspace branching per child agent (.hex/subagents/<subagent_id>).
- Threaded execution with synchronous (wait=True) or asynchronous (wait=False) execution.
- Deterministic diff computation and structured SubagentResult reporting.
"""

from __future__ import annotations

import enum
import json
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

from harness_core.storage.cow import (
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


class SubagentExecutionUnavailableError(RuntimeError):
    """Raised when a subagent has no execution backend (router) and demo mode is off.

    Missing inference must fail closed rather than manufacture a COMPLETED
    summary (B11).
    """


class SubagentConcurrencyError(RuntimeError):
    """Raised when the bounded subagent concurrency limit cannot be acquired (B13)."""


class SubagentBudgetExceededError(RuntimeError):
    """Raised when a child exceeds its bounded tool-call budget (B10)."""


class SubagentToolTurnLimitError(RuntimeError):
    """Raised when a child exhausts its bounded tool-loop turns without finishing (B10)."""


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
    parent_workspace: Path | None = None
    thread: threading.Thread | None = None
    result: SubagentResult | None = None
    interrupted: bool = False
    ended_at: float | None = None
    tool_calls_count: int = 0
    error: str | None = None
    budget: Any = None
    gateway_task_id: str | None = None
    tool_sequence: int = 0
    slot_acquired: bool = False
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
        demo_mode: bool = False,
        max_concurrent: int = 4,
        spawn_wait_seconds: float = 60.0,
        max_child_tool_turns: int = 8,
    ) -> None:
        self.ledger = ledger
        self.cow = cow or get_default_snapshot_provider()
        self.router = router
        self.gateway = gateway
        self.max_depth = max_depth
        self.demo_mode = bool(demo_mode) or os.environ.get("HEX_DEMO_MODE", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        self.max_concurrent = max(1, int(max_concurrent))
        self.spawn_wait_seconds = float(spawn_wait_seconds)
        self.max_child_tool_turns = max(1, int(max_child_tool_turns))
        self._spawn_slots = threading.Semaphore(self.max_concurrent)

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
        """Spawn a child subagent with an isolated CoW workspace and role-gated tools.

        Depth is server-owned (B13): it is derived from the trusted parent
        subagent record, not the caller-supplied ``current_depth`` (which is
        accepted for API compatibility but ignored for the recursion cap).
        """
        with self._lock:
            parent_record = self._active_subagents.get(parent_task_id)
        parent_depth = parent_record.depth if parent_record is not None else 0
        if parent_depth >= self.max_depth:
            raise MaxRecursionDepthExceededError(
                f"Spawn depth {parent_depth} exceeds maximum permitted depth {self.max_depth}"
            )

        # B13: bounded spawn concurrency. A spawn that cannot obtain a slot is
        # refused instead of silently creating another unbounded daemon thread.
        if not self._spawn_slots.acquire(timeout=self.spawn_wait_seconds):
            raise SubagentConcurrencyError(
                f"Subagent concurrency limit ({self.max_concurrent}) reached; "
                f"no spawn slot became available within {self.spawn_wait_seconds}s"
            )

        try:
            norm_role = normalize_role(role)
            subagent_id = f"sub_{uuid.uuid4().hex[:12]}"
            now = time.time()

            # Setup isolated branch workspace under .hex/subagents/<subagent_id>
            resolved_parent_ws: Path | None = None
            if parent_ws is not None:
                parent_path = Path(parent_ws).resolve()
                resolved_parent_ws = parent_path
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
                depth=parent_depth + 1,
                status=SubagentStatus.PENDING,
                started_at=now,
                workspace_path=subagent_ws,
                context=context,
                parent_workspace=resolved_parent_ws,
                budget=budget,
                slot_acquired=True,
            )

            with self._lock:
                self._active_subagents[subagent_id] = record
        except BaseException:
            self._spawn_slots.release()
            raise

        # Dispatch execution
        try:
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
        except BaseException:
            self._release_slot(record)
            raise

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

    def interrupt_children_of(self, parent_task_id: str) -> int:
        """B12: fan a parent cancellation out to every live child subagent.

        Interrupts all PENDING/RUNNING children spawned under ``parent_task_id``
        so a canceled parent does not leave orphaned children running. Returns
        the number of children interrupted.
        """
        with self._lock:
            child_ids = [
                r.subagent_id
                for r in self._active_subagents.values()
                if r.parent_task_id == parent_task_id
                and r.status in (SubagentStatus.PENDING, SubagentStatus.RUNNING)
            ]
        count = 0
        for sid in child_ids:
            if self.interrupt_subagent(sid):
                count += 1
        return count

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

    @staticmethod
    def _budget_timeout(budget: Any) -> float | None:
        """Extract a deadline cap (seconds) from a budget, if one is specified."""
        if isinstance(budget, dict):
            for key in ("timeout", "max_duration", "deadline_seconds"):
                val = budget.get(key)
                if isinstance(val, (int, float)) and val > 0:
                    return float(val)
        return None

    @staticmethod
    def _budget_max_tool_calls(budget: Any) -> int | None:
        """Extract a tool-call ceiling from a budget (numeric or dict form)."""
        if isinstance(budget, bool):
            return None
        if isinstance(budget, (int, float)) and budget > 0:
            return int(budget)
        if isinstance(budget, dict):
            val = budget.get("max_tool_calls")
            if isinstance(val, (int, float)) and val > 0:
                return int(val)
        return None

    def _release_slot(self, record: SubagentRecord) -> None:
        """Release the bounded spawn slot exactly once for this child (B13)."""
        with self._lock:
            if not record.slot_acquired:
                return
            record.slot_acquired = False
        self._spawn_slots.release()

    def _resolve_child_task_id(self, record: SubagentRecord) -> str | None:
        """Return a ledger task the child's tool calls can be policy-checked against.

        The child reuses its parent's task when the parent is a real ledger task;
        otherwise it gets its own ledger task so gateway policy, journaling and
        replay still apply (B10) rather than dispatching handlers directly.
        """
        if record.gateway_task_id:
            return record.gateway_task_id
        ledger = self.ledger
        if ledger is None or not hasattr(ledger, "get_task"):
            return None
        if ledger.get_task(record.parent_task_id) is not None:
            record.gateway_task_id = record.parent_task_id
            return record.gateway_task_id
        try:
            created = ledger.submit_task(
                {
                    "goal": f"Subagent {record.subagent_id}: {record.goal}",
                    "execution_mode": "subagent",
                    "workspace_root": str(record.workspace_path),
                }
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a tool error below
            logger.warning("Could not create ledger task for subagent %s: %s", record.subagent_id, exc)
            return None
        record.gateway_task_id = created.get("task_id")
        return record.gateway_task_id

    @staticmethod
    def _collect_artifacts(
        tool_id: str,
        args: dict[str, Any],
        record: SubagentRecord,
        artifacts: list[str],
    ) -> None:
        """Record files a successful child write touched (B10 artifact reporting)."""
        if tool_id not in ("write_file", "patch"):
            return
        raw = args.get("path") or args.get("file_path")
        if not raw:
            return
        try:
            rel = str(Path(str(raw)).resolve().relative_to(record.workspace_path.resolve()))
        except Exception:
            rel = str(raw)
        if rel not in artifacts:
            artifacts.append(rel)

    def _invoke_child_tool(
        self,
        record: SubagentRecord,
        allowed_tools: frozenset[str],
        tool_id: str,
        args: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Invoke one child tool call through the shared gateway (B10).

        The role subset is enforced here, before dispatch, so a RESEARCHER or
        TESTER child cannot mutate the workspace even though the gateway itself
        would allow the tool. All execution still passes through
        ``ToolGateway.invoke`` so policy, journaling and replay apply; a denied or
        approval-gated call produces no side effects.
        """
        record.tool_calls_count += 1
        if tool_id not in allowed_tools:
            return (
                {
                    "ok": False,
                    "error": (
                        f"Denied: tool '{tool_id}' is outside the {record.role.value} role "
                        "tool subset; no side effects were performed"
                    ),
                },
                False,
            )
        if self.gateway is None or not hasattr(self.gateway, "invoke"):
            return (
                {"ok": False, "error": "Denied: no tool gateway is attached to subagent execution"},
                False,
            )
        task_id = self._resolve_child_task_id(record)
        if task_id is None:
            return (
                {
                    "ok": False,
                    "error": "Denied: no ledger task is available for policy-checked execution",
                },
                False,
            )

        from harness_core.gateway import ApprovalRequiredInterrupt, PolicyBlockedError

        sequence = record.tool_sequence
        record.tool_sequence += 1
        try:
            result = self.gateway.invoke(
                task_id=task_id,
                attempt_id=f"att_{record.subagent_id}",
                step_id=f"subagent_step_{record.subagent_id}",
                sequence_in_step=sequence,
                tool_id=tool_id,
                args=args or {},
                workspace_dir=record.workspace_path,
            )
        except PolicyBlockedError as exc:
            return ({"ok": False, "error": f"Denied by gateway policy: {exc}"}, False)
        except ApprovalRequiredInterrupt as exc:
            approval_id = exc.approval_record.get("approval_id", "unknown")
            return (
                {
                    "ok": False,
                    "error": (
                        f"Tool '{tool_id}' requires owner approval (approval_id={approval_id}); "
                        "no side effects were performed"
                    ),
                },
                False,
            )
        except Exception as exc:  # noqa: BLE001 - reported to the child, never swallowed
            return ({"ok": False, "error": str(exc)}, False)

        if not isinstance(result, dict):
            return ({"ok": True, "result": result}, True)
        ok = not (result.get("ok") is False or result.get("error"))
        return (result, ok)

    def _run_tool_loop(
        self,
        record: SubagentRecord,
        allowed_tools: frozenset[str],
        deadline: float | None,
        max_tool_calls: int | None,
        findings: list[str],
        artifacts: list[str],
    ) -> str:
        """Run the child's bounded tool loop through the shared gateway (B10).

        The child reasons, executes role-permitted tools, and receives each
        result until it replies without a tool call. Turns, deadline and
        tool-call budget all bound the loop.
        """
        from harness_core.core.react_loop import ReActLoop

        transcript_parts: list[str] = []
        summary = ""
        for _turn in range(self.max_child_tool_turns):
            if record.interrupted:
                return summary
            if max_tool_calls is not None and record.tool_calls_count > max_tool_calls:
                raise SubagentBudgetExceededError(
                    f"Subagent tool-call budget exceeded "
                    f"({record.tool_calls_count} > {max_tool_calls})"
                )
            content = self._run_reasoning_bounded(
                record, allowed_tools, deadline, "\n".join(transcript_parts)
            )
            summary = content or ""
            _thought, calls = ReActLoop.parse_tool_calls(summary)
            if not calls or record.interrupted:
                return summary
            for call in calls:
                if max_tool_calls is not None and record.tool_calls_count >= max_tool_calls:
                    raise SubagentBudgetExceededError(
                        f"Subagent tool-call budget exceeded "
                        f"({record.tool_calls_count} >= {max_tool_calls})"
                    )
                result, ok = self._invoke_child_tool(
                    record, allowed_tools, call.name, call.arguments
                )
                if ok:
                    self._collect_artifacts(call.name, call.arguments, record, artifacts)
                findings.append(f"{call.name}: {'ok' if ok else 'error'}")
                transcript_parts.append(
                    f"Tool result ({call.name}, {'ok' if ok else 'error'}): "
                    f"{json.dumps(result, default=str)[:2000]}"
                )
        raise SubagentToolTurnLimitError(
            f"Subagent exceeded {self.max_child_tool_turns} tool-loop turns without a final answer"
        )

    def _reasoning_call(
        self,
        record: SubagentRecord,
        allowed_tools: frozenset[str],
        transcript: str = "",
    ) -> str:
        """Produce the child's next turn via the router; fail closed without a backend.

        B11: a missing router (or an empty model response) must not manufacture a
        COMPLETED summary. The deterministic stand-in is only used in explicit
        demo mode and is labeled as such. B10: the child is told which tools it
        may call and how to call them, so a CODER/TESTER child can act rather
        than only describe.
        """
        if self.router is not None and hasattr(self.router, "execute_routed_request"):
            tools_str = ", ".join(sorted(allowed_tools))
            system_prompt = (
                f"You are a specialized {record.role.value.upper()} subagent in HEX Agent.\n"
                f"Goal: {record.goal}\n"
                f"Permitted tools: {tools_str}\n"
                f"Context: {record.context or 'None'}\n"
                f"Execute with strict focus and return your final findings, artifacts produced, and summary."
            )
            tool_instructions = ""
            if self.gateway is not None and allowed_tools:
                tool_instructions = (
                    f"You may use only these tools: {tools_str}.\n"
                    "To act, emit one or more tool calls in exactly this form and nothing else:\n"
                    '<tool_call>{"name": "<tool>", "arguments": {...}}</tool_call>\n'
                    "Each result comes back as a 'Tool result' line. When the goal is "
                    "complete, reply with your final findings as plain text and no tool calls.\n"
                )
            work_so_far = f"\nWork so far:\n{transcript}" if transcript else ""
            prompt = (
                f"Subagent Goal: {record.goal}\n"
                f"{tool_instructions}"
                f"{work_so_far}\n"
                "Provide your next action or final answer."
            )
            decision, model_resp = self.router.execute_routed_request(
                prompt=prompt,
                task_class="coding" if record.role in (SubagentRole.CODER, SubagentRole.TESTER) else "research",
                risk_class="R1" if record.role in (SubagentRole.RESEARCHER, SubagentRole.REVIEWER, SubagentRole.PLANNER) else "R2",
                system_prompt=system_prompt,
            )
            content = getattr(model_resp, "content", None) if model_resp else None
            if content:
                return content
            if not self.demo_mode:
                raise SubagentExecutionUnavailableError(
                    "Subagent router returned no content; execution backend unavailable."
                )
            return f"[demo] Subagent [{record.role.value}] processed goal: {record.goal}"
        if not self.demo_mode:
            raise SubagentExecutionUnavailableError(
                "No router configured for subagent execution; failing closed. "
                "Enable demo mode for the deterministic stand-in."
            )
        return f"[demo] Subagent [{record.role.value}] processed goal: {record.goal}"

    def _run_reasoning_bounded(
        self,
        record: SubagentRecord,
        allowed_tools: frozenset[str],
        deadline: float | None,
        transcript: str = "",
    ) -> str:
        """Run the reasoning call bounded by the deadline and responsive to interrupt.

        B12: the timeout is enforced, not merely accepted. The blocking reasoning
        call runs on a daemon thread; this wait polls for interruption and the
        deadline so a cancel or expiry stops the child promptly instead of
        blocking indefinitely.
        """
        box: dict[str, Any] = {}

        def _call() -> None:
            try:
                box["summary"] = self._reasoning_call(record, allowed_tools, transcript)
            except BaseException as exc:  # noqa: BLE001 - re-raised on the waiting thread
                box["error"] = exc

        worker = threading.Thread(target=_call, daemon=True, name=f"SubagentReason-{record.subagent_id}")
        worker.start()
        while worker.is_alive():
            if record.interrupted:
                break
            if deadline is not None:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise SubagentTimeoutError(
                        f"Subagent '{record.subagent_id}' exceeded its deadline"
                    )
                worker.join(timeout=min(0.05, remaining))
            else:
                worker.join(timeout=0.05)
        if "error" in box:
            raise box["error"]
        return box.get("summary", "")

    def _finalize_timed_out(self, record: SubagentRecord, start_t: float) -> None:
        if record.status == SubagentStatus.INTERRUPTED:
            return
        record.status = SubagentStatus.TIMED_OUT
        record.ended_at = time.time()
        record.error = record.error or "Subagent exceeded its deadline"
        record.result = SubagentResult(
            subagent_id=record.subagent_id,
            parent_task_id=record.parent_task_id,
            role=record.role.value,
            status=SubagentStatus.TIMED_OUT.value,
            summary="Subagent timed out before completing its goal",
            started_at=start_t,
            ended_at=record.ended_at,
            duration_seconds=record.ended_at - start_t,
            tool_calls_count=record.tool_calls_count,
            error=record.error,
        )

    def _run_subagent_sync(self, record: SubagentRecord, timeout: float) -> None:
        """Synchronously execute the child subagent reasoning loop.

        B12: enforces the deadline and tool-call budget, honors interruption
        throughout (not just before the call), and never lets a late result
        overwrite an INTERRUPTED/TIMED_OUT terminal state.
        """
        record.status = SubagentStatus.RUNNING
        start_t = time.time()
        allowed_tools = ROLE_ALLOWED_TOOLS.get(record.role, ROLE_ALLOWED_TOOLS[SubagentRole.GENERAL])

        deadline: float | None = None
        if timeout and float(timeout) > 0:
            deadline = start_t + float(timeout)
        budget_deadline = self._budget_timeout(record.budget)
        if budget_deadline is not None:
            cap = start_t + budget_deadline
            deadline = cap if deadline is None else min(deadline, cap)
        max_tool_calls = self._budget_max_tool_calls(record.budget)

        findings: list[str] = []
        artifacts: list[str] = []
        diff_str = ""

        try:
            if record.interrupted:
                record.status = SubagentStatus.INTERRUPTED
                return

            summary = self._run_tool_loop(
                record, allowed_tools, deadline, max_tool_calls, findings, artifacts
            )

            # A cancel may have landed while reasoning ran: keep INTERRUPTED.
            if record.interrupted or record.status in (SubagentStatus.INTERRUPTED, SubagentStatus.TIMED_OUT):
                return

            # B17: diff against the explicit parent workspace recorded at spawn,
            # never a directory inferred by walking up from the child path.
            parent_ws_candidate = record.parent_workspace
            if (
                parent_ws_candidate is not None
                and parent_ws_candidate.exists()
                and parent_ws_candidate != record.workspace_path
            ):
                try:
                    diff_str = compute_workspace_diff(parent_ws_candidate, record.workspace_path)
                except Exception:
                    diff_str = ""

            # Recheck terminal state before publishing: a late result must not
            # overwrite INTERRUPTED, and an expired deadline yields TIMED_OUT.
            if record.interrupted or record.status in (SubagentStatus.INTERRUPTED, SubagentStatus.TIMED_OUT):
                return
            if deadline is not None and time.time() > deadline:
                self._finalize_timed_out(record, start_t)
                return

            record.status = SubagentStatus.COMPLETED
            record.ended_at = time.time()
            record.result = SubagentResult(
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

        except SubagentTimeoutError:
            self._finalize_timed_out(record, start_t)
        except Exception as exc:
            if record.interrupted or record.status == SubagentStatus.INTERRUPTED:
                return
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
            self._release_slot(record)
