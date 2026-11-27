"""`hex team` commands for RBAC, project sessions, leases, and failover."""

from __future__ import annotations

import argparse
import json as _json
import sys
from typing import Any


def _require(value: Any, flag: str) -> str:
    if not value:
        raise ValueError(f"{flag} is required for this operation")
    return str(value)


def run_team_command(args: argparse.Namespace) -> int:
    from cloud.session import CloudAccountError, CloudAccountManager

    manager = CloudAccountManager(base_url=getattr(args, "base_url", None))
    command = getattr(args, "team_cmd", None) or "list"
    try:
        if command == "list":
            result: Any = {"teams": manager.list_teams()}
        elif command == "create":
            result = manager.create_team(_require(getattr(args, "name", None), "--name"))
        elif command == "invite":
            result = manager.create_team_invite(
                _require(getattr(args, "team", None), "--team"),
                _require(getattr(args, "email", None), "--email"),
                getattr(args, "role", "developer"),
            )
        elif command == "accept":
            result = manager.accept_team_invite(
                _require(getattr(args, "token", None), "--token")
            )
        elif command == "members":
            result = {
                "members": manager.list_team_members(
                    _require(getattr(args, "team", None), "--team")
                )
            }
        elif command == "role":
            result = manager.update_team_member_role(
                _require(getattr(args, "team", None), "--team"),
                _require(getattr(args, "user", None), "--user"),
                getattr(args, "role", "developer"),
            )
        elif command == "sessions":
            result = {
                "sessions": manager.list_project_sessions(
                    _require(getattr(args, "team", None), "--team")
                )
            }
        elif command == "session-create":
            result = manager.create_project_session(
                _require(getattr(args, "team", None), "--team"),
                _require(getattr(args, "name", None), "--name"),
                _require(getattr(args, "device", None), "--device"),
                getattr(args, "repo_fingerprint", "") or "",
            )
        elif command == "lease":
            result = manager.get_session_lease(
                _require(getattr(args, "session", None), "--session")
            )
        elif command == "failover":
            failed = getattr(args, "failed_device", None)
            target = getattr(args, "target_device", None)
            if bool(failed) == bool(target):
                raise ValueError("Specify exactly one of --failed-device or --target-device.")
            result = manager.failover_project_session(
                _require(getattr(args, "session", None), "--session"),
                failed_device_id=failed,
                target_device_id=target,
            )
        else:
            raise ValueError(f"Unknown team command: {command}")
    except (CloudAccountError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2 if isinstance(exc, ValueError) else 1

    if getattr(args, "json", False):
        print(_json.dumps(result, indent=2, default=str))
        return 0

    if command == "list":
        teams = result["teams"]
        if not teams:
            print("No teams.")
        for team in teams:
            print(f"{team['team_id']}  {team['name']}  ({team.get('role', 'member')})")
    elif command == "invite":
        print(f"Team: {result['team_id']}")
        print(f"Email: {result['invited_email']} ({result['role']})")
        print(f"Invite token: {result['invite_token']}")
    elif command == "accept":
        print(f"Joined {result['team_id']} as {result['role']}.")
    elif command == "members":
        for member in result["members"]:
            print(f"{member['user_id']}  {member['role']}")
    elif command == "sessions":
        for entry in result["sessions"]:
            session = entry["session"]
            lease = entry.get("signed_lease") or {}
            payload = lease.get("payload") or {}
            epoch = payload.get("host_epoch", "-")
            print(f"{session['project_session_id']}  {session['name']}  epoch {epoch}")
    elif command in {"lease", "failover", "session-create"}:
        print(_json.dumps(result, indent=2, default=str))
    else:
        print(_json.dumps(result, indent=2, default=str))
    return 0
