"""Authenticated, chunked team-turn transport over secure peer sessions."""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from .p2p_transport import P2PProtocolError, SecurePeerSession
from .team_mesh import (
    SignedTurnCommitBundle,
    TeamMeshCoordinator,
    canonical_json_bytes,
    sign_turn_ack,
    verify_turn_ack,
)
from .team_replication import team_payload_digest
from .updater import verify_signed_envelope

TEAM_PROTOCOL_VERSION = 1
_CHUNK_MAGIC = b"HEX/TC1\0"
_CHUNK_BYTES = 512 * 1024
_MAX_PAYLOAD_BYTES = 64 * 1024 * 1024
_MAX_CONTROL_BYTES = 1024 * 1024
_MAX_CHUNKS = 128
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class TeamTransportError(Exception):
    pass


@dataclass(slots=True)
class _PendingTurn:
    signed: SignedTurnCommitBundle
    payload: Mapping[str, Any]
    digest: str
    future: asyncio.Future[dict[str, Any]]


@dataclass(slots=True)
class _IncomingTurn:
    signed: SignedTurnCommitBundle
    digest: str
    payload_bytes: int
    chunk_count: int
    chunks: list[bytes]


def _load_json(raw: bytes) -> dict[str, Any]:
    def object_from_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise TeamTransportError("Duplicate team control key.")
            value[key] = item
        return value

    def reject_constant(value: str) -> Any:
        raise TeamTransportError("Invalid team control constant.")

    try:
        parsed = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=object_from_pairs,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise TeamTransportError("Invalid team control JSON.") from exc
    if not isinstance(parsed, dict):
        raise TeamTransportError("Team control message must be an object.")
    return parsed


def _require_fields(value: Mapping[str, Any], expected: set[str]) -> None:
    if set(value) != expected:
        raise TeamTransportError("Unexpected team control message fields.")


def _positive_int(value: Any, label: str) -> int:
    if type(value) is not int or value < 1:
        raise TeamTransportError(f"Invalid team {label}.")
    return value


async def _send_control(session: SecurePeerSession, message_type: str, **fields: Any) -> None:
    raw = canonical_json_bytes(
        {"protocol_version": TEAM_PROTOCOL_VERSION, "type": message_type, **fields}
    )
    if len(raw) > _MAX_CONTROL_BYTES:
        raise TeamTransportError("Team control message is too large.")
    await session.send(raw)


