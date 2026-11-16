"""Comprehensive unit tests for CredentialPool, Model Cooldowns, and Endpoint Blackholing.

Verifies:
1. Multi-key registration and ingestion from environment and config dictionaries.
2. Selection strategies (round_robin, least_used, fill_first).
3. Failover rotation, jittered backoff, and terminal invalidation (mark_dead).
4. Model-scoped cooldown queues protecting siblings from 503 / overload.
5. 30-second transient endpoint blackholing and connect-timeout detection.
6. Backward-compatibility facade and top-level module forwarding.
"""

from __future__ import annotations

import socket
import time
import urllib.error
from unittest.mock import patch
import pytest

from harness_core.models.credential_pool import (
    CredentialPool,
    CredentialEntry,
    STATUS_OK,
    STATUS_EXHAUSTED,
    STATUS_DEAD,
    STRATEGY_ROUND_ROBIN,
    STRATEGY_LEAST_USED,
    STRATEGY_FILL_FIRST,
    _ENDPOINT_BLACKHOLE_TTL_SECONDS,
    _endpoint_host_key,
    _note_endpoint_blackholed,
    _endpoint_blackholed,
    _is_connect_timeout,
    _note_if_connect_timeout,
    _clear_endpoint_blackholes,
    _get_blackholed_endpoints,
    check_endpoint_or_raise,
)
import harness_core.models.credential_pool as top_level_cp
from harness_core.models import CredentialPool as ModelsCredentialPool


@pytest.fixture(autouse=True)
def clean_blackholes():
    """Ensure clean endpoint blackhole cache before and after every test."""
    _clear_endpoint_blackholes()
    yield
    _clear_endpoint_blackholes()


# --- 1. Multi-Key Registration and Ingestion ---


def test_add_key_basic_and_duplicate_deduplication():
    pool = CredentialPool()
    e1 = pool.add_key("openai", "sk-test-1", key_id="key-1", priority=10)
    assert e1.id == "key-1"
    assert e1.provider == "openai"
    assert e1.secret_value == "sk-test-1"
    assert e1.priority == 10
    assert e1.status == STATUS_OK
    assert e1.is_healthy is True

    # Adding identical secret_value updates metadata and priority without creating duplicates
    e2 = pool.add_key("openai", "sk-test-1", priority=20, metadata={"source": "test"})
    assert e2.id == "key-1"
    assert e2.priority == 20
    assert e2.metadata["source"] == "test"
    assert len(pool.list_keys("openai")) == 1

    with pytest.raises(ValueError):
        pool.add_key("openai", "   ")


def test_load_from_env():
    pool = CredentialPool()
    fake_env = {
        "OPENAI_API_KEY": "sk-openai-primary",
        "OPENAI_API_KEY_1": "sk-openai-secondary",
        "OPENAI_API_KEY_2": "sk-openai-tertiary",
        "ANTHROPIC_API_KEYS": "sk-ant-1, sk-ant-2, sk-ant-3",
        "GOOGLE_API_KEY": "ai-gemini-key",
        "IGNORED_VAR": "not-a-key",
    }
    loaded = pool.load_from_env(env=fake_env)
    assert loaded == 7

    openai_keys = pool.list_keys("openai")
    assert len(openai_keys) == 3
    assert {k.secret_value for k in openai_keys} == {
        "sk-openai-primary",
        "sk-openai-secondary",
        "sk-openai-tertiary",
    }

    ant_keys = pool.list_keys("anthropic")
    assert len(ant_keys) == 3
    assert {k.secret_value for k in ant_keys} == {"sk-ant-1", "sk-ant-2", "sk-ant-3"}

    gemini_keys = pool.list_keys("gemini")
    assert len(gemini_keys) == 1
    assert gemini_keys[0].secret_value == "ai-gemini-key"


def test_load_from_env_filtered_providers():
    pool = CredentialPool()
    fake_env = {
        "OPENAI_API_KEY": "sk-openai-1",
        "ANTHROPIC_API_KEY": "sk-ant-1",
    }
    loaded = pool.load_from_env(env=fake_env, providers=["anthropic"])
    assert loaded == 1
    assert len(pool.list_keys("openai")) == 0
    assert len(pool.list_keys("anthropic")) == 1


