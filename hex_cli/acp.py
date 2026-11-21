"""HEX private ACP-style JSON-RPC 2.0 stdio server.

This is HEX's *internal* Agent Client Protocol dialect, spoken by HEX's own Rust
desktop/TUI clients. It is NOT a certified implementation of the standard Agent
Client Protocol: it uses an MCP-style ``protocolVersion`` string, custom
approval/update payloads, and does not expose the standard
authenticate/permission/fs/terminal/mode surfaces. Compatibility with standard
ACP editors (Zed, VSCode, Neovim, Cursor) is unverified — the matching HEX Rust
client is not proof of third-party editor interop. The ``initialize`` result
advertises ``protocolDialect`` so a client can detect this explicitly.

Dispatched methods:
- initialize, ping
- session/new, session/load, session/list, session/models
- session/prompt, session/cancel, session/command
- session/history, session/historyContent, session/toolResult, session/subagents
- session/approvalDecision
- tools/list, tools/call
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness_core import __version__
from harness_core.storage import compute_project_id

logger = logging.getLogger(__name__)


_SECRET_TEXT_NAMES = (
    "key", "api_key", "apikey", "api-key", "token", "access_token",
    "auth_token", "refresh_token", "id_token", "secret", "client_secret",
    "password", "passwd", "authorization", "x-api-key", "x-auth-token",
)


def _redact_sensitive_text(text: object) -> str:
    """Mask credentials embedded in arbitrary text before it reaches a client.

    Provider discovery errors can echo the request URL (e.g. Gemini's
    ``?key=...``) or an Authorization header; returning that raw would leak the
    configured API key/token to any ACP client consuming ``session/models``.
    """
    s = str(text)
    names = "|".join(re.escape(n) for n in _SECRET_TEXT_NAMES)
    s = re.sub(
        rf"([?&](?:{names})=)[^&\s\"'<>]+",
        r"\1[REDACTED]",
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(
        r"(Bearer\s+)[A-Za-z0-9._\-+/=]+",
        r"\1[REDACTED]",
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(
        rf"\b((?:{names})\s*[=:]\s*)[\"']?[^\s\"',;}}\[\]]+",
        r"\1[REDACTED]",
        s,
        flags=re.IGNORECASE,
    )
    return s


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


def build_conversation_history(
    records: Sequence[dict[str, Any]], limit: int = 10
) -> list[dict[str, Any]]:
    """Rebuild model context from durable turns without dropping tool work (B41).

    Previously only prompt/response text survived, so tool results and reasoning
    vanished from follow-up turns. Structured rows keep each turn's tool calls
    paired with their outcomes.
    """
    history: list[dict[str, Any]] = []
    for turn in list(records)[-limit:]:
        if not isinstance(turn, dict):
            continue
        if turn.get("prompt"):
            history.append({"role": "user", "content": str(turn["prompt"])})
        for event in (turn.get("tool_calls") or [])[-20:]:
            if not isinstance(event, dict):
                continue
            payload = event.get("payload", "")
            if isinstance(payload, str):
                content = payload[:1500]
            else:
                content = json.dumps(payload, default=str)[:1500]
            history.append({
                "role": "tool",
                "tool": str(event.get("tool") or event.get("toolId") or "tool"),
                "status": str(event.get("event") or event.get("eventType") or "complete"),
                "content": content,
            })
        if turn.get("reasoning"):
            history.append({"role": "reasoning", "content": str(turn["reasoning"])[:1200]})
        if turn.get("response"):
            history.append({"role": "assistant", "content": str(turn["response"])})
    return history


class SQLiteHistory(Sequence[dict[str, Any]]):
    """Append-only transcript handle. No connections or records are retained."""

    def __init__(self, path: Path, session_id: str) -> None:
        self.path = path
        self.session_id = session_id

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path), timeout=30)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS history (
                    session_id TEXT NOT NULL,
                    turn_index INTEGER NOT NULL,
                    record_json TEXT NOT NULL,
                    task_id TEXT,
                    metadata_json TEXT NOT NULL,
                    prompt TEXT,
                    response TEXT,
                    prompt_chars INTEGER NOT NULL,
                    response_chars INTEGER NOT NULL,
                    PRIMARY KEY (session_id, turn_index)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS history_tasks ON history(session_id, task_id)")

    def _insert(self, conn: sqlite3.Connection, index: int, record: dict[str, Any]) -> None:
        prompt = record.get("prompt")
        response = record.get("response")
        prompt = prompt if isinstance(prompt, str) else None
        response = response if isinstance(response, str) else None
        metadata = dict(record)
        for name, text in (("prompt", prompt), ("response", response)):
            if text is not None:
                metadata[name] = ""
        task_id = record.get("task_id")
        conn.execute("""
            INSERT INTO history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id, turn_index) DO NOTHING
        """, (
            self.session_id, index, json.dumps(record, ensure_ascii=False),
            str(task_id) if task_id is not None else None,
            json.dumps(metadata, ensure_ascii=False), prompt, response,
            len(prompt) if prompt is not None else 0,
            len(response) if response is not None else 0,
        ))

    def migrate(self, records: Sequence[dict[str, Any]]) -> None:
        self.initialize()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for index, record in enumerate(records):
                self._insert(conn, index, record)

    def append(self, record: dict[str, Any]) -> None:
        with self._connect() as conn:
            # Allocate ordinals inside the write transaction, including across threads.
            conn.execute("BEGIN IMMEDIATE")
            index = conn.execute(
                "SELECT COALESCE(MAX(turn_index) + 1, 0) FROM history WHERE session_id = ?",
                (self.session_id,),
            ).fetchone()[0]
            self._insert(conn, index, record)

    def __len__(self) -> int:
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM history WHERE session_id = ?", (self.session_id,)
            ).fetchone()[0]

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(len(self)))]
        if not isinstance(index, int):
            raise TypeError("History indices must be integers or slices")
        if index < 0:
            index += len(self)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT record_json FROM history WHERE session_id = ? AND turn_index = ?",
                (self.session_id, index),
            ).fetchone()
        if row is None:
            raise IndexError("History index out of range")
        return json.loads(row[0])

    def __iter__(self) -> Iterator[dict[str, Any]]:
        for index in range(len(self)):
            yield self[index]

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, Sequence):
            return NotImplemented
        return len(self) == len(other) and all(a == b for a, b in zip(self, other))

    def has_task(self, task_id: str) -> bool:
        with self._connect() as conn:
            return conn.execute(
                "SELECT 1 FROM history WHERE session_id = ? AND task_id = ? LIMIT 1",
                (self.session_id, task_id),
            ).fetchone() is not None

    def task_ids(self) -> set[str]:
        with self._connect() as conn:
            return {row[0] for row in conn.execute(
                "SELECT DISTINCT task_id FROM history WHERE session_id = ? AND task_id IS NOT NULL AND task_id != ''",
                (self.session_id,),
            )}

    @staticmethod
    def _clip_prefix(text: str, budget: int) -> str:
        # Budget for default JSON escaping too, which is at least as large as the
        # server's ensure_ascii=False UTF-8 encoding (notably for astral Unicode).
        if len(json.dumps(text)) - 2 <= budget:
            return text
        low, high = 0, len(text)
        while low < high:
            mid = (low + high + 1) // 2
            if len(json.dumps(text[:mid])) - 2 <= budget:
                low = mid
            else:
                high = mid - 1
        return text[:low]

    def page(self, before: int, limit: int, base: dict[str, Any] | None = None) -> dict[str, Any]:
        result = dict(base or {})
        with self._connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM history WHERE session_id = ?", (self.session_id,)
            ).fetchone()[0]
            if before > count:
                raise ValueError("before exceeds historyCount")
            start = max(0, before - limit)
            # Reject impossible metadata pages without allocating their full JSON.
            metadata_bytes = conn.execute("""
                SELECT COALESCE(SUM(length(CAST(metadata_json AS BLOB))), 0)
                FROM history WHERE session_id = ? AND turn_index >= ? AND turn_index < ?
            """, (self.session_id, start, before)).fetchone()[0]
            if metadata_bytes > 64 * 1024:
                raise ValueError("History page metadata exceeds 64 KiB")
            records = []
            text_fields = []
            for index, metadata, pc, rc, pt, rt in conn.execute("""
                SELECT turn_index, metadata_json, prompt_chars, response_chars,
                       prompt IS NOT NULL, response IS NOT NULL
                FROM history WHERE session_id = ? AND turn_index >= ? AND turn_index < ?
                ORDER BY turn_index
            """, (self.session_id, start, before)):
                record = json.loads(metadata)
                record.update(turnIndex=index, promptChars=pc, responseChars=rc)
                records.append(record)
                for name, is_text in (("prompt", pt), ("response", rt)):
                    if is_text:
                        text_fields.append((record, index, name))
            result.update(history=records, historyCount=count)
            if start > 0 and records:
                result["nextBefore"] = records[0]["turnIndex"]
            remaining = 64 * 1024 - len(json.dumps(result).encode("utf-8"))
            if remaining < 0:
                raise ValueError("History page metadata exceeds 64 KiB")
            budget = remaining // max(1, len(text_fields))
            for record, index, name in text_fields:
                cap = min(4096, budget)
                # BLOB slicing also preserves embedded NULs. At most four UTF-8
                # bytes per Unicode character; never select the original JSON.
                raw = conn.execute(
                    f"SELECT substr(CAST({name} AS BLOB), 1, ?) FROM history WHERE session_id = ? AND turn_index = ?",
                    (cap * 4, self.session_id, index),
                ).fetchone()[0]
                prefix = raw.decode("utf-8", errors="ignore")[:cap]
                record[name] = self._clip_prefix(prefix, budget)
        return result

    def content(self, index: int, name: str, offset: int, limit: int) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                f"""SELECT {name}_chars, {name} IS NOT NULL,
                           length({name}) = {name}_chars, substr({name}, ?, ?)
                    FROM history WHERE session_id = ? AND turn_index = ?""",
                (offset + 1, limit, self.session_id, index),
            ).fetchone()
            if row is None:
                raise ValueError("turnIndex is out of range")
            total, is_text, no_nul, content = row
            if not is_text:
                raise ValueError("History field has no text content")
            if offset > total:
                raise ValueError("offset exceeds totalChars")
            if not no_nul:
                # SQLite TEXT substr/length stops at NUL. Only this exceptional
                # case needs bounded BLOB scanning to locate Unicode offsets.
                byte_offset = 1
                chars_left = offset
                pieces = []
                wanted = min(limit, total - offset)
                while chars_left or wanted:
                    raw = conn.execute(
                        f"SELECT substr(CAST({name} AS BLOB), ?, 16384) FROM history WHERE session_id = ? AND turn_index = ?",
                        (byte_offset, self.session_id, index),
                    ).fetchone()[0]
                    text = raw.decode("utf-8", errors="ignore")
                    if not text:
                        raise ValueError("Invalid stored history text")
                    byte_offset += len(text.encode("utf-8"))
                    skip = min(chars_left, len(text))
                    chars_left -= skip
                    if not chars_left:
                        part = text[skip:skip + wanted]
                        pieces.append(part)
                        wanted -= len(part)
                content = "".join(pieces)
        result = {"content": content, "totalChars": total}
        if offset + len(content) < total:
            result["nextOffset"] = offset + len(content)
        return result


@dataclass
class ACPSession:
    """Active ACP editor session."""

    session_id: str
    workspace_root: Path
    model: str = "default"
    history: list[dict[str, Any]] | SQLiteHistory = field(default_factory=list)
    cancelled: bool = False
    created_at: float = field(default_factory=time.time)
    last_task_id: str | None = None
    running: bool = False
    cancel_ack: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        self.cancel_ack.set()

    def has_task(self, task_id: str) -> bool:
        if isinstance(self.history, SQLiteHistory):
            return self.history.has_task(task_id)
        return any(turn.get("task_id") == task_id for turn in self.history)

    def task_ids(self) -> set[str]:
        if isinstance(self.history, SQLiteHistory):
            return self.history.task_ids()
        return {str(turn["task_id"]) for turn in self.history if turn.get("task_id")}


_SUPPORTED_METHODS = (
    "initialize", "ping",
    "session/new", "session/load", "session/list", "session/models",
    "session/prompt", "session/cancel", "session/command",
    "session/history", "session/historyContent", "session/toolResult",
    "session/subagents", "session/approvalDecision",
    "tools/list", "tools/call",
)


class ACPServer:
    """HEX private ACP-style JSON-RPC 2.0 stdio server (not certified standard ACP)."""

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
        self._persist_lock = threading.RLock()
        self._tools_call_task_id: str | None = None
        self._tools_call_seq = 0
        self._turn_threads: list[threading.Thread] = []
        self._shutting_down = False

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
        if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_\-]+", session_id):
            return None
        return self._sessions_dir / f"{session_id}.json"

    def _persist_session(self, sess: ACPSession) -> bool:
        path = self._session_path(sess.session_id)
        if path is None:
            return False
        with self._persist_lock:
            try:
                history = sess.history
                if not isinstance(history, SQLiteHistory):
                    durable = SQLiteHistory(self._sessions_dir / "history.db", sess.session_id)
                    # Commit all legacy records first. A failed metadata replace
                    # leaves both the source JSON and its in-memory list intact.
                    durable.migrate(history)
                else:
                    durable = history
                payload = {
                    "session_id": sess.session_id,
                    "workspace_root": str(sess.workspace_root),
                    "model": sess.model,
                    "created_at": sess.created_at,
                    "last_task_id": sess.last_task_id,
                    "history_store": "sqlite",
                }
                tmp = path.with_suffix(f".{threading.get_ident()}.tmp")
                tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                tmp.replace(path)
                sess.history = durable
                return True
            except Exception as exc:
                logger.warning("Failed to persist ACP session %s: %s", sess.session_id, exc)
                return False

    def _load_session_from_disk(self, session_id: str) -> ACPSession | None:
        path = self._session_path(session_id)
        if path is None or not path.is_file():
            return None
        with self._persist_lock:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data["session_id"] != session_id:
                    raise ValueError("Session metadata identity does not match its path")
                if data.get("history_store") == "sqlite":
                    db_path = self._sessions_dir / "history.db"
                    if not db_path.is_file():
                        raise FileNotFoundError("Session history storage is missing")
                    history = SQLiteHistory(db_path, session_id)
                else:
                    history = data.get("history") or []
                sess = ACPSession(
                    session_id=session_id,
                    workspace_root=Path(data.get("workspace_root") or self.workspace_root),
                    model=data.get("model", "default"),
                    history=history,
                    created_at=float(data.get("created_at") or time.time()),
                    last_task_id=data.get("last_task_id"),
                )
                if not isinstance(history, SQLiteHistory):
                    self._persist_session(sess)
                return sess
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
        params = req.get("params", {})
        if method not in ("session/load", "session/history", "session/historyContent", "session/toolResult"):
            params = params or {}

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
            elif method == "session/history":
                res = self._handle_session_history(params)
            elif method == "session/historyContent":
                res = self._handle_session_history_content(params)
            elif method == "session/prompt":
                res = self._handle_session_prompt(params)
            elif method == "session/cancel":
                res = self._handle_session_cancel(params)
            elif method == "session/list":
                res = self._handle_session_list(params)
            elif method == "session/models":
                res = self._handle_session_models(params)
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
                    "error": {
                        "code": -32601,
                        "message": (
                            f"Method not found: {method}. This is HEX's private "
                            "ACP dialect (hex-private-acp/1). Supported methods: "
                            + ", ".join(_SUPPORTED_METHODS)
                        ),
                    },
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
                    "code": -32602 if method in {
                        "session/toolResult", "session/load", "session/history", "session/historyContent",
                        "session/approvalDecision"
                    } and isinstance(exc, (ValueError, TypeError)) else -32000,
                    "message": str(exc),
                },
            }

    def _handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "protocolVersion": "2024-11-05",
            "protocolDialect": "hex-private-acp/1",
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
                "description": (
                    "HEX internal ACP-style dialect (hex-private-acp/1); not a "
                    "certified standard Agent Client Protocol implementation. "
                    "Zed/VSCode/Neovim/Cursor interop is unverified."
                ),
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

    def _validate_history_params(self, params: Any, allowed: set[str], *, identifier: str = "sessionId") -> None:
        if not isinstance(params, dict):
            raise ValueError("Session history params must be an object")
        if params.keys() - allowed:
            raise ValueError("Unknown session history parameter")
        sid = params.get(identifier)
        if not isinstance(sid, str) or len(sid) > 256 or self._session_path(sid) is None:
            raise ValueError(f"{identifier} must be a valid session identifier")

    @staticmethod
    def _history_integer(params: dict[str, Any], name: str, default: int, minimum: int, maximum: int) -> int:
        value = params.get(name, default)
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
        return value

    def _existing_session(self, sid: str) -> ACPSession:
        with self._lock:
            sess = self.sessions.get(sid)
        if sess is None:
            sess = self._load_session_from_disk(sid)
        if sess is None:
            raise KeyError(f"Session not found: {sid}")
        return sess

    def _durable_history(self, sess: ACPSession) -> SQLiteHistory:
        if not isinstance(sess.history, SQLiteHistory):
            if not self._persist_session(sess):
                raise OSError("Session history persistence unavailable; source retained")
        return sess.history

    def _session_id_for_task(self, task_id: str) -> str:
        matches: set[str] = set()
        db_path = self._sessions_dir / "history.db"
        if db_path.is_file():
            # Read-only lookup cannot create storage when a resume entry is stale.
            conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
            try:
                matches.update(row[0] for row in conn.execute(
                    "SELECT DISTINCT session_id FROM history WHERE task_id = ? LIMIT 2",
                    (task_id,),
                ))
            finally:
                conn.close()
        with self._lock:
            matches.update(sess.session_id for sess in self.sessions.values()
                           if not isinstance(sess.history, SQLiteHistory) and sess.has_task(task_id))
        if not matches:
            raise KeyError(f"Task not found in ACP session history: {task_id}")
        if len(matches) != 1:
            raise ValueError(f"Task maps to multiple ACP sessions: {task_id}")
        sid = matches.pop()
        self._validate_history_params({"sessionId": sid}, {"sessionId"})
        return sid

    def _handle_session_load(self, params: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise ValueError("Session history params must be an object")
        if ("sessionId" in params) == ("taskId" in params):
            raise ValueError("Exactly one of sessionId or taskId is required")
        identifier = "taskId" if "taskId" in params else "sessionId"
        self._validate_history_params(params, {"sessionId", "taskId", "paged"}, identifier=identifier)
        paged = params.get("paged", False)
        if type(paged) is not bool:
            raise ValueError("paged must be a boolean")
        sid = self._session_id_for_task(params["taskId"]) if identifier == "taskId" else params["sessionId"]
        sess = self._existing_session(sid)
        with self._lock:
            self.sessions[sess.session_id] = sess
        # B41: approvals live only in memory, so a reloaded session must not
        # leave the client waiting on a request no turn is listening for.
        withdrawn = 0
        bridge = getattr(self, "approval_bridge", None)
        if bridge is not None and hasattr(bridge, "cancel_pending"):
            try:
                withdrawn = bridge.cancel_pending(
                    session_id=sess.session_id,
                    reason="Session reloaded; pending approval withdrawn and denied.",
                )
            except Exception as exc:
                logger.warning(
                    "Could not withdraw pending approvals for %s: %s", sess.session_id, exc
                )
        result = {
            "sessionId": sess.session_id,
            "cwd": str(sess.workspace_root),
            "model": sess.model,
            "pendingApprovalsWithdrawn": withdrawn,
        }
        if paged:
            history = self._durable_history(sess)
            return history.page(len(history), 32, result)
        result.update(historyCount=len(sess.history), history=list(sess.history))
        return result

    def _handle_session_history(self, params: dict[str, Any]) -> dict[str, Any]:
        self._validate_history_params(params, {"sessionId", "before", "limit"})
        limit = self._history_integer(params, "limit", 32, 1, 32)
        # Validate even before looking up storage, without treating bool as int.
        before = self._history_integer(params, "before", 0, 0, 2**31 - 1)
        history = self._durable_history(self._existing_session(params["sessionId"]))
        return history.page(before if "before" in params else len(history), limit)

    def _handle_session_history_content(self, params: dict[str, Any]) -> dict[str, Any]:
        self._validate_history_params(params, {"sessionId", "turnIndex", "field", "offset", "limit"})
        if "turnIndex" not in params:
            raise ValueError("turnIndex is required")
        index = self._history_integer(params, "turnIndex", 0, 0, 2**31 - 1)
        name = params.get("field")
        if not isinstance(name, str) or name not in {"prompt", "response"}:
            raise ValueError("field must be prompt or response")
        offset = self._history_integer(params, "offset", 0, 0, 2**31 - 1)
        limit = self._history_integer(params, "limit", 4096, 1, 4096)
        history = self._durable_history(self._existing_session(params["sessionId"]))
        return history.content(index, name, offset, limit)

    def _append_history(self, sess: ACPSession, record: dict[str, Any]) -> None:
        with self._persist_lock:
            sess.history.append(record)
            if not isinstance(sess.history, SQLiteHistory):
                self._persist_session(sess)

    def _handle_session_prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        if type(params.get("pagedResponse", False)) is not bool:
            raise ValueError("pagedResponse must be a boolean")
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
        if not isinstance(sess.history, SQLiteHistory):
            self._persist_session(sess)

        # Carry prior dialogue, including tool rows and reasoning (B41).
        conversation_history = build_conversation_history(sess.history)

        from harness_core.core.models import TaskSubmitRequest
        exec_mode = str(params.get("execution_mode") or "fast_path")
        task_req = TaskSubmitRequest(
            goal=prompt_text,
            project_id=compute_project_id(sess.workspace_root),
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

        paged_response = params.get("pagedResponse", False)
        streamed = False

        def stream_cb(chunk: str) -> None:
            nonlocal streamed
            if not chunk or sess.cancelled:
                return
            # Iterate slices lazily; model callbacks may contain an entire answer.
            step = 4096 if paged_response else len(chunk)
            for offset in range(0, len(chunk), step):
                if sess.cancelled:
                    return
                streamed = True
                self._emit({
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": sess.session_id,
                        "updateType": "agent_response",
                        "delta": chunk[offset:offset + step] if paged_response else chunk,
                    },
                })

        active_tool_calls: dict[str, str] = {}
        turn_tool_events: list[dict[str, Any]] = []

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
                # Durable, bounded record of the call outcome for later turns (B41).
                turn_tool_events.append({
                    "tool": tool_id,
                    "event": event_type,
                    "payload": tool["payload"],
                    "durationMs": dur_ms,
                })
                del turn_tool_events[:-30]
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
            self._append_history(sess, {
                "task_id": task_id,
                "prompt": prompt_text,
                "status": status_str,
                "response": terminal_reason,
                "duration": time.time() - start_t,
                "tool_calls": list(turn_tool_events),
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

        self._append_history(sess, {
            "task_id": task_id,
            "prompt": prompt_text,
            "status": status_str,
            "response": terminal_reason,
            "duration": time.time() - start_t,
            "tool_calls": list(turn_tool_events),
        })

        terminal_text = str(terminal_reason)
        response_chars = len(terminal_text)
        turn_index = len(sess.history) - 1
        paged_final = paged_response and not streamed and response_chars > 4096
        if paged_final:
            # The complete original is committed before these synthetic deltas.
            stream_cb(terminal_text)

        # Never fall back to a huge Ready delta if cancellation suppressed slices.
        update_notif: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": sess.session_id,
                "updateType": "status",
                "status": "Ready",
            },
        }
        if not streamed and not paged_final:
            update_notif["params"]["delta"] = terminal_text
        self._emit(update_notif)

        response_metadata: dict[str, Any] = {}
        if paged_response and response_chars > 4096:
            response_metadata = {"responseChars": response_chars, "turnIndex": turn_index}
            terminal_text = terminal_text[:4096] + f"\n[Full response: /history-turn {turn_index} response]"

        return {
            "sessionId": sess.session_id,
            "status": status_str,
            "content": [
                {
                    "type": "text",
                    "text": terminal_text,
                }
            ],
            "taskId": task_id,
            **response_metadata,
        }

    def _handle_session_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        sid = params.get("sessionId")
        with self._lock:
            sess = self.sessions.get(sid)
        if sess is None:
            raise KeyError(f"Session not found: {sid}")
        sess.cancelled = True

        # B48: wake any blocking approval wait bound to this session/task so the
        # turn stops promptly instead of blocking until the approval timeout.
        bridge = getattr(self, "approval_bridge", None)
        if bridge is not None and hasattr(bridge, "cancel_pending"):
            try:
                bridge.cancel_pending(task_id=sess.last_task_id, session_id=sid)
            except Exception as exc:
                logger.debug("ACP cancel could not withdraw approvals for %s: %s", sid, exc)

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

        def summary(sess: ACPSession) -> dict[str, Any]:
            return {
                "sessionId": sess.session_id,
                "cwd": str(sess.workspace_root),
                "model": sess.model,
                "created_at": sess.created_at,
                "turns": len(sess.history),
            }

        sess_list = [summary(sess) for sess in known.values()]
        # Inactive sessions contribute only metadata, never retained transcripts.
        if self._sessions_dir.is_dir():
            for path in sorted(self._sessions_dir.glob("*.json")):
                if path.stem not in known:
                    sess = self._load_session_from_disk(path.stem)
                    if sess is not None:
                        sess_list.append(summary(sess))
                    del sess
        return {"sessions": sess_list}

    def _handle_session_models(self, params: dict[str, Any]) -> dict[str, Any]:
        """B32: live model catalog from the configured providers, never a hardcoded list."""
        from harness_core.providers.cloud_provider import fetch_available_models

        sid = params.get("sessionId") or params.get("session_id", "default")
        with self._lock:
            sess = self.sessions.get(sid)
        router = getattr(self.orchestrator, "router", None)
        cp = getattr(router, "cloud_provider", None)

        provider_type = "antigravity"
        api_key: str | None = None
        if cp is not None:
            if getattr(cp, "provider_id", "") == "google_antigravity":
                api_key = getattr(cp, "access_token", None)
            elif hasattr(cp, "provider_type"):
                pt = cp.provider_type
                provider_type = str(getattr(pt, "value", pt)).lower()
                api_key = getattr(cp, "api_key", None)
        if provider_type not in ("antigravity", "anthropic", "openai", "gemini", "local"):
            provider_type = (os.getenv("HEX_CLOUD_PROVIDER") or "antigravity").lower()
        provider_label = getattr(cp, "provider_id", None) or provider_type

        models: list[dict[str, Any]] = []
        errors: list[str] = []
        try:
            for mid, disp in fetch_available_models(provider_type, api_key=api_key):
                models.append({
                    "id": mid, "displayName": disp,
                    "provider": provider_label, "isLocal": False,
                })
        except Exception as exc:
            errors.append(f"{provider_label}: {_redact_sensitive_text(exc)}")

        if getattr(router, "local_model_id", None):
            try:
                for mid, disp in fetch_available_models("local"):
                    models.append({
                        "id": mid, "displayName": disp,
                        "provider": "Local", "isLocal": True,
                    })
            except Exception as exc:
                errors.append(f"local: {_redact_sensitive_text(exc)}")

        if not models:
            detail = "; ".join(errors) or (
                "No cloud credentials and no local runtime models found "
                "(Ollama / LM Studio / HuggingFace cache)."
            )
            raise ValueError(f"No models discovered from the configured providers. {detail}")

        active = getattr(router, "cloud_model_id", None) or getattr(sess, "model", None) or ""
        result: dict[str, Any] = {
            "sessionId": sid,
            "activeModel": active,
            "provider": provider_label,
            "models": models,
        }
        if errors:
            result["error"] = "; ".join(errors)
        return result

    def _handle_tools_list(self, params: dict[str, Any]) -> dict[str, Any]:
        # Advertise the live authorized registry (built-ins + dynamically
        # registered MCP tools), not the static built-in catalog (B57).
        defs = self.orchestrator.gateway.live_tool_definitions()
        return {"tools": defs}

    def _ensure_tools_call_task(self) -> str:
        """Ledger task backing direct tools/call so gateway policy always applies."""
        with self._lock:
            if self._tools_call_task_id is None:
                record = self.orchestrator.ledger.submit_task({
                    "goal": "ACP direct tools/call",
                    "execution_mode": "standard",
                    "workspace_root": str(self.workspace_root),
                })
                self._tools_call_task_id = record["task_id"]
            return self._tools_call_task_id

    def _handle_tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        from harness_core.gateway import ApprovalRequiredInterrupt, PolicyBlockedError

        name = params.get("name")
        args = params.get("arguments") or {}
        if not name:
            raise ValueError("Tool call missing 'name' parameter")

        gateway = self.orchestrator.gateway
        if name not in gateway.tools:
            raise KeyError(f"Unknown tool: {name}")

        task_id = self._ensure_tools_call_task()
        with self._lock:
            self._tools_call_seq += 1
            seq = self._tools_call_seq

        # Mandatory gateway boundary (B02): policy, approval, and journaling
        # run inside invoke(); handlers are never dispatched directly here.
        try:
            inv_res = gateway.invoke(
                task_id=task_id,
                attempt_id="att_acp_tools",
                step_id="acp_tools_call",
                sequence_in_step=seq,
                tool_id=name,
                args=args,
                workspace_dir=self.workspace_root,
            )
        except PolicyBlockedError as exc:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {"ok": False, "error": f"Denied by gateway policy: {exc}"},
                            ensure_ascii=False,
                        ),
                    }
                ],
                "isError": True,
            }
        except ApprovalRequiredInterrupt as exc:
            approval_id = exc.approval_record.get("approval_id", "unknown")
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "ok": False,
                                "error": (
                                    f"Tool '{name}' requires owner approval before execution "
                                    f"(approval_id={approval_id}); no side effects were performed."
                                ),
                                "approvalId": approval_id,
                            },
                            ensure_ascii=False,
                        ),
                    }
                ],
                "isError": True,
            }

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
        ttl = params.get("leaseTtlSeconds", params.get("lease_ttl_seconds"))
        if not approval_id:
            raise ValueError("approvalId is required")

        with self._lock:
            self.pending_approvals[approval_id] = {
                "decision": decision,
                "reason": reason,
                "timestamp": time.time(),
                "status": "decided",
            }

        if self.approval_bridge is not None:
            outcome = self.approval_bridge.resolve(approval_id, decision, reason or None, ttl)
        else:
            from harness_core.client.acp_approval import _APPROVE_ONCE, _APPROVE_SCOPE, _DENY

            if decision not in _APPROVE_ONCE | _APPROVE_SCOPE | _DENY:
                raise ValueError(f"Unknown approval decision: {decision!r}")
            outcome = {"applied": False, "outcome": None, "lease_id": None, "lease_ttl_seconds": None}

        # B35: distinguish a late decision against an already closed approval
        # (expired/canceled/resolved) from a genuinely unknown id ("acknowledged").
        if outcome["applied"]:
            status = "applied"
        else:
            status = outcome.get("closed_reason") or "acknowledged"

        return {
            "approvalId": approval_id,
            "decision": decision,
            "status": status,
            "applied": outcome["applied"],
            "outcome": outcome["outcome"],
            "leaseId": outcome["lease_id"],
            "leaseTtlSeconds": outcome["lease_ttl_seconds"],
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
        if task_id != sess.last_task_id and not sess.has_task(task_id):
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
            task_ids.update(sess.task_ids())

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
            self._persist_session(sess)

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

        metadata: dict[str, Any] = {}
        try:
            result = handler(sess, args)
            if isinstance(result, dict):
                output = result["output"]
                is_error = bool(result.get("isError", False))
                if not is_error:
                    metadata = {
                        key: result[key]
                        for key in ("model", "provider")
                        if result.get(key) is not None
                    }
            else:
                output = result
                is_error = False
        except Exception as exc:
            output = f"Command failed: {exc}"
            is_error = True

        return {
            "sessionId": sid,
            "command": cmd,
            "output": output,
            "isError": is_error,
            **metadata,
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

    def _cmd_model(self, sess: ACPSession, args: str) -> dict[str, Any]:
        if not args:
            router = getattr(self.orchestrator, "router", None)
            current = getattr(router, "cloud_model_id", None) or sess.model
            provider = getattr(getattr(router, "cloud_provider", None), "provider_id", None)
            return {
                "output": f"Active model: {current} (session profile: {sess.model})",
                "model": current,
                "provider": provider,
            }
        if not hasattr(self.orchestrator, "set_active_model"):
            raise ValueError("Model switching unavailable: orchestrator has no set_active_model.")
        prov, final_id, _saved = self.orchestrator.set_active_model(args, durable=False)
        sess.model = final_id
        return {
            "output": f"Active model switched to: {final_id} (provider: {prov}; session-only)",
            "model": final_id,
            "provider": prov,
        }

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
            project_id=compute_project_id(sess.workspace_root),
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
            project_id=compute_project_id(sess.workspace_root),
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
            return (
                f"Current log compressor mode: {cur} "
                f"(usage: /compressor <{'|'.join(valid)}>; governs verbose log compression, "
                "not the live context window)"
            )
        if mode not in valid:
            raise ValueError(f"Invalid compressor mode '{mode}'. Valid: {', '.join(valid)}")
        router.compressor_mode = "off" if mode == "disabled" else mode
        return (
            f"Log compressor mode set to: {router.compressor_mode} (session-only; governs verbose "
            "log compression, not the live context window)"
        )

    def _cmd_rollback(self, sess: ACPSession, args: str) -> str:
        if not hasattr(self.orchestrator, "cancel_task"):
            raise ValueError("Rollback unavailable: orchestrator has no cancel_task.")
        tid = self._resolve_task_id(sess, args)
        res = self.orchestrator.cancel_task(tid, actor="acp")
        sess.last_task_id = None
        status = res.get("status") if isinstance(res, dict) else "CANCELED"
        return f"Task {tid} canceled (status: {status}); uncommitted CoW workspace layer rolled back."

    def _shutdown_orderly(self, reason: str = "eof", join_timeout: float = 5.0) -> dict[str, Any]:
        """B49: orderly shutdown on EOF/interrupt instead of abandoning workers.

        Withdraws pending approvals so blocked turn threads wake, cancels each
        session's in-flight task (cooperative flag + CoW rollback), revokes
        active approval leases, and joins worker threads within a bounded
        timeout. Records an explicit interrupted state.
        """
        summary: dict[str, Any] = {
            "interrupted": True,
            "reason": reason,
            "canceled_tasks": [],
            "leases_revoked": 0,
        }
        if self._shutting_down:
            return summary
        self._shutting_down = True

        with self._lock:
            sessions = list(self.sessions.values())
        bridge = getattr(self, "approval_bridge", None)
        orch = self._orchestrator

        for sess in sessions:
            sess.cancelled = True
            tid = sess.last_task_id
            if bridge is not None and hasattr(bridge, "cancel_pending"):
                try:
                    bridge.cancel_pending(task_id=tid, session_id=sess.session_id)
                except Exception as exc:
                    logger.debug("ACP shutdown approval withdraw failed for %s: %s", sess.session_id, exc)
            if tid and orch is not None and hasattr(orch, "cancel_task"):
                try:
                    from harness_core.core.models import TaskState

                    ledger = getattr(orch, "ledger", None)
                    task = ledger.get_task(tid) if ledger is not None else None
                    terminal = task is not None and TaskState(task["status"]).is_terminal
                    if not terminal:
                        orch.cancel_task(tid, actor="acp-shutdown")
                        summary["canceled_tasks"].append(tid)
                except Exception as exc:
                    logger.debug("ACP shutdown could not cancel task %s: %s", tid, exc)
            try:
                self._persist_session(sess)
            except Exception:
                pass

        gateway = getattr(orch, "gateway", None)
        leases = getattr(gateway, "approval_lease_manager", None)
        if leases is not None and hasattr(leases, "list_active_leases"):
            try:
                for lease in leases.list_active_leases():
                    if leases.revoke_lease(lease.lease_id):
                        summary["leases_revoked"] += 1
            except Exception as exc:
                logger.debug("ACP shutdown lease cleanup failed: %s", exc)

        for t in list(self._turn_threads):
            try:
                t.join(timeout=join_timeout)
            except Exception:
                pass
        return summary

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

        shutdown_reason = "eof"
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
                    worker = threading.Thread(target=_respond_async, args=(req,), daemon=True)
                    self._turn_threads.append(worker)
                    worker.start()
                else:
                    resp = self.handle_request(req)
                    self._emit(resp)
                    if req.get("method") == "session/cancel":
                        with self._lock:
                            sess = self.sessions.get((req.get("params") or {}).get("sessionId"))
                        if sess is not None:
                            sess.cancel_ack.set()
            except (KeyboardInterrupt, BrokenPipeError):
                shutdown_reason = "interrupt"
                break
            except Exception as exc:
                err_resp = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {exc}"},
                }
                self._emit(err_resp)

        # B49: EOF or interrupt — quiesce workers, cancel/roll back in-flight
        # turns, withdraw approvals, and clean leases before exiting.
        self._shutdown_orderly(shutdown_reason)
