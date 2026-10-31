"""
MCP Client Adapter and Transport Implementations.

Provides zero-dependency Model Context Protocol (MCP) JSON-RPC 2.0 client
connectivity over Stdio (subprocess) and HTTP/SSE transports, with dynamic
tool discovery and R0-R4 risk classification.
"""

from __future__ import annotations

import collections
import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from harness_core.models import RiskClass

logger = logging.getLogger(__name__)


@runtime_checkable
class MCPTransport(Protocol):
    """Protocol defining the interface for an MCP transport."""

    def send_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Send a JSON-RPC 2.0 request and wait for the response result."""
        ...

    def send_notification(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        """Send a one-way JSON-RPC 2.0 notification."""
        ...

    def close(self) -> None:
        """Close the transport and release underlying resources."""
        ...


@dataclass
class MCPToolDefinition:
    """Represents an MCP tool discovered from a remote server."""

    name: str
    description: str
    input_schema: dict[str, Any]
    risk_class: RiskClass
    server_id: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize tool definition to a dictionary."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "risk_class": self.risk_class.value if isinstance(self.risk_class, RiskClass) else str(self.risk_class),
            "server_id": self.server_id,
        }


def classify_mcp_tool_risk(
    name: str,
    description: str = "",
    overrides: dict[str, RiskClass] | None = None,
) -> RiskClass:
    """
    Classify an MCP tool into risk classes R0-R4.

    Heuristic precedence:
    1. Per-server / caller overrides (if name present in overrides).
    2. R4 (Root / Destructive / Policy / Secret).
    3. R3 (Privileged / Execution / Shell / Network Outbound).
    4. R2 (Workspace Write / Build / Mutate / Test).
    5. R1 (Read-Only Inspection / Query / Search).
    6. Safe Default-Deny: unrecognized defaults to R2.
    """
    if overrides and name in overrides:
        val = overrides[name]
        return val if isinstance(val, RiskClass) else RiskClass(val)

    name_lower = (name or "").strip().lower()
    desc_lower = (description or "").strip().lower()
    name_tokens = set(re.split(r"[^a-z0-9]+", name_lower))
    desc_tokens = set(re.split(r"[^a-z0-9]+", desc_lower))
    all_tokens = name_tokens | desc_tokens

    def _matches(prefixes: tuple[str, ...], keywords: set[str]) -> bool:
        for p in prefixes:
            if name_lower == p or name_lower.startswith(f"{p}_") or name_lower.startswith(f"{p}-") or name_lower.startswith(f"{p}."):
                return True
        if bool(all_tokens & keywords):
            return True
        return False

    # 1. R4 (Root / Destructive / Policy / Secret)
    r4_prefixes = ("sudo", "root", "secret", "auth", "policy", "revoke", "grant", "admin", "eval", "format_disk")
    r4_keywords = {"root", "admin", "secret", "credential", "token", "master_key"}
    if _matches(r4_prefixes, r4_keywords):
        return RiskClass.R4

    # 2. R3 (Privileged / Execution / Shell / Network Outbound)
    r3_prefixes = ("exec", "shell", "bash", "cmd", "command", "publish", "deploy", "git_push", "post", "send_email", "external_call")
    r3_keywords = {"exec", "shell", "command", "deploy", "publish"}
    if _matches(r3_prefixes, r3_keywords):
        return RiskClass.R3

    # 3. R2 (Workspace Write / Build / Mutate / Test)
    r2_prefixes = ("write", "create", "update", "modify", "edit", "append", "patch", "delete", "remove", "run_test", "compile", "build")
    r2_keywords = {"write", "update", "delete", "edit", "modify"}
    if _matches(r2_prefixes, r2_keywords):
        return RiskClass.R2

    # 4. R1 (Read-Only Inspection / Query / Search)
    r1_prefixes = ("get", "read", "list", "fetch", "search", "query", "describe", "view", "find", "check", "inspect")
    r1_keywords = {"read", "list", "query", "search", "fetch"}
    if _matches(r1_prefixes, r1_keywords):
        return RiskClass.R1

    # Safe Default-Deny: unrecognized tools default to R2 (never R0 or R1)
    return RiskClass.R2


class StdioMCPTransport(MCPTransport):
    """
    Subprocess-based standard I/O MCP transport.

    Communicates with an MCP server subprocess via stdin and stdout using
    JSON-RPC 2.0 message framing (supporting both newline-delimited JSON and
    Content-Length header framing).
    """

    def __init__(
        self,
        command: list[str],
        cwd: Path | str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.command = list(command)
        self.cwd = Path(cwd) if cwd else None
        self.env = dict(env) if env is not None else None

        self._next_id = 0
        self._id_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[int, tuple[threading.Event, dict[str, Any]]] = {}
        self._notifications: list[dict[str, Any]] = []
        self._stderr_buffer: collections.deque[str] = collections.deque(maxlen=200)

        self._closed = False
        self._closing = threading.Event()

        # Launch child subprocess
        self.process = subprocess.Popen(
            self.command,
            cwd=str(self.cwd) if self.cwd else None,
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )

        # Background reader threads
        self._stdout_thread = threading.Thread(
            target=self._read_stdout_loop,
            name="mcp-stdio-stdout-reader",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr_loop,
            name="mcp-stdio-stderr-reader",
            daemon=True,
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _get_next_id(self) -> int:
        with self._id_lock:
            self._next_id += 1
            return self._next_id

    def _read_stdout_loop(self) -> None:
        """Continuously read and demultiplex JSON-RPC responses from stdout."""
        stdout = self.process.stdout
        if stdout is None:
            return

        while not self._closing.is_set():
            try:
                line = stdout.readline()
                if not line:
                    break

                # Support Content-Length header framing if present
                line_lower = line.lower()
                if line_lower.startswith(b"content-length:"):
                    parts = line.split(b":", 1)
                    length = int(parts[1].strip())
                    # Consume remaining headers until blank line
                    while True:
                        hdr = stdout.readline()
                        if hdr in (b"\r\n", b"\n", b""):
                            break
                    raw_body = stdout.read(length)
                    if not raw_body:
                        break
                    text = raw_body.decode("utf-8", errors="replace")
                else:
                    text = line.decode("utf-8", errors="replace").strip()

                if not text:
                    continue

                try:
                    message = json.loads(text)
                except json.JSONDecodeError:
                    logger.debug("Non-JSON stdout line from MCP server: %s", text)
                    continue

                if not isinstance(message, dict):
                    continue

                # Response message contains "id"
                if "id" in message and message["id"] is not None:
                    req_id = message["id"]
                    with self._pending_lock:
                        entry = self._pending.pop(req_id, None)
                    if entry:
                        event, result_box = entry
                        if "error" in message and message["error"] is not None:
                            result_box["error"] = message["error"]
                        else:
                            result_box["response"] = message.get("result", {})
                        event.set()
                else:
                    # Notification
                    self._notifications.append(message)

            except Exception as exc:
                if not self._closing.is_set():
                    logger.debug("Exception in MCP stdout reader: %s", exc)
                break

        # If stdout closed or EOF reached, unblock all pending requests
        with self._pending_lock:
            for event, result_box in self._pending.values():
                result_box["error"] = ConnectionError("MCP subprocess closed connection")
                event.set()
            self._pending.clear()

    def _read_stderr_loop(self) -> None:
        """Capture stderr diagnostics into rolling buffer."""
        stderr = self.process.stderr
        if stderr is None:
            return

        while not self._closing.is_set():
            try:
                line = stderr.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip()
                self._stderr_buffer.append(decoded)
            except Exception:
                break

    def get_stderr_diagnostics(self) -> str:
        """Return captured stderr lines."""
        return "\n".join(self._stderr_buffer)

    def send_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        if self._closed or self._closing.is_set():
            raise ConnectionError("Stdio transport is closed")

        req_id = self._get_next_id()
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params if params is not None else {},
        }

        event = threading.Event()
        result_box: dict[str, Any] = {"response": None, "error": None}

        with self._pending_lock:
            self._pending[req_id] = (event, result_box)

        try:
            with self._write_lock:
                if self.process.stdin is None or self.process.stdin.closed:
                    raise ConnectionError("MCP subprocess stdin is closed")
                data = (json.dumps(payload) + "\n").encode("utf-8")
                self.process.stdin.write(data)
                self.process.stdin.flush()
        except (OSError, BrokenPipeError) as exc:
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise ConnectionError(f"Failed to write to MCP stdin: {exc}") from exc

        # Wait for response with timeout
        if not event.wait(timeout=timeout):
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise TimeoutError(f"MCP request '{method}' (id={req_id}) timed out after {timeout}s")

        error_data = result_box.get("error")
        if error_data is not None:
            if isinstance(error_data, Exception):
                raise error_data
            if isinstance(error_data, dict):
                code = error_data.get("code")
                msg = error_data.get("message", "JSON-RPC error")
                data = error_data.get("data")
                if code == -32601:
                    raise ValueError(f"MCP method not found: {method} ({msg})")
                raise RuntimeError(f"MCP RPC error [{code}]: {msg} (data={data})")
            raise RuntimeError(f"MCP RPC error: {error_data}")

        return result_box.get("response", {})

    def send_notification(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        if self._closed or self._closing.is_set():
            return

        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params if params is not None else {},
        }
        try:
            with self._write_lock:
                if self.process.stdin is None or self.process.stdin.closed:
                    return
                data = (json.dumps(payload) + "\n").encode("utf-8")
                self.process.stdin.write(data)
                self.process.stdin.flush()
        except (OSError, BrokenPipeError):
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._closing.set()

        # Unblock pending requests
        with self._pending_lock:
            for event, result_box in self._pending.values():
                result_box["error"] = ConnectionError("Transport closed")
                event.set()
            self._pending.clear()

        # Close stdin
        if self.process.stdin and not self.process.stdin.closed:
            try:
                self.process.stdin.close()
            except (OSError, BrokenPipeError):
                pass

        # Terminate process gracefully
        if self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=2.0)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    self.process.kill()
                    self.process.wait(timeout=1.0)
                except OSError:
                    pass

        # Close stdout and stderr pipes
        if self.process.stdout and not self.process.stdout.closed:
            try:
                self.process.stdout.close()
            except (OSError, BrokenPipeError):
                pass
        if self.process.stderr and not self.process.stderr.closed:
            try:
                self.process.stderr.close()
            except (OSError, BrokenPipeError):
                pass

        # Join reader threads
        if self._stdout_thread.is_alive():
            self._stdout_thread.join(timeout=0.5)
        if self._stderr_thread.is_alive():
            self._stderr_thread.join(timeout=0.5)


class HttpSSEMCPTransport(MCPTransport):
    """
    HTTP and Server-Sent Events (SSE) based MCP transport.

    Communicates with an MCP server endpoint over HTTP POST requests using
    standard library urllib.
    """

    def __init__(
        self,
        endpoint_url: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.endpoint_url = endpoint_url
        self.headers = headers or {}
        self._next_id = 0
        self._lock = threading.Lock()
        self._closed = False
        self._notifications: list[dict[str, Any]] = []

    def _get_next_id(self) -> int:
        with self._lock:
            self._next_id += 1
            return self._next_id

    def send_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        if self._closed:
            raise ConnectionError("HttpSSEMCPTransport is closed")

        req_id = self._get_next_id()
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params if params is not None else {},
        }
        body = json.dumps(payload).encode("utf-8")
        req_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            **self.headers,
        }
        req = urllib.request.Request(
            self.endpoint_url,
            data=body,
            headers=req_headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp_bytes = resp.read()
                resp_data = json.loads(resp_bytes.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            try:
                err_json = json.loads(err_body)
                if isinstance(err_json, dict) and "error" in err_json:
                    err = err_json["error"]
                    code = err.get("code")
                    msg = err.get("message", "HTTP RPC error")
                    if code == -32601:
                        raise ValueError(f"MCP method not found: {method} ({msg})")
                    raise RuntimeError(f"MCP RPC error [{code}]: {msg}")
            except (json.JSONDecodeError, TypeError):
                pass
            raise RuntimeError(f"HTTP request failed with status {exc.code}: {err_body}") from exc
        except (socket.timeout, TimeoutError) as exc:
            raise TimeoutError(f"HTTP request for '{method}' timed out after {timeout}s") from exc
        except OSError as exc:
            raise ConnectionError(f"HTTP request connection failed: {exc}") from exc

        if not isinstance(resp_data, dict):
            raise RuntimeError(f"Invalid JSON-RPC response format: {resp_data}")

        if "error" in resp_data and resp_data["error"] is not None:
            err = resp_data["error"]
            code = err.get("code")
            msg = err.get("message", "JSON-RPC error")
            if code == -32601:
                raise ValueError(f"MCP method not found: {method} ({msg})")
            raise RuntimeError(f"MCP RPC error [{code}]: {msg}")

        return resp_data.get("result", {})

    def send_notification(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        if self._closed:
            return
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params if params is not None else {},
        }
        body = json.dumps(payload).encode("utf-8")
        req_headers = {
            "Content-Type": "application/json",
            **self.headers,
        }
        req = urllib.request.Request(
            self.endpoint_url,
            data=body,
            headers=req_headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10.0):
                pass
        except Exception:
            pass

    def close(self) -> None:
        self._closed = True


class MCPClientAdapter:
    """
    High-level Model Context Protocol (MCP) client adapter.

    Manages lifecycle initialization, protocol handshake, dynamic tool discovery,
    risk classification, tool invocation, and manifest extraction.
    """

    def __init__(
        self,
        server_id: str,
        transport: MCPTransport,
        risk_overrides: dict[str, RiskClass] | None = None,
    ) -> None:
        self.server_id = server_id
        self.transport = transport
        self.risk_overrides = dict(risk_overrides) if risk_overrides else {}

        self.initialized = False
        self.server_info: dict[str, Any] = {}
        self.server_capabilities: dict[str, Any] = {}
        self.protocol_version: str = ""
        self.tools: dict[str, MCPToolDefinition] = {}

    def initialize(self, timeout: float = 30.0) -> dict[str, Any]:
        """
        Execute standard MCP initialize handshake and send notifications/initialized.
        """
        params = {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "roots": {"listChanged": True},
                "sampling": {},
            },
            "clientInfo": {
                "name": "harness-mcp-client",
                "version": "0.1.0",
            },
        }

        result = self.transport.send_request("initialize", params=params, timeout=timeout)
        self.protocol_version = result.get("protocolVersion", "2024-11-05")
        self.server_info = result.get("serverInfo", {})
        self.server_capabilities = result.get("capabilities", {})

        # Send notifications/initialized
        self.transport.send_notification("notifications/initialized", {})
        self.initialized = True
        return result

    def discover_tools(self, timeout: float = 30.0) -> list[MCPToolDefinition]:
        """
        Query remote MCP server for available tools via tools/list,
        classify risks dynamically, and store definitions.
        """
        if not self.initialized:
            self.initialize(timeout=timeout)

        result = self.transport.send_request("tools/list", {}, timeout=timeout)
        tools_raw = result.get("tools", [])

        discovered: list[MCPToolDefinition] = []
        for raw in tools_raw:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name", "")).strip()
            if not name:
                continue
            description = str(raw.get("description", ""))
            input_schema = raw.get("inputSchema", {})
            if not isinstance(input_schema, dict):
                input_schema = {}

            risk = classify_mcp_tool_risk(name, description, self.risk_overrides)
            tool_def = MCPToolDefinition(
                name=name,
                description=description,
                input_schema=input_schema,
                risk_class=risk,
                server_id=self.server_id,
            )
            self.tools[name] = tool_def
            discovered.append(tool_def)

        return discovered

    def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """
        Invoke an MCP tool on the remote server via tools/call.

        Returns structured tool output:
        {"ok": bool, "output": str, "content": list, "raw": dict}
        """
        params = {
            "name": tool_name,
            "arguments": arguments,
        }
        result = self.transport.send_request("tools/call", params=params, timeout=timeout)

        content = result.get("content", [])
        is_error = bool(result.get("isError", False))

        # Extract text snippets from content items
        text_snippets: list[str] = []
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_snippets.append(str(item.get("text", "")))
                elif isinstance(item, str):
                    text_snippets.append(item)
                elif isinstance(item, dict):
                    text_snippets.append(json.dumps(item))
        elif isinstance(content, str):
            text_snippets.append(content)

        combined_text = "\n".join(text_snippets)
        if not combined_text and is_error:
            combined_text = "Tool execution returned an error."

        return {
            "ok": not is_error,
            "output": combined_text,
            "content": content,
            "raw": result,
        }

    def get_manifest_data(self) -> dict[str, Any]:
        """
        Return snapshot dictionary of server info and discovered tools
        suitable for SHA-256 pinning and SkillSpector security validation.
        """
        return {
            "server_id": self.server_id,
            "server_info": self.server_info,
            "protocol_version": self.protocol_version,
            "tools": [t.to_dict() for t in self.tools.values()],
        }

    def close(self) -> None:
        """Close transport and reset adapter state."""
        self.transport.close()
        self.initialized = False
