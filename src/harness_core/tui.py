"""Interactive Terminal UI (TUI) Engine for HEX Autonomous AI Workbench (PRD Section 8, Pillar 2).

Provides:
- Rich / 24-bit TrueColor ANSI terminal compatibility with zero-dependency ANSI fallbacks.
- Hermes/OpenCode-grade Retro-Block Gradient Banner + Framed Two-Column Capability Splash Card
  (Braille Caduceus/Hex emblem + model/workspace/session info on left; categorized Available Tools
  & Available Skills with NVIDIA SkillSpector badges on right).
- Data-Driven Skin / Theme Engine (`SKIN_REGISTRY`: `amber`, `cyberpunk`, `matrix`, `nord`, `mono`).
- Persistent bottom status bar (`render_status_bar`) with active model, context compression ratio,
  visual budget progress bar (`[████░░░░░░]`), 0ms replay counter, and elapsed session timer.
- Animated `ActivitySpinner` and `┊` vertical tool-call activity feed (`render_activity_feed`).
- Unified (`render_unified_diff`) and side-by-side (`render_side_by_side_diff`) colorized diff visualizers.
- Active Graphify AST blast-radius tree rendering (`render_blast_radius_tree`) with risk badges.
- Streaming token renderer (`StreamingTokenRenderer`) with Markdown & code-fence highlighting.
- `HexConsole`: Interactive pair-programming REPL with `readline` tab-completion, 20+ slash commands,
  and interactive keyboard approval gate (`[y/n/d/s/q]`).
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any, Callable, TextIO

from .gateway import ApprovalRequiredInterrupt, redact_secrets
from .voice import VoiceConfig, VoiceEngine, clean_text_for_speech

# Optional rich imports for enhanced terminal aesthetics
try:
    import rich
    from rich.console import Console as RichConsole
    from rich.markdown import Markdown as RichMarkdown
    from rich.panel import Panel as RichPanel
    from rich.syntax import Syntax as RichSyntax
    from rich.table import Table as RichTable
    from rich.tree import Tree as RichTree
    HAS_RICH = True
except ImportError:
    HAS_RICH = False


# Standard ANSI terminal escape sequences
ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_DIM = "\033[2m"
ANSI_ITALIC = "\033[3m"
ANSI_RED = "\033[31m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_BLUE = "\033[34m"
ANSI_MAGENTA = "\033[35m"
ANSI_CYAN = "\033[36m"
ANSI_WHITE = "\033[37m"

ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def fg_rgb(r: int, g: int, b: int) -> str:
    """Return 24-bit TrueColor foreground ANSI escape sequence."""
    return f"\033[38;2;{r};{g};{b}m"


def bg_rgb(r: int, g: int, b: int) -> str:
    """Return 24-bit TrueColor background ANSI escape sequence."""
    return f"\033[48;2;{r};{g};{b}m"


def colorize_rgb(
    text: str,
    fg: tuple[int, int, int] | None = None,
    bg: tuple[int, int, int] | None = None,
    bold: bool = False,
    dim: bool = False,
    enabled: bool = True,
) -> str:
    if not enabled:
        return text
    codes: list[str] = []
    if bold:
        codes.append(ANSI_BOLD)
    if dim:
        codes.append(ANSI_DIM)
    if fg is not None:
        codes.append(fg_rgb(*fg))
    if bg is not None:
        codes.append(bg_rgb(*bg))
    if not codes:
        return text
    return f"{''.join(codes)}{text}{ANSI_RESET}"


def strip_ansi(text: str) -> str:
    """Strip ANSI escape sequences from text for accurate column width calculations."""
    return ANSI_ESCAPE_RE.sub("", text)


def _pad_visible(content: str, width: int, align: str = "left") -> str:
    """Pad content with spaces to match visible character width."""
    vis_len = len(strip_ansi(content))
    pad = max(0, width - vis_len)
    if align == "right":
        return (" " * pad) + content
    if align == "center":
        left = pad // 2
        right = pad - left
        return (" " * left) + content + (" " * right)
    return content + (" " * pad)


def _truncate_visible(text: str, max_width: int, ellipsis: str = "...") -> str:
    plain = strip_ansi(text)
    if len(plain) <= max_width:
        return text
    if max_width <= len(ellipsis):
        return plain[:max_width]
    return plain[: max_width - len(ellipsis)] + ellipsis


# ==============================================================================
# Data-Driven Skin / Theme Engine (`SKIN_REGISTRY`)
# ==============================================================================


@dataclass(frozen=True)
class TUISkin:
    name: str
    description: str
    banner_gradient: tuple[tuple[int, int, int], ...]
    border_rgb: tuple[int, int, int]
    heading_rgb: tuple[int, int, int]
    accent_rgb: tuple[int, int, int]
    category_rgb: tuple[int, int, int]
    text_rgb: tuple[int, int, int]
    muted_rgb: tuple[int, int, int]
    success_rgb: tuple[int, int, int]
    warn_rgb: tuple[int, int, int]
    danger_rgb: tuple[int, int, int]
    status_bg_rgb: tuple[int, int, int]
    spinner_faces: tuple[str, ...] = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
    spinner_verbs: tuple[str, ...] = ("Thinking", "Analyzing", "Exploring", "Formulating", "Reflecting", "Synthesizing")
    prompt_symbol: str = "⚕"


SKIN_REGISTRY: dict[str, TUISkin] = {
    "amber": TUISkin(
        name="amber",
        description="Hermes-style warm gold, amber, and burnished bronze gradient",
        banner_gradient=(
            (255, 215, 0),    # Bright gold
            (245, 185, 15),   # Warm yellow-gold
            (235, 150, 20),   # Rich amber
            (217, 119, 24),   # Deep orange-amber
            (194, 89, 22),    # Burnished copper
            (168, 68, 20),    # Bronze shadow
        ),
        border_rgb=(180, 118, 42),
        heading_rgb=(250, 204, 21),
        accent_rgb=(245, 158, 11),
        category_rgb=(186, 135, 58),
        text_rgb=(243, 244, 246),
        muted_rgb=(138, 129, 115),
        success_rgb=(52, 211, 153),
        warn_rgb=(251, 191, 36),
        danger_rgb=(248, 113, 113),
        status_bg_rgb=(28, 25, 23),
        prompt_symbol="⚕",
    ),
    "cyberpunk": TUISkin(
        name="cyberpunk",
        description="High-contrast neon cyan, electric magenta, and violet grid",
        banner_gradient=(
            (0, 245, 255),
            (35, 205, 255),
            (95, 155, 255),
            (160, 105, 255),
            (220, 65, 255),
            (255, 40, 180),
        ),
        border_rgb=(0, 195, 215),
        heading_rgb=(0, 245, 255),
        accent_rgb=(244, 114, 182),
        category_rgb=(56, 189, 248),
        text_rgb=(241, 245, 249),
        muted_rgb=(100, 116, 139),
        success_rgb=(16, 185, 129),
        warn_rgb=(250, 204, 21),
        danger_rgb=(244, 63, 94),
        status_bg_rgb=(15, 23, 42),
        spinner_faces=("⚡", "◈", "◇", "◆", "◈", "⚡"),
        prompt_symbol="⚡",
    ),
    "matrix": TUISkin(
        name="matrix",
        description="Terminal phosphor emerald and deep jade monochrome",
        banner_gradient=(
            (167, 243, 208),
            (110, 231, 183),
            (52, 211, 153),
            (16, 185, 129),
            (5, 150, 105),
            (4, 120, 87),
        ),
        border_rgb=(16, 185, 129),
        heading_rgb=(110, 231, 183),
        accent_rgb=(52, 211, 153),
        category_rgb=(5, 150, 105),
        text_rgb=(209, 250, 229),
        muted_rgb=(75, 135, 105),
        success_rgb=(52, 211, 153),
        warn_rgb=(250, 204, 21),
        danger_rgb=(248, 113, 113),
        status_bg_rgb=(6, 28, 20),
        spinner_faces=("◢", "◣", "◤", "◥"),
        prompt_symbol="⌘",
    ),
    "nord": TUISkin(
        name="nord",
        description="Arctic frost blue, glacier cyan, and polar slate",
        banner_gradient=(
            (143, 188, 187),
            (136, 192, 208),
            (129, 161, 193),
            (94, 129, 172),
            (76, 86, 106),
            (67, 76, 94),
        ),
        border_rgb=(129, 161, 193),
        heading_rgb=(136, 192, 208),
        accent_rgb=(163, 190, 140),
        category_rgb=(129, 161, 193),
        text_rgb=(236, 239, 244),
        muted_rgb=(125, 138, 161),
        success_rgb=(163, 190, 140),
        warn_rgb=(235, 203, 139),
        danger_rgb=(191, 97, 106),
        status_bg_rgb=(46, 52, 64),
        spinner_faces=("❄", "❅", "❆", "❅"),
        prompt_symbol="❄",
    ),
    "mono": TUISkin(
        name="mono",
        description="Clean high-contrast grayscale for minimal or CI terminals",
        banner_gradient=(
            (255, 255, 255),
            (230, 230, 230),
            (200, 200, 200),
            (170, 170, 170),
            (140, 140, 140),
            (110, 110, 110),
        ),
        border_rgb=(140, 140, 140),
        heading_rgb=(255, 255, 255),
        accent_rgb=(220, 220, 220),
        category_rgb=(180, 180, 180),
        text_rgb=(240, 240, 240),
        muted_rgb=(130, 130, 130),
        success_rgb=(200, 200, 200),
        warn_rgb=(180, 180, 180),
        danger_rgb=(255, 255, 255),
        status_bg_rgb=(30, 30, 30),
        spinner_faces=("-", "\\", "|", "/"),
        prompt_symbol=">",
    ),
}


# ==============================================================================
# Retro-Block Title Banner & Braille Caduceus/Hex Emblem
# ==============================================================================

HERMES_STYLE_BANNER_FULL: tuple[str, ...] = (
    "██╗  ██╗███████╗██╗  ██╗          █████╗  ██████╗ ███████╗███╗   ██╗████████╗",
    "██║  ██║██╔════╝╚██╗██╔╝         ██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝",
    "███████║█████╗   ╚███╔╝  █████╗  ███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║   ",
    "██╔══██║██╔══╝   ██╔██╗  ╚════╝  ██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║   ",
    "██║  ██║███████╗██╔╝ ██╗         ██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║   ",
    "╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝         ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝   ",
)

HERMES_STYLE_BANNER_COMPACT: tuple[str, ...] = HERMES_STYLE_BANNER_FULL

# Geometric 3D Isometric Hex-Core & Circuit Emblem (purpose-built for HEX)
EMBLEM_ART: tuple[str, ...] = (
    "            ▄▄▀▀▀▀▀▀▄▄            ",
    "         ▄▀▀  ▄▄▄▄▄▄  ▀▀▄         ",
    "      ▄▀▀  ▄▀▀      ▀▀▄  ▀▀▄      ",
    "    ▄▀   ▄▀    ▄▄▄▄    ▀▄   ▀▄    ",
    "   ▐▌   ▐▌   ▄▀    ▀▄   ▐▌   ▐▌   ",
    "   ▐▌   ▐▌  ▐▌  ██  ▐▌  ▐▌   ▐▌   ",
    "   ▐▌   ▐▌  ▐▌ ████ ▐▌  ▐▌   ▐▌   ",
    "   ▐▌   ▐▌   ▀▄ ▀▀ ▄▀   ▐▌   ▐▌   ",
    "    ▀▄   ▀▄    ▀▀▀▀    ▄▀   ▄▀    ",
    "      ▀▄▄  ▀▄▄      ▄▄▀  ▄▄▀      ",
    "        ▐▌ ▀▀▄▄▀▀▀▀▄▄▀▀ ▐▌        ",
    "        ▐▌     ▀▀▀▀     ▐▌        ",
    "         ▀▄▄          ▄▄▀         ",
    "           ▀▀▄▄    ▄▄▀▀           ",
    "              ▀▀▀▀▀▀              ",
    "         ───═══╬═══───            ",
)

TOOL_CATEGORY_MAP: dict[str, str] = {
    "read_file": "file",
    "write_file": "file",
    "list_files": "file",
    "query_graphify": "ast_graph",
    "web_search": "search",
    "lsp_diagnostics": "lsp_compiler",
    "lsp_definition": "lsp_compiler",
    "lsp_references": "lsp_compiler",
    "lsp_hover": "lsp_compiler",
    "run_tests": "verification",
    "git_push_remote": "git_ops",
    "modify_system_policy": "policy_guard",
}

BUILTIN_SKILL_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("architecture", ("active-graphify-blast-radius", "cow-snapshot-branching", "0ms-journal-replay")),
    ("autonomous-repair", ("multi-turn-repair-loop", "speculative-tree-of-work", "dynamic-dag-replan")),
    ("security-gates", ("nvidia-skillspector-static", "osv-cve-scanner", "local-semantic-alignment")),
    ("model-routing", ("tier1-local-compressor", "tier2-cloud-prefix-cache", "privacy-local-pinning")),
    ("distributed-ops", ("cas-fencing-leases", "cow-delta-tar-sync", "telegram-discord-webhooks")),
)


# ==============================================================================
# Diff Visualizers
# ==============================================================================


def render_unified_diff(diff_text: str, title: str = "Proposed Changes", width: int = 80) -> str:
    """Render unified diff text with colorized additions, deletions, chunk headers, and file headers.

    - Additions (+) in green
    - Deletions (-) in red
    - Hunk headers (@@) in cyan
    - File headers (---, +++) in bold yellow
    """
    if not diff_text or not diff_text.strip():
        return f"{ANSI_DIM}--- {title}: (no changes) ---{ANSI_RESET}"

    lines = diff_text.splitlines()
    out: list[str] = []
    inner_width = max(20, width - 4)

    header_border = "─" * max(5, inner_width - len(title) - 4)
    out.append(f"{ANSI_BOLD}{ANSI_CYAN}┌── {title} {header_border}┐{ANSI_RESET}")

    for line in lines:
        if line.startswith("---") or line.startswith("+++"):
            styled = f"{ANSI_BOLD}{ANSI_YELLOW}{line}{ANSI_RESET}"
        elif line.startswith("@@"):
            styled = f"{ANSI_CYAN}{line}{ANSI_RESET}"
        elif line.startswith("+"):
            styled = f"{ANSI_GREEN}{line}{ANSI_RESET}"
        elif line.startswith("-"):
            styled = f"{ANSI_RED}{line}{ANSI_RESET}"
        else:
            styled = f"{ANSI_DIM}{line}{ANSI_RESET}"
        out.append(f"{ANSI_CYAN}│{ANSI_RESET} {_pad_visible(styled, inner_width)} {ANSI_CYAN}│{ANSI_RESET}")

    out.append(f"{ANSI_BOLD}{ANSI_CYAN}└──" + ("─" * (inner_width + 2)) + f"┘{ANSI_RESET}")
    return "\n".join(out)


def render_side_by_side_diff(
    old_text: str,
    new_text: str,
    filename: str = "file",
    width: int = 80,
) -> str:
    """Render two-column side-by-side diff with vertical divider and colored modifications."""
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()

    col_w = max(20, (width - 7) // 2)
    header_old = _pad_visible(f"Old: {filename}"[:col_w], col_w)
    header_new = _pad_visible(f"New: {filename}"[:col_w], col_w)

    out: list[str] = []
    out.append(f"{ANSI_BOLD}{ANSI_CYAN}┌─ {header_old} ┬─ {header_new} ─┐{ANSI_RESET}")

    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for i, j in zip(range(i1, i2), range(j1, j2)):
                l = _pad_visible(old_lines[i][:col_w], col_w)
                r = _pad_visible(new_lines[j][:col_w], col_w)
                out.append(f"{ANSI_CYAN}│{ANSI_RESET} {l} {ANSI_CYAN}│{ANSI_RESET} {r} {ANSI_CYAN}│{ANSI_RESET}")
        elif tag == "replace":
            max_len = max(i2 - i1, j2 - j1)
            for k in range(max_len):
                if i1 + k < i2:
                    raw_l = _pad_visible(old_lines[i1 + k][:col_w], col_w)
                    l = f"{ANSI_RED}{raw_l}{ANSI_RESET}"
                else:
                    l = " " * col_w
                if j1 + k < j2:
                    raw_r = _pad_visible(new_lines[j1 + k][:col_w], col_w)
                    r = f"{ANSI_GREEN}{raw_r}{ANSI_RESET}"
                else:
                    r = " " * col_w
                out.append(f"{ANSI_CYAN}│{ANSI_RESET} {l} {ANSI_CYAN}│{ANSI_RESET} {r} {ANSI_CYAN}│{ANSI_RESET}")
        elif tag == "delete":
            for i in range(i1, i2):
                raw_l = _pad_visible(old_lines[i][:col_w], col_w)
                l = f"{ANSI_RED}{raw_l}{ANSI_RESET}"
                r = " " * col_w
                out.append(f"{ANSI_CYAN}│{ANSI_RESET} {l} {ANSI_CYAN}│{ANSI_RESET} {r} {ANSI_CYAN}│{ANSI_RESET}")
        elif tag == "insert":
            for j in range(j1, j2):
                l = " " * col_w
                raw_r = _pad_visible(new_lines[j][:col_w], col_w)
                r = f"{ANSI_GREEN}{raw_r}{ANSI_RESET}"
                out.append(f"{ANSI_CYAN}│{ANSI_RESET} {l} {ANSI_CYAN}│{ANSI_RESET} {r} {ANSI_CYAN}│{ANSI_RESET}")

    bottom_col = "─" * (col_w + 2)
    out.append(f"{ANSI_BOLD}{ANSI_CYAN}└{bottom_col}┴{bottom_col}┘{ANSI_RESET}")
    return "\n".join(out)


# ==============================================================================
# Active Graphify AST Blast-Radius Tree Renderer
# ==============================================================================

RISK_BADGES: dict[str, str] = {
    "R0": f"{ANSI_BOLD}{ANSI_GREEN}[R0 - No Risk / Read Only]{ANSI_RESET}",
    "R1": f"{ANSI_BOLD}{ANSI_GREEN}[R1 - Low Risk]{ANSI_RESET}",
    "R2": f"{ANSI_BOLD}{ANSI_YELLOW}[R2 - Medium Risk]{ANSI_RESET}",
    "R3": f"{ANSI_BOLD}{ANSI_MAGENTA}[R3 - High Risk]{ANSI_RESET}",
    "R4": f"{ANSI_BOLD}{ANSI_RED}[R4 - Critical Risk]{ANSI_RESET}",
}


def render_blast_radius_tree(blast_radius_data: dict[str, Any]) -> str:
    """Render hierarchical tree representing Active Graphify AST impact matrix and test pinning."""
    risk_raw = str(blast_radius_data.get("risk_class", "R1")).upper().strip()
    badge_key = risk_raw[:2] if risk_raw[:2] in RISK_BADGES else "R1"
    badge = RISK_BADGES.get(badge_key, f"{ANSI_BOLD}{ANSI_CYAN}[{risk_raw}]{ANSI_RESET}")

    modified = (
        blast_radius_data.get("modified_files")
        or blast_radius_data.get("modified_symbols")
        or []
    )
    direct = (
        blast_radius_data.get("direct_callers")
        or blast_radius_data.get("1_hop_callers")
        or [d.get("qualified_name", str(d)) if isinstance(d, dict) else str(d) for d in (blast_radius_data.get("hop1_dependents") or [])]
    )
    transitive = (
        blast_radius_data.get("transitive_callers")
        or blast_radius_data.get("2_hop_callers")
        or [d.get("qualified_name", str(d)) if isinstance(d, dict) else str(d) for d in (blast_radius_data.get("hop2_dependents") or [])]
    )
    pinned = (
        blast_radius_data.get("pinned_tests")
        or blast_radius_data.get("impacted_tests")
        or []
    )

    sections = [
        ("📁 Modified Files / Symbols", modified),
        ("🔗 1-Hop Direct Callers", direct),
        ("🌐 2-Hop Transitive Callers", transitive),
        ("🧪 Pinned Verification Tests", pinned),
    ]

    out: list[str] = [
        f"{ANSI_BOLD}Active Graphify AST Impact Matrix{ANSI_RESET} {badge}"
    ]

    for sec_idx, (title, items) in enumerate(sections):
        is_last_sec = sec_idx == len(sections) - 1
        branch = "└── " if is_last_sec else "├── "
        child_indent = "    " if is_last_sec else "│   "

        count_badge = f"{ANSI_DIM}({len(items)}){ANSI_RESET}"
        out.append(f"{branch}{ANSI_BOLD}{title}{ANSI_RESET} {count_badge}")

        if not items:
            out.append(f"{child_indent}└── {ANSI_DIM}(none){ANSI_RESET}")
        else:
            for item_idx, item in enumerate(items):
                is_last_item = item_idx == len(items) - 1
                item_branch = "└── " if is_last_item else "├── "
                out.append(f"{child_indent}{item_branch}{item}")

    return "\n".join(out)


def format_terminal_markdown(text: str, skin: TUISkin | None = None) -> str:
    """Renders GitHub-flavored Markdown text with terminal ANSI formatting, skin colors, and code styling."""
    if not text:
        return ""

    sk = skin or SKIN_REGISTRY.get("amber")
    heading_c = sk.heading_rgb if sk else (250, 204, 21)
    accent_c = sk.accent_rgb if sk else (245, 158, 11)
    cat_c = sk.category_rgb if sk else (186, 135, 58)

    lines = text.split("\n")
    out: list[str] = []
    in_code = False
    code_lang = ""

    for line in lines:
        stripped = line.strip()

        # Fenced code block open/close
        if stripped.startswith("```"):
            if not in_code:
                in_code = True
                code_lang = stripped[3:].strip()
                tag = f" [{code_lang}] " if code_lang else " "
                bar_len = max(0, 60 - len(tag) - 3)
                out.append(f"{ANSI_DIM}┌──{tag}{'─' * bar_len}{ANSI_RESET}")
            else:
                in_code = False
                out.append(f"{ANSI_DIM}└──{'─' * 60}{ANSI_RESET}")
            continue

        if in_code:
            out.append(f"{ANSI_DIM}│{ANSI_RESET} {ANSI_CYAN}{line}{ANSI_RESET}")
            continue

        # Horizontal rule
        if stripped in ("---", "***", "___") or (len(stripped) >= 3 and set(stripped) in ({"-"}, {"*"}, {"_"})):
            out.append(f"{ANSI_DIM}{'─' * 64}{ANSI_RESET}")
            continue

        # Blockquote
        if stripped.startswith(">"):
            quote_text = stripped[1:].strip()
            quote_text = re.sub(r"`([^`]+)`", f"{ANSI_CYAN}\\1{ANSI_RESET}", quote_text)
            quote_text = re.sub(r"\*\*([^*]+)\*\*", f"{ANSI_BOLD}\\1{ANSI_RESET}", quote_text)
            out.append(f"  {colorize_rgb('│', fg=accent_c)} \033[3m{quote_text}\033[0m")
            continue

        # Headings (# H1, ## H2, ### H3, #### H4)
        h_match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if h_match:
            level = len(h_match.group(1))
            title = h_match.group(2)
            title = re.sub(r"`([^`]+)`", f"{ANSI_CYAN}\\1{ANSI_RESET}", title)
            title = re.sub(r"\*\*([^*]+)\*\*", f"{ANSI_BOLD}\\1{ANSI_RESET}", title)
            if level == 1:
                hdr_icon = colorize_rgb("■", fg=heading_c, bold=True)
                hdr_txt = colorize_rgb(title.upper(), fg=heading_c, bold=True)
                out.append(f"\n{hdr_icon} {hdr_txt}")
            elif level == 2:
                hdr_icon = colorize_rgb("◆", fg=accent_c, bold=True)
                hdr_txt = colorize_rgb(title, fg=accent_c, bold=True)
                out.append(f"\n{hdr_icon} {hdr_txt}")
            else:
                hdr_icon = colorize_rgb("●", fg=cat_c, bold=True)
                hdr_txt = f"{ANSI_BOLD}{title}{ANSI_RESET}"
                out.append(f"\n{hdr_icon} {hdr_txt}")
            continue

        # Unordered bullet list (- item, * item, + item)
        bullet_match = re.match(r"^(\s*)[-*+]\s+(.+)$", line)
        if bullet_match:
            indent = bullet_match.group(1)
            content = bullet_match.group(2)
            content = re.sub(r"`([^`]+)`", f"{ANSI_CYAN}\\1{ANSI_RESET}", content)
            content = re.sub(r"\*\*([^*]+)\*\*", f"{ANSI_BOLD}\\1{ANSI_RESET}", content)
            content = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", f"\033[3m\\1\033[0m", content)
            bullet_sym = colorize_rgb("•", fg=accent_c)
            out.append(f"{indent}  {bullet_sym} {content}")
            continue

        # Ordered numbered list (1. item, 2. item)
        num_match = re.match(r"^(\s*)(\d+)\.\s+(.+)$", line)
        if num_match:
            indent = num_match.group(1)
            num = num_match.group(2)
            content = num_match.group(3)
            content = re.sub(r"`([^`]+)`", f"{ANSI_CYAN}\\1{ANSI_RESET}", content)
            content = re.sub(r"\*\*([^*]+)\*\*", f"{ANSI_BOLD}\\1{ANSI_RESET}", content)
            content = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", f"\033[3m\\1\033[0m", content)
            out.append(f"{indent}  {ANSI_BOLD}{num}.{ANSI_RESET} {content}")
            continue

        # Normal prose: inline formatting
        l = line
        l = re.sub(r"`([^`]+)`", f"{ANSI_CYAN}\\1{ANSI_RESET}", l)
        l = re.sub(r"\*\*([^*]+)\*\*", f"{ANSI_BOLD}\\1{ANSI_RESET}", l)
        l = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", f"\033[3m\\1\033[0m", l)
        out.append(l)

    return "\n".join(out)


# ==============================================================================
# Streaming Token Renderer & Animated Spinner
# ==============================================================================


class StreamingTokenRenderer:
    """Progressive token accumulator and renderer with code fence detection and syntax highlighting."""

    def __init__(self, output_stream: TextIO | None = None, enable_color: bool = True):
        self.output_stream = output_stream
        self.enable_color = enable_color
        self._tokens: list[str] = []
        self._in_code_block = False

    def feed(self, token: str) -> None:
        """Feed a progressive token chunk."""
        self._tokens.append(token)
        if "```" in token:
            self._in_code_block = not self._in_code_block

        if self.enable_color and self._in_code_block:
            rendered = f"{ANSI_CYAN}{token}{ANSI_RESET}"
        else:
            rendered = token

        if self.output_stream is not None:
            self.output_stream.write(rendered)
            if hasattr(self.output_stream, "flush"):
                try:
                    self.output_stream.flush()
                except Exception:
                    pass

    def flush(self) -> str:
        """Flush and return full accumulated content string."""
        if self.output_stream is not None and hasattr(self.output_stream, "flush"):
            try:
                self.output_stream.flush()
            except Exception:
                pass
        return "".join(self._tokens)

    def get_content(self) -> str:
        """Return all accumulated tokens as a single string."""
        return "".join(self._tokens)


BRAILLE_SHIMMER: tuple[str, ...] = ("⠴", "⠦", "⠧", "⠇", "⠏", "⠋", "⠙", "⠹", "⠸", "⠼")


class ActivitySpinner:
    """Animated Claude-style terminal activity spinner with smooth braille morphing transitions."""

    def __init__(self, skin: TUISkin | None = None, out: TextIO = sys.stdout, enabled: bool = True) -> None:
        self.skin = skin or SKIN_REGISTRY["amber"]
        self.out = out
        self.enabled = enabled and hasattr(out, "isatty") and out.isatty()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._label: str = ""
        self._start_ts: float = 0.0

    def start(self, label: str = "") -> None:
        self._label = label
        self._start_ts = time.perf_counter()
        if not self.enabled:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._spin_loop, daemon=True)
        self._thread.start()

    def _spin_loop(self) -> None:
        idx = 0
        faces = self.skin.spinner_faces or ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
        verbs = self.skin.spinner_verbs or ("Thinking", "Analyzing", "Exploring", "Formulating", "Reflecting", "Synthesizing")
        phrases = [v if v.endswith("...") else f"{v}..." for v in verbs]

        phrase_duration = 3.5
        transition_duration = 0.35
        cycle_duration = phrase_duration + transition_duration

        while not self._stop_event.is_set():
            elapsed = time.perf_counter() - self._start_ts
            face = faces[idx % len(faces)]

            cycle_idx = int(elapsed // cycle_duration)
            curr_phrase = phrases[cycle_idx % len(phrases)]
            next_phrase = phrases[(cycle_idx + 1) % len(phrases)]
            phase_time = elapsed % cycle_duration

            if phase_time < phrase_duration:
                text = curr_phrase
            else:
                t = (phase_time - phrase_duration) / transition_duration
                max_len = max(len(curr_phrase), len(next_phrase))
                chars = []
                for i in range(max_len):
                    cp = t * (max_len + 4) - 2 - i
                    if cp < 0:
                        chars.append(curr_phrase[i] if i < len(curr_phrase) else " ")
                    elif cp < 3:
                        chars.append(BRAILLE_SHIMMER[(idx + i) % len(BRAILLE_SHIMMER)])
                    else:
                        chars.append(next_phrase[i] if i < len(next_phrase) else " ")
                text = "".join(chars).rstrip()

            extra = f" {self._label}" if self._label else ""
            line = (
                f"\r  {colorize_rgb(face, fg=self.skin.heading_rgb, bold=True)} "
                f"{colorize_rgb(text, fg=self.skin.accent_rgb, bold=True)}{extra} "
                f"{colorize_rgb(f'({elapsed:.1f}s)', fg=self.skin.muted_rgb)}   \033[K"
            )
            try:
                self.out.write(line)
                self.out.flush()
            except Exception:
                break
            idx += 1
            self._stop_event.wait(0.08)

    def stop(self) -> None:
        if not self.enabled:
            return
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.5)
        try:
            self.out.write("\r" + (" " * 96) + "\r\033[K")
            self.out.flush()
        except Exception:
            pass


# ==============================================================================
# Session Banners (Compact 78-col + Hermes-Style Full Two-Column Splash)
# ==============================================================================


def render_session_banner(
    model_id: str = "claude-3-5-sonnet",
    compressor_mode: str = "M4 MLX Local",
    sandbox_backend: str = "bwrap",
    cow_backend: str = "apfs",
    voice_status: str | None = None,
    width: int = 78,
) -> str:
    """Render the compact 78-column HEX pair-programming session header banner."""
    title = "HEX Autonomous AI Workbench — Interactive Terminal Console"
    model_source = os.getenv("HEX_MODEL_SOURCE")
    source_suffix = f"  │  Source: {model_source}" if model_source else ""
    line1 = f"Model: {model_id}{source_suffix}  │  Compressor: {compressor_mode}"
    if voice_status:
        line2 = f"Sandbox: {sandbox_backend}  │  CoW Store: {cow_backend}  │  Voice: {voice_status}"
    else:
        line2 = f"Sandbox: {sandbox_backend}  │  CoW Store: {cow_backend}"
    hint = "Type /help for slash commands, or enter a natural language prompt."

    inner_w = width - 2
    t_pad = _pad_visible(f"  {ANSI_YELLOW}⚡ {ANSI_BOLD}{_truncate_visible(title, inner_w - 4)}{ANSI_RESET}", inner_w)
    l1_pad = _pad_visible(f"  {ANSI_WHITE}{_truncate_visible(line1, inner_w - 4)}{ANSI_RESET}", inner_w)
    l2_pad = _pad_visible(f"  {ANSI_WHITE}{_truncate_visible(line2, inner_w - 4)}{ANSI_RESET}", inner_w)
    h_pad = _pad_visible(f"  {ANSI_DIM}{_truncate_visible(hint, inner_w - 4)}{ANSI_RESET}", inner_w)

    border_top = "═" * (width - 2)
    border_bot = "═" * (width - 2)

    out = [
        f"{ANSI_BOLD}{ANSI_CYAN}╔{border_top}╗{ANSI_RESET}",
        f"{ANSI_CYAN}║{ANSI_RESET}{t_pad}{ANSI_CYAN}║{ANSI_RESET}",
        f"{ANSI_CYAN}║{ANSI_RESET}{l1_pad}{ANSI_CYAN}║{ANSI_RESET}",
        f"{ANSI_CYAN}║{ANSI_RESET}{l2_pad}{ANSI_CYAN}║{ANSI_RESET}",
        f"{ANSI_CYAN}║{ANSI_RESET}{h_pad}{ANSI_CYAN}║{ANSI_RESET}",
        f"{ANSI_BOLD}{ANSI_CYAN}╚{border_bot}╝{ANSI_RESET}",
    ]
    return "\n".join(out)


def render_hermes_splash(
    orchestrator: Any = None,
    *,
    skin_name: str = "amber",
    model_id: str = "gemini-2.5-pro",
    compressor_mode: str = "deterministic",
    workspace_root: Path | str | None = None,
    session_id: str | None = None,
    voice_status: str | None = None,
    width: int = 108,
    enable_color: bool = True,
) -> str:
    """Render the full Hermes/OpenCode-style retro-block gradient banner and two-column framed splash card."""
    skin = SKIN_REGISTRY.get(skin_name.lower(), SKIN_REGISTRY["amber"])
    total_w = max(80, width)
    inner_w = total_w - 2
    left_w = max(32, min(38, inner_w // 3))
    right_w = inner_w - left_w - 3

    def _c(txt: str, fg: tuple[int, int, int] | None = None, bg: tuple[int, int, int] | None = None, bold: bool = False) -> str:
        return colorize_rgb(txt, fg=fg, bg=bg, bold=bold, enabled=enable_color)

    # 1. Retro-block gradient title art
    art_lines = HERMES_STYLE_BANNER_FULL if total_w >= 108 else HERMES_STYLE_BANNER_COMPACT
    grad = skin.banner_gradient
    banner_rows = [
        _c(_pad_visible(line, total_w, align="center"), fg=grad[min(idx, len(grad) - 1)], bold=True)
        for idx, line in enumerate(art_lines)
    ]

    # 2. Top border with version + upstream git commit SHA
    ws_path = Path(workspace_root or (getattr(orchestrator, "default_workspace", None) or Path.cwd())).resolve()
    git_sha = "local-cow"
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--short=7", "HEAD"],
            cwd=str(ws_path),
            capture_output=True,
            text=True,
            timeout=1.5,
        )
        if res.returncode == 0 and res.stdout.strip():
            git_sha = res.stdout.strip()
    except Exception:
        pass

    try:
        import importlib.metadata
        pkg_version = importlib.metadata.version("hex-ai")
    except Exception:
        pkg_version = "0.1.0"

    title_plain = f" HEX Autonomous AI Workbench v{pkg_version} · upstream {git_sha} "
    dash_total = max(2, inner_w - len(title_plain))
    left_d = dash_total // 2
    right_d = dash_total - left_d
    top_border = (
        _c("┌" + ("─" * left_d), fg=skin.border_rgb)
        + _c(title_plain, fg=skin.heading_rgb, bold=True)
        + _c(("─" * right_d) + "┐", fg=skin.border_rgb)
    )
    sub_underline = (
        _c("│", fg=skin.border_rgb)
        + (" " * left_d)
        + _c("╌" * len(title_plain), fg=skin.muted_rgb)
        + (" " * right_d)
        + _c("│", fg=skin.border_rgb)
    )

    # 3. Left column: Stipple Caduceus/Hex Emblem + Model + Workspace + Session ID
    left_lines: list[str] = [""]
    for idx, eline in enumerate(EMBLEM_ART):
        rgb = grad[min(idx // 3, len(grad) - 1)]
        left_lines.append(_c(_pad_visible(eline.strip(), left_w, align="center"), fg=rgb, bold=True))
    left_lines.append("")

    from .onboard import HardwareProfile
    prof = HardwareProfile.detect()
    hw_label = f"Apple Silicon ({prof.machine_model})" if prof.is_apple_silicon else (f"NVIDIA CUDA ({prof.machine_model})" if prof.has_cuda else "Host CPU")
    comp_display = "local AI" if compressor_mode == "local" else compressor_mode
    mdl_line = f"{_c(model_id, fg=skin.heading_rgb, bold=True)}{_c(' · ', fg=skin.muted_rgb)}{_c(f'{hw_label} ({comp_display})', fg=skin.category_rgb)}"
    left_lines.append(_pad_visible(mdl_line, left_w, align="center"))

    home_str = str(Path.home())
    ws_disp = str(ws_path)
    if ws_disp.startswith(home_str):
        ws_disp = "~" + ws_disp[len(home_str):]
    ws_disp = _truncate_visible(ws_disp, left_w - 2)
    left_lines.append(_pad_visible(_c(ws_disp, fg=skin.category_rgb), left_w, align="center"))

    sess = session_id or f"Session: {time.strftime('%Y%m%d_%H%M%S')}"
    left_lines.append(_pad_visible(_c(_truncate_visible(sess, left_w - 2), fg=skin.muted_rgb), left_w, align="center"))
    if voice_status:
        v_upper = voice_status.strip().upper()
        v_icon = "🎙️" if v_upper == "ON" else ("🔇" if v_upper == "MUTED" else "🎙️")
        v_badge = f"{v_icon} Voice: {v_upper}"
        v_fg = skin.accent_rgb if v_upper == "ON" else (skin.warn_rgb if v_upper == "MUTED" else skin.muted_rgb)
        left_lines.append(_pad_visible(_c(v_badge, fg=v_fg, bold=(v_upper == "ON")), left_w, align="center"))

    # 4. Right column: Categorized Available Tools & Available Skills
    right_lines: list[str] = [_c("Available Tools", fg=skin.heading_rgb, bold=True)]
    tools_dict = getattr(getattr(orchestrator, "gateway", None), "tools", {}) or {}
    grouped_tools: dict[str, list[str]] = {}
    if tools_dict:
        for tid in sorted(tools_dict.keys()):
            cat = f"mcp:{tid.split('__', 1)[0]}" if "__" in tid else TOOL_CATEGORY_MAP.get(tid, "general")
            grouped_tools.setdefault(cat, []).append(tid)
    else:
        grouped_tools = {
            "file": ["list_files", "read_file", "write_file"],
            "lsp_compiler": ["lsp_diagnostics", "lsp_definition", "lsp_references", "lsp_hover"],
            "search": ["web_search"],
            "ast_graph": ["query_graphify"],
            "verification": ["run_tests"],
            "git_ops": ["git_push_remote"],
            "policy_guard": ["modify_system_policy"],
        }

    for cat, tnames in grouped_tools.items():
        prefix = _c(f"{cat}: ", fg=skin.category_rgb)
        avail = max(12, right_w - len(strip_ansi(prefix)) - 1)
        right_lines.append(f"{prefix}{_c(_truncate_visible(', '.join(tnames), avail), fg=skin.text_rgb)}")
    right_lines.append(_c("(0ms Call-Level Journal & R0-R4 Default-Deny Policy active)", fg=skin.muted_rgb))
    right_lines.append("")

    right_lines.append(_c("Available Skills", fg=skin.heading_rgb, bold=True))
    db_skills: list[str] = []
    if orchestrator is not None and hasattr(orchestrator, "ledger"):
        try:
            with orchestrator.ledger._lock:
                if orchestrator.ledger.conn is not None:
                    rows = orchestrator.ledger.conn.execute(
                        "SELECT name, status FROM skills ORDER BY created_at DESC"
                    ).fetchall()
                    db_skills = [f"{r['name']} ({r['status']})" for r in rows]
        except Exception:
            db_skills = []

    if db_skills:
        prefix = _c("registered: ", fg=skin.category_rgb)
        avail = max(12, right_w - len(strip_ansi(prefix)) - 1)
        right_lines.append(f"{prefix}{_c(_truncate_visible(', '.join(db_skills), avail), fg=skin.text_rgb)}")

    for g_name, g_items in BUILTIN_SKILL_GROUPS:
        prefix = _c(f"{g_name}: ", fg=skin.category_rgb)
        avail = max(12, right_w - len(strip_ansi(prefix)) - 1)
        right_lines.append(f"{prefix}{_c(_truncate_visible(', '.join(g_items), avail), fg=skin.text_rgb)}")

    right_lines.append("")
    total_t = sum(len(v) for v in grouped_tools.values())
    total_s = len(db_skills) + sum(len(g[1]) for g in BUILTIN_SKILL_GROUPS)
    right_lines.append(_c(f"{total_t} tools · {total_s} skills · /help for commands", fg=skin.category_rgb))

    # 5. Assemble Two-Column Box
    max_rows = max(len(left_lines), len(right_lines))
    vert = _c("│", fg=skin.border_rgb)
    box_rows = [top_border, sub_underline]
    for i in range(max_rows):
        l_cell = _pad_visible(left_lines[i] if i < len(left_lines) else "", left_w)
        r_cell = _pad_visible(right_lines[i] if i < len(right_lines) else "", right_w)
        box_rows.append(f"{vert} {l_cell}  {r_cell}{vert}")
    box_rows.append(_c("└" + ("─" * inner_w) + "┘", fg=skin.border_rgb))

    welcome_lines = [
        _c("Welcome to HEX Autonomous AI Workbench! Type your message or /help for commands.", fg=skin.text_rgb, bold=True),
        _c("✦ Tip: hex chat -Q enables quiet mode for programmatic use — suppresses banner and spinner.", fg=skin.category_rgb),
    ]

    return "\n".join(banner_rows + [""] + box_rows + [""] + welcome_lines)


def render_status_bar(
    *,
    model_id: str = "gemini-2.5-pro",
    compressor_mode: str = "local",
    cow_backend: str = "apfs_clone",
    tokens_in: int = 0,
    max_context_tokens: int = 32768,
    compression_ratio: float = 0.0,
    cost_usd: float = 0.0,
    max_cost_usd: float = 2.0,
    elapsed_seconds: int = 0,
    voice_status: str | None = None,
    skin_name: str = "amber",
    width: int = 108,
    enable_color: bool = True,
) -> str:
    """Render the persistent bottom status bar with context window fill gauge (`[██░░░░░░░░] XX%`)."""
    skin = SKIN_REGISTRY.get(skin_name.lower(), SKIN_REGISTRY["amber"])

    def _c(txt: str, fg: tuple[int, int, int] | None = None, bg: tuple[int, int, int] | None = None, bold: bool = False) -> str:
        return colorize_rgb(txt, fg=fg, bg=bg, bold=bold, enabled=enable_color)

    ctx_max = max(1, max_context_tokens)
    ctx_ratio = min(1.0, max(0.0, tokens_in / ctx_max))
    filled = int(round(ctx_ratio * 10))
    bar_str = ("█" * filled) + ("░" * (10 - filled))

    if tokens_in > 0:
        pct_val = max(1, int(round(ctx_ratio * 100))) if ctx_ratio > 0 else 0
        pct_str = f"{pct_val}%"
        if tokens_in >= 1000:
            tok_fmt = f"{tokens_in / 1000:.1f}k/{ctx_max // 1000}k"
        else:
            tok_fmt = f"{tokens_in}/{ctx_max // 1000}k"
        if compression_ratio > 0:
            sav_pct = int(round(compression_ratio * 100))
            ctx_str = f"ctx {tok_fmt} ({sav_pct}% saved)"
        else:
            ctx_str = f"ctx {tok_fmt}"
    else:
        pct_str = "--"
        ctx_str = "ctx --"

    # Color-code context gauge: normal < 60%, warn 60-85%, danger > 85%
    if ctx_ratio >= 0.85:
        gauge_fg = skin.danger_rgb
    elif ctx_ratio >= 0.60:
        gauge_fg = skin.warn_rgb
    else:
        gauge_fg = skin.accent_rgb

    parts = [
        _c(f" {skin.prompt_symbol} {model_id} ", fg=skin.heading_rgb, bg=skin.status_bg_rgb, bold=True),
    ]
    if tokens_in > 0:
        parts.extend([
            _c(f" {ctx_str} ", fg=skin.category_rgb, bg=skin.status_bg_rgb),
            _c(f" [{bar_str}] {pct_str} ", fg=gauge_fg, bg=skin.status_bg_rgb, bold=True),
        ])
    if compressor_mode and cow_backend:
        parts.append(_c(f" comp:{compressor_mode} · cow:{cow_backend} ", fg=skin.category_rgb, bg=skin.status_bg_rgb))
    elif compressor_mode:
        parts.append(_c(f" comp:{compressor_mode} ", fg=skin.category_rgb, bg=skin.status_bg_rgb))

    if voice_status:
        v_upper = voice_status.strip().upper()
        if v_upper == "ON":
            parts.append(_c(" 🎙️ ON ", fg=skin.accent_rgb, bg=skin.status_bg_rgb, bold=True))
        elif v_upper == "MUTED":
            parts.append(_c(" 🔇 MUTED ", fg=skin.warn_rgb, bg=skin.status_bg_rgb, bold=True))
        elif v_upper == "OFF":
            parts.append(_c(" 🎙️ OFF ", fg=skin.muted_rgb, bg=skin.status_bg_rgb))
        else:
            parts.append(_c(f" 🎙️ {voice_status} ", fg=skin.muted_rgb, bg=skin.status_bg_rgb))

    if cost_usd > 0.0:
        parts.append(_c(f" ${cost_usd:.3f} ", fg=skin.muted_rgb, bg=skin.status_bg_rgb))
    if elapsed_seconds > 0:
        parts.append(_c(f" {elapsed_seconds}s ", fg=skin.muted_rgb, bg=skin.status_bg_rgb))
    sep = _c("│", fg=skin.border_rgb, bg=skin.status_bg_rgb)
    rule = _c("─" * max(76, width), fg=skin.border_rgb)
    return f"{sep.join(parts)}\n{rule}"


# ==============================================================================
# Hermes Docked Input Frame & Dynamic Ghost Suggestions
# ==============================================================================

DEFAULT_PROMPT_SUGGESTIONS: tuple[str, ...] = (
    "Ask me anything...",
    "Describe a feature or fix to implement...",
    "Find and fix bugs in this codebase...",
    "Explain how the model router works...",
    "Refactor a function or write unit tests...",
    "Type /help to see all available commands...",
    "Run tests and verify the latest changes...",
    "Inspect AST dependencies and blast radius...",
    "Ask about architecture, tools, or skills...",
    "Create a CoW snapshot branch to test an idea...",
    "Review git diff against origin/main...",
    "Search files or query the symbol index...",
    "Switch models with /model gemini-3.8-flash-tiered...",
    "Audit security gates and CVE vulnerabilities...",
    "Synthesize a new tool or MCP capability...",
    "What would you like to build or solve today?...",
)


def render_docked_input_frame(
    width: int = 100,
    text: str = "",
    cursor_pos: int = 0,
    suggestion: str = "Ask me anything...",
    mode: str = "plan",
    task_num: int = 1,
    project_name: str = "Harness",
    git_branch: str = "main",
    model_id: str = "gemini-3.8-flash-tiered",
    skin: TUISkin | None = None,
    enable_color: bool = True,
) -> tuple[str, str, str]:
    """Render the Hermes-grade docked input frame with ghost suggestion and status footer."""
    sk = skin or SKIN_REGISTRY["amber"]
    w = max(60, min(120, width))

    def _c(txt: str, fg: tuple[int, int, int] | None = None, bg: tuple[int, int, int] | None = None, bold: bool = False) -> str:
        return colorize_rgb(txt, fg=fg, bg=bg, bold=bold, enabled=enable_color)

    # 1. Top border
    top = _c("╭" + ("─" * (w - 2)) + "╮", fg=sk.border_rgb)

    # 2. Middle input line
    prompt_pfx = f"{_c('│', fg=sk.border_rgb)} {_c('❯', fg=sk.accent_rgb, bold=True)} "
    inner_space = w - 2 - 4  # 4 chars: '│ ❯ '

    if not text:
        sugg_disp = _truncate_visible(suggestion, inner_space)
        pad_len = max(0, inner_space - len(strip_ansi(sugg_disp)))
        mid = f"{prompt_pfx}{_c(sugg_disp, fg=sk.muted_rgb)}{' ' * pad_len}{_c('│', fg=sk.border_rgb)}"
    else:
        plain_len = len(text)
        if plain_len <= inner_space:
            disp_txt = text
            pad_len = max(0, inner_space - plain_len)
            mid = f"{prompt_pfx}{_c(disp_txt, fg=sk.text_rgb)}{' ' * pad_len}{_c('│', fg=sk.border_rgb)}"
        else:
            start = max(0, cursor_pos - inner_space + 4)
            disp_txt = "…" + text[start + 1 : start + inner_space]
            pad_len = max(0, inner_space - len(disp_txt))
            mid = f"{prompt_pfx}{_c(disp_txt, fg=sk.text_rgb)}{' ' * pad_len}{_c('│', fg=sk.border_rgb)}"

    # 3. Bottom border with fused status & shortcut pills
    right_plain = " enter send · tab mode · /help ─╯"
    right_colored = (
        _c(" enter send · tab mode · /help", fg=sk.muted_rgb)
        + _c(" ─╯", fg=sk.border_rgb)
    )

    mode_norm = (mode or "agent").lower()
    if mode_norm in ("plan", "ask"):
        mode_badge = _c("plan", fg=sk.accent_rgb, bold=True)
    elif mode_norm == "code":
        mode_badge = _c("code", fg=(190, 130, 255), bold=True)
    else:
        mode_badge = _c("agent", fg=sk.heading_rgb, bold=True)

    left_badges = [
        mode_badge,
    ]
    if project_name:
        left_badges.append(_c(project_name, fg=sk.heading_rgb))
    if git_branch:
        left_badges.append(_c(git_branch, fg=sk.category_rgb, bold=True))
    if model_id:
        left_badges.append(_c(model_id, fg=sk.category_rgb))

    sep = _c(" · ", fg=sk.muted_rgb)
    left_str = _c("╰─ ", fg=sk.border_rgb) + sep.join(left_badges) + " "
    left_plain_len = len(strip_ansi(left_str))
    right_plain_len = len(strip_ansi(right_plain))

    if left_plain_len + right_plain_len > w:
        left_badges_compact = [
            mode_badge,
            _c(git_branch or project_name, fg=sk.category_rgb, bold=True),
            _c(_truncate_visible(model_id, 18), fg=sk.category_rgb),
        ]
        left_str = _c("╰─ ", fg=sk.border_rgb) + sep.join(left_badges_compact) + " "
        left_plain_len = len(strip_ansi(left_str))

    if left_plain_len + right_plain_len > w:
        left_str = _c(f"╰─ {mode_norm} ", fg=sk.border_rgb)
        left_plain_len = len(strip_ansi(left_str))
        right_plain = " enter send ─╯"
        right_colored = _c(" enter send", fg=sk.muted_rgb) + _c(" ─╯", fg=sk.border_rgb)
        right_plain_len = len(strip_ansi(right_plain))

    dash_fill = max(1, w - left_plain_len - right_plain_len)
    bot = left_str + _c("─" * dash_fill, fg=sk.border_rgb) + right_colored

    return top, mid, bot


# ==============================================================================
# HexConsole Interactive REPL
# ==============================================================================


class HexConsole:
    """Interactive pair-programming terminal REPL for HEX Autonomous AI Workbench."""

    def __init__(
        self,
        orchestrator: Any,
        project_id: str = "default",
        workspace_root: Path | str | None = None,
        stream: bool = True,
        model_profile: str | None = None,
        skin_name: str = "amber",
        quiet: bool = False,
        voice_enabled: bool = False,
        voice_name: str | None = None,
        speech_rate: int = 190,
    ):
        self.orchestrator = orchestrator
        self.project_id = project_id
        if workspace_root:
            self.workspace_root = Path(workspace_root).resolve()
        elif hasattr(orchestrator, "default_workspace") and orchestrator.default_workspace:
            self.workspace_root = Path(orchestrator.default_workspace).resolve()
        else:
            self.workspace_root = Path.cwd().resolve()
        self.stream = stream
        self.model_profile = model_profile
        self.skin_name = skin_name.lower() if skin_name.lower() in SKIN_REGISTRY else "amber"
        self.quiet = quiet
        self.active_task_id: str | None = None
        self.history: list[str] = []
        self._suggestion_idx: int = 0
        self.mode: str = "agent"  # "plan" | "agent" | "code"
        self.session_id = f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        self.session_start = time.time()

        # Session token & budget telemetry
        self.session_tokens_in: int = 0
        self.session_tokens_out: int = 0
        self.session_tokens_saved: int = 0
        self.session_compression_ratio: float = 0.0
        self.session_cost_usd: float = 0.0

        # Hands-free real-time voice & speech pair-programming engine
        self.voice_engine = VoiceEngine(
            VoiceConfig(
                tts_enabled=voice_enabled,
                stt_enabled=voice_enabled,
                voice_name=voice_name,
                speech_rate=speech_rate,
            )
        )

    @property
    def skin(self) -> TUISkin:
        return SKIN_REGISTRY.get(self.skin_name, SKIN_REGISTRY["amber"])

    def _setup_readline(self) -> None:
        try:
            import readline

            slash_cmds = [
                "/help", "/status", "/diff", "/sbs", "/rollback", "/fork",
                "/skills", "/scan", "/model", "/compressor", "/budget",
                "/quota", "/antigravity", "/blast", "/proof", "/memory",
                "/remember", "/reflect", "/search", "/doctor", "/recipe",
                "/lsp", "/backup", "/dump", "/workers", "/harness2", "/skin",
                "/voice", "/banner", "/clear", "/quit", "/exit",
            ]
            voice_cmds = ["/voice on", "/voice off", "/voice mute", "/voice status"]
            skin_cmds = [f"/skin {s}" for s in SKIN_REGISTRY]
            comp_cmds = ["/compressor local", "/compressor cloud"]
            candidates = sorted(set(slash_cmds + voice_cmds + skin_cmds + comp_cmds))

            def completer(text: str, state: int) -> str | None:
                buf = readline.get_line_buffer().lstrip()
                if buf.startswith("/"):
                    matches = [c + " " for c in candidates if c.startswith(buf)]
                    if state < len(matches):
                        return matches[state].split()[-1] if " " in buf else matches[state]
                return None

            readline.set_completer_delims(" \t\n")
            readline.set_completer(completer)
            if "libedit" in getattr(readline, "__doc__", ""):
                readline.parse_and_bind("bind ^I rl_complete")
            else:
                readline.parse_and_bind("tab: complete")
        except Exception:
            pass

    def render_welcome_splash(self) -> str:
        """Render the Hermes-grade full splash card."""
        comp_mode = getattr(getattr(self.orchestrator, "router", None), "compressor_mode", "deterministic")
        active_model = (
            self.model_profile
            or getattr(getattr(self.orchestrator, "router", None), "cloud_model_id", None)
            or "claude-3-5-sonnet"
        )
        v_stat = (
            "MUTED"
            if self.voice_engine.is_muted()
            else ("ON" if self.voice_engine.config.tts_enabled else "OFF")
        )
        return render_hermes_splash(
            self.orchestrator,
            skin_name=self.skin_name,
            model_id=active_model,
            compressor_mode=comp_mode,
            workspace_root=self.workspace_root,
            session_id=f"Session: {self.session_id}",
            voice_status=v_stat,
        )

    def get_status_bar(self) -> str:
        cow_backend = getattr(getattr(self.orchestrator, "cow", None), "backend_name", "apfs")
        comp_mode = getattr(getattr(self.orchestrator, "router", None), "compressor_mode", "local")
        active_model = (
            self.model_profile
            or getattr(getattr(self.orchestrator, "router", None), "cloud_model_id", None)
            or "claude-3-5-sonnet"
        )
        elapsed = int(max(0.0, time.time() - self.session_start))
        v_stat = (
            "MUTED"
            if self.voice_engine.is_muted()
            else ("ON" if self.voice_engine.config.tts_enabled else "OFF")
        )
        return render_status_bar(
            model_id=active_model,
            compressor_mode=comp_mode,
            cow_backend=cow_backend,
            tokens_in=self.session_tokens_in,
            compression_ratio=self.session_compression_ratio,
            cost_usd=self.session_cost_usd,
            elapsed_seconds=elapsed,
            voice_status=v_stat,
            skin_name=self.skin_name,
        )

    def _get_next_suggestion(self) -> str:
        """Return the next ghost suggestion string in rotation."""
        sugg = DEFAULT_PROMPT_SUGGESTIONS[self._suggestion_idx % len(DEFAULT_PROMPT_SUGGESTIONS)]
        self._suggestion_idx += 1
        return sugg

    def _read_docked_input(self, prompt: str = "hex> ") -> str:
        """Hermes-grade docked input frame with ghost suggestions and live status pills."""
        if (
            not hasattr(sys.stdin, "isatty")
            or not sys.stdin.isatty()
            or not hasattr(sys.stdout, "isatty")
            or not sys.stdout.isatty()
            or self.quiet
        ):
            return input(prompt)

        try:
            import termios
            import tty
            import select
            import shutil
        except ImportError:
            return input(prompt)

        ws_name = self.workspace_root.name if self.workspace_root else "Harness"
        git_branch = "main"
        try:
            res = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(self.workspace_root),
                capture_output=True,
                text=True,
                timeout=0.6,
            )
            if res.returncode == 0 and res.stdout.strip():
                git_branch = res.stdout.strip()
        except Exception:
            pass

        active_model = (
            self.model_profile
            or getattr(getattr(self.orchestrator, "router", None), "cloud_model_id", None)
            or "gemini-3.8-flash-tiered"
        )
        task_num = len(self.history) + 1
        mode = getattr(self, "mode", "agent")
        suggestion = self._get_next_suggestion()

        w = min(112, max(68, shutil.get_terminal_size(fallback=(100, 24)).columns))

        top, mid, bot = render_docked_input_frame(
            width=w,
            text="",
            cursor_pos=0,
            suggestion=suggestion,
            mode=mode,
            task_num=task_num,
            project_name=ws_name,
            git_branch=git_branch,
            model_id=active_model,
            skin=self.skin,
            enable_color=True,
        )

        sys.stdout.write(f"\n{top}\n{mid}\n{bot}")
        sys.stdout.flush()
        # Move cursor up 1 line to mid line, and position at column 4 (after '│ ❯ ')
        prompt_offset = 4
        sys.stdout.write(f"\033[A\r\033[{prompt_offset}C")
        sys.stdout.flush()

        buffer: list[str] = []
        cursor: int = 0
        hist_idx = len(self.history)
        fd = sys.stdin.fileno()

        try:
            old_settings = termios.tcgetattr(fd)
        except Exception:
            return input(prompt)

        slash_cmds = [
            "/help", "/mode", "/plan", "/agent", "/code", "/ask",
            "/status", "/tasks", "/diff", "/sbs", "/rollback", "/fork",
            "/skills", "/scan", "/model", "/compressor", "/budget",
            "/quota", "/antigravity", "/blast", "/proof", "/memory",
            "/remember", "/reflect", "/search", "/doctor", "/recipe",
            "/lsp", "/backup", "/dump", "/workers", "/harness2", "/skin",
            "/voice", "/banner", "/clear", "/quit", "/exit",
        ]

        def _redraw(buf: list[str], cur: int, update_bot: bool = False) -> None:
            cur_txt = "".join(buf)
            _, n_mid, n_bot = render_docked_input_frame(
                width=w,
                text=cur_txt,
                cursor_pos=cur,
                suggestion=suggestion,
                mode=getattr(self, "mode", "agent"),
                task_num=task_num,
                project_name=ws_name,
                git_branch=git_branch,
                model_id=active_model,
                skin=self.skin,
                enable_color=True,
            )
            in_sp = w - 2 - 4
            if len(cur_txt) <= in_sp:
                scr_cur = prompt_offset + cur
            else:
                st = max(0, cur - in_sp + 4)
                scr_cur = prompt_offset + (cur - st)
            if update_bot:
                sys.stdout.write(f"\r{n_mid}\n\r{n_bot}\033[A\r\033[{scr_cur}C")
            else:
                sys.stdout.write(f"\r{n_mid}\r\033[{scr_cur}C")
            sys.stdout.flush()

        try:
            tty.setraw(fd)
            while True:
                ch = sys.stdin.read(1)
                if ch in ("\r", "\n"):
                    sys.stdout.write("\033[B\r\n")
                    sys.stdout.flush()
                    return "".join(buffer)
                elif ch == "\x03":  # Ctrl+C
                    sys.stdout.write("\033[B\r\n")
                    sys.stdout.flush()
                    raise KeyboardInterrupt()
                elif ch == "\x04":  # Ctrl+D
                    if not buffer:
                        sys.stdout.write("\033[B\r\n")
                        sys.stdout.flush()
                        raise EOFError()
                    if cursor < len(buffer):
                        buffer.pop(cursor)
                        _redraw(buffer, cursor)
                elif ch in ("\x7f", "\x08"):  # Backspace
                    if cursor > 0:
                        buffer.pop(cursor - 1)
                        cursor -= 1
                        _redraw(buffer, cursor)
                elif ch == "\x1b":  # Escape sequence
                    seq = ""
                    while select.select([sys.stdin], [], [], 0.05)[0]:
                        seq += sys.stdin.read(1)
                    if seq == "[A":  # Up arrow
                        if self.history and hist_idx > 0:
                            hist_idx -= 1
                            buffer = list(self.history[hist_idx])
                            cursor = len(buffer)
                            _redraw(buffer, cursor)
                    elif seq == "[B":  # Down arrow
                        if hist_idx < len(self.history) - 1:
                            hist_idx += 1
                            buffer = list(self.history[hist_idx])
                            cursor = len(buffer)
                            _redraw(buffer, cursor)
                        else:
                            hist_idx = len(self.history)
                            buffer = []
                            cursor = 0
                            _redraw(buffer, cursor)
                    elif seq == "[C":  # Right arrow
                        if cursor < len(buffer):
                            cursor += 1
                            _redraw(buffer, cursor)
                    elif seq == "[D":  # Left arrow
                        if cursor > 0:
                            cursor -= 1
                            _redraw(buffer, cursor)
                    elif seq in ("[H", "[1~"):  # Home
                        cursor = 0
                        _redraw(buffer, cursor)
                    elif seq in ("[F", "[4~"):  # End
                        cursor = len(buffer)
                        _redraw(buffer, cursor)
                    elif seq == "[3~":  # Delete
                        if cursor < len(buffer):
                            buffer.pop(cursor)
                            _redraw(buffer, cursor)
                elif ch == "\t":  # Tab completion or empty-line mode cycling
                    txt = "".join(buffer)
                    if not txt:
                        # Empty prompt: Tab cycles mode through plan -> agent -> code -> plan
                        mode_order = ["plan", "agent", "code"]
                        curr_mode = getattr(self, "mode", "agent")
                        if curr_mode == "ask":
                            curr_mode = "plan"
                        curr_idx = mode_order.index(curr_mode) if curr_mode in mode_order else 1
                        self.mode = mode_order[(curr_idx + 1) % len(mode_order)]
                        _redraw(buffer, cursor, update_bot=True)
                    elif txt.startswith("/"):
                        matches = [c for c in slash_cmds if c.startswith(txt)]
                        if len(matches) == 1:
                            buffer = list(matches[0] + " ")
                            cursor = len(buffer)
                            _redraw(buffer, cursor)
                elif ord(ch) >= 32:
                    b_val = ord(ch)
                    extra_bytes = 0
                    if (b_val & 0xE0) == 0xC0:
                        extra_bytes = 1
                    elif (b_val & 0xF0) == 0xE0:
                        extra_bytes = 2
                    elif (b_val & 0xF8) == 0xF0:
                        extra_bytes = 3

                    if extra_bytes > 0:
                        raw_bytes = bytearray([b_val])
                        for _ in range(extra_bytes):
                            nxt = sys.stdin.read(1)
                            raw_bytes.append(ord(nxt))
                        ch_str = raw_bytes.decode("utf-8", errors="replace")
                    else:
                        ch_str = ch

                    buffer.insert(cursor, ch_str)
                    cursor += len(ch_str)
                    _redraw(buffer, cursor)
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            except Exception:
                pass

    def start(self, input_fn: Callable[[str], str] | None = None) -> int:
        """Start interactive REPL prompt loop. Returns exit code 0 on termination."""
        self._setup_readline()
        if not self.quiet:
            print(self.render_welcome_splash())

        if input_fn is not None:
            read_line = input_fn
        else:
            read_line = self._read_docked_input

        try:
            while True:
                try:
                    line = read_line("hex> ")
                except (EOFError, KeyboardInterrupt):
                    print(f"\n{ANSI_DIM}Exiting HEX console.{ANSI_RESET}")
                    return 0

                line = line.strip()
                if not line:
                    continue

                self.history.append(line)

                try:
                    if line.startswith("/"):
                        should_exit = self.handle_slash_command(line)
                        if should_exit:
                            return 0
                    else:
                        self.handle_user_prompt(line)
                except KeyboardInterrupt:
                    print(f"\n{ANSI_YELLOW}⚡ Task cancelled by user (Ctrl+C).{ANSI_RESET}\n")
        finally:
            if hasattr(self, "voice_engine"):
                self.voice_engine.shutdown()

        return 0

    def handle_slash_command(self, cmd_line: str) -> bool:
        """Dispatch slash commands. Returns True if REPL should terminate, False otherwise."""
        parts = cmd_line.strip().split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd in ("/quit", "/exit"):
            print(f"{ANSI_DIM}Exiting HEX console.{ANSI_RESET}")
            return True
        elif cmd in ("/help", "/h", "/?"):
            self._cmd_help()
        elif cmd in ("/mode", "/m"):
            self._cmd_mode(arg)
        elif cmd in ("/plan", "/ask"):
            self._cmd_mode("plan")
        elif cmd == "/agent":
            self._cmd_mode("agent")
        elif cmd == "/code":
            self._cmd_mode("code")
        elif cmd in ("/status", "/tasks"):
            self._cmd_status()
        elif cmd == "/diff":
            self._cmd_diff()
        elif cmd == "/sbs":
            self._cmd_sbs(arg)
        elif cmd == "/rollback":
            self._cmd_rollback()
        elif cmd == "/fork":
            self._cmd_fork()
        elif cmd == "/skills":
            self._cmd_skills()
        elif cmd in ("/scan", "/scan-skill"):
            self._cmd_scan(arg)
        elif cmd == "/model":
            self._cmd_model(arg)
        elif cmd in ("/compressor", "/comp"):
            self._cmd_compressor(arg)
        elif cmd == "/budget":
            self._cmd_budget()
        elif cmd in ("/quota", "/antigravity", "/ag"):
            self._cmd_quota(arg)
        elif cmd in ("/blast", "/graph", "/ast"):
            self._cmd_blast(arg)
        elif cmd == "/proof":
            self._cmd_proof(arg)
        elif cmd in ("/memory", "/mem"):
            self._cmd_memory(arg)
        elif cmd == "/remember":
            self._cmd_remember(arg)
        elif cmd in ("/reflect", "/lessons"):
            self._cmd_reflect()
        elif cmd == "/search":
            self._cmd_search(arg)
        elif cmd == "/doctor":
            self._cmd_doctor(arg)
        elif cmd == "/recipe":
            self._cmd_recipe()
        elif cmd in ("/lsp", "/compiler"):
            self._cmd_lsp(arg)
        elif cmd == "/backup":
            self._cmd_backup()
        elif cmd == "/dump":
            self._cmd_dump()
        elif cmd == "/workers":
            self._cmd_workers()
        elif cmd in ("/harness2", "/h2"):
            self._cmd_harness2()
        elif cmd in ("/project", "/proj"):
            self._cmd_project(arg)
        elif cmd in ("/skin", "/theme"):
            self._cmd_skin(arg)
        elif cmd == "/voice":
            self._cmd_voice(arg)
        elif cmd in ("/banner", "/splash"):
            print(self.render_welcome_splash())
        elif cmd == "/clear":
            self._cmd_clear()
        else:
            print(f"{ANSI_RED}Unknown command: {cmd}. Type /help for available commands.{ANSI_RESET}")
        return False

    def _cmd_mode(self, arg: str = "") -> None:
        target = arg.strip().lower()
        if not target:
            modes = ["plan", "agent", "code"]
            curr = getattr(self, "mode", "agent")
            if curr == "ask":
                curr = "plan"
            idx = modes.index(curr) if curr in modes else 1
            self.mode = modes[(idx + 1) % len(modes)]
            print(f"\n{ANSI_GREEN}✓ Switched mode to: {self.mode.upper()}{ANSI_RESET} (Usage: /mode <plan|agent|code> or press Tab)\n")
            return
        if target == "ask":
            target = "plan"
        if target not in ("plan", "agent", "code"):
            print(f"\n{ANSI_YELLOW}Unknown mode '{arg}'. Available modes: plan, agent, code{ANSI_RESET}\n")
            return
        self.mode = target
        print(f"\n{ANSI_GREEN}✓ Switched mode to: {self.mode.upper()}{ANSI_RESET}\n")

    def _cmd_help(self) -> None:
        commands = [
            ("/help", "Show table of all available slash commands"),
            ("/mode [mode]", "Switch mode: plan (architecture & read-only spec), agent (autonomous execution), code (direct synthesis)"),
            ("/status", "Display active/recent tasks, proof bundle, and worker state"),
            ("/diff", "Inspect uncommitted changes in active workspace via unified diff"),
            ("/sbs <file>", "Render two-column side-by-side diff for a modified workspace file"),
            ("/blast [sym|file]", "Render Active Graphify AST blast-radius tree (or --html export)"),
            ("/proof [task_id]", "Display cryptographic Proof-Carrying Artifact bundle & diff"),
            ("/rollback", "Roll back workspace changes or cancel active task"),
            ("/fork", "Fork active task into speculative attempt branches"),
            ("/skills", "List registered skills, risk scores, and gate decisions"),
            ("/scan <dir>", "Scan and register a SKILL.md bundle via NVIDIA SkillSpector"),
            ("/model [name]", "Inspect or switch active model routing profile (or 'antigravity')"),
            ("/compressor [mode]", "Switch Tier-1 log compressor between 'local' and 'cloud'"),
            ("/budget", "Display session token usage, compression ratio, and cost"),
            ("/quota", "Inspect Google Antigravity OAuth session & live model quota windows"),
            ("/memory [query]", "Search governed memories with AST code-hash staleness badges"),
            ("/remember <k>: <v>", "Record a governed project memory linked to AST nodes"),
            ("/reflect", "Run Graphify deterministic work-memory reflection (LESSONS.md)"),
            ("/search <query>", "Run SQLite FTS5 BM25 search across tasks, logs, and tool calls"),
            ("/doctor [--fix]", "Run 8-category system health diagnostics and self-healing"),
            ("/recipe", "Auto-detect project bootstrap, build, test, and start recipes"),
            ("/lsp [status|diag]", "Inspect language servers and query compiler diagnostics"),
            ("/backup", "Create a quick zip backup of state, config, and ledger"),
            ("/dump", "Generate plain-text system diagnostic dump with secret redaction"),
            ("/workers", "List registered distributed workers, CoW backends, and heartbeats"),
            ("/harness2", "Run a gated Google Research Harness² Propose-Probe-Compose probe"),
            ("/skin [name]", "Switch TUI theme (amber, cyberpunk, matrix, nord, mono)"),
            ("/voice [on|off|mute]", "Toggle hands-free voice mode, mute, speech rate or audio status"),
            ("/project [list|switch]", "Inspect active project card, list workspaces, or switch context"),
            ("/banner", "Re-render the Hermes-style startup splash screen and status bar"),
            ("/clear", "Clear the terminal screen"),
            ("/quit, /exit", "Exit interactive console session"),
        ]
        print(f"\n{ANSI_BOLD}{ANSI_CYAN}Available HEX Slash Commands:{ANSI_RESET}")
        for c, desc in commands:
            print(f"  {ANSI_BOLD}{c:<18}{ANSI_RESET} - {desc}")
        print()

    def _cmd_status(self) -> None:
        print(f"\n{ANSI_BOLD}{ANSI_CYAN}HEX Console Session Status:{ANSI_RESET}")
        print(f"  Project ID:     {self.project_id}")
        print(f"  Workspace Root: {self.workspace_root}")
        print(f"  Skin / Theme:   {self.skin.name} ({self.skin.description})")
        print(f"  Streaming:      {'Enabled' if self.stream else 'Disabled'}")
        v_stat = (
            "MUTED"
            if self.voice_engine.is_muted()
            else ("ON" if self.voice_engine.config.tts_enabled else "OFF")
        )
        print(f"  Voice Mode:     {v_stat} (TTS:{self.voice_engine.config.tts_enabled}, STT:{self.voice_engine.config.stt_enabled})")
        print(f"  Active Task ID: {self.active_task_id or '(none)'}")

        workers = []
        if hasattr(self.orchestrator, "ledger") and hasattr(self.orchestrator.ledger, "list_workers"):
            try:
                workers = self.orchestrator.ledger.list_workers()
            except Exception:
                pass
        print(f"  Active Workers: {len(workers)}")
        for w in workers[:3]:
            wid = w.get("worker_id", "unknown")
            status = w.get("status") or w.get("health", "unknown")
            mclass = w.get("machine_class", "worker")
            print(f"    • {wid} [{mclass}] ({status})")

        if self.active_task_id and hasattr(self.orchestrator, "ledger"):
            task = self.orchestrator.ledger.get_task(self.active_task_id)
            if task:
                state = task.get("status")
                goal = task.get("goal")
                print(f"  Active Task:    [{state}] {goal}")
            proof = self.orchestrator.ledger.get_proof_bundle(self.active_task_id)
            if proof:
                ver_res = proof.get("verification_results") or {}
                verified = proof.get("verified", ver_res.get("passed", False))
                v_str = f"{ANSI_GREEN}VERIFIED{ANSI_RESET}" if verified else f"{ANSI_YELLOW}UNVERIFIED{ANSI_RESET}"
                sha = str(proof.get("workspace_tree_sha", ""))[:12]
                print(f"  Proof Bundle:   {v_str} (Merkle Tree: {sha}...)")
        elif hasattr(self.orchestrator, "ledger") and hasattr(self.orchestrator.ledger, "list_tasks"):
            try:
                tasks = self.orchestrator.ledger.list_tasks(limit=1)
                if tasks:
                    last = tasks[0]
                    print(f"  Last Task:      {last.get('task_id')} [{last.get('status')}] — {last.get('goal')}")
            except Exception:
                pass
        print()

    def _cmd_diff(self) -> None:
        diff_text = ""
        if self.active_task_id and hasattr(self.orchestrator, "ledger"):
            attempts = self.orchestrator.ledger.list_attempts(self.active_task_id)
            chk = self.orchestrator.ledger.get_latest_checkpoint(self.active_task_id)
            if attempts and chk:
                last_ws = attempts[-1].get("workspace_path")
                snap_path = chk.get("snapshot_path")
                if last_ws and snap_path and Path(last_ws).is_dir() and Path(snap_path).is_dir():
                    if hasattr(self.orchestrator, "cow") and hasattr(self.orchestrator.cow, "compute_workspace_diff"):
                        diff_text = self.orchestrator.cow.compute_workspace_diff(snap_path, last_ws)
                    else:
                        from .cow import compute_workspace_diff
                        diff_text = compute_workspace_diff(snap_path, last_ws)

        if not diff_text:
            print(f"{ANSI_DIM}No uncommitted changes detected in active workspace.{ANSI_RESET}")
        else:
            print(render_unified_diff(diff_text, title=f"Task {self.active_task_id} Workspace Diff"))

    def _cmd_sbs(self, arg: str = "") -> None:
        rel = arg.strip()
        if not rel:
            print(f"{ANSI_YELLOW}Usage: /sbs <relative_file_path>{ANSI_RESET}")
            return
        cur_file = (self.workspace_root / rel).resolve()
        if not cur_file.is_file():
            print(f"{ANSI_RED}File not found in workspace: {rel}{ANSI_RESET}")
            return
        new_text = cur_file.read_text(encoding="utf-8", errors="replace")
        old_text = new_text
        if self.active_task_id and hasattr(self.orchestrator, "ledger"):
            chk = self.orchestrator.ledger.get_latest_checkpoint(self.active_task_id)
            if chk and chk.get("snapshot_path"):
                snap_file = Path(chk["snapshot_path"]) / rel
                if snap_file.is_file():
                    old_text = snap_file.read_text(encoding="utf-8", errors="replace")
        print(render_side_by_side_diff(old_text, new_text, filename=rel))

    def _cmd_rollback(self) -> None:
        if not self.active_task_id:
            print(f"{ANSI_DIM}No active task to roll back.{ANSI_RESET}")
            return
        try:
            if hasattr(self.orchestrator, "cancel_task"):
                self.orchestrator.cancel_task(self.active_task_id, actor="console")
                print(f"{ANSI_YELLOW}Task {self.active_task_id} canceled and uncommitted CoW workspace rolled back.{ANSI_RESET}")
                self.active_task_id = None
            else:
                print(f"{ANSI_RED}Orchestrator does not support cancel_task.{ANSI_RESET}")
        except Exception as exc:
            print(f"{ANSI_RED}Rollback failed: {exc}{ANSI_RESET}")

    def _cmd_fork(self) -> None:
        if not self.active_task_id:
            print(f"{ANSI_DIM}No active task to fork into speculative branches.{ANSI_RESET}")
            return
        try:
            if hasattr(self.orchestrator, "fork_speculative_branches"):
                res = self.orchestrator.fork_speculative_branches(
                    self.active_task_id,
                    branch_strategies=[{"name": "speculative_branch_a"}, {"name": "speculative_branch_b"}],
                )
                winner = res.get("winner_branch_id") or res.get("winning_branch") or res.get("status") or "completed"
                print(f"{ANSI_GREEN}Speculative branches evaluated successfully. Promoted: {winner}{ANSI_RESET}")
            else:
                print(f"{ANSI_RED}Orchestrator does not support fork_speculative_branches.{ANSI_RESET}")
        except Exception as exc:
            print(f"{ANSI_RED}Fork speculative execution failed: {exc}{ANSI_RESET}")

    def _cmd_skills(self) -> None:
        skills = []
        if hasattr(self.orchestrator, "ledger"):
            if hasattr(self.orchestrator.ledger, "list_skills"):
                try:
                    skills = self.orchestrator.ledger.list_skills()
                except Exception:
                    pass
            elif getattr(self.orchestrator.ledger, "conn", None) is not None:
                try:
                    with self.orchestrator.ledger._lock:
                        rows = self.orchestrator.ledger.conn.execute(
                            "SELECT * FROM skills ORDER BY created_at DESC"
                        ).fetchall()
                        skills = [self.orchestrator.ledger._row_to_dict(r) for r in rows if r]
                except Exception:
                    pass
        if not skills:
            print(f"{ANSI_DIM}No registered skills found.{ANSI_RESET}")
            return
        print(f"\n{ANSI_BOLD}{ANSI_CYAN}Registered Skills ({len(skills)}):{ANSI_RESET}")
        for s in skills:
            sid = s.get("skill_id", "unknown")
            status = s.get("status", "unknown")
            scan = s.get("scan") or {}
            risk = scan.get("risk_score", s.get("risk_class", "R1"))
            gate = scan.get("gate_decision", "ALLOW")
            quarantine = "QUARANTINED" if status == "quarantined" else "ACTIVE"
            badge = RISK_BADGES.get(str(risk)[:2], f"[{risk}]")
            print(f"  • {ANSI_BOLD}{sid:<24}{ANSI_RESET} {badge}  Gate: {gate:<16} Status: {status} ({quarantine})")
        print()

    def _cmd_scan(self, arg: str = "") -> None:
        path_str = arg.strip()
        if not path_str:
            print(f"{ANSI_YELLOW}Usage: /scan <skill_bundle_directory>{ANSI_RESET}")
            return
        try:
            res = self.orchestrator.scanner.scan_and_register_skill(Path(path_str))
            scan = res.get("scan") or {}
            gate = scan.get("gate_decision", "UNKNOWN")
            score = scan.get("risk_score", 0)
            col = ANSI_GREEN if gate == "ALLOW" else (ANSI_RED if gate == "QUARANTINE" else ANSI_YELLOW)
            print(
                f"{col}{ANSI_BOLD}SkillSpector Scan Complete:{ANSI_RESET} {res.get('name')} ({res.get('skill_id')}) "
                f"→ Gate: {col}{gate}{ANSI_RESET} (Score: {score}/100)"
            )
        except Exception as exc:
            print(f"{ANSI_RED}Skill scan failed: {exc}{ANSI_RESET}")

    def _cmd_model(self, arg: str = "") -> None:
        arg = arg.strip()
        if arg:
            profile_names = {"hybrid", "local", "cloud", "offline"}
            provider_names = {"antigravity", "google-antigravity", "agy"}
            if hasattr(self.orchestrator, "router"):
                if arg.lower() in provider_names:
                    if hasattr(self.orchestrator.router, "switch_cloud_provider"):
                        self.orchestrator.router.switch_cloud_provider("antigravity")
                    self.model_profile = "antigravity"
                    final_id = self.orchestrator.router.cloud_model_id
                    print(f"{ANSI_GREEN}Provider switched to: antigravity (model: {final_id}; session-only){ANSI_RESET}")
                    return
                if arg.lower() in profile_names:
                    self.orchestrator.router.profile = arg.lower()
                    self.model_profile = arg.lower()
                    print(f"{ANSI_GREEN}Model profile updated to: {arg.lower()} (session-only){ANSI_RESET}")
                    return

                # Keep the immediately rendered state and the execution provider in
                # sync even when an older/custom orchestrator has no durable setter.
                self.model_profile = arg
                self.orchestrator.router.cloud_model_id = arg
                provider = getattr(self.orchestrator.router, "cloud_provider", None)
                if provider is not None and hasattr(provider, "model_id"):
                    provider.model_id = arg

            if hasattr(self.orchestrator, "set_active_model"):
                try:
                    res = self.orchestrator.set_active_model(arg, durable=True)
                    if isinstance(res, (tuple, list)) and len(res) >= 3:
                        prov, final_id, saved_path = res[:3]
                        scope = f" (provider: {prov}, project override saved to {saved_path})" if saved_path else f" (provider: {prov})"
                        print(f"{ANSI_GREEN}Model profile updated to: {arg}{scope}{ANSI_RESET}")
                        return
                except Exception:
                    pass
            print(f"{ANSI_GREEN}Model profile updated to: {arg}{ANSI_RESET}")
            return
        else:
            in_stream = getattr(self, "stream_in", sys.stdin)
            out_stream = getattr(self, "stream_out", sys.stdout)
            is_interactive = (
                hasattr(in_stream, "isatty")
                and in_stream.isatty()
                and hasattr(out_stream, "isatty")
                and out_stream.isatty()
            )
            if is_interactive:
                from .ui_selector import select_option
                from .providers.cloud_provider import fetch_available_models

                active_provider = "antigravity"
                api_key: str | None = None

                if hasattr(self.orchestrator, "router"):
                    r = self.orchestrator.router
                    cp = getattr(r, "cloud_provider", None)
                    if cp is not None:
                        prov_id = getattr(cp, "provider_id", "")
                        if prov_id == "google_antigravity":
                            active_provider = "antigravity"
                            api_key = getattr(cp, "access_token", None)
                        elif hasattr(cp, "provider_type"):
                            pt = cp.provider_type
                            active_provider = str(getattr(pt, "value", pt)).lower()
                            api_key = getattr(cp, "api_key", None)
                if not active_provider or active_provider not in ("antigravity", "anthropic", "openai", "gemini", "local"):
                    active_provider = (os.getenv("HEX_CLOUD_PROVIDER") or "antigravity").lower()

                if not api_key:
                    if active_provider == "antigravity":
                        api_key = os.getenv("HEX_ANTIGRAVITY_TOKEN") or os.getenv("ANTIGRAVITY_OAUTH_TOKEN")
                    elif active_provider == "anthropic":
                        api_key = os.getenv("HEX_ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
                    elif active_provider == "openai":
                        api_key = os.getenv("HEX_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
                    elif active_provider == "gemini":
                        api_key = os.getenv("HEX_GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY")

                print(f"◈ Querying live models from {active_provider}...")
                models = fetch_available_models(active_provider, api_key=api_key)
                if models:
                    try:
                        chosen_idx = select_option(
                            "Select Active Model:",
                            models,
                            stream_in=in_stream,
                            stream_out=getattr(self, "stream_out", sys.stdout),
                        )
                        chosen_model_id = models[chosen_idx][0]
                        self.model_profile = chosen_model_id
                        if hasattr(self.orchestrator, "router"):
                            self.orchestrator.router.cloud_model_id = chosen_model_id
                            if hasattr(self.orchestrator.router, "cloud_provider"):
                                cp = self.orchestrator.router.cloud_provider
                                if hasattr(cp, "model_id"):
                                    cp.model_id = chosen_model_id

                        if hasattr(self.orchestrator, "set_active_model"):
                            try:
                                res = self.orchestrator.set_active_model(chosen_model_id, durable=True)
                                if isinstance(res, (tuple, list)) and len(res) >= 3:
                                    prov, final_id, saved_path = res[:3]
                                    scope = f" (project override saved to {saved_path})" if saved_path else " (session-only)"
                                    print(f"{ANSI_GREEN}✓ Active model switched to: {final_id}{scope}{ANSI_RESET}")
                                    return
                            except Exception:
                                pass
                        print(f"{ANSI_GREEN}✓ Active model switched to: {chosen_model_id}{ANSI_RESET}")
                        return
                    except Exception as exc:
                        print(f"{ANSI_RED}Model selection cancelled: {exc}{ANSI_RESET}")
                        return

            current = self.model_profile or "hybrid (Two-Tier MLX Local + Cloud)"
            print(f"\n{ANSI_BOLD}{ANSI_CYAN}Active Model Routing Configuration:{ANSI_RESET}")
            print(f"  Profile:          {current}")
            if hasattr(self.orchestrator, "router"):
                r = self.orchestrator.router
                prov_id = getattr(getattr(r, "cloud_provider", None), "provider_id", "cloud_frontier")
                print(f"  Tier-1 Local:     {getattr(r, 'local_model_id', 'local')}")
                print(f"  Tier-2 Provider:  {prov_id}")
                print(f"  Tier-2 Model:     {getattr(r, 'cloud_model_id', 'model')}")
                print(f"  Compressor Mode:  {getattr(r, 'compressor_mode', 'local')}")
                if hasattr(r, "budget_tracker"):
                    bt = r.budget_tracker
                    try:
                        max_usd = float(getattr(bt, "max_usd_per_task", 1.0))
                    except (TypeError, ValueError):
                        max_usd = 1.0
                    print(f"  Task Ceiling:     ${max_usd:.2f} USD")
            print()

    def _cmd_compressor(self, arg: str = "") -> None:
        mode = arg.strip().lower()
        valid = ("deterministic", "cloud", "local", "off", "disabled")
        if mode not in valid:
            cur = getattr(getattr(self.orchestrator, "router", None), "compressor_mode", "deterministic")
            print(f"{ANSI_CYAN}Current compressor mode:{ANSI_RESET} {cur} (Usage: /compressor <deterministic|cloud|local|off>)")
            return
        if hasattr(self.orchestrator, "router"):
            self.orchestrator.router.compressor_mode = "off" if mode == "disabled" else mode
        print(f"{ANSI_GREEN}Compressor mode updated to: {mode} (session-only; sensitive/secret logs remain auto-pinned to local){ANSI_RESET}")

    def _cmd_budget(self) -> None:
        print(f"\n{ANSI_BOLD}{ANSI_CYAN}Session Budget & Token Telemetry:{ANSI_RESET}")
        print(f"  Input Tokens:      {self.session_tokens_in:,}")
        print(f"  Output Tokens:     {self.session_tokens_out:,}")
        print(f"  Tokens Saved:      {self.session_tokens_saved:,}")
        ratio = f"{self.session_compression_ratio * 100:.1f}%" if self.session_compression_ratio > 0 else "0.0%"
        print(f"  Compression Ratio: {ratio}")
        print(f"  Session Cost:      ${self.session_cost_usd:.4f} USD")
        if hasattr(self.orchestrator, "router") and hasattr(self.orchestrator.router, "budget_tracker"):
            bt = self.orchestrator.router.budget_tracker
            spent = getattr(bt, "total_usd_spent", 0.0)
            print(f"  Lifetime Spend:    ${spent:.4f} USD")
        print()

    def _cmd_quota(self, arg: str = "") -> None:
        from .providers.antigravity_provider import AntigravityModelProvider, AntigravityOAuthManager

        print(f"\n{ANSI_BOLD}{ANSI_CYAN}Google Antigravity OAuth & Quota Status:{ANSI_RESET}")
        mgr = AntigravityOAuthManager()
        cred = mgr.resolve_credential(auto_refresh=False)
        if not cred or not (cred.access_token or cred.refresh_token):
            print(f"  Status:           {ANSI_YELLOW}Unauthenticated{ANSI_RESET}")
            print(f"  Action:           Run `hex auth antigravity --auth-url` or sign in to Antigravity IDE")
            print()
            return

        print(f"  Session Status:   {ANSI_GREEN}Active ($0.00 marginal API billing){ANSI_RESET}")
        print(f"  Credential Source:{cred.source}")
        if cred.project_id:
            print(f"  Project ID:       {cred.project_id}")

        prov = getattr(getattr(self.orchestrator, "router", None), "cloud_provider", None)
        if not isinstance(prov, AntigravityModelProvider):
            prov = AntigravityModelProvider(oauth_manager=mgr)
        try:
            snap = prov.fetch_quota_status()
            print(f"  Plan Tier:        {snap.plan_name}")
            if snap.available_prompt_credits is not None:
                print(f"  Prompt Credits:   {snap.available_prompt_credits:,}")
            if snap.models:
                print(f"  Model Quotas:")
                for m in snap.models:
                    rem_pct = round(m.remaining_fraction * 100.0, 1)
                    col = ANSI_GREEN if rem_pct > 30 else (ANSI_YELLOW if rem_pct > 0 else ANSI_RED)
                    rst = f" (resets {m.reset_time})" if m.reset_time else ""
                    print(f"    • {m.model_id:<28} {col}{rem_pct:>5.1f}% remaining{ANSI_RESET}{rst}")
        except Exception as exc:
            print(f"  Live Quota Probe: {ANSI_DIM}{exc}{ANSI_RESET}")
        print()

    def _cmd_blast(self, arg: str = "") -> None:
        target = arg.strip()
        if not hasattr(self.orchestrator, "graphify") or self.orchestrator.graphify is None:
            print(f"{ANSI_YELLOW}Graphify adapter not initialized.{ANSI_RESET}")
            return
        if target in ("--html", "html"):
            out_path = self.workspace_root / ".hex" / "architecture_tree.html"
            if hasattr(self.orchestrator.graphify, "export_interactive_html"):
                saved = self.orchestrator.graphify.export_interactive_html(out_path, workspace_dir=self.workspace_root)
                print(f"{ANSI_GREEN}Interactive D3 Architecture Tree exported to:{ANSI_RESET} {saved}")
                return
        self.orchestrator.graphify.build_or_refresh(self.workspace_root)
        if not target or target.endswith((".py", ".ts", ".tsx", ".js", ".go", ".rs")):
            files = [target] if target else list(self.orchestrator.graphify.symbols_by_file.keys())[:2]
            br = self.orchestrator.graphify.compute_blast_radius(self.workspace_root, files)
            data = br.to_dict()
            data["risk_class"] = "R2" if files else "R0"
            print(render_blast_radius_tree(data))
            if hasattr(self.orchestrator.graphify, "analyze_architecture"):
                arch = self.orchestrator.graphify.analyze_architecture(self.workspace_root, top_n=5)
                print(f"\n{ANSI_DIM}{arch.get('compact_summary', '')}{ANSI_RESET}")
        else:
            q = self.orchestrator.graphify.query_symbol(target)
            data = {
                "modified_files": [f"{d['file']}:{d['qualified_name']}" for d in q.get("definitions", [])] or [target],
                "direct_callers": [c["qualified_name"] for c in q.get("hop1_callers", [])],
                "transitive_callers": [c["qualified_name"] for c in q.get("hop2_callers", [])],
                "pinned_tests": [],
                "risk_class": "R1",
            }
            print(render_blast_radius_tree(data))

    def _cmd_proof(self, arg: str = "") -> None:
        tid = arg.strip() or self.active_task_id
        if not tid and hasattr(self.orchestrator, "ledger"):
            tasks = self.orchestrator.ledger.list_tasks(limit=1)
            if tasks:
                tid = tasks[0]["task_id"]
        if not tid:
            print(f"{ANSI_DIM}No task available for proof inspection.{ANSI_RESET}")
            return
        proof = self.orchestrator.ledger.get_proof_bundle(tid)
        if not proof:
            print(f"{ANSI_YELLOW}No Proof-Carrying Artifact bundle found for task {tid}.{ANSI_RESET}")
            return
        print(f"\n{ANSI_BOLD}{ANSI_CYAN}Proof-Carrying Artifact Bundle ({proof['proof_id']}):{ANSI_RESET}")
        print(f"  Task ID:          {proof['task_id']}")
        print(f"  Workspace SHA:    {proof['workspace_tree_sha']}")
        print(f"  Git Diff SHA:     {proof['git_diff_sha']}")
        print(f"  Ledger Signature: {ANSI_GREEN}{proof['ledger_signature']}{ANSI_RESET}")
        diff_txt = proof.get("git_diff_text", "")
        if diff_txt:
            print(render_unified_diff(diff_txt, title=f"Proof Diff ({tid})"))

    def _cmd_memory(self, arg: str = "") -> None:
        if not hasattr(self.orchestrator, "memory"):
            print(f"{ANSI_DIM}Memory service unavailable.{ANSI_RESET}")
            return
        mems = self.orchestrator.memory.retrieve_memories(
            project_id=self.project_id,
            query=arg.strip(),
            include_personal=True,
            include_episodic=True,
            workspace_dir=self.workspace_root,
        )
        if not mems:
            print(f"{ANSI_DIM}No matching governed memories found.{ANSI_RESET}")
            return
        print(f"\n{ANSI_BOLD}{ANSI_CYAN}Governed Memories ({len(mems)}):{ANSI_RESET}")
        for m in mems:
            stale_tag = f" {ANSI_YELLOW}[STALE - CODE CHANGED]{ANSI_RESET}" if m.get("is_stale") else ""
            outcome_tag = f" [{m.get('outcome', 'useful')}]" if m.get("outcome") else ""
            nodes_str = f" (nodes: {', '.join(m['source_nodes'])})" if m.get("source_nodes") else ""
            print(
                f"  • [{m['scope']}]{outcome_tag}{stale_tag} {ANSI_BOLD}{m['subject']}{ANSI_RESET}: "
                f"{m['content']}{nodes_str} (conf={m['confidence']})"
            )
        print()

    def _cmd_remember(self, arg: str = "") -> None:
        if ":" not in arg:
            print(f"{ANSI_YELLOW}Usage: /remember <subject>: <content>{ANSI_RESET}")
            return
        subj, content = arg.split(":", 1)
        mem = self.orchestrator.memory.write_memory(
            scope="project",
            project_id=self.project_id,
            subject=subj.strip(),
            content=content.strip(),
            workspace_dir=self.workspace_root,
        )
        print(f"{ANSI_GREEN}✓ Stored governed project memory {mem['memory_id']} ('{mem['subject']}').{ANSI_RESET}")

    def _cmd_reflect(self) -> None:
        if not hasattr(self.orchestrator, "graphify") or not hasattr(self.orchestrator.graphify, "reflect_work_memory"):
            print(f"{ANSI_YELLOW}Graphify reflection unavailable.{ANSI_RESET}")
            return
        res = self.orchestrator.graphify.reflect_work_memory(self.workspace_root)
        md = (res.get("lessons_markdown") or "").strip()
        print(f"\n{ANSI_BOLD}{ANSI_CYAN}Graphify Work-Memory Reflection ({res.get('lessons_path')}):{ANSI_RESET}")
        print(md if md else f"{ANSI_DIM}No work-memory outcomes recorded yet.{ANSI_RESET}")
        print()

    def _cmd_search(self, arg: str = "") -> None:
        q = arg.strip()
        if not q:
            print(f"{ANSI_YELLOW}Usage: /search <query>{ANSI_RESET}")
            return
        if hasattr(self.orchestrator, "ledger") and hasattr(self.orchestrator.ledger, "search_tasks"):
            results = self.orchestrator.ledger.search_tasks(q, project_id=self.project_id)
            if not results:
                print(f"{ANSI_DIM}No tasks matched '{q}'.{ANSI_RESET}")
                return
            print(f"\n{ANSI_BOLD}{ANSI_CYAN}FTS5 Search Results ({len(results)}):{ANSI_RESET}")
            for r in results:
                d = r.to_dict() if hasattr(r, "to_dict") else dict(r)
                print(f"  • [{d.get('task_id')}] ({d.get('status')}) score={d.get('score')}: {d.get('snippet')}")
            print()

    def _cmd_doctor(self, arg: str = "") -> None:
        from .doctor import HexDoctor

        doc = HexDoctor(workspace_root=self.workspace_root, data_dir=getattr(self.orchestrator, "data_dir", None))
        rep = doc.run_all(fix=("--fix" in arg), live=("--live" in arg))
        print(rep.format_summary(color=True))

    def _cmd_recipe(self) -> None:
        from .recipes import detect_recipe

        rcp = detect_recipe(self.workspace_root)
        if not rcp:
            print(f"{ANSI_YELLOW}No build/test recipe detected in {self.workspace_root}.{ANSI_RESET}")
            return
        print(f"\n{ANSI_BOLD}{ANSI_CYAN}Detected Project Recipe:{ANSI_RESET} {rcp.name} ({rcp.kind})")
        print(f"  Bootstrap: {rcp.bootstrap or 'None'}")
        print(f"  Build:     {rcp.build or 'None'}")
        print(f"  Test:      {rcp.test}")
        print(f"  Start:     {rcp.start or 'None'}")
        print()

    def _cmd_lsp(self, arg: str = "") -> None:
        from .lsp.manager import LSPManager
        from .lsp.registry import LanguageServerRegistry

        reg = LanguageServerRegistry()
        tokens = arg.strip().split()
        subaction = tokens[0].lower() if tokens else ""
        target_file = tokens[1] if len(tokens) > 1 else None

        mgr = getattr(self.orchestrator, "lsp_manager", None)
        created_mgr = False
        if mgr is None:
            mgr = LSPManager(workspace_root=self.workspace_root, registry=reg)
            created_mgr = True

        try:
            if subaction in ("status", "servers"):
                specs = reg.get_all_specs()
                detected_specs = reg.detect_workspace_servers(self.workspace_root)
                detected_langs = {s.language_id for s in detected_specs}

                print(f"\n{ANSI_BOLD}{ANSI_CYAN}Polyglot Language Server Status ({Path(self.workspace_root).name}):{ANSI_RESET}")
                header = f"  {'LANGUAGE':<12} {'SERVER':<26} {'STATUS':<14} {'EXTENSIONS'}"
                print(f"{ANSI_BOLD}{header}{ANSI_RESET}")
                print(f"  {'-' * (len(header) - 2)}")
                for spec in specs:
                    installed_bin = spec.resolve_binary()
                    badge = f"{ANSI_GREEN}[INSTALLED]{ANSI_RESET}" if installed_bin else f"{ANSI_DIM}[NOT FOUND]{ANSI_RESET}"
                    det = " *" if spec.language_id in detected_langs else ""
                    exts = ", ".join(spec.file_extensions)
                    print(f"  {spec.language_id + det:<12} {spec.server_name:<26} {badge:<23} {exts}")
                print(f"\n{ANSI_DIM}  (* = detected in workspace root){ANSI_RESET}\n")

            elif subaction in ("diagnostics", "diag"):
                f_path = (self.workspace_root / target_file).resolve() if target_file else None
                if f_path and f_path.is_file():
                    mgr.sync_file(f_path)
                    diags = mgr.get_diagnostics(file_path=f_path)
                else:
                    diags = mgr.get_diagnostics()

                errors = [d for d in diags if d.severity == 1 or getattr(d, "severity_name", "") == "ERROR"]
                warnings = [d for d in diags if d.severity == 2 or getattr(d, "severity_name", "") == "WARNING"]

                scope_label = target_file if target_file else "Workspace"
                print(f"\n{ANSI_BOLD}{ANSI_CYAN}LSP Compiler Diagnostics — {scope_label}:{ANSI_RESET}")
                print(f"  Errors: {ANSI_RED}{len(errors)}{ANSI_RESET} | Warnings: {ANSI_YELLOW}{len(warnings)}{ANSI_RESET} | Total: {len(diags)}")

                if not diags:
                    print(f"  {ANSI_GREEN}No compiler syntax errors or warnings detected.{ANSI_RESET}\n")
                else:
                    print()
                    for d in diags:
                        rel_p = str(d.file_path)
                        try:
                            rel_p = str(Path(d.file_path).relative_to(self.workspace_root))
                        except Exception:
                            pass
                        sev_badge = f"{ANSI_RED}[ERROR]{ANSI_RESET}" if d.severity == 1 or getattr(d, "severity_name", "") == "ERROR" else f"{ANSI_YELLOW}[WARN]{ANSI_RESET}"
                        code_str = f" [{d.code}]" if getattr(d, "code", None) else ""
                        print(f"  {rel_p}:{d.line + 1}:{d.character + 1} {sev_badge}{code_str} {d.message}")
                    print()

            else:
                detected_specs = reg.detect_workspace_servers(self.workspace_root)
                installed_detected = [s for s in detected_specs if s.resolve_binary()]
                diags = mgr.get_diagnostics()
                errors = [d for d in diags if d.severity == 1 or getattr(d, "severity_name", "") == "ERROR"]
                warnings = [d for d in diags if d.severity == 2 or getattr(d, "severity_name", "") == "WARNING"]

                print(f"\n{ANSI_BOLD}{ANSI_CYAN}Language Server Protocol (LSP) Gateway Summary:{ANSI_RESET}")
                print(f"  Workspace:        {self.workspace_root}")
                active_langs = mgr.active_languages
                print(f"  Active Servers:   {', '.join(active_langs) if active_langs else '(none running)'}")
                detected_names = [f"{s.language_id}:{s.server_name}" for s in installed_detected]
                print(f"  Detected Specs:   {', '.join(detected_names) if detected_names else '(none installed)'}")
                print(f"  Compiler Status:  Errors: {ANSI_RED}{len(errors)}{ANSI_RESET} | Warnings: {ANSI_YELLOW}{len(warnings)}{ANSI_RESET}")
                if errors:
                    print(f"\n{ANSI_BOLD}{ANSI_RED}Top Fatal Syntax / Compiler Errors:{ANSI_RESET}")
                    for d in errors[:5]:
                        rel_p = str(d.file_path)
                        try:
                            rel_p = str(Path(d.file_path).relative_to(self.workspace_root))
                        except Exception:
                            pass
                        print(f"  • {rel_p}:{d.line + 1}:{d.character + 1} - {d.message}")
                print(f"\n{ANSI_DIM}  Use '/lsp status' for full server matrix, '/lsp diagnostics [file]' for details.{ANSI_RESET}\n")

        finally:
            if created_mgr:
                mgr.close_all()

    def _cmd_voice(self, arg: str = "") -> None:
        sub = arg.strip().split()
        subcmd = sub[0].lower() if sub else "status"
        param = sub[1] if len(sub) > 1 else ""

        if subcmd == "on":
            self.voice_engine.config.tts_enabled = True
            self.voice_engine.config.stt_enabled = True
            self.voice_engine.unmute()
            self.voice_engine.speak("Voice mode enabled", clean=False)
            print(f"\n{ANSI_GREEN}✔ Voice mode enabled (TTS and speech capture active).{ANSI_RESET}\n")
        elif subcmd == "off":
            self.voice_engine.config.tts_enabled = False
            self.voice_engine.config.stt_enabled = False
            self.voice_engine.stop()
            print(f"\n{ANSI_YELLOW}⏹ Voice mode disabled.{ANSI_RESET}\n")
        elif subcmd == "mute":
            is_muted = self.voice_engine.toggle_mute()
            if is_muted:
                print(f"\n{ANSI_YELLOW}🔇 Voice muted.{ANSI_RESET}\n")
            else:
                print(f"\n{ANSI_GREEN}🎙️ Voice unmuted.{ANSI_RESET}\n")
        elif subcmd == "voice":
            if param:
                self.voice_engine.set_voice(param)
                print(f"\n{ANSI_GREEN}✔ Voice set to: {param}{ANSI_RESET}\n")
            else:
                curr = self.voice_engine.config.voice_name or "(system default)"
                print(f"\n{ANSI_CYAN}Current voice: {curr}{ANSI_RESET}\n")
        elif subcmd == "rate":
            if param:
                try:
                    wpm = int(param)
                    self.voice_engine.set_rate(wpm)
                    print(f"\n{ANSI_GREEN}✔ Speech rate set to: {self.voice_engine.config.speech_rate} wpm{ANSI_RESET}\n")
                except ValueError:
                    print(f"\n{ANSI_RED}Invalid speech rate: {param}. Please provide an integer (80-400).{ANSI_RESET}\n")
            else:
                print(f"\n{ANSI_CYAN}Current speech rate: {self.voice_engine.config.speech_rate} wpm{ANSI_RESET}\n")
        elif subcmd == "status":
            from .voice import probe_audio_hardware

            hw = probe_audio_hardware()
            v_stat = (
                "MUTED"
                if self.voice_engine.is_muted()
                else ("ON" if self.voice_engine.config.tts_enabled else "OFF")
            )
            print(f"\n{ANSI_BOLD}{ANSI_CYAN}HEX Hands-Free Voice Status:{ANSI_RESET}")
            print(f"  Voice Mode:     {v_stat}")
            print(f"  TTS Enabled:    {self.voice_engine.config.tts_enabled}")
            print(f"  STT Enabled:    {self.voice_engine.config.stt_enabled}")
            print(f"  Muted:          {self.voice_engine.is_muted()}")
            print(f"  Active Voice:   {self.voice_engine.config.voice_name or '(default)'}")
            print(f"  Speech Rate:    {self.voice_engine.config.speech_rate} wpm")
            print(f"  Backend:        {self.voice_engine.config.backend}")
            print(f"  Host Platform:  {hw.get('platform')}")
            print(f"  TTS Synthesizer:{hw.get('tts_engine') or 'None (using mock)'}")
            print(f"  Audio Output:   {hw.get('audio_player') or 'None'}")
            print(f"  Audio Input:    {hw.get('mic_tool') or 'None'}\n")
        else:
            print(f"\n{ANSI_RED}Usage: /voice [on|off|mute|status|rate <wpm>|voice <name>]{ANSI_RESET}\n")

    def _cmd_backup(self) -> None:
        from .backup import create_backup

        res = create_backup(data_dir=getattr(self.orchestrator, "data_dir", None), workspace_root=self.workspace_root, quick=True)
        print(f"{ANSI_GREEN}✓ Quick state backup created:{ANSI_RESET} {res['backup_path']} ({res['files_archived']} files)")

    def _cmd_dump(self) -> None:
        from .backup import generate_dump

        print(generate_dump(data_dir=getattr(self.orchestrator, "data_dir", None), workspace_root=self.workspace_root))

    def _cmd_workers(self) -> None:
        workers = self.orchestrator.ledger.list_workers() if hasattr(self.orchestrator, "ledger") else []
        print(f"\n{ANSI_BOLD}{ANSI_CYAN}Registered Distributed Workers ({len(workers)}):{ANSI_RESET}")
        for w in workers:
            print(
                f"  • {ANSI_BOLD}{w['worker_id']}{ANSI_RESET} [{w['machine_class']}] "
                f"CoW={w['cow_backend']} Health={w.get('health', 'HEALTHY')} Heartbeat={w.get('last_heartbeat_at')}"
            )
        print()

    def _cmd_harness2(self) -> None:
        if not hasattr(self.orchestrator, "harness2_adapter"):
            print(f"{ANSI_YELLOW}Harness² adapter not available.{ANSI_RESET}")
            return
        res = self.orchestrator.harness2_adapter.propose_and_evaluate_candidate(
            parent_revision_id=None,
            base_tree={"systemprompt.md": "# Base Agent Instructions\nUse read_file.\n"},
            candidate_tree={"systemprompt.md": "# Refined Agent Instructions\nUse read_file and query_graphify.\n"},
            run_demo_probe=True,
        )
        print(
            f"{ANSI_GREEN}✓ Harness² Propose-Probe-Compose complete:{ANSI_RESET} "
            f"Revision {res['revision_id']} ({res['status']}) · Eval {res['evaluation_id']}"
        )

    def _cmd_skin(self, arg: str = "") -> None:
        name = arg.strip().lower()
        if not name:
            print(f"{ANSI_CYAN}Available skins:{ANSI_RESET} {', '.join(SKIN_REGISTRY.keys())} (Active: {self.skin_name})")
            return
        if name not in SKIN_REGISTRY:
            print(f"{ANSI_RED}Unknown skin '{name}'. Choose from: {', '.join(SKIN_REGISTRY.keys())}{ANSI_RESET}")
            return
        self.skin_name = name
        print(f"{ANSI_GREEN}Skin updated to: {name} (session-only; {self.skin.description}){ANSI_RESET}")

    def _cmd_project(self, arg: str = "") -> None:
        from .project_registry import ProjectRegistry

        registry = ProjectRegistry()
        target = arg.strip()

        if not target:
            rec = registry.detect_enclosing_project(self.workspace_root)
            proj_name = rec.name if rec else self.workspace_root.name
            stack = rec.stack if rec else "generic"
            data_dir = self.workspace_root / ".hex" / "data"
            lessons_file = self.workspace_root / ".hex" / "reflections" / "LESSONS.md"
            lessons_count = 0
            if lessons_file.is_file():
                try:
                    lines = [
                        line
                        for line in lessons_file.read_text(encoding="utf-8").splitlines()
                        if line.strip().startswith("- ") or line.strip().startswith("* ")
                    ]
                    lessons_count = len(lines)
                except Exception:
                    pass

            print(f"\n{ANSI_BOLD}{ANSI_CYAN}Active Project Workspace:{ANSI_RESET}")
            print(f"  Name:          {proj_name}")
            print(f"  Path:          {self.workspace_root}")
            print(f"  Stack:         {stack}")
            print(f"  Lessons:       {lessons_count} recorded in .hex/reflections/LESSONS.md")
            print(f"  Data Dir:      {data_dir if data_dir.is_dir() else getattr(self.orchestrator, 'data_dir', 'default')}")
            print(f"  Config:        {self.workspace_root / '.hex' / 'config.yaml'}")
            print()
            return

        if target in ("list", "ls"):
            projects = registry.list_projects()
            print(f"\n{ANSI_BOLD}{ANSI_CYAN}Registered Project Workspaces ({len(projects)}):{ANSI_RESET}")
            if not projects:
                print("  No projects registered yet. Use '/project switch <path>' or 'hex project add <path>'.")
            else:
                print(f"  {'PROJECT':<20} {'STACK':<10} {'TASKS':<8} {'LAST ACTIVE':<24} {'PATH'}")
                print("  " + "-" * 78)
                for p in projects:
                    active_marker = " *" if str(Path(p.path).resolve()) == str(self.workspace_root.resolve()) else "  "
                    count = p.task_count
                    if hasattr(self.orchestrator, "ledger") and hasattr(self.orchestrator.ledger, "count_project_tasks"):
                        try:
                            count = self.orchestrator.ledger.count_project_tasks(p.id)
                        except Exception:
                            pass
                    print(f"{active_marker}{p.name:<20} {p.stack:<10} {count:<8} {p.last_active:<24} {p.path}")
            print()
            return

        switch_target = target[7:].strip() if target.startswith("switch ") else target
        rec = registry.get_project(switch_target)
        matches = registry.find_name_matches(switch_target)
        if not rec and len(matches) > 1:
            print(f"{ANSI_YELLOW}Project name '{switch_target}' is ambiguous. Use one of these paths or IDs:{ANSI_RESET}")
            for match in matches:
                print(f"  • {match.id}  {match.path}")
            return
        if not rec and (Path(switch_target).exists() or Path(switch_target).is_dir()):
            rec = registry.register_project(Path(switch_target).resolve())

        if not rec:
            print(f"{ANSI_RED}Project '{switch_target}' not found in registry. Type '/project list' to view registered projects.{ANSI_RESET}")
            return

        new_ws = Path(rec.path).resolve()
        if hasattr(self.orchestrator, "switch_project"):
            sw = self.orchestrator.switch_project(new_ws, rec.id)
            self.workspace_root = Path(sw["workspace_root"])
            self.project_id = sw["project_id"]
            self.model_profile = sw.get("cloud_model_id") or self.model_profile
            print(f"\n{ANSI_GREEN}✓ Switched active project to '{rec.name}' ({rec.stack}){ANSI_RESET}")
            print(f"  Workspace: {new_ws}")
            print(f"  Config:    {sw.get('config_path') or rec.config_path}")
            print(f"  Model:     {sw.get('cloud_model_id')} ({sw.get('cloud_provider')})\n")
        else:
            self.workspace_root = new_ws
            self.project_id = rec.id
            registry.touch_active(new_ws)
            if hasattr(self.orchestrator, "graphify"):
                self.orchestrator.graphify.workspace_root = new_ws
                try:
                    self.orchestrator.graphify.build_or_refresh(new_ws)
                except Exception:
                    pass
            proj_data = new_ws / ".hex" / "data"
            if proj_data.is_dir() and hasattr(self.orchestrator, "data_dir"):
                self.orchestrator.data_dir = proj_data
            print(f"\n{ANSI_GREEN}✓ Switched active project to '{rec.name}' ({rec.stack}){ANSI_RESET}")
            print(f"  Workspace: {new_ws}")
            print(f"  Config:    {rec.config_path}\n")

    def _cmd_clear(self) -> None:
        if os.name == "nt":
            os.system("cls")
        else:
            sys.stdout.write("\033[H\033[2J")
            sys.stdout.flush()

    def handle_approval_prompt(
        self,
        apr_interrupt: ApprovalRequiredInterrupt,
        prompt_fn: Callable[[str], str] | None = None,
        shell_runner: Callable[[str | Path], Any] | None = None,
    ) -> dict[str, Any]:
        """Intercept privileged actions and prompt operator for interactive approval.

        Options:
        - [y] Approve: records APPROVED decision in ledger and resumes task execution.
        - [n] Reject: prompts for optional comment, records REJECTED decision.
        - [d] Full Diff: renders unified diff between base checkpoint and attempt workspace.
        - [s] Sandbox Shell: spawns subshell inside isolated CoW attempt directory.
        - [q] Abort: cancels task via cancel_task and rolls back uncommitted workspace.
        """
        approval = apr_interrupt.approval_record
        approval_id = approval.get("approval_id", "unknown")
        task_id = approval.get("task_id", "")
        step_id = approval.get("step_id", "")
        action_type = approval.get("action_type", "unknown")
        risk_class = approval.get("risk_class", "R3")
        blast_radius = approval.get("blast_radius_summary") or "(none)"
        action_params = approval.get("action_params") or {}
        redacted_params = redact_secrets(action_params)

        badge = RISK_BADGES.get(risk_class[:2], f"[{risk_class}]")

        print()
        print(f"{ANSI_BOLD}{ANSI_MAGENTA}🚨 Approval Required: {action_type} ({risk_class}){ANSI_RESET}")
        print("─" * 70)
        print(f"  {ANSI_BOLD}Approval ID:{ANSI_RESET}   {approval_id}")
        print(f"  {ANSI_BOLD}Task ID:{ANSI_RESET}       {task_id}")
        print(f"  {ANSI_BOLD}Step ID:{ANSI_RESET}       {step_id}")
        print(f"  {ANSI_BOLD}Risk Class:{ANSI_RESET}    {badge}")
        print(f"  {ANSI_BOLD}Blast Radius:{ANSI_RESET}  {blast_radius}")
        param_str = json.dumps(redacted_params, indent=2) if redacted_params else "{}"
        if "\n" in param_str:
            print(f"  {ANSI_BOLD}Parameters:{ANSI_RESET}")
            for pline in param_str.splitlines():
                print(f"    {ANSI_DIM}{pline}{ANSI_RESET}")
        else:
            print(f"  {ANSI_BOLD}Parameters:{ANSI_RESET}    {ANSI_DIM}{param_str}{ANSI_RESET}")
        print("─" * 70)
        print(f"  {ANSI_BOLD}Options:{ANSI_RESET} [y] Approve   [n] Reject   [d] Full Diff   [s] Sandbox Shell   [q] Abort")
        print()

        reader = prompt_fn if prompt_fn is not None else input

        while True:
            try:
                choice = reader("approval [y/n/d/s/q]> ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print(f"\n{ANSI_YELLOW}Approval input closed/interrupted; aborting task safely.{ANSI_RESET}")
                if hasattr(self.orchestrator, "cancel_task"):
                    try:
                        self.orchestrator.cancel_task(task_id, actor="owner")
                    except Exception:
                        pass
                return {
                    "decision": "ABORTED",
                    "approval_id": approval_id,
                    "task_id": task_id,
                }

            if not choice:
                continue

            if choice in ("y", "approve"):
                res = self.orchestrator.ledger.decide_approval(
                    approval_id, "APPROVED", decided_by="owner"
                )
                print(f"{ANSI_GREEN}✓ Approval recorded. Resuming task execution...{ANSI_RESET}")
                if hasattr(self.orchestrator, "resume_task"):
                    try:
                        task_res = self.orchestrator.resume_task(task_id)
                        res["task"] = task_res
                    except ApprovalRequiredInterrupt as next_apr:
                        return self.handle_approval_prompt(next_apr, prompt_fn=prompt_fn, shell_runner=shell_runner)
                    except Exception as exc:
                        print(f"{ANSI_RED}Resume task failed: {exc}{ANSI_RESET}")
                return res

            elif choice in ("n", "reject"):
                try:
                    comment = reader("Rejection comment (optional): ").strip()
                except (EOFError, KeyboardInterrupt):
                    comment = ""
                res = self.orchestrator.ledger.decide_approval(
                    approval_id, "REJECTED", decided_by="owner", comment=comment
                )
                print(f"{ANSI_RED}✗ Action rejected by operator.{ANSI_RESET}")
                return res

            elif choice in ("d", "diff"):
                diff_text = ""
                attempt_ws = None
                base_snap_path = None
                if hasattr(self.orchestrator, "ledger"):
                    attempts = self.orchestrator.ledger.list_attempts(task_id)
                    chk = self.orchestrator.ledger.get_latest_checkpoint(task_id)
                    if attempts:
                        attempt_ws = attempts[-1].get("workspace_path")
                    if chk:
                        base_snap_path = chk.get("snapshot_path")

                if attempt_ws and base_snap_path and Path(attempt_ws).is_dir() and Path(base_snap_path).is_dir():
                    if hasattr(self.orchestrator, "cow") and hasattr(self.orchestrator.cow, "compute_workspace_diff"):
                        diff_text = self.orchestrator.cow.compute_workspace_diff(base_snap_path, attempt_ws)
                    else:
                        from .cow import compute_workspace_diff
                        diff_text = compute_workspace_diff(base_snap_path, attempt_ws)

                if not diff_text or not diff_text.strip():
                    print(f"{ANSI_DIM}--- Proposed Changes: (no changes) ---{ANSI_RESET}")
                else:
                    print(render_unified_diff(diff_text, title=f"Approval Workspace Diff ({approval_id})"))
                print(f"\n{ANSI_BOLD}Options:{ANSI_RESET} [y] Approve   [n] Reject   [d] Full Diff   [s] Sandbox Shell   [q] Abort\n")

            elif choice in ("s", "shell"):
                attempt_ws = None
                if hasattr(self.orchestrator, "ledger"):
                    attempts = self.orchestrator.ledger.list_attempts(task_id)
                    if attempts:
                        attempt_ws = attempts[-1].get("workspace_path")
                if not attempt_ws or not Path(attempt_ws).is_dir():
                    attempt_ws = str(self.workspace_root)

                print(f"{ANSI_YELLOW}Entering isolated CoW attempt subshell at {attempt_ws} (Type 'exit' to return)...{ANSI_RESET}")
                if shell_runner is not None:
                    shell_runner(attempt_ws)
                else:
                    sub_shell = os.environ.get("SHELL", "/bin/sh")
                    subprocess.run([sub_shell], cwd=attempt_ws)
                print(f"{ANSI_DIM}Returned from subshell.{ANSI_RESET}")
                print(f"\n{ANSI_BOLD}Options:{ANSI_RESET} [y] Approve   [n] Reject   [d] Full Diff   [s] Sandbox Shell   [q] Abort\n")

            elif choice in ("q", "abort"):
                if hasattr(self.orchestrator, "cancel_task"):
                    self.orchestrator.cancel_task(task_id, actor="owner")
                print(f"{ANSI_YELLOW}Task aborted by operator; uncommitted CoW changes rolled back.{ANSI_RESET}")
                return {
                    "decision": "ABORTED",
                    "approval_id": approval_id,
                    "task_id": task_id,
                }

            else:
                print(f"{ANSI_YELLOW}Invalid choice: '{choice}'. Enter [y] Approve, [n] Reject, [d] Full Diff, [s] Sandbox Shell, or [q] Abort.{ANSI_RESET}")

    def render_activity_feed(self, task_id: str) -> str:
        """Render Hermes-style `┊` vertical activity feed for tool calls and AST blast-radius checks."""
        if not hasattr(self.orchestrator, "ledger"):
            return ""
        calls = self.orchestrator.ledger.list_tool_calls(task_id)
        lines: list[str] = []
        for c in calls:
            tool_id = c.get("tool_id", "tool")
            risk = c.get("risk_class", "R1")
            status = c.get("status", "SEALED")
            res = c.get("result") or {}
            is_replay = bool(c.get("replayed_from_call_id") or (isinstance(res, dict) and res.get("_replayed")))
            dur = res.get("_duration_ms", 0.0) if isinstance(res, dict) else 0.0

            if is_replay:
                badge = f"{ANSI_YELLOW}↻ [replayed]{ANSI_RESET}"
            elif status == "SEALED":
                if dur >= 1000.0:
                    badge = f"{ANSI_GREEN}✓ ({dur/1000.0:.1f}s){ANSI_RESET}"
                else:
                    badge = f"{ANSI_GREEN}✓{ANSI_RESET}"
            elif status in ("FAILED", "ERROR"):
                err_msg = res.get("error", status) if isinstance(res, dict) else status
                badge = f"{ANSI_RED}✗ [{err_msg}]{ANSI_RESET}"
            else:
                badge = f"{ANSI_CYAN}[{status}]{ANSI_RESET}"

            lines.append(f"  {ANSI_CYAN}┊{ANSI_RESET} ⚙ {ANSI_BOLD}{tool_id}{ANSI_RESET} ({risk}) {badge}")
            if isinstance(res, dict) and "ast_blast_radius" in res:
                warn_msg = res["ast_blast_radius"].get("warning_message", "")
                if warn_msg:
                    lines.append(f"  {ANSI_CYAN}┊{ANSI_RESET}   ↳ {ANSI_YELLOW}{warn_msg}{ANSI_RESET}")
        return "\n".join(lines)

    def handle_user_prompt(self, prompt: str) -> None:
        """Submit natural language coding prompt to orchestrator."""
        from .models import TaskSubmitRequest

        mode_val = getattr(self, "mode", "agent")
        effective_prompt = prompt
        if mode_val in ("plan", "ask"):
            effective_prompt = f"[MODE: PLAN - READ-ONLY ARCHITECTURE PLANNING & SPECIFICATION / DO NOT MODIFY FILES OR RUN MUTATING TOOLS]\n{prompt}"
        elif mode_val == "code":
            effective_prompt = f"[MODE: CODE - DIRECT CODE SYNTHESIS & TARGETED FILE EDITS]\n{prompt}"

        print(f"{ANSI_CYAN}⚡ Submitting task to HEX orchestrator ({mode_val} mode)...{ANSI_RESET}")
        req = TaskSubmitRequest(
            goal=effective_prompt,
            project_id=self.project_id,
            workspace_root=str(self.workspace_root) if self.workspace_root else None,
            execution_mode="fast_path" if mode_val in ("plan", "ask") else "auto",
        )

        spinner = ActivitySpinner(self.skin, enabled=not self.quiet)
        spinner.start()
        try:
            task = self.orchestrator.submit_and_run(
                req,
                auto_execute=True,
                on_submitted=lambda submitted: setattr(self, "active_task_id", submitted.get("task_id")),
            )
        except KeyboardInterrupt:
            spinner.stop()
            print()
            canceled = False
            cancel_error = ""
            if getattr(self, "active_task_id", None) and hasattr(self.orchestrator, "cancel_task"):
                try:
                    result = self.orchestrator.cancel_task(self.active_task_id, actor="owner")
                    canceled = result.get("status") == "CANCELED" if isinstance(result, dict) else False
                except Exception as exc:
                    cancel_error = str(exc)
            if canceled:
                print(f"{ANSI_YELLOW}⚡ Task cancellation confirmed.{ANSI_RESET}\n")
            else:
                detail = f": {cancel_error}" if cancel_error else ""
                print(f"{ANSI_YELLOW}⚡ Cancellation requested but not confirmed for {self.active_task_id or 'the task'}{detail}{ANSI_RESET}\n")
            return
        except ApprovalRequiredInterrupt as apr_int:
            spinner.stop()
            apr_res = self.handle_approval_prompt(apr_int)
            task = apr_res.get("task") if isinstance(apr_res, dict) else None
            if not task:
                return
        except Exception as exc:
            spinner.stop()
            from .providers.base import ProviderUnavailableError

            is_auth_error = "401" in str(exc) or "UNAUTHENTICATED" in str(exc)
            err_title = (
                "Model Provider Error"
                if isinstance(exc, ProviderUnavailableError) or is_auth_error
                else "Execution Error"
            )

            def _col(txt: str, fg: tuple[int, int, int] | None = None, bold: bool = False) -> str:
                return colorize_rgb(txt, fg=fg, bold=bold)

            print()
            print(_col(f"✖ {err_title}: {exc}", fg=self.skin.danger_rgb, bold=True))
            if is_auth_error:
                print(_col("  Your authentication credentials are invalid or expired.", fg=self.skin.category_rgb))
                print(_col("  Options to resolve:", fg=self.skin.muted_rgb))
                print(_col("    • Re-authenticate Antigravity: hex auth antigravity --auth-url", fg=self.skin.category_rgb))
                print(_col("    • Re-run setup wizard:         hex setup", fg=self.skin.category_rgb))
                print(_col("    • Switch model provider:       /model (anthropic / openai / gemini / local)", fg=self.skin.category_rgb))
            else:
                print(_col("  Tip: Type /model to inspect active provider, or /help for commands.", fg=self.skin.muted_rgb))
            print()
            return
        finally:
            spinner.stop()

        self.active_task_id = task.get("task_id") if task else None
        tid = self.active_task_id
        status = task.get("status") if task else None

        task_seq = 1
        if tid and hasattr(self.orchestrator, "ledger") and hasattr(self.orchestrator.ledger, "get_project_task_sequence"):
            try:
                task_seq = self.orchestrator.ledger.get_project_task_sequence(tid, self.project_id)
            except Exception:
                task_seq = 1
        task_label = f"Task #{task_seq}"

        if status == "WAITING_APPROVAL":
            if hasattr(self.orchestrator, "ledger") and hasattr(self.orchestrator.ledger, "list_approvals"):
                try:
                    approvals = self.orchestrator.ledger.list_approvals(task_id=tid, decision="PENDING")
                    if approvals:
                        apr_res = self.handle_approval_prompt(ApprovalRequiredInterrupt(approvals[-1]))
                        if isinstance(apr_res, dict) and apr_res.get("task"):
                            task = apr_res["task"]
                            status = task.get("status")
                except Exception:
                    pass

        if self.stream:
            renderer = StreamingTokenRenderer(output_stream=sys.stdout)
            renderer.feed(f"{ANSI_DIM}Executing {task_label}...{ANSI_RESET}\n")
            renderer.flush()

        if tid:
            feed = self.render_activity_feed(tid)
            if feed:
                print(feed)

        if status in ("COMPLETED", "SUCCEEDED"):
            print(f"{ANSI_GREEN}✔ {task_label} completed successfully!{ANSI_RESET}")
            proof = self.orchestrator.ledger.get_proof_bundle(tid) if hasattr(self.orchestrator, "ledger") else None

            # Render assistant's synthesized response / deliverable
            result_text = (
                task.get("result")
                or task.get("deliverable")
                or (task.get("payload") or {}).get("result")
                or (proof.get("summary") if proof else None)
            )
            if result_text:
                active_model = self.model_profile or getattr(getattr(self.orchestrator, "router", None), "cloud_model_id", "model")
                print(f"\n{ANSI_BOLD}HEX Assistant{ANSI_RESET} ({active_model}):\n")
                print(format_terminal_markdown(result_text.strip(), skin=self.skin))
                print()
            if hasattr(self, "voice_engine") and self.voice_engine.config.tts_enabled and not self.voice_engine.is_muted():
                raw_summary = (
                    task.get("summary")
                    or task.get("result")
                    or (proof.get("summary") if proof else None)
                    or f"{task_label} completed successfully."
                )
                cleaned = clean_text_for_speech(str(raw_summary))
                sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", cleaned) if s.strip()]
                speech_text = " ".join(sentences[:2]) if sentences else f"{task_label} completed successfully."
                self.voice_engine.speak(speech_text, clean=False)
        elif status == "REJECTED":
            print(f"{ANSI_RED}✖ {task_label} rejected: {task.get('terminal_reason')}{ANSI_RESET}")
            if hasattr(self, "voice_engine") and self.voice_engine.config.tts_enabled and not self.voice_engine.is_muted():
                reason = task.get("terminal_reason") or "Task rejected."
                self.voice_engine.speak(f"Task rejected. {reason}", clean=True)
        elif status == "NEEDS_ATTENTION":
            print(f"{ANSI_YELLOW}⚠ {task_label} requires operator attention: {task.get('terminal_reason')}{ANSI_RESET}")
            if hasattr(self, "voice_engine") and self.voice_engine.config.tts_enabled and not self.voice_engine.is_muted():
                self.voice_engine.speak("Task requires operator attention.", clean=True)
        else:
            print(f"{task_label} status: {status}")


# Alias for backward & forward compatibility
HarnessTUI = HexConsole
