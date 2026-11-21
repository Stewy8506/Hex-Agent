"""System Prompt Assembly Pipeline & Security Gate for HEX.

Implements the strict 5-Slot system prompt architecture:
  Slot #1: SOUL.md Identity (resolved via SoulManager)
  Slot #2: Mode Behavioral Overrides (3-4 punchy lines for plan / agent / code)
  Slot #3: Environment Context & Execution Boundaries
  Slot #4: Curated Memory Snapshot (fenced in <memory-context>)
  Slot #5: Project Rules & Skills Context (AGENTS.md, .cursorrules)
"""

from __future__ import annotations

import logging
import platform
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from harness_core.cognition.soul import DEFAULT_SOUL_NAME, SoulManager, get_hex_home
from harness_core.resources import bundled_skills_dir

logger = logging.getLogger(__name__)

# Heuristic threat patterns for prompt injection in context files
_INJECTION_PATTERNS = [
    (
        "ignore_instructions",
        re.compile(
            r"(?i)(?:ignore|disregard|forget|bypass)\s+(?:all\s+)?(?:previous\s+|prior\s+|above\s+|system\s+)?(?:instructions|directives|prompts|rules)"
        ),
    ),
    (
        "jailbreak_mode",
        re.compile(
            r"(?i)(?:you\s+are\s+now\s+in\s+|enter\s+|enable\s+)(?:developer\s+mode|dan\s+mode|unrestricted\s+mode|god\s+mode|jailbreak)"
        ),
    ),
    (
        "prompt_override",
        re.compile(
            r"(?i)(?:system\s+prompt\s+override|override\s+(?:all\s+)?(?:system\s+)?(?:directives|instructions|tools?|rules)|new\s+system\s+instruction)"
        ),
    ),
    (
        "secret_exfiltration",
        re.compile(
            r"(?i)(?:print|output|exfiltrate|send|leak|dump)\s+(?:all\s+)?(?:env|environment\s+variables|api[_\s-]?keys?|secrets?|tokens?|credentials?)"
        ),
    ),
    (
        "tool_override",
        re.compile(
            r"(?i)(?:override|bypass|disable)\s+(?:all\s+)?(?:tools?|security|policies|guardrails|safety)"
        ),
    ),
    (
        "role_prompt",
        re.compile(
            r"(?i)(?:^|\n)\s*(?:system\s*:|<\|im_start\|>system|<system>)"
        ),
    ),
]


def scan_for_threats(content: str) -> List[str]:
    """Scan content for heuristic prompt injection and exfiltration patterns."""
    findings = []
    for threat_name, pattern in _INJECTION_PATTERNS:
        if pattern.search(content):
            findings.append(threat_name)
    return findings


def scan_context_content(content: str, filename: str, *, user_authored: bool = False) -> str:
    """Scan a context file for injection.

    - user_authored=True (user's own ~/.hex/SOUL.md or ~/.hex/USER.md): warns on match, still loads.
    - user_authored=False (cloned repo files or third-party profiles): matches are BLOCKED.
    """
    cleaned = content.removeprefix("\ufeff")
    findings = scan_for_threats(cleaned)
    if not findings:
        return cleaned

    if user_authored:
        logger.warning(
            "Context file %s matched injection pattern(s) %s; loaded anyway because it is user-authored in HEX_HOME",
            filename,
            ", ".join(findings),
        )
        return cleaned

    logger.warning("Context file %s blocked: matches %s", filename, ", ".join(findings))
    return f"[BLOCKED: {filename} contained potential prompt injection ({', '.join(findings)}). Content not loaded.]"


# Punchy 3-4 line behavioral constraints for Slot #2
MODE_MODULATORS: Dict[str, str] = {
    "plan": (
        "[Mode: ARCHITECT]\n"
        "- Inquisitive and trade-off focused; surface ambiguities and edge cases early.\n"
        "- Decompose problems into verified milestones before proposing file changes.\n"
        "- Never emit implementation code until requirements and verification criteria are locked."
    ),
    "agent": (
        "[Mode: BUILDER]\n"
        "- Decisive, test-driven, and empirical; bias toward execution and verification.\n"
        "- Keep conversational chatter to a minimum; let tool results and test passes drive progress.\n"
        "- Confirm all file edits with concrete verification commands."
    ),
    "code": (
        "[Mode: SURGICAL CODER]\n"
        "- Minimal diff specialist adhering strictly to YAGNI.\n"
        "- Zero conversational preamble or postscript boilerplate; emit only surgical AST edits.\n"
        "- Prefer deletion over addition; add zero unrequested abstractions or dependencies."
    ),
}

