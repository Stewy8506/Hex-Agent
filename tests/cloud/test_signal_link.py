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
        self.assertFalse(link.send_turn_ack("proj_1", 3))
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
        link.start()
        self.addCleanup(link.stop)
        self.assertTrue(_poll(lambda: link.status()["connected"]))
        self.assertTrue(link.send_turn_ack("proj_1", 7))
        frame = json.loads(sock.sent[0])
        self.assertEqual(frame, {
            "type": "turn_ack",
            "project_session_id": "proj_1",
            "turn_seq": 7,
        })


if __name__ == "__main__":
    unittest.main()
