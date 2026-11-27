"""HEX Harness client integration package for hex-backend."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "ComponentUpdaterClient": ".updater",
    "CognitionSyncStore": ".cognition_sync",
    "DeviceIdentity": ".auth_client",
    "HexAuthClient": ".auth_client",
    "InMemoryNetwork": ".p2p_transport",
    "NegotiationError": ".transport_broker",
    "P2P_SUBPROTOCOL": ".p2p_transport",
    "P2PSignalingClient": ".signaling",
    "P2PTransportError": ".p2p_transport",
    "PeerAddress": ".p2p_transport",
    "PeerIdentity": ".p2p_transport",
    "PeerTunnelRegistry": ".transport_broker",
    "QuicPeerServer": ".p2p_transport",
    "RemoteConfigClient": ".remote_config",
    "RelayPeerServer": ".relay_transport",
    "SecurePeerSession": ".p2p_transport",
    "SyncEnvelope": ".sync_contract",
    "SyncEngine": ".sync_engine",
    "SyncEngineError": ".sync_engine",
    "SyncResult": ".sync_engine",
    "SyncStream": ".sync_contract",
    "TeamMeshCoordinator": ".team_mesh",
    "TransportBroker": ".transport_broker",
    "TurnCommitBundle": ".team_mesh",
    "WebRTCPeer": ".webrtc_transport",
    "WebSocketPeerServer": ".p2p_transport",
    "connect_quic_peer": ".p2p_transport",
    "connect_relay_peer": ".relay_transport",
    "connect_websocket_peer": ".p2p_transport",
    "create_webrtc_answer": ".webrtc_transport",
    "create_webrtc_offer": ".webrtc_transport",
    "cognition_scope": ".sync_contract",
    "generate_relay_ticket": ".relay_transport",
    "workspace_id_from_cognition_scope": ".sync_contract",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
