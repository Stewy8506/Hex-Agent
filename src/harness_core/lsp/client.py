"""Polyglot Language Server Protocol (LSP) Client and Stdio Transport.

Implements JSON-RPC 2.0 message framing over stdio using standard
HTTP-style Content-Length headers, asynchronous reader threads,
request/response demultiplexing, notification dispatch, document synchronization,
and compiler diagnostics tracking.
"""

from __future__ import annotations

import collections
import json
import logging
import os
import subprocess
import threading
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

SEVERITY_MAP: dict[int, str] = {
    1: "ERROR",
    2: "WARNING",
    3: "INFO",
    4: "HINT",
}


def path_to_uri(path: Path | str) -> str:
    """Convert local file path to LSP file URI."""
    return Path(path).resolve().as_uri()


def uri_to_path(uri: str) -> str:
    """Convert LSP file URI to local filesystem path."""
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme == "file":
        return urllib.request.url2pathname(parsed.path)
    return uri


@dataclass
class LSPDiagnostic:
    """Represents a compiler / linter diagnostic published by an LSP server."""

    file_path: str
    line: int
    character: int
    end_line: int
    end_character: int
    severity: int  # 1=Error, 2=Warning, 3=Info, 4=Hint
    severity_name: str  # "ERROR", "WARNING", "INFO", "HINT"
    message: str
    source: str = ""
    code: str | int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize diagnostic to dictionary."""
        return {
            "file_path": self.file_path,
            "line": self.line,
            "character": self.character,
            "end_line": self.end_line,
            "end_character": self.end_character,
            "severity": self.severity,
            "severity_name": self.severity_name,
            "message": self.message,
            "source": self.source,
            "code": self.code,
        }


@dataclass
class LSPMessage:
    """Represents a JSON-RPC 2.0 message."""

    jsonrpc: str = "2.0"
    id: int | str | None = None
    method: str | None = None
    params: dict[str, Any] | list[Any] | None = None
    result: Any = None
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert message to dictionary omitting None keys where appropriate."""
        data: dict[str, Any] = {"jsonrpc": self.jsonrpc}
        if self.id is not None:
            data["id"] = self.id
        if self.method is not None:
            data["method"] = self.method
        if self.params is not None:
            data["params"] = self.params
        if self.result is not None:
            data["result"] = self.result
        if self.error is not None:
            data["error"] = self.error
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LSPMessage:
        """Construct an LSPMessage from a parsed JSON dictionary."""
        return cls(
            jsonrpc=data.get("jsonrpc", "2.0"),
            id=data.get("id"),
            method=data.get("method"),
            params=data.get("params"),
            result=data.get("result"),
            error=data.get("error"),
        )


