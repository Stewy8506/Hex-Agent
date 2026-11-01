"""Interactive Terminal UI (TUI) Engine for HEX Autonomous AI Workbench (PRD Section 8, Pillar 2).

Provides:
- Rich / ANSI terminal compatibility with zero-dependency ANSI fallbacks.
- Unified and side-by-side colorized diff visualizers for CoW patches.
- Active Graphify AST blast-radius tree rendering with risk classification badges.
- Streaming token renderer with Markdown code-fence detection and syntax highlighting.
- Session banner and status headers.
- `HexConsole`: Interactive pair-programming REPL with slash command dispatch.
"""

from __future__ import annotations

import difflib
import os
import re
import sys
from io import StringIO
from pathlib import Path
from typing import Any, Callable, TextIO

# Optional rich imports for enhanced terminal aesthetics
try:
    import rich
    from rich.console import Console as RichConsole
    from rich.panel import Panel as RichPanel
    from rich.syntax import Syntax as RichSyntax
    from rich.table import Table as RichTable
    from rich.tree import Tree as RichTree
    from rich.markdown import Markdown as RichMarkdown
    HAS_RICH = True
except ImportError:
    HAS_RICH = False


# Standard ANSI terminal escape sequences
ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_DIM = "\033[2m"
ANSI_RED = "\033[31m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_BLUE = "\033[34m"
ANSI_MAGENTA = "\033[35m"
ANSI_CYAN = "\033[36m"
ANSI_WHITE = "\033[37m"

ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def strip_ansi(text: str) -> str:
    """Strip ANSI escape sequences from text for accurate column width calculations."""
    return ANSI_ESCAPE_RE.sub("", text)


def _pad_visible(content: str, width: int) -> str:
    """Pad content with trailing spaces to match visible character width."""
    vis_len = len(strip_ansi(content))
    pad = max(0, width - vis_len)
    return content + (" " * pad)


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
        or []
    )
    transitive = (
        blast_radius_data.get("transitive_callers")
        or blast_radius_data.get("2_hop_callers")
        or []
    )
    pinned = blast_radius_data.get("pinned_tests") or []

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


# ==============================================================================
# Streaming Token Renderer
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


# ==============================================================================
# Session Banner
# ==============================================================================


def render_session_banner(
    model_id: str = "claude-3-5-sonnet",
    compressor_mode: str = "M4 MLX Local",
    sandbox_backend: str = "bwrap",
    cow_backend: str = "apfs",
    width: int = 78,
) -> str:
    """Render the HEX pair-programming session header banner."""
    title = "HEX Autonomous AI Workbench — Interactive Terminal Console"
    line1 = f"Model: {model_id}  │  Compressor: {compressor_mode}"
    line2 = f"Sandbox: {sandbox_backend}  │  CoW Store: {cow_backend}"
    hint = "Type /help for slash commands, or enter a natural language prompt."

    inner_w = width - 4
    t_pad = _pad_visible(f"  {ANSI_YELLOW}⚡ {ANSI_BOLD}{title}{ANSI_RESET}", inner_w)
    l1_pad = _pad_visible(f"  {ANSI_WHITE}{line1}{ANSI_RESET}", inner_w)
    l2_pad = _pad_visible(f"  {ANSI_WHITE}{line2}{ANSI_RESET}", inner_w)
    h_pad = _pad_visible(f"  {ANSI_DIM}{hint}{ANSI_RESET}", inner_w)

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
