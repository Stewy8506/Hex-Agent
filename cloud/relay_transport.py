"""Ticket-paired stateless relay for encrypted HEX peer frames."""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import ssl
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from websockets.exceptions import ConnectionClosed, WebSocketException

from .p2p_transport import (
    MAX_FRAME_BYTES,
    P2P_SUBPROTOCOL,
    P2PConnectionClosed,
    P2PHandshakeError,
    P2PProtocolError,
    P2PTransportError,
    PeerAddress,
    PeerIdentity,
    SecurePeerSession,
    _MODERN_WEBSOCKETS,
    _NoRedirectWebSocketConnect,
    _WebSocketChannel,
    _initiate_handshake,
    _is_loopback_host,
    _respond_handshake,
    _validate_client_ssl_context,
    _validate_network_timeout,
    _validate_websocket_uri,
    _websocket_serve,
)

_RELAY_PATH = "/hex/relay/v1"
_TICKET_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_ROLES = {"initiator", "responder"}


def generate_relay_ticket() -> str:
    """Generate a 256-bit rendezvous capability for exchange over authenticated signaling."""
    return secrets.token_urlsafe(32)


def _validate_ticket(value: Any) -> str:
    if not isinstance(value, str) or _TICKET_RE.fullmatch(value) is None:
        raise ValueError("Relay ticket must be a 256-bit URL-safe capability.")
    return value


