"""HEX Harness client integration package for hex-backend."""

from .auth_client import DeviceIdentity, HexAuthClient
from .cognition_sync import CognitionSyncStore
from .p2p_transport import (
    P2P_SUBPROTOCOL,
    InMemoryNetwork,
    P2PTransportError,
    PeerAddress,
    PeerIdentity,
    QuicPeerServer,
    SecurePeerSession,
    WebSocketPeerServer,
    connect_quic_peer,
    connect_websocket_peer,
)
from .remote_config import RemoteConfigClient
from .relay_transport import RelayPeerServer, connect_relay_peer, generate_relay_ticket
from .signaling import P2PSignalingClient
from .sync_contract import (
    SyncEnvelope,
    SyncStream,
    cognition_scope,
    workspace_id_from_cognition_scope,
)
from .sync_engine import SyncEngine, SyncEngineError, SyncResult
from .team_mesh import TeamMeshCoordinator, TurnCommitBundle
from .transport_broker import NegotiationError, PeerTunnelRegistry, TransportBroker
from .updater import ComponentUpdaterClient
from .webrtc_transport import WebRTCPeer, create_webrtc_answer, create_webrtc_offer

__all__ = [
    "ComponentUpdaterClient",
    "CognitionSyncStore",
    "DeviceIdentity",
    "HexAuthClient",
    "InMemoryNetwork",
    "NegotiationError",
    "P2P_SUBPROTOCOL",
    "P2PSignalingClient",
    "P2PTransportError",
    "PeerAddress",
    "PeerIdentity",
    "PeerTunnelRegistry",
    "QuicPeerServer",
    "RemoteConfigClient",
    "RelayPeerServer",
    "SecurePeerSession",
    "SyncEnvelope",
    "SyncEngine",
    "SyncEngineError",
    "SyncResult",
    "SyncStream",
    "TransportBroker",
    "WebRTCPeer",
    "WebSocketPeerServer",
    "connect_quic_peer",
    "connect_relay_peer",
    "connect_websocket_peer",
    "create_webrtc_answer",
    "create_webrtc_offer",
    "cognition_scope",
    "generate_relay_ticket",
    "workspace_id_from_cognition_scope",
    "TeamMeshCoordinator",
    "TurnCommitBundle",
]
