"""Authenticated peer transport and loopback tests."""

from __future__ import annotations

import asyncio
import base64
import json
import unittest
from unittest.mock import patch

from cloud.p2p_transport import (
    MAX_FRAME_BYTES,
    InMemoryNetwork,
    P2PHandshakeError,
    P2PProtocolError,
    PeerAddress,
    PeerIdentity,
)
from cloud.auth_client import generate_device_identity
from cloud.sync_contract import SyncEnvelope, SyncStream, canonical_json_bytes, ledger_scope


def _replace_signature(frame: bytes) -> bytes:
    value = json.loads(frame.decode("utf-8"))
    raw = bytearray(
        base64.b64decode(
            value["signature"] + "=" * (-len(value["signature"]) % 4),
            altchars=b"-_",
            validate=True,
        )
    )
    raw[0] ^= 1
    value["signature"] = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return canonical_json_bytes(value)


class P2PTransportTests(unittest.IsolatedAsyncioTestCase):
    async def _pair(self, network: InMemoryNetwork | None = None):
        network = network or InMemoryNetwork()
        alice = network.endpoint(PeerIdentity.generate("dev_alice"))
        bob = network.endpoint(PeerIdentity.generate("dev_bob"))
        alice_session = await alice.connect(bob)
        bob_session = await bob.accept()
        self.addAsyncCleanup(alice_session.close)
        self.addAsyncCleanup(bob_session.close)
        return network, alice, bob, alice_session, bob_session

    async def test_mutual_authentication_and_bidirectional_transfer(self) -> None:
        _, alice_endpoint, bob_endpoint, alice, bob = await self._pair()

        self.assertEqual(alice.remote_device_id, "dev_bob")
        self.assertEqual(bob.remote_device_id, "dev_alice")
        self.assertEqual(
            alice.remote_signing_public_key, bob_endpoint.identity.signing_public_key
        )
        self.assertEqual(
            bob.remote_signing_public_key, alice_endpoint.identity.signing_public_key
        )

        await alice.send(b"from-alice")
        await bob.send(b"from-bob")
        self.assertEqual(await bob.receive(), b"from-alice")
        self.assertEqual(await alice.receive(), b"from-bob")

    async def test_sync_envelope_round_trip(self) -> None:
        _, _, _, alice, bob = await self._pair()
        envelope = SyncEnvelope(
            SyncStream.LEDGER,
            ledger_scope("task-1"),
            "dev_alice",
            7,
            {"version": 1, "events": [{"sequence_no": 8}]},
        )

        await alice.send_envelope(envelope)

        self.assertEqual(await bob.receive_envelope(), envelope)

    async def test_large_payload_round_trip(self) -> None:
        _, _, _, alice, bob = await self._pair()
        payload = b"x" * (1024 * 1024)

        await alice.send(payload)

        self.assertEqual(await bob.receive(), payload)

    async def test_concurrent_sends_are_authenticated_and_ordered(self) -> None:
        _, _, _, alice, bob = await self._pair()
        payloads = [f"message-{index}".encode() for index in range(25)]

        await asyncio.gather(*(alice.send(payload) for payload in payloads))
        received = [await bob.receive() for _ in payloads]

        self.assertEqual(received, payloads)

    async def test_plaintext_never_appears_in_transport_frames(self) -> None:
        observed: list[bytes] = []

        def record(frame: bytes) -> bytes:
            observed.append(frame)
            return frame

        _, _, _, alice, bob = await self._pair(InMemoryNetwork(frame_transform=record))
        secret = b"provider-secret-that-must-stay-private"

        await alice.send(secret)

        self.assertEqual(await bob.receive(), secret)
        self.assertNotIn(secret, b"".join(observed))

    async def test_tampered_data_frame_is_rejected(self) -> None:
        def tamper(frame: bytes) -> bytes:
            if frame.startswith(b"HXP1"):
                return frame[:-1] + bytes((frame[-1] ^ 1,))
            return frame

        _, _, _, alice, bob = await self._pair(InMemoryNetwork(frame_transform=tamper))
        await alice.send(b"protected")

        with self.assertRaises(P2PProtocolError):
            await bob.receive()

    async def test_replayed_data_frame_is_rejected(self) -> None:
        encrypted_frames: list[bytes] = []

        def replay_second(frame: bytes) -> bytes:
            if not frame.startswith(b"HXP1"):
                return frame
            encrypted_frames.append(frame)
            if len(encrypted_frames) == 2:
                return encrypted_frames[0]
            return frame

        _, _, _, alice, bob = await self._pair(InMemoryNetwork(frame_transform=replay_second))
        await alice.send(b"first")
        await alice.send(b"second")

        self.assertEqual(await bob.receive(), b"first")
        with self.assertRaises(P2PProtocolError):
            await bob.receive()

    async def test_tampered_responder_signature_is_rejected(self) -> None:
        def tamper_response(frame: bytes) -> bytes:
            if b'"role":"responder"' in frame:
                return _replace_signature(frame)
            return frame

        network = InMemoryNetwork(frame_transform=tamper_response)
        alice = network.endpoint(PeerIdentity.generate("dev_alice"))
        bob = network.endpoint(PeerIdentity.generate("dev_bob"))

        with self.assertRaises(P2PHandshakeError):
            await alice.connect(bob)

    async def test_tampered_initiator_finish_is_rejected(self) -> None:
        finish_frames = 0

        def tamper_finish(frame: bytes) -> bytes:
            nonlocal finish_frames
            if b'"role":"initiator"' in frame and b'"signature"' in frame:
                finish_frames += 1
                if finish_frames == 1:
                    return _replace_signature(frame)
            return frame

        network = InMemoryNetwork(frame_transform=tamper_finish)
        alice = network.endpoint(PeerIdentity.generate("dev_alice"))
        bob = network.endpoint(PeerIdentity.generate("dev_bob"))

        with self.assertRaises(P2PHandshakeError):
            await alice.connect(bob)

    async def test_unsupported_handshake_version_is_rejected(self) -> None:
        def change_version(frame: bytes) -> bytes:
            if b'"role":"initiator"' in frame and b'"nonce"' in frame:
                value = json.loads(frame.decode("utf-8"))
                value["v"] = 2
                return canonical_json_bytes(value)
            return frame

        network = InMemoryNetwork(frame_transform=change_version)
        alice = network.endpoint(PeerIdentity.generate("dev_alice"))
        bob = network.endpoint(PeerIdentity.generate("dev_bob"))

        with self.assertRaises(P2PHandshakeError):
            await alice.connect(bob)

    async def test_unexpected_peer_identity_is_rejected(self) -> None:
        network = InMemoryNetwork()
        alice = network.endpoint(PeerIdentity.generate("dev_alice"))
        bob = network.endpoint(PeerIdentity.generate("dev_bob"))
        impostor = PeerIdentity.generate("dev_impostor")

        with self.assertRaises(P2PHandshakeError):
            await alice.connect(bob, expected_peer=impostor.address)

    async def test_oversized_payload_is_rejected_before_sending(self) -> None:
        _, _, _, alice, bob = await self._pair()

        with patch("cloud.p2p_transport.MAX_FRAME_BYTES", 128):
            with self.assertRaises(ValueError):
                await alice.send(b"x" * 128)

        self.assertEqual(bob.status()["receive_sequence"], 0)

    async def test_invalid_sync_envelope_is_rejected(self) -> None:
        _, _, _, alice, bob = await self._pair()
        await alice.send(b'{"stream":"unknown"}')

        with self.assertRaises(ValueError):
            await bob.receive_envelope()

    async def test_close_is_idempotent_and_notifies_peer(self) -> None:
        _, _, _, alice, bob = await self._pair()

        await alice.close()
        await alice.close()

        self.assertIsNone(await bob.receive())
        self.assertTrue(alice.closed)
        self.assertTrue(bob.closed)

    async def test_duplicate_handshake_keys_are_rejected(self) -> None:
        def duplicate_device_id(frame: bytes) -> bytes:
            if b'"role":"initiator"' in frame and b'"nonce"' in frame:
                return b'{"device_id":"dev_impostor",' + frame[1:]
            return frame

        network = InMemoryNetwork(frame_transform=duplicate_device_id)
        alice = network.endpoint(PeerIdentity.generate("dev_alice"))
        bob = network.endpoint(PeerIdentity.generate("dev_bob"))

        with self.assertRaises(P2PHandshakeError):
            await alice.connect(bob)

    async def test_duplicate_and_nonfinite_envelope_json_are_rejected(self) -> None:
        _, _, _, alice, bob = await self._pair()
        await alice.send(b'{"stream":"ledger","stream":"cognition"}')
        with self.assertRaises(P2PProtocolError):
            await bob.receive_envelope()

        await alice.send(b'{"stream":"ledger","payload":{"value":NaN}}')
        with self.assertRaises(P2PProtocolError):
            await bob.receive_envelope()

    async def test_peer_identity_adapts_existing_device_identity(self) -> None:
        device_identity = generate_device_identity("account-key")

        peer = PeerIdentity.from_device_identity(device_identity, "dev_alice")

        self.assertEqual(peer.device_id, "dev_alice")
        self.assertEqual(peer.signing_public_key, device_identity.device_pubkey)

    async def test_peer_address_normalizes_signing_key(self) -> None:
        identity = PeerIdentity.generate("dev_alice")
        raw = identity.signing_public_key[len("ed25519:") :].upper()

        self.assertEqual(
            PeerAddress("dev_alice", raw),
            PeerAddress("dev_alice", identity.signing_public_key),
        )


if __name__ == "__main__":
    unittest.main()
