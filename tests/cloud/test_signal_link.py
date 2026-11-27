"""Unit tests: SignalLink WebSocket runner against a fake connect factory."""

from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from cloud.p2p_transport import PeerIdentity
from cloud.signaling import P2PSignalingClient, SignalLink


class FakeSocket:
    """Minimal stand-in for a websockets.sync connection."""

    def __init__(self, incoming: list[str]) -> None:
        self.incoming = list(incoming)
        self.sent: list[str] = []
        self.closed = threading.Event()
        self._index = 0
        self._lock = threading.Lock()

    def __iter__(self) -> "FakeSocket":
        return self

    def __next__(self) -> str:
        while True:
            if self.closed.is_set():
                raise ConnectionError("socket closed")
            with self._lock:
                if self._index < len(self.incoming):
                    message = self.incoming[self._index]
                    self._index += 1
                    return message
            time.sleep(0.01)

    def send(self, raw: str) -> None:
        self.sent.append(raw)

    def close(self) -> None:
        self.closed.set()


def _poll(predicate, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class SignalLinkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.signaling = P2PSignalingClient("dev_local")
        self.factories: list = []

    def _make_link(self, incoming: list[str], token: str | None = "tok") -> tuple[SignalLink, FakeSocket]:
        sock = FakeSocket(incoming)
        created: list[FakeSocket] = []

        def factory(url: str) -> FakeSocket:
            created.append(url)
            return sock

        link = SignalLink(
            "http://127.0.0.1:8787",
            self.signaling,
            token_provider=lambda: token,
            connect_factory=factory,
            reconnect_initial=0.01,
            reconnect_max=0.05,
        )
        return link, sock

    def test_connects_and_routes_server_events(self) -> None:
        welcome = json.dumps({
            "type": "welcome",
            "online_peers": [{
                "device_record_id": "dev_b",
                "user_id": "user_2",
                "device_pubkey": "ed25519:aa",
                "host_eligible": True,
            }],
        })
        link, sock = self._make_link([welcome])
        link.start()
        self.addCleanup(link.stop)

        self.assertTrue(_poll(lambda: self.signaling.online_peers.get("dev_b") is not None))
        status = link.status()
        self.assertTrue(status["connected"])
        self.assertTrue(sock.sent == [])  # nothing sent unless we send

        self.assertTrue(link.send_p2p_signal("dev_b", {"hello": 1}))
        self.assertEqual(json.loads(sock.sent[0])["type"], "p2p_signal")

    def test_url_carries_token_and_device(self) -> None:
        urls: list[str] = []

        def factory(url: str) -> FakeSocket:
            urls.append(url)
            sock = FakeSocket([])
            sock.closed.set()  # immediately closes: exercises reconnect path
            return sock

        link = SignalLink(
            "http://127.0.0.1:8787",
            self.signaling,
            token_provider=lambda: "secret-token",
            connect_factory=factory,
            reconnect_initial=0.01,
            reconnect_max=0.02,
        )
        link.start()
        self.addCleanup(link.stop)

        self.assertTrue(_poll(lambda: bool(urls)))
        self.assertIn("/v1/signal?", urls[0])
        self.assertIn("token=secret-token", urls[0])
        self.assertIn("device_id=dev_local", urls[0])

    def test_waits_without_token(self) -> None:
        link, sock = self._make_link([], token=None)
        link.start()
        self.addCleanup(link.stop)
        time.sleep(0.2)
        self.assertFalse(link.status()["connected"])
        self.assertIsNone(self.signaling.online_peers.get("dev_b"))

    def test_send_returns_false_when_disconnected(self) -> None:
        link, _sock = self._make_link([])
        signer = PeerIdentity.generate("dev_local")
        self.assertFalse(link.send_turn_ack("proj_1", 1, 3, "a" * 64, signer))
        self.assertFalse(link.request_failover("team_1", "proj_1", "dev_b"))

    def test_stop_terminates_runner(self) -> None:
        link, _sock = self._make_link([])
        link.start()
        self.assertTrue(_poll(lambda: link.status()["running"]))
        link.stop()
        self.assertFalse(link.status()["running"])
        self.assertFalse(link.status()["connected"])

    def test_turn_ack_frame_shape(self) -> None:
        link, sock = self._make_link([])
        signer = PeerIdentity.generate("dev_local")
        link.start()
        self.addCleanup(link.stop)
        self.assertTrue(_poll(lambda: link.status()["connected"]))
        self.assertTrue(link.send_turn_ack("proj_1", 1, 7, "a" * 64, signer))
        frame = json.loads(sock.sent[0])
        self.assertEqual(frame["type"], "turn_ack")
        self.assertEqual(frame["project_session_id"], "proj_1")
        self.assertEqual(frame["host_epoch"], 1)
        self.assertEqual(frame["turn_seq"], 7)
        self.assertEqual(frame["bundle_digest"], "a" * 64)
        self.assertTrue(frame["signature"].startswith("ed25519:"))

    def test_observer_does_not_request_failover(self) -> None:
        self.signaling.local_host_eligible = False
        self.signaling.handle_server_event({
            "type": "host_lease_updated",
            "lease": {
                "project_session_id": "proj_1", "team_id": "team_1", "host_epoch": 1,
                "primary_device_record_id": "dev_remote", "last_committed_turn_seq": 0,
            },
        })
        link, sock = self._make_link([])
        link.start()
        self.addCleanup(link.stop)
        self.assertTrue(_poll(lambda: link.status()["connected"]))
        link._supervise_coordinators_once()
        self.assertEqual(sock.sent, [])

    def test_heartbeat_and_stale_coordinator_failover(self) -> None:
        link, sock = self._make_link([])
        now = [100.0]
        self.signaling._clock = lambda: now[0]
        self.signaling.handle_server_event({
            "type": "welcome",
            "online_peers": [],
            "host_leases": [{
                "project_session_id": "proj_1",
                "team_id": "team_1",
                "host_epoch": 1,
                "primary_device_record_id": "dev_remote",
                "last_committed_turn_seq": 4,
            }],
        })
        link.start()
        self.addCleanup(link.stop)
        self.assertTrue(_poll(lambda: link.status()["connected"]))

        now[0] = 110.1
        link._supervise_coordinators_once()
        link._supervise_coordinators_once()
        failovers = [json.loads(raw) for raw in sock.sent if json.loads(raw)["type"] == "request_failover"]
        self.assertEqual(len(failovers), 1)
        self.assertEqual(failovers[0]["failed_device_id"], "dev_remote")

        self.signaling.handle_server_event({
            "type": "host_lease_updated",
            "lease": {
                "project_session_id": "proj_1",
                "team_id": "team_1",
                "host_epoch": 2,
                "primary_device_record_id": "dev_local",
                "last_committed_turn_seq": 4,
            },
        })
        self.signaling.handle_server_event({
            "type": "turn_ack_result", "accepted": True,
            "project_session_id": "proj_1", "host_epoch": 2, "turn_seq": 4,
        })
        link._supervise_coordinators_once()
        heartbeats = [json.loads(raw) for raw in sock.sent if json.loads(raw)["type"] == "coordinator_heartbeat"]
        self.assertEqual(heartbeats[-1]["host_epoch"], 2)
        self.assertEqual(heartbeats[-1]["turn_seq"], 4)

        self.signaling.handle_server_event({
            "type": "coordinator_heartbeat",
            "project_session_id": "proj_1",
            "host_epoch": 1,
            "device_record_id": "dev_remote",
        })
        self.assertFalse(self.signaling.coordinator_is_stale("proj_1", 10.0, now=now[0]))
        now[0] = 120.2
        self.assertTrue(self.signaling.coordinator_is_stale("proj_1", 10.0, now=now[0]))


if __name__ == "__main__":
    unittest.main()
