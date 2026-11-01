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
        self.active_task_id: str | None = None
        self.history: list[str] = []

        # Session token & budget telemetry
        self.session_tokens_in: int = 0
        self.session_tokens_out: int = 0
        self.session_tokens_saved: int = 0
        self.session_compression_ratio: float = 0.0
        self.session_cost_usd: float = 0.0

    def start(self, input_fn: Callable[[str], str] | None = None) -> int:
        """Start interactive REPL prompt loop. Returns exit code 0 on termination."""
        cow_backend = getattr(getattr(self.orchestrator, "cow", None), "backend_name", "apfs")
        banner = render_session_banner(
            model_id=self.model_profile or "claude-3-5-sonnet",
            cow_backend=cow_backend,
        )
        print(banner)

        if input_fn is not None:
            read_line = input_fn
        else:
            read_line = input

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

            if line.startswith("/"):
                should_exit = self.handle_slash_command(line)
                if should_exit:
                    return 0
            else:
                self.handle_user_prompt(line)

        return 0

    def handle_slash_command(self, cmd_line: str) -> bool:
        """Dispatch slash commands. Returns True if REPL should terminate, False otherwise."""
        parts = cmd_line.strip().split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd in ("/quit", "/exit"):
            print(f"{ANSI_DIM}Exiting HEX console.{ANSI_RESET}")
            return True
        elif cmd == "/help":
            self._cmd_help()
        elif cmd == "/status":
            self._cmd_status()
        elif cmd == "/diff":
            self._cmd_diff()
        elif cmd == "/rollback":
            self._cmd_rollback()
        elif cmd == "/fork":
            self._cmd_fork()
        elif cmd == "/skills":
            self._cmd_skills()
        elif cmd == "/model":
            self._cmd_model(arg)
        elif cmd == "/budget":
            self._cmd_budget()
        elif cmd == "/clear":
            self._cmd_clear()
        else:
            print(f"{ANSI_RED}Unknown command: {cmd}. Type /help for available commands.{ANSI_RESET}")
        return False

    def _cmd_help(self) -> None:
        commands = [
            ("/help", "Show table of all available slash commands"),
            ("/status", "Display active/recent tasks, proof bundle, and worker state"),
            ("/diff", "Inspect uncommitted changes in active workspace via unified diff"),
            ("/rollback", "Roll back workspace changes or cancel active task"),
            ("/fork", "Fork active task into speculative attempt branches"),
            ("/skills", "List registered skills, risk scores, and gate decisions"),
            ("/model [name]", "Inspect or switch active model routing profile"),
            ("/budget", "Display session token usage, compression ratio, and cost"),
            ("/clear", "Clear the terminal screen"),
            ("/quit, /exit", "Exit interactive console session"),
        ]
        print(f"\n{ANSI_BOLD}{ANSI_CYAN}Available HEX Slash Commands:{ANSI_RESET}")
        for c, desc in commands:
            print(f"  {ANSI_BOLD}{c:<16}{ANSI_RESET} - {desc}")
        print()

    def _cmd_status(self) -> None:
        print(f"\n{ANSI_BOLD}{ANSI_CYAN}HEX Console Session Status:{ANSI_RESET}")
        print(f"  Project ID:     {self.project_id}")
        print(f"  Workspace Root: {self.workspace_root}")
        print(f"  Streaming:      {'Enabled' if self.stream else 'Disabled'}")
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
            status = w.get("status", "unknown")
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
                verified = proof.get("verified", False)
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
                winner = res.get("winner_branch_id") or res.get("status") or "completed"
                print(f"{ANSI_GREEN}Speculative branches evaluated successfully. Promoted: {winner}{ANSI_RESET}")
            else:
                print(f"{ANSI_RED}Orchestrator does not support fork_speculative_branches.{ANSI_RESET}")
        except Exception as exc:
            print(f"{ANSI_RED}Fork speculative execution failed: {exc}{ANSI_RESET}")

    def _cmd_skills(self) -> None:
        skills = []
        if hasattr(self.orchestrator, "ledger") and hasattr(self.orchestrator.ledger, "list_skills"):
            try:
                skills = self.orchestrator.ledger.list_skills()
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

    def _cmd_model(self, arg: str = "") -> None:
        arg = arg.strip()
        if arg:
            self.model_profile = arg
            if hasattr(self.orchestrator, "router") and hasattr(self.orchestrator.router, "profile"):
                self.orchestrator.router.profile = arg
            print(f"{ANSI_GREEN}Model profile updated to: {arg}{ANSI_RESET}")
        else:
            current = self.model_profile or "hybrid (Two-Tier MLX Local + Cloud)"
            print(f"\n{ANSI_BOLD}{ANSI_CYAN}Active Model Routing Configuration:{ANSI_RESET}")
            print(f"  Profile:          {current}")
            if hasattr(self.orchestrator, "router"):
                r = self.orchestrator.router
                print(f"  Tier-1 Local:     {getattr(r, 'local_model_id', 'mlx-community/Qwen2.5-Coder-7B-Instruct-4bit')}")
                print(f"  Tier-2 Cloud:     {getattr(r, 'cloud_model_id', 'claude-3-5-sonnet')}")
                if hasattr(r, "budget_tracker"):
                    bt = r.budget_tracker
                    max_usd = getattr(bt, "max_usd_per_task", 1.0)
                    print(f"  Task Ceiling:     ${max_usd:.2f} USD")
            print()

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

    def _cmd_clear(self) -> None:
        if os.name == "nt":
            os.system("cls")
        else:
            sys.stdout.write("\033[H\033[2J")
            sys.stdout.flush()

    def handle_user_prompt(self, prompt: str) -> None:
        """Submit natural language coding prompt to orchestrator."""
        from .models import TaskSubmitRequest

        print(f"{ANSI_CYAN}⚡ Submitting task to HEX orchestrator...{ANSI_RESET}")
        req = TaskSubmitRequest(
            goal=prompt,
            project_id=self.project_id,
            workspace_root=str(self.workspace_root) if self.workspace_root else None,
        )

        task = self.orchestrator.submit_and_run(req, auto_execute=True)
        self.active_task_id = task.get("task_id")
        tid = self.active_task_id
        status = task.get("status")

        if self.stream:
            renderer = StreamingTokenRenderer(output_stream=sys.stdout)
            renderer.feed(f"{ANSI_DIM}Executing task {tid}...{ANSI_RESET}\n")
            renderer.flush()

        if status in ("COMPLETED", "SUCCEEDED"):
            print(f"{ANSI_GREEN}✔ Task {tid} completed successfully!{ANSI_RESET}")
            proof = self.orchestrator.ledger.get_proof_bundle(tid)
            if proof:
                verified = proof.get("verified", False)
                v_badge = f"{ANSI_GREEN}[VERIFIED]{ANSI_RESET}" if verified else f"{ANSI_YELLOW}[UNVERIFIED]{ANSI_RESET}"
                sha = str(proof.get("workspace_tree_sha", ""))[:12]
                print(f"  Proof-Carrying Artifact: {v_badge} Merkle SHA: {sha}...")
        elif status == "REJECTED":
            print(f"{ANSI_RED}✖ Task {tid} rejected: {task.get('terminal_reason')}{ANSI_RESET}")
        elif status == "NEEDS_ATTENTION":
            print(f"{ANSI_YELLOW}⚠ Task {tid} requires operator attention: {task.get('terminal_reason')}{ANSI_RESET}")
        else:
            print(f"Task {tid} status: {status}")
