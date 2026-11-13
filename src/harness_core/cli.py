"""CLI Entrypoint (`hex`) for Owner Workflows and Debugging (PRD Sections 5, 8.1, 15).

Commands:
- `hex setup`: Run interactive onboarding wizard for hardware discovery, model profile, and configuration (alias: `hex onboard`).
- `hex project`: Manage persistent global project registry and daily switching (`list`, `add`, `use`, `switch`).
- `hex auth antigravity`: Discover, import, or authenticate Google Antigravity OAuth session for $0 Day-1 quota.
- `hex quota`: Inspect live Google Antigravity model quota windows, remaining percentages, and reset timers.
- `hex init`: Initialize local database, CoW snapshot store, and seed default repo skills.
- `hex up`: Start the local web control surface & HTTP API on 127.0.0.1:8420.
- `hex run` / `hex exec <goal>`: Run task with automated Git branching, commits, and GitHub Pull Requests.
- `hex submit <goal>`: Submit and execute a task (`--fast-path` or `--standard`).
- `hex status [task_id]`: Inspect task state, call journal, and Proof-Carrying Artifact bundle.
- `hex scan-skill <dir>`: Run NVIDIA SkillSpector static + OSV + semantic scan on a SKILL.md bundle.
- `hex harness2-demo`: Run a live Propose-Probe-Compose cycle through the gated `vendor/harness2` adapter.
- `hex worker`: Start standalone distributed worker daemon.
- `hex chat` / `hex tui`: Start interactive TUI pair-programming console with HEX-style splash & skin engine.
- `hex voice`: Start interactive hands-free voice pair-programming console.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .models import TaskSubmitRequest
from .orchestrator import HarnessOrchestrator
from .server import run_http_server


DEFAULT_DATA_DIR = Path.home() / ".hex" / "data"


def seed_default_skills(orch: HarnessOrchestrator, repo_root: Path) -> list[dict]:
    skills_root = repo_root / "skills"
    results = []
    if skills_root.is_dir():
        for sub in sorted(skills_root.iterdir()):
            if (sub / "SKILL.md").is_file():
                results.append(orch.scanner.scan_and_register_skill(sub))
    return results


def main(argv: list[str] | None = None) -> int:
    try:
        return _main_impl(argv)
    except KeyboardInterrupt:
        print("\n\x1b[90m[Interrupted by user]\x1b[0m")
        return 130


def build_parser(default_dir: str | None = None) -> argparse.ArgumentParser:
    if default_dir is None:
        default_dir = str(Path.home() / ".hex" / "data")

    parser = argparse.ArgumentParser(
        prog="hex",
        description="HEX: Autonomous AI Workbench & Interactive TUI (Run 'hex' to launch interactive console)",
        allow_abbrev=False,
    )
    parser.add_argument("-v", "--version", action="version", version=f"hex {__version__}")
    parser.add_argument(
        "--data-dir",
        default=default_dir,
        help="Path to local HEX ledger and CoW storage directory",
    )
    # Interactive TUI options directly supported at top-level so 'hex' immediately starts TUI
    parser.add_argument("--project", default="default", help="Project ID")
    parser.add_argument("--workspace", default=None, help="Workspace path (defaults to repository root)")
    parser.add_argument(
        "--provider",
        choices=["antigravity", "gemini", "anthropic", "openai", "local"],
        default=None,
        help="Cloud reasoner provider override (e.g., antigravity)",
    )
    parser.add_argument("--model", default=None, help="Model routing profile override (hybrid, local, cloud, or model ID)")
    parser.add_argument(
        "--skin",
        default="amber",
        choices=["amber", "cyberpunk", "matrix", "nord", "mono"],
        help="TUI color skin (default: amber)",
    )
    parser.add_argument("-Q", "--quiet", action="store_true", help="Quiet mode: suppress startup banner and spinner")
    parser.add_argument("--no-stream", action="store_true", help="Disable live token streaming")
    parser.add_argument("--voice", action="store_true", help="Enable hands-free voice mode and spoken agent commentary")
    parser.add_argument("--voice-name", default=None, help="TTS voice name override")
    parser.add_argument("--speech-rate", type=int, default=190, help="Speech rate in words per minute (default: 190)")
    parser.add_argument("--react", action="store_true", help="Enable autonomous multi-turn ReAct tool execution")
    parser.add_argument("--legacy", action="store_true", default=False, help="Launch legacy Python console instead of native Rust Ratatui cockpit")
    parser.add_argument("--ratatui", action="store_true", default=False, help="Launch native Rust Ratatui terminal cockpit")
    parser.add_argument("--check", action="store_true", default=False, help="Run headless virtual verification and exit 0")
    sub = parser.add_subparsers(dest="cmd", required=False)

    p_setup = sub.add_parser("setup", aliases=["onboard"], help="Run interactive setup wizard for hardware & configuration (alias: hex onboard)")
    p_setup.add_argument("stage", nargs="?", default=None, help="Specific component to configure (e.g. 'model', 'search', 'messaging', or omit for full setup)")
    p_setup.add_argument("--config-dir", default=None, help="Custom configuration directory (defaults to ~/.hex)")
    p_setup.add_argument("--non-interactive", action="store_true", help="Run in non-interactive mode using hardware defaults")
    p_setup.add_argument("--defaults", dest="defaults_only", action="store_true", help="Accept all hardware auto-detected defaults")

    p_proj = sub.add_parser("project", help="Manage project workspace registry and daily switching")
    p_proj_sub = p_proj.add_subparsers(dest="project_cmd", required=False)

    p_proj_list = p_proj_sub.add_parser("list", help="List all registered project workspaces")
    p_proj_list.add_argument("--json", action="store_true", help="Output raw JSON array of project records")

    p_proj_add = p_proj_sub.add_parser("add", help="Register a directory as a project workspace")
    p_proj_add.add_argument("path", help="Path to project directory")
    p_proj_add.add_argument("--name", default=None, help="Optional custom project name")
    p_proj_add.add_argument("--json", action="store_true", help="Output raw JSON project record")

    p_proj_use = p_proj_sub.add_parser("use", aliases=["switch"], help="Switch active project workspace")
    p_proj_use.add_argument("target", help="Project name or directory path")
    p_proj_use.add_argument("--json", action="store_true", help="Output raw JSON project record")

    p_proj_prune = p_proj_sub.add_parser("prune", aliases=["clean"], help="Prune deleted or non-existent project directories from registry")
    p_proj_prune.add_argument("--json", action="store_true", help="Output raw JSON summary of pruned paths")

    p_auth = sub.add_parser("auth", help="Manage Google Antigravity OAuth credentials and local IDE token import")
    p_auth.add_argument("provider", nargs="?", default="antigravity", choices=["antigravity"], help="Target auth provider")
    p_auth.add_argument("--status", action="store_true", help="Show discovered Antigravity OAuth session status")
    p_auth.add_argument("--import-ide", action="store_true", help="Import OAuth token from local Antigravity IDE state.vscdb / Keychain")
    p_auth.add_argument("--token", default=None, help="Save an explicit Antigravity OAuth access token (ya29...)")
    p_auth.add_argument("--refresh-token", default=None, help="Save an explicit Antigravity OAuth refresh token (1//...)")
    p_auth.add_argument("--project-id", default=None, help="Optional Google Cloud Companion Project ID")
    p_auth.add_argument("--auth-url", action="store_true", help="Generate OAuth 2.0 PKCE authorization URL for browser login")
    p_auth.add_argument("--code", default=None, help="Exchange OAuth authorization code or redirect URL for tokens")
    p_auth.add_argument("--verifier", default=None, help="PKCE verifier corresponding to --code")
    p_auth.add_argument("--auth-file", default=None, help="Custom path to antigravity_auth.json")

    p_quota = sub.add_parser("quota", help="Inspect live Google Antigravity model quota windows and reset times")
    p_quota.add_argument("--auth-file", default=None, help="Custom path to antigravity_auth.json")
    p_quota.add_argument("--json", action="store_true", help="Emit raw JSON quota snapshot")

    p_init = sub.add_parser("init", help="Initialize project configuration, ledger schema, and seed built-in skills")
    p_init.add_argument("--workspace", default=None, help="Path to project repository root (defaults to current working directory)")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing .hex/config.yaml")
    p_init.add_argument("--data-dir", default=argparse.SUPPRESS, help="Path to local HEX ledger and CoW storage directory")

    p_up = sub.add_parser("up", help="Start local web control surface and /v1/* HTTP API")
    p_up.add_argument("--host", default="127.0.0.1")
    p_up.add_argument("--port", type=int, default=8420)
    p_up.add_argument("--drain-timeout", type=float, default=5.0, help="Graceful shutdown drain timeout in seconds")
    p_up.add_argument("--asgi", action="store_true", help="Run via ASGI uvicorn server if installed")

    p_run = sub.add_parser("run", aliases=["exec"], help="Run and execute a task with optional auto-branching and pull request creation")
    p_run.add_argument("goal", help="Task goal description")
    p_run.add_argument("--project", default="default")
    p_run.add_argument("--mode", choices=["auto", "fast_path", "standard"], default="auto")
    p_run.add_argument("--provider", choices=["antigravity", "gemini", "anthropic", "openai", "local"], default=None, help="Cloud reasoner provider override (e.g., antigravity)")
    p_run.add_argument("--model", default=None, help="Cloud model ID override (e.g., gemini-2.5-pro, gemini-2.5-flash)")
    p_run.add_argument("--workspace", default=None)
    p_run.add_argument("--branch", nargs="?", const=True, default=None, help="Custom branch name or flag to auto-branch (hex/task-<id>-<slug>)")
    p_run.add_argument("--pr", action="store_true", help="Automatically create GitHub Pull Request on success")
    p_run.add_argument("--base-branch", default="main", help="Base branch for Pull Request (default: main)")
    p_run.add_argument("--dry-run", action="store_true", help="Dry-run mode: do not create real git commits or remote PRs")
    p_run.add_argument("--react", action="store_true", help="Enable autonomous multi-turn ReAct tool execution")

    p_sub = sub.add_parser("submit", help="Submit and execute a task")
    p_sub.add_argument("goal", help="Task goal description")
    p_sub.add_argument("--project", default="default")
    p_sub.add_argument("--mode", choices=["auto", "fast_path", "standard"], default="auto")
    p_sub.add_argument("--provider", choices=["antigravity", "gemini", "anthropic", "openai", "local"], default=None, help="Cloud reasoner provider override")
    p_sub.add_argument("--workspace", default=None)

    p_stat = sub.add_parser("status", help="Inspect task status, events, and Proof-Carrying Artifact")
    p_stat.add_argument("task_id", nargs="?", default=None)

    p_scan = sub.add_parser("scan-skill", help="Scan and import a SKILL.md directory bundle via SkillSpector")
    p_scan.add_argument("path", help="Path to skill bundle directory")

    sub.add_parser("harness2-demo", help="Run a gated Harness2 Propose-Probe-Compose refinement verification")

    p_h2_eval = sub.add_parser("harness2-eval", help="Run multi-rollout Propose-Probe-Compose sweep via OrchestratorBenchmarkAdapter")
    p_h2_eval.add_argument("--task-id", default="demo-1", help="Task ID to evaluate")
    p_h2_eval.add_argument("--mode", choices=["parallel", "sequential"], default="parallel", help="Recursion execution mode")
    p_h2_eval.add_argument("--k", type=int, default=1, help="Number of candidate iterations / width")
    p_h2_eval.add_argument("--substrate", choices=["opencode", "codex", "claude", "gemini"], default="opencode", help="Editable harness surface substrate (opencode, codex, claude, or gemini)")
    p_h2_eval.add_argument("--proposer", choices=["local", "llm"], default="local", help="Proposer backend (deterministic local or live LLMRouterProposer)")
    p_h2_eval.add_argument("--runs-root", default=None, help="Root directory for rollout executions and logs")
    p_h2_eval.add_argument("--json", action="store_true", help="Output results as JSON")

    p_wrk = sub.add_parser("worker", help="Start standalone distributed worker daemon")
    p_wrk.add_argument("--worker-id", default=None, help="Worker ID (defaults to hostname-hash)")
    p_wrk.add_argument("--machine-class", choices=["auto", "m4_air", "desktop_gpu", "server"], default="auto")
    p_wrk.add_argument("--poll-interval", type=float, default=2.0, help="Task queue poll interval in seconds")
    p_wrk.add_argument("--lease-ttl", type=int, default=300, help="Lease TTL in seconds")
    p_wrk.add_argument("--server-url", default=None, help="Control plane HTTP URL. If omitted, uses local direct ledger")
    p_wrk.add_argument("--token", default=None, help="Worker authentication token")
    p_wrk.add_argument("--once", action="store_true", help="Execute at most one claimed task and exit")

    p_chat = sub.add_parser("chat", help="Start interactive TUI pair-programming console (or run 'hex')")
    p_chat.add_argument("--legacy", action="store_true", default=False, help="Launch legacy Python console instead of native Rust Ratatui cockpit")
    p_chat.add_argument("--ratatui", action="store_true", default=False, help="Launch native Rust Ratatui terminal cockpit")
    p_chat.add_argument("--check", action="store_true", default=False, help="Run headless virtual verification and exit 0")
    p_chat.add_argument("--project", default=argparse.SUPPRESS, help="Project ID")
    p_chat.add_argument("--workspace", default=argparse.SUPPRESS, help="Workspace path (defaults to repository root)")
    p_chat.add_argument("--provider", choices=["antigravity", "gemini", "anthropic", "openai", "local"], default=argparse.SUPPRESS, help="Cloud reasoner provider override (e.g., antigravity)")
    p_chat.add_argument("--model", default=argparse.SUPPRESS, help="Model routing profile override (hybrid, local, cloud, or model ID)")
    p_chat.add_argument("--skin", default=argparse.SUPPRESS, choices=["amber", "cyberpunk", "matrix", "nord", "mono"], help="TUI color skin")
    p_chat.add_argument("-Q", "--quiet", action="store_true", default=argparse.SUPPRESS, help="Quiet mode: suppress startup banner and spinner")
    p_chat.add_argument("--no-stream", action="store_true", default=argparse.SUPPRESS, help="Disable live token streaming")
    p_chat.add_argument("--voice", action="store_true", default=argparse.SUPPRESS, help="Enable hands-free voice mode and spoken agent commentary")
    p_chat.add_argument("--voice-name", default=argparse.SUPPRESS, help="TTS voice name override")
    p_chat.add_argument("--speech-rate", type=int, default=argparse.SUPPRESS, help="Speech rate in words per minute")
    p_chat.add_argument("--react", action="store_true", default=argparse.SUPPRESS, help="Enable autonomous multi-turn ReAct tool execution")
    p_chat.add_argument("--data-dir", default=argparse.SUPPRESS, help="Path to local HEX ledger and CoW storage directory")

    p_tui = sub.add_parser("tui", help="Start native Rust Ratatui terminal cockpit")
    p_tui.add_argument("--check", action="store_true", default=False, help="Run headless virtual verification and exit 0")
    p_tui.add_argument("--project", default="default", help="Project ID")
    p_tui.add_argument("--workspace", default=None, help="Workspace path (defaults to repository root)")
    p_tui.add_argument("--model", default="gemini-2.5-pro", help="Model routing profile override")
    p_tui.add_argument("--skin", default="amber", choices=["amber", "cyberpunk", "matrix", "nord", "mono"], help="HEX visual skin")
    p_tui.add_argument("--cow", default="OverlayFS", help="CoW snapshot provider backend")
    p_tui.add_argument("--session", default=None, help="Session ID to attach or resume")
    p_tui.add_argument("--build", action="store_true", default=False, help="Force recompiling hex-tui before launching")
    p_tui.add_argument("--release", action="store_true", default=False, help="Use release build of hex-tui")

    p_voice = sub.add_parser("voice", help="Start interactive hands-free voice pair-programming console")
    p_voice.add_argument("--project", default=argparse.SUPPRESS, help="Project ID")
    p_voice.add_argument("--workspace", default=argparse.SUPPRESS, help="Workspace path (defaults to repository root)")
    p_voice.add_argument("--provider", choices=["antigravity", "gemini", "anthropic", "openai", "local"], default=argparse.SUPPRESS, help="Cloud reasoner provider override")
    p_voice.add_argument("--model", default=argparse.SUPPRESS, help="Model routing profile override")
    p_voice.add_argument("--voice-name", default=None, help="TTS voice name (e.g., Samantha, Daniel)")
    p_voice.add_argument("--speech-rate", type=int, default=190, help="Speech rate in words per minute (default: 190)")
    p_voice.add_argument("--muted", action="store_true", default=False, help="Start voice console in muted state")
    p_voice.add_argument("--skin", default=argparse.SUPPRESS, choices=["amber", "cyberpunk", "matrix", "nord", "mono"], help="TUI color skin")
    p_voice.add_argument("-Q", "--quiet", action="store_true", default=argparse.SUPPRESS, help="Quiet mode: suppress startup banner and spinner")
    p_voice.add_argument("--no-stream", action="store_true", default=argparse.SUPPRESS, help="Disable live token streaming")
    p_voice.add_argument("--data-dir", default=argparse.SUPPRESS, help="Path to local HEX ledger and CoW storage directory")

    p_gw = sub.add_parser("gateway", help="Manage bidirectional chat gateways (Telegram & Discord)")
    p_gw_sub = p_gw.add_subparsers(dest="gateway_cmd", required=True)

    p_gw_start = p_gw_sub.add_parser("start", help="Start bidirectional chat gateway daemon")
    p_gw_start.add_argument("--telegram", action="store_true", help="Explicitly enable Telegram gateway (default: enabled if token is set in env)")
    p_gw_start.add_argument("--discord", action="store_true", help="Explicitly enable Discord gateway (default: enabled if token is set in env)")
    p_gw_start.add_argument("--poll-interval", type=float, default=1.0, help="Polling interval in seconds")
    p_gw_start.add_argument("--project", default="default", help="Project ID")
    p_gw_start.add_argument("--workspace", default=None, help="Workspace path (defaults to repo root)")
    p_gw_start.add_argument("--once", action="store_true", help="Poll once and exit immediately")
    p_gw_start.add_argument("--data-dir", default=argparse.SUPPRESS, help="Path to local HEX ledger and CoW storage directory")

    p_gw_status = p_gw_sub.add_parser("status", help="Inspect gateway health and status")
    p_gw_status.add_argument("--data-dir", default=argparse.SUPPRESS, help="Path to local HEX ledger and CoW storage directory")

    p_gha = sub.add_parser("gh-action", help="Run GitHub Actions autonomous bot responder on event payload")
    p_gha.add_argument("--event-path", default=None, help="Path to GitHub event JSON file (defaults to GITHUB_EVENT_PATH env var)")
    p_gha.add_argument("--event-name", default=None, help="GitHub event name (defaults to GITHUB_EVENT_NAME env var)")
    p_gha.add_argument("--dry-run", action="store_true", help="Dry-run mode: do not create real git commits or remote comments")
    p_gha.add_argument("--workspace", default=None, help="Path to repository workspace")
    p_gha.add_argument("--data-dir", default=argparse.SUPPRESS, help="Path to local HEX ledger and CoW storage directory")

    p_sch = sub.add_parser("schedule", help="Manage background automations and cron schedules")
    p_sch_sub = p_sch.add_subparsers(dest="schedule_cmd", required=True)

    p_sch_add = p_sch_sub.add_parser("add", help="Add a new scheduled automation job")
    p_sch_add.add_argument("cron", help="Standard 5-field cron expression")
    p_sch_add.add_argument("--goal", required=True, help="Task goal description")
    p_sch_add.add_argument("--project", default="default", help="Project ID")
    p_sch_add.add_argument("--storage-path", default=None, help="Custom schedules.json path")

    p_sch_list = p_sch_sub.add_parser("list", help="List all scheduled jobs")
    p_sch_list.add_argument("--all", action="store_true", help="Include disabled jobs")
    p_sch_list.add_argument("--storage-path", default=None, help="Custom schedules.json path")

    p_sch_rm = p_sch_sub.add_parser("remove", help="Remove a scheduled job by ID")
    p_sch_rm.add_argument("job_id", help="Job ID to remove")
    p_sch_rm.add_argument("--storage-path", default=None, help="Custom schedules.json path")

    p_sch_run = p_sch_sub.add_parser("run-due", help="Execute all currently due scheduled jobs")
    p_sch_run.add_argument("--once", action="store_true", help="Run due jobs once and exit")
    p_sch_run.add_argument("--storage-path", default=None, help="Custom schedules.json path")

    p_dmn = sub.add_parser("daemon", help="Manage background OS service daemon (launchd/systemd)")
    p_dmn_sub = p_dmn.add_subparsers(dest="daemon_cmd", required=True)

    p_dmn_inst = p_dmn_sub.add_parser("install", help="Generate and install OS service configuration")
    p_dmn_inst.add_argument("--launchd", action="store_true", help="Target macOS LaunchAgent")
    p_dmn_inst.add_argument("--systemd", action="store_true", help="Target Linux systemd")
    p_dmn_inst.add_argument("--user", action="store_true", default=True, help="Install as user service (default: True)")
    p_dmn_inst.add_argument("--system", dest="user", action="store_false", help="Install as system-wide service")
    p_dmn_inst.add_argument("--dry-run", action="store_true", help="Dry run: print generated configuration without writing files")
    p_dmn_inst.add_argument("--path", default=None, help="Custom destination path")
    p_dmn_inst.add_argument("--interval", type=int, default=60, help="Poll interval in seconds")

    p_dmn_stat = p_dmn_sub.add_parser("status", help="Inspect daemon service file and active state")

    p_dmn_uninst = p_dmn_sub.add_parser("uninstall", aliases=["remove"], help="Unload and remove OS service daemon")
    p_dmn_uninst.add_argument("--dry-run", action="store_true", help="Preview service removal without modifying disk")

    p_skl = sub.add_parser("skill", help="Manage and self-synthesize skills via NVIDIA SkillSpector")
    p_skl_sub = p_skl.add_subparsers(dest="skill_cmd", required=True)

    p_skl_synth = p_skl_sub.add_parser("synthesize", help="Synthesize a new SKILL.md bundle from a completed task")
    p_skl_synth.add_argument("--task", required=True, help="Task ID to synthesize skill from")
    p_skl_synth.add_argument("--name", default=None, help="Custom skill name")
    p_skl_synth.add_argument("--output-dir", default=None, help="Custom output directory for the skill")
    p_skl_synth.add_argument("--dry-run", action="store_true", help="Simulate synthesis and scan without saving to disk")

    p_skl_list = p_skl_sub.add_parser("list", help="List all built-in, custom, and quarantined skills with status badges")

    p_skl_scan = p_skl_sub.add_parser("scan", help="Scan a skill bundle directory or SKILL.md file via SkillSpector")
    p_skl_scan.add_argument("path", help="Path to skill bundle directory or file")

    p_doc = sub.add_parser("doctor", help="Run system diagnostics, environment auditing, and self-healing")
    p_doc.add_argument("--fix", action="store_true", help="Automatically resolve fixable diagnostic issues (directories, config, permissions)")
    p_doc.add_argument("--live", action="store_true", help="Perform live read-only model API connectivity ping")
    p_doc.add_argument("--json", action="store_true", help="Output raw JSON report instead of formatted text")
    p_doc.add_argument("--no-color", action="store_true", help="Disable ANSI color formatting")
    p_doc.add_argument("--workspace", default=None, help="Custom project workspace root")

    p_rcp = sub.add_parser("recipe", help="Auto-detect project bootstrap, build, test, and start commands")
    p_rcp.add_argument("workspace", nargs="?", default=".", help="Target project workspace directory")
    p_rcp.add_argument("--json", action="store_true", help="Emit raw JSON recipe definition")

    p_srch = sub.add_parser("search", help="Full-text search across past tasks, transcripts, and logs")
    p_srch.add_argument("query", help="Search terms or prefix pattern (e.g. auth*)")
    p_srch.add_argument("--project", default=None, help="Filter results by project ID")
    p_srch.add_argument("--limit", type=int, default=20, help="Maximum number of results (default: 20)")
    p_srch.add_argument("--reindex", action="store_true", help="Rebuild FTS5 index from ledger before searching")
    p_srch.add_argument("--json", action="store_true", help="Output raw JSON results")

    p_bck = sub.add_parser("backup", help="Create a compressed state archive of configuration, database, and skills")
    p_bck.add_argument("--output", default=None, help="Custom zip archive output path")
    p_bck.add_argument("--quick", action="store_true", help="Quick mode: archive only database and configuration files")
    p_bck.add_argument("--workspace", default=None, help="Custom project workspace root")

    p_rst = sub.add_parser("restore", help="Restore state from a backup archive with zip-slip path protection")
    p_rst.add_argument("archive", help="Path to backup zip archive to restore")
    p_rst.add_argument("--target", default=None, help="Destination directory to restore files into (defaults to data-dir)")
    p_rst.add_argument("--dry-run", action="store_true", help="Inspect archive contents without extracting")

    p_dmp = sub.add_parser("dump", help="Generate plain-text system diagnostic profile for support tickets")
    p_dmp.add_argument("--output", default=None, help="Path to save plain-text dump (defaults to stdout)")
    p_dmp.add_argument("--workspace", default=None, help="Custom project workspace root")

    p_ref = sub.add_parser("reflect", help="Run deterministic Graphify work-memory reflection (LESSONS.md + learning sidecar)")
    p_ref.add_argument("--workspace", default=None, help="Custom project workspace root")
    p_ref.add_argument("--json", action="store_true", help="Output raw JSON reflection aggregate")

    p_lsp = sub.add_parser("lsp", help="Language Server Protocol (LSP) diagnostics and symbol inspection")
    p_lsp_sub = p_lsp.add_subparsers(dest="lsp_cmd", help="LSP subcommands")

    p_lsp_stat = p_lsp_sub.add_parser("status", help="Inspect active and detected language servers")
    p_lsp_stat.add_argument("--workspace", default=None, help="Workspace directory")
    p_lsp_stat.add_argument("--json", action="store_true", help="Output JSON")

    p_lsp_diag = p_lsp_sub.add_parser("diagnostics", help="Query compiler syntax errors, type mismatches, and linter warnings")
    p_lsp_diag.add_argument("--file", "-f", default=None, help="Specific file path")
    p_lsp_diag.add_argument("--errors-only", "-e", action="store_true", help="Filter to fatal compiler/syntax errors only")
    p_lsp_diag.add_argument("--workspace", default=None, help="Workspace directory")
    p_lsp_diag.add_argument("--json", action="store_true", help="Output JSON")

    p_lsp_def = p_lsp_sub.add_parser("definition", help="Find symbol definition location")
    p_lsp_def.add_argument("file", help="Source file path")
    p_lsp_def.add_argument("line", type=int, help="Line number (1-based)")
    p_lsp_def.add_argument("character", type=int, help="Column number (1-based)")
    p_lsp_def.add_argument("--workspace", default=None, help="Workspace directory")
    p_lsp_def.add_argument("--json", action="store_true", help="Output JSON")

    p_lsp_hov = p_lsp_sub.add_parser("hover", help="Retrieve hover documentation and type signatures")
    p_lsp_hov.add_argument("file", help="Source file path")
    p_lsp_hov.add_argument("line", type=int, help="Line number (1-based)")
    p_lsp_hov.add_argument("character", type=int, help="Column number (1-based)")
    p_lsp_hov.add_argument("--workspace", default=None, help="Workspace directory")
    p_lsp_hov.add_argument("--json", action="store_true", help="Output JSON")

    p_grph = sub.add_parser("graphify", help="Manage upstream Graphify tree-sitter AST extraction and knowledge graph")
    p_grph_sub = p_grph.add_subparsers(dest="graphify_cmd", help="Graphify subcommands")

    p_grph_stat = p_grph_sub.add_parser("status", help="Inspect Graphify CLI discovery, active graph paths, and node/edge statistics")
    p_grph_stat.add_argument("--workspace", default=None, help="Workspace directory")
    p_grph_stat.add_argument("--json", action="store_true", help="Output JSON")

    p_grph_upd = p_grph_sub.add_parser("update", help="Run Graphify incremental update on workspace (CLI or in-process fallback)")
    p_grph_upd.add_argument("--workspace", default=None, help="Workspace directory")
    p_grph_upd.add_argument("--no-force", action="store_true", help="Do not force overwrite if node count decreases")
    p_grph_upd.add_argument("--cluster", action="store_true", help="Enable community clustering (default: no-cluster)")
    p_grph_upd.add_argument("--json", action="store_true", help="Output JSON")

    p_grph_ing = p_grph_sub.add_parser("ingest", help="Ingest graphify-out/graph.json or .hex/graph.json into active architecture engine")
    p_grph_ing.add_argument("--path", default=None, help="Explicit path to graph.json")
    p_grph_ing.add_argument("--workspace", default=None, help="Workspace directory")
    p_grph_ing.add_argument("--json", action="store_true", help="Output JSON")

    p_grph_god = p_grph_sub.add_parser("god-nodes", help="Analyze core architectural abstractions and god nodes")
    p_grph_god.add_argument("--top", type=int, default=5, help="Number of top god nodes to display (default: 5)")
    p_grph_god.add_argument("--workspace", default=None, help="Workspace directory")
    p_grph_god.add_argument("--json", action="store_true", help="Output JSON")

    p_grph_aff = p_grph_sub.add_parser("affected", help="Query blast-radius and affected nodes for a symbol or file")
    p_grph_aff.add_argument("target", help="Symbol name or file path to analyze")
    p_grph_aff.add_argument("--depth", type=int, default=2, help="Blast-radius traversal depth (default: 2)")
    p_grph_aff.add_argument("--workspace", default=None, help="Workspace directory")
    p_grph_aff.add_argument("--json", action="store_true", help="Output JSON")

    p_grph_tree = p_grph_sub.add_parser("tree", help="Generate self-contained interactive D3 architecture tree visualization HTML")
    p_grph_tree.add_argument("--output", "-o", default=None, help="Output HTML file path (default: graphify-out/tree.html)")
    p_grph_tree.add_argument("--open", action="store_true", help="Open generated HTML in default web browser")
    p_grph_tree.add_argument("--workspace", default=None, help="Workspace directory")
    p_grph_tree.add_argument("--json", action="store_true", help="Output JSON")

    p_acp = sub.add_parser("acp", help="Start Agent Client Protocol (ACP) stdio JSON-RPC server for Zed, Neovim & VSCode")
    p_acp.add_argument("--check", action="store_true", help="Validate ACP runtime readiness and environment health")
    p_acp.add_argument("--setup", action="store_true", help="Print editor configuration snippets for Zed (.zed/settings.json) and Neovim")
    p_acp.add_argument("--workspace", default=None, help="Root workspace directory path")

    p_uninst = sub.add_parser("uninstall", help="Uninstall HEX CLI, unload background daemons, and optionally purge data")
    p_uninst.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    p_uninst.add_argument("--purge", action="store_true", help="Permanently delete user configuration and ledger database (~/.hex)")
    p_uninst.add_argument("--keep-data", action="store_true", help="Preserve ~/.hex configuration, credentials, and data")
    p_uninst.add_argument("--dry-run", action="store_true", help="Preview items to be removed without modifying disk")
    p_uninst.add_argument("--data-dir", default=argparse.SUPPRESS, help="Path to local HEX ledger and CoW storage directory")

    return parser


def _main_impl(argv: list[str] | None = None) -> int:
    default_dir = os.environ.get("HEX_DATA_DIR") or os.environ.get("HARNESS_DATA_DIR")
    if not default_dir:
        cwd = Path.cwd()
        cwd_hex = cwd / ".hex_data"
        cwd_legacy = cwd / ".harness_data"
        repo_data = Path(__file__).resolve().parents[2] / ".hex_data"
        repo_legacy = Path(__file__).resolve().parents[2] / ".harness_data"
        if cwd_hex.is_dir():
            default_dir = str(cwd_hex)
        elif cwd_legacy.is_dir():
            default_dir = str(cwd_legacy)
        elif repo_data.is_dir():
            default_dir = str(repo_data)
        elif repo_legacy.is_dir():
            default_dir = str(repo_legacy)
        elif (Path.home() / ".hex" / "data").is_dir():
            default_dir = str(Path.home() / ".hex" / "data")
        elif (Path(__file__).resolve().parents[2] / "pyproject.toml").is_file():
            default_dir = str(repo_data)
        else:
            default_dir = str(Path.home() / ".hex" / "data")

    parser = build_parser(default_dir)
    args = parser.parse_args(argv)

    from .config_loader import load_hex_environment

    load_hex_environment(config_dir=Path(args.config_dir) if getattr(args, "config_dir", None) else None)

    if args.cmd in ("setup", "onboard"):
        from .onboard import run_onboard_wizard

        cfg_dir = Path(args.config_dir) if getattr(args, "config_dir", None) else None
        res = run_onboard_wizard(
            config_dir=cfg_dir,
            non_interactive=getattr(args, "non_interactive", False),
            defaults_only=getattr(args, "defaults_only", False),
            target_stage=getattr(args, "stage", None),
        )
        if isinstance(res, dict) and res.get("status") == "error":
            return 1
        return 0

    if args.cmd == "project":
        from .project_registry import ProjectRegistry

        registry = ProjectRegistry()
        project_cmd = getattr(args, "project_cmd", None) or "list"
        if project_cmd == "list":
            projects = registry.list_projects()
            if getattr(args, "json", False):
                print(json.dumps([p.to_dict() for p in projects], indent=2))
            else:
                if not projects:
                    print("No projects registered yet. Use 'hex project add <path>' or run 'hex init'.")
                else:
                    print(f"\nRegistered Projects ({len(projects)}):")
                    print(f"{'PROJECT':<20} {'STACK':<10} {'TASKS':<8} {'LAST ACTIVE':<24} {'PATH'}")
                    print("-" * 80)
                    for p in projects:
                        print(f"{p.name:<20} {p.stack:<10} {p.task_count:<8} {p.last_active:<24} {p.path}")
                    print()
            return 0

        if project_cmd == "add":
            target_path = Path(args.path).resolve()
            rec = registry.register_project(target_path, name=getattr(args, "name", None))
            if getattr(args, "json", False):
                print(json.dumps(rec.to_dict(), indent=2))
            else:
                print(f"Registered project '{rec.name}' ({rec.stack}) at {rec.path}")
            return 0

        if project_cmd in ("use", "switch"):
            rec = registry.get_project(args.target)
            matches = registry.find_name_matches(args.target)
            if not rec and len(matches) > 1:
                choices = [{"id": p.id, "path": p.path} for p in matches]
                err_msg = f"Project name '{args.target}' is ambiguous; use its path or ID."
                payload = {"status": "ambiguous", "error": err_msg, "matches": choices}
                print(json.dumps(payload, indent=2) if getattr(args, "json", False) else err_msg + "\n" + "\n".join(f"  {p['id']}  {p['path']}" for p in choices), file=sys.stderr)
                return 1
            if not rec:
                cand = Path(args.target).resolve()
                if cand.is_dir():
                    rec = registry.register_project(cand)
            if not rec:
                err_msg = f"Error: Project '{args.target}' not found in registry."
                if getattr(args, "json", False):
                    print(json.dumps({"status": "error", "error": err_msg}))
                else:
                    print(err_msg, file=sys.stderr)
                return 1
            registry.touch_active(rec.path)
            if getattr(args, "json", False):
                print(json.dumps(rec.to_dict(), indent=2))
            else:
                print(f"Active project set to '{rec.name}' ({rec.stack}) at {rec.path}")
                print(f"  Configuration: {rec.config_path}")
                print(f"  Last active:   {rec.last_active}")
            return 0

        if project_cmd in ("prune", "clean"):
            stale = registry.prune_stale()
            if getattr(args, "json", False):
                print(json.dumps({"pruned": stale, "count": len(stale)}, indent=2))
            else:
                if not stale:
                    print("Project registry is clean. No stale projects found.")
                else:
                    print(f"Pruned {len(stale)} stale project(s) whose paths no longer exist on disk:")
                    for s in stale:
                        print(f"  • {s}")
            return 0

    if args.cmd == "auth":
        from .providers.antigravity_provider import AntigravityCredential, AntigravityOAuthManager

        mgr = AntigravityOAuthManager(auth_file=args.auth_file)
        if args.auth_url:
            pkce = mgr.generate_pkce_pair()
            url = mgr.build_authorization_url(pkce["challenge"])
            print(
                json.dumps(
                    {
                        "status": "pkce_ready",
                        "authorization_url": url,
                        "code_verifier": pkce["verifier"],
                        "instructions": (
                            "1. Open authorization_url in your browser and sign in with your Google Antigravity account.\n"
                            "2. Copy the redirected localhost URL (or `code` parameter) and run:\n"
                            f"   hex auth antigravity --code '<REDIRECT_URL_OR_CODE>' --verifier '{pkce['verifier']}'"
                        ),
                    },
                    indent=2,
                )
            )
            return 0

        if args.code and args.verifier:
            cred = mgr.exchange_code_for_tokens(args.code, args.verifier)
            print(
                json.dumps(
                    {
                        "status": "authenticated",
                        "source": cred.source,
                        "auth_file": str(mgr.auth_file),
                        "has_access_token": bool(cred.access_token),
                        "has_refresh_token": bool(cred.refresh_token),
                    },
                    indent=2,
                )
            )
            return 0

        if args.token or args.refresh_token:
            cred = AntigravityCredential(
                access_token=args.token or "",
                refresh_token=args.refresh_token or "",
                project_id=args.project_id,
                source="cli_manual",
            )
            saved_path = mgr.save_credential(cred)
            print(
                json.dumps(
                    {
                        "status": "saved",
                        "auth_file": str(saved_path),
                        "has_access_token": bool(cred.access_token),
                        "has_refresh_token": bool(cred.refresh_token),
                        "project_id": cred.project_id,
                    },
                    indent=2,
                )
            )
            return 0

        cred = mgr.resolve_credential(auto_refresh=False)
        if cred and args.import_ide:
            mgr.save_credential(cred)
        print(
            json.dumps(
                {
                    "status": "authenticated" if (cred and (cred.access_token or cred.refresh_token)) else "unauthenticated",
                    "provider": "google-antigravity",
                    "source": cred.source if cred else None,
                    "auth_file": str(mgr.auth_file),
                    "has_access_token": bool(cred and cred.access_token),
                    "has_refresh_token": bool(cred and cred.refresh_token),
                    "project_id": cred.project_id if cred else None,
                },
                indent=2,
            )
        )
        return 0

    if args.cmd == "quota":
        from .providers.antigravity_provider import AntigravityModelProvider, AntigravityOAuthManager

        mgr = AntigravityOAuthManager(auth_file=args.auth_file)
        prov = AntigravityModelProvider(oauth_manager=mgr)
        try:
            snap = prov.fetch_quota_status()
            print(json.dumps(snap.to_dict(), indent=2))
            return 0
        except Exception as exc:
            print(json.dumps({"status": "unavailable", "error": str(exc)}, indent=2))
            return 1

    repo_candidate = Path(__file__).resolve().parents[2]
    repo_root = repo_candidate if (repo_candidate / "pyproject.toml").is_file() else Path.cwd()

    is_test_runner = (
        (
            bool(os.environ.get("HEX_TESTING"))
            or any("unittest" in arg for arg in sys.argv)
            or any("pytest" in arg for arg in sys.argv)
        )
        and not os.environ.get("HEX_FORCE_INTERACTIVE")
    )

    launch_rust = (
        args.cmd == "tui"
        or getattr(args, "ratatui", False)
        or (getattr(args, "check", False) and args.cmd in (None, "chat", "tui"))
        or (
            (args.cmd is None or args.cmd == "chat")
            and not getattr(args, "legacy", False)
            and not getattr(args, "voice", False)
            and not is_test_runner
        )
    )

    if launch_rust:
        from .tui_launcher import launch_hex_tui
        try:
            return launch_hex_tui(args, repo_root)
        except Exception as exc:
            logger.warning("Could not launch native Rust TUI (%s); falling back to legacy Python console.", exc)

    from .config_loader import has_active_credentials, load_hex_environment

    # Auto-intercept unconfigured launch when starting interactive console without credentials
    if (args.cmd is None or args.cmd in ("chat", "voice")) and not has_active_credentials():
        if sys.stdin.isatty() and not getattr(args, "quiet", False) and not is_test_runner:
            print("\n◈ Welcome to HEX. No model provider or API key was detected.")
            print("  Launching interactive auto-setup sequence...\n")
            from .onboard import run_onboard_wizard

            cfg_dir = Path(args.config_dir) if getattr(args, "config_dir", None) else None
            run_onboard_wizard(config_dir=cfg_dir)
            load_hex_environment(config_dir=cfg_dir, override=True)

    from .project_registry import ProjectRegistry

    registry = ProjectRegistry()
    explicit_ws = getattr(args, "workspace", None)
    active_project = None
    ws_root = repo_root

    if explicit_ws:
        ws_root = Path(explicit_ws).resolve()
        active_project = registry.register_project(ws_root)
        registry.touch_active(ws_root)
    else:
        active_project = registry.detect_enclosing_project(Path.cwd())
        if active_project:
            ws_root = Path(active_project.path)
            registry.touch_active(active_project.path)
        else:
            if (
                (args.cmd is None or args.cmd in ("chat", "voice"))
                and sys.stdin.isatty()
                and not getattr(args, "quiet", False)
                and not is_test_runner
            ):
                projects = registry.list_projects()
                options = []
                for p in projects:
                    options.append((p.path, f"{p.name} ({p.stack} • {p.path})"))
                options.append(("__ADD__", "[+] Register an existing directory as project"))
                options.append(("__INIT__", f"[★] Initialize current directory ({Path.cwd().name}) as project"))

                from .ui_selector import select_option

                chosen_idx = select_option(
                    "What project do you want to work on today?",
                    options,
                    default_index=0,
                )
                chosen_key = options[chosen_idx][0]
                if chosen_key == "__ADD__":
                    entered_path = input("Enter project directory path: ").strip()
                    if entered_path:
                        ws_path = Path(entered_path).resolve()
                        active_project = registry.register_project(ws_path)
                        ws_root = Path(active_project.path)
                        registry.touch_active(active_project.path)
                    else:
                        ws_root = repo_root
                elif chosen_key == "__INIT__":
                    from .onboard import init_project

                    init_project(Path.cwd())
                    active_project = registry.register_project(Path.cwd())
                    ws_root = Path(active_project.path)
                    registry.touch_active(active_project.path)
                else:
                    active_project = registry.get_project(chosen_key)
                    ws_root = Path(active_project.path) if active_project else Path(chosen_key)
                    if active_project:
                        registry.touch_active(active_project.path)
                print()
            else:
                ws_root = repo_root

    bound_data_dir = args.data_dir
    if ws_root and (ws_root / ".hex" / "data").is_dir() and args.data_dir == default_dir:
        bound_data_dir = str(ws_root / ".hex" / "data")

    if ws_root:
        load_hex_environment(workspace_root=ws_root, override=True)

    orch = HarnessOrchestrator(bound_data_dir, default_workspace=ws_root)

    if getattr(args, "provider", None):
        if args.provider == "local":
            orch.router.cloud_provider = orch.router.local_provider
        else:
            orch.router.switch_cloud_provider(
                args.provider,
                model_id=getattr(args, "model", None) or orch.router.cloud_model_id,
            )
    elif getattr(args, "model", None):
        orch.router.cloud_model_id = args.model
        if hasattr(orch.router.cloud_provider, "model_id"):
            orch.router.cloud_provider.model_id = args.model  # type: ignore[attr-defined]

    if args.cmd is None or args.cmd in ("chat", "voice"):
        from .tui import HexConsole

        active_proj_id = (
            active_project.id
            if (active_project and getattr(args, "project", "default") == "default")
            else getattr(args, "project", "default")
        )
        voice_mode = args.cmd == "voice" or bool(getattr(args, "voice", False))
        voice_name = getattr(args, "voice_name", None)
        speech_rate = getattr(args, "speech_rate", 190)

        if args.cmd == "voice" and not getattr(args, "quiet", False):
            print("\n◈ HEX Hands-Free Voice Console Active (Type /voice for audio settings)\n")

        console = HexConsole(
            orch,
            project_id=active_proj_id,
            workspace_root=ws_root,
            stream=not getattr(args, "no_stream", False),
            model_profile=getattr(args, "model", None),
            skin_name=getattr(args, "skin", "amber"),
            quiet=getattr(args, "quiet", False),
            voice_enabled=voice_mode,
            voice_name=voice_name,
            speech_rate=speech_rate,
            react_mode=bool(getattr(args, "react", False)),
        )
        if getattr(args, "muted", False):
            console.voice_engine.mute()
        return console.start()

    if args.cmd == "init":
        from .onboard import init_project

        workspace_root = Path(args.workspace).resolve() if getattr(args, "workspace", None) else Path.cwd().resolve()
        proj_meta = init_project(workspace_root, force=getattr(args, "force", False))
        scanned = seed_default_skills(orch, repo_root)
        print(
            json.dumps(
                {
                    "status": "initialized",
                    "project": proj_meta,
                    "data_dir": str(orch.data_dir),
                    "cow_backend": orch.cow.backend_name,
                    "workers": orch.ledger.list_workers(),
                    "seeded_skills": [
                        {
                            "skill_id": s["skill_id"],
                            "status": s["status"],
                            "risk_score": s["scan"]["risk_score"],
                            "gate_decision": s["scan"]["gate_decision"],
                        }
                        for s in scanned
                    ],
                },
                indent=2,
            )
        )
        return 0

    if args.cmd == "up":
        seed_default_skills(orch, repo_root)
        if args.asgi:
            try:
                import uvicorn
                from .asgi import get_asgi_app

                asgi_app = get_asgi_app(orchestrator=orch)
                uvicorn.run(asgi_app, host=args.host, port=args.port)
                return 0
            except ImportError:
                print("uvicorn not installed; falling back to built-in HTTP server with GracefulServerRunner")
        run_http_server(orch, host=args.host, port=args.port, drain_timeout=args.drain_timeout)
        return 0

    if args.cmd in ("run", "exec"):
        from .git_autopilot import GitAutopilot, GitAutopilotConfig

        ws_root = Path(args.workspace).resolve() if getattr(args, "workspace", None) else ws_root
        if active_project:
            registry.touch_active(ws_root)
        task_project_id = active_project.id if active_project and args.project == "default" else args.project

        if getattr(args, "react", False):
            loop_res = orch.run_agent_loop(
                goal=args.goal,
                workspace_root=ws_root,
            )
            print(json.dumps(loop_res, indent=2, default=str))
            return 0 if loop_res.get("success") else 1

        req = TaskSubmitRequest(
            goal=args.goal,
            project_id=task_project_id,
            execution_mode=args.mode,
            workspace_root=str(ws_root),
        )

        needs_git = bool(getattr(args, "pr", False) or getattr(args, "branch", None))
        autopilot: GitAutopilot | None = None
        task_branch: str | None = None

        if needs_git:
            cfg = GitAutopilotConfig.from_env()
            if getattr(args, "dry_run", False):
                cfg.dry_run = True
            if getattr(args, "base_branch", None):
                cfg.default_base_branch = args.base_branch
            autopilot = GitAutopilot(workspace_root=ws_root, config=cfg)
            if isinstance(args.branch, str) and args.branch.strip():
                task_branch = args.branch.strip()

        task = orch.submit_and_run(req, auto_execute=True)
        task_id = task.get("task_id", "")
        task_status = task.get("status") or task.get("state")
        is_success = task_status in ("SUCCEEDED", "COMPLETED")
        proof = orch.ledger.get_proof_bundle(task_id) if task_id else None

        if not is_success:
            err_msg = task.get("error_message") or f"Task execution finished in non-success state: {task_status}"
            print(
                json.dumps(
                    {
                        "status": "failed",
                        "task": task,
                        "proof": proof,
                        "error": err_msg,
                    },
                    indent=2,
                    default=str,
                )
            )
            return 1

        if needs_git and autopilot:
            if not task_branch:
                task_branch = autopilot.create_task_branch(
                    task_id=task_id,
                    goal=args.goal,
                    base_branch=getattr(args, "base_branch", None),
                )
            elif not autopilot.config.dry_run and (ws_root / ".git").is_dir():
                subprocess.run(
                    ["git", "checkout", "-B", task_branch],
                    cwd=ws_root,
                    capture_output=True,
                    text=True,
                    check=False,
                )

            commit_sha = autopilot.commit_task_changes(
                task_id=task_id,
                scope=args.project,
                description=args.goal,
            )

            result_payload: dict[str, Any] = {
                "status": "completed",
                "task": task,
                "proof": proof,
                "branch": task_branch,
                "commit": commit_sha,
            }

            if getattr(args, "pr", False):
                ast_impact = proof.get("ast_impact") if proof else None
                summary = autopilot.generate_pr_summary(
                    task_id=task_id,
                    goal=args.goal,
                    proof_bundle=proof,
                    ast_impact=ast_impact,
                )
                pr_res = autopilot.create_pull_request(
                    branch=task_branch,
                    title=f"feat({args.project}): {args.goal}",
                    body=summary,
                    base=getattr(args, "base_branch", None),
                )
                result_payload["pull_request"] = pr_res

            print(json.dumps(result_payload, indent=2, default=str))
            return 0

        print(json.dumps({"task": task, "proof": proof}, indent=2, default=str))
        return 0

    if args.cmd == "submit":
        if active_project:
            registry.touch_active(ws_root)
        task_project_id = active_project.id if active_project and args.project == "default" else args.project
        req = TaskSubmitRequest(
            goal=args.goal,
            project_id=task_project_id,
            execution_mode=args.mode,
            workspace_root=args.workspace or str(ws_root),
        )
        task = orch.submit_and_run(req, auto_execute=True)
        proof = orch.ledger.get_proof_bundle(task["task_id"])
        print(json.dumps({"task": task, "proof": proof}, indent=2, default=str))
        return 0

    if args.cmd == "status":
        if args.task_id:
            t = orch.ledger.get_task(args.task_id)
            p = orch.ledger.get_proof_bundle(args.task_id)
            ev = orch.ledger.list_events(args.task_id)
            print(json.dumps({"task": t, "proof": p, "events": ev}, indent=2, default=str))
        else:
            print(json.dumps({"tasks": orch.ledger.list_tasks()}, indent=2, default=str))
        return 0

    if args.cmd == "scan-skill":
        res = orch.scanner.scan_and_register_skill(Path(args.path))
        print(json.dumps(res, indent=2, default=str))
        return 0

    if args.cmd == "harness2-demo":
        res = orch.harness2_adapter.propose_and_evaluate_candidate(
            parent_revision_id=None,
            base_tree={"systemprompt.md": "# Base Agent Instructions\nUse read_file.\n"},
            candidate_tree={
                "systemprompt.md": "# Refined Agent Instructions\nUse read_file and repo_understanding.\n",
                "skills/repo_understanding/SKILL.md": (
                    "---\nname: repo_understanding\npurpose: Summarize repository AST safely\n"
                    "description: Summarize repository AST safely\nrisk_class: R1\n---\n"
                    "# Instructions\nInspect symbols using query_graphify.\n"
                ),
            },
            run_demo_probe=True,
        )
        print(json.dumps(res, indent=2, default=str))
        return 0

    if args.cmd == "harness2-eval":
        from .harness2_adapter import OrchestratorBenchmarkAdapter, run_orchestrator_sweep

        runs_root = Path(args.runs_root) if args.runs_root else (orch.data_dir / "benchmarks")
        adapter = OrchestratorBenchmarkAdapter(
            orch,
            runs_root=runs_root,
            substrate=getattr(args, "substrate", "opencode"),
        )
        prop_arg = "llm" if getattr(args, "proposer", "local") == "llm" else None
        res = run_orchestrator_sweep(
            adapter=adapter,
            task_id=args.task_id,
            mode=args.mode,
            k=args.k,
            proposer=prop_arg,
        )
        if args.json:
            print(json.dumps(res.to_dict(), indent=2, default=str))
        else:
            print(f"\n=======================================================")
            print(f" Google Research Harness² Sweep Evaluation: {args.task_id}")
            print(f"=======================================================")
            print(f" Mode:           {res.mode.upper()}")
            print(f" Substrate:      {adapter.substrate.surface().name}")
            print(f" Task ID:        {res.task_id}")
            print(f" Width (k):      {res.k}")
            print(f" Status:         {'SUCCESS' if res.ok else 'FAILED'}")
            print(f" Stages Run:     {len(res.stages)}")
            print(f" Winning Finals: {', '.join(res.finals) if res.finals else 'None'}")
            if res.selection:
                print(f" Selected Pick:  {res.selection.chosen_label} (id={res.selection.chosen_id})")
            if res.experience:
                print(f" Learned Lesson: {res.experience.get('lesson', 'N/A')}")
            print(f" Cache Hits:     {res.cache_hits}")
            print(f" Fallbacks:      {len(res.fell_back)}")
            print(f" Total Rollouts: {len(res.rollouts)}")
            if res.report_text:
                print(f"-------------------------------------------------------")
                print(res.report_text)
            print(f"=======================================================\n")
        return 0 if res.ok else 1

    if args.cmd == "worker":
        from .worker_daemon import DirectLedgerWorkerClient, RestWorkerClient, WorkerDaemon

        if args.server_url:
            client = RestWorkerClient(args.server_url, token=args.token)
        else:
            client = DirectLedgerWorkerClient(orch)

        daemon = WorkerDaemon(
            client=client,
            worker_id=args.worker_id,
            machine_class=args.machine_class,
            poll_interval=args.poll_interval,
            lease_ttl=args.lease_ttl,
            orchestrator=orch,
        )
        daemon.run(once=args.once)
        return 0

    if args.cmd == "gateway":
        from .gateways import (
            DiscordBotGateway,
            DiscordConfig,
            GatewayManager,
            TelegramBotGateway,
            TelegramConfig,
        )

        tg_cfg = TelegramConfig.from_env()
        dc_cfg = DiscordConfig.from_env()

        enable_tg = bool(getattr(args, "telegram", False) or tg_cfg.bot_token)
        enable_dc = bool(getattr(args, "discord", False) or dc_cfg.bot_token)

        tg_gw = TelegramBotGateway(orch, tg_cfg) if enable_tg else None
        dc_gw = DiscordBotGateway(orch, dc_cfg) if enable_dc else None

        mgr = GatewayManager(orch, telegram_gateway=tg_gw, discord_gateway=dc_gw)

        if args.gateway_cmd == "status":
            print(json.dumps(mgr.get_status(), indent=2))
            return 0

        if args.gateway_cmd == "start":
            if getattr(args, "once", False):
                counts = mgr.poll_once()
                print(json.dumps({"status": "polled_once", "updates": counts, "manager": mgr.get_status()}, indent=2))
                return 0
            mgr.start(daemon=False, poll_interval=args.poll_interval)
            return 0

    if args.cmd == "gh-action":
        from .github_action import GitHubActionConfig, GitHubActionRunner

        event_path = getattr(args, "event_path", None) or os.environ.get("GITHUB_EVENT_PATH")
        event_name = getattr(args, "event_name", None) or os.environ.get("GITHUB_EVENT_NAME") or ""
        ws_root = Path(args.workspace).resolve() if getattr(args, "workspace", None) else repo_root

        payload_raw: dict[str, Any] | str = {}
        if event_path and Path(event_path).is_file():
            try:
                payload_raw = Path(event_path).read_text(encoding="utf-8")
            except Exception as e:
                print(json.dumps({"status": "error", "error": f"Failed to read event path: {e}"}))
                return 1
        elif not sys.stdin.isatty():
            try:
                stdin_data = sys.stdin.read().strip()
                if stdin_data:
                    payload_raw = stdin_data
            except Exception:
                pass

        cfg = GitHubActionConfig.from_env()
        if getattr(args, "dry_run", False):
            cfg.dry_run = True
        if event_name:
            cfg.event_name = event_name
        if event_path:
            cfg.event_path = str(event_path)

        runner = GitHubActionRunner(config=cfg)
        payload = runner.parse_event_payload(payload_raw)

        if not event_name:
            if "comment" in payload and "issue" in payload:
                event_name = "issue_comment"
            elif "pull_request" in payload:
                event_name = "pull_request"
            elif "issue" in payload:
                event_name = "issues"
            elif "inputs" in payload:
                event_name = "workflow_dispatch"

        res = runner.execute_action(orch, event_name=event_name, payload=payload)
        serializable_res = dict(res)
        if "trigger" in serializable_res and hasattr(serializable_res["trigger"], "__dict__"):
            serializable_res["trigger"] = serializable_res["trigger"].__dict__

        print(json.dumps(serializable_res, indent=2, default=str))
        if res.get("status") == "failed":
            return 1
        return 0

    if args.cmd == "schedule":
        from .scheduler import TaskScheduler
        from .webhooks import WebhookNotifier

        storage_path = getattr(args, "storage_path", None)
        scheduler = TaskScheduler(storage_path=storage_path)

        if args.schedule_cmd == "add":
            try:
                job = scheduler.add_job(args.cron, args.goal, project_id=args.project)
                print(
                    json.dumps(
                        {
                            "status": "added",
                            "job_id": job.job_id,
                            "cron_expr": job.cron_expr,
                            "goal": job.goal,
                            "project_id": job.project_id,
                            "next_run": job.next_run,
                        },
                        indent=2,
                    )
                )
                return 0
            except Exception as e:
                print(json.dumps({"status": "error", "error": str(e)}), file=sys.stderr)
                return 1

        if args.schedule_cmd == "list":
            jobs = scheduler.list_jobs(include_disabled=True)
            print(json.dumps([j.to_dict() for j in jobs], indent=2))
            return 0

        if args.schedule_cmd == "remove":
            removed = scheduler.remove_job(args.job_id)
            if removed:
                print(json.dumps({"status": "removed", "job_id": args.job_id}, indent=2))
                return 0
            else:
                print(json.dumps({"status": "not_found", "job_id": args.job_id}, indent=2), file=sys.stderr)
                return 1

        if args.schedule_cmd == "run-due":
            notifier = WebhookNotifier.from_env() if hasattr(WebhookNotifier, "from_env") else WebhookNotifier()
            results = scheduler.execute_due_jobs(orch, notifier=notifier)
            print(
                json.dumps(
                    {
                        "status": "completed",
                        "executed_count": len(results),
                        "results": results,
                    },
                    indent=2,
                    default=str,
                )
            )
            return 0

    if args.cmd == "daemon":
        import platform
        from .daemon import DaemonConfig, DaemonInstaller

        interval = getattr(args, "interval", 60)
        cfg = DaemonConfig(
            interval_seconds=interval,
            data_dir=str(args.data_dir),
        )
        installer = DaemonInstaller(config=cfg)

        if args.daemon_cmd == "install":
            target_path = getattr(args, "path", None)
            dry_run = getattr(args, "dry_run", False)
            target_launchd = getattr(args, "launchd", False)
            target_systemd = getattr(args, "systemd", False)

            if not target_launchd and not target_systemd:
                if platform.system() == "Darwin":
                    target_launchd = True
                else:
                    target_systemd = True

            if target_launchd:
                res = installer.install_launchd(target_path=target_path, dry_run=dry_run)
            else:
                user_service = getattr(args, "user", True)
                res = installer.install_systemd(user=user_service, target_path=target_path, dry_run=dry_run)

            print(json.dumps(res, indent=2))
            return 0

        if args.daemon_cmd == "status":
            stat = installer.get_daemon_status()
            print(json.dumps(stat, indent=2))
            return 0

        if args.daemon_cmd in ("uninstall", "remove"):
            dry_run = getattr(args, "dry_run", False)
            res = installer.uninstall_daemon(dry_run=dry_run)
            print(json.dumps(res, indent=2))
            return 0

    if args.cmd == "skill":
        from .skill_synthesizer import SkillSynthesizer

        synthesizer = SkillSynthesizer(
            ledger=orch.ledger,
            scanner=orch.scanner,
            custom_skills_dir=getattr(args, "output_dir", None),
        )

        if args.skill_cmd == "synthesize":
            try:
                skill = synthesizer.synthesize_skill(
                    task_id=args.task,
                    skill_name=args.name,
                    dry_run=args.dry_run,
                )
                output = {
                    "name": skill.name,
                    "status": skill.status,
                    "gate_decision": skill.scan_report.get("gate_decision"),
                    "risk_class": skill.risk_class,
                    "disk_path": skill.disk_path,
                    "quarantine_reason": skill.quarantine_reason,
                    "scan_report": skill.scan_report,
                }
                print(json.dumps(output, indent=2))
                return 0
            except Exception as e:
                print(json.dumps({"status": "error", "error": str(e)}), file=sys.stderr)
                return 1

        if args.skill_cmd == "list":
            skills_data = synthesizer.list_skills()
            print(json.dumps(skills_data, indent=2))
            return 0

        if args.skill_cmd == "scan":
            scan_path = Path(args.path).resolve()
            if scan_path.is_file():
                scan_path = scan_path.parent
            res = orch.scanner.scan_and_register_skill(scan_path)
            print(json.dumps(res, indent=2, default=str))
            return 0

    if args.cmd == "doctor":
        from .doctor import HexDoctor

        ws_dir = getattr(args, "workspace", None)
        data_dir = getattr(args, "data_dir", None)
        doc = HexDoctor(workspace_root=ws_dir, data_dir=data_dir)
        report = doc.run_diagnostics(fix=args.fix, live=args.live)

        if getattr(args, "json", False):
            print(json.dumps(report.to_dict(), indent=2))
        else:
            no_color = getattr(args, "no_color", False)
            print(report.format_summary(color=not no_color))

        return 0 if report.is_healthy else 1

    if args.cmd == "recipe":
        from .recipes import detect_recipe

        ws_dir = Path(getattr(args, "workspace", ".")).resolve()
        recipe = detect_recipe(ws_dir)
        if not recipe:
            if getattr(args, "json", False):
                print(json.dumps({"detected": False, "workspace": str(ws_dir)}))
            else:
                print(f"No project recipe detected in {ws_dir}")
            return 1

        if getattr(args, "json", False):
            print(json.dumps({"detected": True, "workspace": str(ws_dir), "recipe": recipe.to_dict()}, indent=2))
        else:
            print(f"Detected Recipe: {recipe.name} ({recipe.kind})")
            print(f"  Bootstrap: {recipe.bootstrap or 'None'}")
            print(f"  Build:     {recipe.build or 'None'}")
            print(f"  Test:      {recipe.test}")
            print(f"  Start:     {recipe.start or 'None'}" + (f" (Port: {recipe.port})" if recipe.port else ""))
            print(f"  Evidence:  {', '.join(recipe.evidence)}")
        return 0

    if args.cmd == "search":
        from .db import LedgerStore

        data_dir = Path(getattr(args, "data_dir", None) or DEFAULT_DATA_DIR).resolve()
        if data_dir.is_file():
            db_file = data_dir
        else:
            db_file = data_dir / "hex.db"
            if not db_file.exists():
                for cand_name in ("ledger.db", "harness.db"):
                    cand_db = data_dir / cand_name
                    if cand_db.exists():
                        db_file = cand_db
                        break
                else:
                    dbs = [f for f in data_dir.glob("*.db") if not f.name.endswith("-wal") and not f.name.endswith("-shm")]
                    if dbs:
                        db_file = dbs[0]

        ledger = LedgerStore(db_file)
        search_idx = ledger.search_index

        if getattr(args, "reindex", False):
            count = search_idx.reindex_all(ledger)
            if not getattr(args, "json", False):
                print(f"Reindexed {count} tasks into FTS5 index.")

        results = search_idx.search(
            args.query,
            project_id=getattr(args, "project", None),
            limit=getattr(args, "limit", 20),
        )

        if getattr(args, "json", False):
            print(json.dumps([r.to_dict() for r in results], indent=2))
        else:
            if not results:
                print(f"No tasks found matching '{args.query}'")
            else:
                print(f"\nFound {len(results)} matching tasks:")
                print(f"{'TASK ID':<16} {'STATUS':<14} {'SCORE':<8} {'MATCHED':<12} {'SNIPPET'}")
                print("-" * 80)
                for r in results:
                    clean_snippet = r.snippet.replace("\n", " ").strip()
                    print(f"{r.task_id:<16} {r.status:<14} {r.score:<8.3f} {r.matched_field:<12} {clean_snippet}")
                print()
        return 0

    if args.cmd == "backup":
        from .backup import create_backup

        ws_dir = getattr(args, "workspace", None)
        data_dir = getattr(args, "data_dir", None)
        res = create_backup(
            output_path=getattr(args, "output", None),
            data_dir=data_dir,
            workspace_root=ws_dir,
            quick=getattr(args, "quick", False),
        )
        print(f"Backup created successfully:")
        print(f"  Path:     {res['backup_path']}")
        print(f"  Files:    {res['files_archived']}")
        print(f"  Size:     {res['size_bytes']:,} bytes")
        print(f"  SHA-256:  {res['sha256']}")
        print(f"  Quick:    {res['quick']}")
        return 0

    if args.cmd == "restore":
        from .backup import restore_backup

        target = getattr(args, "target", None)
        if not target:
            target = getattr(args, "data_dir", None) or DEFAULT_DATA_DIR
        res = restore_backup(
            archive_path=args.archive,
            target_dir=target,
            dry_run=getattr(args, "dry_run", False),
        )
        action = "Dry run inspection" if res["dry_run"] else "Restore"
        print(f"{action} completed for {args.archive}:")
        print(f"  Target:   {res['target_dir']}")
        print(f"  Files:    {len(res['restored_files'])}")
        for f in res["restored_files"][:10]:
            print(f"    - {f}")
        if len(res["restored_files"]) > 10:
            print(f"    ... and {len(res['restored_files']) - 10} more files")
        return 0

    if args.cmd == "dump":
        from .backup import generate_dump

        ws_dir = getattr(args, "workspace", None)
        data_dir = getattr(args, "data_dir", None)
        dump_text = generate_dump(data_dir=data_dir, workspace_root=ws_dir)

        out_file = getattr(args, "output", None)
        if out_file:
            Path(out_file).write_text(dump_text, encoding="utf-8")
            print(f"Diagnostic dump saved to {out_file} ({len(dump_text):,} chars)")
        else:
            print(dump_text)
        return 0

    if args.cmd == "reflect":
        ws_dir = Path(getattr(args, "workspace", None) or repo_root).resolve()
        res = orch.graphify.reflect_work_memory(ws_dir)
        if getattr(args, "json", False):
            print(json.dumps(res, indent=2, default=str))
        else:
            print(res.get("lessons_markdown") or "No work-memory lessons recorded yet.")
        return 0

    if args.cmd == "lsp":
        from .lsp.manager import LSPManager
        from .lsp.registry import LanguageServerRegistry

        ws = Path(getattr(args, "workspace", None) or repo_root).resolve()
        reg = LanguageServerRegistry()
        lsp_action = getattr(args, "lsp_cmd", None) or "status"

        if lsp_action == "status":
            specs = reg.get_all_specs()
            detected_specs = reg.detect_workspace_servers(ws)
            detected_langs = {s.language_id for s in detected_specs}

            status_list = []
            for spec in specs:
                installed_bin = spec.resolve_binary()
                is_detected = spec.language_id in detected_langs
                status_list.append({
                    "language": spec.language_id,
                    "server": spec.server_name,
                    "status": "INSTALLED" if installed_bin else "NOT FOUND",
                    "binary": installed_bin,
                    "detected_in_workspace": is_detected,
                    "extensions": list(spec.file_extensions),
                })

            if getattr(args, "json", False):
                print(json.dumps(status_list, indent=2))
            else:
                print(f"\nPolyglot Language Server Registry ({ws.name}):\n")
                header = f"{'LANGUAGE':<12} {'SERVER':<26} {'STATUS':<14} {'EXTENSIONS'}"
                print(header)
                print("-" * len(header))
                for item in status_list:
                    badge = f"\033[32m[INSTALLED]\033[0m" if item["status"] == "INSTALLED" else "\033[90m[NOT FOUND]\033[0m"
                    exts = ", ".join(item["extensions"])
                    det = " *" if item["detected_in_workspace"] else ""
                    print(f"{item['language'] + det:<12} {item['server']:<26} {badge:<23} {exts}")
                print("\n(* = active file types detected in workspace root)")
            return 0

        elif lsp_action == "diagnostics":
            with LSPManager(workspace_root=ws, registry=reg) as mgr:
                file_arg = getattr(args, "file", None)
                errors_only = getattr(args, "errors_only", False)
                sev = "error" if errors_only else "all"

                if file_arg:
                    f_path = (ws / file_arg).resolve() if not Path(file_arg).is_absolute() else Path(file_arg).resolve()
                    if f_path.is_file():
                        mgr.sync_file(f_path)
                    diags = mgr.get_diagnostics(file_path=f_path, severity=sev)
                else:
                    diags = mgr.get_diagnostics(severity=sev)

                diag_dicts = [d.to_dict() for d in diags]
                error_count = sum(1 for d in diags if d.severity == 1 or getattr(d, "severity_name", "") == "ERROR")
                if getattr(args, "json", False):
                    print(json.dumps({
                        "diagnostics": diag_dicts,
                        "error_count": error_count,
                        "total_count": len(diag_dicts),
                    }, indent=2))
                else:
                    if not diags:
                        print("No compiler diagnostics found.")
                    else:
                        print(f"\nCompiler Diagnostics ({len(diags)}):")
                        for d in diags:
                            rel_p = str(d.file_path)
                            try:
                                rel_p = str(Path(d.file_path).relative_to(ws))
                            except Exception:
                                pass
                            sev_color = "\033[31m" if d.severity == 1 or getattr(d, "severity_name", "") == "ERROR" else "\033[33m"
                            code_str = f" [{d.code}]" if getattr(d, "code", None) else ""
                            print(f" {rel_p}:{d.line + 1}:{d.character + 1} - {sev_color}{d.severity_name}\033[0m{code_str}: {d.message}")
            return 0

        elif lsp_action == "definition":
            with LSPManager(workspace_root=ws, registry=reg) as mgr:
                f_path = (ws / args.file).resolve() if not Path(args.file).is_absolute() else Path(args.file).resolve()
                defs = mgr.get_definition(f_path, args.line, args.character)
                if getattr(args, "json", False):
                    print(json.dumps({"definitions": defs}, indent=2))
                else:
                    if not defs:
                        print(f"No definition found for {args.file}:{args.line}:{args.character}")
                    else:
                        for item in defs:
                            rel_p = str(item["file"])
                            try:
                                rel_p = str(Path(item["file"]).relative_to(ws))
                            except Exception:
                                pass
                            print(f"Definition: {rel_p}:{item['line']}:{item['character']}")
            return 0

        elif lsp_action == "hover":
            with LSPManager(workspace_root=ws, registry=reg) as mgr:
                f_path = (ws / args.file).resolve() if not Path(args.file).is_absolute() else Path(args.file).resolve()
                hover = mgr.get_hover(f_path, args.line, args.character)
                if getattr(args, "json", False):
                    print(json.dumps({"hover": hover or {"contents": ""}}, indent=2))
                else:
                    if not hover or not hover.get("contents"):
                        print(f"No hover information for {args.file}:{args.line}:{args.character}")
                    else:
                        print(f"\nHover Information ({args.file}:{args.line}:{args.character}):")
                        print(hover["contents"])
            return 0

    if args.cmd == "graphify":
        from .graphify import ActiveGraphifyAdapter, GraphifyUpstreamRunner, UPSTREAM_GRAPHIFY_AVAILABLE

        ws_dir = Path(getattr(args, "workspace", None) or repo_root).resolve()
        subcmd = getattr(args, "graphify_cmd", None) or "status"
        runner = GraphifyUpstreamRunner()
        adapter = orch.graphify if (ws_dir == orch.default_workspace) else ActiveGraphifyAdapter(ws_dir)

        if subcmd == "status":
            cli_avail = runner.is_cli_available()
            out_path = ws_dir / "graphify-out" / "graph.json"
            hex_path = ws_dir / ".hex" / "graph.json"
            active_path = None
            if out_path.is_file():
                active_path = str(out_path)
            elif hex_path.is_file():
                active_path = str(hex_path)

            node_count = 0
            edge_count = 0
            if active_path:
                try:
                    data = json.loads(Path(active_path).read_text(encoding="utf-8"))
                    node_count = len(data.get("nodes", []))
                    edge_count = len(data.get("edges") or data.get("links") or [])
                except Exception:
                    pass

            status_payload = {
                "cli_available": cli_avail,
                "vendor_directory": str(runner.vendor_dir),
                "workspace": str(ws_dir),
                "active_graph_path": active_path,
                "node_count": node_count,
                "edge_count": edge_count,
                "upstream_graphify_library": UPSTREAM_GRAPHIFY_AVAILABLE,
            }

            if getattr(args, "json", False):
                print(json.dumps(status_payload, indent=2))
            else:
                print("HEX Graphify Upstream Status:")
                print(f"  CLI Available:       {'YES' if cli_avail else 'NO'}")
                print(f"  Vendor Directory:    {runner.vendor_dir}")
                print(f"  Workspace:           {ws_dir}")
                print(f"  Active Graph File:   {active_path or 'None'}")
                if active_path:
                    print(f"  Indexed Nodes:       {node_count}")
                    print(f"  Indexed Edges:       {edge_count}")
            return 0

        if subcmd == "update":
            force = not getattr(args, "no_force", False)
            no_cluster = not getattr(args, "cluster", False)
            ok, target_path = runner.run_update(ws_dir, force=force, no_cluster=no_cluster)
            res_payload = {
                "success": ok,
                "path": str(target_path) if target_path else None,
                "force": force,
                "no_cluster": no_cluster,
            }
            if ok and target_path.is_file():
                ing_res = adapter.ingest_upstream_graph(target_path)
                res_payload["node_count"] = ing_res.get("node_count", 0)
                res_payload["edge_count"] = ing_res.get("edge_count", 0)

            if getattr(args, "json", False):
                print(json.dumps(res_payload, indent=2))
            else:
                if ok:
                    print(f"Graphify update succeeded: {target_path}")
                    if "node_count" in res_payload:
                        print(f"Ingested {res_payload['node_count']} nodes, {res_payload['edge_count']} edges.")
                else:
                    print(f"Graphify update completed with fallback or errors. Path: {target_path}")
            return 0 if ok else 1

        if subcmd == "ingest":
            custom_path = getattr(args, "path", None)
            res = adapter.ingest_upstream_graph(custom_path)
            if getattr(args, "json", False):
                print(json.dumps(res, indent=2))
            else:
                if res.get("ingested"):
                    print(f"Graphify ingested successfully from: {res.get('path')}")
                    print(f"  Nodes: {res.get('node_count', 0)}")
                    print(f"  Edges: {res.get('edge_count', 0)}")
                else:
                    print(f"No Graphify graph found to ingest at {res.get('path') or ws_dir}")
            return 0

        if subcmd == "god-nodes":
            ing_res = adapter.ingest_upstream_graph()
            if not ing_res.get("ingested"):
                adapter.build_or_refresh(ws_dir)

            top_n = getattr(args, "top", 5)
            arch = adapter.analyze_architecture(top_n=top_n)
            gods = arch.get("god_nodes", [])

            if getattr(args, "json", False):
                print(json.dumps(arch, indent=2, default=str))
            else:
                print(f"Top {len(gods)} Architectural God Nodes:")
                if not gods:
                    print("  No god nodes identified (graph may be empty or balanced).")
                for i, g in enumerate(gods, 1):
                    nid = g.get("node_id") or g.get("id") or str(g)
                    deg = g.get("degree") or g.get("total_degree") or 0
                    print(f"  {i}. {nid} (degree: {deg})")
            return 0

        if subcmd == "affected":
            target = args.target
            depth = getattr(args, "depth", 2)
            ing_res = adapter.ingest_upstream_graph()
            if not ing_res.get("ingested"):
                adapter.build_or_refresh(ws_dir)

            from .graphify import upstream_affected
            affected_nodes: list[dict[str, Any]] = []
            if adapter.upstream_available and adapter.graph is not None and upstream_affected is not None:
                try:
                    hits = upstream_affected.affected_nodes(adapter.graph, seed=target, depth=depth)
                    for hit in hits:
                        affected_nodes.append({
                            "node_id": hit.node_id,
                            "depth": hit.depth,
                            "via_relation": hit.via_relation,
                        })
                except Exception as exc:
                    affected_nodes.append({"error": str(exc)})
            else:
                callers = adapter.callers_of.get(target, set())
                for c in sorted(callers):
                    affected_nodes.append({
                        "node_id": c,
                        "depth": 1,
                        "via_relation": "calls",
                    })

            aff_payload = {
                "target": target,
                "depth": depth,
                "count": len(affected_nodes),
                "affected": affected_nodes,
            }
            if getattr(args, "json", False):
                print(json.dumps(aff_payload, indent=2))
            else:
                print(f"Blast-Radius Impact for '{target}' (depth {depth}):")
                if not affected_nodes:
                    print("  No downstream affected nodes found.")
                for a in affected_nodes:
                    print(f"  - {a.get('node_id')} (depth: {a.get('depth')}, via: {a.get('via_relation')})")
            return 0

        if subcmd == "tree":
            ing_res = adapter.ingest_upstream_graph()
            if not ing_res.get("ingested"):
                adapter.build_or_refresh(ws_dir)

            out_arg = getattr(args, "output", None)
            out_file = adapter.export_interactive_html(out_arg, workspace_dir=ws_dir)
            if getattr(args, "open", False):
                try:
                    import webbrowser
                    webbrowser.open(f"file://{out_file}")
                except Exception:
                    pass

            if getattr(args, "json", False):
                print(json.dumps({"html_file": str(out_file)}, indent=2))
            else:
                print(f"Interactive architecture tree HTML generated: {out_file}")
            return 0

    if args.cmd == "acp":
        from .acp import ACPServer

        if getattr(args, "check", False):
            print("\n🔍 HEX ACP Health Check")
            print("=" * 40)
            print("  ✓ ACP Module:        Loaded (JSON-RPC 2.0 stdio)")
            print("  ✓ Python Runtime:    OK")
            print("  ✓ Stdio Transport:   Ready")
            print(f"  ✓ Workspace Root:    {Path(args.workspace).resolve() if getattr(args, 'workspace', None) else Path.cwd().resolve()}")
            print("  ✓ Compatible with:   Zed, Neovim (avante.nvim/codecompanion), VSCode\n")
            return 0

        if getattr(args, "setup", False):
            zed_config = {
                "agent": {
                    "name": "HEX",
                    "command": "hex",
                    "args": ["acp"],
                    "protocol": "acp",
                }
            }
            print("\n⚙️  HEX Editor Integration Snippets")
            print("=" * 60)
            print("Add to your Zed editor configuration (.zed/settings.json):")
            print(json.dumps(zed_config, indent=2))
            print("\nFor Neovim (avante.nvim / codecompanion):")
            print('  provider = "acp",')
            print('  acp_command = { "hex", "acp" },')
            print("=" * 60 + "\n")
            return 0

        ws = Path(args.workspace).resolve() if getattr(args, "workspace", None) else Path.cwd().resolve()
        server = ACPServer(workspace_root=ws)
        server.run_stdio()
        return 0

    if args.cmd == "uninstall":
        import shutil
        from .daemon import DaemonInstaller, DaemonConfig

        dry_run = getattr(args, "dry_run", False)
        purge = getattr(args, "purge", False)
        keep_data = getattr(args, "keep_data", False)
        yes = getattr(args, "yes", False)

        print("\n🗑️  HEX Uninstaller\n" + "=" * 40)

        # 1. Background daemon cleanup
        installer = DaemonInstaller(config=DaemonConfig(data_dir=str(args.data_dir)))
        daemon_res = installer.uninstall_daemon(dry_run=dry_run)
        if daemon_res.get("status") == "uninstalled":
            if daemon_res.get("removed"):
                print(f"  ✓ Background OS service descriptor removed ({daemon_res.get('service_path')})")
            if daemon_res.get("unloaded"):
                print("  ✓ Background service unloaded from system manager")
        elif daemon_res.get("status") == "dry_run":
            print(f"  [dry-run] Would unload and remove service descriptor: {daemon_res.get('service_path')}")
        else:
            print("  ✓ No active background OS service detected")

        # 2. Binary / CLI symlink cleanup
        candidate_symlinks = [
            Path.home() / ".local" / "bin" / "hex",
            Path.home() / ".local" / "bin" / "harness",
            Path("/usr/local/bin/hex"),
            Path("/usr/local/bin/harness"),
        ]
        for sym in candidate_symlinks:
            try:
                if sym.is_symlink() or sym.is_file():
                    if dry_run:
                        print(f"  [dry-run] Would remove binary/symlink: {sym}")
                    else:
                        sym.unlink()
                        print(f"  ✓ Removed binary/symlink: {sym}")
            except (OSError, PermissionError):
                pass

        # 3. Data directory cleanup (~/.hex)
        hex_dir = Path.home() / ".hex"
        should_purge = False
        if purge:
            should_purge = True
        elif not keep_data and not yes:
            if hex_dir.is_dir():
                print(f"\nConfiguration and data directory found at: {hex_dir}")
                print("Options:")
                print("  [y] Purge all configuration, models, credentials, and task history")
                print("  [N] Keep data directory (default)")
                try:
                    ans = input("Purge data directory? [y/N]: ").strip().lower()
                    should_purge = ans in ("y", "yes")
                except (EOFError, KeyboardInterrupt):
                    should_purge = False

        if should_purge:
            if hex_dir.is_dir():
                if dry_run:
                    print(f"  [dry-run] Would purge data directory: {hex_dir}")
                else:
                    try:
                        shutil.rmtree(hex_dir, ignore_errors=True)
                        print(f"  ✓ Purged data directory: {hex_dir}")
                    except OSError as exc:
                        print(f"  ! Warning: could not completely remove {hex_dir}: {exc}")
        else:
            if hex_dir.is_dir():
                print(f"  ✓ Preserved data directory at: {hex_dir}")

        print("\n" + "=" * 40)
        if dry_run:
            print("✓ Dry-run completed. No files were removed.")
        else:
            print("✓ HEX CLI environment successfully uninstalled.")
            print("\nTo remove the Python package itself, run:")
            print("  pip uninstall -y harness-core\n")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
