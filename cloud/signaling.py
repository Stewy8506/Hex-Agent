"""Client-side P2P signaling: message router + live WebSocket link to hex-backend."""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable

from .team_mesh import sign_turn_ack


@dataclass
class PeerInfo:
    device_record_id: str
    user_id: str
    device_pubkey: str
    host_eligible: bool = True


class P2PSignalingClient:
    """Tracks online sibling/team peers, revocations, kill switches, and host leases."""

    def __init__(self, local_device_id: str, *, host_eligible: bool = True) -> None:
        self.local_device_id = local_device_id
        self.local_host_eligible = host_eligible
        self.online_peers: dict[str, PeerInfo] = {}
        self.revoked: bool = False
        self.active_kill_switches: dict[str, dict[str, Any]] = {}
        self.latest_host_leases: dict[str, dict[str, Any]] = {}
        self.local_verified_turn_seq: dict[str, int] = {}
        self._coordinator_heartbeats: dict[str, float] = {}
        self._clock = time.monotonic
        self._lock = threading.RLock()
        self._signal_handlers: list[Callable[[str, dict[str, Any]], None]] = []
        self._security_handlers: list[Callable[[dict[str, Any]], None]] = []

    def on_p2p_signal(self, handler: Callable[[str, dict[str, Any]], None]) -> None:
        with self._lock:
            self._signal_handlers.append(handler)

    def remove_p2p_signal_handler(self, handler: Callable[[str, dict[str, Any]], None]) -> None:
        with self._lock:
            self._signal_handlers = [item for item in self._signal_handlers if item != handler]

    def on_security_event(self, handler: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            self._security_handlers.append(handler)

    def remove_security_event_handler(self, handler: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            self._security_handlers = [item for item in self._security_handlers if item != handler]

    def peer_info(self, device_id: str) -> PeerInfo | None:
        with self._lock:
            peer = self.online_peers.get(device_id)
            if peer is None:
                return None
            return PeerInfo(
                device_record_id=peer.device_record_id,
                user_id=peer.user_id,
                device_pubkey=peer.device_pubkey,
                host_eligible=peer.host_eligible,
            )

    def _record_host_lease(self, lease: Any) -> None:
        if not isinstance(lease, dict):
            return
        project_session_id = lease.get("project_session_id")
        if not isinstance(project_session_id, str) or not project_session_id:
            return
        previous = self.latest_host_leases.get(project_session_id)
        if not previous or previous.get("host_epoch") != lease.get("host_epoch"):
            self._coordinator_heartbeats[project_session_id] = self._clock()
            self.local_verified_turn_seq.pop(project_session_id, None)
        self.latest_host_leases[project_session_id] = lease

    def coordinator_is_stale(
        self, project_session_id: str, stale_after_seconds: float, now: float | None = None
    ) -> bool:
        with self._lock:
            heartbeat = self._coordinator_heartbeats.get(project_session_id)
            return heartbeat is None or (self._clock() if now is None else now) - heartbeat > stale_after_seconds

    def note_turn_ack(self, project_session_id: str, host_epoch: int, turn_seq: int) -> bool:
        with self._lock:
            lease = self.latest_host_leases.get(project_session_id)
            if not lease or lease.get("host_epoch") != host_epoch or type(turn_seq) is not int or turn_seq < 1:
                return False
            self.local_verified_turn_seq[project_session_id] = max(
                turn_seq, self.local_verified_turn_seq.get(project_session_id, 0)
            )
            return True

    def security_blocked(self, kill_switch_names: set[str]) -> bool:
        with self._lock:
            return self.revoked or any(name in self.active_kill_switches for name in kill_switch_names)

    def handle_server_event(self, event: dict[str, Any]) -> None:
        """Process a JSON event received from hex-backend /v1/signal."""
        if not isinstance(event, dict):
            return
        etype = event.get("type")
        security_event = etype in {"peer_offline", "peer_revoked", "device_revoked", "kill_switch_updated"}
        with self._lock:
            if etype == "welcome":
                self.online_peers.clear()
                for p in event.get("online_peers", []):
                    info = PeerInfo(
                        device_record_id=p["device_record_id"],
                        user_id=p["user_id"],
                        device_pubkey=p["device_pubkey"],
                        host_eligible=bool(p.get("host_eligible", True)),
                    )
                    self.online_peers[info.device_record_id] = info
                leases = event.get("host_leases", [])
                if isinstance(leases, list):
                    for lease in leases:
                        self._record_host_lease(lease)
            elif etype == "peer_online":
                info = PeerInfo(
                    device_record_id=event["device_record_id"],
                    user_id=event["user_id"],
                    device_pubkey=event["device_pubkey"],
                    host_eligible=bool(event.get("host_eligible", True)),
                )
                self.online_peers[info.device_record_id] = info
            elif etype in {"peer_offline", "peer_revoked"}:
                self.online_peers.pop(event.get("device_record_id", ""), None)
            elif etype == "device_revoked":
                if event.get("device_record_id") == self.local_device_id:
                    self.revoked = True
                    self.online_peers.clear()
            elif etype == "kill_switch_updated":
                ks = event.get("event", {})
                if not isinstance(ks, dict):
                    return
                name = ks.get("switch")
                if isinstance(name, str) and name:
                    if ks.get("active"):
                        self.active_kill_switches[name] = ks
                    else:
                        self.active_kill_switches.pop(name, None)
            elif etype in {"host_lease_updated", "failover_complete"}:
                self._record_host_lease(event.get("lease"))
            elif etype == "coordinator_heartbeat":
                pid = event.get("project_session_id")
                lease = self.latest_host_leases.get(pid) if isinstance(pid, str) else None
                if (
                    lease
                    and lease.get("host_epoch") == event.get("host_epoch")
                    and lease.get("primary_device_record_id") == event.get("device_record_id")
                ):
                    self._coordinator_heartbeats[pid] = self._clock()
            elif etype == "turn_ack_result" and event.get("accepted"):
                self.note_turn_ack(
                    event.get("project_session_id", ""),
                    event.get("host_epoch", 0),
                    event.get("turn_seq", 0),
                )
            signal_handlers = tuple(self._signal_handlers)
            security_handlers = tuple(self._security_handlers)

        if etype == "p2p_signal":
            sender = event.get("from_device_id", "")
            payload = event.get("payload", {})
            if not isinstance(sender, str) or not isinstance(payload, dict):
                return
            for handler in signal_handlers:
                try:
                    handler(sender, payload)
                except Exception:
                    continue
        if security_event:
            for handler in security_handlers:
                try:
                    handler(event)
                except Exception:
                    continue

    @staticmethod
    def build_turn_ack(
        signer: Any,
        project_session_id: str,
        host_epoch: int,
        turn_seq: int,
        bundle_digest: str,
    ) -> dict[str, Any]:
        return sign_turn_ack(
            signer, project_session_id, host_epoch, turn_seq, bundle_digest
        )

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
        ack_signer: Any = None,
        heartbeat_interval_seconds: float = 3.0,
        coordinator_stale_seconds: float = 10.0,
        reconnect_initial: float = 1.0,
        reconnect_max: float = 30.0,
    ) -> None:
        if heartbeat_interval_seconds <= 0 or coordinator_stale_seconds <= heartbeat_interval_seconds:
            raise ValueError("Heartbeat timing must be positive with stale timeout above the interval.")
        self.base_url = base_url.rstrip("/")
        self.signaling = signaling
        self._token_provider = token_provider
        self._device_id_provider = device_id_provider or (lambda: signaling.local_device_id)
        self._connect = connect_factory or _default_connect
        self._ack_signer = ack_signer
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._coordinator_stale_seconds = coordinator_stale_seconds
        self._failover_attempts: set[tuple[str, int, str]] = set()
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
        self._heartbeat_thread = threading.Thread(
            target=self._supervise_coordinators, name="hex-team-heartbeat", daemon=True
        )
        self._thread.start()
        self._heartbeat_thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        self._close_current()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        heartbeat_thread = self._heartbeat_thread
        if heartbeat_thread and heartbeat_thread.is_alive() and heartbeat_thread is not threading.current_thread():
            heartbeat_thread.join(timeout=timeout)
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

    def send_turn_ack(
        self,
        project_session_id: str,
        host_epoch: int,
        turn_seq: int,
        bundle_digest: str,
        signer: Any = None,
    ) -> bool:
        signer = signer or self._ack_signer
        if signer is None:
            return False
        ack = P2PSignalingClient.build_turn_ack(
            signer, project_session_id, host_epoch, turn_seq, bundle_digest
        )
        return self.send({"type": "turn_ack", "ack": ack})

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

    def _supervise_coordinators(self) -> None:
        while not self._stop.wait(self._heartbeat_interval_seconds):
            self._supervise_coordinators_once()

    def _supervise_coordinators_once(self) -> None:
        with self.signaling._lock:
            leases = list(self.signaling.latest_host_leases.items())
            local_eligible = self.signaling.local_host_eligible
            sequences = dict(self.signaling.local_verified_turn_seq)
        for project_session_id, lease in leases:
            if not isinstance(lease, dict):
                continue
            coordinator = lease.get("primary_device_record_id")
            epoch = lease.get("host_epoch")
            if type(epoch) is not int or epoch < 1:
                continue
            if coordinator == self.signaling.local_device_id:
                self.send({
                    "type": "coordinator_heartbeat",
                    "project_session_id": project_session_id,
                    "host_epoch": epoch,
                    "turn_seq": sequences.get(project_session_id, 0),
                })
            elif local_eligible and self.signaling.coordinator_is_stale(
                project_session_id, self._coordinator_stale_seconds
            ):
                team_id = lease.get("team_id")
                if not isinstance(team_id, str) or not team_id or not isinstance(coordinator, str):
                    continue
                attempt = (project_session_id, epoch, coordinator)
                if attempt in self._failover_attempts:
                    continue
                if self.request_failover(team_id, project_session_id, coordinator):
                    self._failover_attempts.add(attempt)

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
