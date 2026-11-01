"""HTTP Control API Server & SSE Event Stream (PRD Section 15.1 & 15.2).

Implements all 16 endpoints specified in PRD Section 15.1 plus `GET /` for the
Local Web Control Surface dashboard using Python's built-in `http.server.ThreadingHTTPServer`
so it runs out-of-the-box with zero external package installation required.
"""

from __future__ import annotations

import json
import re
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .dashboard import DASHBOARD_HTML
from .db import ConcurrencyFenceError
from .models import TaskSubmitRequest
from .orchestrator import HarnessOrchestrator


class HarnessAPIApp:
    """Pure routing logic for all `/v1/*` endpoints so both HTTP server and tests can call it directly."""

    def __init__(self, orchestrator: HarnessOrchestrator, auth_token: str | None = None):
        self.orch = orchestrator
        self.auth_token = auth_token

    def handle_request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any] | str, str]:
        """Returns (status_code, payload_or_html, content_type)."""
        parsed = urlparse(path)
        route = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        body = body or {}
        headers = headers or {}

        try:
            # Check authentication if auth_token is configured
            if self.auth_token is not None:
                token = ""
                for k, v in headers.items():
                    if k.lower() == "x-worker-token":
                        token = v
                        break
                    if k.lower() == "authorization" and v.startswith("Bearer "):
                        token = v[7:].strip()
                        break
                if token != self.auth_token:
                    is_worker_or_mutating = (
                        route in ("/v1/workers/register", "/v1/workers/heartbeat", "/v1/tasks/claim")
                        or route.startswith("/v1/leases")
                        or method in ("POST", "PUT", "DELETE")
                    )
                    if is_worker_or_mutating:
                        return (HTTPStatus.UNAUTHORIZED, {"error": "Unauthorized worker token"}, "application/json")

            # Dashboard root
            if method == "GET" and route == "/":
                return (HTTPStatus.OK, DASHBOARD_HTML, "text/html; charset=utf-8")

            # 1. POST /v1/tasks
            if method == "POST" and route == "/v1/tasks":
                idem = headers.get("Idempotency-Key") or headers.get("idempotency-key") or body.get("idempotency_key")
                req = TaskSubmitRequest(
                    goal=str(body.get("goal", "")),
                    project_id=str(body.get("project_id", "default")),
                    priority=int(body.get("priority", 50)),
                    deadline_at=body.get("deadline_at"),
                    deliverable=str(body.get("deliverable", "proof_bundle")),
                    constraints=body.get("constraints") or {},
                    budget=body.get("budget") or {},
                    execution_mode=str(body.get("execution_mode", "auto")),
                    approval_policy=str(body.get("approval_policy", "default")),
                    idempotency_key=idem,
                    workspace_root=body.get("workspace_root"),
                    steps=body.get("steps"),
                    skill_id=body.get("skill_id"),
                )
                auto_exec = bool(body.get("auto_execute", True))
                task = self.orch.submit_and_run(req, auto_execute=auto_exec)
                return (HTTPStatus.CREATED, task, "application/json")

            # Claim task: POST /v1/tasks/claim
            if method == "POST" and route == "/v1/tasks/claim":
                worker_id = str(body.get("worker_id", "worker-default"))
                ttl_seconds = int(body.get("ttl_seconds", 300))
                claimed = self.orch.ledger.claim_next_task(worker_id=worker_id, ttl_seconds=ttl_seconds)
                if claimed:
                    return (HTTPStatus.OK, claimed, "application/json")
                return (HTTPStatus.OK, {"task": None}, "application/json")

            # GET /v1/tasks
            if method == "GET" and route == "/v1/tasks":
                status_filter = query.get("status", [None])[0]
                tasks = self.orch.ledger.list_tasks(status=status_filter)
                return (HTTPStatus.OK, {"tasks": tasks}, "application/json")

            # Task-scoped routes: /v1/tasks/{task_id}...
            m_task_action = re.fullmatch(r"/v1/tasks/([^/:]+):(pause|resume|fork|cancel|invoke-skill)", route)
            if method == "POST" and m_task_action:
                task_id, action = m_task_action.group(1), m_task_action.group(2)
                if action == "pause":
                    return (HTTPStatus.OK, self.orch.pause_task(task_id), "application/json")
                if action == "resume":
                    return (HTTPStatus.OK, self.orch.resume_task(task_id), "application/json")
                if action == "cancel":
                    return (HTTPStatus.OK, self.orch.cancel_task(task_id), "application/json")
                if action == "fork":
                    branches = body.get("branches") or []
                    return (HTTPStatus.OK, self.orch.fork_speculative_branches(task_id, branches), "application/json")
                if action == "invoke-skill":
                    skill_id = str(body.get("skill_id", ""))
                    skill = self.orch.scanner.verify_skill_runtime_integrity(skill_id)
                    req = TaskSubmitRequest(
                        goal=str(body.get("goal") or f"Invoke skill {skill['name']}"),
                        project_id=str(body.get("project_id", "default")),
                        skill_id=skill_id,
                        parent_task_id=task_id,
                        steps=body.get("steps"),
                        workspace_root=body.get("workspace_root"),
                    )
                    created = self.orch.submit_and_run(req, auto_execute=bool(body.get("auto_execute", True)))
                    return (HTTPStatus.CREATED, created, "application/json")

            m_task_sub = re.fullmatch(r"/v1/tasks/([^/:]+)(?:/(events|proof|artifacts))?", route)
            if method == "GET" and m_task_sub:
                task_id, sub = m_task_sub.group(1), m_task_sub.group(2)
                task = self.orch.ledger.get_task(task_id)
                if not task:
                    return (HTTPStatus.NOT_FOUND, {"error": f"Task not found: {task_id}"}, "application/json")
                if sub is None:
                    return (HTTPStatus.OK, task, "application/json")
                if sub == "events":
                    events = self.orch.ledger.list_events(task_id)
                    calls = self.orch.ledger.list_tool_calls(task_id)
                    return (
                        HTTPStatus.OK,
                        {"task_id": task_id, "events": events, "tool_calls": calls},
                        "application/json",
                    )
                if sub == "proof":
                    proof = self.orch.ledger.get_proof_bundle(task_id)
                    return (HTTPStatus.OK, {"task_id": task_id, "proof": proof}, "application/json")
                if sub == "artifacts":
                    arts = self.orch.ledger.list_artifacts(task_id)
                    return (HTTPStatus.OK, {"task_id": task_id, "artifacts": arts}, "application/json")

            # Approvals: GET /v1/approvals & POST /v1/approvals/{approval_id}:decide
            if method == "GET" and route == "/v1/approvals":
                return (HTTPStatus.OK, {"approvals": self.orch.ledger.list_approvals()}, "application/json")

            m_apr = re.fullmatch(r"/v1/approvals/([^/:]+):decide", route)
            if method == "POST" and m_apr:
                apr_id = m_apr.group(1)
                decision = str(body.get("decision", "APPROVED"))
                comment = str(body.get("comment", ""))
                decided = self.orch.ledger.decide_approval(apr_id, decision, comment=comment)
                if decided["decision"] == "APPROVED" and body.get("auto_resume", True):
                    self.orch.resume_task(decided["task_id"])
                return (HTTPStatus.OK, decided, "application/json")

            # Workers: GET /v1/workers, POST /v1/workers/register, POST /v1/workers/heartbeat
            if method == "GET" and route == "/v1/workers":
                return (HTTPStatus.OK, {"workers": self.orch.ledger.list_workers()}, "application/json")

            if method == "POST" and route == "/v1/workers/register":
                worker_id = str(body.get("worker_id") or f"worker-{uuid.uuid4().hex[:8]}")
                machine_class = str(body.get("machine_class", "m4_air"))
                cow_backend = str(body.get("cow_backend", "apfs_clone"))
                capabilities = body.get("capabilities") or {}
                local_llm_models = body.get("local_llm_models") or []
                trust_level = str(body.get("trust_level", "trusted_local"))
                res = self.orch.ledger.register_worker(
                    worker_id=worker_id,
                    machine_class=machine_class,
                    cow_backend=cow_backend,
                    capabilities=capabilities,
                    local_llm_models=local_llm_models,
                    trust_level=trust_level,
                )
                return (HTTPStatus.OK, res, "application/json")

            if method == "POST" and route == "/v1/workers/heartbeat":
                worker_id = str(body.get("worker_id", ""))
                if not worker_id:
                    return (HTTPStatus.BAD_REQUEST, {"error": "Missing worker_id"}, "application/json")
                health = str(body.get("health", "HEALTHY"))
                res = self.orch.ledger.heartbeat_worker(worker_id=worker_id, health=health)
                return (HTTPStatus.OK, res, "application/json")

            # Leases: POST /v1/leases/{lease_id}:renew & POST /v1/leases/{lease_id}:release
            m_lease_renew = re.fullmatch(r"/v1/leases/([^/:]+):renew", route)
            if method == "POST" and m_lease_renew:
                lease_id = m_lease_renew.group(1)
                fencing_token = int(body.get("fencing_token", 0))
                ttl_seconds = int(body.get("ttl_seconds", 300))
                res = self.orch.ledger.renew_lease(
                    lease_id=lease_id,
                    fencing_token=fencing_token,
                    ttl_seconds=ttl_seconds,
                )
                return (HTTPStatus.OK, res, "application/json")

            m_lease_release = re.fullmatch(r"/v1/leases/([^/:]+):release", route)
            if method == "POST" and m_lease_release:
                lease_id = m_lease_release.group(1)
                fencing_token = int(body["fencing_token"]) if "fencing_token" in body and body["fencing_token"] is not None else None
                res = self.orch.ledger.release_lease(
                    lease_id=lease_id,
                    fencing_token=fencing_token,
                )
                return (HTTPStatus.OK, res, "application/json")

            # Skills & SkillSpector: GET /v1/skills, POST /v1/skills:import, GET /v1/skills/{skill_id}/scans|revisions
            if method == "GET" and route == "/v1/skills":
                with self.orch.ledger._lock:
                    rows = self.orch.ledger.conn.execute(
                        "SELECT * FROM skills ORDER BY created_at DESC"
                    ).fetchall()
                    skills = []
                    for r in rows:
                        d = self.orch.ledger._row_to_dict(r)
                        if d and d.get("latest_scan_id"):
                            sc_row = self.orch.ledger.conn.execute(
                                "SELECT * FROM skill_scans WHERE scan_id = ?", (d["latest_scan_id"],)
                            ).fetchone()
                            d["scan"] = self.orch.ledger._row_to_dict(sc_row)
                        if d:
                            skills.append(d)
                return (HTTPStatus.OK, {"skills": skills}, "application/json")

            if method == "POST" and route == "/v1/skills:import":
                disk_path = body.get("disk_path")
                if not disk_path:
                    return (HTTPStatus.BAD_REQUEST, {"error": "Missing disk_path"}, "application/json")
                res = self.orch.scanner.scan_and_register_skill(Path(disk_path))
                return (HTTPStatus.OK, res, "application/json")

            m_skill_sub = re.fullmatch(r"/v1/skills/([^/:]+)/(scans|revisions)", route)
            if method == "GET" and m_skill_sub:
                skill_id, sub = m_skill_sub.group(1), m_skill_sub.group(2)
                with self.orch.ledger._lock:
                    if sub == "scans":
                        rows = self.orch.ledger.conn.execute(
                            "SELECT * FROM skill_scans WHERE skill_id = ? ORDER BY scanned_at DESC",
                            (skill_id,),
                        ).fetchall()
                        return (
                            HTTPStatus.OK,
                            {"skill_id": skill_id, "scans": [self.orch.ledger._row_to_dict(r) for r in rows]},
                            "application/json",
                        )
                    rows = self.orch.ledger.conn.execute(
                        "SELECT * FROM skills WHERE skill_id = ? ORDER BY created_at DESC",
                        (skill_id,),
                    ).fetchall()
                    return (
                        HTTPStatus.OK,
                        {"skill_id": skill_id, "revisions": [self.orch.ledger._row_to_dict(r) for r in rows]},
                        "application/json",
                    )

            return (HTTPStatus.NOT_FOUND, {"error": f"Unknown endpoint: {method} {route}"}, "application/json")

        except ConcurrencyFenceError as exc:
            return (HTTPStatus.CONFLICT, {"error": "ConcurrencyFenceError", "detail": str(exc)}, "application/json")
        except KeyError as exc:
            return (HTTPStatus.NOT_FOUND, {"error": str(exc)}, "application/json")
        except PermissionError as exc:
            return (HTTPStatus.FORBIDDEN, {"error": str(exc)}, "application/json")
        except Exception as exc:
            return (HTTPStatus.BAD_REQUEST, {"error": f"{type(exc).__name__}: {exc}"}, "application/json")