def test_load_from_config():
    pool = CredentialPool()
    config = {
        "credentials": {
            "openai": ["sk-1", "sk-2"],
            "groq": "gsk-1, gsk-2",
            "deepseek": {
                "keys": ["dsk-1", "dsk-2"],
            },
            "together": [
                {"id": "tog-primary", "key": "tog-1", "priority": 100},
            ],
        }
    }
    loaded = pool.load_from_config(config)
    assert loaded == 7
    assert len(pool.list_keys("openai")) == 2
    assert len(pool.list_keys("groq")) == 2
    assert len(pool.list_keys("deepseek")) == 2
    tog = pool.get_entry("together", "tog-primary")
    assert tog is not None
    assert tog.secret_value == "tog-1"
    assert tog.priority == 100


# --- 2. Selection Strategies ---


def test_strategy_round_robin():
    pool = CredentialPool(default_strategy=STRATEGY_ROUND_ROBIN)
    pool.add_key("openai", "sk-1")
    pool.add_key("openai", "sk-2")
    pool.add_key("openai", "sk-3")

    dispatched = [pool.get_key("openai").secret_value for _ in range(6)]
    assert dispatched == ["sk-1", "sk-2", "sk-3", "sk-1", "sk-2", "sk-3"]

    # Verify usage counters increment
    keys = {k.secret_value: k.usage_count for k in pool.list_keys("openai")}
    assert keys == {"sk-1": 2, "sk-2": 2, "sk-3": 2}


def test_strategy_least_used():
    pool = CredentialPool()
    pool.set_strategy("openai", STRATEGY_LEAST_USED)
    assert pool.get_strategy("openai") == STRATEGY_LEAST_USED

    k1 = pool.add_key("openai", "sk-1")
    k2 = pool.add_key("openai", "sk-2")
    k3 = pool.add_key("openai", "sk-3")

    # Manually bias usage count
    k1.usage_count = 10
    k2.usage_count = 5
    k3.usage_count = 0

    first = pool.get_key("openai")
    assert first.secret_value == "sk-3"
    assert first.usage_count == 1

    second = pool.get_key("openai")
    assert second.secret_value == "sk-3"
    assert second.usage_count == 2


def test_strategy_fill_first():
    pool = CredentialPool()
    pool.set_strategy("openai", STRATEGY_FILL_FIRST)
    pool.add_key("openai", "sk-low-priority", priority=1)
    pool.add_key("openai", "sk-high-priority", priority=100)

    # Should keep selecting sk-high-priority as long as it's healthy
    for _ in range(3):
        k = pool.get_key("openai")
        assert k.secret_value == "sk-high-priority"

    # When high priority key is exhausted, falls back to sk-low-priority
    pool.mark_exhausted("openai", "sk-high-priority")
    fallback = pool.get_key("openai")
    assert fallback.secret_value == "sk-low-priority"


def test_invalid_strategy_rejected():
    pool = CredentialPool()
    with pytest.raises(ValueError, match="Unsupported strategy"):
        pool.set_strategy("openai", "invalid_strategy")


# --- 3. Failover Rotation and Backoff ---


def test_failover_rotation_on_exhausted():
    pool = CredentialPool()
    pool.add_key("anthropic", "sk-ant-1")
    pool.add_key("anthropic", "sk-ant-2")

    k_first = pool.get_key("anthropic")
    assert k_first.secret_value == "sk-ant-1"

    # Mark first key exhausted
    pool.mark_exhausted("anthropic", "sk-ant-1", retry_after=120, reason="HTTP 429")
    entry_1 = pool.get_entry("anthropic", "sk-ant-1")
    assert entry_1.status == STATUS_EXHAUSTED
    assert entry_1.error_reason == "HTTP 429"
    assert entry_1.cooldown_until is not None

    # Next get_key call rotates to remaining healthy key
    k_next = pool.get_key("anthropic")
    assert k_next.secret_value == "sk-ant-2"

    # Exhaust second key too
    pool.mark_exhausted("anthropic", "sk-ant-2")
    assert pool.get_key("anthropic") is None


