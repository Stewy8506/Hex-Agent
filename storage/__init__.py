"""Storage and persistence subpackage: ledger database, CoW snapshots, sandboxes, backup, GC, and project registry."""

from __future__ import annotations

import importlib
from typing import Any

_SUBMODULES: set[str] = {'project_registry', 'gc', 'backup', 'sandbox', 'db', 'cow', 'vault_keychain', 'timeline_rewind'}
_SYMBOL_MAP: dict[str, str] = {
    'canonical_json': 'db',
    'sha256_hex': 'db',
    'compute_event_hash': 'db',
    'normalize_query': 'db',
    'normalize_param': 'db',
    'ClaimTransactionError': 'db',
    'ConcurrencyFenceError': 'db',
    'InvalidTransitionError': 'db',
    'ChainIntegrityReport': 'db',
    'LedgerStore': 'db',
    'RewindTarget': 'timeline_rewind',
    'RewindResult': 'timeline_rewind',
    'TimelineRewindManager': 'timeline_rewind',
    'IGNORE_DIRS': 'cow',
    'SnapshotMetadata': 'cow',
    'WorkspaceSnapshotProvider': 'cow',
    'compute_tree_sha': 'cow',
    'export_delta_archive': 'cow',
    'import_delta_archive': 'cow',
    'compute_workspace_diff': 'cow',
    'APFSAndPortableCoWProvider': 'cow',
    'OverlayFSSnapshotProvider': 'cow',
    'BtrfsSnapshotProvider': 'cow',
    'get_default_snapshot_provider': 'cow',
    'sanitize_environment': 'sandbox',
    'SandboxResult': 'sandbox',
    'ExecutionSandbox': 'sandbox',
    'RestrictedSubprocessSandbox': 'sandbox',
    'BubblewrapSandbox': 'sandbox',
    'DockerPodmanSandbox': 'sandbox',
    'get_default_sandbox': 'sandbox',
    'EXCLUDED_DIR_NAMES': 'backup',
    'EXCLUDED_SUFFIXES': 'backup',
    'SECRET_ENV_KEYS': 'backup',
    'SecurityViolationError': 'backup',
    'create_backup': 'backup',
    'restore_backup': 'backup',
    'generate_dump': 'backup',
    'parse_ttl_seconds': 'gc',
    'format_bytes': 'gc',
    'GCReport': 'gc',
    'GarbageCollector': 'gc',
    'compute_project_id': 'project_registry',
    'ProjectRecord': 'project_registry',
    'ProjectRegistry': 'project_registry',
    'SERVICE': 'vault_keychain',
    'KeychainUnavailable': 'vault_keychain',
    'is_keychain_available': 'vault_keychain',
    'store_secret': 'vault_keychain',
    'get_secret': 'vault_keychain',
    'delete_secret': 'vault_keychain',
    'backend_label': 'vault_keychain',
    'migrate_plaintext_credentials': 'vault_keychain',
}

__all__ = [
    "db",
    "cow",
    "sandbox",
    "backup",
    "gc",
    "project_registry",
    "canonical_json",
    "sha256_hex",
    "compute_event_hash",
    "normalize_query",
    "normalize_param",
    "ConcurrencyFenceError",
    "InvalidTransitionError",
    "ChainIntegrityReport",
    "LedgerStore",
    "timeline_rewind",
    "RewindTarget",
    "RewindResult",
    "TimelineRewindManager",
    "IGNORE_DIRS",
    "SnapshotMetadata",
    "WorkspaceSnapshotProvider",
    "compute_tree_sha",
    "export_delta_archive",
    "import_delta_archive",
    "compute_workspace_diff",
    "APFSAndPortableCoWProvider",
    "OverlayFSSnapshotProvider",
    "BtrfsSnapshotProvider",
    "get_default_snapshot_provider",
    "sanitize_environment",
    "SandboxResult",
    "ExecutionSandbox",
    "RestrictedSubprocessSandbox",
    "BubblewrapSandbox",
    "DockerPodmanSandbox",
    "get_default_sandbox",
    "EXCLUDED_DIR_NAMES",
    "EXCLUDED_SUFFIXES",
    "SECRET_ENV_KEYS",
    "SecurityViolationError",
    "create_backup",
    "restore_backup",
    "generate_dump",
    "parse_ttl_seconds",
    "format_bytes",
    "GCReport",
    "GarbageCollector",
    "compute_project_id",
    "ProjectRecord",
    "ProjectRegistry",
    "vault_keychain",
    "SERVICE",
    "KeychainUnavailable",
    "is_keychain_available",
    "store_secret",
    "get_secret",
    "delete_secret",
    "backend_label",
    "migrate_plaintext_credentials",
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
