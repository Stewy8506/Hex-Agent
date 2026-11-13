"""Agent Client Protocol (ACP) Server for HEX Autonomous AI Workbench.

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
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ACPSession:
    """Active ACP editor session."""

    session_id: str
    workspace_root: Path
    model: str = "default"
    history: list[dict[str, Any]] = field(default_factory=list)
    cancelled: bool = False
    created_at: float = field(default_factory=time.time)


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
        self._lock = threading.Lock()

    @property
    def orchestrator(self) -> Any:
        if self._orchestrator is None:
            from .orchestrator import HarnessOrchestrator
            data_dir = self.workspace_root / ".hex" / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            self._orchestrator = HarnessOrchestrator(
                data_dir=data_dir,
                default_workspace=self.workspace_root,
            )
        return self._orchestrator

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
                "error": {"code": -32000, "message": str(exc)},
            }

    def _handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "session": {
                    "streaming": True,
                    "cancel": True,
                    "history": True,
                },
                "tools": {
                    "listChanged": False,
                },
                "prompts": {},
            },
            "serverInfo": {
                "name": "HEX",
                "version": "0.20.1",
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

        return {
            "sessionId": session_id,
            "cwd": str(cwd_path),
            "model": model,
        }

    def _handle_session_load(self, params: dict[str, Any]) -> dict[str, Any]:
        sid = params.get("sessionId")
        with self._lock:
            sess = self.sessions.get(sid)
        if sess is None:
            raise KeyError(f"Session not found: {sid}")
        return {
            "sessionId": sess.session_id,
            "cwd": str(sess.workspace_root),
            "model": sess.model,
            "historyCount": len(sess.history),
        }

    def _handle_session_prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        sid = params.get("sessionId")
        with self._lock:
            sess = self.sessions.get(sid)
        if sess is None:
            # Auto-create session if client provided prompt without explicit new
            sess = ACPSession(
                session_id=sid or f"acp_sess_{uuid.uuid4().hex[:12]}",
                workspace_root=self.workspace_root,
            )
            self.sessions[sess.session_id] = sess

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

        # Check cancellation
        if sess.cancelled:
            return {
                "sessionId": sess.session_id,
                "status": "cancelled",
                "content": [{"type": "text", "text": "Session execution was cancelled."}],
            }

        # Execute through orchestrator
        start_t = time.time()
        task_id = f"tsk_acp_{uuid.uuid4().hex[:8]}"

        # Submit task
        from .models import TaskSubmitRequest
        task_req = TaskSubmitRequest(
            goal=prompt_text,
            project_id="acp_session",
            execution_mode="fast_path",
            workspace_root=str(sess.workspace_root),
        )
        sub_res = self.orchestrator.submit_task(task_req)
        task_id = sub_res["task_id"]
        exec_res = self.orchestrator.execute_task(task_id)

        raw_status = exec_res.get("status", "COMPLETED")
        status_str = "completed" if raw_status in ("SUCCEEDED", "COMPLETED") else raw_status.lower()
        terminal_reason = exec_res.get("terminal_reason") or exec_res.get("model_response") or "Task completed."

        sess.history.append({
            "task_id": task_id,
            "prompt": prompt_text,
            "status": status_str,
            "response": terminal_reason,
            "duration": time.time() - start_t,
        })

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
        return {"sessionId": sid, "cancelled": True}

    def _handle_session_list(self, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            sess_list = [
                {
                    "sessionId": s.session_id,
                    "cwd": str(s.workspace_root),
                    "model": s.model,
                    "created_at": s.created_at,
                    "turns": len(s.history),
                }
                for s in self.sessions.values()
            ]
        return {"sessions": sess_list}

    def _handle_tools_list(self, params: dict[str, Any]) -> dict[str, Any]:
        from .gateway import ToolGateway
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

    def run_stdio(self) -> None:
        """Run stdio JSON-RPC loop reading lines or Content-Length framing."""
        # Ensure logs strictly go to stderr so stdout is purely JSON-RPC
        logging.basicConfig(stream=sys.stderr, level=logging.INFO)

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

                resp = self.handle_request(req)
                resp_json = json.dumps(resp, ensure_ascii=False)
                sys.stdout.write(resp_json + "\n")
                sys.stdout.flush()
            except (KeyboardInterrupt, BrokenPipeError):
                break
            except Exception as exc:
                err_resp = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {exc}"},
                }
                sys.stdout.write(json.dumps(err_resp) + "\n")
                sys.stdout.flush()
