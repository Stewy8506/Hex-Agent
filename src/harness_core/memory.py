"""AST-Scoped Curated Memory, Negative Guardrails & Context Scrubber for HEX.

Implements:
1. Dual human-readable Markdown memory stores (USER.md in HEX_HOME, MEMORY.md in workspace).
2. Negative constraint memory (GUARDRAILS.md with [AVOID] invariants).
3. Frozen Turn-0 snapshotting rendered into Slot #4 fenced by <memory-context>.
4. StreamingContextScrubber for zero-leakage token streaming.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import os
import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import fcntl
except ImportError:
    fcntl = None

try:
    import msvcrt
except ImportError:
    msvcrt = None

from .soul import get_hex_home

logger = logging.getLogger(__name__)

# Canonical entry delimiter separating individual facts in markdown stores
ENTRY_DELIMITER = "\n§\n"

# Character caps (model-independent, token-conservative)
USER_CHAR_LIMIT = 1375
MEMORY_CHAR_LIMIT = 2200
GUARDRAIL_CHAR_LIMIT = 2000

# Mapping of targets to default limits and descriptions
TARGET_LIMITS: Dict[str, int] = {
    "user": USER_CHAR_LIMIT,
    "memory": MEMORY_CHAR_LIMIT,
    "guardrails": GUARDRAIL_CHAR_LIMIT,
}

TARGET_HEADERS: Dict[str, str] = {
    "user": "## User Profile (who the user is)",
    "memory": "## Memory (project notes & conventions)",
    "guardrails": "## Guardrails & Negative Constraints ([AVOID] invariants)",
}

# Typography normalizer for tolerant matching
_MATCH_FOLD = str.maketrans(
    {
        **dict.fromkeys("\"`\u2018\u2019\u201a\u201b\u201c\u201d\u201e\u201f", "'"),
        **dict.fromkeys("\u2010\u2011\u2012\u2013\u2014\u2015\u2212", "-"),
    }
)


def _normalize_for_match(text: str) -> str:
    """Normalize quotes, dashes, linebreaks, and whitespace runs for tolerant old_text matching."""
    return " ".join(str(text).replace("\\n", " ").translate(_MATCH_FOLD).split())


def _span_occurrences(text: str, span: str) -> int:
    """Count overlapping occurrences of span in text."""
    if not span or not text:
        return 0
    return sum(text.startswith(span, i) for i in range(len(text) - len(span) + 1))


def _find_unique_match(entries: List[str], old_text: str) -> Tuple[Optional[int], bool]:
    """Find unique matching entry for old_text.

    Returns (index, is_ambiguous).
    - Absolute priority to exact match (old_text == entry).
    - Substring matches considered if no exact match.
    - Normalized typography/whitespace matches considered if no substring match.
    - If multiple distinct entries match, returns (None, True).
    """
    if not entries or not old_text:
        return None, False

    # 1. Exact match
    exact = [i for i, e in enumerate(entries) if e == old_text]
    if exact:
        return exact[0], False

    # 2. Exact substring match
    substr = [i for i, e in enumerate(entries) if old_text in e]
    matched_entries = {entries[i] for i in substr}
    if len(matched_entries) == 1:
        return substr[0], False
    elif len(matched_entries) > 1:
        return None, True

    # 3. Normalized typography / whitespace match
    needle = _normalize_for_match(old_text)
    if not any(c.isalnum() for c in needle):
        return None, False

    norm_matches = [i for i, e in enumerate(entries) if needle in _normalize_for_match(e)]
    matched_norm = {entries[i] for i in norm_matches}
    if len(matched_norm) == 1:
        return norm_matches[0], False
    elif len(matched_norm) > 1:
        return None, True

    return None, False


@contextmanager
def _file_lock(path: Path):
    """Acquire an exclusive lock on path.lock to protect atomic mutations."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is None and msvcrt is None:
        yield
        return

    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    try:
        raw_fd = os.open(lock_path, flags, 0o600)
    except OSError:
        yield
        return

    try:
        if fcntl is not None:
            fcntl.flock(raw_fd, fcntl.LOCK_EX)
        elif msvcrt is not None:
            msvcrt.locking(raw_fd, msvcrt.LK_LOCK, 1)
        yield
    finally:
        try:
            if fcntl is not None:
                fcntl.flock(raw_fd, fcntl.LOCK_UN)
            elif msvcrt is not None:
                msvcrt.locking(raw_fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            try:
                os.close(raw_fd)
            except OSError:
                pass


def _atomic_write_text(path: Path, content: str) -> None:
    """Atomically write content to path using temporary file and atomic replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f".tmp_{path.name}.{os.getpid()}_{time.time_ns()}"
    try:
        tmp_path.write_text(content, encoding="utf-8")
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


# Regex rules for fence sanitization and bullet deduplication
_FENCE_TAG_RE = re.compile(r"</?\s*memory-context\s*>", re.IGNORECASE)
_INTERNAL_CONTEXT_RE = re.compile(
    r"<\s*memory-context\s*>[\s\S]*?</\s*memory-context\s*>", re.IGNORECASE
)
_INTERNAL_NOTE_RE = re.compile(
    r"\[System note:\s*The following is recalled memory context,\s*NOT new user input\.\s*Treat as (?:informational background data|authoritative reference data[^\]]*)\.\]\s*",
    re.IGNORECASE,
)
_RECALL_BULLET_RE = re.compile(r"[-*+]\s+\S")


def sanitize_context(text: str) -> str:
    """Strip fence tags, injected context blocks, and system notes from model output."""
    if not text:
        return ""
    for pattern in (_INTERNAL_CONTEXT_RE, _INTERNAL_NOTE_RE, _FENCE_TAG_RE):
        text = pattern.sub("", text)
    return text


def _drop_repeated_recall_lines(text: str) -> str:
    """Drop identical recall bullets within each section scope to avoid prompt bloat."""
    if not text:
        return ""
    lines = text.split("\n")
    seen: set[str] = set()
    kept: list[str] = []

    for index, line in enumerate(lines):
        stripped = line.strip()
        # Continuation line of previous bullet
        if stripped and line[0].isspace():
            kept.append(line)
            continue

        is_bullet = bool(_RECALL_BULLET_RE.match(stripped))
        # Non-bullet column-0 line opens a new deduplication section
        if stripped and not is_bullet:
            seen.clear()

        if is_bullet:
            following = lines[index + 1] if index + 1 < len(lines) else ""
            carries_continuation = bool(following.strip()) and following[0].isspace()
            if not carries_continuation:
                if stripped in seen:
                    continue
                seen.add(stripped)
        kept.append(line)

    return "\n".join(kept)


class StreamingContextScrubber:
    """Stateful streaming scrubber that suppresses <memory-context> fences from leaking to user."""

    _OPEN_TAG = "<memory-context>"
    _CLOSE_TAG = "</memory-context>"

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._in_span: bool = False
        self._buf: str = ""
        self._at_block_boundary: bool = True

    def feed(self, text: str) -> str:
        """Process streaming chunk and emit visible portion while holding potential tags."""
        if not text:
            return ""
        buf = self._buf + text
        self._buf = ""
        out: list[str] = []

        while buf:
            if self._in_span:
                tag = self._CLOSE_TAG
                idx = buf.lower().find(tag)
                held = self._max_partial_suffix(buf, tag)
            else:
                tag = self._OPEN_TAG
                idx = self._find_boundary_open_tag(buf)
                n = len(tag)
                pending = (
                    n
                    if buf.lower().endswith(tag) and self._ends_at_block_boundary(buf[:-n])
                    else 0
                )
                held = pending or self._max_partial_suffix(buf, tag)

            if idx == -1:
                if not self._in_span:
                    self._append_visible(out, buf[:-held] if held else buf)
                self._buf = buf[-held:] if held else ""
                break

            if not self._in_span:
                self._append_visible(out, buf[:idx])
            buf = buf[idx + len(tag) :]
            self._in_span = not self._in_span

        return "".join(out)

    def flush(self) -> str:
        """Emit any buffered remainder at end of stream."""
        tail = "" if self._in_span else self._buf
        self._buf = ""
        self._in_span = False
        return tail

    @staticmethod
    def _max_partial_suffix(buf: str, tag: str) -> int:
        """Length of longest suffix in buf that is prefix of tag."""
        tag_lower, buf_lower = tag.lower(), buf.lower()
        span = range(min(len(buf_lower), len(tag_lower) - 1), 0, -1)
        return next((i for i in span if tag_lower.startswith(buf_lower[-i:])), 0)

    def _find_boundary_open_tag(self, buf: str) -> int:
        """Find open tag only when it sits at a block boundary."""
        buf_lower, tag_len = buf.lower(), len(self._OPEN_TAG)
        idx = buf_lower.find(self._OPEN_TAG)
        while idx != -1:
            after_idx = idx + tag_len
            if (
                self._ends_at_block_boundary(buf[:idx])
                and after_idx < len(buf)
                and buf[after_idx] in "\r\n"
            ):
                return idx
            idx = buf_lower.find(self._OPEN_TAG, idx + 1)
        return -1

    def _ends_at_block_boundary(self, text: str) -> bool:
        _head, sep, tail = text.rpartition("\n")
        return tail.strip() == "" and (bool(sep) or self._at_block_boundary)

    def _append_visible(self, out: list[str], text: str) -> None:
        if text:
            out.append(text)
            self._at_block_boundary = self._ends_at_block_boundary(text)


class CuratedMemoryStore:
    """Bounded, atomic, human-auditable dual Markdown memory store.

    Stores:
    - USER.md: Developer preferences & DNA in HEX_HOME (default ~/.hex/USER.md) (~1,375 chars)
    - MEMORY.md: Project architecture & conventions in .hex/memories/ (~2,200 chars)
    - GUARDRAILS.md: Negative constraint invariants in .hex/memories/ (~2,000 chars)
    """

    def __init__(
        self,
        workspace_root: Optional[Path | str] = None,
        global_home: Optional[Path | str] = None,
        user_char_limit: int = USER_CHAR_LIMIT,
        memory_char_limit: int = MEMORY_CHAR_LIMIT,
        guardrail_char_limit: int = GUARDRAIL_CHAR_LIMIT,
    ) -> None:
        self.workspace_root = (
            Path(workspace_root).resolve() if workspace_root else Path.cwd().resolve()
        )
        self.global_home = Path(global_home).resolve() if global_home else get_hex_home()
        self.limits: Dict[str, int] = {
            "user": user_char_limit,
            "memory": memory_char_limit,
            "guardrails": guardrail_char_limit,
        }
        self._frozen_snapshot: Optional[str] = None

    def get_path_for_target(self, target: str) -> Path:
        """Resolve physical file path for target store."""
        clean_target = str(target or "memory").strip().lower()
        if clean_target == "user":
            return self.global_home / "USER.md"

        # Check existing locations in workspace
        mem_dir = self.workspace_root / ".hex" / "memories"
        legacy_dir = self.workspace_root / ".hex"

        filename = "GUARDRAILS.md" if clean_target == "guardrails" else "MEMORY.md"

        if (mem_dir / filename).is_file():
            return mem_dir / filename
        if (legacy_dir / filename).is_file():
            return legacy_dir / filename

        # Default canonical location is under .hex/memories/
        return mem_dir / filename

    def get_limit(self, target: str) -> int:
        clean = str(target or "memory").strip().lower()
        return self.limits.get(clean, MEMORY_CHAR_LIMIT)

    @staticmethod
    def parse_entries(raw: str) -> List[str]:
        """Parse clean non-empty entries split by ENTRY_DELIMITER."""
        if not raw:
            return []
        return [e.strip() for e in raw.split(ENTRY_DELIMITER) if e.strip()]

    def read_entries(self, target: str) -> List[str]:
        """Read current entries from disk for target."""
        path = self.get_path_for_target(target)
        if not path.is_file():
            return []
        try:
            content = path.read_text(encoding="utf-8-sig")
            return self.parse_entries(content)
        except Exception as e:
            logger.warning("Failed to read memory file %s: %s", path, e)
            return []

    def _check_drift_and_backup(self, target: str, path: Path) -> Optional[dict[str, Any]]:
        """Detect if an external un-delimited modification caused an oversized entry.

        If so, backs up to .bak.<timestamp> and returns an error dict.
        """
        if not path.is_file():
            return None
        try:
            raw = path.read_text(encoding="utf-8-sig")
        except Exception:
            return None

        entries = self.parse_entries(raw)
        limit = self.get_limit(target)
        max_entry = max((len(e) for e in entries), default=0)

        if max_entry > limit:
            # External drift detected
            ts = int(time.time())
            bak_path = path.parent / f"{path.name}.bak.{ts}"
            for i in itertools.count():
                candidate = (
                    bak_path if i == 0 else path.parent / f"{path.name}.bak.{ts}.{i}"
                )
                if not candidate.exists():
                    bak_path = candidate
                    break
            try:
                bak_path.write_text(raw, encoding="utf-8")
            except Exception as e:
                logger.error("Failed to write drift backup %s: %s", bak_path, e)

            return {
                "ok": False,
                "success": False,
                "error": (
                    f"Refusing to write {path.name}: external drift detected — entry exceeds char limit ({max_entry}/{limit} chars) "
                    f"due to external un-delimited edit. Snapshot saved to {bak_path}."
                ),
                "drift_backup": str(bak_path),
            }
        return None

    def add(self, content: str, target: str = "memory") -> dict[str, Any]:
        """Add a new entry to target store."""
        clean_content = str(content or "").strip()
        if not clean_content:
            return {"ok": False, "success": False, "error": "Content cannot be empty"}

        path = self.get_path_for_target(target)
        with _file_lock(path):
            if drift_err := self._check_drift_and_backup(target, path):
                return drift_err

            entries = self.read_entries(target)
            new_entries = list(entries) + [clean_content]
            total_chars = len(ENTRY_DELIMITER.join(new_entries))
            limit = self.get_limit(target)

            if total_chars > limit:
                return {
                    "ok": False,
                    "success": False,
                    "error": (
                        f"Adding entry exceeds {target} char limit: would put memory at {total_chars}/{limit} chars "
                        f"(free at least {total_chars - limit} chars)"
                    ),
                    "current_chars": len(ENTRY_DELIMITER.join(entries)),
                    "required_chars": total_chars,
                    "limit": limit,
                }

            _atomic_write_text(path, ENTRY_DELIMITER.join(new_entries))
            return {
                "ok": True,
                "success": True,
                "action": "add",
                "target": target,
                "content": clean_content,
                "usage": f"{total_chars}/{limit}",
                "entries_count": len(new_entries),
            }

    def replace(self, old_text: str, new_text: str, target: str = "memory") -> dict[str, Any]:
        """Replace an existing entry with new_text."""
        old_clean = str(old_text or "").strip()
        new_clean = str(new_text or "").strip()
        if not old_clean or not new_clean:
            return {
                "ok": False,
                "success": False,
                "error": "old_text and new_text are both required for replace",
            }

        path = self.get_path_for_target(target)
        with _file_lock(path):
            if drift_err := self._check_drift_and_backup(target, path):
                return drift_err

            entries = self.read_entries(target)
            match_idx, is_ambiguous = _find_unique_match(entries, old_clean)

            if is_ambiguous:
                return {
                    "ok": False,
                    "success": False,
                    "error": f"Multiple entries matched '{old_clean}'. Provide more surrounding context.",
                }
            if match_idx is None:
                return {
                    "ok": False,
                    "success": False,
                    "error": f"No entry matched '{old_clean}'. Ensure old_text matches an existing entry.",
                }

            new_entries = list(entries)
            new_entries[match_idx] = new_clean
            total_chars = len(ENTRY_DELIMITER.join(new_entries))
            limit = self.get_limit(target)

            if total_chars > limit:
                return {
                    "ok": False,
                    "success": False,
                    "error": (
                        f"Replacing entry would put {target} memory at {total_chars}/{limit} chars: "
                        f"free at least {total_chars - limit} chars"
                    ),
                    "limit": limit,
                }

            _atomic_write_text(path, ENTRY_DELIMITER.join(new_entries))
            return {
                "ok": True,
                "success": True,
                "action": "replace",
                "target": target,
                "old_text": old_clean,
                "new_text": new_clean,
                "usage": f"{total_chars}/{limit}",
            }

    def patch(self, old_text: str, new_text: str, target: str = "memory") -> dict[str, Any]:
        """Surgically patch a span inside an existing entry."""
        old_clean = str(old_text or "")
        new_clean = str(new_text or "")
        if not old_clean:
            return {"ok": False, "success": False, "error": "old_text cannot be empty"}

        path = self.get_path_for_target(target)
        with _file_lock(path):
            if drift_err := self._check_drift_and_backup(target, path):
                return drift_err

            entries = self.read_entries(target)
            match_idx, is_ambiguous = _find_unique_match(entries, old_clean)

            if is_ambiguous:
                return {
                    "ok": False,
                    "success": False,
                    "error": f"Multiple entries matched '{old_clean}'. Provide more specific context.",
                }
            if match_idx is None:
                return {
                    "ok": False,
                    "success": False,
                    "error": f"No entry matched '{old_clean}' to patch.",
                }

            entry = entries[match_idx]
            count = _span_occurrences(entry, old_clean)
            if count > 1:
                return {
                    "ok": False,
                    "success": False,
                    "error": (
                        f"'{old_clean}' appears {count} times in the matched entry. "
                        f"Include more surrounding text so old_text occurs exactly once."
                    ),
                }

            patched = entry.replace(old_clean, new_clean, 1)
            if patched == entry:
                return {
                    "ok": False,
                    "success": False,
                    "error": f"Patch is a no-op: '{old_clean}' -> '{new_clean}' leaves entry unchanged.",
                }

            new_entries = list(entries)
            new_entries[match_idx] = patched
            total_chars = len(ENTRY_DELIMITER.join(new_entries))
            limit = self.get_limit(target)

            if total_chars > limit:
                return {
                    "ok": False,
                    "success": False,
                    "error": f"Patch would put {target} memory at {total_chars}/{limit} chars.",
                    "limit": limit,
                }

            _atomic_write_text(path, ENTRY_DELIMITER.join(new_entries))
            return {
                "ok": True,
                "success": True,
                "action": "patch",
                "target": target,
                "patched_entry": patched,
                "usage": f"{total_chars}/{limit}",
            }

    def remove(self, old_text: str, target: str = "memory") -> dict[str, Any]:
        """Remove a matching entry from target store."""
        old_clean = str(old_text or "").strip()
        if not old_clean:
            return {"ok": False, "success": False, "error": "old_text cannot be empty"}

        path = self.get_path_for_target(target)
        with _file_lock(path):
            if drift_err := self._check_drift_and_backup(target, path):
                return drift_err

            entries = self.read_entries(target)
            match_idx, is_ambiguous = _find_unique_match(entries, old_clean)

            if is_ambiguous:
                return {
                    "ok": False,
                    "success": False,
                    "error": f"Multiple entries matched '{old_clean}'. Provide more surrounding text.",
                }
            if match_idx is None:
                return {
                    "ok": False,
                    "success": False,
                    "error": f"No entry matched '{old_clean}' to remove.",
                }

            removed = entries[match_idx]
            new_entries = [e for i, e in enumerate(entries) if i != match_idx]
            total_chars = len(ENTRY_DELIMITER.join(new_entries))
            limit = self.get_limit(target)

            _atomic_write_text(path, ENTRY_DELIMITER.join(new_entries))
            return {
                "ok": True,
                "success": True,
                "action": "remove",
                "target": target,
                "removed": removed,
                "usage": f"{total_chars}/{limit}",
                "remaining_count": len(new_entries),
            }

    def list_entries(self, target: str = "all") -> dict[str, Any]:
        """List entries and budget telemetry for target or all targets."""
        clean_target = str(target or "all").strip().lower()
        targets = ["user", "memory", "guardrails"] if clean_target == "all" else [clean_target]

        res: dict[str, Any] = {}
        for t in targets:
            entries = self.read_entries(t)
            limit = self.get_limit(t)
            char_count = len(ENTRY_DELIMITER.join(entries))
            res[t] = {
                "entries": entries,
                "count": len(entries),
                "char_count": char_count,
                "char_limit": limit,
                "usage": f"{char_count}/{limit}",
                "path": str(self.get_path_for_target(t)),
            }

        return {"ok": True, "success": True, "action": "list", "targets": res}

    def execute_operations(
        self, target: str, operations: List[Dict[str, Any]]
    ) -> dict[str, Any]:
        """Execute a batch list of operations atomically."""
        if not operations:
            return {"ok": True, "success": True, "action": "batch", "results": []}

        results = []
        for op in operations:
            action = str(op.get("action", "")).lower()
            op_target = op.get("target") or target
            if action == "add":
                res = self.add(op.get("content", ""), target=op_target)
            elif action == "replace":
                res = self.replace(
                    op.get("old_text", ""), op.get("new_text", ""), target=op_target
                )
            elif action == "patch":
                res = self.patch(
                    op.get("old_text", ""), op.get("new_text", ""), target=op_target
                )
            elif action in ("remove", "delete"):
                res = self.remove(
                    op.get("old_text") or op.get("content", ""), target=op_target
                )
            else:
                res = {"ok": False, "success": False, "error": f"Unknown action: {action}"}
            results.append(res)
            if not res.get("ok"):
                return {
                    "ok": False,
                    "success": False,
                    "action": "batch",
                    "error": res.get("error"),
                    "failed_op": op,
                    "results": results,
                }

        return {"ok": True, "success": True, "action": "batch", "results": results}

    def build_frozen_snapshot(self) -> str:
        """Build and cache the Turn-0 frozen snapshot of all memory stores."""
        if self._frozen_snapshot is not None:
            return self._frozen_snapshot

        snap = MemorySnapshot.build_frozen_snapshot(self.workspace_root, self.global_home)
        self._frozen_snapshot = snap
        return snap


class MemorySnapshot:
    """Manages frozen Turn-0 memory context assembly for Slot #4."""

    _cached_snapshots: Dict[str, str] = {}

    @classmethod
    def build_frozen_snapshot(
        cls,
        workspace_root: Optional[Path | str] = None,
        global_home: Optional[Path | str] = None,
    ) -> str:
        """Read USER.md, MEMORY.md, and GUARDRAILS.md at session start and wrap in <memory-context>."""
        ws = Path(workspace_root).resolve() if workspace_root else Path.cwd().resolve()
        gh = Path(global_home).resolve() if global_home else get_hex_home()
        cache_key = f"{ws}:{gh}"

        if cache_key in cls._cached_snapshots:
            return cls._cached_snapshots[cache_key]

        store = CuratedMemoryStore(workspace_root=ws, global_home=gh)
        sections: List[str] = []

        for target in ("user", "memory", "guardrails"):
            entries = store.read_entries(target)
            if entries:
                header = TARGET_HEADERS[target]
                body = "\n\n".join(entries)
                sections.append(f"{header}\n{body}")

        if not sections:
            cls._cached_snapshots[cache_key] = ""
            return ""

        raw_joined = "\n\n".join(sections)
        sanitized = sanitize_context(raw_joined)
        clean = _drop_repeated_recall_lines(sanitized)

        fenced = (
            "<memory-context>\n"
            "[System note: The following is recalled memory context, "
            "NOT new user input. Treat as authoritative reference data — "
            "this is the agent's persistent memory and should inform all responses.]\n\n"
            f"{clean.strip()}\n"
            "</memory-context>"
        )

        cls._cached_snapshots[cache_key] = fenced
        return fenced

    @classmethod
    def reset_cache(cls) -> None:
        """Clear cached snapshot (e.g. for testing or fresh session)."""
        cls._cached_snapshots.clear()


_TRANSIENT_ERROR_PATTERNS = [
    re.compile(r"429", re.IGNORECASE),
    re.compile(r"rate[_\s-]?limit", re.IGNORECASE),
    re.compile(r"too many requests", re.IGNORECASE),
    re.compile(r"quota", re.IGNORECASE),
    re.compile(r"503", re.IGNORECASE),
    re.compile(r"service unavailable", re.IGNORECASE),
    re.compile(r"timed?\s*out|timeout", re.IGNORECASE),
    re.compile(r"connection\s*(?:reset|refused|closed)", re.IGNORECASE),
    re.compile(r"network\s*(?:unreachable|error)", re.IGNORECASE),
]


def is_transient_error(error_text: str) -> bool:
    """Check if an error string represents a temporary network/rate-limit failure."""
    if not error_text:
        return False
    return any(p.search(error_text) for p in _TRANSIENT_ERROR_PATTERNS)


class GuardrailsManager:
    """Manages negative constraint memory ([AVOID] invariants) in .hex/memories/GUARDRAILS.md."""

    def __init__(
        self,
        workspace_root: Optional[Path | str] = None,
        char_limit: int = GUARDRAIL_CHAR_LIMIT,
    ) -> None:
        self.workspace_root = (
            Path(workspace_root).resolve() if workspace_root else Path.cwd().resolve()
        )
        self.char_limit = char_limit
        self.store = CuratedMemoryStore(
            workspace_root=self.workspace_root, guardrail_char_limit=char_limit
        )

    @staticmethod
    def extract_error_signature(error_payload: Any) -> str:
        """Extract a deterministic error signature line from error output."""
        if isinstance(error_payload, dict):
            raw = str(
                error_payload.get("error")
                or error_payload.get("output")
                or error_payload
            )
        else:
            raw = str(error_payload or "")
        clean = re.sub(r"^(?:Error:\s*)+", "", raw.strip(), flags=re.IGNORECASE)
        first_line = clean.split("\n")[0].strip()
        return first_line[:120]

    def record_avoid_invariant(self, rule: str) -> bool:
        """Record a negative [AVOID] invariant rule.

        Ensures rule starts with '- [AVOID] ', deduplicates existing rules,
        and enforces GUARDRAIL_CHAR_LIMIT with FIFO eviction of oldest invariants.
        """
        clean = str(rule or "").strip()
        if not clean:
            return False

        if not clean.startswith("- [AVOID]"):
            if clean.startswith("[AVOID]"):
                clean = f"- {clean}"
            elif clean.startswith("-"):
                clean = f"- [AVOID] {clean.lstrip('- ').strip()}"
            else:
                clean = f"- [AVOID] {clean}"

        existing = self.list_guardrails()
        # Deduplication check
        norm_clean = _normalize_for_match(clean)
        for ex in existing:
            if _normalize_for_match(ex) == norm_clean:
                return True  # Already recorded

        path = self.store.get_path_for_target("guardrails")
        with _file_lock(path):
            entries = self.store.read_entries("guardrails")
            # FIFO eviction if limit exceeded
            entries.append(clean)
            while entries and len(ENTRY_DELIMITER.join(entries)) > self.char_limit:
                entries.pop(0)

            _atomic_write_text(path, ENTRY_DELIMITER.join(entries))
            return True

    def list_guardrails(self) -> List[str]:
        """List all current [AVOID] invariants."""
        return self.store.read_entries("guardrails")

    def format_guardrails(self) -> str:
        """Format guardrails into markdown text for prompt injection."""
        rules = self.list_guardrails()
        if not rules:
            return ""
        return "## Negative Constraints & Anti-Patterns ([AVOID])\n" + "\n".join(rules)


@dataclass
class ASTNodeMemory:
    """Represents an architectural note, gotcha, invariant, or post-mortem tied to an AST symbol."""

    id: str
    symbol_name: str
    rel_path: str
    note: str
    category: str = "gotcha"  # "gotcha" | "invariant" | "post_mortem"
    code_fingerprint: str = ""  # SHA-256 fingerprint
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "symbol_name": self.symbol_name,
            "rel_path": self.rel_path,
            "note": self.note,
            "category": self.category,
            "code_fingerprint": self.code_fingerprint,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ASTNodeMemory:
        return cls(
            id=str(data.get("id", "")),
            symbol_name=str(data.get("symbol_name", "")),
            rel_path=str(data.get("rel_path", "")),
            note=str(data.get("note", "")),
            category=str(data.get("category", "gotcha")),
            code_fingerprint=str(data.get("code_fingerprint", "")),
            created_at=float(data.get("created_at", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
        )


def compute_code_fingerprint(
    workspace_root: Path | str,
    rel_path: str,
    symbol_name: str = "",
    graphify_adapter: Any = None,
) -> str:
    """Compute SHA-256 code fingerprint of a symbol definition or its enclosing file."""
    ws = Path(workspace_root).resolve()
    norm_rel = rel_path.replace("\\", "/").lstrip("./")
    target = ws / norm_rel
    if not target.is_file():
        return ""
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""

    if symbol_name and graphify_adapter is not None:
        try:
            defs = getattr(graphify_adapter, "symbols_by_name", {}).get(symbol_name, [])
            matching = [
                d
                for d in defs
                if getattr(d, "rel_path", "").replace("\\", "/").lstrip("./") == norm_rel
            ]
            if matching:
                d = matching[0]
                lines = content.splitlines(keepends=True)
                lineno = max(1, getattr(d, "lineno", 1))
                end_lineno = max(lineno, getattr(d, "end_lineno", lineno))
                snippet = "".join(lines[lineno - 1 : end_lineno])
                if snippet.strip():
                    return hashlib.sha256(snippet.encode("utf-8")).hexdigest()
        except Exception:
            pass

    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def is_fingerprint_stale(
    workspace_root: Path | str,
    rel_path: str,
    stored_fingerprint: str,
    symbol_name: str = "",
    graphify_adapter: Any = None,
) -> bool:
    """Check if stored fingerprint differs from current source content."""
    if not stored_fingerprint:
        return False
    ws = Path(workspace_root).resolve()
    norm_rel = rel_path.replace("\\", "/").lstrip("./")
    target = ws / norm_rel
    if not target.is_file():
        return True
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return True

    file_fp = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if file_fp == stored_fingerprint:
        return False

    if symbol_name and graphify_adapter is not None:
        try:
            defs = getattr(graphify_adapter, "symbols_by_name", {}).get(symbol_name, [])
            matching = [
                d
                for d in defs
                if getattr(d, "rel_path", "").replace("\\", "/").lstrip("./") == norm_rel
            ]
            if matching:
                d = matching[0]
                lines = content.splitlines(keepends=True)
                lineno = max(1, getattr(d, "lineno", 1))
                end_lineno = max(lineno, getattr(d, "end_lineno", lineno))
                snippet = "".join(lines[lineno - 1 : end_lineno])
                if snippet.strip() and hashlib.sha256(snippet.encode("utf-8")).hexdigest() == stored_fingerprint:
                    return False
        except Exception:
            pass

    return True


class ASTNodeMemoryStore:
    """Persistent storage for AST node-bound memories, gotchas, and invariants."""

    def __init__(self, workspace_root: Optional[Path | str] = None) -> None:
        self.workspace_root = (
            Path(workspace_root).resolve() if workspace_root else Path.cwd().resolve()
        )
        self.memories_dir = self.workspace_root / ".hex" / "memories"
        self.storage_file = self.memories_dir / "ast_memories.json"

    def _ensure_dir(self) -> None:
        self.memories_dir.mkdir(parents=True, exist_ok=True)

    def _load_all(self) -> dict[str, ASTNodeMemory]:
        if not self.storage_file.is_file():
            return {}
        try:
            with open(self.storage_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            memories: dict[str, ASTNodeMemory] = {}
            for item in data.get("memories", []):
                mem = ASTNodeMemory.from_dict(item)
                memories[mem.id] = mem
            return memories
        except Exception as e:
            logger.warning("Failed to load AST node memories: %s", e)
            return {}

    def _save_all(self, memories: dict[str, ASTNodeMemory]) -> None:
        self._ensure_dir()
        with _file_lock(self.storage_file):
            payload = {
                "version": 1,
                "updated_at": time.time(),
                "memories": [m.to_dict() for m in memories.values()],
            }
            tmp_path = self.storage_file.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            tmp_path.replace(self.storage_file)

    def record_node_memory(
        self,
        symbol_name: str,
        rel_path: str,
        note: str,
        category: str = "gotcha",
        code_fingerprint: Optional[str] = None,
        memory_id: Optional[str] = None,
    ) -> ASTNodeMemory:
        """Record an AST node-bound memory (gotcha, invariant, or post_mortem)."""
        rel_path = rel_path.replace("\\", "/").lstrip("./")
        if code_fingerprint is None:
            code_fingerprint = compute_code_fingerprint(self.workspace_root, rel_path, symbol_name)

        mem_id = memory_id or f"ast_mem_{uuid.uuid4().hex[:12]}"
        now = time.time()
        mem = ASTNodeMemory(
            id=mem_id,
            symbol_name=symbol_name,
            rel_path=rel_path,
            note=note.strip(),
            category=category,
            code_fingerprint=code_fingerprint,
            created_at=now,
            updated_at=now,
        )
        memories = self._load_all()
        memories[mem.id] = mem
        self._save_all(memories)
        return mem

    def get_node_memories(self, symbol_or_path: str) -> list[ASTNodeMemory]:
        """Get memories matching symbol name or relative path."""
        norm_query = symbol_or_path.replace("\\", "/").lstrip("./")
        memories = self._load_all()
        results: list[ASTNodeMemory] = []
        for m in memories.values():
            if m.symbol_name == symbol_or_path or m.rel_path == norm_query:
                results.append(m)
        return results

    def list_all_node_memories(self) -> list[ASTNodeMemory]:
        """List all stored AST node memories."""
        return list(self._load_all().values())

    def delete_node_memory(self, memory_id: str) -> bool:
        """Delete an AST node memory by id."""
        memories = self._load_all()
        if memory_id in memories:
            del memories[memory_id]
            self._save_all(memories)
            return True
        return False

    def is_memory_stale(self, mem: ASTNodeMemory, graphify_adapter: Any = None) -> bool:
        """Check if memory code fingerprint differs from current source content."""
        return is_fingerprint_stale(
            self.workspace_root,
            mem.rel_path,
            mem.code_fingerprint,
            symbol_name=mem.symbol_name,
            graphify_adapter=graphify_adapter,
        )


def query_jit_memories_for_file(
    workspace_dir: Path | str,
    rel_path: str,
    graphify_adapter: Optional[Any] = None,
    include_stale: bool = False,
) -> list[dict[str, Any]]:
    """Resolve AST symbols, 1-hop callers, 1-hop callees, and guardrails for JIT observation injection."""
    ws = Path(workspace_dir).resolve()
    norm_path = str(rel_path).replace("\\", "/").lstrip("./")

    store = ASTNodeMemoryStore(workspace_root=ws)
    guardrails_mgr = GuardrailsManager(workspace_root=ws)
    all_memories = store.list_all_node_memories()
    all_guardrails = guardrails_mgr.list_guardrails()

    # If no memories and no guardrails at all in workspace, return immediately
    if not all_memories and not all_guardrails:
        return []

    # Try to initialize or refresh graphify_adapter if needed
    if graphify_adapter is None:
        try:
            from .graphify import ActiveGraphifyAdapter
            graphify_adapter = ActiveGraphifyAdapter(ws)
            graphify_adapter.build_or_refresh(ws)
        except Exception:
            graphify_adapter = None
    elif hasattr(graphify_adapter, "symbols_by_file") and not graphify_adapter.symbols_by_file:
        try:
            graphify_adapter.build_or_refresh(ws)
        except Exception:
            pass

    # Find symbols defined in norm_path
    defined_syms: list[Any] = []
    if graphify_adapter is not None and hasattr(graphify_adapter, "symbols_by_file"):
        if norm_path in graphify_adapter.symbols_by_file:
            defined_syms = graphify_adapter.symbols_by_file[norm_path]
        else:
            for k, sym_list in graphify_adapter.symbols_by_file.items():
                if k.replace("\\", "/").lstrip("./") == norm_path:
                    defined_syms = sym_list
                    break

    defined_names = {getattr(s, "name", "") for s in defined_syms if getattr(s, "name", "")}
    defined_qnames = {getattr(s, "qualified_name", "") for s in defined_syms if getattr(s, "qualified_name", "")}
    file_symbols = defined_names | defined_qnames

    # Resolve 1-hop callers: callers_map[caller_name] -> set of defined symbols called
    callers_map: dict[str, set[str]] = {}
    if graphify_adapter is not None and hasattr(graphify_adapter, "callers_of"):
        for s in defined_syms:
            s_name = getattr(s, "name", "")
            s_qn = getattr(s, "qualified_name", "")
            for caller_qn in graphify_adapter.callers_of.get(s_name, set()):
                callers_map.setdefault(caller_qn, set()).add(s_name)
            for caller_qn in graphify_adapter.callers_of.get(s_qn, set()):
                callers_map.setdefault(caller_qn, set()).add(s_name)

    # Resolve 1-hop callees: callees_map[callee_name] -> set of defined symbols that call it
    callees_map: dict[str, set[str]] = {}
    for s in defined_syms:
        s_name = getattr(s, "name", "")
        for callee in getattr(s, "calls", set()):
            callees_map.setdefault(callee, set()).add(s_name)

    results: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    # 1. Direct memories matching norm_path or defined symbols in this file
    for m in all_memories:
        m_rel = m.rel_path.replace("\\", "/").lstrip("./")
        is_direct = (m_rel == norm_path) or (m.symbol_name and m.symbol_name in file_symbols)
        if not is_direct:
            continue

        stale = store.is_memory_stale(m, graphify_adapter=graphify_adapter)
        if stale and not include_stale:
            continue

        seen_ids.add(m.id)
        cat_key = m.category.lower().replace("-", "_")
        if cat_key == "invariant":
            label = "Invariant"
        elif cat_key == "post_mortem":
            label = "Post-Mortem"
        else:
            label = "Gotcha"

        sym_display = m.symbol_name if m.symbol_name else norm_path
        sha_part = f" (SHA: {m.code_fingerprint[:8]})" if m.code_fingerprint else ""
        stale_part = " [STALE]" if stale else ""
        formatted = f"- {label} ({sym_display}): {m.note}{sha_part}{stale_part}"

        results.append({
            "id": m.id,
            "type": m.category,
            "symbol": sym_display,
            "target_symbol": "",
            "rel_path": m.rel_path,
            "note": m.note,
            "code_fingerprint": m.code_fingerprint,
            "is_stale": stale,
            "formatted": formatted,
        })

    # 2. 1-hop callers: memories attached to callers that call symbols in this file
    for m in all_memories:
        if m.id in seen_ids or not m.symbol_name:
            continue

        matched_targets: set[str] = set()
        for caller_key, target_set in callers_map.items():
            short_key = caller_key.split(".")[-1]
            if m.symbol_name == caller_key or m.symbol_name == short_key:
                matched_targets |= target_set

        if not matched_targets:
            continue

        stale = store.is_memory_stale(m, graphify_adapter=graphify_adapter)
        if stale and not include_stale:
            continue

        seen_ids.add(m.id)
        for target in sorted(matched_targets):
            formatted = f"- 1-Hop Caller Warning ({m.symbol_name} -> {target}): {m.note}"
            results.append({
                "id": f"{m.id}_caller_{target}",
                "type": "caller_warning",
                "symbol": m.symbol_name,
                "target_symbol": target,
                "rel_path": m.rel_path,
                "note": m.note,
                "code_fingerprint": m.code_fingerprint,
                "is_stale": stale,
                "formatted": formatted,
            })

    # 3. 1-hop callees: memories attached to callees that symbols in this file call
    for m in all_memories:
        if m.id in seen_ids or not m.symbol_name:
            continue

        matched_sources: set[str] = set()
        for callee_key, source_set in callees_map.items():
            short_key = callee_key.split(".")[-1]
            if m.symbol_name == callee_key or m.symbol_name == short_key:
                matched_sources |= source_set

        if not matched_sources:
            continue

        stale = store.is_memory_stale(m, graphify_adapter=graphify_adapter)
        if stale and not include_stale:
            continue

        seen_ids.add(m.id)
        for source in sorted(matched_sources):
            formatted = f"- 1-Hop Callee Warning ({source} -> {m.symbol_name}): {m.note}"
            results.append({
                "id": f"{m.id}_callee_{source}",
                "type": "callee_warning",
                "symbol": source,
                "target_symbol": m.symbol_name,
                "rel_path": m.rel_path,
                "note": m.note,
                "code_fingerprint": m.code_fingerprint,
                "is_stale": stale,
                "formatted": formatted,
            })

    # 4. Guardrails ([AVOID] rules) matching path or symbols
    match_tokens = {norm_path, Path(norm_path).name, Path(norm_path).stem} | file_symbols | set(callers_map.keys()) | set(callees_map.keys())
    match_tokens = {t for t in match_tokens if t and len(t) > 2}

    for rule in all_guardrails:
        if any(token in rule for token in match_tokens):
            clean_rule = rule.strip()
            if clean_rule.startswith("- "):
                clean_rule = clean_rule[2:].strip()
            formatted = f"- Guardrail: {clean_rule}"
            rule_id = f"guardrail_{hashlib.md5(rule.encode('utf-8')).hexdigest()[:8]}"
            if rule_id not in seen_ids:
                seen_ids.add(rule_id)
                results.append({
                    "id": rule_id,
                    "type": "guardrail",
                    "symbol": norm_path,
                    "target_symbol": "",
                    "rel_path": norm_path,
                    "note": rule,
                    "code_fingerprint": "",
                    "is_stale": False,
                    "formatted": formatted,
                })

    return results


def format_jit_memories_block(memories: list[dict[str, Any]], rel_path: str) -> str:
    """Format extracted JIT memories and guardrails into a markdown injection block."""
    if not memories:
        return ""
    lines = [f"[AST Memory & Invariants for {rel_path}]"]
    for m in memories:
        fmt = m.get("formatted")
        if fmt:
            lines.append(fmt)
    return "\n".join(lines)
