"""Tool Gateway with Policy Engine, Secret Redaction, 0ms Call-Level Journaling, and Code Mode.

Implements PRD Sections 6.2 (FR-015), 6.6 (FR-040..FR-044), and 12.1–12.2:
- Default-deny policy enforcement across risk classes R0–R4.
- Automatic secret redaction before journaling or cloud model context.
- Deterministic 0ms Call-Level Memoization: replays previously sealed calls on step retry or
  multi-call Code Mode crash recovery without duplicate external side effects.
- Active Graphify Blast-Radius Hook on R2 file modifications.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .db import LedgerStore, canonical_json, sha256_hex
from .graphify import ActiveGraphifyAdapter
from .models import PolicyDecisionType, RiskClass


SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(sk-[A-Za-z0-9]{16,})"),
    re.compile(r"(ghp_[A-Za-z0-9]{20,})"),
    re.compile(r"(AIza[0-9A-Za-z\-_]{25,})"),
    re.compile(r"((?:api_key|secret|token|password)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{8,}['\"]?)", re.I),
]


def redact_secrets(value: Any) -> Any:
    if isinstance(value, str):
        out = value
        for pat in SECRET_PATTERNS:
            out = pat.sub("[REDACTED_SECRET]", out)
        return out
    if isinstance(value, dict):
        return {
            k: ("[REDACTED_SECRET]" if any(s in k.lower() for s in ("secret", "password", "api_key", "token")) else redact_secrets(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact_secrets(v) for v in value]
    return value


@dataclass
class ToolSpec:
    tool_id: str
    description: str
    risk_class: RiskClass
    handler: Callable[[Path, dict[str, Any]], dict[str, Any]]
    result_size_cap: int = 64_000


class PolicyBlockedError(PermissionError):
    """Raised when a tool call is denied by the R0–R4 policy engine."""


class ApprovalRequiredInterrupt(RuntimeError):
    """Raised when an R3 tool requires explicit owner approval before execution."""

    def __init__(self, approval_record: dict[str, Any]):
        super().__init__(f"Approval required: {approval_record['approval_id']}")
        self.approval_record = approval_record


class ToolGateway:
    """Policy-enforcing, call-journaling gateway for direct tools and Code Mode scripts."""

    def __init__(
        self,
        ledger: LedgerStore,
        graphify: ActiveGraphifyAdapter | None = None,
    ):
        self.ledger = ledger
        self.graphify = graphify
        self.tools: dict[str, ToolSpec] = {}
        self._register_builtin_tools()

    def register_tool(self, spec: ToolSpec) -> None:
        self.tools[spec.tool_id] = spec

    def _register_builtin_tools(self) -> None:
        self.register_tool(
            ToolSpec(
                tool_id="read_file",
                description="Read a file inside the task workspace (R0/R1 Fast-Path eligible).",
                risk_class=RiskClass.R0,
                handler=self._tool_read_file,
            )
        )
        self.register_tool(
            ToolSpec(
                tool_id="list_files",
                description="List files in the repository workspace (R0/R1 Fast-Path eligible).",
                risk_class=RiskClass.R0,
                handler=self._tool_list_files,
            )
        )
        self.register_tool(
            ToolSpec(
                tool_id="query_graphify",
                description="Query the repository AST/call graph for symbol definitions and callers (R1).",
                risk_class=RiskClass.R1,
                handler=self._tool_query_graphify,
            )
        )
        self.register_tool(
            ToolSpec(
                tool_id="write_file",
                description="Modify or create a file inside the isolated CoW workspace (R2).",
                risk_class=RiskClass.R2,
                handler=self._tool_write_file,
            )
        )
        self.register_tool(
            ToolSpec(
                tool_id="run_tests",
                description="Run bounded deterministic pytest/unittest suite inside the CoW workspace (R2).",
                risk_class=RiskClass.R2,
                handler=self._tool_run_tests,
            )
        )
        self.register_tool(
            ToolSpec(
                tool_id="git_push_remote",
                description="Push verified commits or publish external deliverables (R3 approval-gated).",
                risk_class=RiskClass.R3,
                handler=self._tool_git_push_remote,
            )
        )
        self.register_tool(
            ToolSpec(
                tool_id="modify_system_policy",
                description="Modify system security policy or disable SkillSpector (R4 always denied).",
                risk_class=RiskClass.R4,
                handler=self._tool_modify_policy,
            )
        )

    # ------------------------------------------------------------------
    # Policy Evaluation & Call-Level Memoized Execution (FR-015, FR-040)
    # ------------------------------------------------------------------

    def evaluate_policy(
        self,
        task: dict[str, Any],
        spec: ToolSpec,
        args: dict[str, Any],
    ) -> tuple[PolicyDecisionType, str]:
        constraints = task.get("constraints") or {}
        allowed_tools = constraints.get("allowed_tools")
        if allowed_tools is not None and spec.tool_id not in allowed_tools:
            return (PolicyDecisionType.DENY, f"Tool '{spec.tool_id}' is not in task allowed_tools")

        if spec.risk_class == RiskClass.R4:
            return (PolicyDecisionType.DENY, "R4 actions (policy/secret mutation) are unconditionally denied to workers")

        if task.get("execution_mode") == "fast_path" and spec.risk_class not in (RiskClass.R0, RiskClass.R1):
            return (
                PolicyDecisionType.DENY,
                f"Fast-Path tasks only permit R0/R1 tools; '{spec.tool_id}' is {spec.risk_class.value}",
            )

        if spec.risk_class == RiskClass.R3:
            # Check if an approved approval record already exists for this exact action
            action_hash = sha256_hex(f"{task['task_id']}|{spec.tool_id}|{canonical_json(redact_secrets(args))}")
            approvals = self.ledger.list_approvals(task_id=task["task_id"], decision="APPROVED")
            if any(a["action_hash"] == action_hash for a in approvals):
                return (PolicyDecisionType.ALLOW, "R3 action explicitly pre-approved by owner")
            return (PolicyDecisionType.REQUIRE_APPROVAL, f"R3 tool '{spec.tool_id}' requires human approval")

        return (PolicyDecisionType.ALLOW, f"Permitted under risk class {spec.risk_class.value}")

    def invoke(
        self,
        *,
        task_id: str,
        attempt_id: str,
        step_id: str,
        sequence_in_step: int,
        tool_id: str,
        args: dict[str, Any],
        workspace_dir: Path | str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Executes a tool through the gateway or replays a sealed call in 0 ms."""
        if tool_id not in self.tools:
            raise PolicyBlockedError(f"Unknown or unregistered tool: {tool_id}")

        spec = self.tools[tool_id]
        task = self.ledger.get_task(task_id)
        if not task:
            raise KeyError(f"Task not found: {task_id}")

        args_hash = sha256_hex(canonical_json(args))
        idem_key = idempotency_key or f"idem:{task_id}:{step_id}:{sequence_in_step}:{tool_id}:{args_hash[:16]}"
        redacted_args = redact_secrets(args)

        # 1. Policy check always runs (even on replay, per Stage 3 exit gate)
        decision, reason = self.evaluate_policy(task, spec, args)
        if decision == PolicyDecisionType.DENY:
            call_id = self.ledger.begin_tool_call(
                task_id=task_id,
                attempt_id=attempt_id,
                step_id=step_id,
                sequence_in_step=sequence_in_step,
                tool_id=tool_id,
                risk_class=spec.risk_class.value,
                canonical_args_hash=args_hash,
                arguments_redacted=redacted_args,
                policy_decision=f"DENY:{reason}",
                idempotency_key=idem_key,
            )
            self.ledger.seal_tool_call(call_id, {"error": reason}, status="DENIED")
            raise PolicyBlockedError(reason)

        if decision == PolicyDecisionType.REQUIRE_APPROVAL:
            approval = self.ledger.create_approval(
                task_id=task_id,
                attempt_id=attempt_id,
                step_id=step_id,
                action_type=tool_id,
                action_params=redacted_args,
                blast_radius_summary=f"R3 external/privileged tool '{tool_id}' requested with args {canonical_json(redacted_args)}",
            )
            raise ApprovalRequiredInterrupt(approval)

        # 2. Check Gateway Call-Level Journal for a previously SEALED call (0ms deterministic replay)
        t0 = time.perf_counter()
        prior = self.ledger.find_sealed_tool_call(
            task_id=task_id,
            step_id=step_id,
            tool_id=tool_id,
            canonical_args_hash=args_hash,
            idempotency_key=idem_key,
        )
        if prior is not None and prior.get("result") is not None:
            replay_call_id = self.ledger.begin_tool_call(
                task_id=task_id,
                attempt_id=attempt_id,
                step_id=step_id,
                sequence_in_step=sequence_in_step,
                tool_id=tool_id,
                risk_class=spec.risk_class.value,
                canonical_args_hash=args_hash,
                arguments_redacted=redacted_args,
                policy_decision=f"ALLOW_REPLAY:{reason}",
                idempotency_key=f"{idem_key}:replay:{attempt_id}",
                replayed_from_call_id=prior["call_id"],
            )
            replayed_result = dict(prior["result"])
            replayed_result["_replayed"] = True
            replayed_result["_replayed_from_call_id"] = prior["call_id"]
            replayed_result["_replay_latency_ms"] = round((time.perf_counter() - t0) * 1000, 3)
            self.ledger.seal_tool_call(replay_call_id, replayed_result, status="SEALED")
            return replayed_result

        # 3. Record PENDING journal entry before executing external/workspace side effect
        call_id = self.ledger.begin_tool_call(
            task_id=task_id,
            attempt_id=attempt_id,
            step_id=step_id,
            sequence_in_step=sequence_in_step,
            tool_id=tool_id,
            risk_class=spec.risk_class.value,
            canonical_args_hash=args_hash,
            arguments_redacted=redacted_args,
            policy_decision=f"ALLOW:{reason}",
            idempotency_key=idem_key,
        )

        ws_path = Path(workspace_dir).resolve()
        before_sources: dict[str, str] = {}
        if spec.tool_id == "write_file" and "path" in args:
            rel_p = str(args["path"]).lstrip("./")
            target_file = (ws_path / rel_p).resolve()
            if target_file.is_file():
                before_sources[rel_p] = target_file.read_text(encoding="utf-8", errors="replace")

        try:
            raw_result = spec.handler(ws_path, args)
        except Exception as exc:
            err_payload = {"ok": False, "error": redact_secrets(str(exc))}
            self.ledger.seal_tool_call(call_id, err_payload, status="FAILED")
            raise

        sanitized_result = redact_secrets(raw_result)
        sanitized_result["_replayed"] = False
        sanitized_result["_call_id"] = call_id

        # 4. Active Graphify Blast-Radius Hook on R2 file modification (FR-036a)
        if spec.tool_id == "write_file" and self.graphify is not None and "path" in args:
            rel_p = str(args["path"]).lstrip("./")
            if rel_p.endswith(".py"):
                br = self.graphify.compute_blast_radius(ws_path, [rel_p], before_sources=before_sources)
                sanitized_result["ast_blast_radius"] = br.to_dict()
                if br.impacted_tests or br.hop1_dependents:
                    self.ledger.append_blast_radius_checks(
                        task_id, br.impacted_tests, br.warning_message
                    )

        self.ledger.seal_tool_call(call_id, sanitized_result, status="SEALED")
        return sanitized_result

    def execute_code_mode_composition(
        self,
        *,
        task_id: str,
        attempt_id: str,
        step_id: str,
        workspace_dir: Path | str,
        operations: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """FR-042..FR-044: Executes a multi-call Code Mode composition through the memoizing gateway.

        If operation #k crashes, re-running this composition replays operations #1..#(k-1)
        from the Tool Gateway journal in 0 ms without duplicate side effects.
        """
        results: list[dict[str, Any]] = []
        for idx, op in enumerate(operations, start=1):
            res = self.invoke(
                task_id=task_id,
                attempt_id=attempt_id,
                step_id=step_id,
                sequence_in_step=idx,
                tool_id=op["tool_id"],
                args=op.get("args", {}),
                workspace_dir=workspace_dir,
                idempotency_key=op.get("idempotency_key"),
            )
            results.append(res)
            if op.get("simulate_crash_after"):
                raise RuntimeError(f"Simulated mid-script failure after call #{idx}")
        return results

    # ------------------------------------------------------------------
    # Built-in Sandboxed Tool Handlers
    # ------------------------------------------------------------------

    def _resolve_confined_path(self, workspace_dir: Path, rel_path: str) -> Path:
        norm = rel_path.strip().lstrip("/")
        if ".." in Path(norm).parts:
            raise PolicyBlockedError(f"Path escapes CoW workspace boundary: {rel_path}")
        full = (workspace_dir / norm).resolve()
        if not str(full).startswith(str(workspace_dir.resolve())):
            raise PolicyBlockedError(f"Symlink/path escape outside CoW workspace: {rel_path}")
        return full

    def _tool_read_file(self, workspace_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
        target = self._resolve_confined_path(workspace_dir, str(args["path"]))
        if not target.is_file():
            raise FileNotFoundError(f"File not found in workspace: {args['path']}")
        content = target.read_text(encoding="utf-8", errors="replace")[:32_000]
        return {"ok": True, "path": str(args["path"]), "content": content, "size": len(content)}

    def _tool_list_files(self, workspace_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
        files: list[str] = []
        for dirpath, dirnames, filenames in os.walk(workspace_dir):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d != "__pycache__")
            for f in sorted(filenames):
                if not f.startswith("."):
                    rel = str((Path(dirpath) / f).relative_to(workspace_dir)).replace("\\", "/")
                    files.append(rel)
        return {"ok": True, "files": files[:500], "count": len(files)}

    def _tool_query_graphify(self, workspace_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
        if self.graphify is None:
            self.graphify = ActiveGraphifyAdapter(workspace_dir)
        self.graphify.build_or_refresh(workspace_dir)
        symbol = str(args.get("symbol", ""))
        if symbol:
            return {"ok": True, "graph": self.graphify.query_symbol(symbol)}
        return {
            "ok": True,
            "indexed_files": len(self.graphify.symbols_by_file),
            "symbols": sorted(self.graphify.symbols_by_name.keys()),
        }

    def _tool_write_file(self, workspace_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
        target = self._resolve_confined_path(workspace_dir, str(args["path"]))
        target.parent.mkdir(parents=True, exist_ok=True)
        content = str(args.get("content", ""))
        target.write_text(content, encoding="utf-8")
        return {
            "ok": True,
            "path": str(args["path"]),
            "bytes_written": len(content.encode("utf-8")),
            "sha256": sha256_hex(content),
        }

    def _tool_run_tests(self, workspace_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
        target_tests = args.get("targets") or []
        if isinstance(target_tests, str):
            target_tests = [target_tests]
        cmd = [sys.executable, "-m", "unittest"]
        if target_tests:
            cmd.extend(target_tests)
        else:
            cmd.extend(["discover", "-s", ".", "-p", "test_*.py"])

        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(workspace_dir)}
        res = subprocess.run(
            cmd,
            cwd=str(workspace_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=int(args.get("timeout", 30)),
        )
        combined_out = (res.stdout + "\n" + res.stderr).strip()
        return {
            "ok": res.returncode == 0,
            "exit_code": res.returncode,
            "output": combined_out[:16_000],
            "output_sha256": sha256_hex(combined_out),
        }

    def _tool_git_push_remote(self, workspace_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "pushed_ref": args.get("branch", "main"),
            "remote": args.get("remote", "origin"),
        }

    def _tool_modify_policy(self, workspace_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
        raise PolicyBlockedError("R4 policy modification is prohibited")
