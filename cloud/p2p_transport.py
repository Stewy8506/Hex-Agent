"""Authenticated encrypted peer transport with an in-memory loopback."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import secrets
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

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


__all__ = [
    "MAX_FRAME_BYTES",
    "InMemoryEndpoint",
    "InMemoryNetwork",
    "P2PChannel",
    "P2PConnectionClosed",
    "P2PHandshakeError",
    "P2PProtocolError",
    "P2PTransportError",
    "PeerAddress",
    "PeerIdentity",
    "SecurePeerSession",
]
