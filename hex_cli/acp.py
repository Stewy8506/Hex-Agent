"""Agent Client Protocol (ACP) Server for HEX Agent.

Implements the ACP JSON-RPC 2.0 protocol over stdio for IDE integration with Zed, Neovim, VSCode, and Cursor.
Standardized methods:
- initialize
- session/new
- session/load
- session/prompt
- session/cancel
- session/list
- tools/list
- tools/call
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness_core import __version__

logger = logging.getLogger(__name__)


def _bounded_tool_preview(payload: Any, *, error: bool = False) -> str:
    """Display-only JSON preview; never traverse or serialize the full result."""
    limit = 4000 if error else 300
    max_string = 1024 if error else 256
    nodes_left = 128
    priority = ("exit_code", "stderr", "error")

    def quoted(value: str, budget: int, cap: int = max_string) -> tuple[str, bool]:
        prefix = value[:min(cap, budget)]
        truncated = len(prefix) < len(value)
        text = json.dumps(prefix + ("..." if truncated else ""))
        if len(text) <= budget:
            return text, truncated
        # Only bounded prefixes are encoded, even for multi-megabyte strings.
        low, high = 0, len(prefix)
        while low < high:
            mid = (low + high + 1) // 2
            if len(json.dumps(prefix[:mid] + "...")) <= budget:
                low = mid
            else:
                high = mid - 1
        return json.dumps(prefix[:low] + "..."), True

    def visit(value: Any, budget: int, depth: int) -> tuple[str, bool]:
        nonlocal nodes_left
        nodes_left -= 1
        if depth >= 6 or nodes_left <= 0:
            return '"..."', True
        if isinstance(value, str):
            return quoted(value, budget)
        if value is None or isinstance(value, (bool, float, int)):
            if isinstance(value, int) and value.bit_length() > 256:
                return '"..."', True
            try:
                text = json.dumps(value, allow_nan=False)
            except ValueError:
                return "null", True
            return (text, False) if len(text) <= budget else ('"..."', True)
        if isinstance(value, (dict, list, tuple)):
            if budget < 32:
                return '"..."', True
            is_dict = isinstance(value, dict)
            parts: list[str] = []
            used = 2
            truncated = False
            marker = '"_truncated": true' if is_dict else '"..."'

            def entries() -> Any:
                if is_dict:
                    for key in priority:
                        if key in value:
                            yield key, value[key]
                    for key, child in value.items():
                        if key not in priority:
                            yield key, child
                else:
                    yield from enumerate(value)

            count = 0
            for key, child in entries():
                # Reserve closing delimiters and an explicit truncation marker.
                remaining = budget - used - len(marker) - 4
                if nodes_left <= 1 or remaining < 24:
                    truncated = True
                    break
                label = ""
                if is_dict:
                    key_text, key_cut = quoted(key if isinstance(key, str) else "<key>", remaining - 8, 64)
                    label = key_text + ": "
                    truncated |= key_cut
                child_budget = remaining - len(label) - (2 if parts else 0)
                if child_budget < 8:
                    truncated = True
                    break
                # A large stderr must not crowd out the error or exit code.
                if is_dict and key in priority:
                    child_budget = min(child_budget, max(8, budget // 3))
                text, child_cut = visit(child, child_budget, depth + 1)
                parts.append(label + text)
                used += len(label) + len(text) + (2 if len(parts) > 1 else 0)
                truncated |= child_cut
                count += 1
            truncated |= count < len(value)
            if truncated:
                parts.append(marker)
            opening, closing = ("{", "}") if is_dict else ("[", "]")
            return opening + ", ".join(parts) + closing, truncated
        # Avoid arbitrary __str__/__repr__ implementations with unbounded output.
        return quoted("<" + type(value).__name__[:64] + ">", budget)

    if error and not isinstance(payload, dict):
        payload = {"error": payload}
    if isinstance(payload, str):
        # Preserve plain success chips while bounding their UTF-8 wire size.
        return payload[:limit].encode("utf-8", errors="replace")[:limit].decode("utf-8", errors="ignore")
    return visit(payload, limit, 0)[0]


@dataclass
class ACPSession:
    """Active ACP editor session."""

    session_id: str
    workspace_root: Path
    model: str = "default"
    history: list[dict[str, Any]] = field(default_factory=list)
    cancelled: bool = False
    created_at: float = field(default_factory=time.time)
    last_task_id: str | None = None
    running: bool = False
    cancel_ack: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        self.cancel_ack.set()


class ACPServer:
    """Agent Client Protocol (ACP) JSON-RPC 2.0 stdio server."""

    def __init__(
        self,
        orchestrator: Any | None = None,
        workspace_root: Path | str | None = None,
    ) -> None:
        self.workspace_root = (
            Path(workspace_root).resolve() if workspace_root else Path.cwd().resolve()
        )
        self._orchestrator = orchestrator
        self.sessions: dict[str, ACPSession] = {}
        self.pending_approvals: dict[str, Any] = {}
        self.approval_bridge: Any | None = None
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()

    def _emit(self, obj: dict[str, Any]) -> None:
        """Serialize one JSON-RPC message to stdout (thread-safe)."""
        data = json.dumps(obj, ensure_ascii=False) + "\n"
        with self._write_lock:
            sys.stdout.write(data)
            sys.stdout.flush()

    # ------------------------------------------------------------------
    # Durable session transcripts: sessions survive server restarts and
    # can be resumed via session/load from any new server process.
    # ------------------------------------------------------------------

    @property
    def _sessions_dir(self) -> Path:
        return self.workspace_root / ".hex" / "data" / "acp_sessions"

    def _session_path(self, session_id: str) -> Path | None:
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", session_id or ""):
            return None
        return self._sessions_dir / f"{session_id}.json"

    def _persist_session(self, sess: ACPSession) -> None:
        path = self._session_path(sess.session_id)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "session_id": sess.session_id,
                "workspace_root": str(sess.workspace_root),
                "model": sess.model,
                "created_at": sess.created_at,
                "last_task_id": sess.last_task_id,
                "history": sess.history,
            }
            # Per-thread tmp name: the cancel handler and the turn worker can
            # persist the same session concurrently.
            tmp = path.with_suffix(f".{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception as exc:
            logger.warning("Failed to persist ACP session %s: %s", sess.session_id, exc)

    def _load_session_from_disk(self, session_id: str) -> ACPSession | None:
        path = self._session_path(session_id)
        if path is None or not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return ACPSession(
                session_id=data["session_id"],
                workspace_root=Path(data.get("workspace_root") or self.workspace_root),
                model=data.get("model", "default"),
                history=list(data.get("history") or []),
                created_at=float(data.get("created_at") or time.time()),
                last_task_id=data.get("last_task_id"),
            )
        except Exception as exc:
            logger.warning("Failed to load ACP session %s from disk: %s", session_id, exc)
            return None

    @property
    def orchestrator(self) -> Any:
        if self._orchestrator is None:
            from harness_core.core.orchestrator import HarnessOrchestrator
            data_dir = self.workspace_root / ".hex" / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            self._orchestrator = HarnessOrchestrator(
                data_dir=data_dir,
                default_workspace=self.workspace_root,
            )
        self._wire_approval_bridge(self._orchestrator)
        return self._orchestrator

    def _wire_approval_bridge(self, orchestrator: Any) -> None:
        """Route the gateway's approval interrupts to this server's ACP client."""
        gateway = getattr(orchestrator, "gateway", None)
        if gateway is None or getattr(gateway, "approval_bridge", None) is not None:
            return
        from harness_core.client.acp_approval import AcpApprovalBridge
        from harness_core.core.approval_lease import ApprovalLeaseManager

        if getattr(gateway, "approval_lease_manager", None) is None:
            gateway.approval_lease_manager = ApprovalLeaseManager(getattr(orchestrator, "ledger", None))
        self.approval_bridge = AcpApprovalBridge(
            emit=self._emit,
            session_for_task=self._session_for_task,
            workspace_root=self.workspace_root,
            lease_manager=gateway.approval_lease_manager,
        )
        gateway.approval_bridge = self.approval_bridge

    def _session_for_task(self, task_id: str | None) -> str | None:
        if not task_id:
            return None
        with self._lock:
            for sess in self.sessions.values():
                if sess.last_task_id == task_id:
                    return sess.session_id
        return None

    def handle_request(self, req: dict[str, Any]) -> dict[str, Any]:
        """Process incoming JSON-RPC 2.0 request and return structured response."""
        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}

        if not method:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32600, "message": "Invalid Request: missing method"},
            }

        try:
            if method == "initialize":
                res = self._handle_initialize(params)
            elif method == "session/new":
                res = self._handle_session_new(params)
            elif method == "session/load":
                res = self._handle_session_load(params)
            elif method == "session/prompt":
                res = self._handle_session_prompt(params)
            elif method == "session/cancel":
                res = self._handle_session_cancel(params)
            elif method == "session/list":
                res = self._handle_session_list(params)
            elif method == "tools/list":
                res = self._handle_tools_list(params)
            elif method == "tools/call":
                res = self._handle_tools_call(params)
            elif method == "session/approvalDecision":
                res = self._handle_session_approval_decision(params)
            elif method == "session/command":
                res = self._handle_session_command(params)
            elif method == "session/subagents":
                res = self._handle_session_subagents(params)
            elif method == "session/toolResult":
                res = self._handle_session_tool_result(params)
            elif method == "ping":
                res = {"pong": True}
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }

            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": res,
            }
        except Exception as exc:
            logger.exception("Error handling ACP method %s: %s", method, exc)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32602 if method == "session/toolResult" and isinstance(exc, (ValueError, TypeError)) else -32000,
                    "message": str(exc),
                },
            }

    def _handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "session": {
                    "streaming": True,
                    "cancel": True,
                    "history": True,
                    "loadSession": True,
                },
                "tools": {
                    "listChanged": False,
                },
                "prompts": {},
            },
            "serverInfo": {
                "name": "HEX",
                "version": __version__,
            },
        }

    def _handle_session_new(self, params: dict[str, Any]) -> dict[str, Any]:
        cwd_param = params.get("cwd") or params.get("rootUri") or str(self.workspace_root)
        if str(cwd_param).startswith("file://"):
            cwd_path = Path(str(cwd_param)[7:]).resolve()
        else:
            cwd_path = Path(cwd_param).resolve()

        session_id = f"acp_sess_{uuid.uuid4().hex[:12]}"
        model = params.get("model", "default")
        sess = ACPSession(
            session_id=session_id,
            workspace_root=cwd_path,
            model=model,
        )

        with self._lock:
            self.sessions[session_id] = sess
        self._persist_session(sess)

        tools_count = len(self.orchestrator.gateway.tools) if hasattr(self.orchestrator, "gateway") else 26
        
        disk_skills_count = 0
        from harness_core.resources import bundled_skills_dir

        builtin_skills_dir = bundled_skills_dir()
        if builtin_skills_dir.is_dir():
            disk_skills_count = len(list(builtin_skills_dir.rglob("SKILL.md")))
        skills_count = disk_skills_count or 83

        cow_backend = getattr(self.orchestrator.cow, "backend_name", "apfs_clone") if hasattr(self.orchestrator, "cow") else "apfs_clone"

        router_model = getattr(getattr(self.orchestrator, "router", None), "cloud_model_id", None)
        active_model = model or router_model or ""

        from harness_core.client.tui import _resolve_provider_label, render_hex_splash
        provider_label = _resolve_provider_label(self.orchestrator, active_model)

        splash_text = render_hex_splash(
            self.orchestrator,
            model_id=active_model,
            workspace_root=cwd_path,
            session_id=session_id,
            enable_color=False,
        )

        return {
            "sessionId": session_id,
            "cwd": str(cwd_path),
            "model": active_model,
            "provider": provider_label,
            "toolsCount": tools_count,
            "skillsCount": skills_count,
            "cowBackend": cow_backend,
            "splashText": splash_text,
        }

    def _handle_session_load(self, params: dict[str, Any]) -> dict[str, Any]:
        sid = params.get("sessionId")
        with self._lock:
            sess = self.sessions.get(sid)
        if sess is None:
            sess = self._load_session_from_disk(sid)
            if sess is not None:
                with self._lock:
                    self.sessions[sid] = sess
        if sess is None:
            raise KeyError(f"Session not found: {sid}")
        return {
            "sessionId": sess.session_id,
            "cwd": str(sess.workspace_root),
            "model": sess.model,
            "historyCount": len(sess.history),
            "history": sess.history,
        }

    def _handle_session_prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        sid = params.get("sessionId")
        with self._lock:
            sess = self.sessions.get(sid)
        if sess is None and sid:
            sess = self._load_session_from_disk(sid)
            if sess is not None:
                with self._lock:
                    self.sessions[sid] = sess
        if sess is None:
            # Auto-create session if client provided prompt without explicit new
            sess = ACPSession(
                session_id=sid or f"acp_sess_{uuid.uuid4().hex[:12]}",
                workspace_root=self.workspace_root,
            )
            with self._lock:
                self.sessions[sess.session_id] = sess
            self._persist_session(sess)

        if sess.running:
            raise ValueError(
                "A turn is already running in this session; wait for completion or send session/cancel."
            )

        prompt_data = params.get("prompt")
        prompt_text = ""
        if isinstance(prompt_data, str):
            prompt_text = prompt_data
        elif isinstance(prompt_data, list):
            for part in prompt_data:
                if isinstance(part, dict) and part.get("type") == "text":
                    prompt_text += part.get("text", "")
        elif isinstance(prompt_data, dict):
            prompt_text = prompt_data.get("text", str(prompt_data))

        # Cancellation is per-turn: a cancelled turn does not poison the session.
        sess.cancelled = False
        sess.cancel_ack.set()
        sess.running = True
        try:
            return self._execute_prompt_turn(sess, params, prompt_text)
        finally:
            sess.running = False
            self._persist_session(sess)

    def _execute_prompt_turn(
        self, sess: ACPSession, params: dict[str, Any], prompt_text: str
    ) -> dict[str, Any]:
        start_t = time.time()

        # Carry prior dialogue so follow-up turns keep conversation context.
        conversation_history: list[dict[str, str]] = []
        for turn in sess.history[-10:]:
            if turn.get("prompt"):
                conversation_history.append({"role": "user", "content": str(turn["prompt"])})
            if turn.get("response"):
                conversation_history.append({"role": "assistant", "content": str(turn["response"])})

        from harness_core.core.models import TaskSubmitRequest
        exec_mode = str(params.get("execution_mode") or "fast_path")
        task_req = TaskSubmitRequest(
            goal=prompt_text,
            project_id="acp_session",
            execution_mode=exec_mode,
            workspace_root=str(sess.workspace_root),
            constraints={
                "surface": "acp",
                "conversation_id": sess.session_id,
                "conversation_history": conversation_history,
            },
        )
        sub_res = self.orchestrator.submit_task(task_req)
        task_id = sub_res["task_id"]
        sess.last_task_id = task_id

        streamed = False

        def stream_cb(chunk: str) -> None:
            nonlocal streamed
            if not chunk or sess.cancelled:
                return
            streamed = True
            self._emit({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": sess.session_id,
                    "updateType": "agent_response",
                    "delta": chunk,
                },
            })

        active_tool_calls: dict[str, str] = {}

        def tool_cb(event_type: str, tool_id: str, payload: Any, dur_ms: int = 0) -> None:
            if sess.cancelled:
                return
            if event_type == "start":
                call_id = f"{tool_id}_{int(time.time() * 1000)}"
                active_tool_calls[tool_id] = call_id
            else:
                call_id = active_tool_calls.pop(tool_id, f"{tool_id}_{int(time.time() * 1000)}")

            tool = {
                "eventType": event_type,
                "toolId": tool_id,
                "callId": call_id,
                "durationMs": dur_ms,
                "payload": _bounded_tool_preview(payload, error=event_type == "error"),
            }
            if event_type != "start":
                tool["taskId"] = task_id
                ledger_call_id = payload.get("_call_id") if isinstance(payload, dict) else None
                if not ledger_call_id and event_type == "error":
                    # The orchestrator invokes this callback inside its except
                    # block; no journal lookup or tool-name guessing is needed.
                    exc = sys.exc_info()[1]
                    if getattr(exc, "_hex_task_id", None) == task_id:
                        ledger_call_id = getattr(exc, "_hex_ledger_call_id", None)
                if isinstance(ledger_call_id, str) and ledger_call_id:
                    tool["ledgerCallId"] = ledger_call_id

            self._emit({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": sess.session_id,
                    "updateType": "tool",
                    "tool": tool,
                },
            })

        exec_res = self.orchestrator.execute_task(
            task_id,
            stream_callback=stream_cb,
            tool_callback=tool_cb,
        )

        raw_status = exec_res.get("status", "COMPLETED")
        status_str = "completed" if raw_status in ("SUCCEEDED", "COMPLETED") else raw_status.lower()
        if status_str == "canceled":
            status_str = "cancelled"
        if sess.cancelled:
            status_str = "cancelled"

        if status_str == "cancelled":
            # The cancel response must reach the client before this turn's
            # terminal events; run_stdio sets the ack after emitting it.
            sess.cancel_ack.wait(timeout=5.0)
            terminal_reason = "Session execution was cancelled."
            sess.history.append({
                "task_id": task_id,
                "prompt": prompt_text,
                "status": status_str,
                "response": terminal_reason,
                "duration": time.time() - start_t,
            })
            self._emit({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": sess.session_id,
                    "updateType": "status",
                    "status": "Cancelled",
                },
            })
            return {
                "sessionId": sess.session_id,
                "status": "cancelled",
                "content": [{"type": "text", "text": terminal_reason}],
                "taskId": task_id,
            }

        if streamed:
            # Emit stream_end notification immediately matching Hermes _stream_delta(None)
            self._emit({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": sess.session_id,
                    "updateType": "stream_end",
                    "status": "Ready",
                },
            })

        # Extract synthesized response or deliverable from task
        deliverable = (
            exec_res.get("payload", {}).get("result")
            or exec_res.get("deliverable")
            or exec_res.get("result")
        )
        if not deliverable and hasattr(self.orchestrator, "ledger"):
            try:
                db_task = self.orchestrator.ledger.get_task(task_id)
                if db_task:
                    deliverable = (
                        db_task.get("deliverable")
                        or (db_task.get("payload") or {}).get("result")
                        or db_task.get("result")
                    )
            except Exception:
                pass

        terminal_reason = (
            deliverable
            or exec_res.get("terminal_reason")
            or exec_res.get("model_response")
            or "Task completed."
        )

        sess.history.append({
            "task_id": task_id,
            "prompt": prompt_text,
            "status": status_str,
            "response": terminal_reason,
            "duration": time.time() - start_t,
        })

        # Send final ready notification (if no stream occurred, include delta)
        update_notif: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": sess.session_id,
                "updateType": "status",
                "status": "Ready",
            },
        }
        if not streamed:
            update_notif["params"]["delta"] = str(terminal_reason)
        self._emit(update_notif)

        return {
            "sessionId": sess.session_id,
            "status": status_str,
            "content": [
                {
                    "type": "text",
                    "text": str(terminal_reason),
                }
            ],
            "taskId": task_id,
        }

    def _handle_session_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        sid = params.get("sessionId")
        with self._lock:
            sess = self.sessions.get(sid)
        if sess is None:
            raise KeyError(f"Session not found: {sid}")
        sess.cancelled = True

        # Reach the active execution: cooperative cancel flag + ledger
        # transition + CoW rollback of the uncommitted workspace layer.
        tid = sess.last_task_id
        if tid and sess.running and hasattr(self.orchestrator, "cancel_task"):
            sess.cancel_ack.clear()
            try:
                from harness_core.core.models import TaskState

                task = None
                ledger = getattr(self.orchestrator, "ledger", None)
                if ledger is not None:
                    task = ledger.get_task(tid)
                terminal = task is not None and TaskState(task["status"]).is_terminal
                if not terminal:
                    self.orchestrator.cancel_task(tid, actor="acp")
            except Exception as exc:
                logger.debug("ACP cancel could not reach task %s: %s", tid, exc)

        self._persist_session(sess)
        return {"sessionId": sid, "cancelled": True}

    def _handle_session_list(self, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            known = dict(self.sessions)
        # Merge durable sessions from disk so restarts keep the listing complete.
        if self._sessions_dir.is_dir():
            for path in sorted(self._sessions_dir.glob("*.json")):
                sid = path.stem
                if sid not in known:
                    sess = self._load_session_from_disk(sid)
                    if sess is not None:
                        known[sid] = sess
        sess_list = [
            {
                "sessionId": s.session_id,
                "cwd": str(s.workspace_root),
                "model": s.model,
                "created_at": s.created_at,
                "turns": len(s.history),
            }
            for s in known.values()
        ]
        return {"sessions": sess_list}

    def _handle_tools_list(self, params: dict[str, Any]) -> dict[str, Any]:
        from harness_core.gateway import ToolGateway
        defs = ToolGateway.list_tool_definitions()
        return {"tools": defs}

    def _handle_tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        args = params.get("arguments") or {}
        if not name:
            raise ValueError("Tool call missing 'name' parameter")

        spec = self.orchestrator.gateway.tools.get(name)
        if spec is None:
            raise KeyError(f"Unknown tool: {name}")

        inv_res = spec.handler(self.workspace_root, args)

        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(inv_res, ensure_ascii=False, default=str),
                }
            ]
        }

    def _handle_session_approval_decision(self, params: dict[str, Any]) -> dict[str, Any]:
        approval_id = params.get("approvalId") or params.get("approval_id")
        decision = (params.get("decision") or "reject").lower()
        reason = params.get("reason", "")
        if not approval_id:
            raise ValueError("approvalId is required")

        with self._lock:
            self.pending_approvals[approval_id] = {
                "decision": decision,
                "reason": reason,
                "timestamp": time.time(),
                "status": "decided",
            }

        applied = False
        if self.approval_bridge is not None:
            applied = self.approval_bridge.resolve(approval_id, decision, reason or None)

        return {
            "approvalId": approval_id,
            "decision": decision,
            "status": "applied" if applied else "acknowledged",
        }

    def _handle_session_tool_result(self, params: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise ValueError("session/toolResult params must be an object")
        if params.keys() - {"sessionId", "taskId", "callId", "offset", "limit"}:
            raise ValueError("Unknown session/toolResult parameter")
        for key in ("sessionId", "taskId", "callId"):
            value = params.get(key)
            if not isinstance(value, str) or not value.strip() or len(value) > 256:
                raise ValueError(f"{key} must be a nonempty string of at most 256 characters")
        sid, task_id, call_id = params["sessionId"], params["taskId"], params["callId"]
        with self._lock:
            sess = self.sessions.get(sid)
        if sess is None:
            sess = self._load_session_from_disk(sid)
        if sess is None:
            raise KeyError(f"Session not found: {sid}")
        if task_id != sess.last_task_id and not any(
            turn.get("task_id") == task_id for turn in sess.history
        ):
            raise KeyError("Task does not belong to this session")
        ledger = getattr(self.orchestrator, "ledger", None)
        if ledger is None:
            raise KeyError("Tool result storage unavailable")
        return ledger.get_tool_result_page(
            task_id, call_id, offset=params.get("offset", 0), limit=params.get("limit", 4096)
        )

    def _handle_session_subagents(self, params: dict[str, Any]) -> dict[str, Any]:
        """Structured subagent telemetry for this session's own tasks.

        The cockpit's agents rail and drill-down modal render from this, so it
        reports only what the SubagentManager recorded — never invented activity.
        """
        sid = params.get("sessionId") or params.get("session_id") or "default"
        with self._lock:
            sess = self.sessions.get(sid)
        if sess is None:
            sess = self._load_session_from_disk(sid)

        task_ids: set[str] = set()
        if sess is not None:
            if sess.last_task_id:
                task_ids.add(str(sess.last_task_id))
            for turn in sess.history:
                tid = turn.get("task_id")
                if tid:
                    task_ids.add(str(tid))

        records: list[dict[str, Any]] = []
        mgr = getattr(self.orchestrator, "subagent_manager", None)
        if mgr is not None and task_ids:
            for rec in mgr.list_active_subagents():
                if rec.get("parent_task_id") not in task_ids:
                    continue
                try:
                    full = mgr.get_subagent_status(rec["subagent_id"])
                except Exception:
                    full = dict(rec)
                records.append(self._subagent_wire(full))
        return {"sessionId": sid, "subagents": records}

    @staticmethod
    def _subagent_wire(rec: dict[str, Any]) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "subagentId": rec.get("subagent_id", ""),
            "parentTaskId": rec.get("parent_task_id", ""),
            "role": rec.get("role", "general"),
            "goal": rec.get("goal", ""),
            "depth": int(rec.get("depth") or 0),
            "status": rec.get("status", "pending"),
            "startedAt": float(rec.get("started_at") or 0.0),
            "endedAt": rec.get("ended_at"),
            "interrupted": bool(rec.get("interrupted")),
            "toolCallsCount": int(rec.get("tool_calls_count") or 0),
        }
        result = rec.get("result")
        if result:
            wire["result"] = {
                "status": result.get("status", wire["status"]),
                "summary": result.get("summary", ""),
                "findings": list(result.get("findings") or []),
                "diffSummary": result.get("diff_summary", ""),
                "artifacts": list(result.get("artifacts") or []),
                "durationSeconds": float(result.get("duration_seconds") or 0.0),
                "error": result.get("error"),
            }
        return wire

    def _handle_session_command(self, params: dict[str, Any]) -> dict[str, Any]:
        sid = params.get("sessionId") or params.get("session_id", "default")
        command_raw = (params.get("command") or "").strip()
        args = (params.get("args") or "").strip()

        if not command_raw:
            raise ValueError("command parameter is required")

        cmd = command_raw.split()[0].lower()
        if not args and len(command_raw.split()) > 1:
            args = command_raw[len(cmd):].strip()

        with self._lock:
            sess = self.sessions.get(sid)
        if sess is None:
            sess = ACPSession(session_id=sid, workspace_root=self.workspace_root)
            self.sessions[sid] = sess

        handlers = self._command_handlers()

        handler = handlers.get(cmd)
        if handler is None:
            from harness_core.client.command_catalog import load_catalog

            for spec in load_catalog():
                if spec.supported_on("acp") and cmd in spec.all_names():
                    handler = handlers.get(spec.name)
                    break
        if handler is None:
            return {
                "sessionId": sid,
                "command": cmd,
                "output": f"Unknown command: {cmd}. Type /help for available commands.",
                "isError": True,
            }

        try:
            output = handler(sess, args)
            is_error = False
        except Exception as exc:
            output = f"Command failed: {exc}"
            is_error = True

        return {
            "sessionId": sid,
            "command": cmd,
            "output": output,
            "isError": is_error,
        }

    # ------------------------------------------------------------------
    # Slash-command implementations. Every handler performs the operation
    # it reports (or raises); none may emit a fabricated success message.
    # Availability is declared in the shared command_catalog.json (F06).
    # ------------------------------------------------------------------

    def _command_handlers(self) -> dict[str, Any]:
        return {
            "/help": self._cmd_help,
            "/status": self._cmd_status,
            "/doctor": self._cmd_doctor,
            "/skills": self._cmd_skills,
            "/blast": self._cmd_blast,
            "/graph": self._cmd_blast,
            "/proof": self._cmd_proof,
            "/model": self._cmd_model,
            "/budget": self._cmd_budget,
            "/subagents": self._cmd_subagents,
            "/react": self._cmd_react,
            "/loop": self._cmd_react,
            "/diff": self._cmd_diff,
            "/sbs": self._cmd_sbs,
            "/scan": self._cmd_scan,
            "/scan-skill": self._cmd_scan,
            "/quota": self._cmd_quota,
            "/antigravity": self._cmd_quota,
            "/ag": self._cmd_quota,
            "/memory": self._cmd_memory,
            "/mem": self._cmd_memory,
            "/remember": self._cmd_remember,
            "/reflect": self._cmd_reflect,
            "/lessons": self._cmd_reflect,
            "/search": self._cmd_search,
            "/recipe": self._cmd_recipe,
            "/lsp": self._cmd_lsp,
            "/compiler": self._cmd_lsp,
            "/fork": self._cmd_fork,
            "/compressor": self._cmd_compressor,
            "/comp": self._cmd_compressor,
            "/rollback": self._cmd_rollback,
        }

    def _cmd_help(self, sess: ACPSession, args: str) -> str:
        from harness_core.client.command_catalog import render_help

        return render_help("acp")

    def _cmd_status(self, sess: ACPSession, args: str) -> str:
        lines = [
            f"Session ID: {sess.session_id}",
            f"Workspace: {sess.workspace_root}",
            f"Model: {sess.model}",
            f"Turns completed: {len(sess.history)}",
        ]
        if sess.last_task_id:
            lines.append(f"Last task: {sess.last_task_id}")
            ledger = getattr(self.orchestrator, "ledger", None)
            if ledger is not None:
                task = ledger.get_task(sess.last_task_id)
                if task:
                    lines.append(f"Last task status: {task.get('status')}")
        else:
            lines.append("Last task: none")
        return "\n".join(lines)

    def _cmd_doctor(self, sess: ACPSession, args: str) -> str:
        from harness_core.client.doctor import HexDoctor

        doc = HexDoctor(
            workspace_root=self.workspace_root,
            data_dir=getattr(self.orchestrator, "data_dir", None),
        )
        report = doc.run_diagnostics(fix="--fix" in args)
        return report.format_summary(color=False)

    def _cmd_skills(self, sess: ACPSession, args: str) -> str:
        ledger = getattr(self.orchestrator, "ledger", None)
        if ledger is None or not hasattr(ledger, "list_skills"):
            raise ValueError("Skill registry unavailable: orchestrator has no ledger.")
        skills = ledger.list_skills()
        if not skills:
            return "No skills registered in the ledger."
        lines = [f"Registered Skills ({len(skills)}):"]
        for s in skills[:20]:
            name = s.get("name") or s.get("skill_id")
            gate = s.get("gate_decision") or s.get("risk_gate") or "unknown"
            lines.append(f"  • {name} (gate: {gate})")
        return "\n".join(lines)

    def _cmd_blast(self, sess: ACPSession, args: str) -> str:
        graphify = getattr(self.orchestrator, "graphify", None)
        if graphify is None:
            raise ValueError("Graphify adapter not initialized on this orchestrator.")
        target = args.strip()
        ws = sess.workspace_root
        if target in ("--html", "html"):
            out_path = ws / ".hex" / "architecture_tree.html"
            saved = graphify.export_interactive_html(out_path, workspace_dir=ws)
            return f"Interactive architecture tree exported to: {saved}"
        graphify.build_or_refresh(ws)
        if not target or target.endswith((".py", ".ts", ".tsx", ".js", ".go", ".rs")):
            files = [target] if target else list(graphify.symbols_by_file.keys())[:2]
            data = graphify.compute_blast_radius(ws, files).to_dict()
        else:
            q = graphify.query_symbol(target)
            data = {
                "modified_files": [f"{d['file']}:{d['qualified_name']}" for d in q.get("definitions", [])] or [target],
                "direct_callers": [c["qualified_name"] for c in q.get("hop1_callers", [])],
                "transitive_callers": [c["qualified_name"] for c in q.get("hop2_callers", [])],
            }
        lines = [f"Blast radius for {target or 'workspace sample'}:"]
        lines.append(f"  Modified: {', '.join(data.get('modified_files') or []) or 'none'}")
        lines.append(f"  Direct callers ({len(data.get('direct_callers') or [])}): {', '.join((data.get('direct_callers') or [])[:10]) or 'none'}")
        lines.append(f"  Transitive callers ({len(data.get('transitive_callers') or [])}): {', '.join((data.get('transitive_callers') or [])[:10]) or 'none'}")
        return "\n".join(lines)

    def _cmd_proof(self, sess: ACPSession, args: str) -> str:
        ledger = getattr(self.orchestrator, "ledger", None)
        if ledger is None:
            raise ValueError("Ledger unavailable: cannot inspect proof artifacts.")
        tid = args.strip() or sess.last_task_id
        if not tid:
            tasks = ledger.list_tasks(limit=1)
            tid = tasks[0]["task_id"] if tasks else None
        if not tid:
            return "No task available for proof inspection."
        proof = ledger.get_proof_bundle(tid)
        if not proof:
            return f"No Proof-Carrying Artifact bundle found for task {tid}."
        lines = [
            f"Proof bundle {proof['proof_id']} (task {proof['task_id']}):",
            f"  Workspace SHA:    {proof['workspace_tree_sha']}",
            f"  Git diff SHA:     {proof['git_diff_sha']}",
            f"  Ledger signature: {proof['ledger_signature']}",
        ]
        if hasattr(ledger, "verify_chain_integrity"):
            report = ledger.verify_chain_integrity(tid)
            state = "VALID" if report.valid else f"INVALID ({len(report.errors)} errors)"
            lines.append(f"  Event chain: {state} ({report.verified_events} events verified)")
        return "\n".join(lines)

    def _cmd_model(self, sess: ACPSession, args: str) -> str:
        if not args:
            router = getattr(self.orchestrator, "router", None)
            current = getattr(router, "cloud_model_id", None) or sess.model
            return f"Active model: {current} (session profile: {sess.model})"
        if not hasattr(self.orchestrator, "set_active_model"):
            raise ValueError("Model switching unavailable: orchestrator has no set_active_model.")
        prov, final_id, _saved = self.orchestrator.set_active_model(args, durable=False)
        sess.model = final_id
        return f"Active model switched to: {final_id} (provider: {prov}; session-only)"

    def _cmd_budget(self, sess: ACPSession, args: str) -> str:
        router = getattr(self.orchestrator, "router", None)
        tracker = getattr(router, "budget_tracker", None)
        if tracker is None:
            tracker = getattr(getattr(router, "cloud_provider", None), "budget_tracker", None)
        if tracker is None:
            raise ValueError("Budget telemetry unavailable: no active provider budget tracker.")
        spent = getattr(tracker, "total_usd_spent", 0.0)
        lines = [f"Provider budget telemetry:", f"  Lifetime spend: ${spent:.4f} USD"]
        for attr, label in (("total_tokens_in", "Input tokens"), ("total_tokens_out", "Output tokens")):
            val = getattr(tracker, attr, None)
            if val is not None:
                lines.append(f"  {label}: {val:,}")
        return "\n".join(lines)

    def _cmd_subagents(self, sess: ACPSession, args: str) -> str:
        mgr = getattr(self.orchestrator, "subagent_manager", None)
        if mgr is None:
            raise ValueError("Subagent manager not initialized on this orchestrator.")
        active = mgr.list_active_subagents()
        if not active:
            return "No active or historical subagents found."
        lines = [f"Subagent registry ({len(active)}):"]
        for sa in active:
            lines.append(
                f"  • {sa['subagent_id']} role={sa['role']} depth={sa['depth']} "
                f"status={sa['status']} calls={sa['tool_calls_count']} goal={sa['goal'][:60]}"
            )
        return "\n".join(lines)

    def _cmd_react(self, sess: ACPSession, args: str) -> str:
        if not hasattr(self.orchestrator, "react_enabled"):
            raise ValueError("ReAct mode control unavailable on this orchestrator.")
        arg_lower = args.lower()
        if arg_lower == "off":
            self.orchestrator.react_enabled = False
        elif arg_lower == "on":
            self.orchestrator.react_enabled = True
        else:
            self.orchestrator.react_enabled = not self.orchestrator.react_enabled
        state = "ENABLED" if self.orchestrator.react_enabled else "DISABLED"
        return f"Autonomous ReAct loop: {state}"

    def _cmd_diff(self, sess: ACPSession, args: str) -> str:
        import subprocess

        git_diff = subprocess.check_output(
            ["git", "diff", "--stat"],
            cwd=sess.workspace_root,
            text=True,
            timeout=5,
        )
        return git_diff.strip() or "No uncommitted modifications in active workspace."

    def _cmd_sbs(self, sess: ACPSession, args: str) -> str:
        import subprocess

        target_file = args.strip()
        if not target_file:
            raise ValueError("Usage: /sbs <filepath>")
        path = sess.workspace_root / target_file
        if not path.is_file():
            raise FileNotFoundError(f"File not found: {target_file}")
        diff = subprocess.check_output(
            ["git", "diff", "--", target_file],
            cwd=sess.workspace_root,
            text=True,
            timeout=5,
        )
        return diff.strip() or f"No uncommitted changes for {target_file}."

    def _cmd_scan(self, sess: ACPSession, args: str) -> str:
        path_str = args.strip()
        if not path_str:
            raise ValueError("Usage: /scan <skill_bundle_directory>")
        scanner = getattr(self.orchestrator, "scanner", None)
        if scanner is None:
            raise ValueError("SkillSpector scanner unavailable on this orchestrator.")
        scan_dir = (sess.workspace_root / path_str).resolve()
        if not scan_dir.is_dir():
            raise FileNotFoundError(f"Skill bundle directory not found: {scan_dir}")
        res = scanner.scan_and_register_skill(scan_dir)
        scan = res.get("scan") or {}
        gate = scan.get("gate_decision", "UNKNOWN")
        score = scan.get("risk_score", 0)
        return (
            f"SkillSpector scan complete: {res.get('name')} ({res.get('skill_id')}) "
            f"→ gate: {gate} (risk score: {score}/100)"
        )

    def _cmd_quota(self, sess: ACPSession, args: str) -> str:
        from harness_core.providers.antigravity_provider import (
            AntigravityModelProvider,
            AntigravityOAuthManager,
        )

        mgr = AntigravityOAuthManager()
        cred = mgr.resolve_credential(auto_refresh=False)
        if not cred or not (cred.access_token or cred.refresh_token):
            return (
                "Antigravity status: unauthenticated.\n"
                "Run `hex auth antigravity --auth-url` or sign in to the Antigravity IDE."
            )
        lines = ["Antigravity status: authenticated", f"  Credential source: {cred.source}"]
        if cred.project_id:
            lines.append(f"  Project ID: {cred.project_id}")
        prov = getattr(getattr(self.orchestrator, "router", None), "cloud_provider", None)
        if not isinstance(prov, AntigravityModelProvider):
            prov = AntigravityModelProvider(oauth_manager=mgr)
        try:
            snap = prov.fetch_quota_status()
            lines.append(f"  Plan tier: {snap.plan_name}")
            if snap.available_prompt_credits is not None:
                lines.append(f"  Prompt credits: {snap.available_prompt_credits:,}")
            for m in snap.models or []:
                rem_pct = round(m.remaining_fraction * 100.0, 1)
                reset = f" (resets {m.reset_time})" if m.reset_time else ""
                lines.append(f"  • {m.model_id}: {rem_pct}% remaining{reset}")
        except Exception as exc:
            lines.append(f"  Live quota probe failed: {exc}")
        return "\n".join(lines)

    def _cmd_memory(self, sess: ACPSession, args: str) -> str:
        memory = getattr(self.orchestrator, "memory", None)
        if memory is None:
            raise ValueError("Governed memory service unavailable on this orchestrator.")
        items = memory.retrieve_memories(
            project_id="acp_session",
            query=args.strip(),
            workspace_dir=sess.workspace_root,
        )
        if not items:
            return f"No governed memories matched '{args.strip()}'." if args.strip() else "No governed memories recorded for this project."
        lines = [f"Governed memories ({len(items)}):"]
        for m in items[:20]:
            stale = " [stale]" if m.get("is_stale") else ""
            lines.append(f"  • {m.get('memory_id')} '{m.get('subject')}': {str(m.get('content'))[:120]}{stale}")
        return "\n".join(lines)

    def _cmd_remember(self, sess: ACPSession, args: str) -> str:
        if ":" not in args:
            raise ValueError("Usage: /remember <subject>: <content>")
        memory = getattr(self.orchestrator, "memory", None)
        if memory is None:
            raise ValueError("Governed memory service unavailable on this orchestrator.")
        subject, content = args.split(":", 1)
        if not subject.strip() or not content.strip():
            raise ValueError("Usage: /remember <subject>: <content> (both parts required)")
        mem = memory.write_memory(
            scope="project",
            project_id="acp_session",
            subject=subject.strip(),
            content=content.strip(),
            workspace_dir=sess.workspace_root,
        )
        return f"Stored governed project memory {mem['memory_id']} ('{mem['subject']}')."

    def _cmd_reflect(self, sess: ACPSession, args: str) -> str:
        graphify = getattr(self.orchestrator, "graphify", None)
        if graphify is None or not hasattr(graphify, "reflect_work_memory"):
            raise ValueError("Graphify work-memory reflection unavailable.")
        res = graphify.reflect_work_memory(sess.workspace_root)
        md = (res.get("lessons_markdown") or "").strip()
        header = f"Work-memory reflection ({res.get('lessons_path')}):"
        return f"{header}\n{md}" if md else f"{header}\nNo work-memory outcomes recorded yet."

    def _cmd_search(self, sess: ACPSession, args: str) -> str:
        query = args.strip()
        if not query:
            raise ValueError("Usage: /search <query>")
        ledger = getattr(self.orchestrator, "ledger", None)
        if ledger is None or not hasattr(ledger, "search_tasks"):
            raise ValueError("Task journal search unavailable: orchestrator has no FTS5 ledger.")
        results = ledger.search_tasks(query)
        if not results:
            return f"No tasks matched '{query}'."
        lines = [f"FTS5 search results ({len(results)}):"]
        for r in results:
            lines.append(f"  • [{r.get('task_id')}] ({r.get('status')}) score={r.get('score')}: {r.get('snippet')}")
        return "\n".join(lines)

    def _cmd_recipe(self, sess: ACPSession, args: str) -> str:
        from harness_core.capabilities.recipes import detect_recipe

        rcp = detect_recipe(sess.workspace_root)
        if not rcp:
            return f"No build/test recipe detected in {sess.workspace_root}."
        return (
            f"Detected recipe: {rcp.name} ({rcp.kind})\n"
            f"  Bootstrap: {rcp.bootstrap or 'None'}\n"
            f"  Build:     {rcp.build or 'None'}\n"
            f"  Test:      {rcp.test}\n"
            f"  Start:     {rcp.start or 'None'}"
        )

    def _cmd_lsp(self, sess: ACPSession, args: str) -> str:
        from harness_core.lsp.registry import LanguageServerRegistry

        tokens = args.strip().split()
        subaction = tokens[0].lower() if tokens else "status"
        reg = LanguageServerRegistry()
        if subaction in ("diag", "diagnostics"):
            mgr = getattr(self.orchestrator, "lsp_manager", None)
            if mgr is None:
                raise ValueError("LSP diagnostics unavailable: no LSP manager on this orchestrator.")
            target_file = tokens[1] if len(tokens) > 1 else None
            if target_file:
                f_path = (sess.workspace_root / target_file).resolve()
                if not f_path.is_file():
                    raise FileNotFoundError(f"File not found: {target_file}")
                mgr.sync_file(f_path)
                diags = mgr.get_diagnostics(file_path=f_path)
            else:
                diags = mgr.get_diagnostics()
            errors = [d for d in diags if d.severity == 1]
            warnings = [d for d in diags if d.severity == 2]
            lines = [f"LSP diagnostics: {len(errors)} errors, {len(warnings)} warnings, {len(diags)} total"]
            for d in diags[:30]:
                sev = "ERROR" if d.severity == 1 else "WARN"
                lines.append(f"  [{sev}] {d.file_path}:{d.line + 1}:{d.character + 1} {d.message}")
            return "\n".join(lines)
        specs = reg.get_all_specs()
        detected = {s.language_id for s in reg.detect_workspace_servers(sess.workspace_root)}
        lines = ["Language server status (* = detected in workspace):"]
        for spec in specs:
            installed = "installed" if spec.resolve_binary() else "not found"
            mark = "*" if spec.language_id in detected else " "
            lines.append(f"  {mark} {spec.language_id:<12} {spec.server_name:<26} [{installed}]")
        return "\n".join(lines)

    def _resolve_task_id(self, sess: ACPSession, args: str) -> str:
        tid = args.strip() or sess.last_task_id
        if not tid:
            raise ValueError("No task specified and no task has run in this session. Usage: pass a task_id.")
        return tid

    def _cmd_fork(self, sess: ACPSession, args: str) -> str:
        if not hasattr(self.orchestrator, "fork_speculative_branches"):
            raise ValueError("Speculative forking unavailable on this orchestrator.")
        tid = self._resolve_task_id(sess, args)
        res = self.orchestrator.fork_speculative_branches(
            tid,
            branch_strategies=[{"name": "speculative_branch_a"}, {"name": "speculative_branch_b"}],
        )
        winner = res.get("winner_branch_id") or res.get("winning_branch") or res.get("status") or "completed"
        return f"Speculative branches evaluated for {tid}. Promoted: {winner}"

    def _cmd_compressor(self, sess: ACPSession, args: str) -> str:
        router = getattr(self.orchestrator, "router", None)
        if router is None:
            raise ValueError("Compressor control unavailable: orchestrator has no router.")
        mode = args.strip().lower()
        valid = ("deterministic", "cloud", "local", "off", "disabled")
        if not mode:
            cur = getattr(router, "compressor_mode", "deterministic")
            return f"Current compressor mode: {cur} (usage: /compressor <{'|'.join(valid)}>)"
        if mode not in valid:
            raise ValueError(f"Invalid compressor mode '{mode}'. Valid: {', '.join(valid)}")
        router.compressor_mode = "off" if mode == "disabled" else mode
        return f"Compressor mode set to: {router.compressor_mode} (session-only)"

    def _cmd_rollback(self, sess: ACPSession, args: str) -> str:
        if not hasattr(self.orchestrator, "cancel_task"):
            raise ValueError("Rollback unavailable: orchestrator has no cancel_task.")
        tid = self._resolve_task_id(sess, args)
        res = self.orchestrator.cancel_task(tid, actor="acp")
        sess.last_task_id = None
        status = res.get("status") if isinstance(res, dict) else "CANCELED"
        return f"Task {tid} canceled (status: {status}); uncommitted CoW workspace layer rolled back."

    def run_stdio(self) -> None:
        """Run stdio JSON-RPC loop reading lines or Content-Length framing.

        session/prompt executes on a worker thread so the loop keeps reading
        stdin while a turn runs — session/cancel and approval decisions can
        reach an in-flight execution instead of queueing behind it.
        """
        # Ensure logs strictly go to stderr so stdout is purely JSON-RPC
        logging.basicConfig(stream=sys.stderr, level=logging.INFO)

        def _respond_async(req: dict[str, Any]) -> None:
            try:
                resp = self.handle_request(req)
            except Exception as exc:
                resp = {
                    "jsonrpc": "2.0",
                    "id": req.get("id"),
                    "error": {"code": -32000, "message": str(exc)},
                }
            self._emit(resp)

        while True:
            try:
                line = sys.stdin.readline()
                if not line:
                    break  # EOF

                line_str = line.strip()
                if not line_str:
                    continue

                # Support Content-Length header or direct raw JSON line
                if line_str.lower().startswith("content-length:"):
                    length = int(line_str.split(":", 1)[1].strip())
                    # Consume empty header separator
                    sep = sys.stdin.readline()
                    raw_body = sys.stdin.read(length)
                    req = json.loads(raw_body)
                else:
                    req = json.loads(line_str)

                if req.get("method") == "session/prompt":
                    threading.Thread(target=_respond_async, args=(req,), daemon=True).start()
                else:
                    resp = self.handle_request(req)
                    self._emit(resp)
                    if req.get("method") == "session/cancel":
                        with self._lock:
                            sess = self.sessions.get((req.get("params") or {}).get("sessionId"))
                        if sess is not None:
                            sess.cancel_ack.set()
            except (KeyboardInterrupt, BrokenPipeError):
                break
            except Exception as exc:
                err_resp = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {exc}"},
                }
                self._emit(err_resp)