def run_http_server(
    orchestrator: HarnessOrchestrator,
    host: str = "127.0.0.1",
    port: int = 8420,
    auth_token: str | None = None,
) -> None:
    app = HarnessAPIApp(orchestrator, auth_token=auth_token)

    class Handler(BaseHTTPRequestHandler):
        def _dispatch(self, method: str) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length).decode("utf-8") if length > 0 else ""
            body: dict[str, Any] = {}
            if raw_body.strip():
                try:
                    body = json.loads(raw_body)
                except json.JSONDecodeError:
                    body = {}
            hdrs = {k: v for k, v in self.headers.items()}

            # Optional SSE mode on GET /v1/tasks/{task_id}/events?stream=true
            if method == "GET" and "stream=true" in self.path:
                status, payload, _ = app.handle_request(method, self.path, body, hdrs)
                self.send_response(status)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                data_str = json.dumps(payload, default=str)
                self.wfile.write(f"event: task_events\ndata: {data_str}\n\n".encode("utf-8"))
                return

            status, payload, content_type = app.handle_request(method, self.path, body, hdrs)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.end_headers()
            if isinstance(payload, str):
                self.wfile.write(payload.encode("utf-8"))
            else:
                self.wfile.write(json.dumps(payload, indent=2, default=str).encode("utf-8"))

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"⚡ Autonomous AI Harness Control Surface listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
