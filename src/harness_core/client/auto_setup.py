"""Autonomous Agent Self-Setup & Environment Auto-Discovery Engine.

Inspects the host machine (hardware, developer tools, compilers, local runtimes,
Antigravity IDE OAuth sessions, and environment variables) to synthesize and
commit an optimal, production-ready HEX configuration without blocking the operator.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness_core.config_loader import parse_config_yaml, parse_env_file
from harness_core.client.onboard import HardwareProfile
from harness_core.providers.antigravity_provider import AntigravityCredential, AntigravityOAuthManager


@dataclass
class EnvironmentSnapshot:
    """Discovered facts describing the host environment, dev tools, and credentials."""

    # Hardware & OS
    os_name: str = "Generic Host"
    arch: str = "unknown"
    machine_model: str = "Generic Host"
    ram_gb: float = 8.0
    is_apple_silicon: bool = False
    has_metal: bool = False
    has_cuda: bool = False
    cow_backend: str = "portable"
    sandbox_backend: str = "restricted_subprocess"

    # Developer Tools & Compilers
    has_git: bool = False
    has_docker: bool = False
    has_python: bool = False
    has_node: bool = False
    has_rustc: bool = False
    has_clang: bool = False
    has_brew: bool = False
    has_winget: bool = False
    has_flatpak: bool = False
    installed_tools: list[str] = field(default_factory=list)

    # IDE Sessions & Credentials
    has_antigravity_ide: bool = False
    antigravity_token: str = ""
    antigravity_source: str = ""
    antigravity_project_id: str | None = None

    # Cloud & Local API Keys
    anthropic_key: str = ""
    openai_key: str = ""
    gemini_key: str = ""
    openrouter_key: str = ""
    deepseek_key: str = ""
    groq_key: str = ""
    xai_key: str = ""
    tavily_key: str = ""
    brave_key: str = ""

    # Local Runtime Daemons
    ollama_running: bool = False
    ollama_models: list[str] = field(default_factory=list)
    lmstudio_running: bool = False

    # Messaging Gateway Credentials
    telegram_bot_token: str = ""
    telegram_allowed_users: str = ""
    discord_bot_token: str = ""
    discord_webhook_url: str = ""

    # Existing Configuration
    is_already_configured: bool = False
    existing_provider: str = ""
    existing_model_id: str = ""
    existing_reasoning_effort: str = ""


def _probe_tool(name: str) -> bool:
    """Return True if an executable binary is discovered in PATH."""
    return shutil.which(name) is not None


def _probe_http_ok(url: str, timeout: float = 0.5) -> bool:
    """Probe whether a local daemon endpoint is reachable."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "HEX-AutoSetup/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status in (200, 204)
    except Exception:
        return False


