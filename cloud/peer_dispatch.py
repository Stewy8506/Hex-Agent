"""Dispatch the first authenticated peer frame to personal sync or Teams."""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping, Awaitable

_MAX_HELLO_BYTES = 1024 * 1024


class PeerDispatchError(ValueError):
    """The first peer frame does not identify a supported protocol."""


class ReplayPeerSession:
    """Present one already-consumed frame to a protocol session."""

    def __init__(self, session: Any, first_frame: bytes) -> None:
        self._session = session
        self._first_frame: bytes | None = first_frame

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    async def receive(self) -> bytes | None:
        if self._first_frame is not None:
            frame, self._first_frame = self._first_frame, None
            return frame
        return await self._session.receive()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise PeerDispatchError("Peer hello contains duplicate fields.")
        value[key] = item
    return value


def _reject_constant(_value: str) -> None:
    raise PeerDispatchError("Peer hello contains an unsupported JSON value.")


def read_hello(raw: bytes | None) -> tuple[str, Mapping[str, Any]]:
    if raw is None or len(raw) > _MAX_HELLO_BYTES:
        raise PeerDispatchError("Peer closed or sent an oversized hello.")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                           parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, PeerDispatchError):
            raise
        raise PeerDispatchError("Peer hello is not valid bounded JSON.") from exc
    if not isinstance(value, dict) or type(value.get("protocol_version")) is not int:
        raise PeerDispatchError("Peer hello is malformed.")
    if value["protocol_version"] != 1 or not isinstance(value.get("type"), str):
        raise PeerDispatchError("Peer hello uses an unsupported protocol.")
    message_type = value["type"]
    if message_type == "sync_hello":
        required = {"device_id", "workspace_id", "capabilities"}
    elif message_type == "team_hello":
        required = {
            "device_id", "role", "project_session_id", "host_epoch",
            "committed_turn_seq", "applied_turn_seq",
        }
    else:
        raise PeerDispatchError("Unknown peer protocol.")
    if set(value) != {"protocol_version", "type", *required}:
        raise PeerDispatchError("Peer hello fields are invalid.")
    session_id = value.get("project_session_id") if message_type == "team_hello" else value.get("workspace_id")
    if not isinstance(session_id, str) or not session_id or len(session_id) > 512:
        raise PeerDispatchError("Peer hello has an invalid session identifier.")
    return message_type, value


async def dispatch_peer_session(
    session: Any,
    peer: Any,
    sync_handler: Callable[[Any, Any], Awaitable[Any]],
    team_handler: Callable[[str, Any, Any], Awaitable[Any]],
) -> Any:
    raw = await session.receive()
    message_type, hello = read_hello(raw)
    replay = ReplayPeerSession(session, raw)
    if message_type == "sync_hello":
        return await sync_handler(replay, peer)
    return await team_handler(str(hello["project_session_id"]), replay, peer)