def test_exhausted_recovery_after_cooldown():
    pool = CredentialPool()
    pool.add_key("openai", "sk-sole")

    now = 1000.0
    with patch("time.monotonic", return_value=now):
        k = pool.get_key("openai")
        assert k.secret_value == "sk-sole"
        # Sole key cooldown defaults to 60s (+ jitter up to 10%)
        pool.mark_exhausted("openai", "sk-sole", retry_after=50.0)
        assert pool.get_key("openai") is None

    # Leap forward 40s (still within cooldown)
    with patch("time.monotonic", return_value=now + 40.0):
        assert pool.get_key("openai") is None

    # Leap forward 56s (past cooldown + jitter)
    with patch("time.monotonic", return_value=now + 56.0):
        recovered = pool.get_key("openai")
        assert recovered is not None
        assert recovered.secret_value == "sk-sole"
        assert recovered.status == STATUS_OK
        assert recovered.cooldown_until is None


def test_mark_dead_permanent_exclusion():
    pool = CredentialPool()
    pool.add_key("openai", "sk-invalid")
    pool.add_key("openai", "sk-valid")

    pool.mark_dead("openai", "sk-invalid", reason="401 Invalid API Key")
    invalid_entry = pool.get_entry("openai", "sk-invalid")
    assert invalid_entry.status == STATUS_DEAD
    assert invalid_entry.is_healthy is False
    assert invalid_entry.error_reason == "401 Invalid API Key"

    # Repeated calls only yield valid key
    for _ in range(4):
        k = pool.get_key("openai")
        assert k.secret_value == "sk-valid"

    # Even far in the future, dead key stays dead
    with patch("time.monotonic", return_value=time.monotonic() + 10_000_000):
        k = pool.get_key("openai")
        assert k.secret_value == "sk-valid"


def test_pool_reset():
    pool = CredentialPool()
    pool.add_key("openai", "sk-1")
    pool.mark_exhausted("openai", "sk-1")
    assert pool.get_key("openai") is None

    pool.reset("openai")
    recovered = pool.get_key("openai")
    assert recovered is not None
    assert recovered.status == STATUS_OK


# --- 4. Model Cooldown Queues ---


def test_model_cooldown_isolates_model_preserves_siblings():
    pool = CredentialPool()
    pool.add_key("openai", "sk-shared")

    # Cool down o3-mini on 503 transient overload
    pool.mark_model_cooldown("openai", "o3-mini", duration_seconds=60.0, reason="503 Service Unavailable")

    assert pool.is_model_in_cooldown("openai", "o3-mini") is True
    assert pool.is_model_in_cooldown("openai", "gpt-4o") is False
    assert pool.get_model_cooldown_remaining("openai", "o3-mini") > 50.0
    assert pool.get_model_cooldown_remaining("openai", "gpt-4o") == 0.0

    # Dispatching o3-mini fails
    assert pool.get_key("openai", model="o3-mini") is None

    # Dispatching sibling gpt-4o on same key succeeds
    sibling = pool.get_key("openai", model="gpt-4o")
    assert sibling is not None
    assert sibling.secret_value == "sk-shared"


def test_model_cooldown_expiry_and_clear():
    pool = CredentialPool()
    pool.add_key("openai", "sk-key")

    now = 5000.0
    with patch("time.monotonic", return_value=now):
        pool.mark_model_cooldown("openai", "claude-test", duration_seconds=30.0)
        assert pool.is_model_in_cooldown("openai", "claude-test") is True

    # 10 seconds later: still cooling down
    with patch("time.monotonic", return_value=now + 10.0):
        assert pool.is_model_in_cooldown("openai", "claude-test") is True

    # 35 seconds later: cooldown has expired
    with patch("time.monotonic", return_value=now + 35.0):
        assert pool.is_model_in_cooldown("openai", "claude-test") is False
        k = pool.get_key("openai", model="claude-test")
        assert k is not None

    # Manual clear
    pool.mark_model_cooldown("openai", "claude-test", duration_seconds=100.0)
    assert pool.is_model_in_cooldown("openai", "claude-test") is True
    pool.clear_model_cooldown("openai", "claude-test")
    assert pool.is_model_in_cooldown("openai", "claude-test") is False


