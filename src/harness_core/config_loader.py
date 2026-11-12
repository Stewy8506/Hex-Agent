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
) -> dict[str, str]:
    """Load ~/.hex/credentials.env and config.yaml into os.environ.

    Returns:
        Dictionary of all loaded environment variables.
    """
    discovered: dict[str, str] = {}
    global_dir = config_dir or (Path.home() / ".hex")
    local_dir = (workspace_root or Path.cwd()) / ".hex"

    # Search paths in order: global first, local second (local overrides global)
    candidate_env_files = [
        global_dir / "credentials.env",
        local_dir / "credentials.env",
    ]

    for env_file in candidate_env_files:
        if env_file.is_file():
            parsed = parse_env_file(env_file)
            discovered.update(parsed)

    # Apply discovered variables to os.environ
    for k, v in discovered.items():
        if override or k not in os.environ:
            os.environ[k] = v

    # Also extract default provider / model from config.yaml if not set
    candidate_config_files = [
        global_dir / "config.yaml",
        local_dir / "config.yaml",
    ]
    cfg_props: dict[str, str] = {}
    for cfg_file in candidate_config_files:
        if cfg_file.is_file():
            cfg_data = parse_config_yaml(cfg_file)
            model_section = cfg_data.get("model", {})
            if isinstance(model_section, dict):
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

            web_search_section = cfg_data.get("web_search", {})
            if isinstance(web_search_section, dict):
                if web_search_section.get("provider"):
                    cfg_props["HEX_WEB_SEARCH_PROVIDER"] = str(web_search_section["provider"])

    for k, v in cfg_props.items():
        if override or k not in os.environ:
            os.environ[k] = v

    return discovered


def has_active_credentials(config_dir: Path | None = None) -> bool:
    """Check if any model provider or credential is valid and active."""
    # Ensure env is hydrated first
    load_hex_environment(config_dir=config_dir, workspace_root=config_dir if config_dir else None)

    # 1. Check direct environment keys
    if (
        os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("HEX_ANTHROPIC_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("HEX_OPENAI_API_KEY")
        or os.environ.get("GEMINI_API_KEY")
        or os.environ.get("HEX_GEMINI_API_KEY")
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

    # 4. Check if explicitly set to local-only mode
    if os.environ.get("HEX_CLOUD_PROVIDER") in ("local", "offline"):
        return True

    return False
