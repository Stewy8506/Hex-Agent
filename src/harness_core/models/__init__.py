"""Domain models and model metadata subpackage."""

from __future__ import annotations

import importlib
from typing import Any

_SUBMODULES: set[str] = {'model_metadata', 'credential_pool'}
_SYMBOL_MAP: dict[str, str] = {
    'CONTEXT_PROBE_TIERS': 'model_metadata',
    'DEFAULT_CONTEXT_LENGTHS': 'model_metadata',
    'DEFAULT_FALLBACK_CONTEXT': 'model_metadata',
    'MODELS_DEV_CACHE_TTL': 'model_metadata',
    'MODELS_DEV_RETRY_DELAY': 'model_metadata',
    'MODELS_DEV_URL': 'model_metadata',
    'fetch_models_dev': 'model_metadata',
    'format_context_tokens': 'model_metadata',
    'get_model_context_length': 'model_metadata',
    'lookup_models_dev_context': 'model_metadata',
    'probe_local_server': 'model_metadata',
    '_clear_endpoint_model_metadata_cache': 'model_metadata',
    '_endpoint_model_metadata_cache': 'model_metadata',
    'CredentialPool': 'credential_pool',
    'CredentialEntry': 'credential_pool',
    'STATUS_OK': 'credential_pool',
    'STATUS_EXHAUSTED': 'credential_pool',
    'STATUS_DEAD': 'credential_pool',
    'STRATEGY_ROUND_ROBIN': 'credential_pool',
    'STRATEGY_LEAST_USED': 'credential_pool',
    'STRATEGY_FILL_FIRST': 'credential_pool',
    'SUPPORTED_STRATEGIES': 'credential_pool',
    'DEFAULT_SOLE_KEY_COOLDOWN': 'credential_pool',
    'DEFAULT_POOLED_KEY_COOLDOWN': 'credential_pool',
    'DEFAULT_JITTER_RATIO': 'credential_pool',
    '_ENDPOINT_BLACKHOLE_TTL_SECONDS': 'credential_pool',
    '_endpoint_host_key': 'credential_pool',
    '_note_endpoint_blackholed': 'credential_pool',
    '_endpoint_blackholed': 'credential_pool',
    '_is_connect_timeout': 'credential_pool',
    '_note_if_connect_timeout': 'credential_pool',
    '_clear_endpoint_blackholes': 'credential_pool',
    '_get_blackholed_endpoints': 'credential_pool',
    'check_endpoint_or_raise': 'credential_pool',
}
_CORE_SYMBOLS: set[str] = {'ExecutionMode', 'TaskBudget', 'PolicyDecisionType', 'RiskClass', 'TaskState', 'VALID_TRANSITIONS', 'utcnow_iso', 'TaskSubmitRequest', 'PlanNode'}

__all__ = [
    "model_metadata",
    "credential_pool",
    "CONTEXT_PROBE_TIERS",
    "DEFAULT_CONTEXT_LENGTHS",
    "DEFAULT_FALLBACK_CONTEXT",
    "MODELS_DEV_CACHE_TTL",
    "MODELS_DEV_RETRY_DELAY",
    "MODELS_DEV_URL",
    "fetch_models_dev",
    "format_context_tokens",
    "get_model_context_length",
    "lookup_models_dev_context",
    "probe_local_server",
    "_clear_endpoint_model_metadata_cache",
    "_endpoint_model_metadata_cache",
    "CredentialPool",
    "CredentialEntry",
    "STATUS_OK",
    "STATUS_EXHAUSTED",
    "STATUS_DEAD",
    "STRATEGY_ROUND_ROBIN",
    "STRATEGY_LEAST_USED",
    "STRATEGY_FILL_FIRST",
    "SUPPORTED_STRATEGIES",
    "DEFAULT_SOLE_KEY_COOLDOWN",
    "DEFAULT_POOLED_KEY_COOLDOWN",
    "DEFAULT_JITTER_RATIO",
    "_ENDPOINT_BLACKHOLE_TTL_SECONDS",
    "_endpoint_host_key",
    "_note_endpoint_blackholed",
    "_endpoint_blackholed",
    "_is_connect_timeout",
    "_note_if_connect_timeout",
    "_clear_endpoint_blackholes",
    "_get_blackholed_endpoints",
    "check_endpoint_or_raise",
    "ExecutionMode",
    "PlanNode",
    "PolicyDecisionType",
    "RiskClass",
    "TaskBudget",
    "TaskState",
    "TaskSubmitRequest",
    "VALID_TRANSITIONS",
    "utcnow_iso",
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
    if name in _CORE_SYMBOLS:
        mod = importlib.import_module("harness_core.core.models")
        val = getattr(mod, name)
        globals()[name] = val
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

def __dir__() -> list[str]:
    return list(__all__)
