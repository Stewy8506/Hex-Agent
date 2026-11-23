"""Client-side P2P signaling: message router + live WebSocket link to hex-backend."""

from __future__ import annotations

import json
import threading
import urllib.parse
from dataclasses import dataclass
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


def _ws_url(base_url: str) -> str:
    if base_url.startswith("https://"):
        return "wss://" + base_url[len("https://"):]
    if base_url.startswith("http://"):
        return "ws://" + base_url[len("http://"):]
    return base_url


def _default_connect(url: str) -> Any:
    from websockets.sync.client import connect

    # __enter__ is invoked manually: SignalLink owns the lifecycle (close on
    # stop/reconnect), and entering marks the connection as context-managed,
    # suppressing websockets' direct-use DeprecationWarning.
    return connect(url, open_timeout=10, close_timeout=2).__enter__()


class SignalLink:
    """Background WebSocket link to hex-backend /v1/signal with auto-reconnect.

    Incoming server events are dispatched into the owned P2PSignalingClient;
    outbound frames are serialized across threads. Fully offline-safe: the
    runner thread never raises into caller code and retries with backoff until
    stopped, re-reading fresh tokens from ``token_provider`` on every attempt.
    """

    def __init__(
        self,
        base_url: str,
        signaling: P2PSignalingClient,
        token_provider: Callable[[], str | None],
        device_id_provider: Callable[[], str | None] | None = None,
        connect_factory: Callable[[str], Any] | None = None,
        reconnect_initial: float = 1.0,
        reconnect_max: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.signaling = signaling
        self._token_provider = token_provider
        self._device_id_provider = device_id_provider or (lambda: signaling.local_device_id)
        self._connect = connect_factory or _default_connect
        self._reconnect_initial = reconnect_initial
        self._reconnect_max = reconnect_max
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._sock: Any = None
        self._send_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self.connected = False
        self.attempts = 0
        self.last_error: str | None = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="hex-signal-link", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        self._close_current()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        with self._status_lock:
            self.connected = False

    def status(self) -> dict[str, Any]:
        with self._status_lock:
            return {
                "running": bool(self._thread and self._thread.is_alive()),
                "connected": self.connected,
                "attempts": self.attempts,
                "last_error": self.last_error,
                "base_url": self.base_url,
            }

    # -- outbound (thread-safe) -----------------------------------------------

    def send(self, payload: dict[str, Any]) -> bool:
        sock = self._sock
        if sock is None:
            return False
        try:
            with self._send_lock:
                sock.send(json.dumps(payload, separators=(",", ":")))
            return True
        except Exception:
            self._close_current()
            return False

    def send_turn_ack(self, project_session_id: str, turn_seq: int) -> bool:
        return self.send(P2PSignalingClient.build_turn_ack(project_session_id, turn_seq))

    def send_p2p_signal(self, target_device_id: str, payload: dict[str, Any]) -> bool:
        return self.send(
            {"type": "p2p_signal", "target_device_id": target_device_id, "payload": payload}
        )

    def request_failover(
        self, team_id: str, project_session_id: str, failed_device_id: str
    ) -> bool:
        return self.send(
            P2PSignalingClient.build_failover_request(team_id, project_session_id, failed_device_id)
        )

    # -- internals -------------------------------------------------------------

    def _set_status(self, connected: bool, error: str | None, attempts: int | None = None) -> None:
        with self._status_lock:
            self.connected = connected
            self.last_error = error
            if attempts is not None:
                self.attempts = attempts

    def _close_current(self) -> None:
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

    def _run(self) -> None:
        delay = self._reconnect_initial
        while not self._stop.is_set():
            token = self._token_provider()
            device_id = self._device_id_provider()
            if not token or not device_id:
                self._set_status(connected=False, error=None)
                if self._stop.wait(delay):
                    break
                delay = min(self._reconnect_max, delay * 2)
                continue

            query = urllib.parse.urlencode({"token": token, "device_id": device_id})
            url = f"{_ws_url(self.base_url)}/v1/signal?{query}"
            with self._status_lock:
                self.attempts += 1
            try:
                sock = self._connect(url)
            except Exception as exc:
                self._set_status(connected=False, error=str(exc))
                if self._stop.wait(delay):
                    break
                delay = min(self._reconnect_max, delay * 2)
                continue

            self._set_status(connected=True, error=None, attempts=0)
            self._sock = sock
            delay = self._reconnect_initial
            try:
                for raw in sock:
                    try:
                        event = json.loads(raw)
                    except ValueError:
                        continue
                    self.signaling.handle_server_event(event)
            except Exception as exc:
                self._set_status(connected=False, error=str(exc))
            finally:
                self._sock = None
                with self._status_lock:
                    self.connected = False
            if self._stop.is_set():
                break
            if self._stop.wait(delay):
                break
            delay = min(self._reconnect_max, delay * 2)
