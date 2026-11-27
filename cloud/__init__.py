"""HEX Harness client integration package for hex-backend."""

from .auth_client import DeviceIdentity, HexAuthClient
from .cognition_sync import CognitionSyncStore
from .p2p_transport import (
    P2P_SUBPROTOCOL,
    InMemoryNetwork,
    P2PTransportError,
    PeerAddress,
    PeerIdentity,
    SecurePeerSession,
    WebSocketPeerServer,
    connect_websocket_peer,
)
from .remote_config import RemoteConfigClient
from .signaling import P2PSignalingClient
from .sync_contract import (
    SyncEnvelope,
    SyncStream,
    cognition_scope,
    workspace_id_from_cognition_scope,
)
from .sync_engine import SyncEngine, SyncEngineError, SyncResult
from .team_mesh import TeamMeshCoordinator, TurnCommitBundle
from .updater import ComponentUpdaterClient

__all__ = [
    "ComponentUpdaterClient",
    "CognitionSyncStore",
    "DeviceIdentity",
    "HexAuthClient",
    "InMemoryNetwork",
    "P2P_SUBPROTOCOL",
    "P2PSignalingClient",
    "P2PTransportError",
    "PeerAddress",
    "PeerIdentity",
    "RemoteConfigClient",
    "SecurePeerSession",
    "SyncEnvelope",
    "SyncEngine",
    "SyncEngineError",
    "SyncResult",
    "SyncStream",
    "WebSocketPeerServer",
    "connect_websocket_peer",
    "cognition_scope",
    "workspace_id_from_cognition_scope",
    "TeamMeshCoordinator",
    "TurnCommitBundle",
]
