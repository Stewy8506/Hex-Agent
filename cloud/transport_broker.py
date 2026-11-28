"""Signaled WebRTC/relay negotiation with revocation-aware tunnel ownership."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import queue
import re
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass, field
from typing import Any, TypeVar

from .p2p_transport import (
    P2PHandshakeError,
    P2PTransportError,
    PeerAddress,
    PeerIdentity,
    SecurePeerSession,
)
from .relay_transport import connect_relay_peer, generate_relay_ticket
from .signaling import P2PSignalingClient
from .webrtc_transport import WebRTCPeer, create_webrtc_answer, create_webrtc_offer

_PROTOCOL = "hex-signaled-transport/v1"
_SESSION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_MAX_SIGNAL_BYTES = 512 * 1024
_MAX_SEEN_SESSIONS = 1024
_SEEN_TTL_SECONDS = 600.0
_DEFAULT_KILL_SWITCHES = frozenset({"p2p_cross_device_sync", "team_primary_host_mesh"})

T = TypeVar("T")
SendSignal = Callable[[str, dict[str, Any]], bool]
SessionHandler = Callable[[SecurePeerSession, PeerAddress], Awaitable[Any]]
PeerOperation = Callable[[SecurePeerSession], Awaitable[T]]


class NegotiationError(P2PTransportError):
    """Raised when authenticated signaling cannot establish a peer transport."""


@dataclass(slots=True)
class PeerTunnel:
    session_id: str
    peer_id: str
    transport: str
    close: Callable[[], Awaitable[None]]
    owner: asyncio.Task[Any] | None = None


class PeerTunnelRegistry:
    def __init__(self) -> None:
        self._tunnels: dict[str, PeerTunnel] = {}

    def add(self, tunnel: PeerTunnel) -> None:
        self._tunnels[tunnel.session_id] = tunnel

    def remove(self, session_id: str) -> None:
        self._tunnels.pop(session_id, None)

    async def close_peer(self, peer_id: str) -> int:
        tunnels = [tunnel for tunnel in self._tunnels.values() if tunnel.peer_id == peer_id]
        await asyncio.gather(*(_close_callable(tunnel.close) for tunnel in tunnels))
        return len(tunnels)

    async def close_session(self, session_id: str, peer_id: str) -> bool:
        tunnel = self._tunnels.get(session_id)
        if tunnel is None or tunnel.peer_id != peer_id:
            return False
        await _close_callable(tunnel.close)
        return True

    async def close_all(self) -> int:
        tunnels = list(self._tunnels.values())
        await asyncio.gather(*(_close_callable(tunnel.close) for tunnel in tunnels))
        return len(tunnels)

    def status(self) -> list[dict[str, str]]:
        return [
            {"session_id": tunnel.session_id, "peer_id": tunnel.peer_id, "transport": tunnel.transport}
            for tunnel in self._tunnels.values()
        ]


async def _close_callable(close: Callable[[], Awaitable[None]]) -> None:
    try:
        await asyncio.wait_for(close(), timeout=3.0)
    except (Exception, asyncio.CancelledError):
        pass


async def _close_session(session: SecurePeerSession) -> None:
    try:
        await asyncio.wait_for(session.close(), timeout=3.0)
    except (Exception, asyncio.CancelledError):
        pass


async def _close_peers(peers: Collection[WebRTCPeer]) -> None:
    for peer in peers:
        try:
            await asyncio.wait_for(peer.close(), timeout=3.0)
        except (Exception, asyncio.CancelledError):
            pass


@dataclass(slots=True)
class _PendingAttempt:
    session_id: str
    peer_id: str
    transport: str
    messages: asyncio.Queue[dict[str, Any] | None] = field(default_factory=asyncio.Queue)
    peers: list[WebRTCPeer] = field(default_factory=list)
    error: BaseException | None = None


@dataclass(slots=True)
class _IncomingAttempt:
    session_id: str
    peer_id: str
    peers: list[WebRTCPeer] = field(default_factory=list)
    cancelled: bool = False


class TransportBroker:
    """Owns a persistent event loop for signal-driven peer sessions."""

    def __init__(
        self,
        signaling: P2PSignalingClient,
        send_signal: SendSignal,
        identity: PeerIdentity,
        *,
        relay_uri: str | None = None,
        stun_servers: Collection[str] | None = None,
        session_handler: SessionHandler | None = None,
        negotiation_timeout: float = 20.0,
        kill_switch_names: Collection[str] | None = None,
    ) -> None:
        if negotiation_timeout <= 0:
            raise ValueError("Negotiation timeout must be positive.")
        self.signaling = signaling
        self._send_signal = send_signal
        self._identity = identity
        self._relay_uri = relay_uri
        self._stun_servers = tuple(stun_servers or ())
        self._session_handler = session_handler
        self._negotiation_timeout = negotiation_timeout
        self._kill_switch_names = frozenset(kill_switch_names or _DEFAULT_KILL_SWITCHES)
        self._incoming: queue.Queue[tuple[str, str, dict[str, Any]] | tuple[str, dict[str, Any]] | None] = queue.Queue()
        self._registry = PeerTunnelRegistry()
        self._pending: dict[str, _PendingAttempt] = {}
        self._incoming_attempts: dict[str, _IncomingAttempt] = {}
        self._seen: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._lock = threading.RLock()
        self._stopping = False
        self._started = False
        self._router: asyncio.Task[Any] | None = None
        signaling.on_p2p_signal(self._on_signal)
        signaling.on_security_event(self._on_security_event)

    @property
    def device_id(self) -> str:
        return self._identity.device_id

    def start(self) -> None:
        with self._lock:
            if self._started and self._thread is not None and self._thread.is_alive():
                return
            if threading.current_thread() is self._thread:
                raise RuntimeError("TransportBroker cannot start itself from its event-loop thread.")
            self._stopping = False
            self._ready.clear()
            self._thread = threading.Thread(target=self._thread_main, name="hex-transport-broker", daemon=True)
            self._thread.start()
            self._started = True
        if not self._ready.wait(timeout=5.0):
            raise RuntimeError("Transport broker event loop did not start.")

    def stop(self) -> None:
        if threading.current_thread() is self._thread:
            raise RuntimeError("TransportBroker cannot stop itself from its event-loop thread.")
        with self._lock:
            thread = self._thread
            loop = self._loop
            if thread is None or loop is None or not thread.is_alive():
                self._started = False
                return
            self._stopping = True
            self._incoming.put(None)
        try:
            asyncio.run_coroutine_threadsafe(self._shutdown(), loop).result(timeout=8.0)
        except (Exception, concurrent.futures.TimeoutError):
            pass
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5.0)
        with self._lock:
            if self._thread is thread:
                self._thread = None
                self._loop = None
                self._started = False
        self.signaling.remove_p2p_signal_handler(self._on_signal)
        self.signaling.remove_security_event_handler(self._on_security_event)

    def __enter__(self) -> "TransportBroker":
        self.start()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.stop()

    def run_with_peer(
        self,
        peer: PeerAddress,
        operation: PeerOperation[T],
        *,
        transport: str = "auto",
        timeout: float | None = None,
    ) -> T:
        future = self.run_with_peer_async(peer, operation, transport=transport, timeout=timeout)
        return future.result()

    async def run_with_peer_async(
        self,
        peer: PeerAddress,
        operation: PeerOperation[T],
        *,
        transport: str = "auto",
        timeout: float | None = None,
    ) -> T:
        future = self._submit_connection(peer, operation, transport, timeout)
        return await asyncio.wrap_future(future)

    def status(self) -> dict[str, Any]:
        with self._lock:
            loop = self._loop
            running = self._started and self._thread is not None and self._thread.is_alive()
        if not running or loop is None:
            return {"running": False, "local_device_id": self._identity.device_id, "active_tunnels": []}
        try:
            return asyncio.run_coroutine_threadsafe(self._status(), loop).result(timeout=2.0)
        except Exception:
            return {"running": False, "local_device_id": self._identity.device_id, "active_tunnels": []}

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._router = loop.create_task(self._route_loop())
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            tasks = asyncio.all_tasks(loop)
            for task in tasks:
                task.cancel()
            if tasks:
                loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            loop.close()

    def submit(self, factory: Callable[[], Awaitable[T]]) -> concurrent.futures.Future[T]:
        """Schedule a coroutine factory on the broker loop from any thread."""
        self.start()
        result: concurrent.futures.Future[T] = concurrent.futures.Future()

        def schedule() -> None:
            if self._stopping or self._loop is None or not self._loop.is_running():
                result.set_exception(NegotiationError("Transport broker is stopped."))
                return
            if result.cancelled():
                return
            task = self._loop.create_task(factory())
            self._tasks.add(task)
            result.add_done_callback(
                lambda future: self._loop.call_soon_threadsafe(task.cancel)
                if future.cancelled() and self._loop is not None
                else None
            )

            def complete(done: asyncio.Task[T]) -> None:
                self._tasks.discard(done)
                if result.cancelled():
                    return
                try:
                    result.set_result(done.result())
                except BaseException as exc:
                    result.set_exception(exc)

            task.add_done_callback(complete)

        loop = self._loop
        if loop is None:
            result.set_exception(NegotiationError("Transport broker event loop is unavailable."))
            return result
        loop.call_soon_threadsafe(schedule)
        return result

    def _submit_connection(
        self,
        peer: PeerAddress,
        operation: PeerOperation[T],
        transport: str,
        timeout: float | None,
    ) -> concurrent.futures.Future[T]:
        if transport not in {"auto", "webrtc", "relay"}:
            raise ValueError("Transport must be auto, webrtc, or relay.")
        if not callable(operation):
            raise TypeError("Peer operation must be callable.")
        return self.submit(lambda: self._run_with_peer(peer, operation, transport, timeout))

    async def _status(self) -> dict[str, Any]:
        return {
            "running": not self._stopping,
            "local_device_id": self._identity.device_id,
            "active_tunnels": self._registry.status(),
        }

    async def _shutdown(self) -> None:
        current = asyncio.current_task()
        tasks = [task for task in self._tasks if task is not current]
        for task in tasks:
            task.cancel()
        if self._router is not None and self._router is not current:
            self._router.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for pending in list(self._pending.values()):
            pending.error = NegotiationError("Transport broker stopped.")
            pending.messages.put_nowait(None)
            await _close_peers(pending.peers)
        self._pending.clear()
        for attempt in self._incoming_attempts.values():
            attempt.cancelled = True
        await asyncio.gather(
            *(_close_peers(attempt.peers) for attempt in self._incoming_attempts.values())
        )
        self._incoming_attempts.clear()
        await self._registry.close_all()

    def _on_signal(self, sender: str, payload: dict[str, Any]) -> None:
        if not isinstance(sender, str) or not isinstance(payload, dict):
            return
        try:
            if len(json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")) > _MAX_SIGNAL_BYTES:
                return
        except (TypeError, ValueError, RecursionError):
            return
        self._incoming.put(("signal", sender, payload))

    def _on_security_event(self, event: dict[str, Any]) -> None:
        self._incoming.put(("security", event))

    def _spawn(self, coroutine: Awaitable[Any]) -> None:
        loop = self._loop
        if loop is None:
            coroutine.close()  # type: ignore[attr-defined]
            return
        task = loop.create_task(coroutine)
        self._tasks.add(task)

        def complete(done: asyncio.Task[Any]) -> None:
            self._tasks.discard(done)
            if done.cancelled():
                return
            try:
                done.result()
            except Exception:
                pass

        task.add_done_callback(complete)

    async def _route_loop(self) -> None:
        while True:
            item = await asyncio.to_thread(self._incoming.get)
            if item is None:
                return
            if item[0] == "security":
                self._apply_security_event(item[1])
                continue
            _, sender, payload = item
            self._route_signal(sender, payload)

    def _apply_security_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "device_revoked" and event.get("device_record_id") == self._identity.device_id:
            self._fail_all_pending(NegotiationError("Local device has been revoked."))
            self._fail_all_incoming()
            self._spawn(self._registry.close_all())
        elif event_type in {"peer_revoked", "peer_offline"}:
            peer_id = event.get("device_record_id")
            if isinstance(peer_id, str):
                self._fail_pending_peer(peer_id, NegotiationError("Peer is no longer trusted."))
                self._fail_incoming_peer(peer_id)
                self._spawn(self._registry.close_peer(peer_id))
        elif event_type == "kill_switch_updated":
            switch = event.get("event", {})
            name = switch.get("switch")
            if switch.get("active") and name in self._kill_switch_names:
                self._fail_all_pending(NegotiationError("Peer connectivity has been disabled."))
                self._fail_all_incoming()
                self._spawn(self._registry.close_all())

    def _fail_all_pending(self, error: BaseException) -> None:
        for pending in list(self._pending.values()):
            pending.error = error
            pending.messages.put_nowait(None)
            self._spawn(_close_peers(pending.peers))
        self._pending.clear()

    def _fail_pending_peer(self, peer_id: str, error: BaseException) -> None:
        for session_id, pending in list(self._pending.items()):
            if pending.peer_id == peer_id:
                pending.error = error
                pending.messages.put_nowait(None)
                self._spawn(_close_peers(pending.peers))
                self._pending.pop(session_id, None)

    def _fail_incoming_peer(self, peer_id: str) -> None:
        for session_id, attempt in list(self._incoming_attempts.items()):
            if attempt.peer_id == peer_id:
                attempt.cancelled = True
                self._spawn(_close_peers(attempt.peers))
                self._incoming_attempts.pop(session_id, None)

    def _fail_all_incoming(self) -> None:
        attempts = list(self._incoming_attempts.values())
        for attempt in attempts:
            attempt.cancelled = True
        self._incoming_attempts.clear()
        self._spawn(self._close_incoming_attempts(attempts))

    async def _close_incoming_attempts(self, attempts: Collection[_IncomingAttempt]) -> None:
        for attempt in attempts:
            await _close_peers(attempt.peers)

    def _route_signal(self, sender: str, payload: dict[str, Any]) -> None:
        kind = payload.get("kind")
        session_id = payload.get("session_id")
        if payload.get("protocol") != _PROTOCOL or not isinstance(session_id, str):
            return
        if not _SESSION_ID_RE.fullmatch(session_id):
            return
        if kind in {"webrtc_answer", "webrtc_error", "relay_error"}:
            pending = self._pending.get(session_id)
            if pending is not None and pending.peer_id == sender:
                pending.messages.put_nowait(payload)
            return
        if kind == "cancel":
            if set(payload) != {"protocol", "kind", "session_id"}:
                return
            attempt = self._incoming_attempts.get(session_id)
            if attempt is not None and attempt.peer_id == sender:
                attempt.cancelled = True
                self._spawn(_close_peers(attempt.peers))
                self._incoming_attempts.pop(session_id, None)
            self._spawn(self._registry.close_session(session_id, sender))
            return
        if kind not in {"webrtc_offer", "relay_invite"}:
            return
        if self._is_security_blocked() or self._seen_session(session_id, sender):
            return
        attempt = _IncomingAttempt(session_id, sender)
        self._incoming_attempts[session_id] = attempt
        self._spawn(self._accept_incoming(sender, attempt, kind, payload))

    def _seen_session(self, session_id: str, sender: str) -> bool:
        now = time.monotonic()
        if session_id in self._seen:
            return True
        self._seen[session_id] = (sender, now)
        while len(self._seen) > _MAX_SEEN_SESSIONS:
            self._seen.popitem(last=False)
        for existing, (_, created) in list(self._seen.items()):
            if now - created > _SEEN_TTL_SECONDS:
                self._seen.pop(existing, None)
        return False

    def _is_security_blocked(self) -> bool:
        return self.signaling.security_blocked(set(self._kill_switch_names))

    def _require_trusted_peer(self, peer: PeerAddress) -> None:
        if self._is_security_blocked():
            raise NegotiationError("Peer connectivity is disabled for this device.")
        info = self.signaling.peer_info(peer.device_id)
        if info is None:
            raise NegotiationError("Peer is not online on the authenticated signaling service.")
        if info.device_pubkey != peer.signing_public_key:
            raise P2PHandshakeError("Peer signaling identity does not match the pinned device key.")

    def _send(self, peer_id: str, kind: str, session_id: str, **fields: Any) -> None:
        payload = {"protocol": _PROTOCOL, "kind": kind, "session_id": session_id, **fields}
        try:
            encoded = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, RecursionError) as exc:
            raise NegotiationError("Signaling payload is not valid bounded JSON.") from exc
        if len(encoded) > _MAX_SIGNAL_BYTES:
            raise NegotiationError("Signaling payload exceeds the negotiation size limit.")
        try:
            sent = self._send_signal(peer_id, payload)
        except Exception as exc:
            raise NegotiationError("Authenticated signaling send failed.") from exc
        if sent is False:
            raise NegotiationError("Peer is not reachable through authenticated signaling.")

    async def _wait_message(
        self, pending: _PendingAttempt, expected: set[str], timeout: float | None
    ) -> dict[str, Any]:
        try:
            message = await asyncio.wait_for(pending.messages.get(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise NegotiationError("Peer negotiation timed out.") from exc
        if message is None:
            raise pending.error or NegotiationError("Peer negotiation was cancelled.")
        kind = message.get("kind")
        if kind == f"{pending.transport}_error":
            if set(message) != {"protocol", "kind", "session_id", "code"}:
                raise NegotiationError("Peer returned an invalid negotiation error.")
            raise NegotiationError("Peer could not establish the requested transport.")
        if kind not in expected:
            raise NegotiationError("Peer returned an out-of-state negotiation message.")
        return message

    async def _run_with_peer(
        self,
        peer: PeerAddress,
        operation: PeerOperation[T],
        transport: str,
        timeout: float | None,
    ) -> T:
        self._require_trusted_peer(peer)
        negotiation_timeout = timeout or self._negotiation_timeout
        if transport in {"auto", "webrtc"}:
            try:
                return await self._webrtc_attempt(peer, operation, negotiation_timeout)
            except Exception as webrtc_error:
                if transport == "webrtc" or not self._relay_uri:
                    raise
                webrtc_failure = webrtc_error
        else:
            webrtc_failure = NegotiationError("WebRTC was not selected.")
        if not self._relay_uri:
            raise webrtc_failure
        return await self._relay_attempt(peer, operation, negotiation_timeout)

    async def _webrtc_attempt(
        self, peer: PeerAddress, operation: PeerOperation[T], timeout: float
    ) -> T:
        self._require_trusted_peer(peer)
        peer_connection = await create_webrtc_offer(
            self._identity, stun_servers=self._stun_servers, timeout=timeout
        )
        session_id = secrets.token_hex(16)
        pending = _PendingAttempt(session_id, peer.device_id, "webrtc", peers=[peer_connection])
        self._pending[session_id] = pending
        try:
            self._require_trusted_peer(peer)
            self._send(peer.device_id, "webrtc_offer", session_id, description=peer_connection.description)
            answer = await self._wait_message(pending, {"webrtc_answer"}, timeout)
            if set(answer) != {"protocol", "kind", "session_id", "description"}:
                raise NegotiationError("Peer returned an invalid WebRTC answer.")
            session = await peer_connection.connect(answer["description"], expected_peer=peer, timeout=timeout)
            return await self._run_owned_operation(
                session_id, peer, session, "webrtc", pending.peers, operation
            )
        except Exception:
            self._send_cancel(peer.device_id, session_id)
            raise
        finally:
            self._pending.pop(session_id, None)
            await _close_peers(pending.peers)

    async def _relay_attempt(
        self, peer: PeerAddress, operation: PeerOperation[T], timeout: float
    ) -> T:
        self._require_trusted_peer(peer)
        if not self._relay_uri:
            raise NegotiationError("No relay URI is configured.")
        ticket = generate_relay_ticket()
        session_id = secrets.token_hex(16)
        pending = _PendingAttempt(session_id, peer.device_id, "relay")
        self._pending[session_id] = pending
        try:
            self._require_trusted_peer(peer)
            self._send(peer.device_id, "relay_invite", session_id, ticket=ticket)
            session = await self._connect_relay_initiator(peer, ticket, pending, timeout)
            return await self._run_owned_operation(
                session_id, peer, session, "relay", (), operation
            )
        except Exception:
            self._send_cancel(peer.device_id, session_id)
            raise
        finally:
            self._pending.pop(session_id, None)

    async def _run_owned_operation(
        self,
        session_id: str,
        peer: PeerAddress,
        session: SecurePeerSession,
        transport: str,
        peers: Collection[WebRTCPeer],
        operation: PeerOperation[T],
    ) -> T:
        owned_peers = tuple(peers)

        async def close_tunnel() -> None:
            owner = asyncio.current_task()
            if tunnel.owner is not None and tunnel.owner is not owner:
                tunnel.owner.cancel()
            await _close_session(session)
            await _close_peers(owned_peers)

        tunnel = PeerTunnel(
            session_id, peer.device_id, transport, close_tunnel, asyncio.current_task()
        )
        self._registry.add(tunnel)
        try:
            return await operation(session)
        finally:
            self._registry.remove(session_id)
            await close_tunnel()

    async def _connect_relay_initiator(
        self,
        peer: PeerAddress,
        ticket: str,
        pending: _PendingAttempt,
        timeout: float,
    ) -> SecurePeerSession:
        if not self._relay_uri:
            raise NegotiationError("No relay URI is configured.")
        connect_task = asyncio.create_task(
            connect_relay_peer(
                self._relay_uri,
                ticket,
                "initiator",
                self._identity,
                expected_peer=peer,
                timeout=timeout,
            )
        )
        message_task = asyncio.create_task(pending.messages.get())
        try:
            while True:
                done, _ = await asyncio.wait(
                    {connect_task, message_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if connect_task in done:
                    return await connect_task
                message = await message_task
                if message is None:
                    raise pending.error or NegotiationError("Peer negotiation was cancelled.")
                if message.get("kind") != "relay_error":
                    raise NegotiationError("Peer returned an out-of-state relay message.")
                raise NegotiationError("Peer could not establish the relay transport.")
        finally:
            for task in (connect_task, message_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(connect_task, message_task, return_exceptions=True)

    def _send_cancel(self, peer_id: str, session_id: str) -> None:
        try:
            self._send(peer_id, "cancel", session_id)
        except Exception:
            pass

    async def _accept_incoming(
        self, sender: str, attempt: _IncomingAttempt, kind: str, payload: dict[str, Any]
    ) -> None:
        session_id = attempt.session_id
        peer_connection: WebRTCPeer | None = None
        session: SecurePeerSession | None = None
        transport = "webrtc" if kind == "webrtc_offer" else "relay"
        try:
            info = self.signaling.peer_info(sender)
            if info is None or self._is_security_blocked() or attempt.cancelled:
                return
            peer = PeerAddress(sender, info.device_pubkey)
            if kind == "webrtc_offer":
                if set(payload) != {"protocol", "kind", "session_id", "description"}:
                    raise NegotiationError("Invalid WebRTC offer fields.")
                peer_connection = await create_webrtc_answer(
                    payload["description"], self._identity, stun_servers=self._stun_servers,
                    timeout=self._negotiation_timeout,
                )
                attempt.peers.append(peer_connection)
                if attempt.cancelled:
                    return
                self._send(sender, "webrtc_answer", session_id, description=peer_connection.description)
                session = await peer_connection.accept(expected_peer=peer, timeout=self._negotiation_timeout)
            else:
                if set(payload) != {"protocol", "kind", "session_id", "ticket"}:
                    raise NegotiationError("Invalid relay invitation fields.")
                ticket = payload["ticket"]
                if not isinstance(ticket, str):
                    raise NegotiationError("Invalid relay ticket.")
                if not self._relay_uri:
                    raise NegotiationError("No relay URI is configured.")
                session = await connect_relay_peer(
                    self._relay_uri, ticket, "responder", self._identity,
                    expected_peer=peer, timeout=self._negotiation_timeout,
                )
            if session is None or attempt.cancelled:
                return
            await self._serve_session(session_id, peer, session, transport, peer_connection)
        except asyncio.CancelledError:
            if session is not None:
                await _close_session(session)
            if peer_connection is not None:
                await _close_peers([peer_connection])
            raise
        except Exception:
            try:
                self._send(sender, f"{transport}_error", session_id, code="negotiation_failed")
            except Exception:
                pass
        finally:
            if self._incoming_attempts.get(session_id) is attempt:
                self._incoming_attempts.pop(session_id, None)
            if session is not None:
                await _close_session(session)
            if peer_connection is not None:
                await _close_peers([peer_connection])

    async def _serve_session(
        self,
        session_id: str,
        peer: PeerAddress,
        session: SecurePeerSession,
        transport: str,
        peer_connection: WebRTCPeer | None,
    ) -> None:
        peers = [peer_connection] if peer_connection is not None else []

        async def close_tunnel() -> None:
            owner = asyncio.current_task()
            if tunnel.owner is not None and tunnel.owner is not owner:
                tunnel.owner.cancel()
            await _close_session(session)
            await _close_peers(peers)

        tunnel = PeerTunnel(
            session_id, peer.device_id, transport, close_tunnel, asyncio.current_task()
        )
        self._registry.add(tunnel)
        try:
            if self._session_handler is not None:
                await self._session_handler(session, peer)
        finally:
            self._registry.remove(session_id)
            await close_tunnel()
