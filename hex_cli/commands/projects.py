from __future__ import annotations

import json
import sys
from argparse import Namespace
from pathlib import Path


def run_project_command(args: Namespace) -> int:
    from harness_core.storage.project_registry import ProjectRegistry

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