def _probe_ollama_tags(url: str = "http://localhost:11434/api/tags", timeout: float = 0.5) -> list[str]:
    """Retrieve available model tags from local Ollama if active."""
    try:
        import json
        req = urllib.request.Request(url, headers={"User-Agent": "HEX-AutoSetup/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                models = data.get("models", [])
                return [m.get("name", "") for m in models if isinstance(m, dict) and m.get("name")]
    except Exception:
        pass
    return []


def detect_environment(config_dir: Path | None = None) -> EnvironmentSnapshot:
    """Inspect host system facts, devtools, active credentials, and daemon states."""
    cfg_dir = config_dir or (Path.home() / ".hex")
    hw = HardwareProfile.detect()

    installed: list[str] = []
    tools_map = {
        "git": "git",
        "docker": "docker",
        "python": "python3",
        "node": "node",
        "rust": "rustc",
        "c_compiler": "clang",
        "brew": "brew",
        "winget": "winget",
        "flatpak": "flatpak",
    }
    tool_status: dict[str, bool] = {}
    for label, binary in tools_map.items():
        present = _probe_tool(binary)
        tool_status[label] = present
        if present:
            installed.append(binary)

    # Probe Antigravity IDE OAuth credentials
    ag_mgr = AntigravityOAuthManager(auth_file=cfg_dir / "antigravity_auth.json")
    ag_cred = ag_mgr.resolve_credential(auto_refresh=False)
    has_ag = bool(ag_cred and (ag_cred.access_token or ag_cred.refresh_token))

    # Search for existing credentials in env and credentials.env
    env_keys = {
        "ANTHROPIC_API_KEY": "",
        "OPENAI_API_KEY": "",
        "GEMINI_API_KEY": "",
        "OPENROUTER_API_KEY": "",
        "DEEPSEEK_API_KEY": "",
        "GROQ_API_KEY": "",
        "XAI_API_KEY": "",
        "TAVILY_API_KEY": "",
        "BRAVE_API_KEY": "",
        "HEX_TELEGRAM_BOT_TOKEN": "",
        "TELEGRAM_BOT_TOKEN": "",
        "HEX_TELEGRAM_ALLOWED_USERS": "",
        "HEX_DISCORD_BOT_TOKEN": "",
        "DISCORD_BOT_TOKEN": "",
        "HEX_DISCORD_WEBHOOK_URL": "",
        "HEX_CLOUD_PROVIDER": "",
        "HEX_CLOUD_MODEL_ID": "",
        "HEX_REASONING_EFFORT": "",
    }

    # First check process environment
    for k in env_keys:
        v = os.getenv(k, "").strip()
        if v:
            env_keys[k] = v

    # Then check files in ~/.hex/credentials.env and local workspace
    cred_candidates = [cfg_dir / "credentials.env", Path.cwd() / ".hex" / "credentials.env"]
    for cfile in cred_candidates:
        if cfile.is_file():
            try:
                for k, v in parse_env_file(cfile).items():
                    if k in env_keys and not env_keys[k]:
                        env_keys[k] = v.strip()
            except Exception:
                pass

    # Check existing config.yaml
    is_configured = False
    existing_prov = env_keys.get("HEX_CLOUD_PROVIDER", "")
    existing_model = env_keys.get("HEX_CLOUD_MODEL_ID", "")
    existing_effort = env_keys.get("HEX_REASONING_EFFORT", "")
    cfg_file = cfg_dir / "config.yaml"
    if cfg_file.is_file():
        is_configured = True
        try:
            cfg_data = parse_config_yaml(cfg_file)
            m_sec = cfg_data.get("model") or {}
            if isinstance(m_sec, dict):
                if not existing_prov and m_sec.get("cloud_provider"):
                    existing_prov = str(m_sec["cloud_provider"]).strip()
                if not existing_model and m_sec.get("cloud_model_id"):
                    existing_model = str(m_sec["cloud_model_id"]).strip()
                if not existing_effort and m_sec.get("reasoning_effort"):
                    existing_effort = str(m_sec["reasoning_effort"]).strip()
        except Exception:
            pass

    # Probe local runtimes
    ollama_ok = _probe_http_ok("http://localhost:11434")
    ollama_models = _probe_ollama_tags() if ollama_ok else []
    lmstudio_ok = _probe_http_ok("http://localhost:1234/v1/models")

    return EnvironmentSnapshot(
        os_name=hw.os_name,
        arch=hw.arch,
        machine_model=hw.machine_model,
        ram_gb=hw.ram_gb,
        is_apple_silicon=hw.is_apple_silicon,
        has_metal=hw.has_metal,
        has_cuda=hw.has_cuda,
        cow_backend=hw.cow_backend,
        sandbox_backend=hw.sandbox_backend,
        has_git=tool_status["git"],
        has_docker=tool_status["docker"],
        has_python=tool_status["python"],
        has_node=tool_status["node"],
        has_rustc=tool_status["rust"],
        has_clang=tool_status["c_compiler"],
        has_brew=tool_status["brew"],
        has_winget=tool_status["winget"],
        has_flatpak=tool_status["flatpak"],
        installed_tools=installed,
        has_antigravity_ide=has_ag,
        antigravity_token=ag_cred.access_token if (ag_cred and ag_cred.access_token) else "",
        antigravity_source=ag_cred.source if ag_cred else "",
        antigravity_project_id=ag_cred.project_id if ag_cred else None,
        anthropic_key=env_keys["ANTHROPIC_API_KEY"],
        openai_key=env_keys["OPENAI_API_KEY"],
        gemini_key=env_keys["GEMINI_API_KEY"],
        openrouter_key=env_keys["OPENROUTER_API_KEY"],
        deepseek_key=env_keys["DEEPSEEK_API_KEY"],
        groq_key=env_keys["GROQ_API_KEY"],
        xai_key=env_keys["XAI_API_KEY"],
        tavily_key=env_keys["TAVILY_API_KEY"],
        brave_key=env_keys["BRAVE_API_KEY"],
        ollama_running=ollama_ok,
        ollama_models=ollama_models,
        lmstudio_running=lmstudio_ok,
        telegram_bot_token=env_keys["HEX_TELEGRAM_BOT_TOKEN"] or env_keys["TELEGRAM_BOT_TOKEN"],
        telegram_allowed_users=env_keys["HEX_TELEGRAM_ALLOWED_USERS"],
        discord_bot_token=env_keys["HEX_DISCORD_BOT_TOKEN"] or env_keys["DISCORD_BOT_TOKEN"],
        discord_webhook_url=env_keys["HEX_DISCORD_WEBHOOK_URL"],
        is_already_configured=is_configured or bool(existing_prov),
        existing_provider=existing_prov,
        existing_model_id=existing_model,
        existing_reasoning_effort=existing_effort,
    )


def synthesize_optimal_config(env: EnvironmentSnapshot) -> dict[str, Any]:
    """Derive optimal provider, models, effort levels, compression, and tools from discovered facts."""
    # 1. Model Provider & Brain Selection
    provider: str
    model_id: str
    reasoning_effort: str
    base_url: str = ""
    profile: str

    if env.is_already_configured and env.existing_provider:
        provider = env.existing_provider
        model_id = env.existing_model_id or "gemini-2.5-pro"
        reasoning_effort = env.existing_reasoning_effort or "medium"
        profile = "antigravity_hybrid" if (env.is_apple_silicon and provider == "antigravity") else ("hybrid" if env.is_apple_silicon else "cloud")
    elif env.has_antigravity_ide:
        # Highest priority: Google Antigravity included quota from local IDE
        provider = "antigravity"
        model_id = "gemini-2.5-pro"
        reasoning_effort = "low"
        profile = "antigravity_hybrid" if env.is_apple_silicon else "antigravity"
    elif env.anthropic_key:
        provider = "anthropic"
        model_id = "claude-3-7-sonnet"
        reasoning_effort = "medium"
        profile = "hybrid" if env.is_apple_silicon else "cloud"
    elif env.openrouter_key:
        provider = "openrouter"
        model_id = "anthropic/claude-3.7-sonnet:thinking"
        reasoning_effort = "medium"
        profile = "hybrid" if env.is_apple_silicon else "cloud"
    elif env.openai_key:
        provider = "openai"
        model_id = "gpt-4o"
        reasoning_effort = "medium"
        profile = "hybrid" if env.is_apple_silicon else "cloud"
    elif env.gemini_key:
        provider = "gemini"
        model_id = "gemini-2.5-pro"
        reasoning_effort = "low"
        profile = "hybrid" if env.is_apple_silicon else "cloud"
    elif env.deepseek_key:
        provider = "deepseek"
        model_id = "deepseek-chat"
        reasoning_effort = "medium"
        profile = "hybrid" if env.is_apple_silicon else "cloud"
    elif env.groq_key:
        provider = "groq"
        model_id = "llama-3.3-70b-versatile"
        reasoning_effort = "none"
        profile = "hybrid" if env.is_apple_silicon else "cloud"
    elif env.ollama_running and env.ollama_models:
        provider = "ollama"
        model_id = env.ollama_models[0]
        reasoning_effort = "none"
        base_url = "http://localhost:11434"
        profile = "local"
    else:
        # Offline fallback
        provider = "local"
        model_id = "qwen2.5-coder:7b"
        reasoning_effort = "none"
        base_url = "http://localhost:11434"
        profile = "local"

    # 2. Context Compressor Selection
    compressor_mode: str
    if provider in ("local", "ollama", "lmstudio"):
        compressor_mode = "local"
    elif env.is_apple_silicon:
        # Apple Silicon M4 / Darwin ARM64 heuristic compressor: 0 RAM, <1ms, $0 cost
        compressor_mode = "deterministic"
    else:
        compressor_mode = "deterministic"

    # 3. Autonomous Web Search Engine Selection
    search_provider: str
    if env.tavily_key:
        search_provider = "tavily"
    elif env.brave_key:
        search_provider = "brave"
    else:
        search_provider = "duckduckgo"

    # 4. Messaging System Gateways
    has_telegram = bool(env.telegram_bot_token)
    has_discord = bool(env.discord_bot_token)

    return {
        "model": {
            "cloud_provider": provider,
            "cloud_model_id": model_id,
            "reasoning_effort": reasoning_effort,
            "profile": profile,
            "compressor_mode": compressor_mode,
            "cloud_base_url": base_url,
        },
        "web_search": {
            "provider": search_provider,
        },
        "messaging": {
            "telegram": {
                "enabled": has_telegram,
                "bot_token": env.telegram_bot_token,
                "allowed_users": env.telegram_allowed_users,
            },
            "discord": {
                "enabled": has_discord,
                "bot_token": env.discord_bot_token,
                "webhook_url": env.discord_webhook_url,
            },
        },
        "facts": {
            "os_name": env.os_name,
            "arch": env.arch,
            "machine_model": env.machine_model,
            "ram_gb": env.ram_gb,
            "is_apple_silicon": env.is_apple_silicon,
            "has_metal": env.has_metal,
            "has_cuda": env.has_cuda,
            "cow_backend": env.cow_backend,
            "sandbox_backend": env.sandbox_backend,
            "installed_tools": env.installed_tools,
            "antigravity_source": env.antigravity_source,
        },
    }


def execute_auto_setup(
    config_dir: Path | None = None,
    custom_env: EnvironmentSnapshot | None = None,
) -> dict[str, Any]:
    """Execute end-to-end automated setup without blocking prompts."""
    cfg_dir = config_dir or (Path.home() / ".hex")
    cfg_dir.mkdir(parents=True, exist_ok=True)
    credentials_file = cfg_dir / "credentials.env"
    config_file = cfg_dir / "config.yaml"

    env = custom_env or detect_environment(cfg_dir)
    cfg = synthesize_optimal_config(env)

    # 1. Write config.yaml (deep-merged over any existing config so unrelated
    # user sections survive an auto-setup re-run; written atomically)
    m_info = cfg["model"]
    s_info = cfg["web_search"]
    msg_info = cfg["messaging"]

    model_section: dict[str, Any] = {
        "cloud_provider": m_info["cloud_provider"],
        "cloud_model_id": m_info["cloud_model_id"],
        "reasoning_effort": m_info["reasoning_effort"],
        "profile": m_info["profile"],
        "compressor_mode": m_info["compressor_mode"],
    }
    if m_info.get("cloud_base_url"):
        model_section["cloud_base_url"] = m_info["cloud_base_url"]

    template_data = {
        "model": model_section,
        "web_search": {"provider": s_info["provider"]},
        "messaging": {
            "telegram_enabled": bool(msg_info["telegram"]["enabled"]),
            "discord_enabled": bool(msg_info["discord"]["enabled"]),
        },
        "runtime": {
            "cow_backend": env.cow_backend,
            "sandbox_backend": env.sandbox_backend,
            "max_turns": 90,
        },
    }
    from harness_core.config_loader import update_config_yaml

    update_config_yaml(
        config_file,
        template_data,
        header="# HEX Autonomous Workbench Configuration\n# Generated automatically via `hex setup --auto`",
    )

    # 2. Write credentials.env (chmod 0600)
    cred_lines = [
        "# HEX Autonomous Workbench Credentials",
        "# Generated automatically via `hex setup --auto`",
        f"HEX_CLOUD_PROVIDER={m_info['cloud_provider']}",
        f"HEX_CLOUD_MODEL_ID={m_info['cloud_model_id']}",
        f"HEX_REASONING_EFFORT={m_info['reasoning_effort']}",
        f"HEX_COMPRESSOR_MODE={m_info['compressor_mode']}",
        f"HEX_WEB_SEARCH_PROVIDER={s_info['provider']}",
    ]
    if m_info.get("cloud_base_url"):
        cred_lines.append(f"HEX_CLOUD_BASE_URL={m_info['cloud_base_url']}")

    # Preserve active API keys
    key_pairs = [
        ("HEX_ANTIGRAVITY_TOKEN", env.antigravity_token),
        ("ANTHROPIC_API_KEY", env.anthropic_key),
        ("OPENAI_API_KEY", env.openai_key),
        ("GEMINI_API_KEY", env.gemini_key),
        ("OPENROUTER_API_KEY", env.openrouter_key),
        ("DEEPSEEK_API_KEY", env.deepseek_key),
        ("GROQ_API_KEY", env.groq_key),
        ("XAI_API_KEY", env.xai_key),
        ("TAVILY_API_KEY", env.tavily_key),
        ("BRAVE_API_KEY", env.brave_key),
        ("HEX_TELEGRAM_BOT_TOKEN", env.telegram_bot_token),
        ("HEX_TELEGRAM_ALLOWED_USERS", env.telegram_allowed_users),
        ("HEX_DISCORD_BOT_TOKEN", env.discord_bot_token),
        ("HEX_DISCORD_WEBHOOK_URL", env.discord_webhook_url),
    ]
    for k, v in key_pairs:
        if v:
            cred_lines.append(f"{k}={v}")

    credentials_file.write_text("\n".join(cred_lines) + "\n", encoding="utf-8")
    try:
        credentials_file.chmod(0o600)
    except Exception:
        pass

    return {
        "status": "success",
        "config_file": str(config_file),
        "credentials_file": str(credentials_file),
        "synthesized": cfg,
    }
