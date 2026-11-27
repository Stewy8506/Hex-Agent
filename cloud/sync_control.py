"""Shared local-data setup and one-shot encrypted sync operations."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

from harness_core.storage.db import LedgerStore

from .cognition_sync import CognitionSyncStore
from .p2p_transport import PeerAddress, SecurePeerSession, connect_websocket_peer
from .session import CloudAccountError, CloudAccountManager
from .sync_engine import SyncEngine
from .transport_broker import TransportBroker


class SyncControl:
    def __init__(
        self,
        workspace_root: Path | str,
        *,
        data_dir: Path | str,
        global_home: Path | str,
        account: CloudAccountManager | None = None,
        ledger: LedgerStore | None = None,
        device_id: str | None = None,
        page_size: int = 500,
        timeout: float = 120.0,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        if not self.workspace_root.is_dir():
            raise ValueError("workspace_root must be an existing directory.")
        self.data_dir = Path(data_dir).resolve()
        self.global_home = Path(global_home).resolve()
        self.account = account or CloudAccountManager()
        self._ledger = ledger
        self._owns_ledger = ledger is None
        self.device_id = device_id or self.account.device_record_id() or (
            "local-" + hashlib.sha256(str(self.workspace_root).encode("utf-8")).hexdigest()[:24]
        )
        self.page_size = page_size
        self.timeout = timeout

    def close(self) -> None:
        if self._owns_ledger and self._ledger is not None:
            self._ledger.close()

    def __enter__(self) -> "SyncControl":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def _engine(self) -> SyncEngine:
        if self._ledger is None:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self._ledger = LedgerStore(self.data_dir / "ledger.db")
        cognition = CognitionSyncStore(
            self.workspace_root,
            global_home=self.global_home,
            ledger=self._ledger,
        )
        return SyncEngine(
            self._ledger,
            cognition,
            self.device_id,
            page_size=self.page_size,
            timeout=self.timeout,
        )

    def status(self) -> dict[str, Any]:
        engine = self._engine()
        state = engine.status()
        return {
            "workspace_id": engine.workspace_id,
            "workspace_root": str(self.workspace_root),
            "device_id": self.device_id,
            "signed_in": bool(self.account.status().get("logged_in")),
            "capabilities": ["ledger", "cognition"],
            "transports": ["websocket", "quic", "signaled-webrtc", "signaled-relay"],
            "peers": state["peers"],
        }

    async def sync_with(self, session: SecurePeerSession) -> dict[str, Any]:
        result = await self._engine().sync_with(session)
        return result.to_dict()

    async def run_websocket(
        self,
        url: str,
        peer_id: str,
        peer_signing_public_key: str,
    ) -> dict[str, Any]:
        if self.account.access_token() is None:
            raise CloudAccountError("Not signed in to HEX cloud.")
        identity = self.account.peer_identity()
        peer = PeerAddress(peer_id, peer_signing_public_key)
        session = await connect_websocket_peer(
            url,
            identity,
            expected_peer=peer,
            timeout=self.timeout,
        )
        try:
            result = await self.sync_with(session)
        finally:
            await session.close()
        return {"transport": "websocket", **result}

    def run_signaled(
        self,
        broker: TransportBroker,
        peer_id: str,
        peer_signing_public_key: str,
        *,
        transport: str = "auto",
    ) -> dict[str, Any]:
        if self.account.access_token() is None:
            raise CloudAccountError("Not signed in to HEX cloud.")
        if broker.device_id != self.device_id:
            raise ValueError("Transport broker identity does not match this sync device.")
        peer = PeerAddress(peer_id, peer_signing_public_key)

        async def operation(session: SecurePeerSession) -> dict[str, Any]:
            return await self.sync_with(session)

        result = broker.run_with_peer(peer, operation, transport=transport, timeout=self.timeout)
        return {"transport": f"signaled-{transport}", **result}


def run_websocket_sync(
    workspace_root: Path | str,
    *,
    data_dir: Path | str,
    global_home: Path | str,
    url: str,
    peer_id: str,
    peer_signing_public_key: str,
    account: CloudAccountManager | None = None,
    ledger: LedgerStore | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    with SyncControl(
        workspace_root,
        data_dir=data_dir,
        global_home=global_home,
        account=account,
        ledger=ledger,
        timeout=timeout,
    ) as control:
        return asyncio.run(control.run_websocket(url, peer_id, peer_signing_public_key))
