"""Google Antigravity (Cloud Code Assist) Quota-Backed Model Provider & OAuth Manager.

Enables HEX to use Google Antigravity subscription quota (Google AI Pro / Ultra / Individual)
from Day 1 at $0 marginal API billing cost via:
1. Multi-source local credential discovery:
   - Environment variables (`HEX_ANTIGRAVITY_TOKEN`, `ANTIGRAVITY_OAUTH_TOKEN`, `ANTIGRAVITY_ACCESS_TOKEN`)
   - HEX credential store (`~/.hex/antigravity_auth.json`)
   - Local Antigravity IDE SQLite database (`state.vscdb` -> `antigravityUnifiedStateSync.oauthToken`)
   - OpenCode / PicoClaw Antigravity auth stores (`~/.config/opencode/antigravity-accounts.json`, `~/.picoclaw/auth.json`)
   - OS Keychain (`security find-generic-password` on macOS)
   - Application Default Credentials (`~/.config/gcloud/application_default_credentials.json`)
2. OAuth 2.0 PKCE flow (`https://accounts.google.com/o/oauth2/v2/auth`) and automatic
   refresh token exchange (`https://oauth2.googleapis.com/token`) with 5-minute expiry buffer.
3. Cloud Code Assist (`https://cloudcode-pa.googleapis.com`) protocol:
   - `/v1internal:loadCodeAssist` (project ID, tier, and prompt credit discovery)
   - `/v1internal:fetchAvailableModels` (live per-model quota fraction and reset time)
   - `/v1internal:streamGenerateContent?alt=sse` (agent-mode generation envelope with SSE/JSON parsing)
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import re
import secrets
import sqlite3
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from harness_core.gateway import redact_secrets

from .base import (
    BudgetExceededError,
    ModelProvider,
    ModelProviderError,
    ModelResponse,
    ModelUsage,
    PolicyBlockedError,
    ProviderCapabilities,
    ProviderUnavailableError,
)


ANTIGRAVITY_PROD_ENDPOINT = "https://cloudcode-pa.googleapis.com"
ANTIGRAVITY_DAILY_ENDPOINT = "https://daily-cloudcode-pa.googleapis.com"
GOOGLE_OAUTH_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v1/userinfo?alt=json"
DEFAULT_FALLBACK_PROJECT_ID = "aicode-consumers"
DEFAULT_REDIRECT_URI = "http://localhost:51121/oauth-callback"

ANTIGRAVITY_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/cclog",
    "https://www.googleapis.com/auth/experimentsandconfigs",
)

# Standard Google Cloud SDK / Antigravity installed-app OAuth client identifiers
# (Overridable via HEX_ANTIGRAVITY_CLIENT_ID / HEX_ANTIGRAVITY_CLIENT_SECRET)
DEFAULT_OAUTH_CLIENT_ID = os.getenv(
    "HEX_ANTIGRAVITY_CLIENT_ID",
    "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com",
)
DEFAULT_OAUTH_CLIENT_SECRET = os.getenv(
    "HEX_ANTIGRAVITY_CLIENT_SECRET",
    "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",
)

ACCESS_TOKEN_RE = re.compile(r"(ya29\.[A-Za-z0-9_\-.]+)")
REFRESH_TOKEN_RE = re.compile(r"(1//[A-Za-z0-9_\-.]+)")


@dataclass
class AntigravityCredential:
    """Resolved OAuth 2.0 credential for Google Antigravity."""

    access_token: str = ""
    refresh_token: str = ""
    expires_at_ms: int = 0
    email: str | None = None
    project_id: str | None = None
    source: str = "unknown"
    plan_tier: str | None = None

    def is_expired(self, buffer_seconds: int = 300) -> bool:
        """Return True if the access token is missing or within buffer_seconds of expiry."""
        if not self.access_token:
            return True
        if self.expires_at_ms <= 0:
            # Token from env/IDE without explicit expiry timestamp is assumed active until 401
            return False
        now_ms = int(time.time() * 1000)
        return now_ms >= (self.expires_at_ms - buffer_seconds * 1000)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "oauth",
            "provider": "google-antigravity",
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at_ms": self.expires_at_ms,
            "email": self.email,
            "project_id": self.project_id,
            "source": self.source,
            "plan_tier": self.plan_tier,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], source: str = "file") -> AntigravityCredential:
        access = str(data.get("access_token") or data.get("access") or data.get("token") or "").strip()
        refresh = str(data.get("refresh_token") or data.get("refresh") or "").strip()
        expires_raw = data.get("expires_at_ms") or data.get("expires") or 0
        expires_ms = 0
        if isinstance(expires_raw, (int, float)):
            # Handle seconds vs milliseconds
            expires_ms = int(expires_raw * 1000) if expires_raw < 10_000_000_000 else int(expires_raw)
        return cls(
            access_token=access,
            refresh_token=refresh,
            expires_at_ms=expires_ms,
            email=data.get("email"),
            project_id=data.get("project_id") or data.get("projectId"),
            source=data.get("source") or source,
            plan_tier=data.get("plan_tier") or data.get("plan"),
        )


@dataclass
class ModelQuotaWindow:
    """Live quota status for an individual Antigravity model."""

    model_id: str
    display_name: str
    remaining_fraction: float = 1.0
    used_percent: float = 0.0
    reset_time: str | None = None
    is_exhausted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "display_name": self.display_name,
            "remaining_fraction": round(self.remaining_fraction, 4),
            "remaining_percent": round(self.remaining_fraction * 100.0, 1),
            "used_percent": round(self.used_percent, 1),
            "reset_time": self.reset_time,
            "is_exhausted": self.is_exhausted,
        }


@dataclass
class AntigravityQuotaSnapshot:
    """Complete snapshot of a user's Google Antigravity plan, project, and per-model quotas."""

    project_id: str
    plan_name: str = "Google AI Antigravity"
    available_prompt_credits: int | None = None
    email: str | None = None
    credential_source: str = "unknown"
    models: list[ModelQuotaWindow] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": "google-antigravity",
            "project_id": self.project_id,
            "plan_name": self.plan_name,
            "available_prompt_credits": self.available_prompt_credits,
            "email": self.email,
            "credential_source": self.credential_source,
            "models": [m.to_dict() for m in self.models],
        }


