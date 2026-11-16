"""Operator approval bridge for the ACP surface.

The gateway calls ``request_approval`` from inside a running turn; this bridge
notifies the connected editor client and blocks that turn until the client
answers ``session/approvalDecision``. Silence is a denial, never an approval.
"""

from __future__ import annotations

import json
import os
import shlex
import threading
import time
from pathlib import Path
from typing import Any, Callable

from harness_core.capabilities.gateways.reaction_listener import ApprovalDecision, ApprovalRequest
from harness_core.core.approval_lease import ApprovalLeaseManager

_COMMAND_KEYS = ("command", "cmd", "command_line", "script")
_APPROVE_ONCE = {"approve", "allow", "allow_once", "approved", "y", "yes"}
_APPROVE_SCOPE = {"approve_scope", "allow_scope", "allow_project", "a"}


class AcpApprovalBridge:
    """Blocking approval bridge between the tool gateway and an ACP client."""

    def __init__(
        self,
        emit: Callable[[dict[str, Any]], None],
        session_for_task: Callable[[str | None], str | None],
        workspace_root: Path | str,
        lease_manager: ApprovalLeaseManager | None = None,
        timeout_seconds: float | None = None,
        scope_ttl_seconds: int = 3600,
    ) -> None:
        self._emit = emit
        self._session_for_task = session_for_task
        self.workspace_root = str(workspace_root)
        self.lease_manager = lease_manager
        self.scope_ttl_seconds = scope_ttl_seconds
        if timeout_seconds is not None:
            self.default_timeout = float(timeout_seconds)
        else:
            try:
                self.default_timeout = float(os.getenv("HEX_APPROVAL_TIMEOUT", "60.0"))
            except ValueError:
                self.default_timeout = 60.0
        self._pending: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def has_pending(self, approval_id: str) -> bool:
        with self._lock:
            return approval_id in self._pending

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        """Notify the client and block until it decides or the timeout expires."""
        timeout = (
            request.timeout_seconds
            if request.timeout_seconds is not None and request.timeout_seconds > 0
            else self.default_timeout
        )
        entry: dict[str, Any] = {"request": request, "event": threading.Event(), "decision": None}
        with self._lock:
            self._pending[request.request_id] = entry

        cwd = self._cwd_for(request)
        try:
            self._emit({
                "jsonrpc": "2.0",
                "method": "session/approvalRequest",
                "params": {
                    "sessionId": self._session_for_task(request.task_id) or "default",
                    "approvalId": request.request_id,
                    "toolName": request.tool_name,
                    "riskLevel": request.risk_class,
                    "parameters": self.parameters_text(request.arguments),
                    "reason": (
                        f"Privileged {request.tool_name} action ({request.risk_class}); "
                        "review the command before allowing."
                    ),
                    "blastRadius": request.blast_radius,
                    "cwd": cwd,
                },
            })
            signaled = entry["event"].wait(timeout)
        finally:
            with self._lock:
                self._pending.pop(request.request_id, None)

        decision = entry["decision"]
        if signaled and decision is not None:
            return decision
        return ApprovalDecision(
            decision="DENY",
            platform="acp",
            timestamp=time.time(),
            reason=f"No operator decision within {timeout:.0f}s; denied by default.",
        )

    def resolve(self, approval_id: str, decision: str, reason: str | None = None) -> bool:
        """Apply a client decision to a waiting turn. False if nothing is waiting."""
        clean = (decision or "").strip().lower()
        if clean in _APPROVE_ONCE:
            outcome, grant_scope = "APPROVE", False
        elif clean in _APPROVE_SCOPE:
            outcome, grant_scope = "APPROVE", True
        else:
            outcome, grant_scope = "DENY", False

        with self._lock:
            entry = self._pending.get(approval_id)
            if entry is None:
                return False
            lease_id = self._grant_scope_lease(entry["request"]) if grant_scope else None
            entry["decision"] = ApprovalDecision(
                decision=outcome,
                responder_id="operator",
                platform="acp",
                timestamp=time.time(),
                reason=reason or ("Approved for this project" if lease_id else f"Operator decision: {outcome}"),
            )
            entry["event"].set()
        return True

    def _grant_scope_lease(self, request: ApprovalRequest) -> str | None:
        """Persist an 'allow in this project' grant so later turns skip the prompt."""
        if self.lease_manager is None:
            return None
        command = self.command_text(request.arguments)
        if command:
            try:
                tokens = shlex.split(command)
            except ValueError:
                tokens = command.split()
            prefix = os.path.basename(tokens[0]) if tokens else request.tool_name
            lease = self.lease_manager.grant_lease(
                command_prefix=prefix,
                ttl_seconds=self.scope_ttl_seconds,
                task_id=request.task_id,
                scope="session",
            )
        else:
            lease = self.lease_manager.grant_lease(
                allowed_paths=[self._cwd_for(request)],
                ttl_seconds=self.scope_ttl_seconds,
                task_id=request.task_id,
                scope="session",
            )
        return lease.lease_id

    def _cwd_for(self, request: ApprovalRequest) -> str:
        cwd = request.arguments.get("cwd") if isinstance(request.arguments, dict) else None
        return str(cwd) if isinstance(cwd, str) and cwd.strip() else self.workspace_root

    @staticmethod
    def command_text(arguments: dict[str, Any]) -> str:
        if not isinstance(arguments, dict):
            return ""
        for key in _COMMAND_KEYS:
            val = arguments.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
            if isinstance(val, list) and val:
                return " ".join(str(x) for x in val).strip()
        return ""

    @classmethod
    def parameters_text(cls, arguments: dict[str, Any]) -> str:
        command = cls.command_text(arguments)
        if command:
            return command
        if not arguments:
            return ""
        return json.dumps(arguments, ensure_ascii=False, default=str)