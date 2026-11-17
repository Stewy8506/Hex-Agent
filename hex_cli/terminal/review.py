from __future__ import annotations

import difflib
from typing import Any

from harness_core.client.terminal.ansi import (
    ANSI_BOLD,
    ANSI_CYAN,
    ANSI_DIM,
    ANSI_GREEN,
    ANSI_MAGENTA,
    ANSI_RED,
    ANSI_RESET,
    ANSI_YELLOW,
    _pad_visible,
)


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