def build_capabilities_guidance(agent_name: str) -> str:
    """Build the Slot #3 guidance block carrying the resolved identity (B64)."""
    return (
        "[Capabilities & Interaction Guidance]\n"
        f"You are {agent_name}, an autonomous AI agent and pairing partner. When asked about your capabilities, what you can do, or available commands, answer concisely from your active tools, skills, and session commands (/help, /mode [plan|agent|code], /model [name], /clear, /quit).\n"
        "Match reply length to the ask. Be direct, calm, and senior; avoid robotic disclaimers, filler, or repetitive chanting of acronyms."
    )

PARALLEL_TOOL_CALL_GUIDANCE: str = (
    "[Parallel Tool Calls]\n"
    "Batch independent read, search, and web lookup calls in a single turn rather than serializing them one by one. "
    "Serialize calls only when a later call depends on an earlier call's output."
)

PLATFORM_HINTS: Dict[str, str] = {
    "tui": "Surface: HEX TUI. Markdown renders cleanly; deliver local files by stating their path.",
    "cli": "Surface: Plain CLI terminal. Prefer concise plain text or minimal markdown; state file paths directly.",
    "acp": "Surface: Editor ACP integration. Keep responses concise, code-focused, and cite exact file paths/symbols.",
    "desktop": "Surface: Desktop GUI. Full GitHub-flavored Markdown and structured tables render natively.",
}


def build_tools_summary(max_tools: int = 35) -> str:
    """Build a compact summary of active tools from ToolGateway.TOOL_DEFINITIONS."""
    try:
        from harness_core.gateway import TOOL_DEFINITIONS
    except Exception:
        return ""

    if not TOOL_DEFINITIONS:
        return ""

    lines = ["[Available Tools]"]
    for t in TOOL_DEFINITIONS[:max_tools]:
        name = t.get("name") or t.get("id") or ""
        desc = (t.get("description") or "").split(".")[0].strip()
        risk = t.get("risk_class", "")
        if name:
            risk_tag = f" ({risk})" if risk else ""
            lines.append(f"- {name}{risk_tag}: {desc}")
    return "\n".join(lines)


def build_skills_index(
    workspace_root: Path,
    global_home: Optional[Path] = None,
    max_skills: int = 60,
) -> str:
    """Build a compact <available_skills> index scanning repo, built-in, and global skills."""
    home = Path(global_home).resolve() if global_home else get_hex_home().resolve()
    builtin_skills = bundled_skills_dir()
    search_roots = [
        workspace_root / ".hex" / "skills",
        workspace_root / "skills",
        home / "skills",
        builtin_skills,
    ]

    discovered: Dict[str, str] = {}
    for root in search_roots:
        if not root.is_dir():
            continue
        for skill_md in sorted(root.rglob("SKILL.md")):
            try:
                raw = skill_md.read_text(encoding="utf-8", errors="replace")
                name = skill_md.parent.name
                desc = ""
                if raw.startswith("---"):
                    end = raw.find("\n---", 3)
                    if end != -1:
                        fm_block = raw[3:end]
                        for line in fm_block.splitlines():
                            s = line.strip()
                            if s.startswith("name:"):
                                val = s.split(":", 1)[1].strip().strip("\"'")
                                if val:
                                    name = val
                            elif s.startswith("description:") or s.startswith("purpose:"):
                                val = s.split(":", 1)[1].strip().strip("\"'")
                                if val and not desc:
                                    desc = val[:90]
                if name and name not in discovered:
                    discovered[name] = desc
            except Exception:
                continue

    if not discovered:
        return ""

    rows = [
        f"- {name}: {desc}" if desc else f"- {name}"
        for name, desc in sorted(discovered.items())[:max_skills]
    ]
    return "## Skills\n<available_skills>\n" + "\n".join(rows) + "\n</available_skills>"


