"""Resilient multi-key credential pool and transient endpoint blackholing.

Provides multi-key pool management per provider, round-robin / least-utilized
routing, automatic rotation on HTTP 429 / quota exhaustion with jittered backoff,
model-specific cooldown queues ((provider, model) -> cooldown_until), and 30-second
transient endpoint blackholing to protect against orchestrator thread lockup on
dead servers.
"""

from __future__ import annotations

import logging
import os
import random
import re
import socket
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

# --- Status and Strategy Constants ---

STATUS_OK: str = "ok"
STATUS_EXHAUSTED: str = "exhausted"
STATUS_DEAD: str = "dead"

STRATEGY_ROUND_ROBIN: str = "round_robin"
STRATEGY_LEAST_USED: str = "least_used"
STRATEGY_FILL_FIRST: str = "fill_first"

SUPPORTED_STRATEGIES: set[str] = {
    STRATEGY_ROUND_ROBIN,
    STRATEGY_LEAST_USED,
    STRATEGY_FILL_FIRST,
}

# Cooldown defaults
DEFAULT_SOLE_KEY_COOLDOWN: float = 60.0
DEFAULT_POOLED_KEY_COOLDOWN: float = 3600.0
DEFAULT_JITTER_RATIO: float = 0.1

# 30-second transient endpoint blackholing sentry
_ENDPOINT_BLACKHOLE_TTL_SECONDS: float = 30.0
_endpoint_blackhole_cache: dict[str, float] = {}  # host:port -> monotonic ts
_blackhole_lock = threading.Lock()


# --- Transient Endpoint Blackholing Helpers ---


def _endpoint_host_key(base_url: str) -> Optional[str]:
    """Parse hostname:port key from an HTTP/HTTPS base URL."""
    try:
        raw = (base_url or "").strip()
        if not raw:
            return None
        if "://" not in raw:
            raw = f"http://{raw}"
        parsed = urllib.parse.urlparse(raw)
        if not parsed.hostname:
            return None
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return f"{parsed.hostname}:{port}"
    except Exception:
        return None


def _note_endpoint_blackholed(base_url: str) -> None:
    """Record that a connection to base_url timed out during TCP connect."""
    key = _endpoint_host_key(base_url)
    if key is not None:
        with _blackhole_lock:
            _endpoint_blackhole_cache[key] = time.monotonic()
        logger.debug(
            "Endpoint %s timed out connecting - blackholed for %.0fs",
            key,
            _ENDPOINT_BLACKHOLE_TTL_SECONDS,
        )


def _endpoint_blackholed(base_url: str) -> bool:
    """True if a recent connection to base_url timed out during TCP connect."""
    if _ENDPOINT_BLACKHOLE_TTL_SECONDS <= 0:
        return False
    key = _endpoint_host_key(base_url)
    if key is None:
        return False
    with _blackhole_lock:
        seen = _endpoint_blackhole_cache.get(key)
        if seen is None:
            return False
        if (time.monotonic() - seen) >= _ENDPOINT_BLACKHOLE_TTL_SECONDS:
            del _endpoint_blackhole_cache[key]
            return False
        return True


def _clear_endpoint_blackholes() -> None:
    """Clear all blackholed endpoints from the in-memory cache."""
    with _blackhole_lock:
        _endpoint_blackhole_cache.clear()


def _get_blackholed_endpoints() -> dict[str, float]:
    """Return a copy of currently recorded blackholed endpoints."""
    with _blackhole_lock:
        return dict(_endpoint_blackhole_cache)


def check_endpoint_or_raise(base_url: str) -> None:
    """Raise ConnectionError if base_url is currently blackholed due to connect timeout."""
    if _endpoint_blackholed(base_url):
        key = _endpoint_host_key(base_url) or base_url
        raise ConnectionError(
            f"Endpoint '{key}' is temporarily blackholed ({_ENDPOINT_BLACKHOLE_TTL_SECONDS:.0f}s TTL) due to connect timeout."
        )


