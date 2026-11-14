"""Tool Gateway with Policy Engine, Secret Redaction, 0ms Call-Level Journaling, and Code Mode.

Implements PRD Sections 6.2 (FR-015), 6.6 (FR-040..FR-044), and 12.1–12.2:
- Default-deny policy enforcement across risk classes R0–R4.
- Automatic secret redaction before journaling or cloud model context.
- Deterministic 0ms Call-Level Memoization: replays previously sealed calls on step retry or
  multi-call Code Mode crash recovery without duplicate external side effects.
- Active Graphify Blast-Radius Hook on R2 file modifications.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .webhooks import WebhookNotifier
    from .subagent import SubagentManager

logger = logging.getLogger(__name__)

from .db import LedgerStore, canonical_json, sha256_hex
from .graphify import (
    ActiveGraphifyAdapter,
    BlastRadiusReport,
    SUPPORTED_CODE_EXTENSIONS,
)
from .lsp.manager import LSPManager
from .mcp_client import MCPClientAdapter, MCPToolDefinition
from .models import PolicyDecisionType, RiskClass
from .process_registry import ProcessRegistry, get_process_registry
from .sandbox import ExecutionSandbox, SandboxResult, get_default_sandbox
from .skillspector import SkillSecurityScanner, compute_mcp_manifest_sha256


SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(sk-[A-Za-z0-9\-_]{14,})"),
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


class ActiveBlastRadiusInterceptor:
    """Active Graphify blast-radius interceptor for tool operations modifying polyglot code files."""

    def __init__(self, graphify: ActiveGraphifyAdapter | None = None) -> None:
        self.graphify = graphify

    def inspect_file_modification(
        self,
        workspace_dir: Path | str,
        rel_path: str,
        *,
        before_sources: dict[str, str] | None = None,
    ) -> BlastRadiusReport | None:
        """Inspects file modification and returns BlastRadiusReport if file has a supported code extension."""
        if self.graphify is None:
            return None
        norm_p = str(rel_path).replace("\\", "/").lstrip("./")
        ext = Path(norm_p).suffix.lower()
        if ext in SUPPORTED_CODE_EXTENSIONS:
            return self.graphify.compute_blast_radius(
                workspace_dir, [norm_p], before_sources=before_sources
            )
        return None

    def compute_blast_radius(
        self,
        workspace_dir: Path | str,
        modified_rel_paths: list[str],
        before_sources: dict[str, str] | None = None,
    ) -> BlastRadiusReport | None:
        """Direct blast-radius calculation for given modified paths."""
        if self.graphify is None:
            return None
        return self.graphify.compute_blast_radius(
            workspace_dir, modified_rel_paths, before_sources=before_sources
        )


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "id": "read_file",
        "name": "read_file",
        "description": "Read a file inside the task workspace (R0/R1 Fast-Path eligible).",
        "risk_class": "R0",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path to file"},
                "offset": {"type": "integer", "description": "Line offset to start reading from", "default": 0},
                "limit": {"type": "integer", "description": "Max lines to return", "default": 1000},
            },
            "required": ["path"],
        },
    },
    {
        "id": "list_files",
        "name": "list_files",
        "description": "List files in the repository workspace (R0/R1 Fast-Path eligible).",
        "risk_class": "R0",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Subdirectory to list (default: root)", "default": "."},
                "recursive": {"type": "boolean", "description": "Whether to list recursively", "default": False},
            },
        },
    },
    {
        "id": "search_files",
        "name": "search_files",
        "description": "Search workspace files using substring or regex matching (R0 Fast-Path eligible).",
        "risk_class": "R0",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Search query pattern"},
                "is_regex": {"type": "boolean", "description": "Whether pattern is regex", "default": False},
                "path": {"type": "string", "description": "Subtree to search in", "default": "."},
            },
            "required": ["pattern"],
        },
    },
    {
        "id": "todo",
        "name": "todo",
        "description": "Track and manage in-session execution tasks and checklist state (R0 Fast-Path eligible).",
        "risk_class": "R0",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "list", "complete", "clear"], "description": "Todo action"},
                "task": {"type": "string", "description": "Task description for add/complete"},
            },
            "required": ["action"],
        },
    },
    {
        "id": "clarify",
        "name": "clarify",
        "description": "Request structured clarification from the user with optional choices (R0 Fast-Path eligible).",
        "risk_class": "R0",
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "Question to prompt user"},
                "choices": {"type": "array", "items": {"type": "string"}, "description": "Optional multiple choice list"},
            },
            "required": ["question"],
        },
    },
    {
        "id": "session_search",
        "name": "session_search",
        "description": "Search past session execution ledgers, tool invocations, and resolutions (R0 Fast-Path eligible).",
        "risk_class": "R0",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query for past sessions"},
                "limit": {"type": "integer", "description": "Max sessions to inspect", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "id": "query_graphify",
        "name": "query_graphify",
        "description": "Query the repository AST/call graph for symbol definitions and callers (R1).",
        "risk_class": "R1",
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Symbol name to query"},
                "kind": {"type": "string", "description": "Symbol kind or relation", "default": "callers"},
            },
            "required": ["symbol"],
        },
    },
    {
        "id": "web_search",
        "name": "web_search",
        "description": "Search the live web for technical documentation, compiler error solutions, and API references (R1).",
        "risk_class": "R1",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query keywords"},
                "max_results": {"type": "integer", "description": "Max search hits", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "id": "web_extract",
        "name": "web_extract",
        "description": "Extract readable text and markdown from web pages and API endpoints (R1).",
        "risk_class": "R1",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Web URL to fetch and convert to markdown"},
            },
            "required": ["url"],
        },
    },
    {
        "id": "memory",
        "name": "memory",
        "description": "Read, write, and persist project facts, conventions, and architectural notes (R1).",
        "risk_class": "R1",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["read", "write", "list"], "description": "Memory operation"},
                "key": {"type": "string", "description": "Memory key identifier"},
                "value": {"type": "string", "description": "Value to persist"},
            },
            "required": ["action"],
        },
    },
    {
        "id": "lsp_diagnostics",
        "name": "lsp_diagnostics",
        "description": "Query compiler syntax errors, type mismatches, and linter warnings from active language servers (R1).",
        "risk_class": "R1",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Optional file path to filter diagnostics"},
            },
        },
    },
    {
        "id": "lsp_definition",
        "name": "lsp_definition",
        "description": "Find symbol definition URI, line, and column coordinates using language server (R1).",
        "risk_class": "R1",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "File path where symbol appears"},
                "line": {"type": "integer", "description": "0-indexed line number"},
                "character": {"type": "integer", "description": "0-indexed character offset"},
            },
            "required": ["file_path", "line", "character"],
        },
    },
    {
        "id": "lsp_references",
        "name": "lsp_references",
        "description": "Find all reference locations and call sites for a symbol across the workspace (R1).",
        "risk_class": "R1",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "File path where symbol appears"},
                "line": {"type": "integer", "description": "0-indexed line number"},
                "character": {"type": "integer", "description": "0-indexed character offset"},
            },
            "required": ["file_path", "line", "character"],
        },
    },
    {
        "id": "lsp_hover",
        "name": "lsp_hover",
        "description": "Retrieve type signatures and markdown docstrings for a symbol at coordinates (R1).",
        "risk_class": "R1",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "File path where symbol appears"},
                "line": {"type": "integer", "description": "0-indexed line number"},
                "character": {"type": "integer", "description": "0-indexed character offset"},
            },
            "required": ["file_path", "line", "character"],
        },
    },
    {
        "id": "write_file",
        "name": "write_file",
        "description": "Modify or create a file inside the isolated CoW workspace (R2).",
        "risk_class": "R2",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative file path to write"},
                "content": {"type": "string", "description": "Full file content string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "id": "patch",
        "name": "patch",
        "description": "Apply unified diff hunks or search/replace blocks to modify files in the isolated CoW workspace (R2).",
        "risk_class": "R2",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative file path to patch"},
                "diff": {"type": "string", "description": "Unified diff hunk or search/replace blocks"},
            },
            "required": ["path", "diff"],
        },
    },
    {
        "id": "run_tests",
        "name": "run_tests",
        "description": "Run bounded deterministic pytest/unittest suite inside the CoW workspace (R2).",
        "risk_class": "R2",
        "parameters": {
            "type": "object",
            "properties": {
                "test_file": {"type": "string", "description": "Optional specific test file path"},
                "extra_args": {"type": "array", "items": {"type": "string"}, "description": "Additional test flags"},
            },
        },
    },
    {
        "id": "terminal",
        "name": "terminal",
        "description": "Execute a terminal shell command inside the sandboxed CoW workspace (R2).",
        "risk_class": "R2",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Bash command string to execute"},
                "timeout": {"type": "number", "description": "Max execution time in seconds", "default": 30.0},
            },
            "required": ["command"],
        },
    },
    {
        "id": "process",
        "name": "process",
        "description": "Inspect or terminate active background processes running in the workspace (R2).",
        "risk_class": "R2",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "kill"], "description": "Process management action"},
                "pid": {"type": "integer", "description": "PID to kill if action is kill"},
            },
            "required": ["action"],
        },
    },
    {
        "id": "git_push_remote",
        "name": "git_push_remote",
        "description": "Push verified commits or publish external deliverables (R3 approval-gated).",
        "risk_class": "R3",
        "parameters": {
            "type": "object",
            "properties": {
                "branch": {"type": "string", "description": "Branch name to push", "default": "main"},
                "remote": {"type": "string", "description": "Git remote name", "default": "origin"},
            },
        },
    },
    {
        "id": "modify_system_policy",
        "name": "modify_system_policy",
        "description": "Modify system security policy or disable SkillSpector (R4 always denied).",
        "risk_class": "R4",
        "parameters": {
            "type": "object",
            "properties": {
                "policy_key": {"type": "string", "description": "Target policy key"},
            },
            "required": ["policy_key"],
        },
    },
    {
        "id": "delegate",
        "name": "delegate",
        "description": "Spawn a role-constrained child subagent inside an isolated CoW workspace (R2).",
        "risk_class": "R2",
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "Specific task or objective for the child subagent"},
                "role": {
                    "type": "string",
                    "enum": ["researcher", "coder", "tester", "reviewer", "planner", "general"],
                    "description": "Role specialization defining permitted tools",
                    "default": "general",
                },
                "context": {"type": "string", "description": "Optional context or instructions for child"},
                "wait": {"type": "boolean", "description": "Whether to wait synchronously for completion", "default": True},
                "timeout": {"type": "number", "description": "Max execution time in seconds", "default": 120.0},
            },
            "required": ["goal"],
        },
    },
    {
        "id": "subagent_status",
        "name": "subagent_status",
        "description": "Query the status and outputs of an in-flight or completed subagent (R0 Fast-Path eligible).",
        "risk_class": "R0",
        "parameters": {
            "type": "object",
            "properties": {
                "subagent_id": {"type": "string", "description": "ID of the subagent to query"},
            },
            "required": ["subagent_id"],
        },
    },
    {
        "id": "subagent_wait",
        "name": "subagent_wait",
        "description": "Block until a background subagent finishes execution (R1).",
        "risk_class": "R1",
        "parameters": {
            "type": "object",
            "properties": {
                "subagent_id": {"type": "string", "description": "ID of the subagent to wait for"},
                "timeout": {"type": "number", "description": "Max wait time in seconds", "default": 60.0},
            },
            "required": ["subagent_id"],
        },
    },
    {
        "id": "browser",
        "name": "browser",
        "description": "Interactive headless browser for navigating web pages, clicking elements, extracting accessibility trees, and taking screenshots.",
        "risk_class": "R2",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["navigate", "snapshot", "click", "type", "screenshot", "evaluate", "close"],
                    "description": "Browser action to perform",
                    "default": "snapshot",
                },
                "url": {"type": "string", "description": "URL to navigate to"},
                "selector": {"type": "string", "description": "Element ref (@e1, @e2) or CSS selector"},
                "text": {"type": "string", "description": "Text to type into input element"},
                "script": {"type": "string", "description": "Expression or regex to evaluate in DOM"},
                "output_path": {"type": "string", "description": "File path to save screenshot/snapshot"},
            },
            "required": ["action"],
        },
    },
    {
        "id": "image_inspect",
        "name": "image_inspect",
        "description": "Inspect local image files or remote URLs using multimodal vision analysis (R1).",
        "risk_class": "R1",
        "parameters": {
            "type": "object",
            "properties": {
                "image_path": {"type": "string", "description": "Local path or HTTP/HTTPS URL of image"},
                "prompt": {"type": "string", "description": "Specific query or instruction for visual analysis"},
                "detail": {
                    "type": "string",
                    "enum": ["auto", "low", "high"],
                    "description": "Visual inspection fidelity",
                    "default": "auto",
                },
            },
            "required": ["image_path", "prompt"],
        },
    },
    {
        "id": "start_process",
        "name": "start_process",
        "description": "Spawn a background command in an asynchronous pseudo-terminal (PTY) or pipe (R2).",
        "risk_class": "R2",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command string to spawn in background"},
                "cwd": {"type": "string", "description": "Working directory relative to workspace root", "default": ""},
                "pty": {"type": "boolean", "description": "Whether to request pseudo-terminal (PTY) allocation", "default": True},
            },
            "required": ["command"],
        },
    },
    {
        "id": "read_terminal",
        "name": "read_terminal",
        "description": "Read output slice from a running or completed background process buffer (R0).",
        "risk_class": "R0",
        "parameters": {
            "type": "object",
            "properties": {
                "process_id": {"type": "string", "description": "Process ID returned from start_process"},
                "offset": {"type": "integer", "description": "Character stream offset to read from", "default": 0},
                "limit": {"type": "integer", "description": "Max characters to read in this slice", "default": 2000},
            },
            "required": ["process_id"],
        },
    },
    {
        "id": "kill_process",
        "name": "kill_process",
        "description": "Terminate an active background process by process_id (R2).",
        "risk_class": "R2",
        "parameters": {
            "type": "object",
            "properties": {
                "process_id": {"type": "string", "description": "Process ID to terminate"},
                "force": {"type": "boolean", "description": "Send SIGKILL immediately instead of graceful SIGTERM", "default": False},
            },
            "required": ["process_id"],
        },
    },
    {
        "id": "list_processes",
        "name": "list_processes",
        "description": "List all background processes and their current status (R0).",
        "risk_class": "R0",
        "parameters": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["running", "completed", "failed", "killed", "all"],
                    "description": "Optional filter by process status",
                },
            },
        },
    },
]


class ToolGateway:
    """Policy-enforcing, call-journaling gateway for direct tools and Code Mode scripts."""

    def __init__(
        self,
        ledger: LedgerStore,
        graphify: ActiveGraphifyAdapter | None = None,
        sandbox: ExecutionSandbox | None = None,
        notifier: WebhookNotifier | None = None,
        lsp_manager: LSPManager | None = None,
        subagent_manager: SubagentManager | None = None,
        browser_manager: Any | None = None,
        vision_engine: Any | None = None,
        process_registry: ProcessRegistry | None = None,
    ):
        self.ledger = ledger
        self.graphify = graphify
        self.blast_interceptor = ActiveBlastRadiusInterceptor(graphify)
        self.sandbox = sandbox or get_default_sandbox()
        self.notifier = notifier
        self.lsp_manager = lsp_manager
        self.subagent_manager = subagent_manager
        self.browser_manager = browser_manager
        self.vision_engine = vision_engine
        self.process_registry = process_registry or get_process_registry()
        self.tools: dict[str, ToolSpec] = {}
        self.mcp_servers: dict[str, dict[str, Any]] = {}
        self._register_builtin_tools()

    @classmethod
    def list_tool_definitions(cls) -> list[dict[str, Any]]:
        """Return standardized schema list of all built-in tools."""
        return [dict(t) for t in TOOL_DEFINITIONS]

    def _get_lsp_manager(self, workspace_dir: Path) -> LSPManager:
        if self.lsp_manager is None:
            self.lsp_manager = LSPManager(workspace_root=workspace_dir)
        elif self.lsp_manager.workspace_root is None:
            self.lsp_manager.workspace_root = Path(workspace_dir).resolve()
        return self.lsp_manager

    def register_tool(self, spec: ToolSpec) -> None:
        self.tools[spec.tool_id] = spec

    def register_mcp_server(
        self,
        adapter: MCPClientAdapter,
        scanner: SkillSecurityScanner | None = None,
        enforce_scan: bool = True,
    ) -> list[str]:
        """Dynamically validates and registers tools exposed by an external MCP server adapter."""
        tools = adapter.discover_tools()
        manifest_data = adapter.get_manifest_data()

        if enforce_scan:
            sec_scanner = scanner or SkillSecurityScanner(self.ledger)
            scan_res = sec_scanner.scan_mcp_manifest(adapter.server_id, manifest_data, tools)
            if scan_res.get("gate_decision") != "ALLOW" or scan_res.get("status") == "quarantined":
                matched = scan_res.get("scan", {}).get("matched_rules", [])
                raise PolicyBlockedError(
                    f"MCP server '{adapter.server_id}' rejected by SkillSpector: {matched}"
                )
            manifest_sha256 = scan_res.get("manifest_sha256", "")
        else:
            manifest_sha256 = manifest_data.get("manifest_sha256") or compute_mcp_manifest_sha256(manifest_data)

        self.mcp_servers[adapter.server_id] = {
            "adapter": adapter,
            "manifest_sha256": manifest_sha256,
        }

        registered_ids: list[str] = []

        def _make_handler(client: MCPClientAdapter, target_tool: str):
            def handler(ws_path: Path, args: dict[str, Any]) -> dict[str, Any]:
                return client.call_tool(target_tool, args)
            return handler

        for tool in tools:
            namespaced_id = f"{adapter.server_id}__{tool.name}"
            risk_cls = tool.risk_class if isinstance(tool.risk_class, RiskClass) else RiskClass(str(tool.risk_class))

            spec = ToolSpec(
                tool_id=namespaced_id,
                description=tool.description,
                risk_class=risk_cls,
                handler=_make_handler(adapter, tool.name),
            )
            self.register_tool(spec)
            registered_ids.append(namespaced_id)

            if tool.name not in self.tools:
                alias_spec = ToolSpec(
                    tool_id=tool.name,
                    description=tool.description,
                    risk_class=risk_cls,
                    handler=_make_handler(adapter, tool.name),
                )
                self.register_tool(alias_spec)
                registered_ids.append(tool.name)

        return registered_ids

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
                tool_id="search_files",
                description="Search workspace files using substring or regex matching (R0 Fast-Path eligible).",
                risk_class=RiskClass.R0,
                handler=self._tool_search_files,
            )
        )
        self.register_tool(
            ToolSpec(
                tool_id="todo",
                description="Track and manage in-session execution tasks and checklist state (R0 Fast-Path eligible).",
                risk_class=RiskClass.R0,
                handler=self._tool_todo,
            )
        )
        self.register_tool(
            ToolSpec(
                tool_id="clarify",
                description="Request structured clarification from the user with optional choices (R0 Fast-Path eligible).",
                risk_class=RiskClass.R0,
                handler=self._tool_clarify,
            )
        )
        self.register_tool(
            ToolSpec(
                tool_id="session_search",
                description="Search past session execution ledgers, tool invocations, and resolutions (R0 Fast-Path eligible).",
                risk_class=RiskClass.R0,
                handler=self._tool_session_search,
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
                tool_id="web_search",
                description="Search the live web for technical documentation, compiler error solutions, and API references (R1).",
                risk_class=RiskClass.R1,
                handler=self._tool_web_search,
            )
        )
        self.register_tool(
            ToolSpec(
                tool_id="web_extract",
                description="Extract readable text and markdown from web pages and API endpoints (R1).",
                risk_class=RiskClass.R1,
                handler=self._tool_web_extract,
            )
        )
        self.register_tool(
            ToolSpec(
                tool_id="memory",
                description="Read, write, and persist project facts, conventions, and architectural notes (R1).",
                risk_class=RiskClass.R1,
                handler=self._tool_memory,
            )
        )
        self.register_tool(
            ToolSpec(
                tool_id="lsp_diagnostics",
                description="Query compiler syntax errors, type mismatches, and linter warnings from active language servers across workspace or specific file (R1).",
                risk_class=RiskClass.R1,
                handler=self._tool_lsp_diagnostics,
            )
        )
        self.register_tool(
            ToolSpec(
                tool_id="lsp_definition",
                description="Find symbol definition URI, line, and column coordinates using language server (R1).",
                risk_class=RiskClass.R1,
                handler=self._tool_lsp_definition,
            )
        )
        self.register_tool(
            ToolSpec(
                tool_id="lsp_references",
                description="Find all reference locations and call sites for a symbol across the workspace (R1).",
                risk_class=RiskClass.R1,
                handler=self._tool_lsp_references,
            )
        )
        self.register_tool(
            ToolSpec(
                tool_id="lsp_hover",
                description="Retrieve type signatures and markdown docstrings for a symbol at coordinates (R1).",
                risk_class=RiskClass.R1,
                handler=self._tool_lsp_hover,
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
                tool_id="patch",
                description="Apply unified diff hunks or search/replace blocks to modify files in the isolated CoW workspace (R2).",
                risk_class=RiskClass.R2,
                handler=self._tool_patch,
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
                tool_id="terminal",
                description="Execute a terminal shell command inside the sandboxed CoW workspace (R2).",
                risk_class=RiskClass.R2,
                handler=self._tool_terminal,
            )
        )
        self.register_tool(
            ToolSpec(
                tool_id="process",
                description="Inspect or terminate active background processes running in the workspace (R2).",
                risk_class=RiskClass.R2,
                handler=self._tool_process,
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
        self.register_tool(
            ToolSpec(
                tool_id="delegate",
                description="Spawn a role-constrained child subagent inside an isolated CoW workspace (R2).",
                risk_class=RiskClass.R2,
                handler=self._tool_delegate,
            )
        )
        self.register_tool(
            ToolSpec(
                tool_id="subagent_status",
                description="Query the status and outputs of an in-flight or completed subagent (R0 Fast-Path eligible).",
                risk_class=RiskClass.R0,
                handler=self._tool_subagent_status,
            )
        )
        self.register_tool(
            ToolSpec(
                tool_id="subagent_wait",
                description="Block until a background subagent finishes execution (R1).",
                risk_class=RiskClass.R1,
                handler=self._tool_subagent_wait,
            )
        )
        self.register_tool(
            ToolSpec(
                tool_id="browser",
                description="Interactive headless browser for navigation, DOM inspection, element interaction, and screenshots (R2).",
                risk_class=RiskClass.R2,
                handler=self._tool_browser,
            )
        )
        self.register_tool(
            ToolSpec(
                tool_id="image_inspect",
                description="Inspect local image files or remote URLs using multimodal vision analysis (R1).",
                risk_class=RiskClass.R1,
                handler=self._tool_image_inspect,
            )
        )
        self.register_tool(
            ToolSpec(
                tool_id="start_process",
                description="Spawn a background command in an asynchronous pseudo-terminal (PTY) or pipe (R2).",
                risk_class=RiskClass.R2,
                handler=self._tool_start_process,
            )
        )
        self.register_tool(
            ToolSpec(
                tool_id="read_terminal",
                description="Read output slice from a running or completed background process buffer (R0).",
                risk_class=RiskClass.R0,
                handler=self._tool_read_terminal,
            )
        )
        self.register_tool(
            ToolSpec(
                tool_id="kill_process",
                description="Terminate an active background process by process_id (R2).",
                risk_class=RiskClass.R2,
                handler=self._tool_kill_process,
            )
        )
        self.register_tool(
            ToolSpec(
                tool_id="list_processes",
                description="List all background processes and their current status (R0).",
                risk_class=RiskClass.R0,
                handler=self._tool_list_processes,
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
        fencing_token: int | None = None,
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
                fencing_token=fencing_token,
            )
            self.ledger.seal_tool_call(call_id, {"error": reason}, status="DENIED", fencing_token=fencing_token)
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
            approval["risk_class"] = spec.risk_class.value
            if self.notifier is not None:
                try:
                    self.notifier.send_approval_request(approval)
                except Exception as exc:
                    logger.warning("Failed to dispatch approval webhook: %s", exc)
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
                fencing_token=fencing_token,
            )
            replayed_result = dict(prior["result"])
            replayed_result["_replayed"] = True
            replayed_result["_replayed_from_call_id"] = prior["call_id"]
            replayed_result["_replay_latency_ms"] = round((time.perf_counter() - t0) * 1000, 3)
            self.ledger.seal_tool_call(replay_call_id, replayed_result, status="SEALED", fencing_token=fencing_token)
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
            fencing_token=fencing_token,
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
            self.ledger.seal_tool_call(call_id, err_payload, status="FAILED", fencing_token=fencing_token)
            raise

        duration_ms = round((time.perf_counter() - t0) * 1000, 3)
        sanitized_result = redact_secrets(raw_result)
        sanitized_result["_replayed"] = False
        sanitized_result["_call_id"] = call_id
        sanitized_result["_duration_ms"] = duration_ms
        sanitized_result["_result_sha256"] = sha256_hex(canonical_json(sanitized_result))

        # 4. Active Graphify Blast-Radius Hook on R2 file modification (FR-036a)
        if spec.tool_id == "write_file" and "path" in args:
            rel_p = str(args["path"]).lstrip("./")
            ext = Path(rel_p).suffix.lower()
            if ext in SUPPORTED_CODE_EXTENSIONS and (
                self.graphify is not None or getattr(self, "blast_interceptor", None) is not None
            ):
                interceptor = getattr(self, "blast_interceptor", None) or ActiveBlastRadiusInterceptor(self.graphify)
                if self.graphify is not None and interceptor.graphify is None:
                    interceptor.graphify = self.graphify
                br = interceptor.inspect_file_modification(ws_path, rel_p, before_sources=before_sources)
                if br is not None:
                    sanitized_result["ast_blast_radius"] = br.to_dict()
                    if br.impacted_tests or br.hop1_dependents:
                        self.ledger.append_blast_radius_checks(
                            task_id, br.impacted_tests, br.warning_message
                        )

        self.ledger.seal_tool_call(call_id, sanitized_result, status="SEALED", fencing_token=fencing_token)
        return sanitized_result

    def execute_code_mode_composition(
        self,
        *,
        task_id: str,
        attempt_id: str,
        step_id: str,
        workspace_dir: Path | str,
        operations: list[dict[str, Any]],
        fencing_token: int | None = None,
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
                fencing_token=fencing_token,
            )
            results.append(res)
            if op.get("simulate_crash_after"):
                raise RuntimeError(f"Simulated mid-script failure after call #{idx}")
        return results

    # ------------------------------------------------------------------
    # Built-in Sandboxed Tool Handlers
    # ------------------------------------------------------------------

    def _resolve_confined_path(self, workspace_dir: Path, rel_path: str) -> Path:
        norm = str(rel_path).strip().lstrip("/")
        ws_resolved = workspace_dir.resolve()
        if ".." in Path(norm).parts or ".." in norm:
            raise PolicyBlockedError(f"Path escapes CoW workspace boundary: {rel_path}")
        full = (workspace_dir / norm).resolve()
        try:
            full.relative_to(ws_resolved)
        except ValueError:
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

    def _tool_search_files(self, workspace_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or args.get("pattern", "")).strip()
        if not query:
            return {"ok": True, "matches": [], "total_matches": 0, "query": ""}

        rel_subpath = str(args.get("path", "")).strip()
        search_dir = self._resolve_confined_path(workspace_dir, rel_subpath) if rel_subpath else workspace_dir
        if not search_dir.exists():
            return {"ok": False, "error": f"Path not found: {rel_subpath}", "matches": []}

        file_pat = str(args.get("file_pattern") or args.get("glob", "*"))
        case_sensitive = bool(args.get("case_sensitive", False))
        max_matches = int(args.get("max_matches", 100))

        regex_flags = 0 if case_sensitive else re.IGNORECASE
        use_regex = True
        try:
            compiled_re = re.compile(query, regex_flags)
        except re.error:
            use_regex = False
            query_cmp = query if case_sensitive else query.lower()

        matches: list[dict[str, Any]] = []
        ignored_dirs = {".git", ".venv", ".pytest_cache", ".hex", "__pycache__", "node_modules", ".mypy_cache"}

        for dirpath, dirnames, filenames in os.walk(search_dir):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in ignored_dirs)
            for fname in sorted(filenames):
                if fname.startswith("."):
                    continue
                if file_pat != "*" and not fnmatch.fnmatch(fname, file_pat):
                    continue

                fpath = Path(dirpath) / fname
                try:
                    if fpath.stat().st_size > 1_000_000:
                        continue
                    with open(fpath, "rb") as bf:
                        sample = bf.read(1024)
                        if b"\x00" in sample:
                            continue

                    rel_path = str(fpath.relative_to(workspace_dir)).replace("\\", "/")
                    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                        for l_idx, line in enumerate(f, 1):
                            matched = False
                            if use_regex:
                                if compiled_re.search(line):
                                    matched = True
                            else:
                                if (line if case_sensitive else line.lower()).find(query_cmp) != -1:
                                    matched = True
                            if matched:
                                matches.append({
                                    "path": rel_path,
                                    "line": l_idx,
                                    "text": line.rstrip("\r\n")[:300],
                                })
                                if len(matches) >= max_matches:
                                    break
                except (OSError, UnicodeDecodeError):
                    continue
            if len(matches) >= max_matches:
                break

        return {
            "ok": True,
            "matches": matches,
            "total_matches": len(matches),
            "query": query,
        }

    def _tool_todo(self, workspace_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
        action = str(args.get("action", "read")).lower()
        todo_file = workspace_dir / ".hex" / "todo.json"

        todos: list[dict[str, Any]] = []
        if todo_file.is_file():
            try:
                loaded = json.loads(todo_file.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    todos = loaded
            except Exception:
                todos = []

        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        if action in ("read", "list"):
            pass
        elif action == "add":
            new_item = args.get("item") or args.get("task")
            items = args.get("items")
            to_add: list[Any] = []
            if items and isinstance(items, list):
                to_add.extend(items)
            elif new_item:
                to_add.append(new_item)

            for item in to_add:
                if isinstance(item, dict):
                    todos.append({
                        "id": item.get("id", len(todos) + 1),
                        "task": str(item.get("task") or item.get("text", "")),
                        "status": str(item.get("status", "pending")),
                        "updated_at": now,
                    })
                else:
                    todos.append({
                        "id": len(todos) + 1,
                        "task": str(item),
                        "status": "pending",
                        "updated_at": now,
                    })
        elif action == "update":
            idx = args.get("index")
            item_id = args.get("id")
            new_status = args.get("status")
            new_task = args.get("task") or args.get("item")

            updated = False
            for i, td in enumerate(todos):
                if (idx is not None and i == int(idx)) or (item_id is not None and str(td.get("id")) == str(item_id)):
                    if new_status:
                        td["status"] = str(new_status)
                    if new_task:
                        td["task"] = str(new_task)
                    td["updated_at"] = now
                    updated = True
                    break
            if not updated and idx is not None and 0 <= int(idx) < len(todos):
                if new_status:
                    todos[int(idx)]["status"] = str(new_status)
                if new_task:
                    todos[int(idx)]["task"] = str(new_task)
                todos[int(idx)]["updated_at"] = now
        elif action in ("clear", "reset"):
            todos = []
        elif action in ("remove", "delete"):
            idx = args.get("index")
            item_id = args.get("id")
            if idx is not None and 0 <= int(idx) < len(todos):
                todos.pop(int(idx))
            elif item_id is not None:
                todos = [td for td in todos if str(td.get("id")) != str(item_id)]

        try:
            todo_file.parent.mkdir(parents=True, exist_ok=True)
            todo_file.write_text(json.dumps(todos, indent=2), encoding="utf-8")
        except OSError:
            pass

        return {
            "ok": True,
            "action": action,
            "todos": todos,
            "count": len(todos),
        }

    def _tool_clarify(self, workspace_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
        question = str(args.get("question", "")).strip()
        options = [str(opt) for opt in args.get("options", [])] if args.get("options") else []
        context = str(args.get("context", "")).strip()

        return {
            "ok": True,
            "question": question,
            "options": options,
            "context": context,
            "clarification_needed": True,
        }

    def _tool_session_search(self, workspace_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query", "")).strip()
        limit = int(args.get("limit", 10))
        project_id = args.get("project_id")

        if not query:
            return {"ok": True, "query": "", "results": [], "count": 0}

        results: list[dict[str, Any]] = []
        if self.ledger is not None:
            try:
                search_res = self.ledger.search_tasks(query=query, project_id=project_id, limit=limit)
                if search_res:
                    return {"ok": True, "query": query, "results": search_res, "count": len(search_res)}
            except Exception:
                pass

            try:
                with self.ledger._lock:
                    if self.ledger.conn is not None:
                        like_pat = f"%{query}%"
                        rows = self.ledger.conn.execute(
                            """
                            SELECT task_id, project_id, status, goal, created_at
                            FROM tasks
                            WHERE goal LIKE ?
                            ORDER BY created_at DESC
                            LIMIT ?
                            """,
                            (like_pat, limit),
                        ).fetchall()
                        for r in rows:
                            results.append({
                                "task_id": r[0],
                                "project_id": r[1],
                                "status": r[2],
                                "snippet": (r[3] or "")[:300],
                                "created_at": r[4],
                            })
            except Exception:
                pass

        return {
            "ok": True,
            "query": query,
            "results": results,
            "count": len(results),
        }

    def _tool_query_graphify(self, workspace_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
        if self.graphify is None:
            self.graphify = ActiveGraphifyAdapter(workspace_dir)
            if hasattr(self, "blast_interceptor") and self.blast_interceptor is not None:
                self.blast_interceptor.graphify = self.graphify
        self.graphify.build_or_refresh(workspace_dir)
        symbol = str(args.get("symbol", ""))
        if symbol:
            return {"ok": True, "graph": self.graphify.query_symbol(symbol)}
        return {
            "ok": True,
            "indexed_files": len(self.graphify.symbols_by_file),
            "symbols": sorted(self.graphify.symbols_by_name.keys()),
        }

    def _tool_web_search(self, workspace_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
        from .web_search import execute_web_search

        query = str(args.get("query", "")).strip()
        max_results = int(args.get("max_results", 5))
        provider = str(args.get("provider", "auto"))
        return execute_web_search(query=query, max_results=max_results, provider=provider)

    def _tool_web_extract(self, workspace_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
        url = str(args.get("url", "")).strip()
        if not url:
            return {"ok": False, "error": "URL parameter is required"}
        if not (url.startswith("http://") or url.startswith("https://")):
            return {"ok": False, "error": "URL must start with http:// or https://"}

        max_chars = int(args.get("max_chars", 20_000))
        timeout = float(args.get("timeout", 10.0))

        class TextExtractParser(HTMLParser):
            def __init__(self) -> None:
                super().__init__()
                self.text_parts: list[str] = []
                self.skip_tags = {"script", "style", "head", "noscript", "svg"}
                self.tag_stack: list[str] = []

            def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
                self.tag_stack.append(tag.lower())

            def handle_endtag(self, tag: str) -> None:
                tag_lower = tag.lower()
                if tag_lower in self.tag_stack:
                    self.tag_stack.remove(tag_lower)
                if tag_lower in ("p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr"):
                    self.text_parts.append("\n")

            def handle_data(self, data: str) -> None:
                if any(t in self.skip_tags for t in self.tag_stack):
                    return
                cleaned = data.strip()
                if cleaned:
                    self.text_parts.append(cleaned + " ")

        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "HEX-Agent/1.0",
                    "Accept": "text/html,application/xhtml+xml,application/json,text/plain;q=0.9,*/*;q=0.8",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw_bytes = resp.read(1_048_576)
                encoding = resp.headers.get_content_charset() or "utf-8"
                raw_text = raw_bytes.decode(encoding, errors="replace")

            parser = TextExtractParser()
            parser.feed(raw_text)
            extracted = "".join(parser.text_parts)
            extracted = re.sub(r"\n{3,}", "\n\n", extracted).strip()
            if len(extracted) < 50 and len(raw_text.strip()) > len(extracted):
                extracted = raw_text.strip()

            redacted = str(redact_secrets(extracted))[:max_chars]
            return {
                "ok": True,
                "url": url,
                "content": redacted,
                "length": len(redacted),
            }
        except Exception as exc:
            return {
                "ok": False,
                "url": url,
                "error": f"Failed to extract URL: {exc}",
            }

    def _tool_memory(self, workspace_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
        action = str(args.get("action", "list")).lower()
        key = str(args.get("key") or args.get("subject", "")).strip()
        val = args.get("value") or args.get("content")
        scope = str(args.get("scope", "project"))
        project_id = str(args.get("project_id", workspace_dir.name))

        mem_file = workspace_dir / ".hex" / "memory.json"
        local_store: dict[str, Any] = {}
        if mem_file.is_file():
            try:
                loaded = json.loads(mem_file.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    local_store = loaded
            except Exception:
                local_store = {}

        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        gov_mem = None
        try:
            from .router import GovernedMemoryManager
            gov_mem = GovernedMemoryManager(self.ledger, self.graphify)
        except Exception:
            pass

        if action in ("set", "save"):
            if not key:
                return {"ok": False, "error": "Key or subject is required to save memory"}
            local_store[key] = {
                "value": val,
                "scope": scope,
                "updated_at": now,
            }
            try:
                mem_file.parent.mkdir(parents=True, exist_ok=True)
                mem_file.write_text(json.dumps(local_store, indent=2), encoding="utf-8")
            except OSError:
                pass

            if gov_mem is not None:
                try:
                    gov_mem.record_memory(
                        project_id=project_id,
                        subject=key,
                        content=str(val),
                        scope=scope,
                        workspace_dir=workspace_dir,
                    )
                except Exception:
                    pass

            return {"ok": True, "action": "set", "key": key, "saved": True}

        elif action in ("get", "read"):
            if key in local_store:
                return {"ok": True, "action": "get", "key": key, "memory": local_store[key]}
            if gov_mem is not None:
                try:
                    mems = gov_mem.retrieve_memories(project_id=project_id, query=key, workspace_dir=workspace_dir)
                    if mems:
                        return {"ok": True, "action": "get", "key": key, "memory": mems[0]}
                except Exception:
                    pass
            return {"ok": False, "action": "get", "key": key, "error": f"Memory key '{key}' not found"}

        elif action in ("delete", "forget"):
            existed = key in local_store
            if existed:
                del local_store[key]
                try:
                    mem_file.write_text(json.dumps(local_store, indent=2), encoding="utf-8")
                except OSError:
                    pass
            if gov_mem is not None and key:
                try:
                    gov_mem.forget_memory(key)
                except Exception:
                    pass
            return {"ok": True, "action": "delete", "key": key, "deleted": existed}

        else:
            query = str(args.get("query", "")).lower()
            results: dict[str, Any] = {}
            for k, v in local_store.items():
                if not query or query in k.lower() or query in str(v).lower():
                    results[k] = v
            return {
                "ok": True,
                "action": "list",
                "memories": results,
                "count": len(results),
            }

    def _tool_lsp_diagnostics(self, workspace_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
        mgr = self._get_lsp_manager(workspace_dir)
        file_path_arg = args.get("file_path") or args.get("path")
        severity = args.get("severity", "all")

        if file_path_arg:
            target_path = self._resolve_confined_path(workspace_dir, str(file_path_arg)) if not Path(str(file_path_arg)).is_absolute() else Path(str(file_path_arg))
            spec = mgr.registry.find_server_for_file(target_path, require_installed=True)
            if not spec:
                spec_uninstalled = mgr.registry.find_server_for_file(target_path, require_installed=False)
                return {
                    "status": "no_server_available",
                    "language": spec_uninstalled.language_id if spec_uninstalled else "unknown",
                    "diagnostics": [],
                    "error_count": 0,
                    "warning_count": 0,
                    "total_count": 0,
                }
            if target_path.is_file():
                mgr.sync_file(target_path)
                # Bounded poll for async publishDiagnostics notification from language server
                start_t = time.time()
                diags = []
                while time.time() - start_t < 0.6:
                    diags = mgr.get_diagnostics(file_path=target_path, severity=severity)
                    if diags:
                        break
                    time.sleep(0.02)
            else:
                diags = mgr.get_diagnostics(file_path=target_path, severity=severity)
        else:
            diags = mgr.get_diagnostics(severity=severity)

        diag_dicts = [d.to_dict() for d in diags]
        error_count = sum(1 for d in diags if d.severity == 1 or d.severity_name == "ERROR")
        warning_count = sum(1 for d in diags if d.severity == 2 or d.severity_name == "WARNING")

        return {
            "diagnostics": diag_dicts,
            "error_count": error_count,
            "warning_count": warning_count,
            "total_count": len(diag_dicts),
        }

    def _tool_lsp_definition(self, workspace_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
        mgr = self._get_lsp_manager(workspace_dir)
        file_path_arg = args.get("file_path") or args.get("path")
        if not file_path_arg:
            return {"definitions": []}
        target_path = self._resolve_confined_path(workspace_dir, str(file_path_arg)) if not Path(str(file_path_arg)).is_absolute() else Path(str(file_path_arg))
        spec = mgr.registry.find_server_for_file(target_path, require_installed=True)
        if not spec:
            spec_uninstalled = mgr.registry.find_server_for_file(target_path, require_installed=False)
            return {
                "status": "no_server_available",
                "language": spec_uninstalled.language_id if spec_uninstalled else "unknown",
                "definitions": [],
            }
        line = int(args.get("line", 1))
        character = int(args.get("character", 1))
        defs = mgr.get_definition(target_path, line, character)
        return {"definitions": defs}

    def _tool_lsp_references(self, workspace_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
        mgr = self._get_lsp_manager(workspace_dir)
        file_path_arg = args.get("file_path") or args.get("path")
        if not file_path_arg:
            return {"references": []}
        target_path = self._resolve_confined_path(workspace_dir, str(file_path_arg)) if not Path(str(file_path_arg)).is_absolute() else Path(str(file_path_arg))
        spec = mgr.registry.find_server_for_file(target_path, require_installed=True)
        if not spec:
            spec_uninstalled = mgr.registry.find_server_for_file(target_path, require_installed=False)
            return {
                "status": "no_server_available",
                "language": spec_uninstalled.language_id if spec_uninstalled else "unknown",
                "references": [],
            }
        line = int(args.get("line", 1))
        character = int(args.get("character", 1))
        include_decl = bool(args.get("include_declaration", True))
        refs = mgr.get_references(target_path, line, character, include_declaration=include_decl)
        return {"references": refs}

    def _tool_lsp_hover(self, workspace_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
        mgr = self._get_lsp_manager(workspace_dir)
        file_path_arg = args.get("file_path") or args.get("path")
        if not file_path_arg:
            return {"hover": {"contents": ""}}
        target_path = self._resolve_confined_path(workspace_dir, str(file_path_arg)) if not Path(str(file_path_arg)).is_absolute() else Path(str(file_path_arg))
        spec = mgr.registry.find_server_for_file(target_path, require_installed=True)
        if not spec:
            spec_uninstalled = mgr.registry.find_server_for_file(target_path, require_installed=False)
            return {
                "status": "no_server_available",
                "language": spec_uninstalled.language_id if spec_uninstalled else "unknown",
                "hover": {"contents": ""},
            }
        line = int(args.get("line", 1))
        character = int(args.get("character", 1))
        hover_data = mgr.get_hover(target_path, line, character)
        if not hover_data:
            return {"hover": {"contents": ""}}
        return {"hover": hover_data}

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

    @staticmethod
    def _apply_unified_diff(original: str, diff_text: str) -> tuple[bool, str, str]:
        lines = original.splitlines(keepends=True)
        diff_lines = diff_text.splitlines(keepends=True)

        hunk_header_re = re.compile(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@")
        hunks: list[tuple[int, int, list[str]]] = []
        current_hunk: list[str] = []
        old_start = 0
        old_len = 0

        for line in diff_lines:
            m = hunk_header_re.match(line)
            if m:
                if current_hunk:
                    hunks.append((old_start, old_len, current_hunk))
                    current_hunk = []
                old_start = int(m.group(1))
                old_len = int(m.group(2)) if m.group(2) is not None else 1
            elif hunks or old_start > 0:
                current_hunk.append(line)

        if current_hunk:
            hunks.append((old_start, old_len, current_hunk))

        if not hunks:
            return False, original, "No unified diff hunks found in diff text"

        result_lines = list(lines)
        for start, length, hunk_lines in sorted(hunks, key=lambda h: h[0], reverse=True):
            line_idx = max(0, start - 1)
            expected_old: list[str] = []
            replacement: list[str] = []
            for hline in hunk_lines:
                if hline.startswith("-"):
                    expected_old.append(hline[1:])
                elif hline.startswith("+"):
                    replacement.append(hline[1:])
                elif hline.startswith(" ") or not hline.strip():
                    ctx = hline[1:] if hline.startswith(" ") else hline
                    expected_old.append(ctx)
                    replacement.append(ctx)

            old_chunk_size = len(expected_old)
            matched_idx = -1

            def chunk_matches(idx: int) -> bool:
                actual = [l.rstrip("\r\n") for l in result_lines[idx : idx + old_chunk_size]]
                expected = [l.rstrip("\r\n") for l in expected_old]
                return actual == expected

            if 0 <= line_idx <= len(result_lines) - old_chunk_size and chunk_matches(line_idx):
                matched_idx = line_idx
            else:
                search_min = max(0, line_idx - 20)
                search_max = min(len(result_lines) - old_chunk_size, line_idx + 20)
                for cand in range(search_min, search_max + 1):
                    if chunk_matches(cand):
                        matched_idx = cand
                        break

            if matched_idx == -1:
                old_str = "".join(expected_old).strip()
                all_str = "".join(result_lines)
                if old_str and old_str in all_str:
                    new_str = "".join(replacement).strip()
                    all_str = all_str.replace(old_str, new_str, 1)
                    return True, all_str, ""
                return False, original, f"Hunk at line {start} does not match target file contents"

            result_lines[matched_idx : matched_idx + old_chunk_size] = replacement

        return True, "".join(result_lines), ""

    def _tool_patch(self, workspace_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
        target = self._resolve_confined_path(workspace_dir, str(args["path"]))
        rel_p = str(args["path"]).replace("\\", "/").lstrip("./")
        target.parent.mkdir(parents=True, exist_ok=True)

        before_content = ""
        if target.is_file():
            before_content = target.read_text(encoding="utf-8", errors="replace")

        after_content = before_content

        find_str = args.get("find") or args.get("search")
        if find_str is not None:
            find_str = str(find_str)
            replace_str = str(args.get("replace", ""))
            if find_str in before_content:
                after_content = before_content.replace(find_str, replace_str, 1)
            elif find_str.replace("\r\n", "\n") in before_content.replace("\r\n", "\n"):
                norm_before = before_content.replace("\r\n", "\n")
                norm_find = find_str.replace("\r\n", "\n")
                norm_replace = replace_str.replace("\r\n", "\n")
                after_content = norm_before.replace(norm_find, norm_replace, 1)
            else:
                return {
                    "ok": False,
                    "error": f"Search block not found in {args['path']}",
                    "path": str(args["path"]),
                }
        elif "diff" in args or "patch" in args:
            diff_text = str(args.get("diff") or args.get("patch", ""))
            success, patched_content, err = self._apply_unified_diff(before_content, diff_text)
            if not success:
                return {
                    "ok": False,
                    "error": f"Failed to apply patch: {err}",
                    "path": str(args["path"]),
                }
            after_content = patched_content
        else:
            return {
                "ok": False,
                "error": "Either 'find'/'replace' or 'diff' must be provided to patch",
                "path": str(args["path"]),
            }

        target.write_text(after_content, encoding="utf-8")

        blast_info = None
        if hasattr(self, "blast_interceptor") and self.blast_interceptor is not None:
            try:
                report = self.blast_interceptor.inspect_file_modification(
                    workspace_dir,
                    rel_p,
                    before_sources={rel_p: before_content},
                )
                if report:
                    blast_info = report.to_dict()
            except Exception:
                pass

        res = {
            "ok": True,
            "path": str(args["path"]),
            "bytes_written": len(after_content.encode("utf-8")),
            "sha256": sha256_hex(after_content),
        }
        if blast_info:
            res["blast_radius"] = blast_info
        return res

    def _tool_run_tests(self, workspace_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
        from .recipes import detect_recipe
        import shlex

        recipe = detect_recipe(workspace_dir)
        explicit_cmd = args.get("command")
        target_tests = args.get("targets") or []
        if isinstance(target_tests, str):
            target_tests = [target_tests]

        if explicit_cmd:
            if isinstance(explicit_cmd, list):
                cmd = list(explicit_cmd)
            else:
                cmd = shlex.split(str(explicit_cmd))
            if target_tests:
                cmd.extend(target_tests)
        elif target_tests:
            if recipe:
                cmd = list(recipe.test_cmd_args) + list(target_tests)
            else:
                cmd = [sys.executable, "-m", "unittest"] + list(target_tests)
        else:
            if recipe:
                cmd = list(recipe.test_cmd_args)
            else:
                cmd = [sys.executable, "-m", "unittest", "discover", "-s", ".", "-p", "test_*.py"]

        # Fall back to standard python -m unittest discovery if pytest is not installed on PATH
        if cmd and cmd[0] == "pytest" and not shutil.which("pytest") and not (Path(sys.executable).parent / "pytest").is_file():
            if target_tests:
                cmd = [sys.executable, "-m", "unittest"] + list(target_tests)
            else:
                cmd = [sys.executable, "-m", "unittest", "discover", "-s", ".", "-p", "test_*.py"]

        env = {"PYTHONPATH": str(workspace_dir)}
        timeout = float(args.get("timeout", 30.0))

        result = self.sandbox.execute(
            cmd=cmd,
            cwd=workspace_dir,
            env=env,
            timeout=timeout,
        )

        if result.violation_detected:
            raise PolicyBlockedError(result.violation_reason or "Sandbox security violation detected")

        combined_out = (result.stdout + "\n" + result.stderr).strip()
        redacted_out = str(redact_secrets(combined_out))[:16_000]
        is_ok = (result.exit_code == 0) and not result.timed_out and not result.violation_detected

        return {
            "ok": is_ok,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "violation_detected": result.violation_detected,
            "sandbox_backend": result.sandbox_backend,
            "output": redacted_out,
            "output_sha256": sha256_hex(redacted_out),
            "recipe": recipe.name if recipe else None,
        }

    def _tool_terminal(self, workspace_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
        raw_cmd = args.get("command") or args.get("cmd")
        if not raw_cmd:
            return {"ok": False, "error": "Command parameter is required"}

        if isinstance(raw_cmd, list):
            cmd = [str(c) for c in raw_cmd]
        else:
            cmd_str = str(raw_cmd).strip()
            if any(op in cmd_str for op in ("|", ">", "<", ";", "&&", "||", "$")):
                cmd = ["/bin/sh", "-c", cmd_str]
            else:
                cmd = shlex.split(cmd_str)

        timeout = float(args.get("timeout", 30.0))
        env = dict(args.get("env") or {})
        env.setdefault("PYTHONPATH", str(workspace_dir))

        result = self.sandbox.execute(
            cmd=cmd,
            cwd=workspace_dir,
            env=env,
            timeout=timeout,
        )

        if result.violation_detected:
            raise PolicyBlockedError(result.violation_reason or "Sandbox security violation detected")

        stdout_redacted = str(redact_secrets(result.stdout))[:16_000]
        stderr_redacted = str(redact_secrets(result.stderr))[:8_000]
        is_ok = (result.exit_code == 0) and not result.timed_out

        return {
            "ok": is_ok,
            "exit_code": result.exit_code,
            "stdout": stdout_redacted,
            "stderr": stderr_redacted,
            "timed_out": result.timed_out,
            "sandbox_backend": result.sandbox_backend,
        }

    def _tool_start_process(self, workspace_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
        command = str(args.get("command", "")).strip()
        if not command:
            return {"ok": False, "error": "Command parameter is required"}

        cwd_arg = str(args.get("cwd", "")).strip()
        if cwd_arg:
            target_cwd = self._resolve_confined_path(workspace_dir, cwd_arg)
        else:
            target_cwd = workspace_dir

        pty_enabled = bool(args.get("pty", True))

        try:
            session = self.process_registry.spawn(
                command=command,
                cwd=str(target_cwd),
                use_pty=pty_enabled,
            )
            data = session.to_dict()
            initial_output = redact_secrets(
                self.process_registry.get_tail_output(session.id, n=2000, strip_ansi_codes=False)
            )
            res: dict[str, Any] = {
                "ok": True,
                "process_id": session.id,
                "initial_output": initial_output,
                **data,
            }
            return redact_secrets(res)
        except Exception as exc:
            return {"ok": False, "error": f"Failed spawning process: {exc}"}

    def _tool_read_terminal(self, workspace_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
        process_id = str(args.get("process_id", "")).strip()
        if not process_id:
            return {"ok": False, "error": "process_id parameter is required"}

        offset = int(args.get("offset", 0))
        limit = int(args.get("limit", 2000))

        try:
            res = self.process_registry.read_output(process_id, offset=offset, limit=limit)
            res["ok"] = True
            res["output"] = redact_secrets(res.get("output", ""))
            return redact_secrets(res)
        except KeyError:
            return {"ok": False, "error": f"Process not found: {process_id}"}
        except Exception as exc:
            return {"ok": False, "error": f"Failed reading terminal output: {exc}"}

    def _tool_kill_process(self, workspace_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
        process_id = str(args.get("process_id", "")).strip()
        if not process_id:
            return {"ok": False, "error": "process_id parameter is required"}

        force = bool(args.get("force", False))

        try:
            res = self.process_registry.kill(process_id, force=force)
            return {"ok": True, "process_id": process_id, **res}
        except KeyError:
            return {"ok": False, "error": f"Process not found: {process_id}"}
        except Exception as exc:
            return {"ok": False, "error": f"Failed killing process: {exc}"}

    def _tool_list_processes(self, workspace_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
        status_filter = args.get("status")
        if status_filter == "all" or not status_filter:
            status_filter = None

        try:
            procs = self.process_registry.list_processes(status_filter=status_filter)
            return {"ok": True, "processes": redact_secrets(procs), "count": len(procs)}
        except Exception as exc:
            return {"ok": False, "error": f"Failed listing processes: {exc}", "processes": [], "count": 0}

    def _tool_process(self, workspace_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
        action = str(args.get("action", "list")).lower()
        if action == "list":
            supervised = self.process_registry.list_processes()
            if supervised:
                return {"ok": True, "processes": redact_secrets(supervised), "count": len(supervised)}
            procs: list[dict[str, Any]] = []
            try:
                ps_res = subprocess.run(
                    ["ps", "-eo", "pid,ppid,stat,command"],
                    capture_output=True,
                    text=True,
                    timeout=5.0,
                    check=False,
                )
                if ps_res.returncode == 0:
                    ws_str = str(workspace_dir)
                    for line in ps_res.stdout.splitlines()[1:]:
                        parts = line.strip().split(None, 3)
                        if len(parts) >= 4:
                            pid_s, ppid_s, stat, cmd = parts
                            if ws_str in cmd or any(w in cmd.lower() for w in ("pytest", "python", "node", "uvicorn", "cargo", "harness")):
                                try:
                                    procs.append({
                                        "pid": int(pid_s),
                                        "ppid": int(ppid_s),
                                        "stat": stat,
                                        "command": cmd[:200],
                                    })
                                except ValueError:
                                    pass
            except Exception as e:
                return {"ok": False, "error": str(e), "processes": []}
            return {"ok": True, "processes": procs, "count": len(procs)}

        elif action == "kill":
            target_pid = args.get("pid")
            proc_id = args.get("process_id")

            if target_pid is not None:
                try:
                    target_pid_int = int(target_pid)
                except ValueError:
                    return {"ok": False, "error": f"Invalid PID: {target_pid}"}

                if target_pid_int <= 1 or target_pid_int == os.getpid():
                    raise PolicyBlockedError(f"Cannot terminate protected system or current process (PID {target_pid_int})")

                for s in self.process_registry.list_processes():
                    if s.get("pid") == target_pid_int:
                        res = self.process_registry.kill(s["id"], force=bool(args.get("force", False)))
                        return {"ok": True, "killed_pid": target_pid_int, **res}

                sig_val = int(args.get("signal", signal.SIGTERM))
                try:
                    os.kill(target_pid_int, sig_val)
                    return {"ok": True, "killed_pid": target_pid_int, "signal": sig_val}
                except ProcessLookupError:
                    return {"ok": False, "error": f"Process {target_pid_int} not found"}
                except PermissionError as pe:
                    return {"ok": False, "error": f"Permission denied killing PID {target_pid_int}: {pe}"}
            elif proc_id is not None:
                try:
                    res = self.process_registry.kill(str(proc_id), force=bool(args.get("force", False)))
                    return {"ok": True, "process_id": proc_id, **res}
                except KeyError:
                    return {"ok": False, "error": f"Process {proc_id} not found"}
            else:
                return {"ok": False, "error": "PID or process_id parameter required to kill process"}
        else:
            return {"ok": False, "error": f"Unknown process action: {action}"}

    def _tool_git_push_remote(self, workspace_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "pushed_ref": args.get("branch", "main"),
            "remote": args.get("remote", "origin"),
        }

    def _tool_modify_policy(self, workspace_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
        raise PolicyBlockedError("R4 policy modification is prohibited")

    def _get_subagent_manager(self) -> SubagentManager:
        if self.subagent_manager is None:
            from .subagent import SubagentManager
            self.subagent_manager = SubagentManager(
                ledger=self.ledger,
                gateway=self,
            )
        return self.subagent_manager

    def _tool_delegate(self, workspace_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
        mgr = self._get_subagent_manager()
        goal = args.get("goal")
        if not goal:
            return {"ok": False, "error": "Missing required 'goal' parameter"}
        role = args.get("role", "general")
        context = args.get("context")
        wait = args.get("wait", True)
        timeout = float(args.get("timeout", 120.0))
        parent_task_id = args.get("task_id", "current_task")

        try:
            res = mgr.spawn_subagent(
                parent_task_id=parent_task_id,
                goal=goal,
                role=role,
                context=context,
                parent_ws=workspace_dir,
                current_depth=int(args.get("current_depth", 0)),
                timeout=timeout,
                wait=wait,
            )
            if isinstance(res, str):
                return {"ok": True, "subagent_id": res, "status": "running", "mode": "async"}
            return {"ok": True, "result": res.to_dict(), "status": res.status}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _tool_subagent_status(self, workspace_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
        mgr = self._get_subagent_manager()
        sid = args.get("subagent_id")
        if not sid:
            return {"ok": False, "error": "Missing 'subagent_id'"}
        try:
            status = mgr.get_subagent_status(sid)
            return {"ok": True, "subagent": status}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _tool_subagent_wait(self, workspace_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
        mgr = self._get_subagent_manager()
        sid = args.get("subagent_id")
        if not sid:
            return {"ok": False, "error": "Missing 'subagent_id'"}
        timeout = float(args.get("timeout", 60.0))
        try:
            res = mgr.wait_subagent(sid, timeout=timeout)
            return {"ok": True, "result": res.to_dict()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _get_browser_manager(self) -> Any:
        if self.browser_manager is None:
            from .browser import BrowserManager
            self.browser_manager = BrowserManager()
        return self.browser_manager

    def _tool_browser(self, workspace_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
        mgr = self._get_browser_manager()
        task_id = args.get("task_id", "default_browser_session")
        session = mgr.get_or_create_session(task_id)
        action = args.get("action", "snapshot").lower().strip()

        if action == "navigate":
            url = args.get("url")
            if not url:
                return {"ok": False, "error": "Missing 'url' for browser navigate"}
            res = session.navigate(url)
            return {"ok": res.ok, "result": res.to_dict(), "error": res.error}
        elif action == "snapshot":
            res = session.snapshot()
            return {"ok": res.ok, "result": res.to_dict()}
        elif action == "click":
            sel = args.get("selector")
            if not sel:
                return {"ok": False, "error": "Missing 'selector' for browser click"}
            res = session.click(sel)
            return {"ok": res.ok, "result": res.to_dict(), "error": res.error}
        elif action == "type":
            sel = args.get("selector")
            txt = args.get("text", "")
            if not sel:
                return {"ok": False, "error": "Missing 'selector' for browser type"}
            res = session.type(sel, txt)
            return {"ok": res.ok, "result": res.to_dict(), "error": res.error}
        elif action == "screenshot":
            out_p = args.get("output_path")
            if out_p and not Path(out_p).is_absolute():
                out_p = str(workspace_dir / out_p)
            res = session.screenshot(out_p)
            return {"ok": res.ok, "result": res.to_dict(), "screenshot_path": res.screenshot_path}
        elif action == "evaluate":
            script = args.get("script", "")
            res = session.evaluate(script)
            return {"ok": res.ok, "result": res.to_dict()}
        elif action == "close":
            session.close()
            mgr.close_session(task_id)
            return {"ok": True, "message": "Browser session closed"}
        else:
            return {"ok": False, "error": f"Unknown browser action: {action}"}

    def _get_vision_engine(self) -> Any:
        if self.vision_engine is None:
            from .vision import VisionEngine
            self.vision_engine = VisionEngine()
        return self.vision_engine

    def _tool_image_inspect(self, workspace_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
        engine = self._get_vision_engine()
        img_p = args.get("image_path")
        prompt = args.get("prompt")
        if not img_p or not prompt:
            return {"ok": False, "error": "Missing required 'image_path' or 'prompt' parameter"}
        detail = args.get("detail", "auto")
        return engine.inspect_image(img_p, prompt, detail=detail, workspace_dir=workspace_dir)
