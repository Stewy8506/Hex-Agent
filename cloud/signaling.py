"""Client-side P2P signaling message router for Harness <-> hex-backend."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class PeerInfo:
    device_record_id: str
    user_id: str
    device_pubkey: str
    host_eligible: bool = True


class P2PSignalingClient:
    """Tracks online sibling/team peers, revocations, kill switches, and host leases."""

    def __init__(self, local_device_id: str) -> None:
        self.local_device_id = local_device_id
        self.online_peers: dict[str, PeerInfo] = {}
        self.revoked: bool = False
        self.active_kill_switches: dict[str, dict[str, Any]] = {}
        self.latest_host_leases: dict[str, dict[str, Any]] = {}
        self._signal_handlers: list[Callable[[str, dict[str, Any]], None]] = []

    def on_p2p_signal(self, handler: Callable[[str, dict[str, Any]], None]) -> None:
        self._signal_handlers.append(handler)

    def handle_server_event(self, event: dict[str, Any]) -> None:
        """Process a JSON event received from hex-backend /v1/signal."""
        etype = event.get("type")
        if etype == "welcome":
            for p in event.get("online_peers", []):
                info = PeerInfo(
                    device_record_id=p["device_record_id"],
                    user_id=p["user_id"],
                    device_pubkey=p["device_pubkey"],
                    host_eligible=bool(p.get("host_eligible", True)),
                )
                self.online_peers[info.device_record_id] = info
        elif etype == "peer_online":
            info = PeerInfo(
                device_record_id=event["device_record_id"],
                user_id=event["user_id"],
                device_pubkey=event["device_pubkey"],
            )
            self.online_peers[info.device_record_id] = info
        elif etype in {"peer_offline", "peer_revoked"}:
            self.online_peers.pop(event.get("device_record_id", ""), None)
        elif etype == "device_revoked":
            if event.get("device_record_id") == self.local_device_id:
                self.revoked = True
                self.online_peers.clear()
        elif etype == "p2p_signal":
            sender = event.get("from_device_id", "")
            payload = event.get("payload", {})
            for handler in self._signal_handlers:
                handler(sender, payload)
        elif etype == "kill_switch_updated":
            ks = event.get("event", {})
            name = ks.get("switch")
            if name:
                if ks.get("active"):
                    self.active_kill_switches[name] = ks
                else:
                    self.active_kill_switches.pop(name, None)
        elif etype == "host_lease_updated":
            lease = event.get("lease", {})
            pid = lease.get("project_session_id")
            if pid:
                self.latest_host_leases[pid] = lease

    @staticmethod
    def build_turn_ack(project_session_id: str, turn_seq: int) -> dict[str, Any]:
        return {
            "type": "turn_ack",
            "project_session_id": project_session_id,
            "turn_seq": turn_seq,
        }

    @staticmethod
    def build_failover_request(
        team_id: str, project_session_id: str, failed_device_id: str
    ) -> dict[str, Any]:
        return {
            "type": "request_failover",
            "team_id": team_id,
            "project_session_id": project_session_id,
            "failed_device_id": failed_device_id,
        }
