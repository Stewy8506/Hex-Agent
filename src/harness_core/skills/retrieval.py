"""Cross-Session FTS5 + Active Graphify AST Hybrid Retrieval Engine.

Implements HybridLearningRetriever according to:
- PRD Section 7.3 & FR-039..FR-039e
- docs/research/HERMES_LEARNING_AND_SOUL_ANALYSIS.md (Part 4 Pillar 3: Hybrid Cross-Session Retrieval)
- Merges SQLite FTS5 BM25 lexical full-text ranking across skills, reflections/lessons,
  and memories with Active Graphify AST 1-hop caller/callee traversal.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Optional

from harness_core.storage.db import LedgerStore
from harness_core.graphify import ActiveGraphifyAdapter
from harness_core.capabilities.search import sanitize_fts5_query
from harness_core.cognition.skillspector import parse_skill_frontmatter
from harness_core.cognition.soul import get_hex_home

logger = logging.getLogger(__name__)


@dataclass
class RetrievalMatch:
    """Structured result returned by HybridLearningRetriever."""

    item_type: str  # "skill" | "lesson" | "memory"
    identifier: str
    title: str
    content: str
    score: float
    match_type: str  # "lexical_fts5" | "ast_1hop" | "hybrid"
    source_file: Optional[str] = None
    associated_symbols: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_type": self.item_type,
            "identifier": self.identifier,
            "title": self.title,
            "content": self.content,
            "score": self.score,
            "match_type": self.match_type,
            "source_file": self.source_file,
            "associated_symbols": list(self.associated_symbols),
        }


class HybridLearningRetriever:
    """Cross-session hybrid retrieval engine combining SQLite FTS5 lexical ranking

    with Active Graphify AST 1-hop caller/callee traversal.
    """

    def __init__(
        self,
        ledger: Optional[LedgerStore] = None,
        graphify: Optional[ActiveGraphifyAdapter] = None,
        workspace_root: Optional[Path | str] = None,
        global_home: Optional[Path | str] = None,
    ) -> None:
        self.workspace_root = (
            Path(workspace_root).resolve()
            if workspace_root
            else Path.cwd().resolve()
        )
        self.global_home = (
            Path(global_home).resolve()
            if global_home
            else get_hex_home().resolve()
        )
        self.ledger = ledger
        self.graphify = graphify or ActiveGraphifyAdapter(workspace_root=self.workspace_root)

        self._lock = threading.RLock()
        self._fts_conn: Optional[sqlite3.Connection] = None
        self._items_by_id: dict[str, dict[str, Any]] = {}
        self._initialized = False

    def _ensure_fts(self) -> sqlite3.Connection:
        with self._lock:
            if self._fts_conn is None:
                self._fts_conn = sqlite3.connect(":memory:", check_same_thread=False)
                try:
                    self._fts_conn.execute(
                        """
                        CREATE VIRTUAL TABLE retrieval_fts USING fts5(
                            item_id UNINDEXED,
                            item_type UNINDEXED,
                            title,
                            content,
                            associated_symbols,
                            tokenize = 'unicode61 remove_diacritics 2'
                        );
                        """
                    )
                except sqlite3.OperationalError:
                    self._fts_conn.execute(
                        """
                        CREATE VIRTUAL TABLE retrieval_fts USING fts5(
                            item_id UNINDEXED,
                            item_type UNINDEXED,
                            title,
                            content,
                            associated_symbols
                        );
                        """
                    )
                self._fts_conn.commit()
            return self._fts_conn

    def _collect_skills(self) -> list[dict[str, Any]]:
        """Collect all skills from workspace and global directories, plus ledger if present."""
        skills: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        search_dirs = [
            self.workspace_root / ".hex" / "skills",
            self.workspace_root / "skills",
            self.global_home / "skills",
        ]

        for s_dir in search_dirs:
            if not s_dir.is_dir():
                continue
            for skill_path in s_dir.rglob("SKILL.md"):
                try:
                    raw_text = skill_path.read_text(encoding="utf-8", errors="replace")
                    fm, body = parse_skill_frontmatter(raw_text)
                    name = str(fm.get("name") or skill_path.parent.name)
                    if name in seen_ids:
                        continue
                    seen_ids.add(name)

                    desc = str(fm.get("description") or fm.get("purpose") or "")
                    syms = list(fm.get("associated_symbols") or [])
                    verify = fm.get("verify")
                    if isinstance(verify, dict):
                        inv = verify.get("ast_invariant")
                        if inv:
                            syms.append(str(inv))
                    elif isinstance(verify, str):
                        syms.append(verify)

                    skills.append(
                        {
                            "item_id": f"skill_{name}",
                            "item_type": "skill",
                            "identifier": name,
                            "title": name,
                            "content": f"{desc}\n{body}".strip(),
                            "source_file": str(skill_path.relative_to(self.workspace_root))
                            if str(skill_path).startswith(str(self.workspace_root))
                            else str(skill_path),
                            "associated_symbols": syms,
                        }
                    )
                except Exception as exc:
                    logger.debug("Failed to read skill at %s: %s", skill_path, exc)

        # Ledger skills check
        if self.ledger is not None:
            try:
                with getattr(self.ledger, "_lock", threading.Lock()):
                    if hasattr(self.ledger, "conn") and self.ledger.conn:
                        cursor = self.ledger.conn.execute("SELECT * FROM skills")
                        for row in cursor.fetchall():
                            row_dict = (
                                self.ledger._row_to_dict(row)
                                if hasattr(self.ledger, "_row_to_dict")
                                else dict(row)
                            )
                            sid = row_dict.get("skill_id") or row_dict.get("name")
                            if sid and sid not in seen_ids:
                                seen_ids.add(sid)
                                skills.append(
                                    {
                                        "item_id": f"skill_{sid}",
                                        "item_type": "skill",
                                        "identifier": sid,
                                        "title": sid,
                                        "content": str(row_dict.get("manifest_json") or row_dict.get("description") or ""),
                                        "source_file": row_dict.get("path"),
                                        "associated_symbols": [],
                                    }
                                )
            except Exception as exc:
                logger.debug("Failed querying ledger skills: %s", exc)

        return skills

    def _collect_lessons(self) -> list[dict[str, Any]]:
        """Collect lessons from .hex/reflections/LESSONS.md and reflections_meta.json."""
        lessons: list[dict[str, Any]] = []
        lessons_md = self.workspace_root / ".hex" / "reflections" / "LESSONS.md"
        if lessons_md.is_file():
            try:
                raw_text = lessons_md.read_text(encoding="utf-8", errors="replace")
                sections = re.split(r"(?=^###\s+Lesson:)", raw_text, flags=re.MULTILINE)
                for sec in sections:
                    clean = sec.strip()
                    if not clean.startswith("### Lesson:"):
                        continue
                    first_line = clean.splitlines()[0]
                    m = re.search(r"###\s+Lesson:\s*([^\s(]+)", first_line)
                    lesson_id = m.group(1).strip() if m else f"lesson_{hashlib.md5(clean.encode()).hexdigest()[:8]}"

                    # Extract linked nodes
                    nodes: list[str] = []
                    m_nodes = re.search(r"-\s+\*\*Linked Nodes\*\*:\s*(.+)", clean)
                    if m_nodes:
                        raw_nodes = m_nodes.group(1).strip()
                        if raw_nodes.lower() != "none":
                            nodes = [n.strip() for n in raw_nodes.split(",") if n.strip()]

                    # Extract symptom / title
                    symptom = ""
                    m_sym = re.search(r"-\s+\*\*Symptom\*\*:\s*(.+)", clean)
                    if m_sym:
                        symptom = m_sym.group(1).strip()

                    lessons.append(
                        {
                            "item_id": f"lesson_{lesson_id}",
                            "item_type": "lesson",
                            "identifier": lesson_id,
                            "title": symptom or lesson_id,
                            "content": clean,
                            "source_file": str(lessons_md.relative_to(self.workspace_root)),
                            "associated_symbols": nodes,
                        }
                    )
            except Exception as exc:
                logger.debug("Failed parsing LESSONS.md: %s", exc)

        return lessons

    def _collect_memories(self) -> list[dict[str, Any]]:
        """Collect active memories and negative guardrails."""
        memories: list[dict[str, Any]] = []

        stores = [
            (self.workspace_root / ".hex" / "memories" / "MEMORY.md", "memory"),
            (self.workspace_root / ".hex" / "MEMORY.md", "memory"),
            (self.workspace_root / ".hex" / "memories" / "GUARDRAILS.md", "guardrails"),
            (self.workspace_root / ".hex" / "GUARDRAILS.md", "guardrails"),
            (self.global_home / "USER.md", "user"),
        ]

        seen_files: set[Path] = set()
        for fpath, scope in stores:
            if not fpath.is_file() or fpath in seen_files:
                continue
            seen_files.add(fpath)
            try:
                content = fpath.read_text(encoding="utf-8", errors="replace")
                # Split into bullet entries or sections
                for idx, line in enumerate(content.splitlines()):
                    clean = line.strip()
                    if clean.startswith("- ") or clean.startswith("* "):
                        bullet = clean[2:].strip()
                        if not bullet or len(bullet) < 5:
                            continue
                        mem_id = f"mem_{scope}_{hashlib.md5(bullet.encode()).hexdigest()[:8]}"
                        tags = re.findall(r"\[\[([^\]]+)\]\]", bullet)
                        memories.append(
                            {
                                "item_id": mem_id,
                                "item_type": "memory",
                                "identifier": mem_id,
                                "title": f"[{scope.upper()}] {bullet[:50]}",
                                "content": bullet,
                                "source_file": str(fpath.relative_to(self.workspace_root))
                                if str(fpath).startswith(str(self.workspace_root))
                                else str(fpath),
                                "associated_symbols": tags,
                            }
                        )
            except Exception as exc:
                logger.debug("Failed reading memory file %s: %s", fpath, exc)

        # Archived memories in SQLite if present
        archive_db = self.workspace_root / ".hex" / "memories" / "archive.db"
        if archive_db.is_file():
            try:
                conn = sqlite3.connect(str(archive_db), check_same_thread=False)
                try:
                    cursor = conn.execute("SELECT memory_id, target, content, tags FROM archived_memories")
                    for row in cursor.fetchall():
                        mid, target, text, tags = row
                        tag_list = [t.strip() for t in str(tags or "").split(",") if t.strip()]
                        memories.append(
                            {
                                "item_id": f"archived_{mid}",
                                "item_type": "memory",
                                "identifier": mid,
                                "title": f"[ARCHIVED:{target}] {str(text)[:50]}",
                                "content": str(text),
                                "source_file": str(archive_db.relative_to(self.workspace_root)),
                                "associated_symbols": tag_list,
                            }
                        )
                finally:
                    conn.close()
            except Exception as exc:
                logger.debug("Failed reading archived memories: %s", exc)

        return memories

    def refresh_index(self) -> int:
        """Re-scan workspace skills, lessons, and memories and rebuild the FTS5 index."""
        conn = self._ensure_fts()
        with self._lock:
            conn.execute("DELETE FROM retrieval_fts")
            self._items_by_id.clear()

            all_items = self._collect_skills() + self._collect_lessons() + self._collect_memories()

            for it in all_items:
                iid = it["item_id"]
                self._items_by_id[iid] = it
                conn.execute(
                    """
                    INSERT INTO retrieval_fts (item_id, item_type, title, content, associated_symbols)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        iid,
                        it["item_type"],
                        it["title"],
                        it["content"],
                        " ".join(it["associated_symbols"]),
                    ),
                )
            conn.commit()
            self._initialized = True
            return len(all_items)

    def _ensure_initialized(self) -> None:
        if not self._initialized:
            self.refresh_index()

    def _resolve_ast_neighborhood(
        self,
        target_file_or_symbol: Optional[str] = None,
        query_text: str = "",
    ) -> tuple[set[str], set[str], set[str]]:
        """Identify (direct_targets, callers_1hop, callees_1hop) using ActiveGraphifyAdapter."""
        direct_targets: set[str] = set()
        callers: set[str] = set()
        callees: set[str] = set()

        if self.graphify is None:
            return direct_targets, callers, callees

        # Build or refresh graphify if not built
        try:
            if not getattr(self.graphify, "symbols_by_name", None):
                self.graphify.build_or_refresh(self.workspace_root)
        except Exception:
            pass

        candidate_symbols: set[str] = set()
        if target_file_or_symbol:
            candidate_symbols.add(target_file_or_symbol.strip())

        # Extract tokens from query_text that match known symbol names or file stems
        words = re.findall(r"\b[A-Za-z_]\w*\b", query_text)
        for w in words:
            if w in self.graphify.symbols_by_name:
                candidate_symbols.add(w)

        for sym in candidate_symbols:
            # Check if it's a file path
            if "/" in sym or sym.endswith((".py", ".ts", ".js", ".rs", ".go")):
                direct_targets.add(sym)
                syms_in_file = self.graphify.symbols_by_file.get(sym, [])
                for s in syms_in_file:
                    direct_targets.add(s.name)
                    direct_targets.add(s.qualified_name)
                    callers.update(self.graphify.callers_of.get(s.name, set()))
                    callers.update(self.graphify.callers_of.get(s.qualified_name, set()))
                    callees.update(s.calls)
                continue

            # It is a symbol name
            direct_targets.add(sym)
            q_res = self.graphify.query_symbol(sym)
            for d in q_res.get("definitions", []):
                qn = d.get("qualified_name")
                if qn:
                    direct_targets.add(qn)
                f = d.get("file")
                if f:
                    direct_targets.add(f)

            for c in q_res.get("hop1_callers", []):
                qn = c.get("qualified_name")
                if qn:
                    callers.add(qn)
                    callers.add(qn.split(".")[-1])

            # Also callees from SymbolDef.calls
            defs = self.graphify.symbols_by_name.get(sym, [])
            for df in defs:
                callees.update(df.calls)

        return direct_targets, callers, callees

    def query(
        self,
        query_text: str,
        target_file_or_symbol: Optional[str] = None,
        limit: int = 10,
    ) -> list[RetrievalMatch]:
        """Query learned skills, lessons, and memories using hybrid BM25 + AST traversal."""
        self._ensure_initialized()

        lexical_scores: dict[str, float] = {}
        sanitized = sanitize_fts5_query(query_text)

        # Step 1: Lexical FTS5 Retrieval with BM25 ranking
        if sanitized.strip():
            conn = self._ensure_fts()
            sql = """
                SELECT item_id, bm25(retrieval_fts) AS rank
                FROM retrieval_fts
                WHERE retrieval_fts MATCH ?
                ORDER BY rank ASC
                LIMIT 50
            """
            try:
                with self._lock:
                    rows = conn.execute(sql, (sanitized,)).fetchall()
                    for item_id, rank in rows:
                        # In SQLite FTS5, bm25 rank is negative where more negative = more relevant
                        score = max(0.1, -float(rank))
                        lexical_scores[item_id] = score
            except sqlite3.OperationalError as exc:
                logger.debug("FTS5 match failed on query '%s': %s", sanitized, exc)

        # Step 2: AST 1-Hop Traversal
        direct_targets, callers, callees = self._resolve_ast_neighborhood(
            target_file_or_symbol=target_file_or_symbol,
            query_text=query_text,
        )

        ast_matches: dict[str, tuple[float, list[str]]] = {}
        for item_id, it in self._items_by_id.items():
            matched_syms: list[str] = []
            proximity_boost = 0.0

            associated = set(it["associated_symbols"])
            content_lower = it["content"].lower()
            title_lower = it["title"].lower()

            # Check direct targets
            for t in direct_targets:
                if t in associated or re.search(rf"\b{re.escape(t)}\b", it["content"]):
                    proximity_boost = max(proximity_boost, 2.5)
                    matched_syms.append(t)

            # Check callers and callees (1-hop)
            for c in callers:
                if c in associated or re.search(rf"\b{re.escape(c)}\b", it["content"]):
                    proximity_boost = max(proximity_boost, 1.8)
                    matched_syms.append(f"caller:{c}")

            for c in callees:
                if c in associated or re.search(rf"\b{re.escape(c)}\b", it["content"]):
                    proximity_boost = max(proximity_boost, 1.8)
                    matched_syms.append(f"callee:{c}")

            if proximity_boost > 0.0:
                ast_matches[item_id] = (proximity_boost, matched_syms)

        # Step 3: Score Fusion & Re-Ranking
        all_candidate_ids = set(lexical_scores.keys()) | set(ast_matches.keys())
        ranked_matches: list[RetrievalMatch] = []

        for item_id in all_candidate_ids:
            it = self._items_by_id.get(item_id)
            if not it:
                continue

            has_lexical = item_id in lexical_scores
            has_ast = item_id in ast_matches

            if has_lexical and has_ast:
                match_type = "hybrid"
                lex_score = lexical_scores[item_id]
                ast_boost, ast_syms = ast_matches[item_id]
                total_score = round(lex_score * 1.2 + ast_boost, 4)
                syms = list(dict.fromkeys(it["associated_symbols"] + ast_syms))
            elif has_ast:
                match_type = "ast_1hop"
                ast_boost, ast_syms = ast_matches[item_id]
                total_score = round(ast_boost, 4)
                syms = list(dict.fromkeys(it["associated_symbols"] + ast_syms))
            else:
                match_type = "lexical_fts5"
                total_score = round(lexical_scores[item_id], 4)
                syms = it["associated_symbols"]

            ranked_matches.append(
                RetrievalMatch(
                    item_type=it["item_type"],
                    identifier=it["identifier"],
                    title=it["title"],
                    content=it["content"],
                    score=total_score,
                    match_type=match_type,
                    source_file=it["source_file"],
                    associated_symbols=syms,
                )
            )

        ranked_matches.sort(key=lambda m: m.score, reverse=True)
        return ranked_matches[:limit]

    def find_skills_for_task(
        self,
        goal: str,
        target_files: Optional[list[str]] = None,
        limit: int = 5,
    ) -> list[RetrievalMatch]:
        """Find relevant reusable skills for a task goal and target files."""
        primary_target = target_files[0] if target_files else None
        matches = self.query(
            query_text=goal,
            target_file_or_symbol=primary_target,
            limit=limit * 3,
        )
        skills = [m for m in matches if m.item_type == "skill"]
        return skills[:limit]