def _is_connect_timeout(exc: BaseException) -> bool:
    """Return True if exception indicates a TCP connect-phase timeout."""
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return True
    try:
        import httpx

        if isinstance(exc, httpx.ConnectTimeout):
            return True
    except ImportError:
        pass
    import urllib.error

    if isinstance(exc, urllib.error.URLError):
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            return True
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if (
        "connecttimeout" in name
        or "connect timeout" in msg
        or "connection timed out" in msg
    ):
        return True
    return False


def _note_if_connect_timeout(exc: BaseException, base_url: str) -> bool:
    """Blackhole base_url if exc is a connect-phase timeout."""
    if _is_connect_timeout(exc):
        _note_endpoint_blackholed(base_url)
        return True
    return False


# --- Credential Entry Data Model ---


@dataclass
class CredentialEntry:
    id: str
    provider: str
    secret_value: str
    status: str = STATUS_OK
    usage_count: int = 0
    last_used_at: Optional[float] = None
    cooldown_until: Optional[float] = None
    priority: int = 0
    error_reason: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return self.secret_value

    @property
    def access_token(self) -> str:
        return self.secret_value

    @property
    def is_healthy(self) -> bool:
        if self.status == STATUS_DEAD:
            return False
        if self.status == STATUS_EXHAUSTED:
            if self.cooldown_until is not None and time.monotonic() >= self.cooldown_until:
                return True
            return False
        return self.status == STATUS_OK


# --- Multi-Key Credential Pool ---


