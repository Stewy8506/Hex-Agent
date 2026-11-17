"""Hermes-parity live model metadata, context window resolution, and models.dev registry."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

import httpx

from harness_core.models.credential_pool import (
    _endpoint_blackholed,
    _is_connect_timeout,
    _note_endpoint_blackholed,
    _note_if_connect_timeout,
)

logger = logging.getLogger(__name__)

MODELS_DEV_URL = "https://models.dev/api.json"
MODELS_DEV_CACHE_TTL = 86400  # 24 hours
MODELS_DEV_RETRY_DELAY = 300  # 5 minutes backoff on failure
DEFAULT_FALLBACK_CONTEXT = 256_000
CONTEXT_PROBE_TIERS = [256_000, 128_000, 64_000, 32_000, 16_000, 8_000]

# Canonical model family context windows (longest-prefix match, identical to Hermes)
DEFAULT_CONTEXT_LENGTHS: dict[str, int] = {
    # Anthropic
    "claude-fable-5": 1_000_000,
    "claude-fable": 1_000_000,
    "claude-mythos": 1_000_000,
    "claude-opus-5": 1_000_000,
    "claude-sonnet-5": 1_000_000,
    "claude-opus-4-8": 1_000_000,
    "claude-opus-4.8": 1_000_000,
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4.7": 1_000_000,
    "claude-opus-4-6": 1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-opus-4.6": 1_000_000,
    "claude-sonnet-4.6": 1_000_000,
    "claude-3-7": 200_000,
    "claude-3.7": 200_000,
    "claude-3-5": 200_000,
    "claude-3.5": 200_000,
    "claude-3": 200_000,
    "claude": 200_000,
    # OpenAI & Next-Gen
    "gpt-6-astra": 1_050_000,
    "gpt-6.1-sol": 1_050_000,
    "gpt-6.1-terra": 1_050_000,
    "gpt-6-sol": 1_050_000,
    "gpt-6-luna": 1_050_000,
    "gpt-5.6-luna": 1_050_000,
    "gpt-5.6-terra": 1_050_000,
    "gpt-5.6-sol": 1_050_000,
    "gpt-5.5": 1_050_000,
    "gpt-5.4-nano": 400_000,
    "gpt-5.4-mini": 400_000,
    "gpt-5.4": 1_050_000,
    "gpt-5.3-codex-spark": 128_000,
    "gpt-5.1-chat": 128_000,
    "gpt-5": 400_000,
    "gpt-4.1": 1_047_576,
    "gpt-4o": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4": 128_000,
    "o1-preview": 128_000,
    "o1-mini": 128_000,
    "o1": 200_000,
    "o3-mini": 200_000,
    "o3": 200_000,
    # Google Gemini & Gemma
    "gemini": 1_048_576,
    "gemma-4-31b-it": 256_000,
    "gemma-4-31b": 256_000,
    "gemma-4-26b": 256_000,
    "gemma-4": 256_000,
    "gemma4": 256_000,
    "gemma-3": 131_072,
    "gemma": 8_192,
    # DeepSeek
    "deepseek-v4-pro": 1_000_000,
    "deepseek-v4.1-flash": 1_000_000,
    "deepseek-v4-flash": 1_000_000,
    "deepseek-chat": 1_000_000,
    "deepseek-reasoner": 1_000_000,
    "deepseek-flash": 1_000_000,
    "deepseek-ai/DeepSeek-V3.2": 65_536,
    "deepseek-v3": 128_000,
    "deepseek": 128_000,
    # Meta / Llama
    "llama-3.3": 131_072,
    "llama-3.2": 131_072,
    "llama-3.1": 131_072,
    "llama-3": 131_072,
    "llama": 131_072,
    "muse-spark-1.3": 1_048_576,
    "muse-spark": 1_048_576,
    "inkling": 1_048_576,
    # Qwen
    "qwen3.8-max": 1_000_000,
    "qwen3.8-flash": 1_000_000,
    "qwen3.6-plus": 1_048_576,
    "qwen3.7-plus": 1_048_576,
    "qwen3-coder-plus": 1_000_000,
    "qwen3-coder": 262_144,
    "qwen3-max": 262_144,
    "qwen2.5-coder": 131_072,
    "qwen2.5": 131_072,
    "qwen": 131_072,
    # xAI Grok
    "grok-4-fast": 2_000_000,
    "grok-4.20": 2_000_000,
    "grok-4.6": 500_000,
    "grok-4.5": 500_000,
    "grok-4.3": 1_000_000,
    "grok-4": 256_000,
    "grok-3": 131_072,
    "grok-2": 131_072,
    "grok-2-vision": 8_192,
    "grok": 131_072,
    "grok-build-latest": 500_000,
    "grok-build": 256_000,
    "grok-code-fast": 256_000,
    "grok-composer": 200_000,
    # Kimi
    "kimi-k3": 1_048_576,
    "kimi": 262_144,
    # MiniMax
    "minimax-m3": 1_000_000,
    "minimax": 204_800,
    # GLM
    "glm-5.3": 1_310_720,
    "glm-5.3-flash": 1_310_720,
    "glm-5.2": 1_048_576,
    "glm-5.1": 204_800,
    "glm-5": 204_800,
    "glm-4.7": 204_800,
    "glm": 202_752,
    # Upstage Solar
    "solar-mini": 32_768,
    "solar-pro2": 65_536,
    "solar-pro3": 131_072,
    "solar-open2": 262_144,
    "solar-": 524_288,
    # Mistral
    "mistral-large": 128_000,
    "mistral-small": 128_000,
    "codestral": 256_000,
    "pixtral": 128_000,
    "mistral": 128_000,
    # NVIDIA
    "nemotron-3.5-lightning": 1_000_000,
    "nemotron-3.5-super-vl": 1_000_000,
    "nemotron-3-ultra": 131_072,
    "nemotron": 131_072,
    # Tencent Hunyuan & Others
    "hy4-preview": 1_048_576,
    "hy3-preview": 262_144,
    "hy3": 262_144,
    "ox-alpha": 1_048_576,
    "union-alpha": 262_144,
    "laguna-s-2.1": 262_144,
    "laguna-xs-2.1": 262_144,
    "trinity": 262_144,
    "elephant": 262_144,
}

_models_dev_cache: dict[str, Any] = {}
_models_dev_cache_time: float = 0.0
_models_dev_retry_after: float = 0.0
_models_dev_lock = threading.Lock()


def _get_hex_cache_dir() -> Path:
    cfg_dir = os.getenv("HEX_CONFIG_DIR")
    if cfg_dir:
        p = Path(cfg_dir)
    else:
        p = Path.home() / ".hex"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _get_disk_cache_path() -> Path:
    return _get_hex_cache_dir() / "models_dev_cache.json"


def _get_etag_path() -> Path:
    return _get_hex_cache_dir() / "models_dev_cache.etag"


def _load_disk_cache() -> dict[str, Any]:
    p = _get_disk_cache_path()
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    # Secondary fallback to Hermes cache if present
    hermes_cache = Path.home() / ".hermes" / "models_dev_cache.json"
    if hermes_cache.is_file():
        try:
            return json.loads(hermes_cache.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_disk_cache(data: dict[str, Any], etag: str = "") -> None:
    p = _get_disk_cache_path()
    try:
        p.write_text(json.dumps(data), encoding="utf-8")
        if etag:
            _get_etag_path().write_text(etag.strip(), encoding="utf-8")
    except Exception as exc:
        logger.debug("Failed to write models_dev_cache.json: %s", exc)


def fetch_models_dev(*, allow_network: bool = True) -> dict[str, Any]:
    """Retrieve models.dev registry with disk cache, memory cache, and ETag conditional fetch."""
    global _models_dev_cache, _models_dev_cache_time, _models_dev_retry_after

    now = time.time()
    # 1. Fast in-memory cache check
    if _models_dev_cache and (now - _models_dev_cache_time) < MODELS_DEV_CACHE_TTL:
        return _models_dev_cache

    with _models_dev_lock:
        if _models_dev_cache and (now - _models_dev_cache_time) < MODELS_DEV_CACHE_TTL:
            return _models_dev_cache

        # 2. Check disk cache
        if not _models_dev_cache:
            disk = _load_disk_cache()
            if disk:
                _models_dev_cache = disk
                try:
                    _models_dev_cache_time = _get_disk_cache_path().stat().st_mtime
                except Exception:
                    _models_dev_cache_time = now

                # If fresh enough, return
                if (now - _models_dev_cache_time) < MODELS_DEV_CACHE_TTL:
                    return _models_dev_cache

        # 3. If network is disabled or in retry backoff, return existing cache
        if not allow_network or now < _models_dev_retry_after:
            return _models_dev_cache

        # 4. Attempt conditional network fetch
        etag = ""
        etag_path = _get_etag_path()
        if etag_path.is_file():
            try:
                etag = etag_path.read_text(encoding="utf-8").strip()
            except Exception:
                pass

        headers = {"User-Agent": "hex-agent/1.0"}
        if etag:
            headers["If-None-Match"] = etag

        req = urllib.request.Request(MODELS_DEV_URL, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                if resp.status == 200:
                    raw = resp.read().decode("utf-8")
                    data = json.loads(raw)
                    new_etag = resp.headers.get("ETag", "")
                    _models_dev_cache = data
                    _models_dev_cache_time = now
                    _save_disk_cache(data, new_etag)
                    return _models_dev_cache
        except urllib.error.HTTPError as he:
            if he.code == 304:
                # Not Modified
                _models_dev_cache_time = now
                return _models_dev_cache
            _models_dev_retry_after = now + MODELS_DEV_RETRY_DELAY
        except Exception as exc:
            logger.debug("models.dev network fetch skipped (%s)", exc)
            _models_dev_retry_after = now + MODELS_DEV_RETRY_DELAY

        return _models_dev_cache


def lookup_models_dev_context(provider: str, model: str, *, allow_network: bool = False) -> int | None:
    """Find context window for provider + model from models.dev catalog."""
    registry = fetch_models_dev(allow_network=allow_network)
    if not isinstance(registry, dict):
        return None

    prov_key = provider.lower().strip()
    model_key = model.lower().strip()

    # Normalize common provider synonyms
    provider_aliases = {
        "google": ["google", "gemini", "google-gemini", "google-ai-studio"],
        "gemini": ["google", "gemini", "google-gemini"],
        "anthropic": ["anthropic", "claude"],
        "openai": ["openai", "chatgpt"],
        "openrouter": ["openrouter"],
        "groq": ["groq"],
        "deepseek": ["deepseek"],
        "mistral": ["mistral", "mistralai"],
    }
    candidate_provs = provider_aliases.get(prov_key, [prov_key])

    for p in candidate_provs:
        p_data = registry.get(p)
        if isinstance(p_data, dict):
            models_dict = p_data.get("models")
            if isinstance(models_dict, dict):
                # Exact match
                if model_key in models_dict:
                    limit = models_dict[model_key].get("limit", {})
                    ctx = limit.get("context")
                    if isinstance(ctx, int) and ctx > 0:
                        return ctx
                # Prefix / suffix search
                for m_id, m_entry in models_dict.items():
                    if m_id.lower() == model_key or model_key.endswith(m_id.lower()):
                        limit = m_entry.get("limit", {}) if isinstance(m_entry, dict) else {}
                        ctx = limit.get("context")
                        if isinstance(ctx, int) and ctx > 0:
                            return ctx
    return None


def _normalize_model_version(model: str) -> str:
    """Dots -> dashes so Nous ids (claude-opus-4-6) compare with OpenRouter's (claude-opus-4.6)."""
    return model.replace(".", "-")


