"""WebRTC DataChannel transport for the authenticated HEX peer protocol."""

from __future__ import annotations

import asyncio
from typing import Any, Collection

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
    _initiate_handshake,
    _respond_handshake,
    _validate_network_timeout,
)

_WEBRTC_CHUNK_BYTES = 16 * 1024
_WEBRTC_BUFFER_LIMIT_BYTES = 256 * 1024
_CHUNK_HEADER_BYTES = 8
_MAX_SDP_BYTES = 256 * 1024


def _validate_stun_servers(servers: Collection[str] | None) -> list[str]:
    if servers is None:
        return []
    if isinstance(servers, (str, bytes)):
        raise ValueError("stun_servers must be a collection of STUN URLs.")
    result: list[str] = []
    for server in servers:
        if (
            not isinstance(server, str)
            or not server.startswith(("stun:", "stuns:"))
            or len(server) > 512
            or any(char.isspace() or ord(char) < 32 for char in server)
        ):
            raise ValueError("Only bounded STUN/STUNS URLs are supported.")
        result.append(server)
    return result


def _validate_description(value: Any, expected_type: str) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"type", "sdp"}:
        raise ValueError("WebRTC description must contain exactly type and sdp.")
    if value["type"] != expected_type:
        raise ValueError(f"Expected a WebRTC {expected_type} description.")
    sdp = value["sdp"]
    if (
        not isinstance(sdp, str)
        or not sdp
        or len(sdp.encode("utf-8")) > _MAX_SDP_BYTES
        or "\x00" in sdp
    ):
        raise ValueError("WebRTC SDP is missing or exceeds the size limit.")
    return {"type": expected_type, "sdp": sdp}


class _WebRTCChannel:
    def __init__(self, peer: "WebRTCPeer", datachannel: Any) -> None:
        self._peer = peer
        self._datachannel = datachannel
        self._incoming: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._buffer: bytearray | None = None
        self._buffer_low = asyncio.Event()
        self._closed = False
        datachannel.on("message", self._on_message)
        datachannel.on("close", self._on_close)
        datachannel.on("error", self._on_error)
        datachannel.on("bufferedamountlow", self._on_buffer_low)
        if hasattr(datachannel, "bufferedAmountLowThreshold"):
            datachannel.bufferedAmountLowThreshold = 64 * 1024

    def _on_message(self, message: Any) -> None:
        if self._closed:
            return
        if not isinstance(message, bytes) or not (
            _CHUNK_HEADER_BYTES < len(message)
            <= _CHUNK_HEADER_BYTES + _WEBRTC_CHUNK_BYTES
        ):
            self._fail(P2PProtocolError("WebRTC peer sent an invalid data chunk."))
            return
        length = int.from_bytes(message[:4], "big")
        offset = int.from_bytes(message[4:_CHUNK_HEADER_BYTES], "big")
        if length == 0 or length > MAX_FRAME_BYTES or offset > length:
            self._fail(P2PProtocolError("WebRTC peer sent an invalid frame header."))
            return
        if self._buffer is None:
            if offset != 0:
                self._fail(P2PProtocolError("WebRTC peer sent a noninitial frame chunk."))
                return
            self._buffer = bytearray()
        expected_offset = len(self._buffer)
        if offset != expected_offset:
            self._fail(P2PProtocolError("WebRTC peer sent out-of-order frame chunks."))
            return
        self._buffer.extend(message[_CHUNK_HEADER_BYTES:])
        if len(self._buffer) > length:
            self._fail(P2PProtocolError("WebRTC peer exceeded the declared frame length."))
            return
        if len(self._buffer) == length:
            frame = bytes(self._buffer)
            self._buffer = None
            self._incoming.put_nowait(frame)

    def _on_close(self) -> None:
        self._closed = True
        self._buffer_low.set()
        self._incoming.put_nowait(None)

    def _on_buffer_low(self) -> None:
        self._buffer_low.set()

    def _on_error(self) -> None:
        self._fail(P2PConnectionClosed("WebRTC data channel failed."))

    def _fail(self, exc: Exception) -> None:
        if self._closed:
            return
        self._closed = True
        self._buffer_low.set()
        self._incoming.put_nowait(None)
        self._peer.fail(exc)

    async def send(self, frame: bytes) -> None:
        if self._closed or self._datachannel.readyState != "open":
            raise P2PConnectionClosed("WebRTC peer channel is closed.")
        if not isinstance(frame, bytes):
            raise TypeError("Peer frame must be bytes.")
        if not frame or len(frame) > MAX_FRAME_BYTES:
            raise P2PProtocolError("WebRTC peer frame has an invalid length.")
        try:
            for offset in range(0, len(frame), _WEBRTC_CHUNK_BYTES):
                if self._closed or self._datachannel.readyState != "open":
                    raise P2PConnectionClosed("WebRTC peer channel is closed.")
                while getattr(self._datachannel, "bufferedAmount", 0) >= _WEBRTC_BUFFER_LIMIT_BYTES:
                    self._buffer_low.clear()
                    if self._closed or self._datachannel.readyState != "open":
                        raise P2PConnectionClosed("WebRTC peer channel is closed.")
                    if getattr(self._datachannel, "bufferedAmount", 0) < _WEBRTC_BUFFER_LIMIT_BYTES:
                        break
                    await self._buffer_low.wait()
                chunk = frame[offset:offset + _WEBRTC_CHUNK_BYTES]
                packet = (
                    len(frame).to_bytes(4, "big")
                    + offset.to_bytes(4, "big")
                    + chunk
                )
                self._datachannel.send(packet)
        except (ConnectionError, OSError, RuntimeError, ValueError) as exc:
            self._closed = True
            raise P2PConnectionClosed("WebRTC peer channel closed during send.") from exc

    async def receive(self) -> bytes | None:
        if self._peer.error is not None:
            raise self._peer.error
        frame = await self._incoming.get()
        if frame is None and self._peer.error is not None:
            raise self._peer.error
        return frame

    def close(self) -> None:
        if self._closed:
            if self._datachannel.readyState != "closed":
                self._datachannel.close()
            return
        self._closed = True
        self._buffer_low.set()
        self._incoming.put_nowait(None)
        self._datachannel.close()