def extract_tokens_from_blob(raw_value: str | bytes) -> tuple[str, str]:
    """Extract (`ya29.*` access_token, `1//*` refresh_token) from raw, JSON, or nested-base64 blobs.

    Supports Antigravity IDE's `state.vscdb` (`antigravityUnifiedStateSync.oauthToken`),
    where the SQLite value may be JSON, base64-encoded JSON/protobuf, or double-base64 encoded.
    """
    text = raw_value.decode("utf-8", errors="replace") if isinstance(raw_value, bytes) else str(raw_value)
    candidates: list[str] = [text]

    # Try decoding outer base64 and any long inner base64 segments
    for depth in range(3):
        new_candidates: list[str] = []
        for cand in list(candidates):
            # Look for base64-like tokens of length >= 32
            for b64_match in re.findall(r"[A-Za-z0-9+/=_=-]{32,}", cand):
                padded = b64_match + ("=" * ((4 - len(b64_match) % 4) % 4))
                for decoder in (base64.b64decode, base64.urlsafe_b64decode):
                    try:
                        decoded = decoder(padded).decode("utf-8", errors="ignore")
                        if decoded and decoded not in candidates and decoded not in new_candidates:
                            if "ya29." in decoded or "1//" in decoded or "access_token" in decoded or len(decoded) > 24:
                                new_candidates.append(decoded)
                    except Exception:
                        pass
        candidates.extend(new_candidates)

    access_token = ""
    refresh_token = ""
    for blob in candidates:
        if not access_token:
            m_acc = ACCESS_TOKEN_RE.search(blob)
            if m_acc:
                access_token = m_acc.group(1)
        if not refresh_token:
            m_ref = REFRESH_TOKEN_RE.search(blob)
            if m_ref:
                refresh_token = m_ref.group(1)
        if access_token and refresh_token:
            break

    # Also check structured JSON keys if tokens didn't use standard prefixes
    if not (access_token and refresh_token):
        for blob in candidates:
            try:
                parsed = json.loads(blob)
                if isinstance(parsed, dict):
                    access_token = access_token or str(
                        parsed.get("access_token") or parsed.get("access") or parsed.get("oauthToken") or ""
                    )
                    refresh_token = refresh_token or str(
                        parsed.get("refresh_token") or parsed.get("refresh") or ""
                    )
            except Exception:
                pass

    return access_token, refresh_token


ANTIGRAVITY_FALLBACK_MODELS: list[tuple[str, str]] = [
    ("claude-opus-4-6-thinking", "claude-opus-4-6-thinking [offline fallback]"),
    ("claude-sonnet-4-6", "claude-sonnet-4-6 [offline fallback]"),
    ("gemini-2.5-pro", "gemini-2.5-pro [offline fallback]"),
    ("gemini-2.5-flash", "gemini-2.5-flash [offline fallback]"),
    ("gemini-3.1-flash-lite", "gemini-3.1-flash-lite [offline fallback]"),
]


def fetch_available_models(
    access_token: str | None = None,
    project_id: str | None = None,
    base_url: str | None = None,
    timeout: float = 3.0,
) -> list[tuple[str, str]]:
    """Query Antigravity endpoints for available models, with graceful offline fallback."""
    tok = (access_token or "").strip()
    proj = project_id or DEFAULT_FALLBACK_PROJECT_ID

    if not tok:
        try:
            tok = (
                os.getenv("HEX_ANTIGRAVITY_TOKEN")
                or os.getenv("ANTIGRAVITY_OAUTH_TOKEN")
                or os.getenv("ANTIGRAVITY_ACCESS_TOKEN")
                or ""
            ).strip()
            if not tok:
                cred = AntigravityOAuthManager().resolve_credential(auto_refresh=False)
                if cred and cred.access_token:
                    tok = cred.access_token.strip()
                    proj = cred.project_id or proj
        except Exception:
            pass

    if not tok:
        return list(ANTIGRAVITY_FALLBACK_MODELS)

    url_base = base_url or ANTIGRAVITY_PROD_ENDPOINT
    models_url = f"{url_base}/v1internal:fetchAvailableModels"
    headers = {
        "Authorization": f"Bearer {tok}",
        "Content-Type": "application/json",
        "User-Agent": "antigravity",
        "X-Goog-Api-Client": "google-cloud-sdk vscode_cloudshelleditor/0.1",
        "Client-Metadata": json.dumps(
            {
                "ideType": "ANTIGRAVITY",
                "platform": "PLATFORM_UNSPECIFIED",
                "pluginType": "GEMINI",
            }
        ),
    }

    # If project_id not explicitly supplied, check if loadCodeAssist provides a specific project
    if not project_id and proj == DEFAULT_FALLBACK_PROJECT_ID:
        try:
            load_url = f"{url_base}/v1internal:loadCodeAssist"
            load_body = json.dumps(
                {
                    "metadata": {
                        "ideType": "ANTIGRAVITY",
                        "platform": "PLATFORM_UNSPECIFIED",
                        "pluginType": "GEMINI",
                    }
                }
            ).encode("utf-8")
            load_req = urllib.request.Request(load_url, data=load_body, headers=headers, method="POST")
            with urllib.request.urlopen(load_req, timeout=min(timeout, 2.0)) as l_resp:
                if l_resp.status in (200, 201):
                    l_data = json.loads(l_resp.read().decode("utf-8"))
                    l_proj = l_data.get("cloudaicompanionProject")
                    if isinstance(l_proj, str) and l_proj.strip():
                        proj = l_proj.strip()
                    elif isinstance(l_proj, dict) and l_proj.get("id"):
                        proj = str(l_proj["id"]).strip()
        except Exception:
            pass

    body = json.dumps({"project": proj}).encode("utf-8")
    req = urllib.request.Request(models_url, data=body, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status in (200, 201):
                raw = resp.read().decode("utf-8")
                data = json.loads(raw)
                models_dict = data.get("models")
                if isinstance(models_dict, dict) and models_dict:
                    parsed: list[tuple[str, str]] = []
                    for mid, minfo in models_dict.items():
                        if not isinstance(minfo, dict):
                            continue
                        # Filter out internal IDE tab completion and raw preview models
                        if mid.startswith("tab_") or mid.startswith("chat_"):
                            continue
                        if mid in (
                            "gemini-2.5-flash-lite",
                            "gemini-2.5-flash-thinking",
                            "gemini-3.5-flash-lite",
                            "gemini-3.8-flash-tiered",
                            "gemini-3.1-flash-image",
                        ):
                            continue
                        disp = str(minfo.get("displayName") or mid)
                        qinfo = minfo.get("quotaInfo")
                        if isinstance(qinfo, dict) and "remainingFraction" in qinfo:
                            try:
                                rem = max(0.0, min(1.0, float(qinfo["remainingFraction"])))
                                rem_pct = int(rem * 100)
                                label = f"{disp} ({rem_pct}% quota remaining)"
                            except (TypeError, ValueError):
                                label = disp
                        else:
                            label = disp
                        parsed.append((str(mid), label))
                    if parsed:
                        priority_order = [
                            "gemini-2.5-pro",
                            "gemini-2.5-flash",
                            "claude-sonnet-4-6",
                            "claude-opus-4-6-thinking",
                            "gpt-oss-120b-medium",
                            "gemini-3.1-flash-lite",
                        ]
                        def _sort_key(item: tuple[str, str]) -> int:
                            return priority_order.index(item[0]) if item[0] in priority_order else 100
                        parsed.sort(key=_sort_key)
                        return parsed
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, Exception):
        pass

    return list(ANTIGRAVITY_FALLBACK_MODELS)


