"""Temporal approval leasing engine for safe command prefixes and directories.

Grants time-bounded auto-approval leases for trusted operations (e.g., test runners,
build systems, isolated workspaces) eliminating repetitive interactive approval
prompts while strictly enforcing time-to-live (TTL) expiration and explicit revocation.
"""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import threading
import time
from typing import Any
import uuid

from dataclasses import dataclass, field


@dataclass
class ApprovalLease:
    """A time-bounded approval lease allowing auto-approval of matching tool invocations."""

    lease_id: str
    task_id: str | None = None
    command_prefix: str | None = None
    allowed_paths: list[str] = field(default_factory=list)
    granted_at: float = field(default_factory=time.time)
    ttl_seconds: int = 900
    expires_at: float = 0.0
    scope: str = "temporal"  # "session" | "temporal"
    is_revoked: bool = False

    def __post_init__(self) -> None:
        if self.expires_at == 0.0:
            self.expires_at = self.granted_at + float(self.ttl_seconds)

    def is_valid(self) -> bool:
        """Return True if lease has not been revoked and has not reached expiration time."""
        return not self.is_revoked and time.time() < self.expires_at

    def to_dict(self) -> dict[str, Any]:
        """Convert lease to serializable dictionary."""
        return {
            "lease_id": self.lease_id,
            "task_id": self.task_id,
            "command_prefix": self.command_prefix,
            "allowed_paths": list(self.allowed_paths),
            "granted_at": self.granted_at,
            "ttl_seconds": self.ttl_seconds,
            "expires_at": self.expires_at,
            "scope": self.scope,
            "is_revoked": self.is_revoked,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ApprovalLease:
        """Reconstruct lease from dictionary representation."""
        paths = data.get("allowed_paths", [])
        if isinstance(paths, str):
            try:
                import json
                paths = json.loads(paths)
            except Exception:
                paths = [paths]
        return cls(
            lease_id=data["lease_id"],
            task_id=data.get("task_id"),
            command_prefix=data.get("command_prefix"),
            allowed_paths=list(paths) if isinstance(paths, list) else [],
            granted_at=float(data.get("granted_at", time.time())),
            ttl_seconds=int(data.get("ttl_seconds", 900)),
            expires_at=float(data.get("expires_at", 0.0)),
            scope=data.get("scope", "temporal"),
            is_revoked=bool(data.get("is_revoked", False)),
        )


class ApprovalLeaseManager:
    """Manages creation, verification, revocation, and persistence of approval leases."""

    def __init__(self, ledger: Any | None = None) -> None:
        self.ledger = ledger
        self._leases: dict[str, ApprovalLease] = {}
        self._lock = threading.RLock()
        self._hydrate_from_ledger()

    def _hydrate_from_ledger(self) -> None:
        """Hydrate active leases from the backing LedgerStore if available."""
        if self.ledger is not None and hasattr(self.ledger, "list_approval_leases"):
            try:
                records = self.ledger.list_approval_leases(active_only=True)
                with self._lock:
                    for rec in records:
                        try:
                            lease = ApprovalLease.from_dict(rec)
                            if lease.is_valid():
                                self._leases[lease.lease_id] = lease
                        except Exception:
                            continue
            except Exception:
                pass

    def grant_lease(
        self,
        command_prefix: str | None = None,
        allowed_paths: list[str] | None = None,
        ttl_seconds: int = 900,
        task_id: str | None = None,
        scope: str = "temporal",
    ) -> ApprovalLease:
        """Grant and register a new temporal or session approval lease."""
        lease_id = f"lease_{uuid.uuid4().hex[:12]}"
        now = time.time()
        norm_paths = []
        if allowed_paths:
            for p in allowed_paths:
                if isinstance(p, (str, Path)) and str(p).strip():
                    norm_paths.append(str(p).strip())

        prefix = command_prefix.strip() if (command_prefix and command_prefix.strip()) else None

        lease = ApprovalLease(
            lease_id=lease_id,
            task_id=task_id,
            command_prefix=prefix,
            allowed_paths=norm_paths,
            granted_at=now,
            ttl_seconds=ttl_seconds,
            expires_at=now + float(ttl_seconds),
            scope=scope,
            is_revoked=False,
        )

        with self._lock:
            self._leases[lease.lease_id] = lease

        if self.ledger is not None and hasattr(self.ledger, "record_approval_lease"):
            try:
                self.ledger.record_approval_lease(
                    lease_id=lease.lease_id,
                    task_id=lease.task_id,
                    command_prefix=lease.command_prefix,
                    allowed_paths=lease.allowed_paths,
                    scope=lease.scope,
                    ttl_seconds=lease.ttl_seconds,
                    granted_at=lease.granted_at,
                    expires_at=lease.expires_at,
                    is_revoked=lease.is_revoked,
                )
            except Exception:
                pass

        return lease

    def check_lease(self, tool_name: str, arguments: dict[str, Any]) -> tuple[bool, str | None]:
        """Check if an active, unexpired lease covers the given tool invocation.

        Returns:
            (True, lease_id) if an active valid lease matches.
            (False, None) if no active lease matches.
        """
        extracted_cmd = self._extract_command(tool_name, arguments)
        extracted_paths = self._extract_paths(tool_name, arguments)

        with self._lock:
            # Check all active leases
            for lease in self._leases.values():
                if not lease.is_valid():
                    continue

                if self._matches_lease(lease, extracted_cmd, extracted_paths):
                    return (True, lease.lease_id)

        return (False, None)

    def _matches_lease(
        self,
        lease: ApprovalLease,
        cmd: str | None,
        paths: list[str],
    ) -> bool:
        """Evaluate if extracted command and paths satisfy lease constraints."""
        has_prefix_constraint = bool(lease.command_prefix)
        has_paths_constraint = bool(lease.allowed_paths)

        # Broad lease with no specific constraints (task-level auto-approval)
        if not has_prefix_constraint and not has_paths_constraint:
            return True

        cmd_matched = False
        if has_prefix_constraint and cmd is not None:
            cmd_matched = self._match_command(lease.command_prefix, cmd)  # type: ignore[arg-type]

        paths_matched = False
        if has_paths_constraint and paths:
            paths_matched = any(self._match_path(lease.allowed_paths, p) for p in paths)

        # If both constraints are present on lease:
        if has_prefix_constraint and has_paths_constraint:
            if cmd is not None and paths:
                return cmd_matched and paths_matched
            elif cmd is not None:
                return cmd_matched
            elif paths:
                return paths_matched
            return False

        # Only command prefix constraint
        if has_prefix_constraint:
            return cmd_matched

        # Only paths constraint
        if has_paths_constraint:
            return paths_matched

        return False

    @staticmethod
    def _match_command(prefix: str, command: str) -> bool:
        """Check if command line matches prefix with boundary safety."""
        clean_prefix = prefix.strip()
        clean_cmd = command.strip()

        if not clean_prefix or not clean_cmd:
            return False

        # Exact match or word boundary prefix match (space, tab, or argument)
        if clean_cmd == clean_prefix:
            return True
        if clean_cmd.startswith(clean_prefix + " ") or clean_cmd.startswith(clean_prefix + "\t"):
            return True

        # Check executable basename / token matching
        try:
            tokens = shlex.split(clean_cmd)
        except Exception:
            tokens = clean_cmd.split()

        if not tokens:
            return False

        first_token = tokens[0]
        base_name = os.path.basename(first_token)

        # Match single-word binary prefix e.g. "pytest" against "/path/to/pytest -v"
        if " " not in clean_prefix:
            if base_name == clean_prefix or first_token == clean_prefix:
                return True

        # Match multi-word prefix e.g. "cargo test" against "cargo test --workspace"
        prefix_tokens = clean_prefix.split()
        if len(tokens) >= len(prefix_tokens):
            if tokens[: len(prefix_tokens)] == prefix_tokens:
                return True
            # Also check if base_name replaces first prefix token
            if [base_name] + tokens[1 : len(prefix_tokens)] == prefix_tokens:
                return True

        # Python -m runner match: python -m pytest ...
        if clean_cmd.startswith("python -m " + clean_prefix) or clean_cmd.startswith("python3 -m " + clean_prefix):
            return True

        return False

    @staticmethod
    def _match_path(allowed_paths: list[str], candidate_path: str) -> bool:
        """Check if candidate path resides within or matches any allowed path."""
        if not candidate_path:
            return False

        clean_cand = str(candidate_path).strip()
        norm_cand = os.path.normpath(clean_cand)

        for allowed in allowed_paths:
            clean_allowed = str(allowed).strip()
            norm_allowed = os.path.normpath(clean_allowed)

            if norm_cand == norm_allowed:
                return True

            if norm_cand.startswith(norm_allowed + os.sep):
                return True

            # Attempt Path-based relative_to check
            try:
                p_cand = Path(clean_cand).resolve()
                p_allowed = Path(clean_allowed).resolve()
                if p_cand == p_allowed or p_cand.is_relative_to(p_allowed):
                    return True
            except (ValueError, Exception):
                pass

        return False

    @staticmethod
    def _extract_command(tool_name: str, arguments: dict[str, Any]) -> str | None:
        """Extract command string from heterogeneous tool invocation arguments."""
        if not isinstance(arguments, dict):
            return None

        for key in ("command", "cmd", "CommandLine", "command_line", "script", "code"):
            val = arguments.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
            elif isinstance(val, list) and val:
                return " ".join(str(x) for x in val).strip()

        if "args" in arguments:
            val = arguments["args"]
            if isinstance(val, str) and val.strip():
                return val.strip()
            elif isinstance(val, list) and val:
                return " ".join(str(x) for x in val).strip()

        return None

    @staticmethod
    def _extract_paths(tool_name: str, arguments: dict[str, Any]) -> list[str]:
        """Extract target paths or directories from tool invocation arguments."""
        if not isinstance(arguments, dict):
            return []

        paths: list[str] = []
        for key in (
            "path",
            "Path",
            "file_path",
            "filepath",
            "filename",
            "file",
            "target",
            "dest",
            "destination",
            "directory",
            "dir",
            "cwd",
        ):
            val = arguments.get(key)
            if isinstance(val, (str, Path)) and str(val).strip():
                paths.append(str(val).strip())
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, (str, Path)) and str(item).strip():
                        paths.append(str(item).strip())

        return paths

    def revoke_lease(self, lease_id: str) -> bool:
        """Revoke an active lease immediately."""
        with self._lock:
            lease = self._leases.get(lease_id)
            if lease is not None:
                lease.is_revoked = True
                if self.ledger is not None and hasattr(self.ledger, "revoke_approval_lease"):
                    try:
                        self.ledger.revoke_approval_lease(lease_id)
                    except Exception:
                        pass
                return True

            if self.ledger is not None and hasattr(self.ledger, "revoke_approval_lease"):
                try:
                    return bool(self.ledger.revoke_approval_lease(lease_id))
                except Exception:
                    pass

        return False

    def cleanup_expired(self) -> int:
        """Prune expired and revoked leases from memory and ledger store."""
        with self._lock:
            to_remove = [lid for lid, l in self._leases.items() if not l.is_valid()]
            for lid in to_remove:
                del self._leases[lid]

            ledger_count = 0
            if self.ledger is not None and hasattr(self.ledger, "cleanup_expired_approval_leases"):
                try:
                    ledger_count = self.ledger.cleanup_expired_approval_leases()
                except Exception:
                    pass

            return max(len(to_remove), ledger_count)

    def list_active_leases(self) -> list[ApprovalLease]:
        """List all valid, unrevoked leases currently registered."""
        with self._lock:
            return [l for l in self._leases.values() if l.is_valid()]

    def get_lease(self, lease_id: str) -> ApprovalLease | None:
        """Retrieve a lease by its ID if registered."""
        with self._lock:
            return self._leases.get(lease_id)
