"""CLI Entrypoint (`hex`) for Owner Workflows and Debugging (PRD Sections 5, 8.1, 15).

Commands:
- `hex onboard`: Run interactive onboarding wizard for hardware discovery, model profile, and configuration.
- `hex init`: Initialize local database, CoW snapshot store, and seed default repo skills.
- `hex up`: Start the local web control surface & HTTP API on 127.0.0.1:8420.
- `hex run` / `hex exec <goal>`: Run task with automated Git branching, commits, and GitHub Pull Requests.
- `hex submit <goal>`: Submit and execute a task (`--fast-path` or `--standard`).
- `hex status [task_id]`: Inspect task state, call journal, and Proof-Carrying Artifact bundle.
- `hex scan-skill <dir>`: Run NVIDIA SkillSpector static + OSV + semantic scan on a SKILL.md bundle.
- `hex harness2-demo`: Run a live Propose-Probe-Compose cycle through the gated `vendor/harness2` adapter.
- `hex worker`: Start standalone distributed worker daemon.
- `hex chat` / `hex tui`: Start interactive TUI pair-programming console with Hermes-style splash & skin engine.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

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

    p_run = sub.add_parser("run", aliases=["exec"], help="Run and execute a task with optional auto-branching and pull request creation")
    p_run.add_argument("goal", help="Task goal description")
    p_run.add_argument("--project", default="default")
    p_run.add_argument("--mode", choices=["auto", "fast_path", "standard"], default="auto")
    p_run.add_argument("--workspace", default=None)
    p_run.add_argument("--branch", nargs="?", const=True, default=None, help="Custom branch name or flag to auto-branch (hex/task-<id>-<slug>)")
    p_run.add_argument("--pr", action="store_true", help="Automatically create GitHub Pull Request on success")
    p_run.add_argument("--base-branch", default="main", help="Base branch for Pull Request (default: main)")
    p_run.add_argument("--dry-run", action="store_true", help="Dry-run mode: do not create real git commits or remote PRs")

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

    if args.cmd in ("chat", "tui"):
        from .tui import HexConsole

        console = HexConsole(
            orch,
            project_id=args.project,
            workspace_root=args.workspace,
            stream=not args.no_stream,
            model_profile=args.model,
            skin_name=getattr(args, "skin", "amber"),
            quiet=getattr(args, "quiet", False),
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

    if args.cmd in ("run", "exec"):
        from .git_autopilot import GitAutopilot, GitAutopilotConfig

        ws_root = Path(args.workspace).resolve() if getattr(args, "workspace", None) else repo_root
        req = TaskSubmitRequest(
            goal=args.goal,
            project_id=args.project,
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

    return 0


if __name__ == "__main__":
    sys.exit(main())