# --- 5. Transient Endpoint Blackholing ---


def test_endpoint_host_key_normalization():
    assert _endpoint_host_key("http://192.168.1.200:11434") == "192.168.1.200:11434"
    assert _endpoint_host_key("http://192.168.1.200:11434/api/generate") == "192.168.1.200:11434"
    assert _endpoint_host_key("https://api.openai.com/v1") == "api.openai.com:443"
    assert _endpoint_host_key("http://localhost") == "localhost:80"
    assert _endpoint_host_key("localhost:8000") == "localhost:8000"
    assert _endpoint_host_key("") is None


def test_endpoint_blackhole_recording_and_ttl_expiry():
    url = "http://192.168.1.200:11434"
    assert _endpoint_blackholed(url) is False

    now = 2000.0
    with patch("time.monotonic", return_value=now):
        _note_endpoint_blackholed(url)
        assert _endpoint_blackholed(url) is True
        assert _endpoint_blackholed("http://192.168.1.200:11434/api/tags") is True
        # Different port should NOT be blackholed
        assert _endpoint_blackholed("http://192.168.1.200:8000") is False

        # check_endpoint_or_raise raises ConnectionError
        with pytest.raises(ConnectionError, match="temporarily blackholed"):
            check_endpoint_or_raise(url)

    # 15 seconds later: still blackholed
    with patch("time.monotonic", return_value=now + 15.0):
        assert _endpoint_blackholed(url) is True

    # 31 seconds later: TTL expired, blackhole cleared
    with patch("time.monotonic", return_value=now + 31.0):
        assert _endpoint_blackholed(url) is False
        # Should no longer raise
        check_endpoint_or_raise(url)


def test_is_connect_timeout_detection():
    # Socket / OS timeouts
    assert _is_connect_timeout(socket.timeout("timed out")) is True
    assert _is_connect_timeout(TimeoutError("timed out")) is True

    # urllib wrapper
    url_err_timeout = urllib.error.URLError(socket.timeout("timed out"))
    assert _is_connect_timeout(url_err_timeout) is True

    # Exception naming
    class MockConnectTimeout(Exception):
        pass

    assert _is_connect_timeout(MockConnectTimeout("connect timeout")) is True

    # Non-connect errors must NOT trigger blackholing
    assert _is_connect_timeout(ValueError("bad value")) is False
    assert _is_connect_timeout(FileNotFoundError()) is False
    assert _is_connect_timeout(urllib.error.HTTPError("http://x", 404, "Not Found", {}, None)) is False
    assert _is_connect_timeout(urllib.error.HTTPError("http://x", 500, "Server Error", {}, None)) is False


def test_note_if_connect_timeout_helper():
    url = "http://10.0.0.99:11434"
    assert _endpoint_blackholed(url) is False

    # Non-timeout error does not blackhole
    noted_false = _note_if_connect_timeout(ValueError("syntax error"), url)
    assert noted_false is False
    assert _endpoint_blackholed(url) is False

    # Connect timeout triggers blackhole
    noted_true = _note_if_connect_timeout(TimeoutError("Connection timed out"), url)
    assert noted_true is True
    assert _endpoint_blackholed(url) is True


# --- 6. Backward Compatibility & Top-Level Stub ---


def test_backward_compatibility_top_level_stub():
    # Verify harness_core.models.credential_pool exports all expected attributes
    assert top_level_cp.CredentialPool is CredentialPool
    assert top_level_cp.STATUS_OK == STATUS_OK
    assert top_level_cp.STATUS_EXHAUSTED == STATUS_EXHAUSTED
    assert top_level_cp.STATUS_DEAD == STATUS_DEAD
    assert callable(top_level_cp._endpoint_blackholed)
    assert callable(top_level_cp._note_endpoint_blackholed)


def test_models_subpackage_reexports():
    # Verify harness_core.models re-exports
    assert ModelsCredentialPool is CredentialPool
    pool = ModelsCredentialPool()
    entry = pool.add_key("groq", "gsk-test")
    assert entry.secret_value == "gsk-test"
    assert pool.get_key("groq").secret_value == "gsk-test"
