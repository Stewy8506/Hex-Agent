"""Authenticated peer transport and loopback tests."""

from __future__ import annotations

import asyncio
import base64
import datetime
import ipaddress
import json
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from websockets.exceptions import ConnectionClosed, WebSocketException

from cloud.p2p_transport import (
    MAX_FRAME_BYTES,
    P2P_SUBPROTOCOL,
    InMemoryNetwork,
    P2PConnectionClosed,
    P2PHandshakeError,
    P2PProtocolError,
    P2PTransportError,
    PeerAddress,
    PeerIdentity,
    QuicPeerServer,
    WebSocketPeerServer,
    connect_quic_peer,
    connect_websocket_peer,
)
from cloud.auth_client import generate_device_identity
from cloud.sync_contract import SyncEnvelope, SyncStream, canonical_json_bytes, ledger_scope


def _write_test_certificate(root: Path) -> tuple[Path, Path]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            ]),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )
    key_path = root / "peer-key.pem"
    certificate_path = root / "peer-cert.pem"
    key_path.write_bytes(private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return key_path, certificate_path


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


class WebSocketTransportTests(unittest.IsolatedAsyncioTestCase):
    async def _pair(
        self,
        *,
        server_ssl_context: ssl.SSLContext | None = None,
        client_ssl_context: ssl.SSLContext | None = None,
        allowed_peers: set[PeerAddress] | None = None,
    ):
        alice = PeerIdentity.generate("dev_alice")
        bob = PeerIdentity.generate("dev_bob")
        server = WebSocketPeerServer(
            alice,
            ssl_context=server_ssl_context,
            allowed_peers=allowed_peers,
        )
        await server.start()
        self.addAsyncCleanup(server.close)
        client_task = asyncio.create_task(connect_websocket_peer(
            server.uri,
            bob,
            expected_peer=alice.address,
            ssl_context=client_ssl_context,
        ))
        alice_session, bob_session = await asyncio.wait_for(
            asyncio.gather(
                server.accept(expected_peer=bob.address),
                client_task,
            ),
            timeout=10,
        )
        self.addAsyncCleanup(alice_session.close)
        self.addAsyncCleanup(bob_session.close)
        return server, alice, bob, alice_session, bob_session

    async def test_authenticated_bidirectional_binary_transport(self) -> None:
        server, alice, bob, alice_session, bob_session = await self._pair()

        self.assertEqual(alice_session.remote_device_id, bob.device_id)
        self.assertEqual(bob_session.remote_device_id, alice.device_id)
        self.assertEqual(server.status()["subprotocol"], P2P_SUBPROTOCOL)
        self.assertEqual(server.status()["accepted_sessions"], 1)
        await alice_session.send(b"from-alice")
        await bob_session.send(b"from-bob")
        self.assertEqual(await bob_session.receive(), b"from-alice")
        self.assertEqual(await alice_session.receive(), b"from-bob")

        envelope = SyncEnvelope(
            SyncStream.LEDGER,
            ledger_scope("task-ws"),
            alice.device_id,
            0,
            {"version": 1, "events": []},
        )
        await alice_session.send_envelope(envelope)
        self.assertEqual(await bob_session.receive_envelope(), envelope)

    async def test_client_rejects_unexpected_device_identity(self) -> None:
        alice = PeerIdentity.generate("dev_alice")
        bob = PeerIdentity.generate("dev_bob")
        impostor = PeerIdentity.generate("dev_impostor")
        server = WebSocketPeerServer(alice)
        await server.start()
        self.addAsyncCleanup(server.close)

        with self.assertRaises(P2PHandshakeError):
            await connect_websocket_peer(
                server.uri,
                bob,
                expected_peer=impostor.address,
            )
        with self.assertRaises(P2PTransportError):
            await server.accept(timeout=0.05)

    async def test_server_rejects_unlisted_device(self) -> None:
        alice = PeerIdentity.generate("dev_alice")
        bob = PeerIdentity.generate("dev_bob")
        allowed = PeerIdentity.generate("dev_allowed")
        server = WebSocketPeerServer(alice, allowed_peers={allowed.address})
        await server.start()
        self.addAsyncCleanup(server.close)
        client = await connect_websocket_peer(
            server.uri,
            bob,
            expected_peer=alice.address,
        )
        self.addAsyncCleanup(client.close)

        with self.assertRaises(P2PTransportError):
            await server.accept(timeout=0.05)
        self.assertEqual(server.status()["accepted_sessions"], 0)

    async def test_empty_allow_list_rejects_every_device(self) -> None:
        alice = PeerIdentity.generate("dev_alice")
        bob = PeerIdentity.generate("dev_bob")
        server = WebSocketPeerServer(alice, allowed_peers=set())
        await server.start()
        self.addAsyncCleanup(server.close)
        client = await connect_websocket_peer(
            server.uri,
            bob,
            expected_peer=alice.address,
        )
        self.addAsyncCleanup(client.close)

        with self.assertRaises(P2PTransportError):
            await server.accept(timeout=0.05)
        self.assertEqual(server.status()["accepted_sessions"], 0)

    async def test_plaintext_transport_is_loopback_only(self) -> None:
        identity = PeerIdentity.generate("dev_alice")
        peer = PeerIdentity.generate("dev_bob").address

        with self.assertRaisesRegex(ValueError, "loopback"):
            WebSocketPeerServer(identity, host="0.0.0.0")
        with self.assertRaisesRegex(ValueError, "loopback"):
            await connect_websocket_peer(
                "ws://192.0.2.1:8787/hex/p2p/v1",
                identity,
                expected_peer=peer,
            )

    async def test_websocket_uri_rejects_userinfo_queries_and_fragments(self) -> None:
        identity = PeerIdentity.generate("dev_alice")
        peer = PeerIdentity.generate("dev_bob").address

        for uri in (
            "http://127.0.0.1:8787/hex/p2p/v1",
            "ws://user@127.0.0.1:8787/hex/p2p/v1",
            "ws://127.0.0.1:8787/hex/p2p/v1?token=secret",
            "ws://127.0.0.1:8787/hex/p2p/v1#fragment",
        ):
            with self.subTest(uri=uri):
                with self.assertRaises(ValueError):
                    await connect_websocket_peer(uri, identity, expected_peer=peer)

    async def test_client_ssl_context_must_verify_certificates(self) -> None:
        identity = PeerIdentity.generate("dev_alice")
        peer = PeerIdentity.generate("dev_bob").address
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

        with self.assertRaisesRegex(ValueError, "require certificate verification"):
            await connect_websocket_peer(
                "wss://127.0.0.1:8787/hex/p2p/v1",
                identity,
                expected_peer=peer,
                ssl_context=context,
            )
        with self.assertRaisesRegex(ValueError, "requires a wss URI"):
            await connect_websocket_peer(
                "ws://127.0.0.1:8787/hex/p2p/v1",
                identity,
                expected_peer=peer,
                ssl_context=context,
            )

    async def test_wss_round_trip_with_private_ca(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hex_wss_test_") as temporary:
            root = Path(temporary)
            key_path, certificate_path = _write_test_certificate(root)
            server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_context.load_cert_chain(certificate_path, key_path)
            client_context = ssl.create_default_context(cafile=certificate_path)
            server, _, _, alice_session, bob_session = await self._pair(
                server_ssl_context=server_context,
                client_ssl_context=client_context,
            )

            self.assertTrue(server.status()["tls"])
            self.assertTrue(server.uri.startswith("wss://"))
            await alice_session.send(b"verified transport")
            self.assertEqual(await bob_session.receive(), b"verified transport")

    async def test_untrusted_wss_certificate_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hex_wss_test_") as temporary:
            root = Path(temporary)
            key_path, _certificate_path = _write_test_certificate(root)
            server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_context.load_cert_chain(_certificate_path, key_path)
            alice = PeerIdentity.generate("dev_alice")
            bob = PeerIdentity.generate("dev_bob")
            server = WebSocketPeerServer(alice, ssl_context=server_context)
            await server.start()
            self.addAsyncCleanup(server.close)

            with self.assertRaises(P2PTransportError):
                await connect_websocket_peer(
                    server.uri,
                    bob,
                    expected_peer=alice.address,
                    timeout=2,
                )

    async def test_websocket_connection_errors_use_transport_error(self) -> None:
        identity = PeerIdentity.generate("dev_alice")
        peer = PeerIdentity.generate("dev_bob").address

        with patch(
            "cloud.p2p_transport._NoRedirectWebSocketConnect",
            side_effect=WebSocketException("handshake rejected"),
        ):
            with self.assertRaises(P2PTransportError):
                await connect_websocket_peer(
                    "ws://127.0.0.1:8787/hex/p2p/v1",
                    identity,
                    expected_peer=peer,
                )

    async def test_cancelled_handshake_closes_connection(self) -> None:
        alice = PeerIdentity.generate("dev_alice")
        bob = PeerIdentity.generate("dev_bob")
        server = WebSocketPeerServer(alice)
        await server.start()
        self.addAsyncCleanup(server.close)
        started = asyncio.Event()

        async def blocked_handshake(*_args: object) -> None:
            started.set()
            await asyncio.Event().wait()

        with patch("cloud.p2p_transport._initiate_handshake", new=blocked_handshake):
            task = asyncio.create_task(connect_websocket_peer(
                server.uri,
                bob,
                expected_peer=alice.address,
            ))
            await asyncio.wait_for(started.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        with self.assertRaises(P2PTransportError):
            await server.accept(timeout=0.05)
        self.assertEqual(server.status()["active_connections"], 0)

    async def test_server_requires_subprotocol_and_rejects_browser_origins(self) -> None:
        try:
            from websockets.asyncio.client import connect as raw_connect
            from websockets.exceptions import InvalidStatus
        except ImportError:
            from websockets.legacy.client import connect as raw_connect
            from websockets.exceptions import InvalidStatusCode as InvalidStatus

        identity = PeerIdentity.generate("dev_alice")
        server = WebSocketPeerServer(identity)
        await server.start()
        self.addAsyncCleanup(server.close)

        with self.assertRaises(InvalidStatus):
            await raw_connect(server.uri)
        with self.assertRaises(P2PTransportError):
            await server.accept(timeout=0.05)

        with self.assertRaises(InvalidStatus):
            await raw_connect(
                server.uri,
                origin="https://browser.invalid",
                subprotocols=[P2P_SUBPROTOCOL],
            )

    async def test_text_messages_are_rejected(self) -> None:
        try:
            from websockets.asyncio.client import connect as raw_connect
        except ImportError:
            from websockets.legacy.client import connect as raw_connect

        identity = PeerIdentity.generate("dev_alice")
        server = WebSocketPeerServer(identity)
        await server.start()
        self.addAsyncCleanup(server.close)
        websocket = await raw_connect(server.uri, subprotocols=[P2P_SUBPROTOCOL])
        self.addAsyncCleanup(websocket.close)

        await websocket.send("not binary")

        with self.assertRaises(ConnectionClosed):
            await websocket.recv()
        with self.assertRaises(P2PTransportError):
            await server.accept(timeout=0.05)
        self.assertEqual(server.status()["accepted_sessions"], 0)

    async def test_abrupt_disconnect_fails_closed(self) -> None:
        server, _, _, alice_session, _bob_session = await self._pair()

        await server.close()

        with self.assertRaises(P2PConnectionClosed):
            await alice_session.receive()

    async def test_restart_does_not_expose_stale_sessions(self) -> None:
        server, _, _, alice_session, bob_session = await self._pair()

        await server.close()
        await asyncio.gather(alice_session.close(), bob_session.close())
        self.assertIs(await server.start(), server)

        with self.assertRaises(P2PTransportError):
            await server.accept(timeout=0.05)
        self.assertEqual(server.status()["active_connections"], 0)

    async def test_clean_close_notifies_peer_and_lifecycle_is_idempotent(self) -> None:
        server, _, _, alice_session, bob_session = await self._pair()

        self.assertIs(await server.start(), server)
        await bob_session.close()
        await bob_session.close()
        self.assertIsNone(await alice_session.receive())
        await server.close()
        await server.close()
        self.assertFalse(server.running)


class QuicTransportTests(unittest.IsolatedAsyncioTestCase):
    async def _start_server(
        self,
        identity: PeerIdentity,
        **options: object,
    ) -> tuple[QuicPeerServer, Path]:
        temporary = tempfile.TemporaryDirectory(prefix="hex_quic_test_")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        key_path, certificate_path = _write_test_certificate(root)
        server = QuicPeerServer(
            identity,
            certificate=certificate_path,
            private_key=key_path,
            **options,
        )
        await server.start()
        self.addAsyncCleanup(server.close)
        return server, certificate_path

    async def _pair(
        self,
        *,
        allowed_peers: set[PeerAddress] | None = None,
    ):
        alice = PeerIdentity.generate("dev_alice")
        bob = PeerIdentity.generate("dev_bob")
        server, certificate_path = await self._start_server(
            alice,
            allowed_peers=allowed_peers,
        )
        client_task = asyncio.create_task(connect_quic_peer(
            server.host,
            server.port,
            bob,
            expected_peer=alice.address,
            cafile=certificate_path,
        ))
        alice_session, bob_session = await asyncio.wait_for(
            asyncio.gather(
                server.accept(expected_peer=bob.address),
                client_task,
            ),
            timeout=10,
        )
        self.addAsyncCleanup(alice_session.close)
        self.addAsyncCleanup(bob_session.close)
        return server, alice, bob, alice_session, bob_session

    async def test_authenticated_bidirectional_transport_and_envelope(self) -> None:
        server, alice, bob, alice_session, bob_session = await self._pair()

        self.assertEqual(alice_session.remote_device_id, bob.device_id)
        self.assertEqual(bob_session.remote_device_id, alice.device_id)
        self.assertEqual(server.status()["alpn"], P2P_SUBPROTOCOL)
        self.assertTrue(server.status()["tls"])
        self.assertEqual(server.status()["accepted_sessions"], 1)
        await alice_session.send(b"from-alice")
        await bob_session.send(b"from-bob")
        self.assertEqual(await bob_session.receive(), b"from-alice")
        self.assertEqual(await alice_session.receive(), b"from-bob")

        payload = b"quic-payload" * (128 * 1024)
        await alice_session.send(payload)
        self.assertEqual(await bob_session.receive(), payload)

        envelope = SyncEnvelope(
            SyncStream.LEDGER,
            ledger_scope("task-quic"),
            alice.device_id,
            0,
            {"version": 1, "events": []},
        )
        await alice_session.send_envelope(envelope)
        self.assertEqual(await bob_session.receive_envelope(), envelope)

    async def test_client_rejects_unexpected_device_identity(self) -> None:
        alice = PeerIdentity.generate("dev_alice")
        bob = PeerIdentity.generate("dev_bob")
        impostor = PeerIdentity.generate("dev_impostor")
        server, certificate_path = await self._start_server(alice)

        with self.assertRaises(P2PHandshakeError):
            await connect_quic_peer(
                server.host,
                server.port,
                bob,
                expected_peer=impostor.address,
                cafile=certificate_path,
                timeout=2,
            )
        with self.assertRaises(P2PTransportError):
            await server.accept(timeout=0.1)
        self.assertEqual(server.status()["accepted_sessions"], 0)

    async def test_server_rejects_unlisted_device(self) -> None:
        alice = PeerIdentity.generate("dev_alice")
        bob = PeerIdentity.generate("dev_bob")
        allowed = PeerIdentity.generate("dev_allowed")
        server, certificate_path = await self._start_server(
            alice,
            allowed_peers={allowed.address},
        )
        client = await connect_quic_peer(
            server.host,
            server.port,
            bob,
            expected_peer=alice.address,
            cafile=certificate_path,
            timeout=2,
        )
        self.addAsyncCleanup(client.close)

        with self.assertRaises(P2PTransportError):
            await server.accept(timeout=0.1)
        self.assertEqual(server.status()["accepted_sessions"], 0)

    async def test_empty_allow_list_rejects_every_device(self) -> None:
        alice = PeerIdentity.generate("dev_alice")
        bob = PeerIdentity.generate("dev_bob")
        server, certificate_path = await self._start_server(alice, allowed_peers=set())
        client = await connect_quic_peer(
            server.host,
            server.port,
            bob,
            expected_peer=alice.address,
            cafile=certificate_path,
            timeout=2,
        )
        self.addAsyncCleanup(client.close)

        with self.assertRaises(P2PTransportError):
            await server.accept(timeout=0.1)
        self.assertEqual(server.status()["accepted_sessions"], 0)

    async def test_untrusted_certificate_is_rejected(self) -> None:
        alice = PeerIdentity.generate("dev_alice")
        bob = PeerIdentity.generate("dev_bob")
        server, _certificate_path = await self._start_server(alice)

        with self.assertRaises(P2PTransportError):
            await connect_quic_peer(
                server.host,
                server.port,
                bob,
                expected_peer=alice.address,
                timeout=2,
            )
        self.assertEqual(server.status()["accepted_sessions"], 0)

    async def test_required_alpn_is_enforced(self) -> None:
        from aioquic.asyncio.server import serve
        from aioquic.quic.configuration import QuicConfiguration

        alice = PeerIdentity.generate("dev_alice")
        bob = PeerIdentity.generate("dev_bob")
        temporary = tempfile.TemporaryDirectory(prefix="hex_quic_alpn_test_")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        key_path, certificate_path = _write_test_certificate(root)
        for alpn_protocols in (None, ["not-hex-p2p"]):
            with self.subTest(alpn_protocols=alpn_protocols):
                configuration = QuicConfiguration(
                    is_client=False,
                    alpn_protocols=alpn_protocols,
                )
                configuration.load_cert_chain(str(certificate_path), str(key_path))
                server = await serve(
                    "127.0.0.1",
                    0,
                    configuration=configuration,
                )
                address = server._transport.get_extra_info("sockname")
                try:
                    with self.assertRaises(P2PTransportError):
                        await connect_quic_peer(
                            "127.0.0.1",
                            address[1],
                            bob,
                            expected_peer=alice.address,
                            cafile=certificate_path,
                            timeout=2,
                        )
                finally:
                    server.close()

    async def test_invalid_frame_lengths_and_server_streams_are_rejected(self) -> None:
        from aioquic.asyncio import QuicConnectionProtocol
        from aioquic.asyncio.server import serve
        from aioquic.quic.configuration import QuicConfiguration
        from aioquic.quic.events import ConnectionTerminated, HandshakeCompleted

        alice = PeerIdentity.generate("dev_alice")
        bob = PeerIdentity.generate("dev_bob")
        temporary = tempfile.TemporaryDirectory(prefix="hex_quic_protocol_test_")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        key_path, certificate_path = _write_test_certificate(root)

        for mode in ("oversized-length", "server-stream"):
            with self.subTest(mode=mode):
                class RawServerProtocol(QuicConnectionProtocol):
                    def __init__(self, quic: object) -> None:
                        super().__init__(quic)
                        self._started = False
                        self._sender: asyncio.Task[None] | None = None

                    def quic_event_received(self, event: object) -> None:
                        super().quic_event_received(event)
                        if isinstance(event, ConnectionTerminated):
                            if self._sender is not None:
                                self._sender.cancel()
                        elif isinstance(event, HandshakeCompleted) and not self._started:
                            self._started = True
                            self._sender = asyncio.create_task(self._send_invalid_data())

                    async def _send_invalid_data(self) -> None:
                        _reader, writer = await self.create_stream()
                        if mode == "oversized-length":
                            writer.write((MAX_FRAME_BYTES + 1).to_bytes(4, "big"))
                        else:
                            writer.write(b"unsolicited")
                            await writer.drain()
                            writer.write_eof()
                        await writer.drain()

                configuration = QuicConfiguration(
                    is_client=False,
                    alpn_protocols=[P2P_SUBPROTOCOL],
                )
                configuration.load_cert_chain(str(certificate_path), str(key_path))
                server = await serve(
                    "127.0.0.1",
                    0,
                    configuration=configuration,
                    create_protocol=RawServerProtocol,
                )
                address = server._transport.get_extra_info("sockname")
                try:
                    with self.assertRaises(P2PTransportError):
                        await connect_quic_peer(
                            "127.0.0.1",
                            address[1],
                            bob,
                            expected_peer=alice.address,
                            cafile=certificate_path,
                            timeout=2,
                        )
                finally:
                    server.close()

    async def test_connection_limit_rejects_additional_client(self) -> None:
        alice = PeerIdentity.generate("dev_alice")
        bob = PeerIdentity.generate("dev_bob")
        server, certificate_path = await self._start_server(
            alice,
            max_connections=1,
            handshake_timeout=2,
        )
        started = asyncio.Event()

        async def blocked_handshake(*_args: object) -> None:
            started.set()
            await asyncio.Event().wait()

        with patch("cloud.p2p_transport._initiate_handshake", new=blocked_handshake):
            first = asyncio.create_task(connect_quic_peer(
                server.host,
                server.port,
                bob,
                expected_peer=alice.address,
                cafile=certificate_path,
                timeout=5,
            ))
            await asyncio.wait_for(started.wait(), timeout=2)
            try:
                with self.assertRaises(P2PTransportError):
                    await connect_quic_peer(
                        server.host,
                        server.port,
                        PeerIdentity.generate("dev_carol"),
                        expected_peer=alice.address,
                        cafile=certificate_path,
                        timeout=0.5,
                    )
            finally:
                first.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await first

    async def test_cancelled_handshake_closes_connection(self) -> None:
        alice = PeerIdentity.generate("dev_alice")
        bob = PeerIdentity.generate("dev_bob")
        server, certificate_path = await self._start_server(
            alice,
            handshake_timeout=0.2,
        )
        started = asyncio.Event()

        async def blocked_handshake(*_args: object) -> None:
            started.set()
            await asyncio.Event().wait()

        with patch("cloud.p2p_transport._initiate_handshake", new=blocked_handshake):
            task = asyncio.create_task(connect_quic_peer(
                server.host,
                server.port,
                bob,
                expected_peer=alice.address,
                cafile=certificate_path,
            ))
            await asyncio.wait_for(started.wait(), timeout=2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        with self.assertRaises(P2PTransportError):
            await server.accept(timeout=1.0)
        self.assertEqual(server.status()["active_connections"], 0)

    async def test_abrupt_disconnect_fails_closed(self) -> None:
        server, _, _, alice_session, _bob_session = await self._pair()

        await server.close()

        with self.assertRaises(P2PConnectionClosed):
            await alice_session.receive()

    async def test_clean_close_restart_and_idempotent_lifecycle(self) -> None:
        server, _, _, alice_session, bob_session = await self._pair()

        self.assertIs(await server.start(), server)
        await bob_session.close()
        await bob_session.close()
        self.assertIsNone(await alice_session.receive())
        await server.close()
        await server.close()
        self.assertFalse(server.running)
        self.assertIs(await server.start(), server)
        with self.assertRaises(P2PTransportError):
            await server.accept(timeout=0.1)
        self.assertEqual(server.status()["active_connections"], 0)


if __name__ == "__main__":
    unittest.main()