class WebRTCPeer:
    """One side of a signaled WebRTC connection with device-pinned HXP1 auth."""

    def __init__(
        self,
        identity: PeerIdentity,
        connection: Any,
        *,
        initiator: bool,
    ) -> None:
        self.identity = identity
        self._connection = connection
        self._initiator = initiator
        self._channel: _WebRTCChannel | None = None
        self._open = asyncio.Event()
        self.error: Exception | None = None
        self._close_task: asyncio.Task[Any] | None = None
        connection.on("datachannel", self._on_datachannel)
        connection.on("connectionstatechange", self._on_connection_state)

    def _on_datachannel(self, channel: Any) -> None:
        if self._initiator or self._channel is not None:
            self.fail(P2PProtocolError("Unexpected WebRTC data channel."))
            channel.close()
            return
        if channel.label != P2P_SUBPROTOCOL:
            self.fail(P2PHandshakeError("WebRTC data channel has the wrong protocol."))
            channel.close()
            return
        self._set_channel(channel)

    def _set_channel(self, channel: Any) -> None:
        self._channel = _WebRTCChannel(self, channel)
        if channel.readyState == "open":
            self._open.set()
        else:
            channel.on("open", self._on_open)

    def _on_open(self) -> None:
        self._open.set()

    def _on_connection_state(self) -> None:
        if self._connection.connectionState in {"failed", "closed"}:
            self.fail(P2PConnectionClosed("WebRTC peer connection closed."))

    def fail(self, exc: Exception) -> None:
        if self.error is None:
            self.error = exc
        if self._channel is not None:
            self._channel.close()
        self._open.set()

    @property
    def description(self) -> dict[str, str]:
        local = self._connection.localDescription
        if local is None:
            raise P2PTransportError("WebRTC local description is not ready.")
        return {"type": local.type, "sdp": local.sdp}

    def status(self) -> dict[str, Any]:
        return {
            "transport": "webrtc",
            "role": "initiator" if self._initiator else "responder",
            "connection_state": self._connection.connectionState,
            "ice_gathering_state": self._connection.iceGatheringState,
            "data_channel": (
                self._channel._datachannel.readyState if self._channel is not None else None
            ),
        }

    async def _wait_open(self, timeout: float) -> None:
        await asyncio.wait_for(self._open.wait(), timeout=timeout)
        if self.error is not None:
            raise self.error
        if self._channel is None:
            raise P2PTransportError("WebRTC peer did not open a data channel.")

    async def connect(
        self,
        remote_description: dict[str, str],
        *,
        expected_peer: PeerAddress,
        timeout: float = 15.0,
    ) -> SecurePeerSession:
        if not self._initiator:
            raise ValueError("Only the WebRTC initiator can connect.")
        if not isinstance(expected_peer, PeerAddress):
            raise ValueError("expected_peer must be a PeerAddress.")
        wait = _validate_network_timeout(timeout, "timeout")
        description = _validate_description(remote_description, "answer")
        from aiortc import RTCSessionDescription

        try:
            async with asyncio.timeout(wait):
                await self._connection.setRemoteDescription(RTCSessionDescription(
                    sdp=description["sdp"], type="answer"
                ))
                await self._wait_open(wait)
                assert self._channel is not None
                return await _initiate_handshake(self._channel, self.identity, expected_peer)
        except P2PTransportError as exc:
            self.fail(exc)
            await self.close()
            raise
        except asyncio.TimeoutError as exc:
            error = P2PTransportError("WebRTC connection timed out.")
            self.fail(error)
            await self.close()
            raise error from exc
        except Exception as exc:
            self.fail(exc)
            await self.close()
            raise P2PTransportError("WebRTC connection failed.") from exc
        except asyncio.CancelledError:
            await self.close()
            raise

    async def accept(
        self,
        *,
        expected_peer: PeerAddress,
        timeout: float = 15.0,
    ) -> SecurePeerSession:
        if self._initiator:
            raise ValueError("Only the WebRTC responder can accept.")
        if not isinstance(expected_peer, PeerAddress):
            raise ValueError("expected_peer must be a PeerAddress.")
        wait = _validate_network_timeout(timeout, "timeout")
        try:
            async with asyncio.timeout(wait):
                await self._wait_open(wait)
                assert self._channel is not None
                return await _respond_handshake(
                    self._channel, self.identity, expected_peer
                )
        except P2PTransportError as exc:
            self.fail(exc)
            await self.close()
            raise
        except asyncio.TimeoutError as exc:
            error = P2PTransportError("WebRTC acceptance timed out.")
            self.fail(error)
            await self.close()
            raise error from exc
        except Exception as exc:
            self.fail(exc)
            await self.close()
            raise P2PTransportError("WebRTC acceptance failed.") from exc
        except asyncio.CancelledError:
            await self.close()
            raise

    async def close(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._connection.close())
        if self._channel is not None:
            self._channel.close()
        try:
            await self._close_task
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
        except Exception:
            pass

    async def wait_closed(self) -> None:
        if self._close_task is not None:
            try:
                await self._close_task
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
            except Exception:
                pass


