"""Signal-driven transport negotiation and security shutdown regressions."""

from __future__ import annotations

import asyncio
import threading
import unittest
from typing import Any
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cloud.p2p_transport import (
    P2PConnectionClosed,
    P2PHandshakeError,
    P2PTransportError,
    PeerAddress,
    PeerIdentity,
)
from cloud.relay_transport import RelayPeerServer
from cloud.signaling import P2PSignalingClient
from cloud.transport_broker import NegotiationError, TransportBroker, _PROTOCOL


def _identity(device_id: str) -> tuple[PeerIdentity, str]:
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ).hex()
    return PeerIdentity(device_id, key), "ed25519:" + public


class SignalBus:
    def __init__(self, clients: dict[str, P2PSignalingClient]) -> None:
        self.clients = clients
        self.messages: list[tuple[str, str, dict[str, Any]]] = []

    def send(self, sender: str, target: str, payload: dict[str, Any]) -> bool:
        self.messages.append((sender, target, payload))
        self.clients[target].handle_server_event({
            "type": "p2p_signal",
            "from_device_id": sender,
            "payload": payload,
        })
        return True


class TransportBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.identity_a, self.key_a = _identity("dev_a")
        self.identity_b, self.key_b = _identity("dev_b")
        self.peer_a = PeerAddress("dev_a", self.key_a)
        self.peer_b = PeerAddress("dev_b", self.key_b)
        self.signaling = {
            "dev_a": P2PSignalingClient("dev_a"),
            "dev_b": P2PSignalingClient("dev_b"),
        }
        self.signaling["dev_a"].handle_server_event({
            "type": "welcome",
            "online_peers": [{
                "device_record_id": "dev_b",
                "user_id": "user_1",
                "device_pubkey": self.key_b,
                "host_eligible": True,
            }],
        })
        self.signaling["dev_b"].handle_server_event({
            "type": "welcome",
            "online_peers": [{
                "device_record_id": "dev_a",
                "user_id": "user_1",
                "device_pubkey": self.key_a,
                "host_eligible": True,
            }],
        })
        self.bus = SignalBus(self.signaling)
        self.relay = RelayPeerServer(join_timeout=5, pair_timeout=10, idle_timeout=30)
        await self.relay.start()
        self.responder_calls: list[PeerAddress] = []
        self.responder_started = threading.Event()
        self.release_responder = threading.Event()
        self.hold_responder = False
        self.brokers: list[TransportBroker] = []
        self.broker_a = self._broker("dev_a", self.identity_a, None)
        self.broker_b = self._broker("dev_b", self.identity_b, self._responder)
        self.brokers.extend([self.broker_a, self.broker_b])

    async def asyncTearDown(self) -> None:
        for broker in reversed(self.brokers):
            broker.stop()
        await self.relay.close()

    def _broker(
        self,
        device_id: str,
        identity: PeerIdentity,
        handler: Any,
    ) -> TransportBroker:
        broker = TransportBroker(
            self.signaling[device_id],
            lambda target, payload: self.bus.send(device_id, target, payload),
            identity,
            relay_uri=self.relay.uri,
            session_handler=handler,
            negotiation_timeout=5,
        )
        broker.start()
        return broker

    async def _responder(self, session: Any, peer: PeerAddress) -> None:
        self.responder_calls.append(peer)
        payload = await session.receive()
        self.responder_started.set()
        if self.hold_responder:
            while not self.release_responder.is_set():
                await asyncio.sleep(0.01)
            return
        await session.send(b"reply:" + payload)

    async def _exchange(self, transport: str) -> bytes:
        async def operation(session: Any) -> bytes:
            await session.send(b"hello")
            return await session.receive()

        return await asyncio.wait_for(
            self.broker_a.run_with_peer_async(self.peer_b, operation, transport=transport, timeout=5),
            timeout=10,
        )

    async def _wait_for(self, predicate: Any, timeout: float = 3.0) -> bool:
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.01)
        return True

    async def test_webrtc_offer_answer_negotiation_is_authenticated(self) -> None:
        self.assertEqual(await self._exchange("webrtc"), b"reply:hello")
        self.assertEqual(self.responder_calls, [self.peer_a])
        kinds = [payload["kind"] for _, _, payload in self.bus.messages]
        self.assertIn("webrtc_offer", kinds)
        self.assertIn("webrtc_answer", kinds)

    async def test_relay_ticket_negotiation_is_authenticated(self) -> None:
        self.assertEqual(await self._exchange("relay"), b"reply:hello")
        invite = next(payload for _, _, payload in self.bus.messages if payload["kind"] == "relay_invite")
        self.assertEqual(len(invite["ticket"]), 43)
        self.assertNotIn("uri", invite)
        self.assertGreater(self.relay.status()["bytes_forwarded"], 0)

    async def test_auto_falls_back_to_relay_after_webrtc_failure(self) -> None:
        with mock.patch(
            "cloud.transport_broker.create_webrtc_offer",
            new=mock.AsyncMock(side_effect=P2PTransportError("direct transport unavailable")),
        ):
            self.assertEqual(await self._exchange("auto"), b"reply:hello")
        self.assertTrue(any(payload["kind"] == "relay_invite" for _, _, payload in self.bus.messages))

    async def test_signaling_identity_mismatch_is_rejected_before_send(self) -> None:
        wrong_peer = PeerAddress("dev_b", self.key_a)
        with self.assertRaises(P2PHandshakeError):
            await self.broker_a.run_with_peer_async(wrong_peer, self._unused_operation)
        self.assertEqual(self.bus.messages, [])
        self.assertEqual(self.responder_calls, [])

    async def test_malformed_and_replayed_invitations_are_ignored(self) -> None:
        self.bus.send("dev_a", "dev_b", {
            "protocol": _PROTOCOL,
            "kind": "relay_invite",
            "session_id": "a" * 32,
            "ticket": "not-a-ticket",
            "unexpected": True,
        })
        self.assertEqual(await self._exchange("relay"), b"reply:hello")
        invite = next(payload for _, _, payload in self.bus.messages if payload["kind"] == "relay_invite")
        self.bus.send("dev_a", "dev_b", invite)
        await asyncio.sleep(0.2)
        self.assertEqual(len(self.responder_calls), 1)

    async def test_peer_revocation_closes_active_tunnels_and_blocks_new_ones(self) -> None:
        await self._assert_security_shutdown("peer_revoked")

    async def test_kill_switch_closes_active_tunnels_and_blocks_new_ones(self) -> None:
        await self._assert_security_shutdown("kill_switch")

    async def _assert_security_shutdown(self, mode: str) -> None:
        self.hold_responder = True

        async def operation(session: Any) -> None:
            await session.send(b"hold")
            await asyncio.sleep(60)

        task = asyncio.create_task(
            self.broker_a.run_with_peer_async(self.peer_b, operation, transport="webrtc", timeout=5)
        )
        self.assertTrue(await asyncio.to_thread(self.responder_started.wait, 5))
        await self._wait_for(lambda: len(self.broker_a.status()["active_tunnels"]) == 1)
        if mode == "peer_revoked":
            self.signaling["dev_a"].handle_server_event({
                "type": "peer_revoked", "device_record_id": "dev_b"
            })
            self.signaling["dev_b"].handle_server_event({
                "type": "device_revoked", "device_record_id": "dev_b"
            })
        else:
            event = {
                "type": "kill_switch_updated",
                "event": {"switch": "p2p_cross_device_sync", "active": True},
            }
            self.signaling["dev_a"].handle_server_event(event)
            self.signaling["dev_b"].handle_server_event(event)
        with self.assertRaises((asyncio.CancelledError, P2PConnectionClosed, NegotiationError)):
            await asyncio.wait_for(task, timeout=5)
        await self._wait_for(lambda: not self.broker_a.status()["active_tunnels"])
        await self._wait_for(lambda: not self.broker_b.status()["active_tunnels"])
        with self.assertRaises(NegotiationError):
            await self.broker_a.run_with_peer_async(self.peer_b, self._unused_operation)
        self.release_responder.set()

    async def test_stop_closes_active_tunnels(self) -> None:
        self.hold_responder = True

        async def operation(session: Any) -> None:
            await session.send(b"hold")
            await asyncio.sleep(60)

        task = asyncio.create_task(
            self.broker_a.run_with_peer_async(self.peer_b, operation, transport="webrtc", timeout=5)
        )
        self.assertTrue(await asyncio.to_thread(self.responder_started.wait, 5))
        await asyncio.to_thread(self.broker_a.stop)
        with self.assertRaises((asyncio.CancelledError, P2PConnectionClosed, NegotiationError)):
            await asyncio.wait_for(task, timeout=5)
        self.release_responder.set()

    async def _unused_operation(self, _session: Any) -> None:
        raise AssertionError("Operation must not run for a rejected peer.")


if __name__ == "__main__":
    unittest.main()