def _load_control(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, (str, bytes)) or len(raw) > 4096:
        raise P2PProtocolError("Relay control message has an invalid size.")

    def object_from_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise P2PProtocolError("Relay control message contains duplicate keys.")
            result[key] = value
        return result

    def reject_constant(_value: str) -> Any:
        raise P2PProtocolError("Relay control message contains a nonfinite number.")

    try:
        value = json.loads(
            raw,
            object_pairs_hook=object_from_pairs,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise P2PProtocolError("Relay control message is not valid JSON.") from exc
    if not isinstance(value, dict):
        raise P2PProtocolError("Relay control message must be an object.")
    return value


@dataclass(eq=False)
class _RelayClient:
    websocket: Any
    ticket: str
    role: str
    closed: bool = False

    async def send(self, value: dict[str, Any]) -> None:
        if self.closed:
            raise P2PConnectionClosed("Relay connection is closed.")
        try:
            await self.websocket.send(json.dumps(value, separators=(",", ":")))
        except (ConnectionClosed, OSError) as exc:
            self.closed = True
            raise P2PConnectionClosed("Relay connection closed during send.") from exc

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        asyncio.create_task(self.websocket.close())

    async def wait_closed(self) -> None:
        with suppress(ConnectionClosed, OSError):
            await self.websocket.wait_closed()


@dataclass
class _RelayPair:
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    clients: dict[str, _RelayClient] = field(default_factory=dict)


class RelayPeerServer:
    """Pair two ticket holders and forward ciphertext without persistence or inspection."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        ssl_context: ssl.SSLContext | None = None,
        max_connections: int = 256,
        max_pending_pairs: int = 1024,
        join_timeout: float = 10.0,
        pair_timeout: float = 30.0,
        idle_timeout: float = 120.0,
    ) -> None:
        if not isinstance(host, str) or not host or len(host) > 255:
            raise ValueError("host must be a bounded string.")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("port must be an integer between 0 and 65535.")
        if ssl_context is not None and not isinstance(ssl_context, ssl.SSLContext):
            raise ValueError("ssl_context must be an SSLContext.")
        if ssl_context is None and not _is_loopback_host(host):
            raise ValueError("A non-loopback relay server requires TLS.")
        for value, label, maximum in (
            (max_connections, "max_connections", 1024),
            (max_pending_pairs, "max_pending_pairs", 4096),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueError(f"{label} must be between 1 and {maximum}.")
        self.host = host
        self.port = port
        self.ssl_context = ssl_context
        self.max_connections = max_connections
        self.max_pending_pairs = max_pending_pairs
        self.join_timeout = _validate_network_timeout(join_timeout, "join_timeout")
        self.pair_timeout = _validate_network_timeout(pair_timeout, "pair_timeout")
        self.idle_timeout = _validate_network_timeout(idle_timeout, "idle_timeout")
        self._server: Any = None
        self._clients: set[_RelayClient] = set()
        self._pairs: dict[str, _RelayPair] = {}
        self._lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._closing = False
        self._bytes_forwarded = 0
        self._pairs_completed = 0

    @property
    def uri(self) -> str:
        if self._server is None or not self._server.sockets:
            raise P2PTransportError("Relay server is not running.")
        address = self._server.sockets[0].getsockname()
        host = str(address[0])
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        scheme = "wss" if self.ssl_context is not None else "ws"
        return f"{scheme}://{host}:{address[1]}{_RELAY_PATH}"

    @property
    def running(self) -> bool:
        return self._server is not None and not self._closing

    def status(self) -> dict[str, Any]:
        return {
            "transport": "relay",
            "running": self.running,
            "tls": self.ssl_context is not None,
            "active_connections": len(self._clients),
            "pending_pairs": sum(len(pair.clients) == 1 for pair in self._pairs.values()),
            "pairs_completed": self._pairs_completed,
            "bytes_forwarded": self._bytes_forwarded,
            "persisted_bytes": 0,
        }

    async def start(self) -> "RelayPeerServer":
        async with self._lifecycle_lock:
            if self.running:
                return self
            self._closing = False
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
                    open_timeout=self.join_timeout,
                    close_timeout=min(5.0, self.join_timeout),
                )
            except (OSError, ValueError) as exc:
                self._server = None
                raise P2PTransportError("Unable to start relay server.") from exc
            return self

    async def close(self) -> None:
        async with self._lifecycle_lock:
            self._closing = True
            server, self._server = self._server, None
            if server is not None:
                closing = server.close()
                if closing is not None:
                    await closing
                await server.wait_closed()
            async with self._lock:
                clients = list(self._clients)
                self._clients.clear()
                for pair in self._pairs.values():
                    pair.ready.set()
                self._pairs.clear()
            for client in clients:
                client.close()
            await asyncio.gather(
                *(client.wait_closed() for client in clients),
                return_exceptions=True,
            )

    async def _handle_connection(self, websocket: Any) -> None:
        if self._closing or len(self._clients) >= self.max_connections:
            await websocket.close(code=1013, reason="relay connection limit")
            return
        if websocket.subprotocol != P2P_SUBPROTOCOL:
            await websocket.close(code=1002, reason="unsupported subprotocol")
            return
        client = _RelayClient(websocket, "", "")
        self._clients.add(client)
        pair: _RelayPair | None = None
        joined = False
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=self.join_timeout)
            control = _load_control(raw)
            if set(control) != {"v", "ticket", "role"} or type(control["v"]) is not int:
                raise P2PProtocolError("Invalid relay join fields.")
            if control["v"] != 1 or control["role"] not in _ROLES:
                raise P2PProtocolError("Unsupported relay join request.")
            ticket = _validate_ticket(control["ticket"])
            client.ticket = ticket
            client.role = str(control["role"])
            async with self._lock:
                if self._closing:
                    raise P2PConnectionClosed("Relay server is closing.")
                pair = self._pairs.get(ticket)
                if pair is None:
                    if len(self._pairs) >= self.max_pending_pairs:
                        raise P2PTransportError("Relay pending-pair limit reached.")
                    pair = _RelayPair()
                    self._pairs[ticket] = pair
                if client.role in pair.clients:
                    raise P2PProtocolError("Relay role is already occupied.")
                pair.clients[client.role] = client
                joined = True
                first = len(pair.clients) == 1
                if not first:
                    pair.ready.set()
            if first:
                await client.send({"v": 1, "status": "waiting"})
                try:
                    await asyncio.wait_for(pair.ready.wait(), timeout=self.pair_timeout)
                except asyncio.TimeoutError as exc:
                    raise P2PTransportError("Relay pair timed out.") from exc
            partner = pair.clients.get("responder" if client.role == "initiator" else "initiator")
            if partner is None or partner.closed or client.closed:
                raise P2PConnectionClosed("Relay partner disconnected.")
            await client.send({"v": 1, "status": "paired"})
            if not first:
                async with self._lock:
                    self._pairs_completed += 1
            await self._forward(client, partner)
        except asyncio.CancelledError:
            raise
        except (ConnectionClosed, OSError):
            pass
        except Exception:
            with suppress(Exception):
                await websocket.close(code=1008, reason="relay protocol error")
        finally:
            client.close()
            if pair is not None and joined:
                async with self._lock:
                    partner = pair.clients.get(
                        "responder" if client.role == "initiator" else "initiator"
                    )
                    if pair.clients.get(client.role) is client:
                        pair.clients.pop(client.role, None)
                    if not pair.clients:
                        self._pairs.pop(client.ticket, None)
                    pair.ready.set()
                if partner is not None:
                    partner.close()
            self._clients.discard(client)
            await client.wait_closed()

    async def _forward(self, client: _RelayClient, partner: _RelayClient) -> None:
        while not client.closed and not partner.closed:
            try:
                raw = await asyncio.wait_for(
                    client.websocket.recv(), timeout=self.idle_timeout
                )
            except asyncio.TimeoutError as exc:
                raise P2PTransportError("Relay connection went idle.") from exc
            if not isinstance(raw, bytes):
                raise P2PProtocolError("Relay data messages must be binary frames.")
            if len(raw) > MAX_FRAME_BYTES:
                raise P2PProtocolError("Relay frame exceeds the size limit.")
            try:
                await partner.websocket.send(raw)
            except (ConnectionClosed, OSError) as exc:
                raise P2PConnectionClosed("Relay partner disconnected.") from exc
            self._bytes_forwarded += len(raw)

    async def __aenter__(self) -> "RelayPeerServer":
        return await self.start()

    async def __aexit__(self, *_args: Any) -> None:
        await self.close()


async def connect_relay_peer(
    uri: str,
    ticket: str,
    role: str,
    local: PeerIdentity,
    *,
    expected_peer: PeerAddress,
    ssl_context: ssl.SSLContext | None = None,
    timeout: float = 30.0,
) -> SecurePeerSession:
    """Join a ticket pair, then authenticate and encrypt directly with the remote device."""
    if role not in _ROLES:
        raise ValueError("Relay role must be initiator or responder.")
    if not isinstance(local, PeerIdentity):
        raise ValueError("local must be a PeerIdentity.")
    if not isinstance(expected_peer, PeerAddress):
        raise ValueError("expected_peer must be a PeerAddress.")
    parsed = _validate_websocket_uri(uri)
    if parsed.path != _RELAY_PATH:
        raise ValueError("Relay URI must use the /hex/relay/v1 path.")
    _validate_client_ssl_context(ssl_context, parsed.scheme)
    capability = _validate_ticket(ticket)
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
        raise P2PTransportError("Unable to connect to encrypted relay.") from exc
    channel = _WebSocketChannel(websocket)
    authenticated = False
    try:
        if websocket.subprotocol != P2P_SUBPROTOCOL:
            raise P2PHandshakeError("Relay does not support the HEX P2P subprotocol.")
        async with asyncio.timeout(wait):
            await websocket.send(json.dumps({
                "v": 1, "ticket": capability, "role": role,
            }, separators=(",", ":")))
            while True:
                raw = await websocket.recv()
                control = _load_control(raw)
                if control == {"v": 1, "status": "waiting"}:
                    continue
                if control == {"v": 1, "status": "paired"}:
                    break
                raise P2PProtocolError("Unexpected relay control response.")
            session = await (
                _initiate_handshake(channel, local, expected_peer)
                if role == "initiator"
                else _respond_handshake(channel, local, expected_peer)
            )
        authenticated = True
        return session
    except P2PTransportError:
        raise
    except asyncio.TimeoutError as exc:
        raise P2PTransportError("Relay pairing or handshake timed out.") from exc
    except (ConnectionClosed, OSError, WebSocketException) as exc:
        raise P2PConnectionClosed("Relay connection closed during pairing.") from exc
    except Exception as exc:
        raise P2PTransportError("Relay peer authentication failed.") from exc
    finally:
        if not authenticated:
            channel.close()
            with suppress(Exception):
                await asyncio.wait_for(channel.wait_closed(), timeout=min(5.0, wait))
