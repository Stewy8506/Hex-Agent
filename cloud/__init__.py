"""HEX Harness client integration package for hex-backend."""

from .auth_client import DeviceIdentity, HexAuthClient
from .cognition_sync import CognitionSyncStore
from .remote_config import RemoteConfigClient
from .signaling import P2PSignalingClient
from .sync_contract import (
    SyncEnvelope,
    SyncStream,
    cognition_scope,
    workspace_id_from_cognition_scope,
)
from .team_mesh import TeamMeshCoordinator, TurnCommitBundle
from .updater import ComponentUpdaterClient

__all__ = [
    "ComponentUpdaterClient",
    "CognitionSyncStore",
    "DeviceIdentity",
    "HexAuthClient",
    "P2PSignalingClient",
    "RemoteConfigClient",
    "SyncEnvelope",
    "SyncStream",
    "cognition_scope",
    "workspace_id_from_cognition_scope",
    "TeamMeshCoordinator",
    "TurnCommitBundle",
]
