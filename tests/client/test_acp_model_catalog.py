"""B32 regression: session/models must report the live backend catalog.

The desktop picker may only offer models the configured providers actually
serve. Discovery failures must surface honestly (partial error or explicit
RPC error), never as a fabricated hardcoded catalog.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from harness_core.client.acp import ACPServer, ACPSession
from harness_core.providers import cloud_provider as cp_mod
from harness_core.providers.cloud_provider import ProviderUnavailableError


class FakeOrchestrator:
    def __init__(self, *, local_model_id=None, provider_type="openai"):
        self.router = SimpleNamespace(
            cloud_model_id="gpt-4o",
            local_model_id=local_model_id,
            cloud_provider=SimpleNamespace(
                provider_id="fake-openai",
                provider_type=provider_type,
                api_key="sk-test",
                model_id="gpt-4o",
            ),
        )
        self.model_switches: list[str] = []

    def set_active_model(self, model_id, durable=True):
        assert durable is False
        self.model_switches.append(model_id)
        self.router.cloud_model_id = model_id
        self.router.cloud_provider.model_id = model_id
        return (self.router.cloud_provider.provider_id, model_id, None)


@pytest.fixture
def server(tmp_path):
    orch = FakeOrchestrator(local_model_id="local-tier")
    srv = ACPServer(orchestrator=orch, workspace_root=tmp_path)
    srv.sessions["s1"] = ACPSession(session_id="s1", workspace_root=tmp_path)
    return srv


def query(server, sid="s1"):
    return server.handle_request({
        "jsonrpc": "2.0", "id": 1, "method": "session/models",
        "params": {"sessionId": sid},
    })


@pytest.fixture
def discovery(monkeypatch):
    calls: list[tuple[str, str | None]] = []

    def fake_fetch(provider_type, api_key=None, base_url=None, timeout=3.0):
        calls.append((str(provider_type), api_key))
        if str(provider_type) == "local":
            return [("qwen2.5-coder-32b", "qwen2.5-coder-32b (Ollama)")]
        return [("gpt-4o", "GPT-4o"), ("o3-mini", "o3-mini")]

    monkeypatch.setattr(cp_mod, "fetch_available_models", fake_fetch)
    return calls


def test_catalog_reports_live_cloud_and_local_models(server, discovery):
    resp = query(server)
    assert "result" in resp, resp
    res = resp["result"]
    assert res["sessionId"] == "s1"
    assert res["activeModel"] == "gpt-4o"
    assert res["provider"] == "fake-openai"
    assert "error" not in res
    assert [(m["id"], m["isLocal"]) for m in res["models"]] == [
        ("gpt-4o", False), ("o3-mini", False), ("qwen2.5-coder-32b", True),
    ]
    assert res["models"][0]["displayName"] == "GPT-4o"
    assert res["models"][2]["provider"] == "Local"
    # Discovery used the router's configured provider identity and credential.
    assert discovery == [("openai", "sk-test"), ("local", None)]


def test_catalog_without_local_tier_skips_local_probe(server, discovery):
    server.orchestrator.router.local_model_id = None
    res = query(server)["result"]
    assert all(not m["isLocal"] for m in res["models"])
    assert discovery == [("openai", "sk-test")]


def test_partial_discovery_failure_keeps_real_models_and_reports_error(server, monkeypatch):
    def fake_fetch(provider_type, api_key=None, base_url=None, timeout=3.0):
        if str(provider_type) == "local":
            return [("llama-3.3-70b", "llama-3.3-70b (Ollama)")]
        raise ProviderUnavailableError("No OpenAI API key found.")

    monkeypatch.setattr(cp_mod, "fetch_available_models", fake_fetch)
    res = query(server)["result"]
    assert [m["id"] for m in res["models"]] == ["llama-3.3-70b"]
    assert "No OpenAI API key found." in res["error"]
    assert "fake-openai" in res["error"]


def test_total_discovery_failure_is_explicit_error_not_hardcoded_catalog(server, monkeypatch):
    def fake_fetch(provider_type, api_key=None, base_url=None, timeout=3.0):
        raise ProviderUnavailableError("provider offline")

    monkeypatch.setattr(cp_mod, "fetch_available_models", fake_fetch)
    resp = query(server)
    assert "result" not in resp
    assert "No models discovered" in resp["error"]["message"]
    assert "provider offline" in resp["error"]["message"]


def test_empty_discovery_without_local_runtime_errors_honestly(server, monkeypatch):
    server.orchestrator.router.local_model_id = None
    monkeypatch.setattr(cp_mod, "fetch_available_models", lambda *a, **k: [])
    resp = query(server)
    assert "result" not in resp
    assert "No models discovered" in resp["error"]["message"]


def test_catalog_active_model_follows_confirmed_switch(server, discovery):
    resp = server.handle_request({
        "jsonrpc": "2.0", "id": 2, "method": "session/command",
        "params": {"sessionId": "s1", "command": "/model", "args": "o3-mini"},
    })
    assert resp["result"]["isError"] is False
    res = query(server)["result"]
    assert res["activeModel"] == "o3-mini"
    assert server.orchestrator.model_switches == ["o3-mini"]


def test_discovery_error_redacts_url_api_key(server, monkeypatch):
    secret = "AIzaSyD-EXAMPLE-SECRET-KEY-1234567890"

    def fake_fetch(provider_type, api_key=None, base_url=None, timeout=3.0):
        if str(provider_type) == "local":
            return [("llama-3.3-70b", "llama-3.3-70b (Ollama)")]
        raise ProviderUnavailableError(
            "HTTP Error 400 for url: "
            f"https://generativelanguage.googleapis.com/v1beta/models?key={secret}"
        )

    monkeypatch.setattr(cp_mod, "fetch_available_models", fake_fetch)
    res = query(server)["result"]
    assert secret not in res["error"]
    assert "key=[REDACTED]" in res["error"]
    assert "llama-3.3-70b" in [m["id"] for m in res["models"]]


def test_total_failure_error_redacts_bearer_token(server, monkeypatch):
    secret = "sk-super-secret-bearer-token-abcdef"

    def fake_fetch(provider_type, api_key=None, base_url=None, timeout=3.0):
        raise ProviderUnavailableError(f"request failed: Authorization: Bearer {secret}")

    monkeypatch.setattr(cp_mod, "fetch_available_models", fake_fetch)
    resp = query(server)
    assert "result" not in resp
    msg = resp["error"]["message"]
    assert secret not in msg
    assert "[REDACTED]" in msg


def test_redact_sensitive_text_masks_common_secret_patterns():
    from harness_core.client.acp import _redact_sensitive_text

    out = _redact_sensitive_text(
        "https://x/models?key=ABC123&access_token=DEF456&normal=text"
    )
    assert "ABC123" not in out
    assert "DEF456" not in out
    assert "normal=text" in out
    assert "tok_abc" not in _redact_sensitive_text("Authorization: Bearer tok_abc")
    assert "shhh" not in _redact_sensitive_text("api_key=shhh")
    assert _redact_sensitive_text("No OpenAI API key found.") == "No OpenAI API key found."