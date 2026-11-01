"""CLI Entrypoint (`harness`) for Owner Workflows and Debugging (PRD Sections 5, 8.1, 15).

Commands:
- `harness init`: Initialize local database, CoW snapshot store, and seed default repo skills.
- `harness up`: Start the local web control surface & HTTP API on 127.0.0.1:8420.
- `harness submit <goal>`: Submit and execute a task (`--fast-path` or `--standard`).
- `harness status [task_id]`: Inspect task state, call journal, and Proof-Carrying Artifact bundle.
- `harness scan-skill <dir>`: Run NVIDIA SkillSpector static + OSV + semantic scan on a SKILL.md bundle.
- `harness harness2-demo`: Run a live Propose-Probe-Compose cycle through the gated `vendor/harness2` adapter.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .models import TaskSubmitRequest
from .orchestrator import HarnessOrchestrator
from .server import run_http_server


DEFAULT_DATA_DIR = Path.home() / ".autonomous_harness"


def seed_default_skills(orch: HarnessOrchestrator, repo_root: Path) -> list[dict]:
    skills_root = repo_root / "skills"
    results = []
    if skills_root.is_dir():
        for sub in sorted(skills_root.iterdir()):
            if (sub / "SKILL.md").is_file():
                results.append(orch.scanner.scan_and_register_skill(sub))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="harness", description="Custom Autonomous AI Harness CLI")
    parser.add_argument(
        "--data-dir",
        default=str(Path(__file__).resolve().parents[2] / ".harness_data"),
        help="Path to local harness ledger and CoW storage directory",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Initialize ledger schema, CoW workspace store, and scan built-in skills")

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

    args = parser.parse_args(argv)
    repo_root = Path(__file__).resolve().parents[2]
    orch = HarnessOrchestrator(args.data_dir, default_workspace=repo_root)

    if args.cmd == "init":
        scanned = seed_default_skills(orch, repo_root)
        print(
            json.dumps(
                {
                    "status": "initialized",
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

    return 0


if __name__ == "__main__":
    sys.exit(main())
