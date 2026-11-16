"""Core domain engine subpackage: orchestration, react loops, routing, models, subagents, and reasoning."""

from __future__ import annotations

import importlib
from typing import Any

_SUBMODULES: set[str] = {'reasoning_effort', 'react_loop', 'subagent', 'router', 'orchestrator', 'models', 'moa', 'approval_lease'}
_SYMBOL_MAP: dict[str, str] = {
    'ApprovalLease': 'approval_lease',
    'ApprovalLeaseManager': 'approval_lease',
    'HarnessOrchestrator': 'orchestrator',
    'run_lifecycle_maintenance': 'orchestrator',
    'process_pending_reflections': 'orchestrator',
    'ReActConfig': 'react_loop',
    'ToolCallRequest': 'react_loop',
    'ToolCallResult': 'react_loop',
    'ReActTurn': 'react_loop',
    'ReActSessionResult': 'react_loop',
    'ReActLoop': 'react_loop',
    'MixtureOfAgents': 'moa',
    'MoAConfig': 'moa',
    'ProposerConfig': 'moa',
    'ProposerOutput': 'moa',
    'MoAResult': 'moa',
    'RouteDecision': 'router',
    'TwoTierModelRouter': 'router',
    'GovernedMemoryService': 'router',
    'utcnow_iso': 'models',
    'TaskState': 'models',
    'VALID_TRANSITIONS': 'models',
    'RiskClass': 'models',
    'PolicyDecisionType': 'models',
    'ExecutionMode': 'models',
    'TaskBudget': 'models',
    'PlanNode': 'models',
    'TaskSubmitRequest': 'models',
    'SubagentRole': 'subagent',
    'SubagentStatus': 'subagent',
    'normalize_role': 'subagent',
    'MaxRecursionDepthExceededError': 'subagent',
    'SubagentTimeoutError': 'subagent',
    'SubagentNotFoundError': 'subagent',
    'SubagentResult': 'subagent',
    'SubagentRecord': 'subagent',
    'SubagentManager': 'subagent',
    'get_provider_supported_efforts': 'reasoning_effort',
    'clamp_effort': 'reasoning_effort',
    'effort_to_anthropic_thinking': 'reasoning_effort',
    'effort_to_gemini_thinking': 'reasoning_effort',
    'model_supports_reasoning': 'reasoning_effort',
    'resolve_reasoning_effort': 'reasoning_effort',
}

__all__ = [
    "orchestrator",
    "react_loop",
    "router",
    "models",
    "subagent",
    "reasoning_effort",
    "moa",
    "approval_lease",
    "ApprovalLease",
    "ApprovalLeaseManager",
    "HarnessOrchestrator",
    "run_lifecycle_maintenance",
    "process_pending_reflections",
    "ReActConfig",
    "ToolCallRequest",
    "ToolCallResult",
    "ReActTurn",
    "ReActSessionResult",
    "ReActLoop",
    "MixtureOfAgents",
    "MoAConfig",
    "ProposerConfig",
    "ProposerOutput",
    "MoAResult",
    "RouteDecision",
    "TwoTierModelRouter",
    "GovernedMemoryService",
    "utcnow_iso",
    "TaskState",
    "VALID_TRANSITIONS",
    "RiskClass",
    "PolicyDecisionType",
    "ExecutionMode",
    "TaskBudget",
    "PlanNode",
    "TaskSubmitRequest",
    "SubagentRole",
    "SubagentStatus",
    "normalize_role",
    "MaxRecursionDepthExceededError",
    "SubagentTimeoutError",
    "SubagentNotFoundError",
    "SubagentResult",
    "SubagentRecord",
    "SubagentManager",
    "get_provider_supported_efforts",
    "clamp_effort",
    "effort_to_anthropic_thinking",
    "effort_to_gemini_thinking",
    "model_supports_reasoning",
    "resolve_reasoning_effort",
]

def __getattr__(name: str) -> Any:
    if name in _SUBMODULES:
        mod = importlib.import_module(f".{name}", __name__)
        globals()[name] = mod
        return mod
    if name in _SYMBOL_MAP:
        target_mod = _SYMBOL_MAP[name]
        mod = importlib.import_module(f".{target_mod}", __name__)
        val = getattr(mod, name)
        globals()[name] = val
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

def __dir__() -> list[str]:
    return list(__all__)
