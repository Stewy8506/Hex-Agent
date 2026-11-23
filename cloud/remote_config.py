"""Client-side remote config & feature flag merger (local security denials always win)."""

from __future__ import annotations

from typing import Any

from .updater import verify_signed_envelope


class RemoteConfigClient:
    """Verifies signed remote config bundles and merges safely with local ~/.hex/config.yaml."""

    def __init__(self, server_public_key_hex: str) -> None:
        self._server_public_key_hex = server_public_key_hex
        self._flags: dict[str, bool] = {}
        self._kill_switches: dict[str, dict[str, Any]] = {}
        self._provider_routing: dict[str, Any] = {}

    def apply_signed_config(
        self, signed_config: dict[str, Any], local_denied_features: set[str] | None = None
    ) -> dict[str, bool]:
        if not verify_signed_envelope(signed_config, self._server_public_key_hex):
            raise ValueError("Remote config signature verification failed.")

        payload = signed_config["payload"]
        denied = local_denied_features or set()
        self._kill_switches = dict(payload.get("active_kill_switches", {}))
        self._provider_routing = dict(payload.get("provider_routing", {}))

        merged: dict[str, bool] = {}
        for flag_name, enabled in payload.get("feature_flags", {}).items():
            if flag_name in denied or flag_name in self._kill_switches:
                merged[flag_name] = False
            else:
                merged[flag_name] = bool(enabled)
        self._flags = merged
        return dict(self._flags)

    def is_enabled(self, flag_name: str, default: bool = False) -> bool:
        if flag_name in self._kill_switches:
            return False
        return self._flags.get(flag_name, default)

    @property
    def provider_routing(self) -> dict[str, Any]:
        return dict(self._provider_routing)

    @property
    def kill_switches(self) -> dict[str, dict[str, Any]]:
        return {name: dict(ks) for name, ks in self._kill_switches.items()}
