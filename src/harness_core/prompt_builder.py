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

from .soul import SoulManager, SoulProvenance, get_hex_home

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

HEX_CAPABILITIES_GUIDANCE: str = (
    "[Capabilities & Interaction Guidance]\n"
    "You are HEX, an autonomous AI agent and pairing partner. When asked about your capabilities, what you can do, or available commands, provide an authoritative, natural, well-structured summary across what you can help with:\n\n"
    "• Code and Engineering: Read, search, and edit codebases, run terminal commands and test suites (Cargo, Pytest, Node, etc.), debug issues at root cause, and ship minimal, verified diffs.\n"
    "• Terminal, Automation & Workspace Tools: Execute shell commands and scripts, spawn isolated subagents for parallel tasks, manage background jobs, and leverage persistent memory and skills.\n"
    "• Codebase Navigation & Analysis: Search AST symbols, trace callers across dependency graphs, audit architectures, and eliminate dead code or unnecessary complexity.\n"
    "• Web, Docs & Technical Research: Search the web, fetch technical documentation, inspect APIs, research libraries, and synthesize papers or architecture proposals.\n"
    "• Data, Files & Workflows: Process structured data (JSON, YAML, CSV, SQL), inspect diffs, manage Git branches and commits, and automate build/release workflows.\n"
    "• Interactive Session Commands: /help (command list), /mode [plan|agent|code] (toggle behavior), /model [name] (view/switch model), /clear or /reset (fresh context), /quit (exit).\n\n"
    "Match reply length to the ask. Be direct, calm, and senior; avoid robotic disclaimers, filler, or repetitive chanting of acronyms. Conclude conversational welcomes with a natural ask like 'What are we working on?'"
)


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
        is_distribution_profile: bool = False,
        session_id: Optional[str] = None,
        soul_provenance: Optional[SoulProvenance | str] = None,
    ) -> str:
        """Compose the full system prompt in strictly ordered slots for KV-cache prefix stability.

        Slot #1: SOUL.md Identity
        Slot #2: Mode Behavioral Overrides (plan / agent / code)
        Slot #3: Environment & Tool Boundaries
        Slot #4: Curated Memory Snapshot (fenced)
        Slot #5: Project Rules / AGENTS.md / Skills
        """
        ws = Path(workspace_root).resolve() if workspace_root else Path.cwd()
        slots: List[str] = []

        # --- Slot #1: SOUL.md Identity ---
        raw_soul, resolved_scope = self.soul_manager.resolve_soul(workspace_root=ws)
        active_scope = soul_provenance or resolved_scope
        # Distinguish local user-authored workspace souls (.hex/SOUL.md) from untrusted distribution profiles
        user_authored = not is_distribution_profile and (
            active_scope in ("global_user", "workspace_user", "workspace", "global", "default")
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
            env_text = (
                f"[Environment Context]\n"
                f"- Platform: {platform.system()} {platform.machine()} (Python {sys.version.split()[0]})\n"
                f"- Workspace Root: {ws}\n"
                f"- Policy: Read files before editing. Verify changes with tests or commands. "
                f"Adhere strictly to YAGNI.\n\n"
                f"{HEX_CAPABILITIES_GUIDANCE}"
            )
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
                from .memory import MemorySnapshot

                frozen_mem = MemorySnapshot.build_frozen_snapshot(
                    workspace_root=ws,
                    global_home=getattr(self.soul_manager, "global_home", None),
                    session_id=session_id,
                )
                if frozen_mem and frozen_mem.strip():
                    slots.append(frozen_mem.strip())
            except Exception as e:
                logger.debug("Failed building frozen memory snapshot in PromptBuilder: %s", e)

        # --- Slot #5: Project Context & Rules ---
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

        if extra_rules and extra_rules.strip():
            safe_extra = scan_context_content(extra_rules.strip(), "extra_rules", user_authored=False)
            project_rules_list.append(safe_extra)

        if project_rules_list:
            slots.append("\n\n".join(project_rules_list))

        return "\n\n".join(slots).strip()
