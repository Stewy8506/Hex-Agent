"""Core domain models, state machine enums, and typed contracts (PRD Sections 6, 9, 10, 12)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskState(str, Enum):
    SUBMITTED = "SUBMITTED"
    VALIDATING = "VALIDATING"
    QUEUED = "QUEUED"
    PLANNING = "PLANNING"
    READY = "READY"
    RUNNING = "RUNNING"
    SPECULATING = "SPECULATING"
    VERIFYING = "VERIFYING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"
    RETRY_WAIT = "RETRY_WAIT"
    PAUSED = "PAUSED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"

    @property
    def is_terminal(self) -> bool:
        return self in {
            TaskState.SUCCEEDED,
            TaskState.FAILED,
            TaskState.REJECTED,
            TaskState.CANCELED,
            TaskState.EXPIRED,
        }


VALID_TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.SUBMITTED: {
        TaskState.VALIDATING,
        TaskState.REJECTED,
        TaskState.CANCELED,
    },
    TaskState.VALIDATING: {
        TaskState.QUEUED,
        TaskState.RUNNING,  # Fast-Path R0/R1 bypass
        TaskState.REJECTED,
        TaskState.NEEDS_ATTENTION,
        TaskState.CANCELED,
    },
    TaskState.QUEUED: {
        TaskState.PLANNING,
        TaskState.READY,
        TaskState.RUNNING,  # Fast-Path R0/R1 bypass
        TaskState.PAUSED,
        TaskState.CANCELED,
        TaskState.EXPIRED,
    },
    TaskState.PLANNING: {
        TaskState.READY,
        TaskState.WAITING_APPROVAL,
        TaskState.NEEDS_ATTENTION,
        TaskState.PAUSED,
        TaskState.CANCELED,
        TaskState.FAILED,
    },
    TaskState.READY: {
        TaskState.RUNNING,
        TaskState.SPECULATING,
        TaskState.WAITING_APPROVAL,
        TaskState.PAUSED,
        TaskState.CANCELED,
        TaskState.EXPIRED,
    },
    TaskState.RUNNING: {
        TaskState.QUEUED,  # Lease expiration handover
        TaskState.VERIFYING,
        TaskState.SPECULATING,
        TaskState.WAITING_APPROVAL,
        TaskState.NEEDS_ATTENTION,
        TaskState.RETRY_WAIT,
        TaskState.PAUSED,
        TaskState.CANCELED,
        TaskState.FAILED,
        TaskState.EXPIRED,
    },
    TaskState.SPECULATING: {
        TaskState.VERIFYING,
        TaskState.RUNNING,
        TaskState.NEEDS_ATTENTION,
        TaskState.PAUSED,
        TaskState.CANCELED,
        TaskState.FAILED,
    },
    TaskState.VERIFYING: {
        TaskState.QUEUED,  # Lease expiration handover
        TaskState.SUCCEEDED,
        TaskState.RUNNING,  # Bounded repair on DAG subgraph
        TaskState.WAITING_APPROVAL,
        TaskState.NEEDS_ATTENTION,
        TaskState.FAILED,
        TaskState.CANCELED,
    },
    TaskState.WAITING_APPROVAL: {
        TaskState.QUEUED,
        TaskState.READY,
        TaskState.RUNNING,
        TaskState.VERIFYING,
        TaskState.REJECTED,
        TaskState.CANCELED,
        TaskState.EXPIRED,
    },
    TaskState.NEEDS_ATTENTION: {
        TaskState.QUEUED,
        TaskState.PLANNING,
        TaskState.READY,
        TaskState.RUNNING,
        TaskState.CANCELED,
        TaskState.FAILED,
    },
    TaskState.RETRY_WAIT: {
        TaskState.QUEUED,
        TaskState.RUNNING,
        TaskState.FAILED,
        TaskState.CANCELED,
        TaskState.EXPIRED,
    },
    TaskState.PAUSED: {
        TaskState.QUEUED,
        TaskState.READY,
        TaskState.RUNNING,
        TaskState.CANCELED,
        TaskState.EXPIRED,
    },
    TaskState.SUCCEEDED: set(),
    TaskState.FAILED: set(),
    TaskState.REJECTED: set(),
    TaskState.CANCELED: set(),
    TaskState.EXPIRED: set(),
}


class RiskClass(str, Enum):
    R0 = "R0"  # Read task metadata, inspect non-sensitive local files (Fast-Path eligible)
    R1 = "R1"  # Read project files, query Graphify, approved read-only APIs (Fast-Path eligible)
    R2 = "R2"  # Modify workspace files in CoW sandbox, run bounded local build/test
    R3 = "R3"  # External writes, deployments, git push, overriding Medium SkillSpector warning
    R4 = "R4"  # Secret/policy changes, disabling audit, bypassing High/Critical blocks (Deny)


class PolicyDecisionType(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"


class ExecutionMode(str, Enum):
    STANDARD = "standard"
    FAST_PATH = "fast_path"


@dataclass
class TaskBudget:
    """Execution constraints and financial/retry ceilings for autonomous task workflows."""

    max_steps: int = 20
    max_tool_calls: int = 50
    max_retries_per_step: int = 2  # Autonomous repair ceiling before escalation to NEEDS_ATTENTION
    max_speculative_branches: int = 2
    max_wall_seconds: int = 1800
    max_cost_usd: float = 2.0  # Hard cost limit triggering forensic CoW snapshot and NEEDS_ATTENTION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | TaskBudget | None) -> TaskBudget:
        if not data:
            return cls()
        if isinstance(data, cls):
            return data
        valid = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


@dataclass
class PlanNode:
    step_id: str
    title: str
    tool_id: str
    args: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    risk_class: str = "R1"
    acceptance_check: str = "exit_code == 0"
    side_effects: list[str] = field(default_factory=list)
    status: str = "PENDING"  # PENDING | COMPLETED | FAILED | INVALIDATED

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TaskSubmitRequest:
    goal: str
    project_id: str = "default"
    priority: int = 50
    deadline_at: str | None = None
    deliverable: str = "proof_bundle"
    constraints: dict[str, Any] = field(default_factory=dict)
    budget: dict[str, Any] = field(default_factory=dict)
    execution_mode: str = "standard"  # 'standard' | 'fast_path' | 'auto'
    approval_policy: str = "default"
    idempotency_key: str | None = None
    workspace_root: str | None = None
    steps: list[dict[str, Any]] | None = None
    skill_id: str | None = None
    parent_task_id: str | None = None