class AntigravityOAuthManager:
    """Discovers, refreshes, and persists Google Antigravity OAuth credentials."""

    def __init__(
        self,
        *,
        auth_file: Path | str | None = None,
        ide_db_paths: list[Path] | None = None,
        external_auth_paths: list[Path] | None = None,
        http_transport: Callable[[str, dict[str, str], bytes | None, str], tuple[int, bytes]] | None = None,
        allow_keychain: bool = True,
    ) -> None:
        default_hex_dir = Path(os.getenv("HEX_CONFIG_DIR") or (Path.home() / ".hex"))
        self._explicit_auth_file: bool = auth_file is not None
        self.auth_file = Path(auth_file) if auth_file else (default_hex_dir / "antigravity_auth.json")
        self.ide_db_paths = ide_db_paths if ide_db_paths is not None else self._default_ide_db_paths()
        self.external_auth_paths = (
            external_auth_paths if external_auth_paths is not None else self._default_external_auth_paths()
        )
        self.http_transport = http_transport
        self.allow_keychain = allow_keychain

    @staticmethod
    def _default_ide_db_paths() -> list[Path]:
        home = Path.home()
        paths: list[Path] = [
            # macOS paths
            home / "Library" / "Application Support" / "Antigravity" / "User" / "globalStorage" / "state.vscdb",
            home / "Library" / "Application Support" / "Antigravity IDE" / "User" / "globalStorage" / "state.vscdb",
            # Linux / XDG paths
            home / ".config" / "Antigravity" / "User" / "globalStorage" / "state.vscdb",
            home / ".config" / "Antigravity IDE" / "User" / "globalStorage" / "state.vscdb",
        ]
        appdata = os.getenv("APPDATA")
        if appdata:
            paths.append(Path(appdata) / "Antigravity" / "User" / "globalStorage" / "state.vscdb")
            paths.append(Path(appdata) / "Antigravity IDE" / "User" / "globalStorage" / "state.vscdb")
        return paths

    @staticmethod
    def _default_external_auth_paths() -> list[Path]:
        home = Path.home()
        return [
            home / ".config" / "opencode" / "antigravity-accounts.json",
            home / ".picoclaw" / "auth.json",
            home / ".gemini" / "oauth_creds.json",
            home / ".config" / "gcloud" / "application_default_credentials.json",
        ]

    # ------------------------------------------------------------------
    # PKCE Generation & OAuth Login Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def generate_pkce_pair() -> dict[str, str]:
        """Generate OAuth 2.0 PKCE verifier and S256 code_challenge."""
        verifier = secrets.token_hex(32)
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        return {"verifier": verifier, "challenge": challenge}

    @classmethod
    def build_authorization_url(
        cls,
        challenge: str,
        state: str | None = None,
        redirect_uri: str = DEFAULT_REDIRECT_URI,
        client_id: str = DEFAULT_OAUTH_CLIENT_ID,
    ) -> str:
        """Build the Google OAuth 2.0 PKCE authorization URL for Antigravity scopes."""
        st = state or secrets.token_hex(16)
        params = {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": " ".join(ANTIGRAVITY_SCOPES),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": st,
            "access_type": "offline",
            "prompt": "consent",
        }
        return f"{GOOGLE_OAUTH_AUTH_URL}?{urllib.parse.urlencode(params)}"

    @staticmethod
    def parse_authorization_code(redirect_url_or_code: str) -> str:
        """Extract authorization `code` from either a full redirect URL or raw code string."""
        cleaned = redirect_url_or_code.strip()
        if cleaned.startswith("http://") or cleaned.startswith("https://") or "code=" in cleaned:
            parsed = urllib.parse.urlparse(cleaned)
            qs = urllib.parse.parse_qs(parsed.query)
            if "code" in qs and qs["code"]:
                return qs["code"][0].strip()
        return cleaned

    def exchange_code_for_tokens(
        self,
        code: str,
        verifier: str,
        redirect_uri: str = DEFAULT_REDIRECT_URI,
        client_id: str = DEFAULT_OAUTH_CLIENT_ID,
        client_secret: str = DEFAULT_OAUTH_CLIENT_SECRET,
    ) -> AntigravityCredential:
        """Exchange an OAuth 2.0 authorization code + PKCE verifier for access and refresh tokens."""
        clean_code = self.parse_authorization_code(code)
        form = urllib.parse.urlencode(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "code": clean_code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            }
        ).encode("utf-8")
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        status, body = self._http_request(GOOGLE_OAUTH_TOKEN_URL, headers, form, "POST")
        if status not in (200, 201):
            err = body.decode("utf-8", errors="replace")[:400]
            raise ProviderUnavailableError(f"Antigravity OAuth code exchange failed (HTTP {status}): {err}")

        data = json.loads(body.decode("utf-8"))
        expires_in = int(data.get("expires_in", 3600))
        expires_at_ms = int((time.time() + expires_in - 300) * 1000)
        access_tok = str(data.get("access_token", ""))
        refresh_tok = str(data.get("refresh_token", ""))

        project_id = DEFAULT_FALLBACK_PROJECT_ID
        plan_tier = "Antigravity ($0 Free Tier)"
        try:
            load_url = f"{ANTIGRAVITY_PROD_ENDPOINT}/v1internal:loadCodeAssist"
            headers_load = {
                "Authorization": f"Bearer {access_tok}",
                "Content-Type": "application/json",
                "User-Agent": "antigravity",
                "X-Goog-Api-Client": "google-cloud-sdk vscode_cloudshelleditor/0.1",
                "Client-Metadata": json.dumps({"ideType": "ANTIGRAVITY", "platform": "PLATFORM_UNSPECIFIED", "pluginType": "GEMINI"}),
            }
            body_load = json.dumps({"metadata": {"ideType": "ANTIGRAVITY", "platform": "PLATFORM_UNSPECIFIED", "pluginType": "GEMINI"}}).encode("utf-8")
            status_l, body_l = self._http_request(load_url, headers_load, body_load, "POST")
            if status_l in (200, 201):
                l_data = json.loads(body_l.decode("utf-8"))
                p = l_data.get("cloudaicompanionProject")
                if isinstance(p, str) and p.strip():
                    project_id = p.strip()
                tier = l_data.get("currentTier")
                if isinstance(tier, dict) and tier.get("name"):
                    plan_tier = str(tier["name"])
        except Exception:
            pass

        cred = AntigravityCredential(
            access_token=access_tok,
            refresh_token=refresh_tok,
            expires_at_ms=expires_at_ms,
            project_id=project_id,
            plan_tier=plan_tier,
            source="pkce_oauth",
        )
        self.save_credential(cred)
        return cred

    def refresh_access_token(
        self,
        cred: AntigravityCredential,
        client_id: str = DEFAULT_OAUTH_CLIENT_ID,
        client_secret: str = DEFAULT_OAUTH_CLIENT_SECRET,
    ) -> AntigravityCredential:
        """Refresh an expired access token using its `refresh_token`."""
        if not cred.refresh_token:
            raise ProviderUnavailableError(
                "Antigravity access token is expired and no refresh_token is available. "
                "Run `hex auth antigravity --login` or sign in to Antigravity IDE."
            )
        form = urllib.parse.urlencode(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": cred.refresh_token,
                "grant_type": "refresh_token",
            }
        ).encode("utf-8")
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        status, body = self._http_request(GOOGLE_OAUTH_TOKEN_URL, headers, form, "POST")
        if status not in (200, 201):
            err = body.decode("utf-8", errors="replace")[:400]
            raise ProviderUnavailableError(f"Antigravity OAuth token refresh failed (HTTP {status}): {err}")

        data = json.loads(body.decode("utf-8"))
        expires_in = int(data.get("expires_in", 3600))
        cred.access_token = str(data.get("access_token", ""))
        if data.get("refresh_token"):
            cred.refresh_token = str(data["refresh_token"])
        cred.expires_at_ms = int((time.time() + expires_in - 300) * 1000)
        self.save_credential(cred)
        return cred

    # ------------------------------------------------------------------
    # Multi-Source Credential Discovery
    # ------------------------------------------------------------------

    def resolve_credential(self, auto_refresh: bool = True) -> AntigravityCredential | None:
        """Discover local Antigravity OAuth credentials across env, HEX store, IDE SQLite, and Keychain."""
        if self._explicit_auth_file:
            cred = (
                self._from_hex_auth_file()
                or self._from_env()
                or self._from_external_auth_files()
                or self._from_ide_state_vscdb()
                or (self._from_os_keychain() if self.allow_keychain else None)
            )
        else:
            cred = (
                self._from_env()
                or self._from_hex_auth_file()
                or self._from_external_auth_files()
                or self._from_ide_state_vscdb()
                or (self._from_os_keychain() if self.allow_keychain else None)
            )
        if cred is None:
            return None

        if auto_refresh and cred.is_expired() and cred.refresh_token:
            try:
                cred = self.refresh_access_token(cred)
            except Exception:
                # If offline or mock environment without refresh server, retain if access_token exists
                if not cred.access_token:
                    raise
        return cred

    def _from_env(self) -> AntigravityCredential | None:
        access = (
            os.getenv("HEX_ANTIGRAVITY_TOKEN")
            or os.getenv("ANTIGRAVITY_OAUTH_TOKEN")
            or os.getenv("ANTIGRAVITY_ACCESS_TOKEN")
            or ""
        ).strip()
        refresh = (
            os.getenv("HEX_ANTIGRAVITY_REFRESH_TOKEN")
            or os.getenv("ANTIGRAVITY_REFRESH_TOKEN")
            or ""
        ).strip()
        project_id = (
            os.getenv("HEX_ANTIGRAVITY_PROJECT_ID")
            or os.getenv("ANTIGRAVITY_PROJECT_ID")
            or None
        )
        if not access and not refresh:
            return None
        return AntigravityCredential(
            access_token=access,
            refresh_token=refresh,
            project_id=project_id,
            source="env",
        )

    def _from_hex_auth_file(self) -> AntigravityCredential | None:
        if not self.auth_file.is_file():
            return None
        try:
            data = json.loads(self.auth_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cred = AntigravityCredential.from_dict(data, source=f"hex_auth:{self.auth_file}")
                if cred.access_token or cred.refresh_token:
                    return cred
        except Exception:
            pass
        return None

    def _from_external_auth_files(self) -> AntigravityCredential | None:
        for path in self.external_auth_paths:
            if not path.is_file():
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue

            if not isinstance(raw, dict):
                continue

            # 1. PicoClaw format: {"credentials": {"google-antigravity": {...}}}
            creds_map = raw.get("credentials")
            if isinstance(creds_map, dict) and isinstance(creds_map.get("google-antigravity"), dict):
                cred = AntigravityCredential.from_dict(
                    creds_map["google-antigravity"], source=f"picoclaw:{path}"
                )
                if cred.access_token or cred.refresh_token:
                    return cred

            # 2. OpenCode antigravity-accounts.json format: {"accounts": [{"access": ..., "refresh": ...}]}
            accounts = raw.get("accounts")
            if isinstance(accounts, list):
                for acc in accounts:
                    if isinstance(acc, dict) and not acc.get("disabled", False):
                        cred = AntigravityCredential.from_dict(acc, source=f"opencode:{path}")
                        if cred.access_token or cred.refresh_token:
                            return cred

            # 3. Direct oauth_creds.json or ADC format
            cred = AntigravityCredential.from_dict(raw, source=f"external:{path}")
            if cred.access_token or cred.refresh_token:
                return cred
        return None

    def _from_ide_state_vscdb(self) -> AntigravityCredential | None:
        """Extract OAuth tokens from Antigravity IDE's local `state.vscdb` SQLite database."""
        for db_path in self.ide_db_paths:
            if not db_path.is_file():
                continue
            try:
                uri = f"file:{db_path}?mode=ro"
                conn = sqlite3.connect(uri, uri=True, timeout=2.0)
                try:
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT value FROM ItemTable WHERE key = 'antigravityUnifiedStateSync.oauthToken' "
                        "OR key LIKE '%antigravity%oauth%'"
                    )
                    rows = cur.fetchall()
                finally:
                    conn.close()

                for (val,) in rows:
                    if not val:
                        continue
                    access, refresh = extract_tokens_from_blob(val)
                    if access or refresh:
                        return AntigravityCredential(
                            access_token=access,
                            refresh_token=refresh,
                            source=f"antigravity_ide_vscdb:{db_path}",
                        )
            except Exception:
                continue
        return None

    def _from_os_keychain(self) -> AntigravityCredential | None:
        if platform.system() != "Darwin":
            return None
        for service_name in ("antigravity", "Antigravity", "antigravity-oauth"):
            try:
                res = subprocess.run(
                    ["security", "find-generic-password", "-s", service_name, "-w"],
                    capture_output=True,
                    text=True,
                    timeout=1.5,
                    check=False,
                )
                if res.returncode == 0 and res.stdout.strip():
                    access, refresh = extract_tokens_from_blob(res.stdout.strip())
                    if access or refresh:
                        return AntigravityCredential(
                            access_token=access,
                            refresh_token=refresh,
                            source=f"macos_keychain:{service_name}",
                        )
            except Exception:
                continue
        return None

    def save_credential(self, cred: AntigravityCredential) -> Path:
        """Persist Antigravity OAuth credential to `~/.hex/antigravity_auth.json` with 0600 permissions."""
        self.auth_file.parent.mkdir(parents=True, exist_ok=True)
        self.auth_file.write_text(json.dumps(cred.to_dict(), indent=2) + "\n", encoding="utf-8")
        try:
            os.chmod(self.auth_file, 0o600)
        except Exception:
            pass
        return self.auth_file

    def _http_request(
        self,
        url: str,
        headers: dict[str, str],
        body: bytes | None = None,
        method: str = "POST",
    ) -> tuple[int, bytes]:
        if self.http_transport is not None:
            return self.http_transport(url, headers, body, method)
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20.0) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except Exception as e:
            raise ProviderUnavailableError(f"Antigravity HTTP request to {url} failed: {e}") from e

    def fetch_available_models(
        self,
        access_token: str | None = None,
        timeout: float = 3.0,
    ) -> list[tuple[str, str]]:
        """Discover live models for Google Antigravity or return fallback."""
        tok = access_token
        proj_id = None
        if not tok:
            try:
                cred = self.resolve_credential(auto_refresh=False)
                if cred and cred.access_token:
                    tok = cred.access_token
                    proj_id = cred.project_id
            except Exception:
                pass
        return fetch_available_models(
            access_token=tok,
            project_id=proj_id,
            timeout=timeout,
        )