def _catalog_key_matches(key: str, model_lower: str) -> bool:
    """Substring match with version separators normalised on both sides (Hermes parity)."""
    return key in model_lower or _normalize_model_version(key) in _normalize_model_version(model_lower)


def _longest_key_match(catalog: dict[str, int], model: str) -> tuple[str, int] | None:
    """First (key, value) whose key is a substring of model_lower, longest key first (Hermes parity)."""
    model_lower = model.lower()
    for key, value in sorted(catalog.items(), key=lambda x: len(x[0]), reverse=True):
        if _catalog_key_matches(key, model_lower):
            return key, value
    return None


# --- Local Socket and Server Introspection Probes ---

_endpoint_model_metadata_cache: dict[str, tuple[dict[str, Any], float]] = {}
_ENDPOINT_PROBE_CACHE_TTL: float = 300.0
_probe_cache_lock = threading.Lock()


def _clear_endpoint_model_metadata_cache() -> None:
    """Clear in-memory probe cache (primarily for tests)."""
    with _probe_cache_lock:
        _endpoint_model_metadata_cache.clear()


def _normalize_server_url(base_url: str) -> str:
    """Normalize base_url to scheme://host[:port], stripping trailing paths/slashes."""
    raw = (base_url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"http://{raw}"
    raw = re.sub(r"^(https?://)localhost(?=[:/]|$)", r"\g<1>127.0.0.1", raw, count=1)
    parsed = urllib.parse.urlparse(raw)
    port_part = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port_part}"