async def _new_peer(
    identity: PeerIdentity,
    *,
    initiator: bool,
    stun_servers: Collection[str] | None,
    timeout: float,
) -> WebRTCPeer:
    from aiortc import RTCPeerConnection, RTCConfiguration

    if not isinstance(identity, PeerIdentity):
        raise ValueError("identity must be a PeerIdentity.")
    wait = _validate_network_timeout(timeout, "timeout")
    servers = _validate_stun_servers(stun_servers)
    connection = RTCPeerConnection(RTCConfiguration(
        iceServers=[{"urls": server} for server in servers]
    ))
    peer = WebRTCPeer(identity, connection, initiator=initiator)
    try:
        if initiator:
            channel = connection.createDataChannel(
                P2P_SUBPROTOCOL, ordered=True, maxPacketLifeTime=None
            )
            peer._set_channel(channel)
        return peer
    except BaseException:
        await peer.close()
        raise


async def _gather_ice(peer: WebRTCPeer, description_type: str, timeout: float) -> None:
    connection = peer._connection
    if connection.iceGatheringState == "complete":
        return
    complete = asyncio.Event()

    def check_state() -> None:
        if connection.iceGatheringState == "complete":
            complete.set()

    connection.on("icegatheringstatechange", check_state)
    try:
        async with asyncio.timeout(timeout):
            if description_type == "offer":
                await connection.setLocalDescription(await connection.createOffer())
            else:
                await connection.setLocalDescription(await connection.createAnswer())
            check_state()
            await complete.wait()
        if connection.localDescription.type != description_type:
            raise P2PTransportError("WebRTC produced the wrong description type.")
    except asyncio.TimeoutError as exc:
        raise P2PTransportError("WebRTC ICE gathering timed out.") from exc
    finally:
        connection.remove_listener("icegatheringstatechange", check_state)


async def create_webrtc_offer(
    identity: PeerIdentity,
    *,
    stun_servers: Collection[str] | None = None,
    timeout: float = 15.0,
) -> WebRTCPeer:
    """Create an SDP offer to exchange through an authenticated signaling channel."""
    peer = await _new_peer(
        identity, initiator=True, stun_servers=stun_servers, timeout=timeout
    )
    try:
        await _gather_ice(peer, "offer", timeout)
        return peer
    except BaseException:
        await peer.close()
        raise


async def create_webrtc_answer(
    offer_description: dict[str, str],
    identity: PeerIdentity,
    *,
    stun_servers: Collection[str] | None = None,
    timeout: float = 15.0,
) -> WebRTCPeer:
    """Create an SDP answer; call `accept()` after sending it to the initiator."""
    description = _validate_description(offer_description, "offer")
    peer = await _new_peer(
        identity, initiator=False, stun_servers=stun_servers, timeout=timeout
    )
    try:
        from aiortc import RTCSessionDescription

        async with asyncio.timeout(timeout):
            await peer._connection.setRemoteDescription(
                RTCSessionDescription(sdp=description["sdp"], type="offer")
            )
        await _gather_ice(peer, "answer", timeout)
        return peer
    except BaseException:
        await peer.close()
        raise
