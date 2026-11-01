"""CLI Entrypoint (`hex`) for Owner Workflows and Debugging (PRD Sections 5, 8.1, 15).

Commands:
- `hex onboard`: Run interactive onboarding wizard for hardware discovery, model profile, and configuration.
- `hex init`: Initialize local database, CoW snapshot store, and seed default repo skills.
- `hex up`: Start the local web control surface & HTTP API on 127.0.0.1:8420.
- `hex submit <goal>`: Submit and execute a task (`--fast-path` or `--standard`).
- `hex status [task_id]`: Inspect task state, call journal, and Proof-Carrying Artifact bundle.
- `hex scan-skill <dir>`: Run NVIDIA SkillSpector static + OSV + semantic scan on a SKILL.md bundle.
- `hex harness2-demo`: Run a live Propose-Probe-Compose cycle through the gated `vendor/harness2` adapter.
- `hex worker`: Start standalone distributed worker daemon.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

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
    default_dir = os.environ.get("HEX_DATA_DIR") or os.environ.get("HARNESS_DATA_DIR")
    if not default_dir:
        repo_data = Path(__file__).resolve().parents[2] / ".hex_data"
        legacy_data = Path(__file__).resolve().parents[2] / ".harness_data"
        default_dir = str(repo_data if repo_data.exists() or not legacy_data.exists() else legacy_data)

    parser = argparse.ArgumentParser(prog="hex", description="HEX: Autonomous AI Workbench CLI")
    parser.add_argument(
        "--data-dir",
        default=default_dir,
        help="Path to local HEX ledger and CoW storage directory",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_onb = sub.add_parser("onboard", help="Run interactive onboarding wizard for hardware & configuration")
    p_onb.add_argument("--config-dir", default=None, help="Custom configuration directory (defaults to ~/.hex)")
    p_onb.add_argument("--non-interactive", action="store_true", help="Run in non-interactive mode using hardware defaults")
    p_onb.add_argument("--defaults", dest="defaults_only", action="store_true", help="Accept all hardware auto-detected defaults")

    p_init = sub.add_parser("init", help="Initialize project configuration, ledger schema, and seed built-in skills")
    p_init.add_argument("--workspace", default=None, help="Path to project repository root (defaults to current working directory)")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing .hex/config.yaml")
    p_init.add_argument("--data-dir", default=argparse.SUPPRESS, help="Path to local HEX ledger and CoW storage directory")

    p_up = sub.add_parser("up", help="Start local web control surface and /v1/* HTTP API")
    p_up.add_argument("--host", default="127.0.0.1")
    p_up.add_argument("--port", type=int, default=8420)
    p_up.add_argument("--drain-timeout", type=float, default=5.0, help="Graceful shutdown drain timeout in seconds")
    p_up.add_argument("--asgi", action="store_true", help="Run via ASGI uvicorn server if installed")

    p_sub = sub.add_parser("submit", help="Submit and execute a task")
    p_sub.add_argument("goal", help="Task goal description")
    p_sub.add_argument("--project", default="default")
    p_sub.add_argument("--mode", choices=["auto", "fast_path", "standard"], default="auto")
    p_sub.add_argument("--workspace", default=None)

    p_stat = sub.add_parser("status", help="Inspect task status, events, and Proof-Carrying Artifact")
    p_stat.add_argument("task_id", nargs="?", default=None)

    p_scan = sub.add_parser("scan-skill", help="Scan and import a SKILL.md directory bundle via SkillSpector")
    p_scan.add_argument("path", help="Path to skill bundle directory")

    sub.add_parser("harness2-demo", help="Run a gated Harness2 Propose-Probe-Compose refinement verification")

    p_wrk = sub.add_parser("worker", help="Start standalone distributed worker daemon")
    p_wrk.add_argument("--worker-id", default=None, help="Worker ID (defaults to hostname-hash)")
    p_wrk.add_argument("--machine-class", choices=["auto", "m4_air", "desktop_gpu", "server"], default="auto")
    p_wrk.add_argument("--poll-interval", type=float, default=2.0, help="Task queue poll interval in seconds")
    p_wrk.add_argument("--lease-ttl", type=int, default=300, help="Lease TTL in seconds")
    p_wrk.add_argument("--server-url", default=None, help="Control plane HTTP URL. If omitted, uses local direct ledger")
    p_wrk.add_argument("--token", default=None, help="Worker authentication token")
    p_wrk.add_argument("--once", action="store_true", help="Execute at most one claimed task and exit")

    p_chat = sub.add_parser("chat", aliases=["tui"], help="Start interactive TUI pair-programming console")
    p_chat.add_argument("--project", default="default", help="Project ID")
    p_chat.add_argument("--workspace", default=None, help="Workspace path (defaults to repository root)")
    p_chat.add_argument("--model", default=None, help="Model routing profile override (hybrid, local, cloud)")
    p_chat.add_argument("--skin", default="amber", choices=["amber", "cyberpunk", "matrix", "nord", "mono"], help="TUI color skin")
    p_chat.add_argument("-Q", "--quiet", action="store_true", help="Quiet mode: suppress startup banner and spinner")
    p_chat.add_argument("--no-stream", action="store_true", help="Disable live token streaming")
    p_chat.add_argument("--data-dir", default=argparse.SUPPRESS, help="Path to local HEX ledger and CoW storage directory")

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

    args = parser.parse_args(argv)

    if args.cmd == "onboard":
        from .onboard import run_onboard_wizard

        cfg_dir = Path(args.config_dir) if args.config_dir else None
        run_onboard_wizard(
            config_dir=cfg_dir,
            non_interactive=args.non_interactive,
            defaults_only=args.defaults_only,
        )
        return 0

    repo_root = Path(__file__).resolve().parents[2]
    orch = HarnessOrchestrator(args.data_dir, default_workspace=repo_root)

    if args.cmd == "chat":
        from .tui import HexConsole

        console = HexConsole(
            orch,
            project_id=args.project,
            workspace_root=args.workspace,
            stream=not args.no_stream,
            model_profile=args.model,
        )
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

    if args.cmd == "submit":
        req = TaskSubmitRequest(
            goal=args.goal,
            project_id=args.project,
            execution_mode=args.mode,
            workspace_root=args.workspace or str(repo_root),
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

    return 0


if __name__ == "__main__":
    sys.exit(main())
