"""Multi-tier Soul Engine & Developer DNA Manager for HEX.

Implements Slot #1 identity resolution, compact token budgeting (~300-500 tokens),
atomic template seeding, legacy template auto-upgrades, and identity renaming.
"""

from __future__ import annotations

import logging
import os
import re
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

# Default agent persona adhering to empirical verification, directness, and pragmatic execution
DEFAULT_SOUL_NAME = "HEX"
DEFAULT_SOUL_MD = (
    "You are HEX, an autonomous AI agent and pairing partner. "
    "Be direct: match the length of your reply to the weight of the ask — a one-line question "
    "gets a one-line answer, and finished work gets a concise report of what changed, what is "
    "verified, and what is left, never a narration of tool calls the user can see. "
    "No conversational filler (\"Great question\", \"I'd be happy to\"), no restating the user's prompt, "
    "and no repetitive chanting of slogans or acronyms. Plain claims over adjectives; when unsure, "
    "state uncertainty plainly. Agree because it is right and verified, not because the user asserted it. "
    "Depth is earned — provide it when the user asks for detail, teaches, or when stakes demand it, not by default. "
    "When working with code, systems, or data, execute with pragmatic discipline: minimal working diffs, "
    "root-cause fixes, existing tools first, and zero speculative bloat. "
    "Keep your voice calm, sharp, mature, and focused on high-leverage execution."
)

_SCAFFOLD_HEAD = (
    "# HEX Agent Persona\n\n<!--\nThis file defines the agent's personality and tone.\n"
    "The agent will embody whatever you write here.\nEdit this to customize how HEX communicates with you.\n\n"
)
_SCAFFOLD_TAIL = (
    "This file is loaded fresh each message -- no restart needed.\n"
    "Delete the contents (or this file) to use the default personality.\n-->"
)

# Known legacy template fingerprints for safe in-place upgrades
_LEGACY_TEMPLATE_SOULS = (
    _SCAFFOLD_HEAD + _SCAFFOLD_TAIL,
    _SCAFFOLD_HEAD + (
        "Examples:\n"
        '  - "You are a concise technical expert. No fluff, just facts."\n'
        '  - "You are an autonomous engineering partner focused on minimal diffs."\n\n'
    ) + _SCAFFOLD_TAIL,
    # Previous generation scaffold strings
    (
        "You are HEX Agent, an autonomous software engineering assistant. "
        "You write clean, minimal code, adhere to YAGNI principles, and verify all actions."
    ),
    (
        "You are HEX Agent, an autonomous engineering agent and agentic pairing partner. "
        "Be direct: match the length of your reply to the weight of the ask — a one-line question "
        "gets a one-line answer, and finished work gets a concise report of what changed, what's "
        "verified, and what's left, never a narration of tool calls the user can see. Plain claims "
        "over adjectives; when unsure, state uncertainty plainly. Agree because it is empirically "
        "verified, not because the user asserted it. Keep your tone calm, concise, and focused on "
        "high-leverage execution."
    ),
    DEFAULT_SOUL_MD.replace("—", "--"),
)

# Developer DNA default template
DEFAULT_USER_MD = (
    "# Developer Profile & Conventions\n\n"
    "- Architecture: Minimal abstractions, modular single-responsibility components, strict YAGNI.\n"
    "- Testing: Verification-first; always provide empirical proof (unit tests, command outputs).\n"
    "- Code Style: Clean, idiomatic, typed, zero unrequested boilerplate.\n"
)

MAX_SOUL_CHARS = 2500  # ~500 tokens cap to prevent prompt bloat on local 8B-9B models
TRUNCATION_MARKER = "\n... [truncated to fit 500-token budget]"


class SoulProvenance(str, Enum):
    """Explicit trust-tier provenance of resolved soul content."""

    GLOBAL_USER = "global_user"
    WORKSPACE_USER = "workspace_user"

    def __eq__(self, other: Any) -> bool:
        if super().__eq__(other):
            return True
        val = getattr(other, "value", other)
        if self.value == "workspace_user" and val in ("workspace", "workspace_user"):
            return True
        if self.value == "global_user" and val in ("global", "default", "global_user"):
            return True
        return False


def normalize_soul(text: str) -> str:
    """Unify line endings, strip leading UTF-8 BOM, and trim surrounding whitespace."""
    if not text:
        return ""
    return text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff").strip()


def get_hex_home() -> Path:
    """Resolve global HEX home directory from env HEX_HOME or default ~/.hex."""
    env_home = os.environ.get("HEX_HOME")
    if env_home:
        return Path(env_home).expanduser().resolve()
    return (Path.home() / ".hex").resolve()


