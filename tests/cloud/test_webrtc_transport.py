"""Authenticated WebRTC DataChannel transport regressions."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from cloud.p2p_transport import (
    MAX_FRAME_BYTES,
    P2P_SUBPROTOCOL,
    P2PHandshakeError,
    P2PProtocolError,
    P2PTransportError,
    PeerIdentity,
)
from cloud.sync_contract import SyncEnvelope, SyncStream, ledger_scope
from cloud.webrtc_transport import (
    _WEBRTC_CHUNK_BYTES,
    WebRTCPeer,
    _validate_description,
    _validate_stun_servers,
    _WebRTCChannel,
    create_webrtc_answer,
    create_webrtc_offer,
)


class _FakeDataChannel:
    def __init__(self) -> None:
        self.readyState = "open"
        self.label = P2P_SUBPROTOCOL
        self.packets: list[bytes] = []
        self.handlers: dict[str, object] = {}
        self.closed = False

    def on(self, event: str, handler: object) -> None:
        self.handlers[event] = handler

    def send(self, packet: bytes) -> None:
        self.packets.append(packet)

    def close(self) -> None:
        self.closed = True


def _chunk(payload: bytes, *, length: int | None = None, offset: int = 0) -> bytes:
    declared = len(payload) if length is None else length
    return declared.to_bytes(4, "big") + offset.to_bytes(4, "big") + payload


class WebRTCChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_frame_is_chunked_with_ordered_offsets(self) -> None:
        datachannel = _FakeDataChannel()
        channel = _WebRTCChannel(SimpleNamespace(error=None), datachannel)
        frame = b"x" * (_WEBRTC_CHUNK_BYTES * 3 + 17)

        await channel.send(frame)

        self.assertEqual(len(datachannel.packets), 4)
        rebuilt = bytearray()
        for index, packet in enumerate(datachannel.packets):
            self.assertEqual(int.from_bytes(packet[:4], "big"), len(frame))
            self.assertEqual(int.from_bytes(packet[4:8], "big"), index * _WEBRTC_CHUNK_BYTES)
            self.assertLessEqual(len(packet) - 8, _WEBRTC_CHUNK_BYTES)
            rebuilt.extend(packet[8:])
        self.assertEqual(bytes(rebuilt), frame)

    async def test_receiver_reassembles_only_complete_ordered_frames(self) -> None:
        datachannel = _FakeDataChannel()
        peer = SimpleNamespace(error=None, fail=Mock())
        channel = _WebRTCChannel(peer, datachannel)

        channel._on_message(_chunk(b"hello ", length=11))
        channel._on_message(_chunk(b"world", length=11, offset=6))

        self.assertEqual(await asyncio.wait_for(channel.receive(), timeout=1), b"hello world")
        peer.fail.assert_not_called()

    async def test_malformed_chunks_fail_closed(self) -> None:
        cases = [
            b"short",
            _chunk(b"x" * (_WEBRTC_CHUNK_BYTES + 1)),
            _chunk(b"", length=0),
            _chunk(b"x", length=MAX_FRAME_BYTES + 1),
            _chunk(b"x", length=1, offset=2),
            _chunk(b"x", offset=1),
        ]
        for packet in cases:
            with self.subTest(packet=packet[:32], length=len(packet)):
                peer = SimpleNamespace(error=None, fail=Mock())
                channel = _WebRTCChannel(peer, _FakeDataChannel())
                channel._on_message(packet)
                peer.fail.assert_called_once()
                self.assertIsInstance(peer.fail.call_args.args[0], P2PProtocolError)

        for packets in (
            [_chunk(b"ab", length=4), _chunk(b"cd", offset=3)],
            [_chunk(b"abc", length=2)],
        ):
            with self.subTest(packets=packets):
                peer = SimpleNamespace(error=None, fail=Mock())
                channel = _WebRTCChannel(peer, _FakeDataChannel())
                for packet in packets:
                    channel._on_message(packet)
                peer.fail.assert_called_once()

    async def test_send_rejects_invalid_frame_lengths(self) -> None:
        channel = _WebRTCChannel(SimpleNamespace(error=None), _FakeDataChannel())
        with self.assertRaises(P2PProtocolError):
            await channel.send(b"")
        with self.assertRaises(P2PProtocolError):
            await channel.send(b"x" * (MAX_FRAME_BYTES + 1))

    def test_stun_servers_are_bounded_and_exclude_turn_credentials(self) -> None:
        self.assertEqual(_validate_stun_servers(None), [])
        self.assertEqual(
            _validate_stun_servers(["stun:localhost:3478", "stuns:example.test"]),
            ["stun:localhost:3478", "stuns:example.test"],
        )
        for value in (
            "stun:localhost:3478",
            ["turn:localhost:3478"],
            ["stun:bad host"],
            ["stun:" + "x" * 513],
            [None],
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    _validate_stun_servers(value)  # type: ignore[arg-type]

    def test_sdp_description_requires_exact_bounded_fields(self) -> None:
        self.assertEqual(
            _validate_description({"type": "offer", "sdp": "v=0"}, "offer"),
            {"type": "offer", "sdp": "v=0"},
        )
        for value in (
            None,
            {"type": "offer"},
            {"type": "answer", "sdp": "v=0"},
            {"type": "offer", "sdp": "v=0", "extra": True},
            {"type": "offer", "sdp": ""},
            {"type": "offer", "sdp": "v=0\x00"},
            {"type": "offer", "sdp": "x" * (256 * 1024 + 1)},
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    _validate_description(value, "offer")


class WebRTCTransportTests(unittest.IsolatedAsyncioTestCase):
    async def _pair(
        self,
        *,
        alice_expected: PeerIdentity | None = None,
        bob_expected: PeerIdentity | None = None,
    ):
        alice_identity = PeerIdentity.generate("dev_alice")
        bob_identity = PeerIdentity.generate("dev_bob")
        offer = await create_webrtc_offer(alice_identity, timeout=10)
        self.addAsyncCleanup(offer.close)
        answer = await create_webrtc_answer(offer.description, bob_identity, timeout=10)
        self.addAsyncCleanup(answer.close)
        accept_task = asyncio.create_task(answer.accept(
            expected_peer=(bob_expected or alice_identity).address,
            timeout=10,
        ))
        try:
            alice_session = await offer.connect(
                answer.description,
                expected_peer=(alice_expected or bob_identity).address,
                timeout=10,
            )
            bob_session = await accept_task
        except BaseException:
            accept_task.cancel()
            await asyncio.gather(accept_task, return_exceptions=True)
            raise
        self.addAsyncCleanup(alice_session.close)
        self.addAsyncCleanup(bob_session.close)
        return offer, answer, alice_identity, bob_identity, alice_session, bob_session

    async def test_authenticated_bidirectional_transfer_and_envelope(self) -> None:
        offer, answer, alice, bob, alice_session, bob_session = await self._pair()

        self.assertEqual(alice_session.remote_device_id, bob.device_id)
        self.assertEqual(bob_session.remote_device_id, alice.device_id)
        self.assertEqual(offer.status()["transport"], "webrtc")
        self.assertEqual(answer.status()["data_channel"], "open")
        await alice_session.send(b"from-alice")
        await bob_session.send(b"from-bob")
        self.assertEqual(await bob_session.receive(), b"from-alice")
        self.assertEqual(await alice_session.receive(), b"from-bob")

        envelope = SyncEnvelope(
            SyncStream.LEDGER,
            ledger_scope("task-webrtc"),
            alice.device_id,
            3,
            {"version": 1, "events": []},
        )
        await alice_session.send_envelope(envelope)
        self.assertEqual(await bob_session.receive_envelope(), envelope)

    async def test_large_encrypted_payload_round_trip(self) -> None:
        _, _, _, _, alice_session, bob_session = await self._pair()
        payload = b"webrtc-payload" * (128 * 1024)

        await alice_session.send(payload)

        self.assertEqual(await bob_session.receive(), payload)

    async def test_unexpected_remote_identity_is_rejected(self) -> None:
        impostor = PeerIdentity.generate("dev_impostor")
        with self.assertRaises(P2PHandshakeError):
            await self._pair(alice_expected=impostor)

    async def test_responder_rejects_unexpected_initiator(self) -> None:
        impostor = PeerIdentity.generate("dev_impostor")
        with self.assertRaises(P2PTransportError):
            await self._pair(bob_expected=impostor)

    async def test_clean_close_notifies_peer_and_peer_lifecycle_is_idempotent(self) -> None:
        _, _, _, _, alice_session, bob_session = await self._pair()

        await bob_session.close()
        await bob_session.close()

        self.assertIsNone(await alice_session.receive())


if __name__ == "__main__":
    unittest.main()
