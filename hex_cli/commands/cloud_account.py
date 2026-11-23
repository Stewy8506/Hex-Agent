"""`hex cloud` command: manage the HEX cloud account, device enrollment, and backend connection."""

from __future__ import annotations

import argparse
import getpass
import json as _json
import sys
from typing import Any


def _prompt_password(args: argparse.Namespace) -> str:
    if getattr(args, "password", None):
        return str(args.password)
    return getpass.getpass("Password: ")


def _require(value: Any, flag: str) -> str:
    if not value:
        print(f"error: {flag} is required for this operation", file=sys.stderr)
        raise SystemExit(2)
    return str(value)


def _print_status(status: dict[str, Any]) -> None:
    print(f"Cloud backend : {status['base_url']}")
    if status.get("logged_in"):
        print(f"Signed in     : yes ({status.get('email')})")
        print(f"User          : {status.get('user_id')}")
    else:
        print("Signed in     : no")
    if status.get("device_record_id"):
        print(f"Device record : {status['device_record_id']}")
    else:
        print("Device record : not enrolled")
    if status.get("platform"):
        print(f"Platform      : {status['platform']}")


def _print_devices(devices: list[dict[str, Any]]) -> None:
    if not devices:
        print("No devices enrolled.")
        return
    for device in devices:
        flags = []
        if device.get("trust_state") != "trusted":
            flags.append(str(device.get("trust_state")))
        if device.get("host_eligible"):
            flags.append("host-eligible")
        suffix = f" [{', '.join(flags)}]" if flags else ""
        print(
            f"{device.get('device_record_id')}  {device.get('platform')}"
            f"  (added {device.get('created_at')}){suffix}"
        )


def run_cloud_command(args: argparse.Namespace) -> int:
    from cloud.session import CloudAccountError, CloudAccountManager

    manager = CloudAccountManager(base_url=getattr(args, "base_url", None))
    cmd = getattr(args, "cloud_cmd", None) or "status"
    try:
        if cmd == "status":
            result: Any = manager.status()
        elif cmd == "register":
            email = _require(getattr(args, "email", None), "--email")
            manager.register(email, _prompt_password(args))
            result = {"registered": email, "next": "hex cloud login"}
        elif cmd == "login":
            email = _require(getattr(args, "email", None), "--email")
            result = manager.login(email, _prompt_password(args))
        elif cmd == "logout":
            result = manager.logout()
        elif cmd == "devices":
            result = {"devices": manager.list_devices(
                include_revoked=bool(getattr(args, "include_revoked", False))
            )}
        elif cmd == "revoke":
            result = manager.revoke_device(
                _require(getattr(args, "device_record_id", None), "--device")
            )
        else:
            print(f"Unknown cloud command: {cmd}", file=sys.stderr)
            return 1
    except CloudAccountError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if getattr(args, "json", False):
        print(_json.dumps(result, indent=2, default=str))
        return 0

    if cmd in ("status", "login", "logout"):
        _print_status(result)
    elif cmd == "devices":
        _print_devices(result.get("devices", []))
    elif cmd == "register":
        print(f"Registered {result['registered']}. Next: hex cloud login")
    elif cmd == "revoke":
        print(f"Revoked {result.get('device_record_id')}")
    return 0
