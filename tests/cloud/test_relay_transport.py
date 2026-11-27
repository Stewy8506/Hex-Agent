"""Ticket-paired stateless relay transport regressions."""

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
from websockets.exceptions import ConnectionClosed

from cloud.p2p_transport import (
    P2P_SUBPROTOCOL,
    P2PHandshakeError,
    P2PTransportError,
    PeerIdentity,
)
from cloud.relay_transport import (
    RelayPeerServer,
    connect_relay_peer,
    generate_relay_ticket,
)

try:
    from websockets.asyncio.client import connect as raw_connect
    from websockets.exceptions import InvalidStatus
except ImportError:
    from websockets.legacy.client import connect as raw_connect
    from websockets.exceptions import InvalidStatusCode as InvalidStatus


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
    key_path = root / "relay-key.pem"
    certificate_path = root / "relay-cert.pem"
    key_path.write_bytes(private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return key_path, certificate_path


class RelayTransportTests(unittest.IsolatedAsyncioTestCase):
    async def _start(self, **options: object) -> RelayPeerServer:
        server = RelayPeerServer(**options)
        await server.start()
        self.addAsyncCleanup(server.close)
        return server

    async def _raw_join(
        self,
        server: RelayPeerServer,
        ticket: str,
        role: str,
    ):
        websocket = await raw_connect(
            server.uri,
            subprotocols=[P2P_SUBPROTOCOL],
            open_timeout=2,
            close_timeout=1,
        )
        self.addAsyncCleanup(websocket.close)
        await websocket.send(json.dumps({"v": 1, "ticket": ticket, "role": role}))
        return websocket

    async def _pair(
        self,
        *,
        server: RelayPeerServer | None = None,
        ssl_context: ssl.SSLContext | None = None,
        bob_expected: PeerIdentity | None = None,
    ):
        server = server or await self._start()
        alice = PeerIdentity.generate("dev_alice")
        bob = PeerIdentity.generate("dev_bob")
        ticket = generate_relay_ticket()
        alice_task = asyncio.create_task(connect_relay_peer(
            server.uri,
            ticket,
            "initiator",
            alice,
            expected_peer=bob.address,
            ssl_context=ssl_context,
            timeout=10,
        ))
        bob_task = asyncio.create_task(connect_relay_peer(
            server.uri,
            ticket,
            "responder",
            bob,
            expected_peer=(bob_expected or alice).address,
            ssl_context=ssl_context,
            timeout=10,
        ))
        try:
            alice_session, bob_session = await asyncio.wait_for(
                asyncio.gather(alice_task, bob_task),
                timeout=15,
            )
        except BaseException:
            for task in (alice_task, bob_task):
                task.cancel()
            await asyncio.gather(alice_task, bob_task, return_exceptions=True)
            raise
        self.addAsyncCleanup(alice_session.close)
        self.addAsyncCleanup(bob_session.close)
        return server, alice, bob, alice_session, bob_session

    def test_ticket_is_unique_256_bit_urlsafe_capability(self) -> None:
        tickets = {generate_relay_ticket() for _ in range(64)}
        self.assertEqual(len(tickets), 64)
        for ticket in tickets:
            self.assertEqual(len(ticket), 43)
            self.assertEqual(len(base64.urlsafe_b64decode(ticket + "=")), 32)

    async def test_invalid_tickets_and_roles_fail_before_connecting(self) -> None:
        local = PeerIdentity.generate("dev_alice")
        remote = PeerIdentity.generate("dev_bob").address
        for ticket in ("", "short", "x" * 42, "x" * 44, "!" * 43):
            with self.subTest(ticket=ticket):
                with self.assertRaises(ValueError):
                    await connect_relay_peer(
                        "ws://127.0.0.1:1/hex/relay/v1",
                        ticket,
                        "initiator",
                        local,
                        expected_peer=remote,
                    )
        with self.assertRaises(ValueError):
            await connect_relay_peer(
                "ws://127.0.0.1:1/hex/relay/v1",
                generate_relay_ticket(),
                "observer",
                local,
                expected_peer=remote,
            )

    def test_non_loopback_server_requires_tls(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires TLS"):
            RelayPeerServer(host="0.0.0.0")

    async def test_non_loopback_plaintext_client_is_rejected(self) -> None:
        local = PeerIdentity.generate("dev_alice")
        remote = PeerIdentity.generate("dev_bob").address
        with self.assertRaisesRegex(ValueError, "loopback"):
            await connect_relay_peer(
                "ws://192.0.2.1:8787/hex/relay/v1",
                generate_relay_ticket(),
                "initiator",
                local,
                expected_peer=remote,
            )

    async def test_authenticated_bidirectional_transfer_and_clean_close(self) -> None:
        server, alice, bob, alice_session, bob_session = await self._pair()

        self.assertEqual(alice_session.remote_device_id, bob.device_id)
        self.assertEqual(bob_session.remote_device_id, alice.device_id)
        await alice_session.send(b"from-alice")
        await bob_session.send(b"from-bob")
        self.assertEqual(await bob_session.receive(), b"from-alice")
        self.assertEqual(await alice_session.receive(), b"from-bob")
        self.assertEqual(server.status()["persisted_bytes"], 0)
        self.assertGreater(server.status()["bytes_forwarded"], 0)

        await bob_session.close()
        self.assertIsNone(await alice_session.receive())

    async def test_large_encrypted_payload_round_trip(self) -> None:
        _, _, _, alice_session, bob_session = await self._pair()
        payload = b"relay-payload" * (128 * 1024)

        await alice_session.send(payload)

        self.assertEqual(await bob_session.receive(), payload)

    async def test_unexpected_remote_identity_is_rejected(self) -> None:
        impostor = PeerIdentity.generate("dev_impostor")
        with self.assertRaises(P2PHandshakeError):
            await self._pair(bob_expected=impostor)

    async def test_relay_forwards_opaque_binary_frames_unchanged(self) -> None:
        server = await self._start()
        ticket = generate_relay_ticket()
        initiator = await self._raw_join(server, ticket, "initiator")
        self.assertEqual(json.loads(await initiator.recv()), {"v": 1, "status": "waiting"})
        responder = await self._raw_join(server, ticket, "responder")
        self.assertEqual(json.loads(await responder.recv()), {"v": 1, "status": "paired"})
        self.assertEqual(json.loads(await initiator.recv()), {"v": 1, "status": "paired"})
        payload = b"\x00opaque-encrypted-frame\xff"

        await initiator.send(payload)

        self.assertEqual(await responder.recv(), payload)
        self.assertEqual(server.status()["bytes_forwarded"], len(payload))
        self.assertEqual(server.status()["persisted_bytes"], 0)

    async def test_text_data_and_malformed_control_messages_are_rejected(self) -> None:
        server = await self._start()
        ticket = generate_relay_ticket()
        initiator = await self._raw_join(server, ticket, "initiator")
        await initiator.recv()
        responder = await self._raw_join(server, ticket, "responder")
        await responder.recv()
        await initiator.recv()

        await initiator.send("plaintext control after pairing")

        with self.assertRaises(ConnectionClosed):
            await asyncio.wait_for(responder.recv(), timeout=2)
        with self.assertRaises(ConnectionClosed):
            await asyncio.wait_for(initiator.recv(), timeout=2)

        malformed = [
            '{"v":1,"v":1,"ticket":"' + generate_relay_ticket() + '","role":"initiator"}',
            '{"v":true,"ticket":"' + generate_relay_ticket() + '","role":"initiator"}',
            '{"v":1,"ticket":"' + generate_relay_ticket() + '","role":"observer"}',
            '{"v":1,"ticket":"bad","role":"initiator"}',
            '{"v":1,"ticket":"' + generate_relay_ticket() + '","role":"initiator","extra":1}',
            "[]",
            "null",
        ]
        for raw in malformed:
            with self.subTest(raw=raw[:80]):
                websocket = await raw_connect(
                    server.uri,
                    subprotocols=[P2P_SUBPROTOCOL],
                    open_timeout=2,
                    close_timeout=1,
                )
                self.addAsyncCleanup(websocket.close)
                await websocket.send(raw)
                with self.assertRaises(ConnectionClosed):
                    await asyncio.wait_for(websocket.recv(), timeout=2)

    async def test_duplicate_role_is_rejected_without_displacing_first_peer(self) -> None:
        server = await self._start()
        ticket = generate_relay_ticket()
        first = await self._raw_join(server, ticket, "initiator")
        self.assertEqual(json.loads(await first.recv())["status"], "waiting")
        duplicate = await self._raw_join(server, ticket, "initiator")

        with self.assertRaises(ConnectionClosed):
            await asyncio.wait_for(duplicate.recv(), timeout=2)
        responder = await self._raw_join(server, ticket, "responder")
        self.assertEqual(json.loads(await responder.recv())["status"], "paired")
        self.assertEqual(json.loads(await first.recv())["status"], "paired")

    async def test_missing_subprotocol_and_browser_origin_are_rejected(self) -> None:
        server = await self._start()
        with self.assertRaises(InvalidStatus):
            await raw_connect(server.uri, open_timeout=2)
        with self.assertRaises(InvalidStatus):
            await raw_connect(
                server.uri,
                subprotocols=[P2P_SUBPROTOCOL],
                origin="https://browser.invalid",
                open_timeout=2,
            )

    async def test_unpaired_connection_times_out_and_is_cleaned_up(self) -> None:
        server = await self._start(pair_timeout=0.05)
        local = PeerIdentity.generate("dev_alice")
        remote = PeerIdentity.generate("dev_bob").address

        with self.assertRaises(P2PTransportError):
            await connect_relay_peer(
                server.uri,
                generate_relay_ticket(),
                "initiator",
                local,
                expected_peer=remote,
                timeout=0.05,
            )

        await server.close()
        self.assertEqual(server.status()["active_connections"], 0)
        self.assertEqual(server.status()["pending_pairs"], 0)

    async def test_cancelled_handshake_closes_both_relay_connections(self) -> None:
        server = await self._start()
        ticket = generate_relay_ticket()
        responder = await self._raw_join(server, ticket, "responder")
        self.assertEqual(json.loads(await responder.recv())["status"], "waiting")
        started = asyncio.Event()

        async def blocked_handshake(*_args: object) -> object:
            started.set()
            await asyncio.Event().wait()

        alice = PeerIdentity.generate("dev_alice")
        bob = PeerIdentity.generate("dev_bob")
        with patch("cloud.relay_transport._initiate_handshake", new=blocked_handshake):
            task = asyncio.create_task(connect_relay_peer(
                server.uri,
                ticket,
                "initiator",
                alice,
                expected_peer=bob.address,
                timeout=5,
            ))
            await asyncio.wait_for(started.wait(), timeout=2)
            self.assertEqual(json.loads(await responder.recv())["status"], "paired")
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        await server.close()
        self.assertEqual(server.status()["active_connections"], 0)

    async def test_wss_round_trip_with_private_ca(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="hex_relay_wss_test_")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        key_path, certificate_path = _write_test_certificate(root)
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(certificate_path, key_path)
        client_context = ssl.create_default_context(cafile=certificate_path)
        server = await self._start(ssl_context=server_context)

        _, _, _, alice_session, bob_session = await self._pair(
            server=server,
            ssl_context=client_context,
        )

        self.assertTrue(server.uri.startswith("wss://"))
        await alice_session.send(b"verified relay")
        self.assertEqual(await bob_session.receive(), b"verified relay")

    async def test_untrusted_wss_certificate_is_rejected(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="hex_relay_wss_bad_ca_")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        key_path, certificate_path = _write_test_certificate(root)
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(certificate_path, key_path)
        server = await self._start(ssl_context=server_context)
        alice = PeerIdentity.generate("dev_alice")
        bob = PeerIdentity.generate("dev_bob")

        with self.assertRaises(P2PTransportError):
            await connect_relay_peer(
                server.uri,
                generate_relay_ticket(),
                "initiator",
                alice,
                expected_peer=bob.address,
                timeout=2,
            )

    async def test_restart_does_not_expose_stale_pairs(self) -> None:
        server = RelayPeerServer()
        await server.start()
        first_uri = server.uri
        await server.close()
        self.assertIs(await server.start(), server)
        self.addAsyncCleanup(server.close)

        self.assertNotEqual(server.uri, first_uri)
        self.assertEqual(server.status()["active_connections"], 0)
        self.assertEqual(server.status()["pending_pairs"], 0)
        self.assertEqual(server.status()["pairs_completed"], 0)


if __name__ == "__main__":
    unittest.main()