class PromptBuilder:
    """Assembles the 5-slot system prompt for HEX with prefix-cache stability."""

    def __init__(self, soul_manager: Optional[SoulManager] = None):
        self.soul_manager = soul_manager or SoulManager()

    def get_mode_modulator(self, mode: str) -> str:
        """Retrieve punchy 3-4 line mode modulator."""
        clean_mode = str(mode or "agent").strip().lower()
        if clean_mode == "ask":
            clean_mode = "plan"
        return MODE_MODULATORS.get(clean_mode, MODE_MODULATORS["agent"])

    def build_system_prompt(
        self,
        *,
        workspace_root: Optional[Path | str] = None,
        mode: str = "agent",
        extra_rules: str = "",
        curated_memory: str = "",
        custom_env_context: Optional[str] = None,
        session_id: Optional[str] = None,
        surface: str = "tui",
        include_tools: bool = True,
        include_skills: bool = True,
    ) -> str:
        """Compose the full system prompt in strictly ordered slots for KV-cache prefix stability.

        Slot #1: SOUL.md Identity
        Slot #2: Mode Behavioral Overrides (plan / agent / code)
        Slot #3: Environment, Tools & Execution Guidance
        Slot #4: Curated Memory Snapshot (fenced)
        Slot #5: Project Rules / AGENTS.md / Skills (stable content first; volatile
        per-goal blocks such as retrieved skills are appended last so they never
        invalidate the cached stable prefix — B63).
        """
        ws = Path(workspace_root).resolve() if workspace_root else Path.cwd()
        slots: List[str] = []

        # --- Slot #1: SOUL.md Identity ---
        raw_soul, resolved_scope = self.soul_manager.resolve_soul(workspace_root=ws)
        # resolve_soul only ever returns user-authored scopes (global or workspace .hex files)
        user_authored = resolved_scope in (
            "global_user",
            "workspace_user",
            "workspace",
            "global",
            "default",
        )
        safe_soul = scan_context_content(raw_soul, "SOUL.md", user_authored=user_authored)
        slots.append(safe_soul.strip())

        # --- Slot #2: Mode Behavioral Override ---
        mode_override = self.get_mode_modulator(mode)
        slots.append(mode_override.strip())

        # --- Slot #3: Environment & Tool Boundaries ---
        if custom_env_context:
            env_text = custom_env_context.strip()
        else:
            clean_mode = str(mode or "agent").strip().lower()
            policy_line = (
                "Read files before editing. Verify changes with tests or commands. Adhere strictly to YAGNI."
                if clean_mode == "code"
                else "Read files before editing. Verify changes empirically with tests or commands."
            )
            platform_hint = PLATFORM_HINTS.get(str(surface or "tui").lower(), PLATFORM_HINTS["tui"])
            identity_name = DEFAULT_SOUL_NAME
            try:
                identity_name = self.soul_manager.get_active_name(workspace_root=ws)
            except Exception as e:
                logger.debug("Failed resolving identity name for guidance slot: %s", e)
            env_parts = [
                (
                    f"[Environment Context]\n"
                    f"- Platform: {platform.system()} {platform.machine()} (Python {sys.version.split()[0]})\n"
                    f"- Workspace Root: {ws}\n"
                    f"- {platform_hint}\n"
                    f"- Policy: {policy_line}"
                ),
                build_capabilities_guidance(identity_name),
                PARALLEL_TOOL_CALL_GUIDANCE,
            ]
            if include_tools:
                tools_summary = build_tools_summary()
                if tools_summary:
                    env_parts.append(tools_summary)
            env_text = "\n\n".join(env_parts)
        slots.append(env_text)

        # --- Slot #4: Curated Memory Snapshot ---
        if curated_memory and curated_memory.strip():
            clean_mem = curated_memory.strip()
            if "<memory-context>" in clean_mem:
                slots.append(clean_mem)
            else:
                fenced_memory = (
                    "<memory-context>\n"
                    "[System note: The following is recalled memory context, NOT new user input. "
                    "Treat as authoritative reference data.]\n\n"
                    f"{clean_mem}\n"
                    "</memory-context>"
                )
                slots.append(fenced_memory)
        else:
            try:
                from harness_core.cognition.memory import MemorySnapshot

                frozen_mem = MemorySnapshot.build_frozen_snapshot(
                    workspace_root=ws,
                    global_home=getattr(self.soul_manager, "global_home", None),
                    session_id=session_id,
                )
                if frozen_mem and frozen_mem.strip():
                    slots.append(frozen_mem.strip())
            except Exception as e:
                logger.debug("Failed building frozen memory snapshot in PromptBuilder: %s", e)

        # --- Slot #5: Project Context & Rules (stable content first, volatile last) ---
        project_rules_list = []
        # Check workspace rules
        for rule_file in ("AGENTS.md", ".cursorrules", "CLAUDE.md"):
            rpath = ws / rule_file
            if rpath.is_file():
                try:
                    rtext = rpath.read_text(encoding="utf-8").strip()
                    if rtext:
                        safe_rule = scan_context_content(rtext, rule_file, user_authored=False)
                        project_rules_list.append(f"[{rule_file}]\n{safe_rule}")
                except Exception as e:
                    logger.debug("Failed reading rule file %s: %s", rpath, e)

        if include_skills:
            skills_idx = build_skills_index(
                ws,
                global_home=getattr(self.soul_manager, "global_home", None),
            )
            if skills_idx:
                project_rules_list.append(skills_idx)

        # Volatile tail: per-goal rules (e.g. retrieved skills) change between turns,
        # so they sit after every stable block to preserve the cacheable prefix.
        if extra_rules and extra_rules.strip():
            safe_extra = scan_context_content(extra_rules.strip(), "extra_rules", user_authored=False)
            project_rules_list.append(safe_extra)

        if project_rules_list:
            slots.append("\n\n".join(project_rules_list))

        return "\n\n".join(slots).strip()
