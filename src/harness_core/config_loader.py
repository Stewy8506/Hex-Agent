"""Universal Environment & Configuration Loader for HEX.

Discovers, parses, and injects credentials from:
1. Local repository workspace (`./.hex/credentials.env`, `./.hex/config.yaml`)
2. Global operator home directory (`~/.hex/credentials.env`, `~/.hex/config.yaml`)

Ensures all model tokens, API keys, web search settings, and messaging configs
are populated into `os.environ` before any orchestrator or CLI command executes.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

_managed_environment: dict[str, str] = {}

# Model selection has a different scope from credentials.  A project may keep
# its own API credentials, but it must explicitly opt in before its selection
# can replace the operator's global model choice.
_MODEL_SELECTION_ENV_KEYS = {
    "HEX_MODEL_PROFILE",
    "HEX_CLOUD_PROVIDER",
    "HEX_CLOUD_MODEL_ID",
    "HEX_LOCAL_MODEL_ID",
    "HEX_COMPRESSOR_MODE",
}

try:
    import yaml  # type: ignore
except ImportError:
    yaml = None  # type: ignore


def parse_env_file(file_path: Path) -> dict[str, str]:
    """Parse a .env formatted file into key-value pairs."""
    env_vars: dict[str, str] = {}
    if not file_path.is_file():
        return env_vars

    try:
        content = file_path.read_text(encoding="utf-8")
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                # Strip wrapping quotes if present
                if (val.startswith('"') and val.endswith('"')) or (
                    val.startswith("'") and val.endswith("'")
                ):
                    val = val[1:-1]
                if key:
                    env_vars[key] = val
    except Exception:
        pass
    return env_vars


def parse_config_yaml(file_path: Path) -> dict[str, Any]:
    """Parse a config.yaml file safely."""
    if not file_path.is_file():
        return {}
    try:
        content = file_path.read_text(encoding="utf-8")
        if yaml is not None:
            data = yaml.safe_load(content)
            return data if isinstance(data, dict) else {}
        # Robust indentation-aware nested dict parser for YAML without pyyaml dependency
        root: dict[str, Any] = {}
        stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(line) - len(line.lstrip(" "))
            if ":" not in stripped:
                continue
            key, val = stripped.split(":", 1)
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            while len(stack) > 1 and stack[-1][0] >= indent:
                stack.pop()
            parent = stack[-1][1]
            if not val:
                sub: dict[str, Any] = {}
                parent[key] = sub
                stack.append((indent, sub))
            else:
                parent[key] = val
        return root
    except Exception:
        return {}


def load_hex_environment(
    config_dir: Path | None = None,
    workspace_root: Path | None = None,
    *,
    override: bool = False,
    reset_managed: bool = False,
) -> dict[str, str]:
    """Load ~/.hex/credentials.env and config.yaml into os.environ.

    Returns:
        Dictionary of all loaded environment variables.
    """
    global _managed_environment
    if reset_managed:
        for key, value in _managed_environment.items():
            if os.environ.get(key) == value:
                os.environ.pop(key, None)
        _managed_environment = {}

    discovered: dict[str, str] = {}
    global_dir = config_dir or (Path.home() / ".hex")
    local_dir = (workspace_root or Path.cwd()) / ".hex"

    def _project_model_override_enabled(section: dict[str, Any]) -> bool:
        return section.get("project_override") is True or str(section.get("project_override", "")).lower() == "true"

    # Credentials are kept separate from model selection.  Older installs wrote
    # selected-model fields into credentials.env too, creating two competing
    # sources of truth (and letting an old value win at startup).
    candidate_env_files = [
        global_dir / "credentials.env",
        local_dir / "credentials.env",
    ]

    for env_file in candidate_env_files:
        if env_file.is_file():
            parsed = parse_env_file(env_file)
            # Preserve real credentials, but always ignore legacy selection
            # fields here. config.yaml is the single source of truth for model
            # choice; a project config needs project_override: true to take
            # precedence over the global config.
            parsed = {k: v for k, v in parsed.items() if k not in _MODEL_SELECTION_ENV_KEYS}
            discovered.update(parsed)

    # Apply discovered variables to os.environ
    for k, v in discovered.items():
        if override or k not in os.environ:
            os.environ[k] = v

    # Also extract default provider / model from config.yaml if not set in environment or env files
    candidate_config_files = [
        global_dir / "config.yaml",
        local_dir / "config.yaml",
    ]
    cfg_props: dict[str, str] = {}
    for cfg_file in candidate_config_files:
        if cfg_file.is_file():
            cfg_data = parse_config_yaml(cfg_file)
            model_section = cfg_data.get("model") or cfg_data.get("models") or {}
            if isinstance(model_section, dict):
                is_project_file = cfg_file == local_dir / "config.yaml" and local_dir != global_dir
                # A project inherits global model settings unless it deliberately
                # opts in. This prevents generated/stale project configs from
                # silently defeating the operator's global model choice.
                use_model_section = not is_project_file or _project_model_override_enabled(model_section)
                if use_model_section:
                    if model_section.get("profile"):
                        cfg_props["HEX_MODEL_PROFILE"] = str(model_section["profile"])
                    if model_section.get("cloud_provider"):
                        cfg_props["HEX_CLOUD_PROVIDER"] = str(model_section["cloud_provider"])
                    if model_section.get("cloud_model_id"):
                        cfg_props["HEX_CLOUD_MODEL_ID"] = str(model_section["cloud_model_id"])
                    if model_section.get("local_model_id"):
                        cfg_props["HEX_LOCAL_MODEL_ID"] = str(model_section["local_model_id"])
                    if model_section.get("compressor_mode"):
                        cfg_props["HEX_COMPRESSOR_MODE"] = str(model_section["compressor_mode"])
                    if model_section.get("cloud_base_url"):
                        cfg_props["HEX_CLOUD_BASE_URL"] = str(model_section["cloud_base_url"])
                    cfg_props["HEX_MODEL_SOURCE"] = "project override" if is_project_file else "global default"

            web_search_section = cfg_data.get("web_search", {})
            if isinstance(web_search_section, dict):
                if web_search_section.get("provider"):
                    cfg_props["HEX_WEB_SEARCH_PROVIDER"] = str(web_search_section["provider"])

            learning_section = cfg_data.get("learning", {})
            if isinstance(learning_section, dict):
                if "auto_reflect" in learning_section:
                    raw_ar = learning_section["auto_reflect"]
                    if raw_ar is False or str(raw_ar).strip().lower() in ("off", "false", "no", "disabled", "0"):
                        cfg_props["HEX_AUTO_REFLECT"] = "off"
                    else:
                        cfg_props["HEX_AUTO_REFLECT"] = str(raw_ar).strip().lower()

    for k, v in cfg_props.items():
        if override or k not in os.environ:
            os.environ[k] = v

    for k, v in {**discovered, **cfg_props}.items():
        if os.environ.get(k) == v:
            _managed_environment[k] = v

    return discovered


def has_active_credentials(config_dir: Path | None = None) -> bool:
    """Check if any model provider or credential is valid and active."""
    # Ensure env is hydrated first
    load_hex_environment(config_dir=config_dir, workspace_root=config_dir if config_dir else None)

    # 1. Check all provider environment keys
    try:
        from .providers.registry import get_all_provider_env_vars
        for env_var in get_all_provider_env_vars():
            if os.environ.get(env_var):
                return True
    except Exception:
        # Fallback to standard core keys
        if (
            os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("HEX_ANTHROPIC_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or os.environ.get("HEX_OPENAI_API_KEY")
            or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("HEX_GEMINI_API_KEY")
            or os.environ.get("OPENROUTER_API_KEY")
            or os.environ.get("GROQ_API_KEY")
            or os.environ.get("DEEPSEEK_API_KEY")
        ):
            return True

    # 2. Check Antigravity OAuth tokens
    if os.environ.get("HEX_ANTIGRAVITY_TOKEN") or os.environ.get("ANTIGRAVITY_OAUTH_TOKEN"):
        return True

    # 3. Check Antigravity OAuth cached file
    cfg_dir = config_dir or (Path.home() / ".hex")
    auth_file = cfg_dir / "antigravity_auth.json"
    if auth_file.is_file():
        try:
            import json

            data = json.loads(auth_file.read_text(encoding="utf-8"))
            if data.get("access_token") or data.get("refresh_token"):
                return True
        except Exception:
            pass

    # 4. Check if explicitly set to local/offline mode
    cloud_prov = (os.environ.get("HEX_CLOUD_PROVIDER") or "").strip().lower()
    if cloud_prov in ("local", "offline", "ollama", "lmstudio"):
        return True

    return False


def get_auto_reflect_policy(workspace_root: Path | str | None = None) -> str:
    """Get the active auto-reflection policy ('cloud_only' | 'idle' | 'off').

    Priority:
    1. Explicit environment variable HEX_AUTO_REFLECT (not merely hydrated from another workspace)
    2. Local workspace .hex/config.yaml under learning.auto_reflect
    3. Global ~/.hex/config.yaml under learning.auto_reflect
    4. Managed environment fallback
    5. Default: 'cloud_only'
    """
    def _normalize_policy(raw_val: Any) -> str:
        if raw_val is False or str(raw_val).strip().lower() in ("off", "false", "no", "disabled", "0"):
            return "off"
        val = str(raw_val).strip().lower()
        if val in ("cloud_only", "cloud-only", "cloud"):
            return "cloud_only"
        if val in ("idle", "on_idle"):
            return "idle"
        return val

    env_val = (os.environ.get("HEX_AUTO_REFLECT") or "").strip()
    is_explicit_env = bool(env_val) and (
        "HEX_AUTO_REFLECT" not in _managed_environment
        or env_val != _managed_environment.get("HEX_AUTO_REFLECT")
    )
    if is_explicit_env:
        return _normalize_policy(env_val)

    # Candidate config files
    ws = Path(workspace_root).resolve() if workspace_root else Path.cwd().resolve()
    candidates = [
        ws / ".hex" / "config.yaml",
        Path.home() / ".hex" / "config.yaml",
    ]
    for cfg_path in candidates:
        if cfg_path.is_file():
            data = parse_config_yaml(cfg_path)
            learning = data.get("learning", {})
            if isinstance(learning, dict) and "auto_reflect" in learning:
                return _normalize_policy(learning["auto_reflect"])

    if env_val:
        return _normalize_policy(env_val)

    return "cloud_only"


def should_execute_background_reflection(
    policy: str,
    router: Any | None = None,
    is_idle: bool = False,
) -> tuple[bool, str]:
    """Determine whether autonomous background reflection may proceed.

    Gated by hardware profiling and policy to protect unified memory on local
    Apple Silicon (M4/M-series) or local inference backends (Ollama, local vLLM).
    """
    norm_policy = (policy or "cloud_only").strip().lower()
    if norm_policy in ("off", "false", "no", "disabled", "0"):
        return False, "Autonomous reflection disabled (policy=off)"

    if norm_policy == "idle":
        if is_idle:
            return True, "Executing during idle"
        return False, "Deferred until idle"

    # Policy is "cloud_only" (or default fallback)
    if router is not None:
        # Check active provider name/type
        provider_name = (
            getattr(router, "active_provider", None)
            or getattr(router, "provider", None)
            or getattr(router, "provider_name", None)
            or getattr(router, "cloud_provider_type", None)
        )
        if provider_name and isinstance(provider_name, str):
            p_clean = provider_name.strip().lower()
            if p_clean in ("mlx", "ollama", "local", "vllm", "cpu", "metal", "lmstudio", "offline"):
                return False, "Suppressed background reflection on local hardware to prevent memory starvation"
            if p_clean in ("cloud", "anthropic", "openai", "gemini", "antigravity", "openrouter", "groq", "deepseek"):
                return True, "Cloud-routed reasoner active"

        # Check explicit is_cloud boolean
        is_cloud_val = getattr(router, "is_cloud", None)
        if isinstance(is_cloud_val, bool):
            if is_cloud_val:
                return True, "Cloud-routed reasoner active"
            return False, "Suppressed background reflection on local hardware to prevent memory starvation"

        # Check cloud_provider attribute on TwoTierModelRouter
        cp = getattr(router, "cloud_provider", None)
        if cp is None:
            return False, "Suppressed background reflection on local hardware to prevent memory starvation"

        # Check availability
        if hasattr(cp, "is_available") and callable(cp.is_available):
            try:
                if not cp.is_available():
                    return False, "Suppressed background reflection on local hardware to prevent memory starvation"
            except Exception:
                return False, "Suppressed background reflection on local hardware to prevent memory starvation"

        cp_type = (
            getattr(cp, "provider_type", None)
            or getattr(cp, "provider_name", None)
            or ""
        )
        if cp_type and isinstance(cp_type, str) and cp_type.strip().lower() in ("mlx", "ollama", "local", "vllm", "offline"):
            return False, "Suppressed background reflection on local hardware to prevent memory starvation"

        return True, "Cloud-routed reasoner active"

    # Fallback when router is None or didn't supply provider
    import platform

    cloud_prov = (os.environ.get("HEX_CLOUD_PROVIDER") or "").strip().lower()
    if cloud_prov in ("local", "offline", "ollama", "lmstudio", "mlx"):
        return False, "Suppressed background reflection on local hardware to prevent memory starvation"

    if cloud_prov in ("anthropic", "openai", "gemini", "antigravity", "openrouter", "groq", "deepseek"):
        return True, "Cloud-routed reasoner active"

    # Check if active credentials exist
    try:
        if has_active_credentials():
            return True, "Cloud-routed reasoner active"
    except Exception:
        pass

    # Local machine detection: Darwin ARM64 (Apple Silicon) or offline
    is_darwin_arm64 = platform.system() == "Darwin" and platform.machine() in ("arm64", "aarch64")
    if is_darwin_arm64:
        return False, "Suppressed background reflection on local hardware to prevent memory starvation"

    return False, "Suppressed background reflection on local hardware to prevent memory starvation"


def load_retention_config(workspace_root: Path | str | None = None) -> dict[str, Any]:
    """Load storage retention and garbage collection settings.

    Priority:
    1. Local workspace .hex/config.toml or .hex/config.yaml under [retention]
    2. Global ~/.hex/config.toml or ~/.hex/config.yaml under [retention]
    3. Defaults: workspace_ttl='24h', cas_ttl='7d', auto_vacuum=True
    """
    defaults = {
        "workspace_ttl": "24h",
        "cas_ttl": "7d",
        "auto_vacuum": True,
    }

    ws = Path(workspace_root).resolve() if workspace_root else Path.cwd().resolve()
    candidates = [
        ws / ".hex" / "config.toml",
        ws / ".hex" / "config.yaml",
        Path.home() / ".hex" / "config.toml",
        Path.home() / ".hex" / "config.yaml",
    ]

    for p in candidates:
        if not p.is_file():
            continue
        try:
            if p.suffix == ".toml":
                try:
                    import tomllib  # type: ignore[import-not-found]
                    content = p.read_bytes()
                    data = tomllib.loads(content.decode("utf-8"))
                    ret = data.get("retention")
                    if isinstance(ret, dict):
                        return {**defaults, **ret}
                except Exception:
                    pass
            elif p.suffix in (".yaml", ".yml"):
                data = parse_config_yaml(p)
                ret = data.get("retention")
                if isinstance(ret, dict):
                    return {**defaults, **ret}
        except Exception:
            continue

    return defaults