class TeamPeerLink:
    """Multiplex ordered team turns and catch-up over one authenticated session."""

    def __init__(
        self,
        session: SecurePeerSession,
        mesh: TeamMeshCoordinator,
        signer: Any,
        device_key_provider: Callable[[str], str | None],
        *,
        server_public_key_hex: str | None = None,
        commit_turn: Callable[[SignedTurnCommitBundle], Awaitable[Mapping[str, Any]]] | None = None,
        history_provider: Callable[[str, int, int], list[dict[str, Any]]] | None = None,
        ack_sink: Callable[[dict[str, Any]], Awaitable[Any]] | None = None,
        timeout: float = 120.0,
    ) -> None:
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Team transport timeout must be finite and positive.")
        if commit_turn is not None and not server_public_key_hex:
            raise ValueError("A server key is required to verify Coordinator commit receipts.")
        self.session = session
        self.mesh = mesh
        self.signer = signer
        self.device_key_provider = device_key_provider
        self.server_public_key_hex = server_public_key_hex
        self.commit_turn = commit_turn
        self.history_provider = history_provider or (lambda _session, _first, _last: [])
        self.ack_sink = ack_sink
        self.timeout = float(timeout)
        self.remote_device_id = session.remote_device_id
        self._pending: dict[str, _PendingTurn] = {}
        self._outbound: asyncio.Queue[_PendingTurn] = asyncio.Queue()
        self._incoming: _IncomingTurn | None = None
        self._current_digest: str | None = None
        self._receive_task: asyncio.Task[None] | None = None
        self._send_task: asyncio.Task[None] | None = None
        self._hello_sent = False
        self._remote_hello: dict[str, Any] | None = None
        self._closed = False
        self._last_error: str | None = None

    @property
    def closed(self) -> bool:
        return self._closed or self.session.closed

    def status(self) -> dict[str, Any]:
        return {
            **self.session.status(),
            "closed": self.closed,
            "pending_turns": len(self._pending),
            "receiving_turn": self._incoming is not None,
            "remote_role": (self._remote_hello or {}).get("role"),
            "last_error": self._last_error,
        }

    async def start(self) -> None:
        if self._receive_task is not None or self._closed:
            return
        await self._send_hello()
        self._send_task = asyncio.create_task(self._outbound_worker(), name="hex-team-send")
        self._receive_task = asyncio.create_task(self._receive_loop(), name="hex-team-receive")

    async def send_turn(
        self,
        signed: SignedTurnCommitBundle,
        payload: Mapping[str, Any],
        *,
        commit: bool = True,
    ) -> dict[str, Any]:
        if self.closed or self._receive_task is None:
            raise TeamTransportError("Team peer link is not running.")
        if self.mesh.mode != "TEAM_PRIMARY_HOST":
            raise TeamTransportError("Only the active Coordinator can relay team turns.")
        if commit:
            await self.commit_signed(signed)
        return await self._enqueue(signed, payload, wait=True)

    async def commit_signed(self, signed: SignedTurnCommitBundle) -> None:
        if self.commit_turn is None:
            raise TeamTransportError("No Coordinator backend commit verifier is configured.")
        signed = SignedTurnCommitBundle.from_dict(signed.to_dict())
        if self.mesh.mode != "TEAM_PRIMARY_HOST":
            raise TeamTransportError("Only the active Coordinator can submit team turns.")
        public_key = self.device_key_provider(signed.author_device_record_id)
        if not public_key or not signed.verify(public_key):
            raise TeamTransportError("Team turn author signature verification failed.")
        if (
            signed.bundle.project_session_id != self.mesh.project_session_id
            or signed.bundle.host_epoch > self.mesh.current_epoch
        ):
            raise TeamTransportError("Team turn does not match the active Coordinator scope.")
        if signed.bundle.host_epoch < self.mesh.current_epoch:
            if signed.bundle.turn_seq > self.mesh.committed_turn_seq:
                raise TeamTransportError("Historical team turn exceeds the trusted lease sequence.")
        elif signed.bundle.turn_seq > self.mesh.committed_turn_seq + 1:
            raise TeamTransportError("Team turn sequence is not contiguous with the Coordinator lease.")
        receipt = self.commit_turn(signed)
        if inspect.isawaitable(receipt):
            receipt = await receipt
        if not isinstance(receipt, Mapping):
            raise TeamTransportError("Coordinator commit receipt is malformed.")
        self._verify_receipt(signed, receipt)
        self.mesh.note_committed_turn(
            signed.bundle.project_session_id,
            signed.bundle.host_epoch,
            signed.bundle.turn_seq,
        )

    async def request_catchup(self) -> bool:
        missing = self.mesh.missing_turn_range
        if missing is None or self.mesh.project_session_id is None:
            return False
        await _send_control(
            self.session,
            "team_catchup_request",
            project_session_id=self.mesh.project_session_id,
            first_seq=missing[0],
            last_seq=missing[1],
        )
        return True

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks = [task for task in (self._receive_task, self._send_task) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for pending in tuple(self._pending.values()):
            if not pending.future.done():
                pending.future.set_exception(TeamTransportError("Team peer link closed."))
        self._pending.clear()
        await self.session.close()

    def _fail_pending(self, message: str) -> None:
        for pending in tuple(self._pending.values()):
            if not pending.future.done():
                pending.future.set_exception(TeamTransportError(message))
        self._pending.clear()

    def _verify_receipt(
        self, signed: SignedTurnCommitBundle, receipt: Mapping[str, Any]
    ) -> None:
        if not self.server_public_key_hex or not verify_signed_envelope(
            receipt, self.server_public_key_hex
        ):
            raise TeamTransportError("Coordinator commit receipt signature verification failed.")
        value = receipt.get("payload")
        required = {
            "project_session_id", "host_epoch", "primary_device_record_id",
            "author_device_record_id", "turn_seq", "bundle_digest", "proof_merkle_root",
        }
        if not isinstance(value, Mapping) or not required.issubset(value):
            raise TeamTransportError("Coordinator commit receipt is malformed.")
        bundle = signed.bundle
        if (
            value["project_session_id"] != bundle.project_session_id
            or value["host_epoch"] != bundle.host_epoch
            or value["turn_seq"] != bundle.turn_seq
            or value["author_device_record_id"] != signed.author_device_record_id
            or value["bundle_digest"] != bundle.compute_digest()
            or value["proof_merkle_root"] != bundle.proof_merkle_root
        ):
            raise TeamTransportError("Coordinator commit receipt does not match the signed turn.")

    async def _enqueue(
        self, signed: SignedTurnCommitBundle, payload: Mapping[str, Any], *, wait: bool
    ) -> dict[str, Any]:
        parsed = SignedTurnCommitBundle.from_dict(signed.to_dict())
        digest = team_payload_digest(payload)
        public_key = self.device_key_provider(parsed.author_device_record_id)
        if not public_key or not parsed.verify(public_key):
            raise TeamTransportError("Team turn author signature verification failed.")
        if parsed.bundle.project_session_id != self.mesh.project_session_id:
            raise TeamTransportError("Team turn belongs to a different session.")
        if parsed.bundle.host_epoch > self.mesh.current_epoch:
            raise TeamTransportError("Team turn is from a future epoch.")
        bundle_digest = parsed.bundle.compute_digest()
        if bundle_digest in self._pending:
            raise TeamTransportError("Team turn is already awaiting acknowledgement.")
        future = asyncio.get_running_loop().create_future()
        pending = _PendingTurn(parsed, payload, digest, future)
        self._pending[bundle_digest] = pending
        await self._outbound.put(pending)
        if wait:
            try:
                return await asyncio.wait_for(future, timeout=self.timeout)
            except Exception as exc:
                if self._pending.get(bundle_digest) is pending:
                    self._pending.pop(bundle_digest, None)
                if isinstance(exc, TeamTransportError):
                    raise
                raise TeamTransportError("Team turn acknowledgement timed out.") from exc
        future.add_done_callback(self._consume_future)
        asyncio.create_task(self._expire_future(bundle_digest, future))
        return {"accepted": False, "queued": True}

    @staticmethod
    def _consume_future(future: asyncio.Future[dict[str, Any]]) -> None:
        if not future.cancelled():
            future.exception()

    async def _expire_future(
        self, bundle_digest: str, future: asyncio.Future[dict[str, Any]]
    ) -> None:
        try:
            await asyncio.wait_for(asyncio.shield(future), timeout=self.timeout)
        except Exception:
            pending = self._pending.get(bundle_digest)
            if pending is not None and pending.future is future and not future.done():
                future.cancel()
                self._pending.pop(bundle_digest, None)

    async def _outbound_worker(self) -> None:
        while True:
            pending = await self._outbound.get()
            try:
                await self._send_record(pending.signed, pending.payload)
            except Exception as exc:
                self._last_error = type(exc).__name__
                if not pending.future.done():
                    pending.future.set_exception(TeamTransportError("Team turn delivery failed."))
                self._pending.pop(pending.signed.bundle.compute_digest(), None)
            finally:
                self._outbound.task_done()

    async def _send_record(
        self, signed: SignedTurnCommitBundle, payload: Mapping[str, Any]
    ) -> None:
        raw = canonical_json_bytes(payload)
        digest = team_payload_digest(payload)
        if len(raw) > _MAX_PAYLOAD_BYTES:
            raise TeamTransportError("Team replication payload is too large.")
        chunk_count = (len(raw) + _CHUNK_BYTES - 1) // _CHUNK_BYTES
        if chunk_count > _MAX_CHUNKS:
            raise TeamTransportError("Team replication payload has too many chunks.")
        await _send_control(
            self.session,
            "team_turn_begin",
            signed=signed.to_dict(),
            payload_digest=digest,
            payload_bytes=len(raw),
            chunk_count=chunk_count,
        )
        for index in range(chunk_count):
            chunk = raw[index * _CHUNK_BYTES : (index + 1) * _CHUNK_BYTES]
            await self.session.send(_CHUNK_MAGIC + index.to_bytes(4, "big") + chunk)

    async def _receive_loop(self) -> None:
        try:
            while not self.closed:
                raw = await self.session.receive()
                if raw is None:
                    self._closed = True
                    self._fail_pending("Team peer link closed.")
                    break
                try:
                    if raw.startswith(_CHUNK_MAGIC):
                        await self._handle_chunk(raw)
                    else:
                        await self._handle_control(raw)
                except P2PProtocolError:
                    raise
                except Exception as exc:
                    self._last_error = type(exc).__name__
                    digest = self._current_digest or ""
                    self._incoming = None
                    self._current_digest = None
                    await self._try_error(digest)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._last_error = type(exc).__name__
            self._closed = True
            self._fail_pending("Team peer link failed.")
            await self.session.close()

    async def _try_error(self, digest: str) -> bool:
        try:
            await _send_control(
                self.session, "team_turn_error", payload_digest=digest, error_code="INVALID_TURN"
            )
            return True
        except Exception:
            return False

    async def _handle_control(self, raw: bytes) -> None:
        if len(raw) > _MAX_CONTROL_BYTES:
            raise TeamTransportError("Team control frame is too large.")
        message = _load_json(raw)
        if type(message.get("protocol_version")) is not int or message["protocol_version"] != TEAM_PROTOCOL_VERSION:
            raise TeamTransportError("Unsupported team protocol version.")
        message_type = message.get("type")
        fields = {key: value for key, value in message.items() if key not in {"protocol_version", "type"}}
        if message_type == "team_hello":
            await self._handle_hello(fields)
        elif message_type == "team_turn_begin":
            await self._handle_begin(fields)
        elif message_type == "team_turn_ack":
            self._handle_ack(fields)
        elif message_type == "team_turn_error":
            self._handle_error(fields)
        elif message_type == "team_catchup_request":
            await self._handle_catchup_request(fields)
        elif message_type == "team_catchup_result":
            self._handle_catchup_result(fields)
        else:
            raise TeamTransportError("Unknown team control message.")

    async def _send_hello(self) -> None:
        if self._hello_sent:
            return
        await _send_control(
            self.session,
            "team_hello",
            device_id=self.mesh.local_device_id,
            role="coordinator" if self.mesh.mode == "TEAM_PRIMARY_HOST" else "replica",
            project_session_id=self.mesh.project_session_id,
            host_epoch=self.mesh.current_epoch,
            committed_turn_seq=self.mesh.committed_turn_seq,
            applied_turn_seq=self.mesh.last_applied_turn_seq,
        )
        self._hello_sent = True

    async def _handle_hello(self, fields: Mapping[str, Any]) -> None:
        _require_fields(fields, {
            "device_id", "role", "project_session_id", "host_epoch",
            "committed_turn_seq", "applied_turn_seq",
        })
        if fields["device_id"] != self.remote_device_id:
            raise TeamTransportError("Team hello does not match the authenticated peer.")
        if fields["role"] not in {"coordinator", "replica"}:
            raise TeamTransportError("Invalid team hello role.")
        session_id = fields["project_session_id"]
        if not isinstance(session_id, str) or self.mesh.project_session_id not in {None, session_id}:
            raise TeamTransportError("Team hello belongs to a different session.")
        epoch = _positive_int(fields["host_epoch"], "hello epoch")
        committed = fields["committed_turn_seq"]
        applied = fields["applied_turn_seq"]
        if type(committed) is not int or type(applied) is not int or not 0 <= applied <= committed:
            raise TeamTransportError("Invalid team hello sequence state.")
        if fields["role"] == "coordinator" and self.mesh.primary_device_id not in {None, self.remote_device_id}:
            raise TeamTransportError("Peer is not the trusted Coordinator.")
        if fields["role"] == "coordinator" and epoch > self.mesh.current_epoch:
            raise TeamTransportError("Peer advertises a future Coordinator epoch.")
        self._remote_hello = dict(fields)
        await self._send_hello()
        if fields["role"] == "coordinator" and self.mesh.missing_turn_range is not None:
            await self.request_catchup()

    async def _handle_begin(self, fields: Mapping[str, Any]) -> None:
        _require_fields(fields, {"signed", "payload_digest", "payload_bytes", "chunk_count"})
        if self._incoming is not None:
            raise TeamTransportError("A team turn is already being received.")
        signed = SignedTurnCommitBundle.from_dict(fields["signed"])
        digest = fields["payload_digest"]
        if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
            raise TeamTransportError("Invalid team payload digest.")
        self._current_digest = digest
        payload_bytes = fields["payload_bytes"]
        chunk_count = fields["chunk_count"]
        if type(payload_bytes) is not int or not 0 < payload_bytes <= _MAX_PAYLOAD_BYTES:
            raise TeamTransportError("Invalid team payload size.")
        if type(chunk_count) is not int or not 1 <= chunk_count <= _MAX_CHUNKS:
            raise TeamTransportError("Invalid team chunk count.")
        if chunk_count != (payload_bytes + _CHUNK_BYTES - 1) // _CHUNK_BYTES:
            raise TeamTransportError("Team chunk count does not match the payload size.")
        public_key = self.device_key_provider(signed.author_device_record_id)
        if not public_key or not signed.verify(public_key):
            raise TeamTransportError("Team turn author signature verification failed.")
        if signed.bundle.project_session_id != self.mesh.project_session_id:
            raise TeamTransportError("Team turn belongs to a different session.")
        if signed.bundle.host_epoch > self.mesh.current_epoch:
            raise TeamTransportError("Team turn is from a future epoch.")
        self._incoming = _IncomingTurn(signed, digest, payload_bytes, chunk_count, [])

    async def _handle_chunk(self, raw: bytes) -> None:
        incoming = self._incoming
        if incoming is None:
            raise TeamTransportError("Unexpected team content chunk.")
        prefix = len(_CHUNK_MAGIC) + 4
        if len(raw) <= prefix:
            raise TeamTransportError("Team content chunk is too small.")
        index = int.from_bytes(raw[len(_CHUNK_MAGIC) : prefix], "big")
        expected_index = len(incoming.chunks)
        if index != expected_index:
            raise TeamTransportError("Team content chunks are replayed or out of order.")
        chunk = raw[prefix:]
        expected_size = min(
            _CHUNK_BYTES, incoming.payload_bytes - expected_index * _CHUNK_BYTES
        )
        if len(chunk) != expected_size:
            raise TeamTransportError("Team content chunk has an invalid size.")
        incoming.chunks.append(chunk)
        if len(incoming.chunks) != incoming.chunk_count:
            return
        self._incoming = None
        payload_raw = b"".join(incoming.chunks)
        payload = _load_json(payload_raw)
        if team_payload_digest(payload) != incoming.digest:
            raise TeamTransportError("Team payload digest does not match its announcement.")
        public_key = self.device_key_provider(incoming.signed.author_device_record_id)
        if not public_key:
            raise TeamTransportError("Enrolled team author key is unavailable.")
        result = self.mesh.accept_turn_commit(
            incoming.signed,
            {incoming.signed.author_device_record_id: public_key},
            sender_device_id=self.remote_device_id,
            replication_payload=payload,
        )
        bundle = incoming.signed.bundle
        ack = sign_turn_ack(
            self.signer,
            bundle.project_session_id,
            bundle.host_epoch,
            bundle.turn_seq,
            bundle.compute_digest(),
        )
        await _send_control(
            self.session,
            "team_turn_ack",
            **ack,
            accepted=True,
            payload_digest=incoming.digest,
        )
        self._current_digest = None
        if self.ack_sink is not None:
            try:
                sink_result = self.ack_sink(ack)
                if inspect.isawaitable(sink_result):
                    await sink_result
            except Exception as exc:
                self._last_error = type(exc).__name__
        del result

    def _handle_ack(self, fields: Mapping[str, Any]) -> None:
        _require_fields(fields, {
            "project_session_id", "host_epoch", "turn_seq", "bundle_digest",
            "signature", "accepted", "payload_digest",
        })
        if (self._remote_hello or {}).get("role") != "replica":
            raise TeamTransportError("Only a replica peer can acknowledge a team turn.")
        if fields["accepted"] is not True:
            raise TeamTransportError("Team turn was not acknowledged.")
        public_key = self.device_key_provider(self.remote_device_id)
        if not public_key:
            raise TeamTransportError("Enrolled peer key is unavailable.")
        ack = {key: fields[key] for key in (
            "project_session_id", "host_epoch", "turn_seq", "bundle_digest", "signature"
        )}
        verify_turn_ack(ack, public_key)
        pending = self._pending.get(fields["bundle_digest"])
        if pending is None:
            return
        bundle = pending.signed.bundle
        if (
            fields["project_session_id"] != bundle.project_session_id
            or fields["host_epoch"] != bundle.host_epoch
            or fields["turn_seq"] != bundle.turn_seq
            or fields["payload_digest"] != pending.digest
        ):
            raise TeamTransportError("Team acknowledgement does not match the pending turn.")
        if not pending.future.done():
            pending.future.set_result({"accepted": True, "ack": ack})
        self._pending.pop(fields["bundle_digest"], None)

    def _handle_error(self, fields: Mapping[str, Any]) -> None:
        _require_fields(fields, {"payload_digest", "error_code"})
        if fields["error_code"] != "INVALID_TURN":
            raise TeamTransportError("Invalid team error code.")
        for bundle_digest, pending in tuple(self._pending.items()):
            if pending.digest != fields["payload_digest"]:
                continue
            if not pending.future.done():
                pending.future.set_exception(TeamTransportError("Peer rejected the team turn."))
            self._pending.pop(bundle_digest, None)
            return

    async def _handle_catchup_request(self, fields: Mapping[str, Any]) -> None:
        _require_fields(fields, {"project_session_id", "first_seq", "last_seq"})
        if self.mesh.mode != "TEAM_PRIMARY_HOST":
            raise TeamTransportError("Only the Coordinator can serve catch-up turns.")
        if (self._remote_hello or {}).get("role") != "replica":
            raise TeamTransportError("Only a replica peer can request catch-up.")
        if fields["project_session_id"] != self.mesh.project_session_id:
            raise TeamTransportError("Catch-up request belongs to a different session.")
        first = _positive_int(fields["first_seq"], "catch-up sequence")
        last = _positive_int(fields["last_seq"], "catch-up sequence")
        if first > last or last > self.mesh.committed_turn_seq:
            raise TeamTransportError("Invalid catch-up range.")
        records = self.history_provider(self.mesh.project_session_id, first, last)
        by_sequence: dict[int, dict[str, Any]] = {}
        for record in records:
            if not isinstance(record, Mapping) or not isinstance(record.get("signed"), Mapping):
                raise TeamTransportError("Catch-up history contains a malformed record.")
            sequence = record["signed"].get("bundle", {}).get("turn_seq")
            if type(sequence) is not int or sequence in by_sequence:
                raise TeamTransportError("Catch-up history contains a duplicate or invalid sequence.")
            by_sequence[sequence] = dict(record)
        available: list[int] = []
        for sequence in range(first, last + 1):
            record = by_sequence.get(sequence)
            if record is None:
                break
            signed = SignedTurnCommitBundle.from_dict(record["signed"])
            if signed.bundle.project_session_id != self.mesh.project_session_id:
                raise TeamTransportError("Catch-up history belongs to a different session.")
            await self._enqueue(signed, record["payload"], wait=False)
            available.append(sequence)
        await _send_control(
            self.session,
            "team_catchup_result",
            project_session_id=self.mesh.project_session_id,
            first_seq=first,
            last_seq=last,
            available_seq=available,
        )

    def _handle_catchup_result(self, fields: Mapping[str, Any]) -> None:
        _require_fields(fields, {"project_session_id", "first_seq", "last_seq", "available_seq"})
        if fields["project_session_id"] != self.mesh.project_session_id:
            raise TeamTransportError("Catch-up result belongs to a different session.")
        first = _positive_int(fields["first_seq"], "catch-up sequence")
        last = _positive_int(fields["last_seq"], "catch-up sequence")
        if first > last:
            raise TeamTransportError("Invalid catch-up result range.")
        available = fields["available_seq"]
        if (
            not isinstance(available, list)
            or len(available) > 10_000
            or any(type(sequence) is not int for sequence in available)
        ):
            raise TeamTransportError("Invalid catch-up availability list.")
        expected = list(range(first, first + len(available)))
        if available != expected or (available and available[-1] > last):
            raise TeamTransportError("Catch-up history is not contiguous.")


def make_backend_turn_committer(
    client: Any,
    access_token_provider: Callable[[], str | None],
    coordinator_device_record_id: str,
) -> Callable[[SignedTurnCommitBundle], Awaitable[Mapping[str, Any]]]:
    """Adapt the synchronous control-plane client without blocking the peer event loop."""
    async def commit(signed: SignedTurnCommitBundle) -> Mapping[str, Any]:
        access_token = access_token_provider()
        if not access_token:
            raise TeamTransportError("A backend access token is required to commit a team turn.")
        return await asyncio.to_thread(
            client.commit_session_turn,
            access_token,
            signed.bundle.project_session_id,
            coordinator_device_record_id,
            signed.to_dict(),
        )

    return commit


class TeamTurnPublisher:
    """Commit once at the Coordinator, then require an ACK from every linked replica."""

    def __init__(self, links: list[TeamPeerLink]) -> None:
        self.links = list(links)

    async def __call__(
        self, signed: SignedTurnCommitBundle, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        if not self.links:
            return {"accepted": False}
        await self.links[0].commit_signed(signed)
        results = await asyncio.gather(
            *(link.send_turn(signed, payload, commit=False) for link in self.links),
            return_exceptions=True,
        )
        accepted = all(
            isinstance(result, Mapping) and result.get("accepted") is True
            for result in results
        )
        return {"accepted": accepted}
