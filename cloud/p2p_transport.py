"""Authenticated encrypted peer transport with in-memory, WebSocket, and QUIC networks."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import math
import secrets
import socket
import ssl
from collections.abc import Collection
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit

from aioquic.asyncio import QuicConnectionProtocol
from aioquic.asyncio.server import QuicServer as _AioquicQuicServer
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.connection import QuicConnection
from aioquic.quic.events import ConnectionTerminated, HandshakeCompleted
from aioquic.quic.packet import QuicErrorCode
from websockets.exceptions import ConnectionClosed, WebSocketException

try:
    from websockets.asyncio.client import connect as _websocket_connect
    from websockets.asyncio.server import serve as _websocket_serve

    class _NoRedirectWebSocketConnect(_websocket_connect):
        def process_redirect(self, exc: Exception) -> Exception:
            return exc

    _MODERN_WEBSOCKETS = True
except ImportError:
    from websockets.legacy.client import connect as _websocket_connect
    from websockets.legacy.server import serve as _websocket_serve

    _NoRedirectWebSocketConnect = _websocket_connect
    _MODERN_WEBSOCKETS = False

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .sync_contract import SyncEnvelope, canonical_json_bytes

_VERSION = 1
_HANDSHAKE_CONTEXT = b"hex-p2p-handshake-v1\x00"
_KEY_CONTEXT = b"hex-p2p-session-keys-v1"
_FRAME_MAGIC = b"HXP1"
_FRAME_HEADER_SIZE = len(_FRAME_MAGIC) + 1 + 8
_TAG_SIZE = 16
_MAX_SEQUENCE = (1 << 64) - 1
MAX_FRAME_BYTES = 64 * 1024 * 1024
P2P_SUBPROTOCOL = "hex-p2p-v1"
_QUIC_FRAME_LENGTH_SIZE = 4
_QUIC_IDLE_TIMEOUT = 60.0
_MAX_HANDSHAKE_BYTES = 16 * 1024
_DATA_FRAME = 1
_CLOSE_FRAME = 2


class P2PTransportError(Exception):
    """Base error for peer transport failures."""


class P2PHandshakeError(P2PTransportError):
    """Raised when peer authentication or handshake validation fails."""


class P2PProtocolError(P2PTransportError):
    """Raised when an encrypted transport frame is invalid."""


class P2PConnectionClosed(P2PTransportError):
    """Raised when an operation uses a closed peer connection."""


def _validate_device_id(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError("Peer device id must be a non-empty bounded string.")
    if any(ord(char) < 32 or char.isspace() for char in value):
        raise ValueError("Peer device id cannot contain whitespace or controls.")
    return value


def _encode_bytes(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_bytes(value: Any, *, length: int, label: str) -> bytes:
    if not isinstance(value, str) or len(value) > 256:
        raise P2PHandshakeError(f"Invalid {label}.")
    try:
        raw = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
    except (binascii.Error, ValueError) as exc:
        raise P2PHandshakeError(f"Invalid {label}.") from exc
    if len(raw) != length:
        raise P2PHandshakeError(f"Invalid {label}.")
    return raw


def _normalize_signing_public_key(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 80:
        raise ValueError("Signing public key must be an Ed25519 public key.")
    raw = value[len("ed25519:") :] if value.startswith("ed25519:") else value
    if len(raw) != 64:
        raise ValueError("Signing public key must be an Ed25519 public key.")
    try:
        key_bytes = bytes.fromhex(raw)
    except ValueError as exc:
        raise ValueError("Signing public key must be an Ed25519 public key.") from exc
    if len(key_bytes) != 32:
        raise ValueError("Signing public key must be an Ed25519 public key.")
    return "ed25519:" + key_bytes.hex()


def _load_signing_public_key(value: str) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(bytes.fromhex(value[len("ed25519:") :]))


def _handshake_device_id(value: Any) -> str:
    try:
        return _validate_device_id(value)
    except ValueError as exc:
        raise P2PHandshakeError("Invalid peer device id.") from exc


def _handshake_signing_public_key(value: Any) -> str:
    try:
        return _normalize_signing_public_key(value)
    except ValueError as exc:
        raise P2PHandshakeError("Invalid peer signing public key.") from exc


def _verify_signature(public_key: Ed25519PublicKey, signature: bytes, message: bytes) -> None:
    try:
        public_key.verify(signature, message)
    except InvalidSignature as exc:
        raise P2PHandshakeError("Peer handshake signature verification failed.") from exc


@dataclass(frozen=True, slots=True)
class PeerAddress:
    device_id: str
    signing_public_key: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "device_id", _validate_device_id(self.device_id))
        object.__setattr__(
            self, "signing_public_key", _normalize_signing_public_key(self.signing_public_key)
        )


@dataclass(frozen=True, slots=True)
class PeerIdentity:
    device_id: str
    private_key: Ed25519PrivateKey = field(repr=False)

    def __post_init__(self) -> None:
        _validate_device_id(self.device_id)
        if not isinstance(self.private_key, Ed25519PrivateKey):
            raise ValueError("PeerIdentity requires an Ed25519 private key.")

    @classmethod
    def generate(cls, device_id: str) -> "PeerIdentity":
        return cls(_validate_device_id(device_id), Ed25519PrivateKey.generate())

    @classmethod
    def from_device_identity(cls, identity: Any, device_record_id: str) -> "PeerIdentity":
        if identity.private_key is None:
            raise ValueError("DeviceIdentity has no private key.")
        public_key = identity.private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        expected = "ed25519:" + public_key.hex()
        if not hmac.compare_digest(
            expected, _normalize_signing_public_key(identity.device_pubkey)
        ):
            raise ValueError("DeviceIdentity signing key does not match its public key.")
        return cls(_validate_device_id(device_record_id), identity.private_key)

    @property
    def signing_public_key(self) -> str:
        raw = self.private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        return "ed25519:" + raw.hex()

    @property
    def address(self) -> PeerAddress:
        return PeerAddress(self.device_id, self.signing_public_key)

    def sign(self, message: bytes) -> bytes:
        return self.private_key.sign(message)


class P2PChannel(Protocol):
    async def send(self, frame: bytes) -> None: ...

    async def receive(self) -> bytes | None: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class _Hello:
    raw: dict[str, Any]
    nonce: bytes
    ephemeral_public_key: X25519PublicKey
    device_id: str
    signing_public_key: str
    ed25519_public_key: Ed25519PublicKey


@dataclass(frozen=True, slots=True)
class _Response:
    raw: dict[str, Any]
    nonce: bytes
    ephemeral_public_key: X25519PublicKey
    device_id: str
    signing_public_key: str


def _validate_version_and_role(value: dict[str, Any], role: str) -> None:
    if type(value.get("v")) is not int or value["v"] != _VERSION:
        raise P2PHandshakeError("Unsupported peer handshake version.")
    if value.get("role") != role:
        raise P2PHandshakeError("Unexpected peer handshake role.")


def _parse_hello(value: Any) -> _Hello:
    expected_keys = {
        "v", "role", "nonce", "ephemeral_public", "device_id", "signing_public"
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise P2PHandshakeError("Malformed peer hello.")
    _validate_version_and_role(value, "initiator")
    nonce = _decode_bytes(value["nonce"], length=32, label="handshake nonce")
    ephemeral_raw = _decode_bytes(
        value["ephemeral_public"], length=32, label="ephemeral public key"
    )
    device_id = _handshake_device_id(value["device_id"])
    signing_public_key = _handshake_signing_public_key(value["signing_public"])
    return _Hello(
        raw=value,
        nonce=nonce,
        ephemeral_public_key=X25519PublicKey.from_public_bytes(ephemeral_raw),
        device_id=device_id,
        signing_public_key=signing_public_key,
        ed25519_public_key=_load_signing_public_key(signing_public_key),
    )


def _parse_response(value: Any, hello: _Hello) -> _Response:
    expected_keys = {
        "v", "role", "nonce", "ephemeral_public", "device_id", "signing_public",
        "signature",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise P2PHandshakeError("Malformed peer response.")
    _validate_version_and_role(value, "responder")
    nonce = _decode_bytes(value["nonce"], length=32, label="handshake nonce")
    if not hmac.compare_digest(nonce, hello.nonce):
        raise P2PHandshakeError("Peer response nonce does not match the hello.")
    ephemeral_raw = _decode_bytes(
        value["ephemeral_public"], length=32, label="ephemeral public key"
    )
    device_id = _handshake_device_id(value["device_id"])
    signing_public_key = _handshake_signing_public_key(value["signing_public"])
    signature = _decode_bytes(value["signature"], length=64, label="handshake signature")
    response_body = {key: item for key, item in value.items() if key != "signature"}
    transcript = _handshake_transcript(hello.raw, response_body)
    ed25519_public_key = _load_signing_public_key(signing_public_key)
    _verify_signature(ed25519_public_key, signature, transcript)
    return _Response(
        raw=response_body,
        nonce=nonce,
        ephemeral_public_key=X25519PublicKey.from_public_bytes(ephemeral_raw),
        device_id=device_id,
        signing_public_key=signing_public_key,
    )


def _parse_finish(value: Any, hello: _Hello, response_body: dict[str, Any]) -> bytes:
    expected_keys = {"v", "role", "nonce", "signature"}
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise P2PHandshakeError("Malformed peer finish.")
    _validate_version_and_role(value, "initiator")
    nonce = _decode_bytes(value["nonce"], length=32, label="handshake nonce")
    if not hmac.compare_digest(nonce, hello.nonce):
        raise P2PHandshakeError("Peer finish nonce does not match the hello.")
    signature = _decode_bytes(value["signature"], length=64, label="handshake signature")
    transcript = _handshake_transcript(hello.raw, response_body)
    _verify_signature(hello.ed25519_public_key, signature, transcript)
    return transcript


def _handshake_transcript(hello: dict[str, Any], response: dict[str, Any]) -> bytes:
    return hashlib.sha256(
        _HANDSHAKE_CONTEXT + canonical_json_bytes(hello) + canonical_json_bytes(response)
    ).digest()


def _check_expected_peer(
    device_id: str, signing_public_key: str, expected: PeerAddress | None
) -> None:
    if expected is None:
        return
    if device_id != expected.device_id or not hmac.compare_digest(
        signing_public_key, expected.signing_public_key
    ):
        raise P2PHandshakeError("Connected peer does not match the expected identity.")


def _derive_session_keys(shared_secret: bytes, transcript: bytes) -> tuple[bytes, bytes]:
    material = HKDF(
        algorithm=hashes.SHA256(),
        length=64,
        salt=transcript,
        info=_KEY_CONTEXT,
    ).derive(shared_secret)
    return material[:32], material[32:]


def _load_transport_json(raw: bytes) -> Any:
    def object_from_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate JSON key.")
            value[key] = item
        return value

    def reject_constant(value: str) -> Any:
        raise ValueError(f"Non-standard JSON constant: {value}")

    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=object_from_pairs,
        parse_constant=reject_constant,
    )


async def _send_handshake(channel: P2PChannel, value: dict[str, Any]) -> None:
    raw = canonical_json_bytes(value)
    if len(raw) > _MAX_HANDSHAKE_BYTES:
        raise P2PHandshakeError("Peer handshake message is too large.")
    await channel.send(raw)


async def _receive_handshake(channel: P2PChannel) -> Any:
    raw = await channel.receive()
    if raw is None:
        raise P2PHandshakeError("Peer closed the connection during handshake.")
    if len(raw) > _MAX_HANDSHAKE_BYTES:
        raise P2PHandshakeError("Peer handshake message is too large.")
    try:
        return _load_transport_json(raw)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise P2PHandshakeError("Peer handshake message is not valid JSON.") from exc


async def _initiate_handshake(
    channel: P2PChannel,
    local: PeerIdentity,
    expected_peer: PeerAddress | None,
) -> SecurePeerSession:
    ephemeral_private_key = X25519PrivateKey.generate()
    ephemeral_public_key = ephemeral_private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    nonce = secrets.token_bytes(32)
    hello_value = {
        "v": _VERSION,
        "role": "initiator",
        "nonce": _encode_bytes(nonce),
        "ephemeral_public": _encode_bytes(ephemeral_public_key),
        "device_id": local.device_id,
        "signing_public": local.signing_public_key,
    }
    await _send_handshake(channel, hello_value)
    response = _parse_response(await _receive_handshake(channel), _parse_hello(hello_value))
    _check_expected_peer(response.device_id, response.signing_public_key, expected_peer)
    transcript = _handshake_transcript(hello_value, response.raw)
    await _send_handshake(
        channel,
        {
            "v": _VERSION,
            "role": "initiator",
            "nonce": _encode_bytes(nonce),
            "signature": _encode_bytes(local.sign(transcript)),
        },
    )
    shared_secret = ephemeral_private_key.exchange(response.ephemeral_public_key)
    initiator_send_key, responder_send_key = _derive_session_keys(shared_secret, transcript)
    return SecurePeerSession(
        channel=channel,
        send_key=initiator_send_key,
        receive_key=responder_send_key,
        remote=PeerAddress(response.device_id, response.signing_public_key),
    )


async def _respond_handshake(
    channel: P2PChannel,
    local: PeerIdentity,
    expected_peer: PeerAddress | None,
) -> SecurePeerSession:
    hello = _parse_hello(await _receive_handshake(channel))
    _check_expected_peer(hello.device_id, hello.signing_public_key, expected_peer)
    ephemeral_private_key = X25519PrivateKey.generate()
    ephemeral_public_key = ephemeral_private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    response_body = {
        "v": _VERSION,
        "role": "responder",
        "nonce": _encode_bytes(hello.nonce),
        "ephemeral_public": _encode_bytes(ephemeral_public_key),
        "device_id": local.device_id,
        "signing_public": local.signing_public_key,
    }
    transcript = _handshake_transcript(hello.raw, response_body)
    await _send_handshake(
        channel,
        {**response_body, "signature": _encode_bytes(local.sign(transcript))},
    )
    _parse_finish(await _receive_handshake(channel), hello, response_body)
    shared_secret = ephemeral_private_key.exchange(hello.ephemeral_public_key)
    initiator_send_key, responder_send_key = _derive_session_keys(shared_secret, transcript)
    return SecurePeerSession(
        channel=channel,
        send_key=responder_send_key,
        receive_key=initiator_send_key,
        remote=PeerAddress(hello.device_id, hello.signing_public_key),
    )


class _FrameCodec:
    def __init__(self, key: bytes) -> None:
        self._aesgcm = AESGCM(key)

    def seal(self, kind: int, sequence: int, plaintext: bytes) -> bytes:
        if sequence > _MAX_SEQUENCE:
            raise P2PProtocolError("Peer transport sequence exhausted.")
        header = _FRAME_MAGIC + bytes((kind,)) + sequence.to_bytes(8, "big")
        nonce = sequence.to_bytes(12, "big")
        return header + self._aesgcm.encrypt(nonce, plaintext, header)

    def open(self, frame: bytes, expected_sequence: int) -> tuple[int, bytes]:
        if len(frame) < _FRAME_HEADER_SIZE + _TAG_SIZE or len(frame) > MAX_FRAME_BYTES:
            raise P2PProtocolError("Invalid encrypted frame size.")
        header = frame[:_FRAME_HEADER_SIZE]
        if header[: len(_FRAME_MAGIC)] != _FRAME_MAGIC:
            raise P2PProtocolError("Invalid encrypted frame magic.")
        sequence = int.from_bytes(header[len(_FRAME_MAGIC) + 1 :], "big")
        # Reliable transports preserve order; duplicates and gaps fail closed.
        if sequence != expected_sequence:
            raise P2PProtocolError("Encrypted frame is replayed, duplicated, or out of order.")
        kind = header[len(_FRAME_MAGIC)]
        try:
            plaintext = self._aesgcm.decrypt(
                sequence.to_bytes(12, "big"), frame[_FRAME_HEADER_SIZE:], header
            )
        except InvalidTag as exc:
            raise P2PProtocolError("Encrypted frame authentication failed.") from exc
        if kind not in {_DATA_FRAME, _CLOSE_FRAME}:
            raise P2PProtocolError("Unknown encrypted frame type.")
        if kind == _CLOSE_FRAME and plaintext:
            raise P2PProtocolError("Close frame cannot contain data.")
        return kind, plaintext


class SecurePeerSession:
    def __init__(
        self,
        *,
        channel: P2PChannel,
        send_key: bytes,
        receive_key: bytes,
        remote: PeerAddress,
    ) -> None:
        self._channel = channel
        self._send_codec = _FrameCodec(send_key)
        self._receive_codec = _FrameCodec(receive_key)
        self._remote = remote
        self._send_sequence = 0
        self._receive_sequence = 0
        self._send_lock = asyncio.Lock()
        self._receive_lock = asyncio.Lock()
        self._closed = False
        self._bytes_sent = 0
        self._bytes_received = 0

    @property
    def remote_device_id(self) -> str:
        return self._remote.device_id

    @property
    def remote_signing_public_key(self) -> str:
        return self._remote.signing_public_key

    @property
    def closed(self) -> bool:
        return self._closed

    def status(self) -> dict[str, Any]:
        return {
            "remote_device_id": self.remote_device_id,
            "remote_signing_public_key": self.remote_signing_public_key,
            "closed": self._closed,
            "bytes_sent": self._bytes_sent,
            "bytes_received": self._bytes_received,
            "send_sequence": self._send_sequence,
            "receive_sequence": self._receive_sequence,
        }

    async def send(self, payload: bytes) -> None:
        if not isinstance(payload, bytes):
            raise TypeError("Peer payload must be bytes.")
        if len(payload) + _FRAME_HEADER_SIZE + _TAG_SIZE > MAX_FRAME_BYTES:
            raise ValueError("Peer payload exceeds the maximum frame size.")
        async with self._send_lock:
            if self._closed:
                raise P2PConnectionClosed("Peer connection is closed.")
            frame = self._send_codec.seal(_DATA_FRAME, self._send_sequence, payload)
            await self._channel.send(frame)
            self._send_sequence += 1
            self._bytes_sent += len(payload)

    async def receive(self) -> bytes | None:
        async with self._receive_lock:
            if self._closed:
                raise P2PConnectionClosed("Peer connection is closed.")
            frame = await self._channel.receive()
            if frame is None:
                self._closed = True
                raise P2PConnectionClosed("Peer connection closed without a close frame.")
            kind, payload = self._receive_codec.open(frame, self._receive_sequence)
            self._receive_sequence += 1
            if kind == _CLOSE_FRAME:
                self._closed = True
                self._channel.close()
                return None
            self._bytes_received += len(payload)
            return payload

    async def send_envelope(self, envelope: SyncEnvelope) -> None:
        await self.send(canonical_json_bytes(envelope.to_dict()))

    async def receive_envelope(self) -> SyncEnvelope | None:
        payload = await self.receive()
        if payload is None:
            return None
        try:
            value = _load_transport_json(payload)
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise P2PProtocolError("Sync envelope is not valid UTF-8 JSON.") from exc
        return SyncEnvelope.from_dict(value)

    async def close(self) -> None:
        async with self._send_lock:
            if self._closed:
                self._channel.close()
                return
            try:
                if self._send_sequence <= _MAX_SEQUENCE:
                    frame = self._send_codec.seal(
                        _CLOSE_FRAME, self._send_sequence, b""
                    )
                    await self._channel.send(frame)
                    self._send_sequence += 1
            except P2PConnectionClosed:
                pass
            finally:
                self._closed = True
                self._channel.close()

    async def __aenter__(self) -> "SecurePeerSession":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()


FrameTransform = Callable[[bytes], bytes]


class _MemoryChannel:
    def __init__(
        self,
        outgoing: asyncio.Queue[bytes | None],
        incoming: asyncio.Queue[bytes | None],
        transform: FrameTransform | None = None,
    ) -> None:
        self._outgoing = outgoing
        self._incoming = incoming
        self._transform = transform
        self._closed = False

    async def send(self, frame: bytes) -> None:
        if self._closed:
            raise P2PConnectionClosed("In-memory peer channel is closed.")
        if self._transform is not None:
            frame = self._transform(frame)
        await self._outgoing.put(frame)

    async def receive(self) -> bytes | None:
        return await self._incoming.get()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._outgoing.put_nowait(None)


class InMemoryNetwork:
    def __init__(self, *, frame_transform: FrameTransform | None = None) -> None:
        self._frame_transform = frame_transform
        self._endpoints: dict[str, InMemoryEndpoint] = {}

    def endpoint(self, identity: PeerIdentity) -> "InMemoryEndpoint":
        if identity.device_id in self._endpoints:
            raise ValueError("A peer endpoint with this device id already exists.")
        endpoint = InMemoryEndpoint(self, identity)
        self._endpoints[identity.device_id] = endpoint
        return endpoint


class InMemoryEndpoint:
    def __init__(self, network: InMemoryNetwork, identity: PeerIdentity) -> None:
        self._network = network
        self.identity = identity
        self._sessions: asyncio.Queue[SecurePeerSession] = asyncio.Queue()

    async def connect(
        self,
        remote: "InMemoryEndpoint",
        *,
        expected_peer: PeerAddress | None = None,
        timeout: float = 5.0,
    ) -> SecurePeerSession:
        if remote._network is not self._network:
            raise ValueError("Peer endpoints belong to different in-memory networks.")
        if remote is self or remote.identity.device_id == self.identity.device_id:
            raise ValueError("A peer cannot connect to itself.")
        expected_peer = expected_peer or remote.identity.address
        initiator_to_responder: asyncio.Queue[bytes | None] = asyncio.Queue()
        responder_to_initiator: asyncio.Queue[bytes | None] = asyncio.Queue()
        initiator_channel = _MemoryChannel(
            initiator_to_responder,
            responder_to_initiator,
            self._network._frame_transform,
        )
        responder_channel = _MemoryChannel(
            responder_to_initiator,
            initiator_to_responder,
            self._network._frame_transform,
        )

        async def handshake_pair() -> tuple[SecurePeerSession, SecurePeerSession]:
            responder_task = asyncio.create_task(
                _respond_handshake(responder_channel, remote.identity, self.identity.address)
            )
            initiator_task = asyncio.create_task(
                _initiate_handshake(initiator_channel, self.identity, expected_peer)
            )
            tasks = (initiator_task, responder_task)
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
                for task in done:
                    task.result()
                return await initiator_task, await responder_task
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        try:
            initiator_session, responder_session = await asyncio.wait_for(
                handshake_pair(), timeout=timeout
            )
        except BaseException:
            initiator_channel.close()
            responder_channel.close()
            raise
        await remote._sessions.put(responder_session)
        return initiator_session

    async def accept(self) -> SecurePeerSession:
        return await self._sessions.get()


def _validate_network_timeout(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{label} must be finite and positive.")
    return float(value)


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_websocket_uri(uri: Any) -> Any:
    if not isinstance(uri, str) or not uri or len(uri) > 2048:
        raise ValueError("WebSocket URI must be a bounded string.")
    try:
        parsed = urlsplit(uri)
        host = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise ValueError("Invalid WebSocket URI.") from exc
    if parsed.scheme not in {"ws", "wss"} or not host:
        raise ValueError("WebSocket URI must use ws or wss with a host.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("WebSocket URI cannot contain user information.")
    if parsed.query or parsed.fragment:
        raise ValueError("WebSocket URI cannot contain query parameters or fragments.")
    if parsed.scheme == "ws" and not _is_loopback_host(host):
        raise ValueError("Plaintext WebSocket transport is restricted to loopback hosts.")
    return parsed


def _validate_client_ssl_context(context: ssl.SSLContext | None, scheme: str) -> None:
    if context is None:
        return
    if not isinstance(context, ssl.SSLContext):
        raise ValueError("ssl_context must be an SSLContext.")
    if scheme == "ws":
        raise ValueError("ssl_context requires a wss URI.")
    if context.verify_mode != ssl.CERT_REQUIRED:
        raise ValueError("ssl_context must require certificate verification.")


class _WebSocketChannel:
    def __init__(self, websocket: Any) -> None:
        self._websocket = websocket
        self._closed = False
        self._close_task: asyncio.Task[Any] | None = None

    async def send(self, frame: bytes) -> None:
        if self._closed:
            raise P2PConnectionClosed("WebSocket peer channel is closed.")
        try:
            await self._websocket.send(frame)
        except (ConnectionClosed, OSError) as exc:
            self._closed = True
            raise P2PConnectionClosed("WebSocket peer channel closed during send.") from exc

    async def receive(self) -> bytes | None:
        if self._closed:
            raise P2PConnectionClosed("WebSocket peer channel is closed.")
        try:
            raw = await self._websocket.recv()
        except ConnectionClosed:
            self._closed = True
            return None
        except OSError as exc:
            self._closed = True
            raise P2PConnectionClosed("WebSocket peer channel failed during receive.") from exc
        if raw is None:
            self._closed = True
            return None
        if not isinstance(raw, bytes):
            raise P2PProtocolError("WebSocket peer messages must be binary frames.")
        if len(raw) > MAX_FRAME_BYTES:
            raise P2PProtocolError("WebSocket peer message exceeds the frame limit.")
        return raw

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._close_task = asyncio.create_task(self._websocket.close())

    async def wait_closed(self) -> None:
        if self._close_task is not None:
            with suppress(ConnectionClosed, OSError):
                await self._close_task
        with suppress(ConnectionClosed, OSError):
            await self._websocket.wait_closed()


class WebSocketPeerServer:
    """Accept authenticated device peers over loopback WS or verified WSS."""

    def __init__(
        self,
        identity: PeerIdentity,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        ssl_context: ssl.SSLContext | None = None,
        allowed_peers: Collection[PeerAddress] | None = None,
        max_connections: int = 32,
        handshake_timeout: float = 10.0,
    ) -> None:
        if not isinstance(identity, PeerIdentity):
            raise ValueError("WebSocketPeerServer requires a PeerIdentity.")
        if not isinstance(host, str) or not host or len(host) > 255:
            raise ValueError("host must be a bounded string.")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("port must be an integer between 0 and 65535.")
        if ssl_context is not None and not isinstance(ssl_context, ssl.SSLContext):
            raise ValueError("ssl_context must be an SSLContext.")
        if ssl_context is None and not _is_loopback_host(host):
            raise ValueError("A non-loopback WebSocket server requires TLS.")
        if (
            isinstance(max_connections, bool)
            or not isinstance(max_connections, int)
            or not 1 <= max_connections <= 256
        ):
            raise ValueError("max_connections must be between 1 and 256.")
        self.identity = identity
        self.host = host
        self.port = port
        self.ssl_context = ssl_context
        self.allowed_peers = (
            None if allowed_peers is None else frozenset(allowed_peers)
        )
        if self.allowed_peers is not None and any(
            not isinstance(peer, PeerAddress) for peer in self.allowed_peers
        ):
            raise ValueError("allowed_peers must contain PeerAddress values.")
        self.max_connections = max_connections
        self.handshake_timeout = _validate_network_timeout(
            handshake_timeout, "handshake_timeout"
        )
        self._server: Any = None
        self._sessions: asyncio.Queue[SecurePeerSession] = asyncio.Queue(
            maxsize=max_connections
        )
        self._channels: set[_WebSocketChannel] = set()
        self._accepted = 0
        self._closing = False
        self._generation = 0
        self._lifecycle_lock = asyncio.Lock()

    @property
    def uri(self) -> str:
        if self._server is None or not self._server.sockets:
            raise P2PTransportError("WebSocket peer server is not running.")
        address = self._server.sockets[0].getsockname()
        host = str(address[0])
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        scheme = "wss" if self.ssl_context is not None else "ws"
        return f"{scheme}://{host}:{address[1]}/hex/p2p/v1"

    @property
    def running(self) -> bool:
        return self._server is not None and not self._closing

    def status(self) -> dict[str, Any]:
        addresses: list[dict[str, Any]] = []
        if self._server is not None:
            for sock in self._server.sockets:
                address = sock.getsockname()
                addresses.append({"host": str(address[0]), "port": address[1]})
        return {
            "transport": "websocket",
            "running": self.running,
            "tls": self.ssl_context is not None,
            "subprotocol": P2P_SUBPROTOCOL,
            "addresses": addresses,
            "active_connections": len(self._channels),
            "accepted_sessions": self._accepted,
        }

    async def start(self) -> "WebSocketPeerServer":
        async with self._lifecycle_lock:
            if self.running:
                return self
            self._closing = False
            self._generation += 1
            try:
                self._server = await _websocket_serve(
                    self._handle_connection,
                    self.host,
                    self.port,
                    ssl=self.ssl_context,
                    subprotocols=[P2P_SUBPROTOCOL],
                    origins=[None],
                    compression=None,
                    max_size=MAX_FRAME_BYTES,
                    max_queue=16,
                    open_timeout=self.handshake_timeout,
                    close_timeout=min(5.0, self.handshake_timeout),
                )
            except (OSError, ValueError) as exc:
                self._server = None
                raise P2PTransportError("Unable to start WebSocket peer server.") from exc
            return self

    async def accept(
        self,
        *,
        expected_peer: PeerAddress | None = None,
        timeout: float = 30.0,
    ) -> SecurePeerSession:
        if not self.running:
            raise P2PTransportError("WebSocket peer server is not running.")
        if expected_peer is not None and not isinstance(expected_peer, PeerAddress):
            raise ValueError("expected_peer must be a PeerAddress.")
        wait = _validate_network_timeout(timeout, "timeout")
        try:
            session = await asyncio.wait_for(self._sessions.get(), timeout=wait)
        except asyncio.TimeoutError as exc:
            raise P2PTransportError("Timed out waiting for a WebSocket peer.") from exc
        remote = PeerAddress(
            session.remote_device_id, session.remote_signing_public_key
        )
        if expected_peer is not None and remote != expected_peer:
            await session.close()
            raise P2PHandshakeError("Accepted peer does not match the expected identity.")
        return session

    async def close(self) -> None:
        async with self._lifecycle_lock:
            self._closing = True
            server, self._server = self._server, None
            if server is not None:
                closing = server.close()
                if closing is not None:
                    await closing
                await server.wait_closed()
            channels = list(self._channels)
            for channel in channels:
                channel.close()
            await asyncio.gather(
                *(channel.wait_closed() for channel in channels),
                return_exceptions=True,
            )
            self._channels.clear()
            self._sessions = asyncio.Queue(maxsize=self.max_connections)

    async def _handle_connection(self, websocket: Any) -> None:
        generation = self._generation
        channel = _WebSocketChannel(websocket)
        if self._closing or len(self._channels) >= self.max_connections:
            channel.close()
            await channel.wait_closed()
            return
        if websocket.subprotocol != P2P_SUBPROTOCOL:
            channel.close()
            await channel.wait_closed()
            return
        self._channels.add(channel)
        try:
            session = await asyncio.wait_for(
                _respond_handshake(channel, self.identity, None),
                timeout=self.handshake_timeout,
            )
            remote = PeerAddress(
                session.remote_device_id, session.remote_signing_public_key
            )
            if self.allowed_peers is not None and remote not in self.allowed_peers:
                raise P2PHandshakeError("Peer is not allowed by this server.")
            if self._closing or generation != self._generation:
                await session.close()
                return
            await self._sessions.put(session)
            self._accepted += 1
            await websocket.wait_closed()
        except asyncio.CancelledError:
            channel.close()
            raise
        except Exception:
            channel.close()
            with suppress(Exception):
                await asyncio.wait_for(channel.wait_closed(), timeout=1.0)
        finally:
            self._channels.discard(channel)

    async def __aenter__(self) -> "WebSocketPeerServer":
        return await self.start()

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()


async def connect_websocket_peer(
    uri: str,
    local: PeerIdentity,
    *,
    expected_peer: PeerAddress,
    ssl_context: ssl.SSLContext | None = None,
    timeout: float = 10.0,
) -> SecurePeerSession:
    """Connect to a pinned device peer over loopback WS or certificate-verified WSS."""

    if not isinstance(local, PeerIdentity):
        raise ValueError("local must be a PeerIdentity.")
    if not isinstance(expected_peer, PeerAddress):
        raise ValueError("expected_peer must be a PeerAddress.")
    parsed = _validate_websocket_uri(uri)
    _validate_client_ssl_context(ssl_context, parsed.scheme)
    wait = _validate_network_timeout(timeout, "timeout")
    options: dict[str, Any] = {
        "subprotocols": [P2P_SUBPROTOCOL],
        "compression": None,
        "max_size": MAX_FRAME_BYTES,
        "max_queue": 16,
        "open_timeout": wait,
        "close_timeout": min(5.0, wait),
        "ssl": ssl_context,
    }
    if _MODERN_WEBSOCKETS:
        options["proxy"] = None
    try:
        websocket = await _NoRedirectWebSocketConnect(uri, **options)
    except (OSError, ValueError, asyncio.TimeoutError, WebSocketException) as exc:
        raise P2PTransportError("Unable to connect to WebSocket peer.") from exc
    channel = _WebSocketChannel(websocket)
    authenticated = False
    try:
        if websocket.subprotocol != P2P_SUBPROTOCOL:
            raise P2PHandshakeError("Peer server does not support the HEX P2P subprotocol.")
        session = await asyncio.wait_for(
            _initiate_handshake(channel, local, expected_peer),
            timeout=wait,
        )
        authenticated = True
        return session
    except P2PTransportError:
        raise
    except asyncio.TimeoutError as exc:
        raise P2PTransportError("WebSocket peer handshake timed out.") from exc
    except Exception as exc:
        raise P2PTransportError("WebSocket peer handshake failed.") from exc
    finally:
        if not authenticated:
            channel.close()
            with suppress(Exception):
                await asyncio.wait_for(
                    channel.wait_closed(), timeout=min(5.0, wait)
                )


class _QuicListener(_AioquicQuicServer):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._closed = asyncio.get_running_loop().create_future()

    def connection_lost(self, exc: Exception | None) -> None:
        if not self._closed.done():
            self._closed.set_result(None)
        super().connection_lost(exc)

    async def wait_closed(self) -> None:
        await self._closed


async def _quic_serve(
    host: str,
    port: int,
    *,
    configuration: QuicConfiguration,
    create_protocol: Callable[..., QuicConnectionProtocol],
) -> _QuicListener:
    loop = asyncio.get_running_loop()
    _, protocol = await loop.create_datagram_endpoint(
        lambda: _QuicListener(
            configuration=configuration,
            create_protocol=create_protocol,
        ),
        local_addr=(host, port),
    )
    return protocol


class _QuicChannel:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        protocol: QuicConnectionProtocol,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._protocol = protocol
        self._closed = False
        self._close_scheduled = False

    async def send(self, frame: bytes) -> None:
        if self._closed:
            raise P2PConnectionClosed("QUIC peer channel is closed.")
        if not isinstance(frame, bytes):
            raise TypeError("Peer frame must be bytes.")
        if len(frame) > MAX_FRAME_BYTES:
            raise P2PProtocolError("QUIC peer frame exceeds the frame limit.")
        try:
            self._writer.write(len(frame).to_bytes(_QUIC_FRAME_LENGTH_SIZE, "big"))
            self._writer.write(frame)
            await self._writer.drain()
        except (ConnectionError, OSError, RuntimeError) as exc:
            self._closed = True
            raise P2PConnectionClosed("QUIC peer channel closed during send.") from exc

    async def receive(self) -> bytes | None:
        if self._closed:
            raise P2PConnectionClosed("QUIC peer channel is closed.")
        try:
            prefix = await self._reader.readexactly(_QUIC_FRAME_LENGTH_SIZE)
            length = int.from_bytes(prefix, "big")
            if length == 0 or length > MAX_FRAME_BYTES:
                raise P2PProtocolError("QUIC peer frame has an invalid length.")
            return await self._reader.readexactly(length)
        except asyncio.IncompleteReadError:
            self._closed = True
            return None
        except (ConnectionError, OSError, RuntimeError) as exc:
            self._closed = True
            raise P2PConnectionClosed("QUIC peer channel failed during receive.") from exc

    def close(self) -> None:
        if self._close_scheduled:
            return
        self._close_scheduled = True
        self._closed = True
        with suppress(ConnectionError, OSError, RuntimeError):
            self._writer.close()
        asyncio.get_running_loop().call_soon(self._close_protocol)

    def _close_protocol(self) -> None:
        with suppress(ConnectionError, OSError, RuntimeError):
            self._protocol.close()

    async def wait_closed(self) -> None:
        with suppress(ConnectionError, OSError, RuntimeError):
            await self._writer.wait_closed()
        await self._protocol.wait_closed()


class _QuicClientProtocol(QuicConnectionProtocol):
    def __init__(self, quic: QuicConnection) -> None:
        super().__init__(quic, stream_handler=self._reject_stream)
        self.alpn_protocol: str | None = None

    def _reject_stream(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.close(
            error_code=QuicErrorCode.PROTOCOL_VIOLATION,
            reason_phrase="server-initiated streams are not allowed",
        )

    def quic_event_received(self, event: Any) -> None:
        if isinstance(event, HandshakeCompleted):
            self.alpn_protocol = event.alpn_protocol
        super().quic_event_received(event)


class _QuicServerProtocol(QuicConnectionProtocol):
    def __init__(self, quic: QuicConnection, *, server: "QuicPeerServer") -> None:
        super().__init__(quic, stream_handler=self._handle_stream)
        self._server = server
        self._stream_writer: asyncio.StreamWriter | None = None
        self._authenticated = False
        self._registered = False
        self._handshake_timer: asyncio.TimerHandle | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        super().connection_made(transport)
        self._registered = self._server._register(self)
        if not self._registered:
            self.close(
                error_code=QuicErrorCode.CONNECTION_REFUSED,
                reason_phrase="connection limit reached",
            )
            return
        self._handshake_timer = asyncio.get_running_loop().call_later(
            self._server.handshake_timeout, self._handshake_expired
        )

    def mark_authenticated(self) -> None:
        self._authenticated = True
        self.cancel_handshake_timer()

    def cancel_handshake_timer(self) -> None:
        if self._handshake_timer is not None:
            self._handshake_timer.cancel()
            self._handshake_timer = None

    def _handshake_expired(self) -> None:
        self._handshake_timer = None
        if not self._authenticated:
            self.close(
                error_code=QuicErrorCode.PROTOCOL_VIOLATION,
                reason_phrase="peer handshake timed out",
            )

    def _handle_stream(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        stream_id = writer.get_extra_info("stream_id")
        if (
            self._stream_writer is not None
            or not isinstance(stream_id, int)
            or stream_id & 0b10
            or self._quic.is_closing()
        ):
            self.close(
                error_code=QuicErrorCode.PROTOCOL_VIOLATION,
                reason_phrase="exactly one bidirectional stream is required",
            )
            return
        self._stream_writer = writer
        self._server._start_authentication(self, reader, writer)

    def quic_event_received(self, event: Any) -> None:
        if isinstance(event, ConnectionTerminated):
            self.cancel_handshake_timer()
            self._server._discard(self)
        if isinstance(event, HandshakeCompleted):
            if event.alpn_protocol != P2P_SUBPROTOCOL:
                self.close(
                    error_code=QuicErrorCode.PROTOCOL_VIOLATION,
                    reason_phrase="unsupported application protocol",
                )
                return
        super().quic_event_received(event)

    def connection_lost(self, exc: Exception | None) -> None:
        self.cancel_handshake_timer()
        self._server._discard(self)
        super().connection_lost(exc)


class QuicPeerServer:
    """Accept authenticated device peers over certificate-verified QUIC."""

    def __init__(
        self,
        identity: PeerIdentity,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        certificate: str | Path,
        private_key: str | Path,
        allowed_peers: Collection[PeerAddress] | None = None,
        max_connections: int = 32,
        handshake_timeout: float = 10.0,
    ) -> None:
        if not isinstance(identity, PeerIdentity):
            raise ValueError("QuicPeerServer requires a PeerIdentity.")
        if not isinstance(host, str) or not host or len(host) > 255:
            raise ValueError("host must be a bounded string.")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("port must be an integer between 0 and 65535.")
        if not isinstance(certificate, (str, Path)) or not str(certificate):
            raise ValueError("certificate must be a certificate file path.")
        if not isinstance(private_key, (str, Path)) or not str(private_key):
            raise ValueError("private_key must be a private key file path.")
        if (
            isinstance(max_connections, bool)
            or not isinstance(max_connections, int)
            or not 1 <= max_connections <= 256
        ):
            raise ValueError("max_connections must be between 1 and 256.")
        self.identity = identity
        self.host = host
        self.port = port
        self.certificate = Path(certificate)
        self.private_key = Path(private_key)
        self.allowed_peers = (
            None if allowed_peers is None else frozenset(allowed_peers)
        )
        if self.allowed_peers is not None and any(
            not isinstance(peer, PeerAddress) for peer in self.allowed_peers
        ):
            raise ValueError("allowed_peers must contain PeerAddress values.")
        self.max_connections = max_connections
        self.handshake_timeout = _validate_network_timeout(
            handshake_timeout, "handshake_timeout"
        )
        self._server: Any = None
        self._protocols: set[_QuicServerProtocol] = set()
        self._channels: set[_QuicChannel] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._sessions: asyncio.Queue[SecurePeerSession] = asyncio.Queue(
            maxsize=max_connections
        )
        self._accepted = 0
        self._closing = False
        self._generation = 0
        self._lifecycle_lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._server is not None and not self._closing

    def status(self) -> dict[str, Any]:
        addresses: list[dict[str, Any]] = []
        if self._server is not None:
            transport = self._server._transport
            if transport is not None:
                address = transport.get_extra_info("sockname")
                if address is not None:
                    addresses.append({"host": str(address[0]), "port": address[1]})
        return {
            "transport": "quic",
            "running": self.running,
            "tls": True,
            "alpn": P2P_SUBPROTOCOL,
            "addresses": addresses,
            "active_connections": len(self._protocols),
            "accepted_sessions": self._accepted,
        }

    async def start(self) -> "QuicPeerServer":
        async with self._lifecycle_lock:
            if self.running:
                return self
            self._closing = False
            self._generation += 1
            configuration = QuicConfiguration(
                is_client=False,
                alpn_protocols=[P2P_SUBPROTOCOL],
                max_data=MAX_FRAME_BYTES,
                max_stream_data=MAX_FRAME_BYTES,
                idle_timeout=_QUIC_IDLE_TIMEOUT,
            )
            try:
                configuration.load_cert_chain(
                    str(self.certificate), str(self.private_key)
                )
                self._server = await _quic_serve(
                    self.host,
                    self.port,
                    configuration=configuration,
                    create_protocol=lambda connection, **kwargs: _QuicServerProtocol(
                        connection, server=self
                    ),
                )
            except (OSError, ValueError) as exc:
                self._server = None
                raise P2PTransportError("Unable to start QUIC peer server.") from exc
            transport = self._server._transport
            address = transport.get_extra_info("sockname")
            if address is not None:
                self.port = int(address[1])
            return self

    async def accept(
        self,
        *,
        expected_peer: PeerAddress | None = None,
        timeout: float = 30.0,
    ) -> SecurePeerSession:
        if not self.running:
            raise P2PTransportError("QUIC peer server is not running.")
        if expected_peer is not None and not isinstance(expected_peer, PeerAddress):
            raise ValueError("expected_peer must be a PeerAddress.")
        wait = _validate_network_timeout(timeout, "timeout")
        try:
            session = await asyncio.wait_for(self._sessions.get(), timeout=wait)
        except asyncio.TimeoutError as exc:
            raise P2PTransportError("Timed out waiting for a QUIC peer.") from exc
        remote = PeerAddress(
            session.remote_device_id, session.remote_signing_public_key
        )
        if expected_peer is not None and remote != expected_peer:
            await session.close()
            raise P2PHandshakeError("Accepted peer does not match the expected identity.")
        return session

    async def close(self) -> None:
        async with self._lifecycle_lock:
            self._closing = True
            self._generation += 1
            server, self._server = self._server, None
            tasks = list(self._tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            channels = list(self._channels)
            for channel in channels:
                channel.close()
            if channels:
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        asyncio.gather(
                            *(channel.wait_closed() for channel in channels),
                            return_exceptions=True,
                        ),
                        timeout=min(5.0, self.handshake_timeout),
                    )
            if server is not None:
                for protocol in list(self._protocols):
                    protocol.cancel_handshake_timer()
                server.close()
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        server.wait_closed(), timeout=min(1.0, self.handshake_timeout)
                    )
            self._channels.clear()
            self._protocols.clear()
            self._tasks.clear()
            self._sessions = asyncio.Queue(maxsize=self.max_connections)

    def _register(self, protocol: _QuicServerProtocol) -> bool:
        if self._closing or len(self._protocols) >= self.max_connections:
            return False
        self._protocols.add(protocol)
        return True

    def _discard(self, protocol: _QuicServerProtocol) -> None:
        self._protocols.discard(protocol)

    def _start_authentication(
        self,
        protocol: _QuicServerProtocol,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.create_task(
            self._authenticate(protocol, reader, writer),
            name="hex-quic-peer-authentication",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _authenticate(
        self,
        protocol: _QuicServerProtocol,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        generation = self._generation
        channel = _QuicChannel(reader, writer, protocol)
        self._channels.add(channel)
        try:
            session = await asyncio.wait_for(
                _respond_handshake(channel, self.identity, None),
                timeout=self.handshake_timeout,
            )
            remote = PeerAddress(
                session.remote_device_id, session.remote_signing_public_key
            )
            if self.allowed_peers is not None and remote not in self.allowed_peers:
                raise P2PHandshakeError("Peer is not allowed by this QUIC server.")
            if self._closing or generation != self._generation:
                await session.close()
                return
            protocol.mark_authenticated()
            await self._sessions.put(session)
            self._accepted += 1
            await protocol.wait_closed()
        except asyncio.CancelledError:
            channel.close()
            raise
        except Exception:
            channel.close()
        finally:
            self._channels.discard(channel)

    async def __aenter__(self) -> "QuicPeerServer":
        return await self.start()

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()


async def connect_quic_peer(
    host: str,
    port: int,
    local: PeerIdentity,
    *,
    expected_peer: PeerAddress,
    cafile: str | Path | None = None,
    capath: str | Path | None = None,
    cadata: bytes | None = None,
    timeout: float = 10.0,
) -> SecurePeerSession:
    """Connect to a pinned device peer over certificate-verified QUIC."""

    if not isinstance(local, PeerIdentity):
        raise ValueError("local must be a PeerIdentity.")
    if not isinstance(expected_peer, PeerAddress):
        raise ValueError("expected_peer must be a PeerAddress.")
    if not isinstance(host, str) or not host or len(host) > 255:
        raise ValueError("host must be a bounded string.")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("port must be an integer between 1 and 65535.")
    if cafile is not None and not isinstance(cafile, (str, Path)):
        raise ValueError("cafile must be a CA certificate file path.")
    if capath is not None and not isinstance(capath, (str, Path)):
        raise ValueError("capath must be a CA directory path.")
    if cadata is not None and not isinstance(cadata, bytes):
        raise ValueError("cadata must be bytes.")
    wait = _validate_network_timeout(timeout, "timeout")
    configuration = QuicConfiguration(
        is_client=True,
        alpn_protocols=[P2P_SUBPROTOCOL],
        max_data=MAX_FRAME_BYTES,
        max_stream_data=MAX_FRAME_BYTES,
        server_name=host,
        idle_timeout=_QUIC_IDLE_TIMEOUT,
        verify_mode=ssl.CERT_REQUIRED,
        cafile=str(cafile) if cafile is not None else None,
        capath=str(capath) if capath is not None else None,
        cadata=cadata,
    )
    loop = asyncio.get_running_loop()
    sock: socket.socket | None = None
    transport: asyncio.DatagramTransport | None = None
    protocol: _QuicClientProtocol | None = None
    channel: _QuicChannel | None = None
    authenticated = False
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
        if not infos:
            raise OSError("No address found for QUIC peer.")
        family, _, _, _, address = infos[0]
        sock = socket.socket(family, socket.SOCK_DGRAM)
        sock.setblocking(False)
        sock.bind(("::", 0) if family == socket.AF_INET6 else ("0.0.0.0", 0))
        sock.connect(address)
        protocol = _QuicClientProtocol(QuicConnection(configuration=configuration))
        transport, _ = await loop.create_datagram_endpoint(
            lambda: protocol,
            sock=sock,
        )
        sock = None
        protocol.connect(address)
        await asyncio.wait_for(protocol.wait_connected(), timeout=wait)
        if protocol.alpn_protocol != P2P_SUBPROTOCOL:
            raise P2PHandshakeError("Peer server does not support the HEX P2P ALPN.")
        reader, writer = await asyncio.wait_for(protocol.create_stream(), timeout=wait)
        channel = _QuicChannel(reader, writer, protocol)
        session = await asyncio.wait_for(
            _initiate_handshake(channel, local, expected_peer),
            timeout=wait,
        )
        authenticated = True
        return session
    except P2PTransportError:
        raise
    except asyncio.TimeoutError as exc:
        raise P2PTransportError("QUIC peer handshake timed out.") from exc
    except Exception as exc:
        raise P2PTransportError("Unable to establish QUIC peer connection.") from exc
    finally:
        if sock is not None:
            sock.close()
        if not authenticated:
            if channel is not None:
                channel.close()
            elif protocol is not None:
                with suppress(ConnectionError, OSError, RuntimeError):
                    protocol.close()
            if transport is not None:
                try:
                    if protocol is not None:
                        with suppress(
                            ConnectionError,
                            OSError,
                            RuntimeError,
                            asyncio.TimeoutError,
                        ):
                            await asyncio.wait_for(
                                protocol.wait_closed(), timeout=min(1.0, wait)
                            )
                finally:
                    transport.close()


__all__ = [
    "MAX_FRAME_BYTES",
    "P2P_SUBPROTOCOL",
    "InMemoryEndpoint",
    "InMemoryNetwork",
    "P2PChannel",
    "P2PConnectionClosed",
    "P2PHandshakeError",
    "P2PProtocolError",
    "P2PTransportError",
    "PeerAddress",
    "PeerIdentity",
    "QuicPeerServer",
    "SecurePeerSession",
    "WebSocketPeerServer",
    "connect_quic_peer",
    "connect_websocket_peer",
]
