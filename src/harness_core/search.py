"""SQLite FTS5 Full-Text Task, Transcript, and Execution Log Search Engine.

Implements full-text search across:
- Task goals & descriptions
- Tool call inputs, arguments, and return values
- Execution events and error logs

Features:
- SQLite FTS5 virtual table (`task_search_fts`) with unicode61 diacritic removal
- BM25 relevance ranking and best-match snippet generation
- Query sanitization against FTS5 special character syntax injection
- Automatic synchronization with `LedgerStore`
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# FTS5 characters that trigger syntax errors outside quoted phrases
_FTS5_SPECIAL_CHARS = '+{}():"^@/#&|~[]<>,;!?$=\\\''
_FTS5_SPECIAL_RE = re.compile(f"[{re.escape(_FTS5_SPECIAL_CHARS)}]")


def sanitize_fts5_query(query: str) -> str:
    """Sanitize and normalize query strings for SQLite FTS5 MATCH expressions.

    - Preserves balanced double-quoted phrases.
    - Strips unmatched quotes and dangerous punctuation.
    - Preserves trailing prefix wildcards (e.g. 'auth*') while collapsing leading/repeated wildcards.
    - Strips dangling boolean operators (AND, OR, NOT) that cause syntax errors.
    - Wraps hyphenated and dotted tokens in quotes to preserve phrase semantics.
    """
    if not query:
        return ""

    query = query.strip()
    if not query:
        return ""

    # Step 1: Preserve balanced double-quoted phrases
    quoted_parts: list[str] = []
    pieces: list[str] = []
    i = 0
    while i < len(query):
        ch = query[i]
        if ch != '"':
            pieces.append(ch)
            i += 1
            continue
        end = query.find('"', i + 1)
        if end == -1:
            # Unmatched quote: convert to space
            pieces.append(" ")
            i += 1
            continue
        quoted_phrase = query[i : end + 1]
        quoted_parts.append(quoted_phrase)
        pieces.append(f"\x00Q{len(quoted_parts) - 1}\x00")
        i = end + 1

    sanitized = "".join(pieces)

    # Step 2: Strip FTS5 special punctuation
    sanitized = _FTS5_SPECIAL_RE.sub(" ", sanitized)
    sanitized = sanitized.replace("%", " ")

    # Step 3: Collapse repeated '*' and strip leading '*'
    sanitized = re.sub(r"\*+", "*", sanitized)
    sanitized = re.sub(r"(^|\s)\*", r"\1", sanitized)

    # Step 4: Clean standalone / dangling boolean operators
    changed = True
    while changed:
        orig = sanitized.strip()
        sanitized = re.sub(r"(?i)^(AND|OR|NOT)\b\s*", "", sanitized.strip())
        sanitized = re.sub(r"(?i)\s+\b(AND|OR|NOT)$", "", sanitized.strip())
        sanitized = re.sub(r"(?i)^(AND|OR|NOT)$", "", sanitized.strip())
        changed = sanitized.strip() != orig

    # Step 5: Wrap unquoted hyphenated or dotted tokens to prevent tokenizer splitting
    sanitized = re.sub(r"\b(\w+(?:[._-]\w+)+)\b", r'"\1"', sanitized)

    # Step 6: Restore quoted phrases
    for idx, qp in enumerate(quoted_parts):
        sanitized = sanitized.replace(f"\x00Q{idx}\x00", qp)

    return re.sub(r"\s+", " ", sanitized).strip()


@dataclass
class TaskSearchResult:
    """Represents a matched task record with relevance score and snippet."""

    task_id: str
    project_id: str
    goal: str
    status: str
    snippet: str
    score: float
    matched_field: str  # "goal" | "tool_calls" | "execution_logs"

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "project_id": self.project_id,
            "goal": self.goal,
            "status": self.status,
            "snippet": self.snippet,
            "score": self.score,
            "matched_field": self.matched_field,
        }


class TaskSearchIndex:
    """SQLite FTS5 virtual table indexer and BM25 ranker for HEX tasks."""

    def __init__(self, db_path: Path | str | sqlite3.Connection) -> None:
        self._lock = threading.RLock()
        if isinstance(db_path, sqlite3.Connection):
            self._conn = db_path
            self._owns_conn = False
        else:
            db_str = str(db_path).strip()
            if db_str != ":memory:":
                Path(db_str).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(db_str, check_same_thread=False)
            self._owns_conn = True

        self._init_fts()

    def _init_fts(self) -> None:
        with self._lock:
            try:
                self._conn.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS task_search_fts USING fts5(
                        task_id UNINDEXED,
                        project_id UNINDEXED,
                        status UNINDEXED,
                        goal,
                        tool_calls,
                        execution_logs,
                        tokenize = 'unicode61 remove_diacritics 2'
                    );
                    """
                )
                self._conn.commit()
            except sqlite3.OperationalError:
                # Fallback to default tokenizer if unicode61 options are unsupported
                self._conn.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS task_search_fts USING fts5(
                        task_id UNINDEXED,
                        project_id UNINDEXED,
                        status UNINDEXED,
                        goal,
                        tool_calls,
                        execution_logs
                    );
                    """
                )
                self._conn.commit()

    def close(self) -> None:
        """Close connection if owned."""
        if self._owns_conn and self._conn:
            try:
                self._conn.close()
            except Exception:
                pass

    def index_task(
        self,
        task_id: str,
        project_id: str,
        goal: str,
        status: str,
        tool_calls_text: str = "",
        logs_text: str = "",
    ) -> None:
        """Index or update a task document in the FTS5 virtual table."""
        with self._lock:
            self._conn.execute("DELETE FROM task_search_fts WHERE task_id = ?", (task_id,))
            self._conn.execute(
                """
                INSERT INTO task_search_fts (
                    task_id, project_id, status, goal, tool_calls, execution_logs
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (task_id, project_id, status, goal, tool_calls_text, logs_text),
            )
            self._conn.commit()

    def update_task_status(self, task_id: str, status: str) -> None:
        """Update the unindexed status column for a task."""
        with self._lock:
            self._conn.execute(
                "UPDATE task_search_fts SET status = ? WHERE task_id = ?",
                (status, task_id),
            )
            self._conn.commit()

    def append_tool_call(
        self,
        task_id: str,
        tool_name: str,
        args_text: str,
        result_text: str,
    ) -> None:
        """Append a tool call transcript to the task document."""
        entry = f"[{tool_name}] args: {args_text} result: {result_text}"
        with self._lock:
            row = self._conn.execute(
                "SELECT tool_calls, project_id, status, goal, execution_logs FROM task_search_fts WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row:
                curr_tc = row[0] or ""
                updated = (curr_tc + "\n" + entry).strip() if curr_tc else entry
                self._conn.execute(
                    "UPDATE task_search_fts SET tool_calls = ? WHERE task_id = ?",
                    (updated, task_id),
                )
            else:
                self._conn.execute(
                    """
                    INSERT INTO task_search_fts (
                        task_id, project_id, status, goal, tool_calls, execution_logs
                    ) VALUES (?, 'default', 'UNKNOWN', '', ?, '')
                    """,
                    (task_id, entry),
                )
            self._conn.commit()

    def search(
        self,
        query: str,
        project_id: str | None = None,
        limit: int = 20,
    ) -> list[TaskSearchResult]:
        """Perform ranked BM25 search across indexed tasks."""
        sanitized = sanitize_fts5_query(query)
        if not sanitized.strip():
            return []

        sql = """
            SELECT
                task_id,
                project_id,
                status,
                goal,
                bm25(task_search_fts) AS rank,
                snippet(task_search_fts, -1, '<b>', '</b>', '...', 16) AS snip,
                snippet(task_search_fts, 3, '<b>', '</b>', '...', 16) AS snip_goal,
                snippet(task_search_fts, 4, '<b>', '</b>', '...', 16) AS snip_tool_calls,
                snippet(task_search_fts, 5, '<b>', '</b>', '...', 16) AS snip_logs
            FROM task_search_fts
            WHERE task_search_fts MATCH ?
        """
        params: list[Any] = [sanitized]
        if project_id:
            sql += " AND project_id = ?"
            params.append(project_id)
        sql += " ORDER BY rank ASC LIMIT ?"
        params.append(limit)

        with self._lock:
            try:
                cursor = self._conn.execute(sql, params)
                rows = cursor.fetchall()
            except sqlite3.OperationalError as exc:
                logger.warning("FTS5 query '%s' failed: %s", sanitized, exc)
                return []

        results: list[TaskSearchResult] = []
        for row in rows:
            t_id = row[0]
            p_id = row[1]
            st = row[2]
            gl = row[3]
            rank = row[4]
            snip = row[5] or ""
            snip_g = row[6] or ""
            snip_tc = row[7] or ""
            snip_l = row[8] or ""

            # Determine which field produced the best match
            if "<b>" in snip_g:
                matched_field = "goal"
            elif "<b>" in snip_tc:
                matched_field = "tool_calls"
            elif "<b>" in snip_l:
                matched_field = "execution_logs"
            else:
                matched_field = "goal"

            score = round(-float(rank), 6) if rank is not None else 0.0
            results.append(
                TaskSearchResult(
                    task_id=t_id,
                    project_id=p_id,
                    goal=gl,
                    status=st,
                    snippet=snip,
                    score=score,
                    matched_field=matched_field,
                )
            )

        return results

    def reindex_all(self, ledger: Any) -> int:
        """Rebuild task_search_fts from historical ledger tasks, tool calls, and event logs."""
        with self._lock:
            self._conn.execute("DELETE FROM task_search_fts")
            tasks = ledger.list_tasks(limit=100000)
            count = 0
            for task in tasks:
                task_id = task["task_id"]
                goal = task.get("goal") or ""
                status = task.get("status") or "UNKNOWN"
                project_id = task.get("project_id") or "default"

                # Tool calls
                tc_parts: list[str] = []
                try:
                    tool_calls = ledger.list_tool_calls(task_id)
                    for tc in tool_calls:
                        t_name = tc.get("tool_id") or tc.get("tool_name") or ""
                        t_args = tc.get("arguments_redacted_json") or tc.get("args_json") or ""
                        t_res = tc.get("result_json") or ""
                        tc_parts.append(f"[{t_name}] args: {t_args} result: {t_res}")
                except Exception:
                    pass
                tool_calls_text = "\n".join(tc_parts)

                # Events / logs
                ev_parts: list[str] = []
                try:
                    events = ledger.list_events(task_id)
                    for ev in events:
                        e_type = ev.get("event_type") or ""
                        e_payload = ev.get("payload_json") or ""
                        ev_parts.append(f"[{e_type}] {e_payload}")
                except Exception:
                    pass
                logs_text = "\n".join(ev_parts)

                self._conn.execute(
                    """
                    INSERT INTO task_search_fts (
                        task_id, project_id, status, goal, tool_calls, execution_logs
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (task_id, project_id, status, goal, tool_calls_text, logs_text),
                )
                count += 1

            self._conn.commit()
            return count