def _ollama_show_context(data: dict[str, Any], gguf_first: bool = False) -> Optional[int]:
    """Context length from Ollama /api/show response.

    parameters.num_ctx is runtime context (Modelfile); model_info.*.context_length is GGUF max.
    """
    def _num_ctx():
        params = data.get("parameters") or ""
        if isinstance(params, str):
            for line in params.split("\n"):
                if "num_ctx" in line:
                    parts = line.strip().split()
                    if len(parts) >= 2:
                        try:
                            yield int(parts[-1])
                        except ValueError:
                            continue
        elif isinstance(params, dict):
            if "num_ctx" in params:
                try:
                    yield int(params["num_ctx"])
                except (ValueError, TypeError):
                    pass

    def _gguf():
        model_info = data.get("model_info") or {}
        if isinstance(model_info, dict):
            for k, v in model_info.items():
                if "context_length" in k:
                    try:
                        yield int(v)
                    except (ValueError, TypeError):
                        continue

    readers = (_gguf, _num_ctx) if gguf_first else (_num_ctx, _gguf)
    for reader in readers:
        for val in reader():
            if val and val > 0:
                return val
    return None


def _probe_ollama(client: httpx.Client, server_url: str, model: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Probe Ollama server for models and context window length."""
    ctx_length: Optional[int] = None
    show_data: Optional[dict[str, Any]] = None

    # Fast path: if model is provided, check /api/show directly
    if model:
        try:
            r_show = client.post(f"{server_url}/api/show", json={"name": model})
            if r_show.status_code == 200:
                show_data = r_show.json()
                if isinstance(show_data, dict):
                    ctx_length = _ollama_show_context(show_data)
                    return {
                        "server_type": "ollama",
                        "model": model,
                        "context_length": ctx_length,
                        "models": [model],
                        "raw": show_data,
                    }
        except Exception as exc:
            _note_if_connect_timeout(exc, server_url)
            if _is_connect_timeout(exc):
                raise exc

    # Discovery path: probe /api/tags and /api/ps
    available_models: list[str] = []
    running_models: list[str] = []

    try:
        r_tags = client.get(f"{server_url}/api/tags")
        if r_tags.status_code == 200:
            payload = r_tags.json()
            if isinstance(payload, dict) and "models" in payload:
                available_models = [
                    m.get("name") or m.get("model")
                    for m in payload["models"]
                    if isinstance(m, dict) and (m.get("name") or m.get("model"))
                ]
    except Exception as exc:
        _note_if_connect_timeout(exc, server_url)
        if _is_connect_timeout(exc):
            raise exc

    try:
        r_ps = client.get(f"{server_url}/api/ps")
        if r_ps.status_code == 200:
            payload = r_ps.json()
            if isinstance(payload, dict) and "models" in payload:
                running_models = [
                    m.get("name") or m.get("model")
                    for m in payload["models"]
                    if isinstance(m, dict) and (m.get("name") or m.get("model"))
                ]
    except Exception as exc:
        _note_if_connect_timeout(exc, server_url)
        if _is_connect_timeout(exc):
            raise exc

    target_model = model
    if not target_model:
        if running_models:
            target_model = running_models[0]
        elif available_models:
            target_model = available_models[0]

    if target_model and show_data is None:
        try:
            r_show = client.post(f"{server_url}/api/show", json={"name": target_model})
            if r_show.status_code == 200:
                show_data = r_show.json()
                if isinstance(show_data, dict):
                    ctx_length = _ollama_show_context(show_data)
        except Exception as exc:
            _note_if_connect_timeout(exc, server_url)
            if _is_connect_timeout(exc):
                raise exc

    if show_data is not None or available_models or running_models:
        return {
            "server_type": "ollama",
            "model": target_model,
            "context_length": ctx_length,
            "models": running_models or available_models,
            "raw": show_data or {},
        }
    return None


def _probe_lmstudio(client: httpx.Client, server_url: str, model: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Probe LM Studio /api/v0/models and /api/v1/models."""
    for path in ("/api/v0/models", "/api/v1/models", "/v1/models"):
        try:
            resp = client.get(f"{server_url}{path}")
            if resp.status_code == 200:
                data = resp.json()
                models_list = data.get("data") or data.get("models") or []
                if not isinstance(models_list, list):
                    continue
                # If models indicate vLLM (owned_by: vllm or max_model_len), defer to vLLM probe
                if any(
                    isinstance(m, dict) and (m.get("owned_by") == "vllm" or "max_model_len" in m)
                    for m in models_list
                ):
                    continue
                # If path was /v1/models, verify it's not generic OpenAI
                if path == "/v1/models" and not any(
                    "lmstudio" in str(m.get("publisher", "")).lower()
                    or "loaded_context_length" in m
                    or "max_context_length" in m
                    for m in models_list
                    if isinstance(m, dict)
                ):
                    continue

                matched = None
                if model:
                    for m in models_list:
                        if isinstance(m, dict):
                            m_id = m.get("id") or m.get("key") or ""
                            if m_id == model or model in m_id or m_id in model:
                                matched = m
                                break
                if not matched and models_list:
                    for m in models_list:
                        if isinstance(m, dict) and (m.get("state") == "loaded" or m.get("loaded_instances")):
                            matched = m
                            break
                    if not matched and isinstance(models_list[0], dict):
                        matched = models_list[0]

                ctx: Optional[int] = None
                if matched:
                    for key in ("loaded_context_length", "context_length", "max_context_length"):
                        val = matched.get(key)
                        if isinstance(val, (int, float)) and val > 0:
                            ctx = int(val)
                            break
                    if ctx is None:
                        instances = matched.get("loaded_instances", [])
                        if isinstance(instances, list) and instances:
                            cfg = instances[0].get("config", {}) if isinstance(instances[0], dict) else {}
                            val = cfg.get("context_length") or cfg.get("max_context_length")
                            if isinstance(val, (int, float)) and val > 0:
                                ctx = int(val)

                return {
                    "server_type": "lm-studio",
                    "model": matched.get("id") or matched.get("key") if matched else model,
                    "context_length": ctx,
                    "models": [
                        m.get("id") or m.get("key")
                        for m in models_list
                        if isinstance(m, dict) and (m.get("id") or m.get("key"))
                    ],
                    "raw": data,
                }
        except Exception as exc:
            _note_if_connect_timeout(exc, server_url)
            if _is_connect_timeout(exc):
                raise exc
    return None


def _probe_llamacpp(client: httpx.Client, server_url: str, model: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Probe llama.cpp /props and /v1/props for default_generation_settings.n_ctx."""
    for path in ("/props", "/v1/props"):
        try:
            resp = client.get(f"{server_url}{path}")
            if resp.status_code == 200:
                data = resp.json()
                if not isinstance(data, dict):
                    continue
                gen_settings = data.get("default_generation_settings") or {}
                n_ctx = gen_settings.get("n_ctx") or data.get("n_ctx")
                ctx: Optional[int] = int(n_ctx) if isinstance(n_ctx, (int, float)) and n_ctx > 0 else None
                model_name = data.get("model_alias") or data.get("model") or model or "llama.cpp"
                return {
                    "server_type": "llamacpp",
                    "model": model_name,
                    "context_length": ctx,
                    "models": [model_name] if model_name else [],
                    "raw": data,
                }
        except Exception as exc:
            _note_if_connect_timeout(exc, server_url)
            if _is_connect_timeout(exc):
                raise exc
    return None


def _probe_vllm(client: httpx.Client, server_url: str, model: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Probe vLLM /v1/models and /version for max_model_len."""
    try:
        resp = client.get(f"{server_url}/v1/models")
        if resp.status_code == 200:
            data = resp.json()
            models_list = data.get("data") if isinstance(data, dict) else []
            if isinstance(models_list, list) and models_list:
                matched = None
                if model:
                    for m in models_list:
                        if isinstance(m, dict) and (m.get("id") == model or model in m.get("id", "")):
                            matched = m
                            break
                if not matched and models_list and isinstance(models_list[0], dict):
                    matched = models_list[0]

                if matched and ("max_model_len" in matched or matched.get("owned_by") == "vllm"):
                    max_len = matched.get("max_model_len")
                    ctx = int(max_len) if isinstance(max_len, (int, float)) and max_len > 0 else None
                    return {
                        "server_type": "vllm",
                        "model": matched.get("id"),
                        "context_length": ctx,
                        "max_model_len": ctx,
                        "models": [m.get("id") for m in models_list if isinstance(m, dict) and m.get("id")],
                        "raw": data,
                    }
    except Exception as exc:
        _note_if_connect_timeout(exc, server_url)
        if _is_connect_timeout(exc):
            raise exc
    return None


def probe_local_server(base_url: str, model: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Probe live local server for model context length and metadata.

    Supports:
    - Ollama: /api/show (Modelfile num_ctx or GGUF context_length), /api/tags, /api/ps
    - LM Studio: /api/v0/models, /api/v1/models
    - llama.cpp: /props, /v1/props (default_generation_settings.n_ctx)
    - vLLM: /v1/models (max_model_len)

    Returns None immediately if the endpoint is blackholed or offline.
    """
    server_url = _normalize_server_url(base_url)
    if not server_url:
        return None

    # Blackhole sentry check
    if _endpoint_blackholed(server_url):
        logger.debug("Skipping probe for blackholed endpoint %s", server_url)
        return None

    # Cache lookup
    cache_key = f"{server_url}:{model or ''}"
    with _probe_cache_lock:
        hit = _endpoint_model_metadata_cache.get(cache_key)
        if hit is not None:
            val, ts = hit
            if (time.monotonic() - ts) < _ENDPOINT_PROBE_CACHE_TTL:
                return val

    # Execute probes with 1.0s connect/read timeout
    try:
        with httpx.Client(timeout=1.0) as client:
            probes = [_probe_ollama, _probe_lmstudio, _probe_llamacpp, _probe_vllm]
            for probe_fn in probes:
                try:
                    result = probe_fn(client, server_url, model=model)
                    if result is not None:
                        with _probe_cache_lock:
                            _endpoint_model_metadata_cache[cache_key] = (result, time.monotonic())
                        return result
                except Exception as exc:
                    _note_if_connect_timeout(exc, server_url)
                    if _is_connect_timeout(exc):
                        return None
    except Exception as exc:
        _note_if_connect_timeout(exc, server_url)
        return None

    return None


def get_model_context_length(
    model: str,
    provider: str = "",
    base_url: str = "",
    config_context_length: int | None = None,
    allow_network: bool = False,
) -> int:
    """Resolve context window length for a model (Hermes parity).

    Hierarchy:
    0. Explicit user config override (config_context_length or model_overrides)
    1. Live local server socket introspection (if base_url provided)
    2. models.dev catalog lookup
    3. Longest-prefix match in canonical model catalog
    4. Safe default fallback (128,000)
    """
    if isinstance(config_context_length, int) and config_context_length > 0:
        return config_context_length

    # 1. Live socket probe if base_url is provided
    if base_url:
        probe = probe_local_server(base_url, model=model)
        if probe and probe.get("context_length"):
            ctx = probe["context_length"]
            if isinstance(ctx, int) and ctx > 0:
                return ctx

    # Clean model identifier (e.g. "openrouter/google/gemini-2.5-flash" -> "gemini-2.5-flash")
    clean_model = model.strip()
    if "/" in clean_model:
        parts = clean_model.split("/")
        # If openrouter model like "nvidia/nemotron-3-ultra-550b", keep the suffix
        if parts[0].lower() in ("openrouter", "openai", "google", "anthropic", "meta-llama"):
            clean_model = "/".join(parts[1:])

    # 2. Catalog lookup via models.dev
    mdev_ctx = lookup_models_dev_context(provider, clean_model, allow_network=allow_network)
    if mdev_ctx is not None and mdev_ctx > 0:
        return mdev_ctx

    # 3. Longest prefix match in DEFAULT_CONTEXT_LENGTHS
    hit = _longest_key_match(DEFAULT_CONTEXT_LENGTHS, clean_model)
    if hit:
        return hit[1]

    # Also try matching against the full raw model string
    if clean_model != model:
        raw_hit = _longest_key_match(DEFAULT_CONTEXT_LENGTHS, model)
        if raw_hit:
            return raw_hit[1]

    # 4. Default fallback
    return DEFAULT_FALLBACK_CONTEXT


def format_context_tokens(tokens: int) -> str:
    """Format token count cleanly: e.g. 1048576 -> '1.0M', 256000 -> '256k', 200000 -> '200k'."""
    if tokens >= 1_000_000:
        m = tokens / 1_000_000.0
        # If rounded tenths is .0, display as 1.0M or 2.0M
        return f"{m:.1f}M" if (m * 10) % 10 != 0 else f"{int(m)}M"
    return f"{tokens // 1000}k"
