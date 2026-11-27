"""`hex sync` command: inspect sync state or run an encrypted peer exchange."""

from __future__ import annotations

import argparse
import json as _json
import sys
from pathlib import Path
from typing import Any


def _print_status(result: dict[str, Any]) -> None:
    print(f"Workspace      : {result['workspace_id']}")
    print(f"Device         : {result['device_id']}")
    print(f"Signed in      : {'yes' if result['signed_in'] else 'no'}")
    print(f"Capabilities   : {', '.join(result['capabilities'])}")
    print(f"Transports     : {', '.join(result['transports'])}")
    peers = result.get("peers") or {}
    if not peers:
        print("Peer cursors   : none")
        return
    print("Peer cursors:")
    for peer_id, state in peers.items():
        tasks = len(state.get("ledger", {}))
        cognition = state.get("cognition") or {}
        print(
            f"  {peer_id}: {tasks} ledger task(s), cognition revision "
            f"{cognition.get('revision', 0)}"
        )


def _print_result(result: dict[str, Any]) -> None:
    print(f"Sync complete  : {result['remote_device_id']} ({result['transport']})")
    print(
        f"Ledger         : {result['ledger_events_sent']} sent, "
        f"{result['ledger_events_received']} received"
    )
    print(
        f"Cognition      : sent {'yes' if result['cognition_sent'] else 'no'}, "
        f"received {'yes' if result['cognition_received'] else 'no'}"
    )
    if result.get("conflicts"):
        print(f"Conflicts      : {len(result['conflicts'])} (preserved for review)")


def run_sync_command(args: argparse.Namespace) -> int:
    from cloud.p2p_transport import P2PTransportError
    from cloud.session import CloudAccountError, CloudAccountManager
    from cloud.sync_control import SyncControl
    from cloud.sync_engine import SyncEngineError

    account = CloudAccountManager(base_url=getattr(args, "base_url", None))
    workspace = Path(getattr(args, "workspace", None) or Path.cwd()).resolve()
    data_dir = Path(getattr(args, "data_dir", None) or Path.home() / ".hex" / "data")
    config_dir = Path(getattr(args, "config_dir", None) or Path.home() / ".hex")
    try:
        with SyncControl(
            workspace,
            data_dir=data_dir,
            global_home=config_dir,
            account=account,
            timeout=float(getattr(args, "timeout", 120.0)),
        ) as control:
            if getattr(args, "sync_cmd", "status") == "run":
                import asyncio

                peer_id = str(getattr(args, "peer_id", None) or "")
                peer_key = str(getattr(args, "peer_key", None) or "")
                if not peer_id or not peer_key:
                    raise ValueError("--peer-id and --peer-key are required.")
                result: Any = asyncio.run(control.run_websocket(
                    str(args.url), peer_id, peer_key
                ))
            else:
                result = control.status()
    except (CloudAccountError, P2PTransportError, SyncEngineError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if getattr(args, "json", False):
        print(_json.dumps(result, indent=2, default=str))
    elif getattr(args, "sync_cmd", "status") == "run":
        _print_result(result)
    else:
        _print_status(result)
    return 0