class CredentialPool:
    """Thread-safe credential pool managing multi-key routing, rotation, and model cooldowns."""

    def __init__(
        self,
        default_strategy: str = STRATEGY_ROUND_ROBIN,
        sole_key_cooldown: float = DEFAULT_SOLE_KEY_COOLDOWN,
        pooled_key_cooldown: float = DEFAULT_POOLED_KEY_COOLDOWN,
        jitter_ratio: float = DEFAULT_JITTER_RATIO,
    ) -> None:
        self._lock = threading.Lock()
        self._default_strategy = default_strategy
        self._sole_key_cooldown = sole_key_cooldown
        self._pooled_key_cooldown = pooled_key_cooldown
        self._jitter_ratio = jitter_ratio
        self._entries: dict[str, list[CredentialEntry]] = {}
        self._rr_indices: dict[str, int] = {}
        self._strategies: dict[str, str] = {}
        self._model_cooldowns: dict[tuple[str, str], float] = {}

    def set_strategy(self, provider: str, strategy: str) -> None:
        """Set the selection strategy for a provider."""
        norm = strategy.strip().lower()
        if norm not in SUPPORTED_STRATEGIES:
            raise ValueError(
                f"Unsupported strategy '{strategy}'. Supported: {SUPPORTED_STRATEGIES}"
            )
        with self._lock:
            self._strategies[provider.strip().lower()] = norm

    def get_strategy(self, provider: str) -> str:
        """Return the selection strategy configured for a provider."""
        with self._lock:
            return self._strategies.get(
                provider.strip().lower(), self._default_strategy
            )

    def add_key(
        self,
        provider: str,
        secret_value: str,
        key_id: Optional[str] = None,
        priority: int = 0,
        metadata: Optional[dict[str, Any]] = None,
    ) -> CredentialEntry:
        """Add or update an API key in the provider pool."""
        p = provider.strip().lower()
        val = secret_value.strip()
        if not val:
            raise ValueError("secret_value cannot be empty")
        with self._lock:
            if p not in self._entries:
                self._entries[p] = []
            for existing in self._entries[p]:
                if existing.secret_value == val:
                    if metadata:
                        existing.metadata.update(metadata)
                    existing.priority = priority
                    return existing
            entry_id = key_id or f"{p}_{uuid.uuid4().hex[:8]}"
            entry = CredentialEntry(
                id=entry_id,
                provider=p,
                secret_value=val,
                priority=priority,
                metadata=dict(metadata or {}),
            )
            self._entries[p].append(entry)
            return entry

    def load_from_env(
        self,
        env: Optional[dict[str, str]] = None,
        providers: Optional[Iterable[str]] = None,
    ) -> int:
        """Ingest keys from environment variables matching *_API_KEY* patterns."""
        source_env = os.environ if env is None else env
        target_providers = (
            {p.strip().lower() for p in providers} if providers else None
        )
        count = 0

        key_pattern = re.compile(r"^([A-Z0-9_]+)_API_KEY(?:_(\d+))?$", re.IGNORECASE)
        keys_pattern = re.compile(r"^([A-Z0-9_]+)_API_KEYS$", re.IGNORECASE)

        for k, v in source_env.items():
            if not v or not v.strip():
                continue
            v = v.strip()

            m = key_pattern.match(k)
            if m:
                raw_prov = m.group(1).lower()
                prov = "gemini" if raw_prov in ("google", "gemini") else raw_prov
                if target_providers and prov not in target_providers:
                    continue
                subkeys = [s.strip() for s in v.split(",") if s.strip()]
                for sk in subkeys:
                    self.add_key(prov, sk)
                    count += 1
                continue

            m = keys_pattern.match(k)
            if m:
                raw_prov = m.group(1).lower()
                prov = "gemini" if raw_prov in ("google", "gemini") else raw_prov
                if target_providers and prov not in target_providers:
                    continue
                subkeys = [s.strip() for s in v.split(",") if s.strip()]
                for sk in subkeys:
                    self.add_key(prov, sk)
                    count += 1
                continue
        return count

    def load_from_config(self, config: dict[str, Any]) -> int:
        """Ingest keys from a configuration dictionary."""
        count = 0
        providers_data = config.get("credentials") or config.get("providers") or config
        if not isinstance(providers_data, dict):
            return 0
        for prov, val in providers_data.items():
            prov_norm = str(prov).strip().lower()
            if isinstance(val, str):
                for sk in [s.strip() for s in val.split(",") if s.strip()]:
                    self.add_key(prov_norm, sk)
                    count += 1
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, str) and item.strip():
                        self.add_key(prov_norm, item.strip())
                        count += 1
                    elif isinstance(item, dict) and "key" in item:
                        self.add_key(
                            prov_norm,
                            str(item["key"]),
                            key_id=item.get("id"),
                            priority=item.get("priority", 0),
                            metadata=item.get("metadata"),
                        )
                        count += 1
            elif isinstance(val, dict):
                keys = val.get("api_keys") or val.get("keys") or [val.get("api_key")]
                if isinstance(keys, list):
                    for item in keys:
                        if isinstance(item, str) and item.strip():
                            self.add_key(prov_norm, item.strip())
                            count += 1
                elif isinstance(keys, str) and keys.strip():
                    for sk in [s.strip() for s in keys.split(",") if s.strip()]:
                        self.add_key(prov_norm, sk)
                        count += 1
        return count

    def _refresh_cooldowns_locked(self, provider: str, now: float) -> None:
        """Recover expired exhausted keys to STATUS_OK."""
        entries = self._entries.get(provider, [])
        for entry in entries:
            if entry.status == STATUS_EXHAUSTED and entry.cooldown_until is not None:
                if now >= entry.cooldown_until:
                    entry.status = STATUS_OK
                    entry.cooldown_until = None
                    entry.error_reason = None

    def get_key(
        self,
        provider: str,
        model: Optional[str] = None,
        strategy: Optional[str] = None,
    ) -> Optional[CredentialEntry]:
        """Select a healthy key for the given provider and model."""
        p = provider.strip().lower()
        now = time.monotonic()

        # Check model cooldown first
        if model and self.is_model_in_cooldown(p, model):
            return None

        with self._lock:
            self._refresh_cooldowns_locked(p, now)
            healthy = [e for e in self._entries.get(p, []) if e.status == STATUS_OK]
            if not healthy:
                return None

            strat = (
                strategy or self._strategies.get(p, self._default_strategy)
            ).lower()
            if strat == STRATEGY_ROUND_ROBIN:
                idx = self._rr_indices.get(p, 0)
                selected = healthy[idx % len(healthy)]
                self._rr_indices[p] = (idx + 1) % len(healthy)
            elif strat == STRATEGY_LEAST_USED:
                selected = min(
                    healthy, key=lambda e: (e.usage_count, e.last_used_at or 0.0)
                )
            elif strat == STRATEGY_FILL_FIRST:
                selected = sorted(healthy, key=lambda e: -e.priority)[0]
            else:
                idx = self._rr_indices.get(p, 0)
                selected = healthy[idx % len(healthy)]
                self._rr_indices[p] = (idx + 1) % len(healthy)

            selected.usage_count += 1
            selected.last_used_at = now
            return selected

    def mark_exhausted(
        self,
        provider: str,
        key_id: Optional[str] = None,
        retry_after: Optional[float] = None,
        reason: Optional[str] = None,
    ) -> None:
        """Mark a credential as exhausted with jittered exponential/linear backoff."""
        p = provider.strip().lower()
        now = time.monotonic()
        with self._lock:
            entries = self._entries.get(p, [])
            if not entries:
                return
            target: Optional[CredentialEntry] = None
            if key_id:
                for e in entries:
                    if e.id == key_id or e.secret_value == key_id:
                        target = e
                        break
            if target is None:
                used = [e for e in entries if e.last_used_at is not None]
                if used:
                    target = max(used, key=lambda e: e.last_used_at or 0.0)
                else:
                    target = entries[0]

            non_dead = [e for e in entries if e.status != STATUS_DEAD]
            is_sole = len(non_dead) <= 1
            if retry_after is not None and retry_after > 0:
                base_duration = float(retry_after)
            else:
                base_duration = (
                    self._sole_key_cooldown if is_sole else self._pooled_key_cooldown
                )

            jitter = random.uniform(0.0, self._jitter_ratio * base_duration)
            duration = base_duration + jitter

            target.status = STATUS_EXHAUSTED
            target.cooldown_until = now + duration
            target.error_reason = reason or "HTTP 429 / Quota Exhausted"

    def mark_dead(
        self,
        provider: str,
        key_id: Optional[str] = None,
        reason: str = "invalid_token",
    ) -> None:
        """Mark a key permanently dead on terminal 401s."""
        p = provider.strip().lower()
        with self._lock:
            entries = self._entries.get(p, [])
            if not entries:
                return
            target: Optional[CredentialEntry] = None
            if key_id:
                for e in entries:
                    if e.id == key_id or e.secret_value == key_id:
                        target = e
                        break
            if target is None:
                used = [e for e in entries if e.last_used_at is not None]
                if used:
                    target = max(used, key=lambda e: e.last_used_at or 0.0)
                else:
                    target = entries[0]

            target.status = STATUS_DEAD
            target.cooldown_until = None
            target.error_reason = reason

    def list_keys(self, provider: Optional[str] = None) -> list[CredentialEntry]:
        """Return all entries for a provider or the entire pool."""
        with self._lock:
            if provider:
                return list(self._entries.get(provider.strip().lower(), []))
            all_keys: list[CredentialEntry] = []
            for entries in self._entries.values():
                all_keys.extend(entries)
            return all_keys

    def get_entry(self, provider: str, key_id: str) -> Optional[CredentialEntry]:
        """Look up a specific entry by ID or secret value."""
        with self._lock:
            for e in self._entries.get(provider.strip().lower(), []):
                if e.id == key_id or e.secret_value == key_id:
                    return e
            return None

    def reset(self, provider: Optional[str] = None) -> None:
        """Reset statuses and cooldowns for a provider or all providers."""
        with self._lock:
            if provider:
                p = provider.strip().lower()
                for e in self._entries.get(p, []):
                    e.status = STATUS_OK
                    e.cooldown_until = None
                    e.error_reason = None
                self._rr_indices.pop(p, None)
                for mp, mm in list(self._model_cooldowns.keys()):
                    if mp == p:
                        del self._model_cooldowns[(mp, mm)]
            else:
                for entries in self._entries.values():
                    for e in entries:
                        e.status = STATUS_OK
                        e.cooldown_until = None
                        e.error_reason = None
                self._rr_indices.clear()
                self._model_cooldowns.clear()

    # --- Model Cooldown Methods ---

    def mark_model_cooldown(
        self,
        provider: str,
        model: str,
        duration_seconds: float = 60.0,
        reason: Optional[str] = None,
    ) -> None:
        """Cool down a specific (provider, model) on transient overload or 503."""
        p = provider.strip().lower()
        m = model.strip().lower()
        until = time.monotonic() + max(0.0, duration_seconds)
        with self._lock:
            self._model_cooldowns[(p, m)] = until
        logger.debug(
            "Model (%s, %s) cooling down for %.1fs (until %.2f) - reason: %s",
            p,
            m,
            duration_seconds,
            until,
            reason or "overload",
        )

    def is_model_in_cooldown(self, provider: str, model: str) -> bool:
        """Return True if (provider, model) is actively cooling down."""
        p = provider.strip().lower()
        m = model.strip().lower()
        now = time.monotonic()
        with self._lock:
            until = self._model_cooldowns.get((p, m))
            if until is None:
                return False
            if now >= until:
                del self._model_cooldowns[(p, m)]
                return False
            return True

    def clear_model_cooldown(self, provider: str, model: str) -> None:
        """Clear active cooldown for (provider, model)."""
        p = provider.strip().lower()
        m = model.strip().lower()
        with self._lock:
            self._model_cooldowns.pop((p, m), None)

    def get_model_cooldown_remaining(self, provider: str, model: str) -> float:
        """Return remaining cooldown seconds for (provider, model), or 0.0."""
        p = provider.strip().lower()
        m = model.strip().lower()
        now = time.monotonic()
        with self._lock:
            until = self._model_cooldowns.get((p, m))
            if until is None:
                return 0.0
            if now >= until:
                del self._model_cooldowns[(p, m)]
                return 0.0
            return max(0.0, until - now)

    # --- Endpoint Blackhole Helper Methods ---

    def is_endpoint_blackholed(self, base_url: str) -> bool:
        """Return True if base_url is currently blackholed due to connect timeout."""
        return _endpoint_blackholed(base_url)

    def note_endpoint_blackholed(self, base_url: str) -> None:
        """Record that base_url timed out during connection phase."""
        _note_endpoint_blackholed(base_url)


__all__ = [
    "CredentialPool",
    "CredentialEntry",
    "STATUS_OK",
    "STATUS_EXHAUSTED",
    "STATUS_DEAD",
    "STRATEGY_ROUND_ROBIN",
    "STRATEGY_LEAST_USED",
    "STRATEGY_FILL_FIRST",
    "SUPPORTED_STRATEGIES",
    "DEFAULT_SOLE_KEY_COOLDOWN",
    "DEFAULT_POOLED_KEY_COOLDOWN",
    "DEFAULT_JITTER_RATIO",
    "_ENDPOINT_BLACKHOLE_TTL_SECONDS",
    "_endpoint_host_key",
    "_note_endpoint_blackholed",
    "_endpoint_blackholed",
    "_is_connect_timeout",
    "_note_if_connect_timeout",
    "_clear_endpoint_blackholes",
    "_get_blackholed_endpoints",
    "check_endpoint_or_raise",
]