class AntigravityModelProvider(ModelProvider):
    """Google Antigravity (Cloud Code Assist) Model Provider.

    Dispatches requests through `https://cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse`
    using the user's local Antigravity OAuth session so every call uses included Antigravity plan quota
    ($0 marginal API cost) while enforcing HEX's privacy gate, secret redaction, and prefix caching.
    """

    provider_id: str = "google_antigravity"

    def __init__(
        self,
        *,
        model_id: str = "gemini-2.5-pro",
        oauth_manager: AntigravityOAuthManager | None = None,
        access_token: str | None = None,
        refresh_token: str | None = None,
        project_id: str | None = None,
        base_url: str = ANTIGRAVITY_PROD_ENDPOINT,
        budget_tracker: Any = None,
        http_transport: Callable[[str, dict[str, str], bytes], tuple[int, bytes]] | None = None,
        enable_prompt_caching: bool = True,
    ) -> None:
        self.model_id = self._normalize_model_id(model_id)
        self.base_url = base_url.rstrip("/")
        self.http_transport = http_transport
        self.enable_prompt_caching = enable_prompt_caching

        from .cloud_provider import TaskBudgetTracker

        self.budget_tracker = budget_tracker or TaskBudgetTracker()
        self.oauth_manager = oauth_manager or AntigravityOAuthManager(
            http_transport=(
                (lambda u, h, b, m: http_transport(u, h, b or b""))
                if http_transport is not None
                else None
            ),
            allow_keychain=(http_transport is None),
        )
        self._explicit_cred: AntigravityCredential | None = None
        if access_token or refresh_token:
            self._explicit_cred = AntigravityCredential(
                access_token=access_token or "",
                refresh_token=refresh_token or "",
                project_id=project_id,
                source="explicit_init",
            )
        self._cached_project_id: str | None = project_id or os.getenv("HEX_ANTIGRAVITY_PROJECT_ID")

    @staticmethod
    def _normalize_model_id(model_id: str) -> str:
        norm = model_id.strip()
        for prefix in ("google/antigravity-", "antigravity/", "antigravity-"):
            if norm.lower().startswith(prefix):
                norm = norm[len(prefix) :]
                break
        return norm or "gemini-2.5-pro"

    def _get_active_credential(self) -> AntigravityCredential | None:
        if self._explicit_cred is not None:
            if self._explicit_cred.is_expired() and self._explicit_cred.refresh_token:
                self._explicit_cred = self.oauth_manager.refresh_access_token(self._explicit_cred)
            return self._explicit_cred
        return self.oauth_manager.resolve_credential(auto_refresh=True)

    def is_available(self) -> bool:
        if self.http_transport is not None and self._explicit_cred is not None:
            return True
        try:
            cred = self._get_active_credential()
            return bool(cred and (cred.access_token or cred.refresh_token))
        except Exception:
            return False

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supports_system_prompt=True,
            supports_streaming=True,
            max_context_window=1_000_000,
            quantization=None,
            local_unified_memory=False,
        )

    @staticmethod
    def _build_antigravity_headers(access_token: str, include_client_meta: bool = False) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "User-Agent": "antigravity",
            "X-Goog-Api-Client": "google-cloud-sdk vscode_cloudshelleditor/0.1",
        }
        if include_client_meta:
            headers["Client-Metadata"] = json.dumps(
                {
                    "ideType": "ANTIGRAVITY",
                    "platform": "PLATFORM_UNSPECIFIED",
                    "pluginType": "GEMINI",
                }
            )
        return headers

    def ensure_project_id(self, access_token: str | None = None) -> str:
        """Resolve `cloudaicompanionProject` via `/v1internal:loadCodeAssist` or cached value."""
        if self._cached_project_id:
            return self._cached_project_id

        cred = self._get_active_credential()
        if cred and cred.project_id:
            self._cached_project_id = cred.project_id
            return self._cached_project_id

        tok = access_token or (cred.access_token if cred else "")
        if not tok:
            return DEFAULT_FALLBACK_PROJECT_ID

        url = f"{self.base_url}/v1internal:loadCodeAssist"
        headers = self._build_antigravity_headers(tok, include_client_meta=True)
        payload = {
            "metadata": {
                "ideType": "ANTIGRAVITY",
                "platform": "PLATFORM_UNSPECIFIED",
                "pluginType": "GEMINI",
            }
        }
        try:
            status, resp_bytes = self._execute_http(url, headers, json.dumps(payload).encode("utf-8"))
            if status in (200, 201):
                data = json.loads(resp_bytes.decode("utf-8"))
                proj = data.get("cloudaicompanionProject")
                if isinstance(proj, str) and proj.strip():
                    self._cached_project_id = proj.strip()
                    return self._cached_project_id
                if isinstance(proj, dict) and proj.get("id"):
                    self._cached_project_id = str(proj["id"]).strip()
                    return self._cached_project_id
        except Exception:
            pass

        self._cached_project_id = DEFAULT_FALLBACK_PROJECT_ID
        return self._cached_project_id

    def fetch_quota_status(self) -> AntigravityQuotaSnapshot:
        """Query `/v1internal:loadCodeAssist` and `/v1internal:fetchAvailableModels` for live quota telemetry."""
        cred = self._get_active_credential()
        if not cred or not cred.access_token:
            raise ProviderUnavailableError(
                "No Google Antigravity OAuth session found. Run `hex auth antigravity --login` "
                "or sign in to the Google Antigravity IDE."
            )

        headers = self._build_antigravity_headers(cred.access_token, include_client_meta=True)

        # 1. Load Code Assist tier & project info
        load_url = f"{self.base_url}/v1internal:loadCodeAssist"
        load_body = json.dumps(
            {
                "metadata": {
                    "ideType": "ANTIGRAVITY",
                    "platform": "PLATFORM_UNSPECIFIED",
                    "pluginType": "GEMINI",
                }
            }
        ).encode("utf-8")
        status_load, bytes_load = self._execute_http(load_url, headers, load_body)
        plan_name = cred.plan_tier or "Google AI Pro (Antigravity)"
        prompt_credits: int | None = None
        project_id = self._cached_project_id or cred.project_id or DEFAULT_FALLBACK_PROJECT_ID

        if status_load in (200, 201):
            try:
                load_data = json.loads(bytes_load.decode("utf-8"))
                proj = load_data.get("cloudaicompanionProject")
                if isinstance(proj, str) and proj.strip():
                    project_id = proj.strip()
                    self._cached_project_id = project_id
                tier = load_data.get("currentTier")
                if isinstance(tier, dict) and tier.get("name"):
                    plan_name = str(tier["name"])
                elif isinstance(load_data.get("planInfo"), dict):
                    plan_name = str(load_data["planInfo"].get("planName") or plan_name)
                if "availablePromptCredits" in load_data and load_data["availablePromptCredits"] is not None:
                    prompt_credits = int(load_data["availablePromptCredits"])
            except Exception:
                pass

        # 2. Fetch per-model quota windows
        models_url = f"{self.base_url}/v1internal:fetchAvailableModels"
        models_body = json.dumps({"project": project_id}).encode("utf-8")
        status_models, bytes_models = self._execute_http(models_url, headers, models_body)
        windows: list[ModelQuotaWindow] = []

        if status_models in (200, 201):
            try:
                m_data = json.loads(bytes_models.decode("utf-8"))
                models_dict = m_data.get("models") or {}
                if isinstance(models_dict, dict):
                    for mid, minfo in models_dict.items():
                        if not isinstance(minfo, dict):
                            continue
                        qinfo = minfo.get("quotaInfo") or {}
                        rem_raw = qinfo.get("remainingFraction", 1.0)
                        try:
                            rem = max(0.0, min(1.0, float(rem_raw)))
                        except (TypeError, ValueError):
                            rem = 1.0
                        exhausted = bool(qinfo.get("isExhausted", rem <= 0.0))
                        windows.append(
                            ModelQuotaWindow(
                                model_id=str(mid),
                                display_name=str(minfo.get("displayName") or mid),
                                remaining_fraction=rem,
                                used_percent=round((1.0 - rem) * 100.0, 1),
                                reset_time=qinfo.get("resetTime"),
                                is_exhausted=exhausted,
                            )
                        )
            except Exception:
                pass

        return AntigravityQuotaSnapshot(
            project_id=project_id,
            plan_name=plan_name,
            available_prompt_credits=prompt_credits,
            email=cred.email,
            credential_source=cred.source,
            models=windows,
        )

    def fetch_available_models(
        self,
        access_token: str | None = None,
        timeout: float = 3.0,
    ) -> list[tuple[str, str]]:
        """Discover live models for Google Antigravity or return fallback."""
        tok = access_token
        proj_id = getattr(self, "_cached_project_id", None)
        base = getattr(self, "base_url", None)
        if not tok:
            try:
                cred = self._get_active_credential()
                if cred and cred.access_token:
                    tok = cred.access_token
                    proj_id = cred.project_id or proj_id
            except Exception:
                pass
        return fetch_available_models(
            access_token=tok,
            project_id=proj_id,
            base_url=base,
            timeout=timeout,
        )

    def generate(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        cacheable_prefix: str | None = None,
        enable_prompt_caching: bool | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        stop_sequences: list[str] | None = None,
        data_classification: str = "internal",
        budget: Any = None,
        **kwargs: Any,
    ) -> ModelResponse:
        """Execute generation against Google Antigravity (`v1internal:streamGenerateContent?alt=sse`)."""
        if data_classification in ("sensitive", "secret"):
            raise PolicyBlockedError(
                f"Cloud egress forbidden for data classification '{data_classification}'. "
                "Data must remain within 100% local boundary."
            )

        if budget is not None:
            self.budget_tracker.sync_with_task_budget(budget)
        self.budget_tracker.check_budget_before_call()

        cred = self._get_active_credential()
        if not cred or not cred.access_token:
            raise ProviderUnavailableError(
                "No Google Antigravity OAuth token available. Run `hex auth antigravity --login` "
                "or sign in to the Google Antigravity IDE."
            )

        clean_prompt = str(redact_secrets(prompt))
        clean_system = str(redact_secrets(system_prompt)) if system_prompt else None
        clean_prefix = str(redact_secrets(cacheable_prefix)) if cacheable_prefix else None
        project_id = self.ensure_project_id(cred.access_token)

        url, headers, body_bytes = self._build_request(
            clean_prompt,
            access_token=cred.access_token,
            project_id=project_id,
            system_prompt=clean_system,
            cacheable_prefix=clean_prefix,
            max_tokens=max_tokens,
            temperature=temperature,
            stop_sequences=stop_sequences,
        )

        t0 = time.perf_counter()
        status_code, resp_bytes = self._execute_http(url, headers, body_bytes)
        latency_ms = (time.perf_counter() - t0) * 1000.0

        # Handle HTTP 401 unauthenticated with automatic refresh token retry
        if status_code == 401 and cred.refresh_token:
            try:
                refreshed_cred = self.oauth_manager.refresh_access_token(cred)
                self.oauth_manager.save_credential(refreshed_cred)
                self._explicit_cred = refreshed_cred
                new_url, new_headers, new_body = self._build_request(
                    clean_prompt,
                    access_token=refreshed_cred.access_token,
                    project_id=project_id,
                    system_prompt=clean_system,
                    cacheable_prefix=clean_prefix,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    stop_sequences=stop_sequences,
                )
                status_code, resp_bytes = self._execute_http(new_url, new_headers, new_body)
            except Exception:
                pass

        # Handle HTTP 429 quota exhaustion with reset delay extraction
        if status_code == 429:
            reset_delay = self._extract_quota_reset_delay(resp_bytes)
            delay_msg = f" (Quota resets in {reset_delay})" if reset_delay else ""
            raise ProviderUnavailableError(
                f"Google Antigravity quota exhausted for model '{self.model_id}'{delay_msg}."
            )

        if status_code not in (200, 201):
            err_msg = resp_bytes.decode("utf-8", errors="replace")[:500]
            raise ProviderUnavailableError(
                f"Google Antigravity returned HTTP {status_code}: {err_msg}"
            )

        text, in_tokens, out_tokens, cached_tokens, finish_reason, raw_meta = self._parse_response(
            resp_bytes, clean_prompt
        )

        # Antigravity requests are covered by subscription quota ($0.00 marginal API billing)
        cost_usd = 0.0
        self.budget_tracker.record_usage(
            in_tokens,
            out_tokens,
            cost_usd,
            cached_input_tokens=cached_tokens,
        )

        usage = ModelUsage(
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            total_tokens=in_tokens + out_tokens,
            cost_usd=cost_usd,
            cached_input_tokens=cached_tokens,
        )
        raw_meta["quota_backed"] = True
        raw_meta["project_id"] = project_id
        raw_meta["credential_source"] = cred.source

        return ModelResponse(
            text=text,
            model_id=self.model_id,
            provider_id=self.provider_id,
            usage=usage,
            latency_ms=latency_ms,
            finish_reason=finish_reason,
            raw=raw_meta,
        )

    def compress_log(
        self,
        raw_log: str,
        max_chars: int = 2400,
        data_classification: str = "internal",
        budget: Any = None,
    ) -> dict[str, Any]:
        """Compress verbose test/build logs using Antigravity quota at $0 marginal billing."""
        if data_classification in ("sensitive", "secret"):
            raise PolicyBlockedError(
                f"Cloud log compression forbidden for data classification '{data_classification}'."
            )

        from .mlx_provider import Tier1LogCompressor

        local_pre = Tier1LogCompressor(mlx_available=False).compress(raw_log, max_chars=max_chars)
        sanitized = str(redact_secrets(raw_log))
        raw_tokens = max(1, len(sanitized) // 4)

        if not sanitized.strip() or not self.is_available():
            local_pre["engine"] = "antigravity_fallback"
            return local_pre

        system_prompt = (
            "Distill the test/build failure log into a concise failure digest preserving exact "
            "failing file paths, line numbers, traceback frames, and assertion errors."
        )
        resp = self.generate(
            prompt=local_pre["compressed_text"],
            system_prompt=system_prompt,
            max_tokens=512,
            temperature=0.0,
            data_classification=data_classification,
            budget=budget,
        )
        combined = (
            f"[Tier-1 Antigravity ({self.model_id}) Compressed Failure Digest]\n"
            f"{local_pre['compressed_text']}\n{resp.text.strip()}"
        )
        if len(combined) > max_chars:
            combined = combined[: max_chars - 38] + "\n...[truncated by antigravity compressor]"
        comp_tokens = max(1, len(combined) // 4)
        savings_pct = round(max(0.0, (1.0 - (comp_tokens / raw_tokens)) * 100.0), 2)
        return {
            "compressed_text": combined,
            "raw_tokens": raw_tokens,
            "compressed_tokens": comp_tokens,
            "token_savings_pct": savings_pct,
            "failing_files": local_pre["failing_files"],
            "failing_assertions": local_pre["failing_assertions"],
            "engine": "google_antigravity",
            "compressor_model": self.model_id,
            "cost_usd": 0.0,
        }

    def _build_request(
        self,
        clean_prompt: str,
        *,
        access_token: str,
        project_id: str,
        system_prompt: str | None,
        cacheable_prefix: str | None,
        max_tokens: int,
        temperature: float,
        stop_sequences: list[str] | None,
    ) -> tuple[str, dict[str, str], bytes]:
        url = f"{self.base_url}/v1internal:streamGenerateContent?alt=sse"
        headers = self._build_antigravity_headers(access_token)

        # Strictly order parts as cacheable_prefix -> volatile prompt
        parts: list[dict[str, str]] = []
        if cacheable_prefix:
            parts.append({"text": cacheable_prefix})
        parts.append({"text": clean_prompt})

        inner_req: dict[str, Any] = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": temperature,
            },
        }
        if system_prompt:
            inner_req["systemInstruction"] = {"parts": [{"text": system_prompt}]}
        if stop_sequences:
            inner_req["generationConfig"]["stopSequences"] = stop_sequences

        envelope = {
            "project": project_id,
            "model": self.model_id,
            "request": inner_req,
            "requestType": "agent",
            "userAgent": "antigravity",
            "requestId": f"agent-{int(time.time() * 1000)}-{secrets.token_hex(4)}",
        }
        return url, headers, json.dumps(envelope).encode("utf-8")

    @staticmethod
    def _extract_quota_reset_delay(resp_bytes: bytes) -> str | None:
        try:
            data = json.loads(resp_bytes.decode("utf-8", errors="replace"))
            err = data.get("error") or {}
            for detail in err.get("details") or []:
                if isinstance(detail, dict):
                    meta = detail.get("metadata") or {}
                    if meta.get("quotaResetDelay"):
                        return str(meta["quotaResetDelay"])
            msg = str(err.get("message") or "")
            m = re.search(r"reset after ([0-9hms.]+)", msg, re.I)
            if m:
                return m.group(1)
        except Exception:
            pass
        return None

    def _parse_response(
        self, resp_bytes: bytes, prompt: str
    ) -> tuple[str, int, int, int, str, dict[str, Any]]:
        raw_str = resp_bytes.decode("utf-8", errors="replace").strip()
        text_chunks: list[str] = []
        in_tokens = 0
        out_tokens = 0
        cached_tokens = 0
        finish_reason = "stop"
        last_obj: dict[str, Any] = {}

        # Support both SSE (`data: {...}`) streams and single JSON object payloads
        json_payloads: list[dict[str, Any]] = []
        if "data:" in raw_str:
            for line in raw_str.splitlines():
                sline = line.strip()
                if not sline.startswith("data:"):
                    continue
                data_part = sline[5:].strip()
                if not data_part or data_part == "[DONE]":
                    continue
                try:
                    parsed = json.loads(data_part)
                    if isinstance(parsed, dict):
                        json_payloads.append(parsed)
                except json.JSONDecodeError:
                    continue
        else:
            try:
                parsed = json.loads(raw_str)
                if isinstance(parsed, dict):
                    json_payloads.append(parsed)
                elif isinstance(parsed, list):
                    json_payloads.extend(p for p in parsed if isinstance(p, dict))
            except json.JSONDecodeError as e:
                raise ModelProviderError(f"Failed to parse Antigravity response: {e}") from e

        for payload in json_payloads:
            last_obj = payload
            inner = payload.get("response") if isinstance(payload.get("response"), dict) else payload
            assert isinstance(inner, dict)
            candidates = inner.get("candidates") or []
            if isinstance(candidates, list):
                for cand in candidates:
                    if not isinstance(cand, dict):
                        continue
                    content = cand.get("content") or {}
                    if isinstance(content, dict):
                        for part in content.get("parts") or []:
                            if isinstance(part, dict) and isinstance(part.get("text"), str):
                                text_chunks.append(part["text"])
                    if cand.get("finishReason"):
                        finish_reason = str(cand["finishReason"]).lower()
            if isinstance(inner.get("text"), str) and not text_chunks:
                text_chunks.append(inner["text"])

            usage_meta = inner.get("usageMetadata") or payload.get("usageMetadata") or {}
            if isinstance(usage_meta, dict):
                in_tokens = max(in_tokens, int(usage_meta.get("promptTokenCount", 0)))
                out_tokens = max(out_tokens, int(usage_meta.get("candidatesTokenCount", 0)))
                cached_tokens = max(cached_tokens, int(usage_meta.get("cachedContentTokenCount", 0)))

        full_text = "".join(text_chunks)
        if in_tokens <= 0:
            in_tokens = max(1, len(prompt) // 4)
        if out_tokens <= 0:
            out_tokens = max(1, len(full_text) // 4)

        return full_text, in_tokens, out_tokens, cached_tokens, finish_reason, last_obj

    def _execute_http(self, url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
        if self.http_transport is not None:
            return self.http_transport(url, headers, body)

        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30.0) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except Exception as e:
            raise ProviderUnavailableError(f"Antigravity HTTP request failed: {e}") from e
