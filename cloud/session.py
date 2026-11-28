"""Cloud account session manager: keychain-backed identity, tokens, and devices.

Owns the local half of the hex-backend account lifecycle:

- Device identity (Ed25519 keypair + pseudonymized device ID) persisted in the
  OS keychain, enrolled exactly once per device via challenge-response.
- Session tokens (access + rotating refresh) persisted in the OS keychain,
  auto-refreshed from the JWT ``exp`` claim before authenticated calls.
- Device listing and revocation against hex-backend.

Everything here is offline-safe: callers get :class:`CloudAccountError` with a
human-readable message instead of raw network tracebacks, and Harness keeps
running locally when the backend is unreachable.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import threading
import time
import urllib.error
from typing import Any, Callable, Mapping

from .auth_client import (
    HexAuthClient,
    generate_device_identity,
    identity_from_material,
    identity_material,
)
from .flags import set_remote_config
from .remote_config import RemoteConfigClient
from .updater import ComponentUpdaterClient, verify_signed_envelope

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:8787"
KEYCHAIN_SERVICE = "HEX Autonomous AI Workbench"
IDENTITY_ACCOUNT = "cloud_device_identity"
SESSION_ACCOUNT = "cloud_session"
REFRESH_MARGIN_SECONDS = 60


class CloudAccountError(RuntimeError):
    """Cloud account operation failed with a user-presentable message."""


class MemorySecretStore:
    """Dict-backed stand-in for the OS keychain (tests, non-keychain sandboxes)."""

    def __init__(self) -> None:
        self._secrets: dict[tuple[str, str], str] = {}

    def store(self, service: str, account: str, secret: str) -> None:
        self._secrets[(service, account)] = secret

    def get(self, service: str, account: str) -> str | None:
        return self._secrets.get((service, account))

    def delete(self, service: str, account: str) -> bool:
        return self._secrets.pop((service, account), None) is not None


class KeychainSecretStore:
    """OS keychain-backed secret store (macOS Keychain / Windows DPAPI / Secret Service)."""

    def store(self, service: str, account: str, secret: str) -> None:
        from harness_core.storage.vault_keychain import KeychainUnavailable, store_secret

        try:
            store_secret(service, account, secret)
        except KeychainUnavailable as exc:
            raise CloudAccountError(f"OS keychain unavailable: {exc}") from exc

    def get(self, service: str, account: str) -> str | None:
        from harness_core.storage.vault_keychain import get_secret

        return get_secret(service, account)

    def delete(self, service: str, account: str) -> bool:
        from harness_core.storage.vault_keychain import delete_secret

        return delete_secret(service, account)


def _jwt_exp(token: str) -> int | None:
    """Decode the exp claim without verifying the signature (client-side freshness only)."""
    try:
        segment = token.split(".")[1]
        segment += "=" * (-len(segment) % 4)
        payload = json.loads(base64.urlsafe_b64decode(segment.encode("ascii")))
        exp = payload.get("exp")
        return int(exp) if exp is not None else None
    except Exception:
        return None


def _http_error_message(exc: urllib.error.HTTPError) -> str:
    try:
        detail = json.loads(exc.read().decode("utf-8")).get("detail")
        if detail:
            return str(detail)
    except Exception:
        pass
    return f"HTTP {exc.code}: {exc.reason}"


def _wrap_transport_error(base_url: str, exc: Exception) -> CloudAccountError:
    if isinstance(exc, urllib.error.HTTPError):
        return CloudAccountError(_http_error_message(exc))
    reason = getattr(exc, "reason", None) or exc
    return CloudAccountError(f"Cannot reach HEX cloud at {base_url}: {reason}")


def resolve_base_url(explicit: str | None = None) -> str:
    """Explicit flag > HEX_BACKEND_URL env (config cloud.base_url) > default loopback.

    HEX_CLOUD_BASE_URL is deliberately NOT consulted here: that variable is the
    model-provider endpoint override (agent/router.py), not the control plane.
    """
    if explicit:
        return explicit.rstrip("/")
    env = os.getenv("HEX_BACKEND_URL", "").strip()
    if env:
        return env.rstrip("/")
    return DEFAULT_BASE_URL


def _local_core_version() -> str:
    try:
        from harness_core import __version__

        return str(__version__)
    except Exception:
        return "0.0.0"


class CloudAccountManager:
    """Thread-safe facade over the keychain-persisted cloud account + device."""

    def __init__(
        self,
        base_url: str | None = None,
        store: Any | None = None,
    ) -> None:
        self.base_url = resolve_base_url(base_url)
        self._store = store if store is not None else KeychainSecretStore()
        self._lock = threading.RLock()

    # -- keychain blobs ----------------------------------------------------

    def _read_blob(self, account: str) -> dict[str, Any] | None:
        raw = self._store.get(KEYCHAIN_SERVICE, account)
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
        except ValueError:
            return None

    def _write_blob(self, account: str, blob: dict[str, Any]) -> None:
        self._store.store(KEYCHAIN_SERVICE, account, json.dumps(blob, separators=(",", ":")))

    def _load_identity(self) -> dict[str, Any] | None:
        return self._read_blob(IDENTITY_ACCOUNT)

    def _load_session(self) -> dict[str, Any] | None:
        session = self._read_blob(SESSION_ACCOUNT)
        if session and not session.get("access_token"):
            return None
        return session

    # -- identity ----------------------------------------------------------

    def _load_or_create_identity(self) -> dict[str, Any]:
        identity = self._load_identity()
        if identity:
            return identity
        account_key = secrets.token_hex(32)
        generated = generate_device_identity(account_key)
        identity = identity_material(generated)
        identity["account_key"] = account_key
        self._write_blob(IDENTITY_ACCOUNT, identity)
        return identity

    # -- session -----------------------------------------------------------

    def status(self) -> dict[str, Any]:
        with self._lock:
            session = self._load_session()
            identity = self._load_identity()
            return {
                "base_url": self.base_url,
                "logged_in": bool(session),
                "email": session.get("email") if session else None,
                "user_id": session.get("user_id") if session else None,
                "device_record_id": (
                    (session or {}).get("device_record_id")
                    or (identity or {}).get("device_record_id")
                ),
                "device_enrolled": bool((identity or {}).get("device_record_id")),
                "platform": (identity or {}).get("platform"),
            }

    def register(self, email: str, password: str) -> dict[str, Any]:
        if not email or not password:
            raise CloudAccountError("Email and password are required.")
        client = HexAuthClient(self.base_url)
        try:
            return client.register(email, password)
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            raise _wrap_transport_error(self.base_url, exc) from exc

    def login(self, email: str, password: str) -> dict[str, Any]:
        if not email or not password:
            raise CloudAccountError("Email and password are required.")
        with self._lock:
            client = HexAuthClient(self.base_url)
            identity = self._load_or_create_identity()
            try:
                payload = client.login(
                    email,
                    password,
                    device_record_id=identity.get("device_record_id"),
                )
                user = payload.get("user") or {"email": email}
                tokens = payload
                if not identity.get("device_record_id"):
                    enrollment = client.enroll_device(
                        tokens["access_token"],
                        identity_from_material(identity),
                    )
                    identity["device_record_id"] = enrollment["device_record_id"]
                    self._write_blob(IDENTITY_ACCOUNT, identity)
            except (urllib.error.HTTPError, urllib.error.URLError) as exc:
                raise _wrap_transport_error(self.base_url, exc) from exc

            session = {
                "base_url": self.base_url,
                "email": user.get("email") or email,
                "user_id": user.get("user_id") or tokens.get("user_id"),
                "access_token": tokens["access_token"],
                "refresh_token": tokens["refresh_token"],
                "device_record_id": identity["device_record_id"],
                "saved_at": int(time.time()),
            }
            self._write_blob(SESSION_ACCOUNT, session)
            return self.status()

    def logout(self) -> dict[str, Any]:
        with self._lock:
            self._store.delete(KEYCHAIN_SERVICE, SESSION_ACCOUNT)
            return self.status()

    def access_token(self) -> str | None:
        """Current auto-refreshed access token, or None when not signed in."""
        with self._lock:
            session = self._load_session()
            if not session:
                return None
            try:
                return str(self._refresh_tokens(session)["access_token"])
            except CloudAccountError:
                return None

    def peer_identity(self) -> Any:
        """Return the enrolled Ed25519 identity for authenticated peer transport."""
        from .p2p_transport import PeerIdentity

        with self._lock:
            identity = self._load_or_create_identity()
            device_id = identity.get("device_record_id")
            if not device_id:
                raise CloudAccountError("Device not enrolled; sign in to HEX cloud first.")
            try:
                return PeerIdentity.from_device_identity(
                    identity_from_material(identity), str(device_id)
                )
            except ValueError as exc:
                raise CloudAccountError("Stored device identity is invalid; sign in again.") from exc

    def device_record_id(self) -> str | None:
        with self._lock:
            session = self._load_session()
            identity = self._load_identity()
            return (
                (session or {}).get("device_record_id")
                or (identity or {}).get("device_record_id")
                or None
            )

    # -- authenticated device operations -----------------------------------

    def _refresh_tokens(self, session: dict[str, Any], force: bool = False) -> dict[str, Any]:
        exp = _jwt_exp(session["access_token"])
        if not force and exp is not None and exp - REFRESH_MARGIN_SECONDS > time.time():
            return session
        client = HexAuthClient(self.base_url)
        try:
            tokens = client.refresh(session["refresh_token"])
        except urllib.error.HTTPError as exc:
            # Refresh rejected (revoked family, expired): force re-login.
            self._store.delete(KEYCHAIN_SERVICE, SESSION_ACCOUNT)
            raise CloudAccountError(
                f"{_http_error_message(exc)} — please sign in again."
            ) from exc
        except urllib.error.URLError as exc:
            raise _wrap_transport_error(self.base_url, exc) from exc
        session = {**session, "access_token": tokens["access_token"],
                   "refresh_token": tokens["refresh_token"], "saved_at": int(time.time())}
        self._write_blob(SESSION_ACCOUNT, session)
        return session

    def _authed(
        self, call: Callable[[str], Any]
    ) -> Any:
        with self._lock:
            session = self._load_session()
            if not session:
                raise CloudAccountError("Not signed in to HEX cloud.")
            session = self._refresh_tokens(session)
            try:
                return call(session["access_token"])
            except urllib.error.HTTPError as exc:
                if exc.code != 401:
                    raise _wrap_transport_error(self.base_url, exc) from exc
            except urllib.error.URLError as exc:
                raise _wrap_transport_error(self.base_url, exc) from exc
            # Access token rejected mid-flight: rotate once and retry.
            session = self._refresh_tokens(session, force=True)
            try:
                return call(session["access_token"])
            except (urllib.error.HTTPError, urllib.error.URLError) as exc:
                raise _wrap_transport_error(self.base_url, exc) from exc

    def list_devices(self, include_revoked: bool = False) -> list[dict[str, Any]]:
        client = HexAuthClient(self.base_url)
        return self._authed(lambda token: client.list_devices(token, include_revoked))

    def revoke_device(self, device_record_id: str) -> dict[str, Any]:
        if not device_record_id:
            raise CloudAccountError("device_record_id is required.")
        client = HexAuthClient(self.base_url)

        def _revoke(token: str) -> dict[str, Any]:
            result = client.revoke_device(token, device_record_id)
            identity = self._load_identity()
            if identity and identity.get("device_record_id") == device_record_id:
                identity = {**identity, "device_record_id": None}
                self._write_blob(IDENTITY_ACCOUNT, identity)
                self._store.delete(KEYCHAIN_SERVICE, SESSION_ACCOUNT)
            return result

        return self._authed(_revoke)

    # -- authenticated team operations -------------------------------------

    def create_team(self, name: str) -> dict[str, Any]:
        client = HexAuthClient(self.base_url)
        return self._authed(lambda token: client.create_team(token, name))

    def list_teams(self) -> list[dict[str, Any]]:
        client = HexAuthClient(self.base_url)
        return self._authed(lambda token: client.list_teams(token))

    def create_team_invite(
        self, team_id: str, email: str, role: str = "developer"
    ) -> dict[str, Any]:
        client = HexAuthClient(self.base_url)
        return self._authed(lambda token: client.create_team_invite(token, team_id, email, role))

    def accept_team_invite(self, invite_token: str) -> dict[str, Any]:
        client = HexAuthClient(self.base_url)
        return self._authed(lambda token: client.accept_team_invite(token, invite_token))

    def list_team_members(self, team_id: str) -> list[dict[str, Any]]:
        client = HexAuthClient(self.base_url)
        return self._authed(lambda token: client.list_team_members(token, team_id))

    def list_team_devices(self, team_id: str) -> list[dict[str, Any]]:
        client = HexAuthClient(self.base_url)
        return self._authed(lambda token: client.list_team_devices(token, team_id))

    def update_team_member_role(
        self, team_id: str, target_user_id: str, role: str
    ) -> dict[str, Any]:
        client = HexAuthClient(self.base_url)
        return self._authed(
            lambda token: client.update_team_member_role(token, team_id, target_user_id, role)
        )

    def create_project_session(
        self,
        team_id: str,
        name: str,
        primary_device_record_id: str,
        repo_fingerprint: str = "",
    ) -> dict[str, Any]:
        client = HexAuthClient(self.base_url)
        return self._authed(
            lambda token: client.create_project_session(
                token, team_id, name, primary_device_record_id, repo_fingerprint
            )
        )

    def list_project_sessions(self, team_id: str) -> list[dict[str, Any]]:
        client = HexAuthClient(self.base_url)
        return self._authed(lambda token: client.list_project_sessions(token, team_id))

    def get_session_lease(self, project_session_id: str) -> dict[str, Any]:
        client = HexAuthClient(self.base_url)
        return self._authed(lambda token: client.get_session_lease(token, project_session_id))

    def failover_project_session(
        self,
        project_session_id: str,
        *,
        failed_device_id: str | None = None,
        target_device_id: str | None = None,
    ) -> dict[str, Any]:
        client = HexAuthClient(self.base_url)
        return self._authed(
            lambda token: client.failover_project_session(
                token,
                project_session_id,
                failed_device_id=failed_device_id,
                target_device_id=target_device_id,
            )
        )

    def commit_session_turn(
        self,
        project_session_id: str,
        coordinator_device_record_id: str,
        signed_bundle: Mapping[str, Any],
    ) -> dict[str, Any]:
        client = HexAuthClient(self.base_url)
        return self._authed(
            lambda token: client.commit_session_turn(
                token, project_session_id, coordinator_device_record_id, signed_bundle
            )
        )

    # -- signed control-plane artifacts --------------------------------------

    def server_public_key_document(self) -> dict[str, Any]:
        """Fetch the backend identity document used to build a rollover keyring."""
        client = HexAuthClient(self.base_url)
        try:
            payload = client.fetch_server_pubkey()
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            raise _wrap_transport_error(self.base_url, exc) from exc
        if payload.get("algorithm") != "ed25519" or not payload.get("public_key"):
            raise CloudAccountError("Server published an unsupported identity key type.")
        return dict(payload)

    def server_public_key(self) -> str:
        """Fetch the hex-backend Ed25519 identity key used to verify signed envelopes."""
        payload = self.server_public_key_document()
        return str(payload["public_key"])

    def check_updates(
        self,
        installed_versions: dict[str, str] | None = None,
        channel: str | None = None,
        platform_name: str | None = None,
    ) -> dict[str, Any]:
        """Signed update verdict (compatibility + rollout), locally verified."""
        installed = dict(installed_versions or {"hex-core": _local_core_version()})
        channel = channel or os.getenv("HEX_CLOUD_CHANNEL", "stable")
        client = HexAuthClient(self.base_url)

        def _call(token: str) -> dict[str, Any]:
            updater = ComponentUpdaterClient(self.server_public_key())
            return updater.evaluate_update_response(
                client.check_updates(token, installed, channel=channel, platform_name=platform_name)
            )

        return self._authed(_call)

    def evaluate_remote_config(
        self,
        subscription_tier: str | None = None,
        channel: str | None = None,
        profile: str | None = None,
        beta_opt_in: bool | None = None,
        local_denied_features: set[str] | None = None,
    ) -> dict[str, bool]:
        """Signed feature-flag bundle, locally verified, then published process-wide."""
        tier = subscription_tier or os.getenv("HEX_CLOUD_TIER", "free")
        channel = channel or os.getenv("HEX_CLOUD_CHANNEL", "stable")
        profile = profile or os.getenv("HEX_CLOUD_PROFILE", "default")
        if beta_opt_in is None:
            beta_opt_in = os.getenv("HEX_CLOUD_BETA_OPT_IN", "").strip().lower() in ("1", "true", "yes", "on")
        client = HexAuthClient(self.base_url)
        holder: dict[str, RemoteConfigClient] = {}

        def _call(token: str) -> dict[str, bool]:
            remote_cfg = RemoteConfigClient(self.server_public_key())
            holder["client"] = remote_cfg
            envelope = client.evaluate_config(
                token,
                subscription_tier=tier,
                channel=channel,
                profile=profile,
                beta_opt_in=beta_opt_in,
            )
            return remote_cfg.apply_signed_config(
                envelope, local_denied_features=local_denied_features
            )

        flags = self._authed(_call)
        set_remote_config(holder["client"])
        return flags

    def release_manifest(self, channel: str = "stable") -> dict[str, Any]:
        """Signed release manifest payload for a channel, locally verified."""
        client = HexAuthClient(self.base_url)
        try:
            envelope = client.get_manifest(channel)
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            raise _wrap_transport_error(self.base_url, exc) from exc
        if not verify_signed_envelope(envelope, self.server_public_key()):
            raise CloudAccountError("Release manifest signature verification failed.")
        return envelope["payload"]