class LSPResponseError(Exception):
    """Raised when a language server returns a JSON-RPC error response."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"LSP error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


class LSPTimeoutError(TimeoutError):
    """Raised when an LSP request times out waiting for server response."""

    pass


class StdioLSPTransport:
    """Subprocess stdio transport with HTTP-style Content-Length framing."""

    def __init__(
        self,
        command: list[str] | None = None,
        cwd: Path | str | None = None,
        env: dict[str, str] | None = None,
        process: subprocess.Popen[bytes] | None = None,
    ) -> None:
        if process is not None:
            self.process = process
            self.command = command or []
        elif command:
            self.command = list(command)
            self.cwd = Path(cwd) if cwd else None
            self.env = dict(env) if env is not None else None
            self.process = subprocess.Popen(
                self.command,
                cwd=str(self.cwd) if self.cwd else None,
                env=self.env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        else:
            raise ValueError("Either command or process must be provided")

        self.cwd = Path(cwd) if cwd else None
        self._next_id = 0
        self._id_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._handler_lock = threading.Lock()

        # req_id -> (Event, result_box)
        self._pending: dict[int | str, tuple[threading.Event, dict[str, Any]]] = {}
        # method -> list[callable]
        self._handlers: dict[str, list[Callable[[dict[str, Any]], None]]] = {}
        self._stderr_buffer: collections.deque[str] = collections.deque(maxlen=100)

        self._closed = False
        self._closing = threading.Event()

        # Reader threads
        self._stdout_thread = threading.Thread(
            target=self._read_stdout_loop,
            name="lsp-stdout-reader",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr_loop,
            name="lsp-stderr-reader",
            daemon=True,
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

    @property
    def is_alive(self) -> bool:
        """Check if the transport and underlying subprocess are active."""
        return not self._closed and self.process.poll() is None

    def _get_next_id(self) -> int:
        with self._id_lock:
            self._next_id += 1
            return self._next_id

    def register_handler(
        self,
        method: str,
        handler: Callable[[dict[str, Any]], None],
    ) -> None:
        """Register a notification callback for a given method or '*'."""
        with self._handler_lock:
            if method not in self._handlers:
                self._handlers[method] = []
            self._handlers[method].append(handler)

    def _read_stdout_loop(self) -> None:
        """Continuously parse Content-Length framed JSON-RPC messages from stdout."""
        stdout = self.process.stdout
        if stdout is None:
            return

        while not self._closing.is_set():
            try:
                # 1. Read headers until blank line (\r\n or \n)
                content_length: int | None = None
                while not self._closing.is_set():
                    line = stdout.readline()
                    if not line:
                        return  # Subprocess closed stdout
                    line_clean = line.strip()
                    if not line_clean:
                        # Blank line indicates end of headers
                        if content_length is not None:
                            break
                        continue

                    if b":" in line:
                        k, v = line.split(b":", 1)
                        if k.strip().lower() == b"content-length":
                            try:
                                content_length = int(v.strip())
                            except ValueError:
                                pass

                if content_length is None:
                    continue

                # 2. Read exact body length
                body_chunks: list[bytes] = []
                bytes_read = 0
                while bytes_read < content_length and not self._closing.is_set():
                    chunk_to_read = min(4096, content_length - bytes_read)
                    chunk = stdout.read(chunk_to_read)
                    if not chunk:
                        return  # Subprocess closed before full body
                    body_chunks.append(chunk)
                    bytes_read += len(chunk)

                if bytes_read < content_length:
                    return

                raw_body = b"".join(body_chunks)
                text = raw_body.decode("utf-8", errors="replace")
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    logger.debug("Non-JSON stdout body from LSP server: %s", text)
                    continue

                if not isinstance(payload, dict):
                    continue

                self._handle_inbound_message(payload)

            except Exception as exc:
                if not self._closing.is_set():
                    logger.debug("Exception in LSP stdout loop: %s", exc)
                break

        # EOF or close: unblock any remaining pending requests
        with self._pending_lock:
            for event, result_box in self._pending.values():
                result_box["error"] = ConnectionError("LSP server closed connection")
                event.set()
            self._pending.clear()

    def _handle_inbound_message(self, payload: dict[str, Any]) -> None:
        """Route message based on whether it is a response, request, or notification."""
        req_id = payload.get("id")
        method = payload.get("method")

        # Case 1: Server response to client request (id present, method absent)
        if req_id is not None and method is None:
            with self._pending_lock:
                entry = self._pending.pop(req_id, None)
            if entry:
                event, result_box = entry
                if "error" in payload and payload["error"] is not None:
                    result_box["error"] = payload["error"]
                else:
                    result_box["result"] = payload.get("result")
                event.set()

        # Case 2: Server request to client (id present, method present)
        elif req_id is not None and method is not None:
            self._respond_to_server_request(req_id, method, payload.get("params"))

        # Case 3: Server notification (no id, method present)
        elif method is not None:
            self._dispatch_notification(method, payload.get("params"))

    def _respond_to_server_request(
        self,
        req_id: int | str,
        method: str,
        params: Any,
    ) -> None:
        """Respond with safe default to server requests to prevent deadlocks."""
        logger.debug("LSP server-to-client request: method=%s, id=%s", method, req_id)
        default_result: Any = None
        if method == "workspace/configuration":
            items = params.get("items", []) if isinstance(params, dict) else []
            default_result = [None] * len(items)

        response = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": default_result,
        }
        try:
            self._send_payload(response)
        except Exception as exc:
            logger.debug("Failed responding to server request: %s", exc)

    def _dispatch_notification(self, method: str, params: Any) -> None:
        """Dispatch notification to registered handlers."""
        handlers: list[Callable[[dict[str, Any]], None]] = []
        with self._handler_lock:
            handlers.extend(self._handlers.get(method, []))
            handlers.extend(self._handlers.get("*", []))

        notification_data = {"method": method, "params": params}
        for handler in handlers:
            try:
                handler(notification_data)
            except Exception as exc:
                logger.warning("Error in LSP notification handler for %s: %s", method, exc)

    def _read_stderr_loop(self) -> None:
        """Collect stderr diagnostics into rolling buffer."""
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

    def _send_payload(self, payload: dict[str, Any]) -> None:
        """Encode and write a Content-Length framed JSON-RPC message."""
        if self._closed or self._closing.is_set():
            raise ConnectionError("Stdio transport is closed")

        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        header = f"Content-Length: {len(data)}\r\n\r\n".encode("ascii")
        with self._write_lock:
            stdin = self.process.stdin
            if stdin is None or stdin.closed:
                raise ConnectionError("LSP server stdin is closed")
            stdin.write(header + data)
            stdin.flush()

    def send_request(
        self,
        method: str,
        params: Any = None,
        timeout: float = 30.0,
    ) -> Any:
        """Send a JSON-RPC 2.0 request and wait for the result."""
        req_id = self._get_next_id()
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = params

        event = threading.Event()
        result_box: dict[str, Any] = {"result": None, "error": None}

        with self._pending_lock:
            self._pending[req_id] = (event, result_box)

        try:
            self._send_payload(payload)
        except Exception:
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise

        signaled = event.wait(timeout=timeout)
        if not signaled:
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise LSPTimeoutError(
                f"LSP request '{method}' (id={req_id}) timed out after {timeout:.1f}s"
            )

        err = result_box["error"]
        if err is not None:
            if isinstance(err, Exception):
                raise err
            if isinstance(err, dict):
                raise LSPResponseError(
                    code=err.get("code", -1),
                    message=err.get("message", "Unknown LSP error"),
                    data=err.get("data"),
                )
            raise LSPResponseError(code=-1, message=str(err))

        return result_box["result"]

    def send_notification(self, method: str, params: Any = None) -> None:
        """Send a one-way JSON-RPC 2.0 notification."""
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        self._send_payload(payload)

    def close(self, timeout: float = 2.0) -> None:
        """Gracefully terminate child process and release resources."""
        if self._closed:
            return
        self._closed = True
        self._closing.set()

        if self.process.stdin and not self.process.stdin.closed:
            try:
                self.process.stdin.close()
            except Exception:
                pass

        if self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=timeout)
            except (subprocess.TimeoutExpired, Exception):
                try:
                    self.process.kill()
                    self.process.wait(timeout=1.0)
                except Exception:
                    pass

        # Close stdout and stderr streams to release file descriptors cleanly
        if self.process.stdout and not self.process.stdout.closed:
            try:
                self.process.stdout.close()
            except Exception:
                pass

        if self.process.stderr and not self.process.stderr.closed:
            try:
                self.process.stderr.close()
            except Exception:
                pass

        with self._pending_lock:
            for event, result_box in self._pending.values():
                result_box["error"] = ConnectionError("LSP transport closed")
                event.set()
            self._pending.clear()


class LSPClient:
    """High-level Polyglot LSP client managing lifecycle, documents, and diagnostics."""

    def __init__(
        self,
        transport: StdioLSPTransport | None = None,
        command: list[str] | None = None,
        workspace_root: Path | str | None = None,
        spec: Any = None,
        cwd: Path | str | None = None,
    ) -> None:
        if transport is not None:
            self.transport = transport
        elif command:
            self.transport = StdioLSPTransport(command=command, cwd=cwd or workspace_root)
        else:
            raise ValueError("Either transport or command must be provided")

        self.workspace_root = Path(workspace_root).resolve() if workspace_root else None
        self.spec = spec
        self._diagnostics: dict[str, list[LSPDiagnostic]] = {}
        self._diag_lock = threading.Lock()
        self._doc_versions: dict[str, int] = {}
        self._initialized = False
        self._server_capabilities: dict[str, Any] = {}

        # Register publishDiagnostics handler
        self.transport.register_handler(
            "textDocument/publishDiagnostics",
            self._on_publish_diagnostics,
        )

    @property
    def is_alive(self) -> bool:
        """Return True if underlying transport is alive."""
        return self.transport.is_alive

    @property
    def is_initialized(self) -> bool:
        """Return True if LSP initialize handshake succeeded."""
        return self._initialized

    @property
    def capabilities(self) -> dict[str, Any]:
        """Return language server capabilities returned during initialization."""
        return dict(self._server_capabilities)

    def _on_publish_diagnostics(self, notification: dict[str, Any]) -> None:
        """Parse textDocument/publishDiagnostics notification into LSPDiagnostic objects."""
        params = notification.get("params") or {}
        uri = params.get("uri", "")
        raw_diagnostics = params.get("diagnostics", [])

        file_path = uri_to_path(uri) if uri else ""
        parsed: list[LSPDiagnostic] = []

        for d in raw_diagnostics:
            rng = d.get("range", {})
            start = rng.get("start", {})
            end = rng.get("end", {})
            sev = d.get("severity", 1)
            sev_name = SEVERITY_MAP.get(sev, "ERROR")

            parsed.append(
                LSPDiagnostic(
                    file_path=file_path or uri,
                    line=start.get("line", 0),
                    character=start.get("character", 0),
                    end_line=end.get("line", 0),
                    end_character=end.get("character", 0),
                    severity=sev,
                    severity_name=sev_name,
                    message=d.get("message", ""),
                    source=d.get("source", ""),
                    code=d.get("code"),
                )
            )

        with self._diag_lock:
            if uri:
                self._diagnostics[uri] = parsed
            if file_path:
                self._diagnostics[file_path] = parsed

    def initialize(
        self,
        workspace_root: Path | str | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Complete standard LSP initialize handshake and initialized notification."""
        if workspace_root:
            self.workspace_root = Path(workspace_root).resolve()

        root_path_str = str(self.workspace_root) if self.workspace_root else None
        root_uri_str = path_to_uri(self.workspace_root) if self.workspace_root else None

        init_params: dict[str, Any] = {
            "processId": os.getpid(),
            "rootUri": root_uri_str,
            "rootPath": root_path_str,
            "capabilities": {
                "workspace": {
                    "applyEdit": True,
                    "workspaceFolders": True,
                    "configuration": True,
                },
                "textDocument": {
                    "synchronization": {
                        "dynamicRegistration": False,
                        "willSave": False,
                        "willSaveWaitUntil": False,
                        "didSave": True,
                    },
                    "publishDiagnostics": {
                        "relatedInformation": True,
                        "versionSupport": True,
                        "codeDescriptionSupport": True,
                        "dataSupport": True,
                    },
                    "hover": {
                        "dynamicRegistration": False,
                        "contentFormat": ["markdown", "plaintext"],
                    },
                    "definition": {
                        "dynamicRegistration": False,
                        "linkSupport": True,
                    },
                    "references": {
                        "dynamicRegistration": False,
                    },
                },
            },
            "trace": "off",
        }

        if self.spec and getattr(self.spec, "initialization_options", None):
            init_params["initializationOptions"] = self.spec.initialization_options

        result = self.transport.send_request("initialize", init_params, timeout=timeout)
        self._server_capabilities = result.get("capabilities", {}) if isinstance(result, dict) else {}
        self.transport.send_notification("initialized", {})
        self._initialized = True
        return result if isinstance(result, dict) else {}

    def did_open(
        self,
        file_path: Path | str,
        text: str | None = None,
        language_id: str | None = None,
    ) -> None:
        """Send textDocument/didOpen notification for a file."""
        path_obj = Path(file_path).resolve()
        uri = path_to_uri(path_obj)

        if text is None:
            try:
                text = path_obj.read_text(encoding="utf-8")
            except Exception:
                text = ""

        if language_id is None:
            if self.spec and getattr(self.spec, "language_id", None):
                language_id = self.spec.language_id
            else:
                ext_map = {
                    ".py": "python",
                    ".ts": "typescript",
                    ".tsx": "typescriptreact",
                    ".js": "javascript",
                    ".jsx": "javascriptreact",
                    ".go": "go",
                    ".rs": "rust",
                    ".c": "c",
                    ".cpp": "cpp",
                    ".java": "java",
                }
                language_id = ext_map.get(path_obj.suffix.lower(), "plaintext")

        self._doc_versions[uri] = 1
        params = {
            "textDocument": {
                "uri": uri,
                "languageId": language_id,
                "version": 1,
                "text": text,
            }
        }
        self.transport.send_notification("textDocument/didOpen", params)

    def did_change(
        self,
        file_path: Path | str,
        text: str,
        version: int | None = None,
    ) -> None:
        """Send textDocument/didChange notification with full document contents."""
        path_obj = Path(file_path).resolve()
        uri = path_to_uri(path_obj)

        if version is None:
            version = self._doc_versions.get(uri, 1) + 1
        self._doc_versions[uri] = version

        params = {
            "textDocument": {
                "uri": uri,
                "version": version,
            },
            "contentChanges": [
                {"text": text}
            ],
        }
        self.transport.send_notification("textDocument/didChange", params)

    def did_close(self, file_path: Path | str) -> None:
        """Send textDocument/didClose notification."""
        path_obj = Path(file_path).resolve()
        uri = path_to_uri(path_obj)
        self._doc_versions.pop(uri, None)

        params = {
            "textDocument": {
                "uri": uri,
            }
        }
        self.transport.send_notification("textDocument/didClose", params)

    def get_definition(
        self,
        file_path: Path | str,
        line: int,
        character: int,
        timeout: float = 10.0,
    ) -> list[dict[str, Any]]:
        """Query symbol definition locations at line/character (0-indexed)."""
        uri = path_to_uri(file_path)
        params = {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
        }
        res = self.transport.send_request("textDocument/definition", params, timeout=timeout)
        if res is None:
            return []
        if isinstance(res, dict):
            return [res]
        if isinstance(res, list):
            return res
        return []

    def get_references(
        self,
        file_path: Path | str,
        line: int,
        character: int,
        include_declaration: bool = True,
        timeout: float = 10.0,
    ) -> list[dict[str, Any]]:
        """Find symbol references across the workspace at line/character (0-indexed)."""
        uri = path_to_uri(file_path)
        params = {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
            "context": {"includeDeclaration": include_declaration},
        }
        res = self.transport.send_request("textDocument/references", params, timeout=timeout)
        if res is None:
            return []
        if isinstance(res, list):
            return res
        if isinstance(res, dict):
            return [res]
        return []

    def get_hover(
        self,
        file_path: Path | str,
        line: int,
        character: int,
        timeout: float = 10.0,
    ) -> dict[str, Any] | None:
        """Retrieve hover documentation and type signatures at line/character (0-indexed)."""
        uri = path_to_uri(file_path)
        params = {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
        }
        res = self.transport.send_request("textDocument/hover", params, timeout=timeout)
        return res if isinstance(res, dict) else None

    def get_diagnostics(
        self,
        file_path: Path | str | None = None,
        severity: str | int | None = None,
    ) -> list[LSPDiagnostic]:
        """Retrieve stored diagnostics filtered by file and/or severity."""
        with self._diag_lock:
            if file_path is not None:
                target_str = str(Path(file_path).resolve())
                target_uri = path_to_uri(file_path)
                diags = self._diagnostics.get(target_str) or self._diagnostics.get(target_uri) or []
                candidates = list(diags)
            else:
                # Deduplicate by (file_path, line, character, message)
                seen: set[tuple[str, int, int, str]] = set()
                candidates = []
                for diag_list in self._diagnostics.values():
                    for d in diag_list:
                        key = (d.file_path, d.line, d.character, d.message)
                        if key not in seen:
                            seen.add(key)
                            candidates.append(d)

        if severity is None or str(severity).upper() == "ALL":
            return candidates

        sev_filter: str | int
        if isinstance(severity, int):
            sev_filter = severity
            return [d for d in candidates if d.severity == sev_filter]
        else:
            sev_name_filter = str(severity).upper()
            return [d for d in candidates if d.severity_name == sev_name_filter]

    def shutdown(self, timeout: float = 5.0) -> None:
        """Send LSP shutdown request, exit notification, and close transport."""
        if self._initialized:
            try:
                self.transport.send_request("shutdown", None, timeout=timeout)
            except Exception as exc:
                logger.debug("Error during LSP shutdown request: %s", exc)
            try:
                self.transport.send_notification("exit", None)
            except Exception as exc:
                logger.debug("Error during LSP exit notification: %s", exc)
        self.transport.close()
        self._initialized = False
