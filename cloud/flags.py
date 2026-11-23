"""Process-wide remote feature flags fed by the verified remote-config bundle.

The singleton is intentionally None until a signed bundle is applied: with no
backend reachable, Harness runs fully local and every flag falls back to its
caller-supplied default.
"""

from __future__ import annotations

import threading
from typing import Any

from .remote_config import RemoteConfigClient

_lock = threading.Lock()
_client: RemoteConfigClient | None = None


def set_remote_config(client: RemoteConfigClient | None) -> None:
    global _client
    with _lock:
        _client = client


def remote_config() -> RemoteConfigClient | None:
    with _lock:
        return _client


def flag_enabled(flag_name: str, default: bool = False) -> bool:
    client = remote_config()
    if client is None:
        return default
    return client.is_enabled(flag_name, default)


def active_kill_switches() -> dict[str, dict[str, Any]]:
    client = remote_config()
    if client is None:
        return {}
    return client.kill_switches


def reset_for_tests() -> None:
    set_remote_config(None)