class SoulManager:
    """Manages global and workspace SOUL.md files and /rename."""

    def __init__(self, global_home: Optional[Path] = None):
        self.global_home = Path(global_home).resolve() if global_home else get_hex_home()
        self.global_home.mkdir(parents=True, exist_ok=True)
        self.global_soul_path = self.global_home / "SOUL.md"
        self.global_user_path = self.global_home / "USER.md"
        self._ensure_global_files()

    def _ensure_global_files(self) -> None:
        """Seed default global SOUL.md and USER.md if missing, or upgrade legacy templates."""
        if not self.global_soul_path.exists():
            self._atomic_write(self.global_soul_path, DEFAULT_SOUL_MD)
        else:
            self.maybe_upgrade_soul(self.global_soul_path)

        if not self.global_user_path.exists():
            self._atomic_write(self.global_user_path, DEFAULT_USER_MD)

    def _atomic_write(self, target_path: Path, content: str) -> None:
        """Atomically write content using a temporary file in the same directory."""
        target_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = target_path.parent / f".{target_path.name}.{os.getpid()}.tmp"
        try:
            tmp_path.write_text(content, encoding="utf-8")
            tmp_path.replace(target_path)
        except Exception:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            raise

    def maybe_upgrade_soul(self, soul_path: Path) -> bool:
        """Upgrade soul file in-place if its content matches a known legacy default template."""
        if not soul_path.exists():
            return False
        try:
            raw = soul_path.read_text(encoding="utf-8")
            norm = normalize_soul(raw)
            for legacy in _LEGACY_TEMPLATE_SOULS:
                if norm == normalize_soul(legacy):
                    logger.info("Upgrading legacy soul template at %s to latest default", soul_path)
                    self._atomic_write(soul_path, DEFAULT_SOUL_MD)
                    return True
        except Exception as e:
            logger.debug("Failed reading soul at %s for legacy check: %s", soul_path, e)
        return False

    def resolve_soul(
        self, workspace_root: Optional[Path | str] = None
    ) -> Tuple[str, SoulProvenance]:
        """Resolve the effective soul text and provenance scope.

        Returns (soul_content, SoulProvenance).
        Enforces MAX_SOUL_CHARS budget to protect local model context windows.
        """
        # 1. Check workspace .hex/SOUL.md first (tagged as workspace_user)
        if workspace_root:
            ws_path = Path(workspace_root).resolve()
            ws_soul = ws_path / ".hex" / "SOUL.md"
            if ws_soul.is_file():
                try:
                    content = ws_soul.read_text(encoding="utf-8").strip()
                    if content:
                        return (
                            self._enforce_budget(content, f"workspace ({ws_soul})"),
                            SoulProvenance.WORKSPACE_USER,
                        )
                except Exception as e:
                    logger.debug("Could not read workspace soul %s: %s", ws_soul, e)

        # 2. Fall back to global SOUL.md (tagged as global_user)
        if self.global_soul_path.is_file():
            try:
                content = self.global_soul_path.read_text(encoding="utf-8").strip()
                if content:
                    return (
                        self._enforce_budget(content, f"global ({self.global_soul_path})"),
                        SoulProvenance.GLOBAL_USER,
                    )
            except Exception as e:
                logger.debug("Could not read global soul %s: %s", self.global_soul_path, e)

        return DEFAULT_SOUL_MD, SoulProvenance.GLOBAL_USER

    def _enforce_budget(self, content: str, source_label: str) -> str:
        """Enforce compact token budget (~300-500 tokens / 2,500 chars).

        Reserves space for TRUNCATION_MARKER so total characters never exceed MAX_SOUL_CHARS.
        """
        if len(content) > MAX_SOUL_CHARS:
            logger.warning(
                "Soul from %s exceeds budget (%d > %d chars); truncating to protect context cache",
                source_label,
                len(content),
                MAX_SOUL_CHARS,
            )
            keep_len = MAX_SOUL_CHARS - len(TRUNCATION_MARKER)
            return content[:keep_len].rstrip() + TRUNCATION_MARKER
        return content

    def rename_identity(
        self,
        new_name: str,
        scope: str = "global",
        workspace_root: Optional[Path | str] = None,
    ) -> bool:
        """Rename the agent identity in the target SOUL.md.

        Replaces 'You are <name>' pattern or prepends an explicit identity assertion.
        scope: 'global' or 'workspace'.
        """
        clean_name = new_name.strip()
        if not clean_name:
            raise ValueError("Identity name cannot be blank")

        target_file = self.global_soul_path
        if scope == "workspace":
            if not workspace_root:
                raise ValueError("workspace_root required for workspace scope rename")
            target_file = Path(workspace_root).resolve() / ".hex" / "SOUL.md"

        current_content = DEFAULT_SOUL_MD
        if target_file.exists():
            try:
                current_content = target_file.read_text(encoding="utf-8")
            except Exception:
                current_content = DEFAULT_SOUL_MD

        # Regex replace 'You are <OldName>' or insert at top
        pattern = re.compile(r"^You are [A-Za-z0-9_\-]+(?:\s+Agent)?([,\.])", re.IGNORECASE)
        lines = current_content.splitlines()
        updated = False
        new_lines = []

        for line in lines:
            if not updated and pattern.match(line):
                new_line = pattern.sub(f"You are {clean_name}\\1", line)
                new_lines.append(new_line)
                updated = True
            else:
                new_lines.append(line)

        if not updated:
            # Prepend identity line
            new_lines.insert(0, f"You are {clean_name}.")

        final_content = "\n".join(new_lines).strip() + "\n"
        self._atomic_write(target_file, final_content)
        logger.info("Renamed agent identity to '%s' in %s", clean_name, target_file)
        return True

    def get_active_name(self, workspace_root: Optional[Path | str] = None) -> str:
        """Extract the agent's current active name from resolved soul content."""
        soul_text, _ = self.resolve_soul(workspace_root)
        m = re.search(r"\bYou are ([A-Za-z0-9_\-]+)", soul_text)
        if m:
            return m.group(1)
        return DEFAULT_SOUL_NAME
