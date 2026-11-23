"""`hex update` command: signed update checks against hex-backend."""

from __future__ import annotations

import argparse
import json as _json
import sys
from typing import Any


def _print_updates(updates: list[dict[str, Any]]) -> None:
    for upd in updates:
        mandatory = "MANDATORY" if upd.get("mandatory") else "optional"
        print(f"  {upd.get('component')}  {upd.get('installed_version')} -> {upd.get('target_version')}  ({mandatory})")
        notes = upd.get("release_notes")
        if notes:
            print(f"    {notes}")
        artifact = upd.get("artifact") or {}
        if artifact.get("path"):
            print(f"    artifact: {artifact['path']}")


def _print_rollbacks(rollbacks: list[dict[str, Any]]) -> None:
    for rb in rollbacks:
        print(
            f"  {rb.get('component')}  {rb.get('installed_version')} -> {rb.get('rollback_to')}"
            f"  ({rb.get('reason')})"
        )


def run_update_command(args: argparse.Namespace) -> int:
    from cloud.session import CloudAccountError, CloudAccountManager

    manager = CloudAccountManager(base_url=getattr(args, "base_url", None))
    channel = getattr(args, "channel", None)
    try:
        if getattr(args, "manifest", False):
            result: Any = manager.release_manifest(channel or "stable")
        else:
            result = manager.check_updates(channel=channel)
    except CloudAccountError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if getattr(args, "json", False):
        print(_json.dumps(result, indent=2, default=str))
        return 0

    if getattr(args, "manifest", False):
        components = result.get("components", {})
        print(f"Channel        : {result.get('channel')}")
        print(f"Manifest ver   : {result.get('manifest_version')} (published {result.get('published_at')})")
        for name, spec in components.items():
            print(
                f"  {name}  latest {spec.get('latest_version')}"
                f"  min {spec.get('min_supported_version')}"
                f"  rollout {spec.get('rollout_percentage')}%"
            )
        return 0

    if not result.get("apply_allowed"):
        print("Update check   : INCOMPATIBLE")
        for issue in result.get("compatibility_issues", []):
            print(
                f"  conflict: {issue.get('component')} requires"
                f" {issue.get('requires_component')} {issue.get('constraint')}"
                f" (projected {issue.get('projected_version')})"
            )
    else:
        mandatory = result.get("mandatory_upgrade_required")
        print(
            f"Update check   : compatible"
            f" (mandatory upgrade: {'yes' if mandatory else 'no'}, channel {result.get('channel')})"
        )

    updates = result.get("updates") or []
    rollbacks = result.get("rollbacks") or []
    if updates:
        print("Updates:")
        _print_updates(updates)
    if rollbacks:
        print("Rollbacks (yanked installed versions):")
        _print_rollbacks(rollbacks)
    if not updates and not rollbacks:
        print("No updates available — up to date.")
    return 0
