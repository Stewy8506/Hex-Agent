"""Session-scoped Team Mesh runtime over authenticated peer transports."""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import json
import math
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from harness_core.storage import cow

from .cognition_sync import CognitionSyncStore
from .p2p_transport import PeerAddress, SecurePeerSession
from .team_fastlane import (
    TeamActivityError,
    TeamActivityStore,
    make_activity_event,
    verify_activity_event,
)
from .team_history import TeamHistory
from .team_mesh import DEVICE_ID_PATTERN, SignedTurnCommitBundle, TeamMeshCoordinator
from .team_outbox import TeamTurnOutbox, VerifiedTeamTurn
from .team_replication import TeamReplicationService
from .team_scope import TeamScopeAllocator
from .team_transport import TeamPeerLink, TeamTransportError, verify_turn_receipt
from .updater import ServerKeyring, verify_signed_envelope

_ELIGIBLE_ROLES = frozenset({"owner", "admin", "developer"})
_DEVICE_KEY_RE = re.compile(r"^ed25519:[0-9a-fA-F]{64}$")


class TeamRuntimeError(RuntimeError):
    """Raised when a team session cannot be started or safely synchronized."""


class TeamRuntime:
    """Own durable replication, scope allocation, and peer links for one session."""

    def __init__(
        self,
        *,
        account: Any,
        signer: Any,
        server_public_key_hex: str,
        server_key_document: Mapping[str, Any] | None = None,
        team_id: str,
        project_session_id: str,
        workspace_root: Path | str,
        data_dir: Path | str,
        ledger: Any,
        broker: Any | None = None,
        cognition: CognitionSyncStore | None = None,
        global_home: Path | str | None = None,
        on_event: Callable[[dict[str, Any]], Any] | None = None,
        peer_query_provider: Callable[
            [str, str, str], Awaitable[Any] | Any
        ] | None = None,
        peer_query_timeout: float = 15.0,
        maintenance_interval: float = 3.0,
        lease_max_age: float = 10.0,
    ) -> None:
        if not isinstance(team_id, str) or not DEVICE_ID_PATTERN.fullmatch(team_id):
            raise ValueError("Invalid team id.")
        if not isinstance(project_session_id, str) or not DEVICE_ID_PATTERN.fullmatch(
            project_session_id
        ):
            raise ValueError("Invalid project session id.")
        if maintenance_interval <= 0 or lease_max_age <= 0:
            raise ValueError("Team runtime timing must be positive.")
        if (
            not isinstance(peer_query_timeout, (int, float))
            or isinstance(peer_query_timeout, bool)
            or not math.isfinite(peer_query_timeout)
            or peer_query_timeout <= 0
        ):
            raise ValueError("Team peer query timeout must be finite and positive.")
        self.workspace_root = Path(workspace_root).resolve()
        if not self.workspace_root.is_dir():
            raise ValueError("Team workspace must be an existing directory.")
        self.root = Path(data_dir).resolve() / "teams" / project_session_id
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.account = account
        self.signer = signer
        self.local_device_id = signer.device_id
        self.server_public_key_hex = server_public_key_hex
        self.team_id = team_id
        self.project_session_id = project_session_id
        self.ledger = ledger
        self.broker = broker
        self.on_event = on_event
        self.peer_query_provider = peer_query_provider
        self.peer_query_timeout = float(peer_query_timeout)
        self.maintenance_interval = float(maintenance_interval)
        self.lease_max_age = float(lease_max_age)
        self._server_keyring_path = self.root / "server_keyring.json"
        initial_keyring = ServerKeyring(server_public_key_hex)
        persisted_keyring = self._load_server_keyring()
        if server_key_document is not None:
            initial_keyring = ServerKeyring.from_document(server_key_document)
        elif (
            persisted_keyring is not None
            and persisted_keyring.current_key_hex == initial_keyring.current_key_hex
        ):
            initial_keyring = persisted_keyring
        self._server_keys = initial_keyring
        self.server_public_key_hex = initial_keyring.current_key_hex
        self._write_server_keyring()
        self.cognition = cognition or CognitionSyncStore(
            self.workspace_root,
            global_home=global_home,
            workspace_id=project_session_id,
            ledger=ledger,
        )
        if self.cognition.workspace_id != project_session_id:
            raise ValueError("Team cognition belongs to a different project session.")
        self.replication = TeamReplicationService(
            self.root, self.workspace_root, ledger, self.cognition
        )
        self.mesh = TeamMeshCoordinator(
            self.local_device_id,
            self._server_keys,
            project_session_id,
            replication_service=self.replication,
        )
        self.history = TeamHistory(self.root)
        self.activity = TeamActivityStore(
            self.workspace_root, project_session_id
        )
        self.allocator = TeamScopeAllocator(
            self.root,
            local_device_id=self.local_device_id,
            server_public_key_hex=self._server_keys,
            lease_provider=self._lease_provider,
        )
        self.outbox = TeamTurnOutbox(
            self.root,
            ledger=ledger,
            cow=cow,
            cognition=self.cognition,
            signer=signer,
            scope_provider=self._reserve_scope,
            publisher=self._publish,
        )
        self._devices: dict[str, dict[str, Any]] = {}
        self._signed_lease: Mapping[str, Any] | None = None
        self._lease_fetched_at = 0.0
        self._links: dict[str, TeamPeerLink] = {}
        self._connections: dict[str, concurrent.futures.Future[Any]] = {}
        self._connect_attempts: dict[str, float] = {}
        self._state_lock = threading.RLock()
        self._refresh_lock = asyncio.Lock()
        self._link_lock = asyncio.Lock()
        self._commit_lock = asyncio.Lock()
        self._maintenance_task: asyncio.Task[Any] | None = None
        self._flush_scheduled = False
        self._started = False
        self._stopped = False
        self._last_error: str | None = None

    @classmethod
    async def create(cls, **kwargs: Any) -> "TeamRuntime":
        account = kwargs["account"]
        signer = kwargs.get("signer")
        server_key = kwargs.get("server_public_key_hex")
        if signer is None:
            signer = await asyncio.to_thread(account.peer_identity)
        if server_key is None:
            fetch_document = getattr(account, "server_public_key_document", None)
            if callable(fetch_document):
                document = await asyncio.to_thread(fetch_document)
                validated = ServerKeyring.from_document(document)
                server_key = validated.current_key_hex
                kwargs["server_key_document"] = document
            else:
                server_key = await asyncio.to_thread(account.server_public_key)
        kwargs["signer"] = signer
        kwargs["server_public_key_hex"] = server_key
        return cls(**kwargs)

    async def start(self) -> dict[str, Any]:
        async with self._refresh_lock:
            if self._started:
                return self.status()
            if self._stopped:
                raise TeamRuntimeError("Team runtime has been stopped.")
            await self._refresh_control_plane()
            applied = self.replication.expected_turn_seq(self.project_session_id) - 1
            if applied > self.mesh.committed_turn_seq:
                raise TeamRuntimeError("Local team state is ahead of the signed backend lease.")
            self.mesh.last_applied_turn_seq = applied
            self._started = True
            self._maintenance_task = asyncio.create_task(
                self._maintenance_loop(), name=f"hex-team-{self.project_session_id}"
            )
        self._emit("team/runtime_started", mode=self.mesh.mode, host_epoch=self.mesh.current_epoch)
        await self.flush()
        return self.status()

    async def stop(self) -> None:
        with self._state_lock:
            self._stopped = True
            self._started = False
            links = list(self._links.values())
            connections = list(self._connections.values())
        task = self._maintenance_task
        self._maintenance_task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for future in connections:
            future.cancel()
        await asyncio.gather(*(link.close() for link in links), return_exceptions=True)
        with self._state_lock:
            self._links.clear()
            self._connections.clear()
        self._emit("team/runtime_stopped")

    def __call__(self, turn: VerifiedTeamTurn) -> dict[str, Any]:
        if not self._started:
            raise TeamRuntimeError("Team runtime is not running.")
        result = self.outbox(turn)
        if result.get("status") == "queued":
            self._schedule_flush()
        return result

    async def publish(self, turn: VerifiedTeamTurn) -> dict[str, Any]:
        if not self._started:
            raise TeamRuntimeError("Team runtime is not running.")
        return await self.outbox.publish_async(turn)

    async def flush(self) -> dict[str, Any]:
        result = await self.outbox.flush_async()
        if result.get("delivered"):
            self._emit("team/outbox_flushed", delivered=result["delivered"])
        return result

    async def peer_state(self, state: str) -> dict[str, Any]:
        return await self._publish_activity(
            "PEER_STATE", f"Peer state: {state}", data={"state": state}
        )

    async def claim_scope(
        self,
        task_id: str,
        *,
        paths: list[str] | None = None,
        symbols: list[str] | None = None,
        ttl_seconds: int = 120,
        claim_id: str | None = None,
        summary: str | None = None,
    ) -> dict[str, Any]:
        return await self._publish_activity(
            "INTENT_CLAIM",
            summary or f"Scope claim for {task_id}",
            paths=paths,
            symbols=symbols,
            data={
                "task_id": task_id,
                "claim_id": claim_id or uuid.uuid4().hex,
                "ttl_seconds": ttl_seconds,
            },
        )

    async def release_scope(
        self,
        task_id: str,
        claim_id: str,
        *,
        paths: list[str] | None = None,
        symbols: list[str] | None = None,
    ) -> dict[str, Any]:
        lock = next(
            (
                item
                for item in self.activity.scope_locks(host_epoch=self.mesh.current_epoch)
                if item["author_device_record_id"] == self.local_device_id
                and item["data"]["task_id"] == task_id
                and item["data"]["claim_id"] == claim_id
            ),
            None,
        )
        if lock is None:
            raise TeamRuntimeError("No active local scope claim matches this release.")
        return await self._publish_activity(
            "SCOPE_RELEASE",
            f"Scope release for {task_id}",
            paths=paths if paths is not None else lock["paths"],
            symbols=symbols if symbols is not None else lock["symbols"],
            data={"task_id": task_id, "claim_id": claim_id},
        )

    async def contract_change(
        self,
        task_id: str,
        *,
        path: str,
        symbol: str,
        before: str,
        after: str,
    ) -> dict[str, Any]:
        return await self._publish_activity(
            "CONTRACT_CHANGE",
            f"Contract change: {path} {symbol}",
            paths=[path],
            symbols=[symbol],
            data={
                "task_id": task_id,
                "path": path,
                "symbol": symbol,
                "before": before,
                "after": after,
            },
        )

    async def blackboard_note(
        self,
        body: str,
        *,
        task_id: str | None = None,
        pinned: bool = False,
    ) -> dict[str, Any]:
        return await self._publish_activity(
            "BLACKBOARD_NOTE",
            "Shared blackboard note",
            data={"task_id": task_id, "body": body, "pinned": pinned},
        )

    def activity_status(self, *, limit: int = 50) -> dict[str, Any]:
        return {
            "store": self.activity.status(),
            "scope_locks": self.activity.scope_locks(
                host_epoch=self.mesh.current_epoch
            ),
            "peer_state": self.activity.peer_activity(
                host_epoch=self.mesh.current_epoch
            ),
            "blackboard": self.activity.blackboard(limit=limit),
            "recent": self.activity.recent_activity(
                limit=limit, host_epoch=self.mesh.current_epoch
            ),
        }

    async def ask_peer(
        self, peer_device_id: str, question: str, task_id: str
    ) -> dict[str, Any]:
        self._require_running()
        if peer_device_id == self.local_device_id:
            raise TeamRuntimeError("A peer query must target another device.")
        self._allowed_peer(peer_device_id)
        with self._state_lock:
            link = self._links.get(peer_device_id)
        if link is None or link.closed:
            if self.broker is None:
                raise TeamRuntimeError("No authenticated peer link is available.")
            self.connect(peer_device_id)
            async with asyncio.timeout(self.peer_query_timeout):
                while True:
                    with self._state_lock:
                        link = self._links.get(peer_device_id)
                    if link is not None and not link.closed:
                        break
                    await asyncio.sleep(0.05)
        result = await link.ask_peer(question, task_id)
        local = self._local_device()
        try:
            envelope = make_activity_event(
                signer=self.signer,
                project_session_id=self.project_session_id,
                host_epoch=self.mesh.current_epoch,
                kind="PEER_QUERY_ANSWER",
                summary="Cached peer answer",
                data={
                    "request_id": result["request_id"],
                    "question": question,
                    "answer": result["answer"],
                    "peer_device_record_id": self.local_device_id,
                },
            )
            await asyncio.to_thread(
                self.activity.accept, envelope, local["device_pubkey"]
            )
        except TeamActivityError as exc:
            raise TeamRuntimeError("Peer answer could not be cached.") from exc
        try:
            shared = await self._share_activity(envelope)
        except (TeamRuntimeError, TeamTransportError):
            shared = {"accepted": False, "queued": False}
        self._emit(
            "team/peer_query_answered",
            project_session_id=self.project_session_id,
            host_epoch=self.mesh.current_epoch,
            device_record_id=peer_device_id,
            author_device_record_id=self.local_device_id,
        )
        return {**result, "cached": True, "shared": shared.get("accepted") is True}

    async def refresh(self) -> dict[str, Any]:
        if not self._started:
            raise TeamRuntimeError("Team runtime is not running.")
        async with self._refresh_lock:
            await self._refresh_control_plane()
        return self.status()

    def connect(self, peer_device_id: str, *, transport: str = "auto") -> dict[str, Any]:
        if transport not in {"auto", "webrtc", "relay"}:
            raise ValueError("Transport must be auto, webrtc, or relay.")
        if not self._started or self.broker is None:
            raise TeamRuntimeError("A running team runtime and transport broker are required.")
        record = self._allowed_peer(peer_device_id)
        with self._state_lock:
            existing = self._links.get(peer_device_id)
            if existing is not None and not existing.closed:
                return {"status": "connected", "device_record_id": peer_device_id}
            pending = self._connections.get(peer_device_id)
            if pending is not None and not pending.done():
                return {"status": "connecting", "device_record_id": peer_device_id}
            self._connect_attempts[peer_device_id] = time.monotonic()
            peer = PeerAddress(peer_device_id, record["device_pubkey"])
            future = self.broker.submit(
                lambda: self.broker.run_with_peer_async(
                    peer,
                    lambda session: self._serve_link(session),
                    transport=transport,
                )
            )
            self._connections[peer_device_id] = future

        def complete(done: concurrent.futures.Future[Any]) -> None:
            with self._state_lock:
                if self._connections.get(peer_device_id) is done:
                    self._connections.pop(peer_device_id, None)
            if not done.cancelled():
                done.exception()

        future.add_done_callback(complete)
        return {"status": "connecting", "device_record_id": peer_device_id}

    async def disconnect(self, peer_device_id: str) -> dict[str, Any]:
        with self._state_lock:
            link = self._links.get(peer_device_id)
        if link is not None:
            await link.close()
            with self._state_lock:
                if self._links.get(peer_device_id) is link:
                    self._links.pop(peer_device_id, None)
        return {"disconnected": link is not None, "device_record_id": peer_device_id}

    async def serve_session(self, session: SecurePeerSession, peer: Any) -> dict[str, Any]:
        await self._serve_link(session, peer)
        return {"accepted": True}

    async def _serve_link(self, session: SecurePeerSession, peer: Any = None) -> dict[str, Any]:
        if peer is not None and peer.device_id != session.remote_device_id:
            raise TeamRuntimeError("Peer transport identity does not match the authenticated session.")
        link = await self._attach_link(session)
        try:
            await link.wait_closed()
        finally:
            with self._state_lock:
                if self._links.get(link.remote_device_id) is link:
                    self._links.pop(link.remote_device_id, None)
            await link.close()
        return {"accepted": True, "device_record_id": link.remote_device_id}

    async def _attach_link(self, session: SecurePeerSession) -> TeamPeerLink:
        remote_id = session.remote_device_id
        async with self._link_lock:
            record = self._allowed_peer(remote_id)
            if not _keys_match(record["device_pubkey"], session.remote_signing_public_key):
                raise TeamRuntimeError("Authenticated peer does not match its enrolled device key.")
            with self._state_lock:
                existing = self._links.get(remote_id)
                if existing is not None and not existing.closed:
                    raise TeamRuntimeError("A team peer tunnel is already active.")
            link = TeamPeerLink(
                session,
                self.mesh,
                self.signer,
                self._device_key,
                server_public_key_hex=self._server_keys,
                commit_turn=self._commit_signed,
                history_provider=self.history.history_range,
                ack_sink=self._on_ack,
                scope_allocator=self.allocator,
                device_role_provider=self._device_role,
                submission_validator=self._validate_submission,
                history_sink=self.history.archive,
                submission_publisher=self._fanout_submission,
                activity_sink=self._accept_activity,
                query_provider=self._query_provider,
                query_timeout=self.peer_query_timeout,
            )
            with self._state_lock:
                self._links[remote_id] = link
            try:
                await link.start()
            except BaseException:
                with self._state_lock:
                    if self._links.get(remote_id) is link:
                        self._links.pop(remote_id, None)
                await link.close()
                raise
            self._emit(
                "team/peer_connected",
                device_record_id=remote_id,
                role=record["role"],
                transport=session.status().get("transport", "unknown"),
            )
            return link

    async def _publish(
        self, signed: SignedTurnCommitBundle, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        if self.mesh.mode == "TEAM_REPLICA_PEER":
            link = self._coordinator_link()
            result = await link.submit_turn(signed, payload)
            self._emit(
                "team/turn_applied",
                turn_seq=signed.bundle.turn_seq,
                host_epoch=signed.bundle.host_epoch,
                author_device_record_id=signed.author_device_record_id,
            )
            return result
        if self.mesh.mode != "TEAM_PRIMARY_HOST":
            raise TeamRuntimeError("Local team turns require an active Coordinator lease.")
        await asyncio.to_thread(
            self.replication.validate_turn, signed, payload, self._public_keys()
        )
        receipt = await self._commit_signed(signed)
        self.mesh.accept_turn_commit(
            signed,
            self._public_keys(),
            sender_device_id=self.local_device_id,
            replication_payload=payload,
        )
        self.history.archive(signed, payload, receipt)
        result = await self._fanout(signed, payload, receipt)
        self._emit(
            "team/turn_committed",
            turn_seq=signed.bundle.turn_seq,
            host_epoch=signed.bundle.host_epoch,
            author_device_record_id=signed.author_device_record_id,
        )
        return result

    async def _fanout_submission(
        self,
        signed: SignedTurnCommitBundle,
        payload: Mapping[str, Any],
        receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        return await self._fanout(signed, payload, receipt)

    async def _fanout(
        self,
        signed: SignedTurnCommitBundle,
        payload: Mapping[str, Any],
        receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self._state_lock:
            links = [link for link in self._links.values() if not link.closed]
        if not links:
            return {"accepted": True, "ack": True}
        results = await asyncio.gather(
            *(
                link.send_turn(signed, payload, receipt=receipt, commit=False)
                for link in links
            ),
            return_exceptions=True,
        )
        accepted = all(
            isinstance(result, Mapping)
            and (result.get("accepted") is True or result.get("ack") is True)
            for result in results
        )
        return {"accepted": accepted, "ack": accepted, "peer_count": len(links)}

    async def _publish_activity(
        self,
        kind: str,
        summary: str,
        *,
        paths: list[str] | None = None,
        symbols: list[str] | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._require_running()
        local = self._local_device()
        try:
            envelope = make_activity_event(
                signer=self.signer,
                project_session_id=self.project_session_id,
                host_epoch=self.mesh.current_epoch,
                kind=kind,
                summary=summary,
                paths=paths,
                symbols=symbols,
                data=data,
            )
            accepted = await asyncio.to_thread(
                self.activity.accept, envelope, local["device_pubkey"]
            )
        except TeamActivityError as exc:
            raise TeamRuntimeError(str(exc)) from exc
        result = await self._share_activity(envelope)
        self._emit(
            "team/activity",
            project_session_id=self.project_session_id,
            host_epoch=self.mesh.current_epoch,
            author_device_record_id=self.local_device_id,
            activity_kind=kind,
            event_id=envelope["event"]["event_id"],
            duplicate=accepted.get("duplicate") is True,
        )
        return {**result, "event": envelope["event"]}

    async def _share_activity(self, envelope: Mapping[str, Any]) -> dict[str, Any]:
        if self.mesh.mode == "TEAM_PRIMARY_HOST":
            return await self._fanout_activity(envelope)
        if self.mesh.mode == "TEAM_REPLICA_PEER":
            return await self._coordinator_link().send_activity(envelope, wait=True)
        raise TeamRuntimeError("Team activity requires an active Coordinator lease.")

    async def _fanout_activity(
        self, envelope: Mapping[str, Any]
    ) -> dict[str, Any]:
        with self._state_lock:
            links = [link for link in self._links.values() if not link.closed]
        if not links:
            return {"accepted": True, "queued": True, "peer_count": 0}
        results = await asyncio.gather(
            *(link.send_activity(envelope) for link in links),
            return_exceptions=True,
        )
        accepted = all(
            isinstance(result, Mapping) and result.get("accepted") is True
            for result in results
        )
        if not accepted:
            self._last_error = "TeamActivityFanout"
        return {"accepted": accepted, "queued": accepted, "peer_count": len(links)}

    async def _accept_activity(
        self, envelope: Mapping[str, Any], sender_id: str, relayed: bool
    ) -> dict[str, Any]:
        if not self._started:
            return {"accepted": False}
        event = envelope.get("event") if isinstance(envelope, Mapping) else None
        author = event.get("author_device_record_id") if isinstance(event, Mapping) else None
        with self._state_lock:
            record = self._devices.get(author) if isinstance(author, str) else None
        if (
            record is None
            or record["role"] not in _ELIGIBLE_ROLES
            or not record["host_eligible"]
            or event.get("project_session_id") != self.project_session_id
            or event.get("host_epoch") != self.mesh.current_epoch
        ):
            return {"accepted": False}
        if relayed:
            if sender_id != self.mesh.primary_device_id:
                return {"accepted": False}
        elif self.mesh.mode != "TEAM_PRIMARY_HOST" or sender_id != author:
            return {"accepted": False}
        try:
            verify_activity_event(
                envelope,
                project_session_id=self.project_session_id,
                public_key=record["device_pubkey"],
            )
            accepted = await asyncio.to_thread(
                self.activity.accept, envelope, record["device_pubkey"]
            )
        except TeamActivityError:
            return {"accepted": False}
        if self.mesh.mode == "TEAM_PRIMARY_HOST":
            await self._fanout_activity(envelope)
        self._emit(
            "team/activity",
            project_session_id=self.project_session_id,
            host_epoch=self.mesh.current_epoch,
            author_device_record_id=author,
            activity_kind=event["kind"],
            event_id=event["event_id"],
            relayed=relayed,
            duplicate=accepted.get("duplicate") is True,
        )
        return accepted

    def _query_provider(self, peer_id: str, task_id: str, question: str) -> Any:
        if self.peer_query_provider is not None:
            return self.peer_query_provider(peer_id, task_id, question)
        return self._local_peer_answer(task_id, question)

    def _local_peer_answer(self, task_id: str, question: str) -> str:
        if not self._started:
            return "The local team runtime is not running."
        words = {
            word
            for word in re.findall(r"[a-z0-9_]{3,}", question.lower())
            if word not in {"the", "and", "for", "what", "which", "with", "from"}
        }
        candidates = []
        for note in self.activity.blackboard(limit=100):
            if note["kind"] == "BLACKBOARD_NOTE":
                text = note["data"]["body"]
                matches_task = note["data"].get("task_id") == task_id
            else:
                text = note["data"]["answer"]
                matches_task = False
            searchable = text.lower() + " " + note["summary"].lower()
            if note["kind"] == "PEER_QUERY_ANSWER":
                searchable += " " + note["data"]["question"].lower()
            score = sum(word in searchable for word in words)
            if matches_task or score:
                candidates.append((score, note["created_at"], text))
        if candidates:
            return max(candidates)[2][:4096]
        lowered = question.lower()
        if any(word in lowered for word in {"scope", "working", "file", "claim"}):
            locks = [
                lock
                for lock in self.activity.scope_locks(
                    host_epoch=self.mesh.current_epoch
                )
                if lock["data"]["task_id"] == task_id
            ]
            if not locks:
                return "No active shared scope claim matches this task."
            return "; ".join(
                f"{lock['author_device_record_id']}: {', '.join(lock['paths'])}"
                for lock in locks
            )[:4096]
        states = self.activity.peer_activity(host_epoch=self.mesh.current_epoch)
        local_state = next(
            (
                item["data"]["state"]
                for item in states
                if item["author_device_record_id"] == self.local_device_id
            ),
            "UNKNOWN",
        )
        return f"No shared answer matches this task yet. Local peer state: {local_state}."

    def _require_running(self) -> None:
        if not self._started:
            raise TeamRuntimeError("Team runtime is not running.")
        self._local_device()

    def _local_device(self) -> dict[str, Any]:
        with self._state_lock:
            record = self._devices.get(self.local_device_id)
        if (
            record is None
            or record["role"] not in _ELIGIBLE_ROLES
            or not record["host_eligible"]
        ):
            raise TeamRuntimeError("This device is not authorized for team activity.")
        return record

    def _validate_submission(
        self, signed: SignedTurnCommitBundle, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self.replication.validate_turn(signed, payload, self._public_keys())

    async def _commit_signed(
        self, signed: SignedTurnCommitBundle
    ) -> Mapping[str, Any]:
        signed = SignedTurnCommitBundle.from_dict(signed.to_dict())
        author = self._devices.get(signed.author_device_record_id)
        if (
            not author
            or author["role"] not in _ELIGIBLE_ROLES
            or not author["host_eligible"]
            or not signed.verify(author["device_pubkey"])
        ):
            raise TeamTransportError("Team turn author is not an eligible trusted device.")
        bundle = signed.bundle
        if (
            bundle.project_session_id != self.project_session_id
            or bundle.host_epoch > self.mesh.current_epoch
        ):
            raise TeamTransportError("Team turn does not match the active Coordinator scope.")
        if bundle.host_epoch < self.mesh.current_epoch:
            if bundle.turn_seq > self.mesh.committed_turn_seq:
                raise TeamTransportError("Historical team turn exceeds the trusted lease sequence.")
        elif bundle.turn_seq > self.mesh.committed_turn_seq + 1:
            raise TeamTransportError("Team turn sequence is not contiguous with the Coordinator lease.")
        async with self._commit_lock:
            if self.mesh.mode != "TEAM_PRIMARY_HOST":
                raise TeamTransportError("Only the active Coordinator can commit team turns.")
            receipt = await asyncio.to_thread(
                self.account.commit_session_turn,
                self.project_session_id,
                self.local_device_id,
                signed.to_dict(),
            )
            verify_turn_receipt(signed, receipt, self._server_keys)
            self.mesh.note_committed_turn(
                self.project_session_id, bundle.host_epoch, bundle.turn_seq
            )
            return receipt

    async def _reserve_scope(self, task_id: str) -> Mapping[str, Any]:
        if self.mesh.mode == "TEAM_PRIMARY_HOST":
            return await asyncio.to_thread(self.allocator.reserve, task_id)
        if self.mesh.mode == "TEAM_REPLICA_PEER":
            return await self._coordinator_link().reserve_scope(task_id)
        raise TeamRuntimeError("Turn scope requires an active team lease.")

    def _coordinator_link(self) -> TeamPeerLink:
        coordinator = self.mesh.primary_device_id
        with self._state_lock:
            link = self._links.get(coordinator or "")
        if link is None or link.closed or (link.status().get("remote_role") != "coordinator"):
            raise TeamRuntimeError("No authenticated Coordinator peer link is available.")
        return link

    def _lease_provider(self) -> Mapping[str, Any]:
        with self._state_lock:
            lease = self._signed_lease
            fetched_at = self._lease_fetched_at
        if lease is None:
            raise TeamRuntimeError("No signed team lease is available.")
        if time.monotonic() - fetched_at > self.lease_max_age:
            raise TeamRuntimeError("The cached team lease is stale; refresh before allocating scope.")
        return lease

    async def _refresh_server_keyring(self) -> None:
        try:
            fetch_document = getattr(self.account, "server_public_key_document", None)
            if callable(fetch_document):
                document = await asyncio.to_thread(fetch_document)
            else:
                key_hex = await asyncio.to_thread(self.account.server_public_key)
                fallback = ServerKeyring(key_hex)
                document = {
                    "algorithm": "ed25519",
                    "public_key": fallback.current_key_hex,
                    "key_id": fallback.current_key_id,
                }
            updated = ServerKeyring.from_document(document)
            if updated.to_dict() != self._server_keys.to_dict():
                self._write_server_keyring(updated)
        except Exception as exc:
            await self._fail_closed()
            raise TeamRuntimeError("Team server identity refresh failed.") from exc
        changed = updated.current_key_hex != self._server_keys.current_key_hex
        self._server_keys = updated
        self.server_public_key_hex = updated.current_key_hex
        self.mesh._server_public_key_hex = updated
        self.allocator.server_public_key_hex = updated
        with self._state_lock:
            links = list(self._links.values())
        for link in links:
            link.server_public_key_hex = updated
        if changed:
            await self._reconcile_links(reset=True)
            self._emit("team/server_key_rotated", key_id=updated.current_key_id)

    def _load_server_keyring(self) -> ServerKeyring | None:
        if not self._server_keyring_path.exists():
            return None
        try:
            state = json.loads(self._server_keyring_path.read_text(encoding="utf-8"))
            return ServerKeyring.from_state(state)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise TeamRuntimeError("Stored team server keyring is malformed.") from exc

    def _write_server_keyring(self, keyring: ServerKeyring | None = None) -> None:
        value = keyring or self._server_keys
        encoded = json.dumps(value.to_dict(), sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        descriptor, temporary = tempfile.mkstemp(prefix=".server-keyring-", dir=self.root)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._server_keyring_path)
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)

    async def _refresh_control_plane(self) -> None:
        await self._refresh_server_keyring()
        roster, lease = await asyncio.gather(
            asyncio.to_thread(self.account.list_team_devices, self.team_id),
            asyncio.to_thread(self.account.get_session_lease, self.project_session_id),
        )
        if not isinstance(roster, list) or not isinstance(lease, Mapping):
            await self._fail_closed()
            raise TeamRuntimeError("Team roster or lease is malformed.")
        if not verify_signed_envelope(lease, self._server_keys):
            await self._fail_closed()
            raise TeamRuntimeError("Team lease signature verification failed.")
        payload = lease.get("payload")
        if not isinstance(payload, Mapping) or payload.get("team_id") != self.team_id:
            await self._fail_closed()
            raise TeamRuntimeError("Team lease belongs to a different team.")
        devices: dict[str, dict[str, Any]] = {}
        try:
            for item in roster:
                if not isinstance(item, Mapping):
                    raise TeamRuntimeError("Team roster contains a malformed device.")
                device_id = item.get("device_record_id")
                public_key = item.get("device_pubkey")
                role = item.get("role")
                host_eligible = item.get("host_eligible")
                if (
                    not isinstance(device_id, str)
                    or not DEVICE_ID_PATTERN.fullmatch(device_id)
                    or device_id in devices
                    or not isinstance(public_key, str)
                    or not _DEVICE_KEY_RE.fullmatch(public_key)
                    or role not in _ELIGIBLE_ROLES | {"observer"}
                    or item.get("trust_state") != "trusted"
                    or type(host_eligible) not in {bool, int}
                    or host_eligible not in {0, 1}
                ):
                    raise TeamRuntimeError("Team roster contains an untrusted or malformed device.")
                devices[device_id] = {
                    **dict(item), "host_eligible": bool(host_eligible)
                }
        except Exception:
            await self._fail_closed()
            raise
        with self._state_lock:
            self._devices = devices
        local = devices.get(self.local_device_id)
        if (
            local is None
            or local["role"] not in _ELIGIBLE_ROLES
            or not local["host_eligible"]
        ):
            await self._fail_closed()
            raise TeamRuntimeError("This device is not an eligible member of the team.")
        previous_epoch = self.mesh.current_epoch
        try:
            self.mesh.apply_signed_host_lease(dict(lease))
        except Exception as exc:
            await self._fail_closed()
            raise TeamRuntimeError("Team lease validation failed.") from exc
        with self._state_lock:
            self._devices = devices
            self._signed_lease = dict(lease)
            self._lease_fetched_at = time.monotonic()
        await self._reconcile_links(reset=previous_epoch != self.mesh.current_epoch)
        if previous_epoch != self.mesh.current_epoch:
            self._emit(
                "team/lease_updated",
                host_epoch=self.mesh.current_epoch,
                primary_device_record_id=self.mesh.primary_device_id,
                mode=self.mesh.mode,
            )
        await self._auto_connect()

    async def _fail_closed(self) -> None:
        with self._state_lock:
            self._started = False
            self._devices = {}
            self._signed_lease = None
            self._lease_fetched_at = 0.0
            connections = list(self._connections.values())
            self._connections.clear()
        for future in connections:
            future.cancel()
        await self._reconcile_links()

    async def _reconcile_links(self, *, reset: bool = False) -> None:
        allowed = self._allowed_peer_ids()
        with self._state_lock:
            links = list(self._links.items())
        for device_id, link in links:
            record = self._devices.get(device_id)
            if reset or device_id not in allowed or not _keys_match(
                record["device_pubkey"] if record else "", link.session.remote_signing_public_key
            ):
                await link.close()
                with self._state_lock:
                    if self._links.get(device_id) is link:
                        self._links.pop(device_id, None)

    async def _auto_connect(self) -> None:
        if self.broker is None:
            return
        now = time.monotonic()
        for device_id in sorted(self._allowed_peer_ids()):
            with self._state_lock:
                if device_id in self._links or device_id in self._connections:
                    continue
                if now - self._connect_attempts.get(device_id, 0.0) < 15.0:
                    continue
            info = self.broker.signaling.peer_info(device_id)
            if info is None:
                continue
            try:
                self.connect(device_id)
            except Exception as exc:
                self._last_error = type(exc).__name__

    async def _maintenance_loop(self) -> None:
        while not self._stopped:
            await asyncio.sleep(self.maintenance_interval)
            try:
                await self.refresh()
                await self.flush()
                self._last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = type(exc).__name__
                self._emit("team/runtime_error", error_type=type(exc).__name__)
                if not self._started:
                    break

    def _schedule_flush(self) -> None:
        with self._state_lock:
            if self._flush_scheduled:
                return
            self._flush_scheduled = True

        def complete(_done: Any) -> None:
            with self._state_lock:
                self._flush_scheduled = False

        try:
            if self.broker is not None:
                future = self.broker.submit(self.flush)
            else:
                loop = asyncio.get_running_loop()
                future = loop.create_task(self.flush())
        except RuntimeError:
            with self._state_lock:
                self._flush_scheduled = False
            return
        except Exception:
            with self._state_lock:
                self._flush_scheduled = False
            return

        def consume(done: Any) -> None:
            complete(done)
            if isinstance(done, concurrent.futures.Future) and not done.cancelled():
                done.exception()
            elif isinstance(done, asyncio.Task) and not done.cancelled():
                done.exception()

        future.add_done_callback(consume)

    def _allowed_peer(self, device_id: str) -> dict[str, Any]:
        with self._state_lock:
            record = self._devices.get(device_id)
        if record is None or device_id not in self._allowed_peer_ids():
            raise TeamRuntimeError("Peer is not authorized for this team session.")
        return record

    def _allowed_peer_ids(self) -> set[str]:
        with self._state_lock:
            eligible = {
                device_id for device_id, record in self._devices.items()
                if record["role"] in _ELIGIBLE_ROLES and record["host_eligible"]
            }
        if self.mesh.mode not in {"TEAM_PRIMARY_HOST", "TEAM_REPLICA_PEER"}:
            return set()
        return eligible - {self.local_device_id}

    def _device_key(self, device_id: str) -> str | None:
        with self._state_lock:
            record = self._devices.get(device_id)
            return str(record["device_pubkey"]) if record else None

    def _device_role(self, device_id: str) -> str | None:
        with self._state_lock:
            record = self._devices.get(device_id)
            return str(record["role"]) if record else None

    def _public_keys(self) -> dict[str, str]:
        with self._state_lock:
            return {
                device_id: str(record["device_pubkey"])
                for device_id, record in self._devices.items()
            }

    def _on_ack(self, ack: Mapping[str, Any]) -> None:
        self._emit(
            "team/turn_acknowledged",
            project_session_id=ack.get("project_session_id"),
            host_epoch=ack.get("host_epoch"),
            turn_seq=ack.get("turn_seq"),
            device_record_id=self.local_device_id,
        )

    def _emit(self, kind: str, **fields: Any) -> None:
        if self.on_event is None:
            return
        try:
            result = self.on_event({"kind": kind, **fields})
            if inspect.isawaitable(result):
                task = asyncio.get_running_loop().create_task(result)
                task.add_done_callback(_consume_task)
        except Exception:
            pass

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            links = [
                {
                    "device_record_id": device_id,
                    "role": self._device_role(device_id),
                    **link.status(),
                }
                for device_id, link in self._links.items()
            ]
            connecting = [
                device_id for device_id, future in self._connections.items() if not future.done()
            ]
        return {
            "running": self._started,
            "team_id": self.team_id,
            "project_session_id": self.project_session_id,
            "local_device_id": self.local_device_id,
            "server_key_id": self._server_keys.current_key_id,
            "mode": self.mesh.mode,
            "host_epoch": self.mesh.current_epoch,
            "primary_device_record_id": self.mesh.primary_device_id,
            "committed_turn_seq": self.mesh.committed_turn_seq,
            "applied_turn_seq": self.mesh.last_applied_turn_seq,
            "pending_turns": self.outbox.pending_count,
            "history": self.history.status(),
            "activity": self.activity_status(),
            "roster_devices": len(self._devices),
            "peers": links,
            "connecting": connecting,
            "last_error": self._last_error,
        }


def _keys_match(left: str, right: str) -> bool:
    return (
        isinstance(left, str)
        and isinstance(right, str)
        and left.removeprefix("ed25519:").lower()
        == right.removeprefix("ed25519:").lower()
    )


def _consume_task(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        task.exception()
