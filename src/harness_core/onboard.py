"""HEX Onboarding Wizard (`hex onboard`).

Guides the operator through hardware discovery, snapshot & sandbox configuration,
Google Antigravity OAuth quota detection, model provider selection, web search engine,
remote messaging gateways (Telegram & Discord), credential storage, and ledger initialization.
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config_loader import load_hex_environment, parse_config_yaml, parse_env_file
from .cow import get_default_snapshot_provider
from .orchestrator import HarnessOrchestrator
from .providers.antigravity_provider import AntigravityOAuthManager
from .providers.cloud_provider import fetch_available_models
from .sandbox import get_default_sandbox
from .ui_selector import prompt_secret, select_option


HEX_BANNER = r"""
  ██╗  ██╗███████╗██╗  ██╗
  ██║  ██║██╔════╝╚██╗██╔╝
  ███████║█████╗   ╚███╔╝ 
  ██╔══██║██╔══╝   ██╔██╗ 
  ██║  ██║███████╗██╔╝ ██╗
  ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝
  HEX: Autonomous AI Workbench
"""


@dataclass
class HardwareProfile:
    os_name: str
    arch: str
    machine_model: str = "Generic Host"
    ram_gb: float = 8.0
    is_apple_silicon: bool = False
    has_metal: bool = False
    has_cuda: bool = False
    cow_backend: str = "portable"
    sandbox_backend: str = "restricted_subprocess"

    @classmethod
    def detect(cls, data_dir: Path | None = None) -> HardwareProfile:
        os_name = platform.system()
        arch = platform.machine()
        machine_model = "Generic POSIX Host"
        ram_gb = 8.0
        is_apple_silicon = False
        has_metal = False
        has_cuda = False

        if os_name == "Darwin":
            if arch.lower() in ("arm64", "aarch64"):
                is_apple_silicon = True
                has_metal = True
                try:
                    out = subprocess.check_output(
                        ["sysctl", "-n", "machdep.cpu.brand_string"], text=True, stderr=subprocess.DEVNULL
                    ).strip()
                    if out:
                        machine_model = out
                except Exception:
                    machine_model = "Apple Silicon Mac"

            try:
                mem_bytes = int(
                    subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True, stderr=subprocess.DEVNULL).strip()
                )
                ram_gb = round(mem_bytes / (1024**3), 1)
            except Exception:
                pass
        elif os_name == "Linux":
            try:
                with open("/proc/meminfo", "r") as f:
                    for line in f:
                        if line.startswith("MemTotal:"):
                            parts = line.split()
                            kb = int(parts[1])
                            ram_gb = round(kb / (1024**2), 1)
                            break
            except Exception:
                pass

            try:
                subprocess.check_output(["nvidia-smi"], stderr=subprocess.DEVNULL)
                has_cuda = True
                machine_model = "Linux with NVIDIA GPU"
            except Exception:
                machine_model = "Linux Standard Host"

        cow_provider = get_default_snapshot_provider(data_dir or Path("/tmp"))
        cow_backend = cow_provider.backend_name

        sandbox = get_default_sandbox()
        sandbox_backend = sandbox.backend_name

        return cls(
            os_name=os_name,
            arch=arch,
            machine_model=machine_model,
            ram_gb=ram_gb,
            is_apple_silicon=is_apple_silicon,
            has_metal=has_metal,
            has_cuda=has_cuda,
            cow_backend=cow_backend,
            sandbox_backend=sandbox_backend,
        )


def _probe_antigravity_token(token: str) -> bool:
    """Test whether an Antigravity OAuth access token is live and authenticated."""
    if not token or not token.strip():
        return False
    import urllib.request
    url = "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist"
    headers = {
        "Authorization": f"Bearer {token.strip()}",
        "Content-Type": "application/json",
        "User-Agent": "HEX/0.1.0",
    }
    body = b'{"metadata":{"ideType":"ANTIGRAVITY","platform":"PLATFORM_UNSPECIFIED","pluginType":"GEMINI"}}'
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            return resp.status in (200, 201)
    except Exception:
        return False


def update_config_yaml_model(
    config_path: Path,
    *,
    cloud_provider: str,
    cloud_model_id: str,
    profile: str,
) -> None:
    """Safely update model section in config.yaml without requiring third-party PyYAML."""
    if not config_path.is_file():
        return
    try:
        def _clean_val(v: str) -> str:
            return v.strip(' \t\n\r"\'\\')

        content = config_path.read_text(encoding="utf-8")
        content = re.sub(
            r'(\bcloud_provider:\s*)[^\n]+',
            rf'\g<1>"{_clean_val(cloud_provider)}"',
            content,
        )
        content = re.sub(
            r'(\bcloud_model_id:\s*)[^\n]+',
            rf'\g<1>"{_clean_val(cloud_model_id)}"',
            content,
        )
        content = re.sub(
            r'(\bprofile:\s*)[^\n]+',
            rf'\g<1>"{_clean_val(profile)}"',
            content,
        )
        config_path.write_text(content, encoding="utf-8")
    except Exception:
        pass


def run_onboard_wizard(
    *,
    config_dir: Path | None = None,
    non_interactive: bool = False,
    defaults_only: bool = False,
    target_stage: str | None = None,
) -> dict[str, Any]:
    """Execute the interactive or automated onboarding sequence for HEX."""
    cfg_dir = config_dir or (Path.home() / ".hex")
    try:
        cfg_dir.mkdir(parents=True, exist_ok=True)
        test_file = cfg_dir / ".write_test"
        test_file.touch()
        test_file.unlink()
    except (PermissionError, OSError):
        cfg_dir = Path(".hex").resolve()
        cfg_dir.mkdir(parents=True, exist_ok=True)
    credentials_file = cfg_dir / "credentials.env"
    config_file = cfg_dir / "config.yaml"

    profile = HardwareProfile.detect()
    ag_mgr = AntigravityOAuthManager(auth_file=cfg_dir / "antigravity_auth.json")
    ag_cred = ag_mgr.resolve_credential(auto_refresh=True)

    print(HEX_BANNER)
    print("Welcome to HEX — The Autonomous AI Workbench.")
    print("=" * 64)
    print(f"  OS / Architecture:  {profile.os_name} ({profile.arch})")
    print(f"  Hardware Model:     {profile.machine_model}")
    print(f"  System Memory:      {profile.ram_gb} GB RAM")
    print(f"  CoW Snapshot Mode:  {profile.cow_backend}")
    print(f"  Execution Sandbox:  {profile.sandbox_backend}")
    if profile.is_apple_silicon:
        print("  Apple Silicon MLX:  Accelerated (Metal Unified Memory available)")
    if ag_cred and (ag_cred.access_token or ag_cred.refresh_token):
        print(f"  Antigravity Quota:  Detected ({ag_cred.source}) — Ready for $0 Day-1 routing")
    else:
        print("  Antigravity Quota:  Ready to link via `hex auth antigravity`")
    print("=" * 64)
    print()

    # Discover existing configuration and credentials if already configured
    existing_config: dict[str, Any] = {}
    if config_file.is_file():
        existing_config = parse_config_yaml(config_file)

    # Also inspect local project .hex/config.yaml if running in a project workspace without custom config_dir
    if config_dir is None:
        local_proj_cfg = Path.cwd() / ".hex" / "config.yaml"
        if local_proj_cfg.is_file() and local_proj_cfg.resolve() != config_file.resolve():
            loaded_p = parse_config_yaml(local_proj_cfg)
            if isinstance(loaded_p, dict) and "model" in loaded_p:
                existing_config.setdefault("model", {}).update(loaded_p["model"])

    creds_files = [credentials_file]
    if config_dir is None:
        p_cred = Path.cwd() / ".hex" / "credentials.env"
        if p_cred.is_file() and p_cred.resolve() != credentials_file.resolve():
            creds_files.append(p_cred)

    existing_creds: dict[str, str] = {}
    for cf in creds_files:
        if cf.is_file():
            try:
                for k, v in parse_env_file(cf).items():
                    existing_creds[k] = v
            except Exception:
                pass

    is_already_configured = bool(existing_creds or existing_config)

    existing_provider = (
        existing_creds.get("HEX_CLOUD_PROVIDER")
        or (existing_config.get("model") or {}).get("cloud_provider")
    )
    existing_model_id = (
        existing_creds.get("HEX_CLOUD_MODEL_ID")
        or (existing_config.get("model") or {}).get("cloud_model_id")
    )
    existing_profile = (
        (existing_config.get("model") or {}).get("profile")
    )
    existing_search = (
        existing_creds.get("HEX_WEB_SEARCH_PROVIDER")
        or (existing_config.get("web_search") or {}).get("provider")
    )
    existing_telegram = existing_creds.get("HEX_TELEGRAM_BOT_TOKEN")
    existing_telegram_users = existing_creds.get("HEX_TELEGRAM_ALLOWED_USERS")
    existing_discord = existing_creds.get("HEX_DISCORD_BOT_TOKEN")
    existing_discord_hook = existing_creds.get("HEX_DISCORD_WEBHOOK_URL")
    existing_antigravity_token = (
        (ag_cred.access_token if ag_cred and ag_cred.access_token else "")
        or existing_creds.get("HEX_ANTIGRAVITY_TOKEN", "")
    )
    existing_anthropic_key = existing_creds.get("ANTHROPIC_API_KEY", "")
    existing_openai_key = existing_creds.get("OPENAI_API_KEY", "")
    existing_gemini_key = existing_creds.get("GEMINI_API_KEY", "")
    existing_tavily_key = existing_creds.get("TAVILY_API_KEY", "")
    existing_brave_key = existing_creds.get("BRAVE_API_KEY", "")

    norm_stage = (target_stage or "").lower().strip()
    is_model_stage = norm_stage in ("model", "models", "provider", "brain")
    is_search_stage = norm_stage in ("search", "web_search", "web")
    is_messaging_stage = norm_stage in ("messaging", "msg", "telegram", "discord", "gateway", "gateways")

    if norm_stage and not (is_model_stage or is_search_stage or is_messaging_stage or norm_stage == "all"):
        print(f"\n✖ Unrecognized setup target: '{target_stage}'")
        print("  Available targets:")
        print("    • hex setup model      (Configure model provider, brain & active model)")
        print("    • hex setup search     (Configure autonomous web search engine)")
        print("    • hex setup messaging  (Configure remote Telegram & Discord bot gateways)")
        print("    • hex setup            (Run complete end-to-end setup wizard)\n")
        return {"status": "error", "error": f"Unrecognized setup target: {target_stage}"}

    should_run_model = not (is_search_stage or is_messaging_stage)
    should_run_search = not (is_model_stage or is_messaging_stage)
    should_run_messaging = not (is_model_stage or is_search_stage)

    # Initial state resolving existing settings first
    cloud_provider = existing_provider or "antigravity"
    cloud_model_id = existing_model_id or ("claude-opus-4-6-thinking" if cloud_provider == "antigravity" else "")
    model_profile = existing_profile or (
        "antigravity_hybrid" if profile.is_apple_silicon and cloud_provider == "antigravity" else ("hybrid" if profile.is_apple_silicon else "cloud")
    )

    anthropic_key = existing_anthropic_key
    openai_key = existing_openai_key
    gemini_key = existing_gemini_key
    antigravity_token = existing_antigravity_token

    web_search_provider = existing_search or "duckduckgo"
    tavily_key = existing_tavily_key
    brave_key = existing_brave_key

    telegram_token = existing_telegram or ""
    telegram_allowed = existing_telegram_users or ""
    discord_token = existing_discord or ""
    discord_webhook = existing_discord_hook or ""

    provider_name_map = {
        "antigravity": "Google Antigravity",
        "anthropic": "Anthropic",
        "openai": "OpenAI",
        "gemini": "Google Gemini",
        "local": "Local Offline Model",
    }

    if not non_interactive and not defaults_only:
        # Step 1: Model Provider Selection
        if should_run_model:
            provider_keys = ["antigravity", "anthropic", "openai", "gemini", "local"]

            def _prov_label(pkey: str, label: str) -> str:
                if existing_provider == pkey:
                    return f"{label} [Currently Configured]"
                return label

            provider_options: list[tuple[str, str]] = [
                ("antigravity", _prov_label("antigravity", "Google Antigravity OAuth ($0 Day-1 Free Tier / Auto-detect IDE session)")),
                ("anthropic", _prov_label("anthropic", "Anthropic API Key (Claude 3.5 Sonnet / Opus)")),
                ("openai", _prov_label("openai", "OpenAI API Key (GPT-4o / o1 / o3-mini)")),
                ("gemini", _prov_label("gemini", "Google Gemini API Key (Gemini 2.5 Pro / Flash)")),
                ("local", _prov_label("local", "Local Offline Model (Apple Silicon MLX / Ollama — Zero egress)")),
            ]

            default_prov_idx = 0
            if existing_provider in provider_keys:
                default_prov_idx = provider_keys.index(existing_provider)

            p_idx = select_option(
                "Select Primary Model Provider & Brain:",
                provider_options,
                default_index=default_prov_idx,
            )
            selected_provider_key = provider_options[p_idx][0]

            if selected_provider_key == "antigravity":
                cloud_provider = "antigravity"
                model_profile = "antigravity_hybrid" if profile.is_apple_silicon else "antigravity"
                provider_title = "Google Antigravity"

                if antigravity_token:
                    if ag_cred and ag_cred.access_token:
                        antigravity_token = ag_cred.access_token
                    tok_preview = antigravity_token[:12] if len(antigravity_token) > 12 else antigravity_token
                    auth_options: list[tuple[str, str]] = [
                        ("keep", f"Use currently configured Antigravity session ({tok_preview}...)"),
                        ("browser", "Sign in with Google in browser (Browser PKCE OAuth 2.0)"),
                        ("manual", "Enter OAuth token manually (ya29... / 1//...)"),
                    ]
                    a_idx = select_option("Google Antigravity Authentication:", auth_options, default_index=0)
                    chosen_auth = auth_options[a_idx][0]

                    if chosen_auth == "keep":
                        if ag_mgr:
                            try:
                                refreshed = ag_mgr.resolve_credential(auto_refresh=True)
                                if refreshed and refreshed.access_token:
                                    antigravity_token = refreshed.access_token
                            except Exception:
                                pass
                        print("  ✓ Keeping currently configured Antigravity session.\n")
                    elif chosen_auth == "browser":
                        pkce = ag_mgr.generate_pkce_pair()
                        auth_url = ag_mgr.build_authorization_url(pkce["challenge"])
                        print("\n  1. Opening Google Antigravity Sign-In in your browser...")
                        print(f"     URL: {auth_url}\n")
                        try:
                            import webbrowser

                            webbrowser.open(auth_url)
                        except Exception:
                            pass
                        print("  2. After signing in, copy the redirected URL from your browser address bar")
                        print("     (e.g., http://localhost:8420/?code=... or just the code parameter).")
                        code_in = input("  Paste redirected URL or authorization code: ").strip()
                        if code_in:
                            try:
                                cred = ag_mgr.exchange_code_for_tokens(code_in, pkce["verifier"])
                                antigravity_token = cred.access_token
                                print(f"\n  ✓ Successfully authenticated with Google Antigravity!\n")
                            except Exception as exc:
                                print(f"\n  ✖ OAuth exchange error: {exc}\n")
                    elif chosen_auth == "manual":
                        k_ag = prompt_secret("  Enter Antigravity OAuth Token: ")
                        if k_ag:
                            antigravity_token = k_ag
                else:
                    auth_options = [
                        ("browser", "Sign in with Google in browser (Browser PKCE OAuth 2.0)"),
                        ("manual", "Enter OAuth token manually (ya29... / 1//...)"),
                        ("skip", "Skip for now (configure later)"),
                    ]
                    a_idx = select_option("Google Antigravity Authentication:", auth_options, default_index=0)
                    chosen_auth = auth_options[a_idx][0]
                    if chosen_auth == "browser":
                        pkce = ag_mgr.generate_pkce_pair()
                        auth_url = ag_mgr.build_authorization_url(pkce["challenge"])
                        print("\n  1. Opening Google Antigravity Sign-In in your browser...")
                        print(f"     URL: {auth_url}\n")
                        try:
                            import webbrowser

                            webbrowser.open(auth_url)
                        except Exception:
                            pass
                        print("  2. After signing in, copy the redirected URL from your browser address bar")
                        print("     (e.g., http://localhost:8420/?code=... or just the code parameter).")
                        code_in = input("  Paste redirected URL or authorization code: ").strip()
                        if code_in:
                            try:
                                cred = ag_mgr.exchange_code_for_tokens(code_in, pkce["verifier"])
                                antigravity_token = cred.access_token
                                print(f"\n  ✓ Successfully authenticated with Google Antigravity!\n")
                            except Exception as exc:
                                print(f"\n  ✖ OAuth exchange error: {exc}\n")
                    elif chosen_auth == "manual":
                        k_ag = prompt_secret("  Enter Antigravity OAuth Token: ")
                        if k_ag:
                            antigravity_token = k_ag

                print("  ◈ Querying available models from Google Antigravity...")
                try:
                    discovered_models = fetch_available_models("antigravity", api_key=antigravity_token)
                except Exception as exc:
                    print(f"  ⚠ Live model query failed: {exc}")
                    discovered_models = []
                model_options: list[tuple[str, str]] = []
                if existing_model_id and existing_provider == "antigravity":
                    curr_label = existing_model_id
                    for mid, mlabel in (discovered_models or []):
                        if mid == existing_model_id:
                            curr_label = mlabel
                            break
                    model_options.append(("keep", f"Use currently configured ({curr_label})"))
                for mid, mlabel in (discovered_models or []):
                    if mid != existing_model_id:
                        model_options.append((mid, mlabel))

                if model_options:
                    m_idx = select_option(f"Select Primary {provider_title} Model:", model_options, default_index=0)
                    chosen_mid = model_options[m_idx][0]
                    cloud_model_id = existing_model_id if chosen_mid == "keep" else chosen_mid
                    print(f"  ✓ Active model selected: {cloud_model_id}\n")
                else:
                    print(f"  ℹ [Offline/Manual Entry] Live model discovery was unavailable for {provider_title}.")
                    default_mid = existing_model_id or "gemini-3-flash"
                    manual_mid = input(f"  Enter {provider_title} Model ID (default: {default_mid}): ").strip()
                    cloud_model_id = manual_mid or default_mid
                    print(f"  ✓ Active model configured (offline mode): {cloud_model_id}\n")

            elif selected_provider_key == "anthropic":
                cloud_provider = "anthropic"
                model_profile = "hybrid" if profile.is_apple_silicon else "cloud"
                provider_title = "Anthropic"

                if not anthropic_key:
                    anthropic_key = prompt_secret("Enter Anthropic API Key (sk-ant-...): ")
                else:
                    k_prev = f"...{anthropic_key[-4:]}" if len(anthropic_key) >= 4 else "configured"
                    new_key = prompt_secret(f"Enter Anthropic API Key (Press Enter to keep currently configured {k_prev}): ")
                    if new_key.strip():
                        anthropic_key = new_key.strip()

                print("  ◈ Querying available models from Anthropic...")
                try:
                    discovered_models = fetch_available_models("anthropic", api_key=anthropic_key)
                except Exception as exc:
                    print(f"  ⚠ Live model query failed: {exc}")
                    discovered_models = []
                model_options = []
                if existing_model_id and existing_provider == "anthropic":
                    curr_label = existing_model_id
                    for mid, mlabel in (discovered_models or []):
                        if mid == existing_model_id:
                            curr_label = mlabel
                            break
                    model_options.append(("keep", f"Use currently configured ({curr_label})"))
                for mid, mlabel in (discovered_models or []):
                    if mid != existing_model_id:
                        model_options.append((mid, mlabel))

                if model_options:
                    m_idx = select_option(f"Select Primary {provider_title} Model:", model_options, default_index=0)
                    chosen_mid = model_options[m_idx][0]
                    cloud_model_id = existing_model_id if chosen_mid == "keep" else chosen_mid
                    print(f"  ✓ Active model selected: {cloud_model_id}\n")
                else:
                    print(f"  ℹ [Offline/Manual Entry] Live model discovery was unavailable for {provider_title}.")
                    default_mid = existing_model_id or "claude-3-5-sonnet"
                    manual_mid = input(f"  Enter {provider_title} Model ID (default: {default_mid}): ").strip()
                    cloud_model_id = manual_mid or default_mid
                    print(f"  ✓ Active model configured (offline mode): {cloud_model_id}\n")

            elif selected_provider_key == "openai":
                cloud_provider = "openai"
                model_profile = "hybrid" if profile.is_apple_silicon else "cloud"
                provider_title = "OpenAI"

                if not openai_key:
                    openai_key = prompt_secret("Enter OpenAI API Key (sk-...): ")
                else:
                    k_prev = f"...{openai_key[-4:]}" if len(openai_key) >= 4 else "configured"
                    new_key = prompt_secret(f"Enter OpenAI API Key (Press Enter to keep currently configured {k_prev}): ")
                    if new_key.strip():
                        openai_key = new_key.strip()

                print("  ◈ Querying available models from OpenAI...")
                try:
                    discovered_models = fetch_available_models("openai", api_key=openai_key)
                except Exception as exc:
                    print(f"  ⚠ Live model query failed: {exc}")
                    discovered_models = []
                model_options = []
                if existing_model_id and existing_provider == "openai":
                    curr_label = existing_model_id
                    for mid, mlabel in (discovered_models or []):
                        if mid == existing_model_id:
                            curr_label = mlabel
                            break
                    model_options.append(("keep", f"Use currently configured ({curr_label})"))
                for mid, mlabel in (discovered_models or []):
                    if mid != existing_model_id:
                        model_options.append((mid, mlabel))

                if model_options:
                    m_idx = select_option(f"Select Primary {provider_title} Model:", model_options, default_index=0)
                    chosen_mid = model_options[m_idx][0]
                    cloud_model_id = existing_model_id if chosen_mid == "keep" else chosen_mid
                    print(f"  ✓ Active model selected: {cloud_model_id}\n")
                else:
                    print(f"  ℹ [Offline/Manual Entry] Live model discovery was unavailable for {provider_title}.")
                    default_mid = existing_model_id or "gpt-4o"
                    manual_mid = input(f"  Enter {provider_title} Model ID (default: {default_mid}): ").strip()
                    cloud_model_id = manual_mid or default_mid
                    print(f"  ✓ Active model configured (offline mode): {cloud_model_id}\n")

            elif selected_provider_key == "gemini":
                cloud_provider = "gemini"
                model_profile = "hybrid" if profile.is_apple_silicon else "cloud"
                provider_title = "Google Gemini"

                if not gemini_key:
                    gemini_key = prompt_secret("Enter Gemini API Key (AIza...): ")
                else:
                    k_prev = f"...{gemini_key[-4:]}" if len(gemini_key) >= 4 else "configured"
                    new_key = prompt_secret(f"Enter Gemini API Key (Press Enter to keep currently configured {k_prev}): ")
                    if new_key.strip():
                        gemini_key = new_key.strip()

                print("  ◈ Querying available models from Google Gemini...")
                try:
                    discovered_models = fetch_available_models("gemini", api_key=gemini_key)
                except Exception as exc:
                    print(f"  ⚠ Live model query failed: {exc}")
                    discovered_models = []
                model_options = []
                if existing_model_id and existing_provider == "gemini":
                    curr_label = existing_model_id
                    for mid, mlabel in (discovered_models or []):
                        if mid == existing_model_id:
                            curr_label = mlabel
                            break
                    model_options.append(("keep", f"Use currently configured ({curr_label})"))
                for mid, mlabel in (discovered_models or []):
                    if mid != existing_model_id:
                        model_options.append((mid, mlabel))

                if model_options:
                    m_idx = select_option(f"Select Primary {provider_title} Model:", model_options, default_index=0)
                    chosen_mid = model_options[m_idx][0]
                    cloud_model_id = existing_model_id if chosen_mid == "keep" else chosen_mid
                    print(f"  ✓ Active model selected: {cloud_model_id}\n")
                else:
                    print(f"  ℹ [Offline/Manual Entry] Live model discovery was unavailable for {provider_title}.")
                    default_mid = existing_model_id or "gemini-2.5-pro"
                    manual_mid = input(f"  Enter {provider_title} Model ID (default: {default_mid}): ").strip()
                    cloud_model_id = manual_mid or default_mid
                    print(f"  ✓ Active model configured (offline mode): {cloud_model_id}\n")

            elif selected_provider_key == "local":
                cloud_provider = "local"
                model_profile = "local"
                provider_title = "Local"

                print("  ◈ Checking local model runtimes...")
                try:
                    all_local = fetch_available_models("local")
                except Exception as exc:
                    print(f"  ⚠ Local discovery error: {exc}")
                    all_local = []

                model_options = []
                if existing_model_id and existing_provider == "local":
                    curr_label = existing_model_id
                    for mid, mlabel in all_local:
                        if mid == existing_model_id:
                            curr_label = mlabel
                            break
                    model_options.append(("keep", f"Use currently configured ({curr_label})"))
                for mid, mlabel in all_local:
                    if mid != existing_model_id:
                        model_options.append((mid, mlabel))

                if model_options:
                    m_idx = select_option(f"Select Primary {provider_title} Model:", model_options, default_index=0)
                    chosen_mid = model_options[m_idx][0]
                    cloud_model_id = existing_model_id if chosen_mid == "keep" else chosen_mid
                else:
                    print("  ℹ No local models discovered in Ollama or HuggingFace/LM-Studio cache.")
                    manual_model = input("  Enter Local Model Path or HuggingFace ID / Tag: ").strip()
                    cloud_model_id = manual_model if manual_model else (existing_model_id or "mlx-community/Qwen2.5-Coder-7B-Instruct-4bit")
                print("  ✓ Local offline profile configured. Zero external network egress.\n")

        # Step 2: Web Search Provider Selection
        if should_run_search:
            search_options: list[tuple[str, str]] = []
            if is_already_configured and existing_search:
                search_options.append(("keep", f"Use currently configured ({existing_search})"))
            search_options.extend([
                ("duckduckgo", "DuckDuckGo Search (100% Free, zero API key required, instant ready)"),
                ("tavily", "Tavily Search API (Optimized for AI agents & clean markdown extraction)"),
                ("brave", "Brave Search API (Fast, independent web search index)"),
                ("none", "Disabled"),
            ])
            s_idx = select_option(
                "Select Web Search Engine for Autonomous Research:",
                search_options,
                default_index=0,
            )
            chosen_search = search_options[s_idx][0]
            if chosen_search == "keep":
                web_search_provider = existing_search
                print(f"  ✓ Using currently configured search engine: {existing_search}\n")
            else:
                web_search_provider = chosen_search
                if web_search_provider == "tavily":
                    if not tavily_key:
                        tavily_key = prompt_secret("  Enter Tavily API Key (tvly-...): ")
                    else:
                        k_prev = f"...{tavily_key[-4:]}" if len(tavily_key) >= 4 else "configured"
                        new_t = prompt_secret(f"  Enter Tavily API Key (Press Enter to keep currently configured {k_prev}): ")
                        if new_t.strip():
                            tavily_key = new_t.strip()
                elif web_search_provider == "brave":
                    if not brave_key:
                        brave_key = prompt_secret("  Enter Brave Search API Key (BSA...): ")
                    else:
                        k_prev = f"...{brave_key[-4:]}" if len(brave_key) >= 4 else "configured"
                        new_b = prompt_secret(f"  Enter Brave Search API Key (Press Enter to keep currently configured {k_prev}): ")
                        if new_b.strip():
                            brave_key = new_b.strip()
                print()

        # Step 3: Remote Messaging & Bot Gateways
        if should_run_messaging:
            messaging_options: list[tuple[str, str]] = []
            has_existing_msg = is_already_configured and bool(existing_telegram or existing_discord or (existing_config.get("messaging")))
            if has_existing_msg:
                cfg_parts = []
                if existing_telegram:
                    cfg_parts.append("Telegram")
                if existing_discord:
                    cfg_parts.append("Discord")
                m_summary = " + ".join(cfg_parts) if cfg_parts else "Disabled / Skip"
                messaging_options.append(("keep", f"Use currently configured ({m_summary})"))

            messaging_options.extend([
                ("skip", "Skip for now (Terminal Cockpit and Web Surface only)"),
                ("telegram", "Configure Telegram Bot (Telegram bot token + allowed user IDs)"),
                ("discord", "Configure Discord Bot (Discord bot token + channel webhook)"),
                ("both", "Configure Both (Telegram + Discord)"),
            ])
            m_idx = select_option(
                "Configure Remote Messaging Gateways (Control HEX from Phone):",
                messaging_options,
                default_index=0,
            )
            chosen_msg = messaging_options[m_idx][0]
            if chosen_msg == "keep":
                print(f"  ✓ Using currently configured messaging gateways.\n")
            else:
                if chosen_msg in ("telegram", "both"):
                    if not telegram_token:
                        telegram_token = prompt_secret("  Enter Telegram Bot Token: ")
                    else:
                        k_prev = f"...{telegram_token[-4:]}" if len(telegram_token) >= 4 else "configured"
                        new_tg = prompt_secret(f"  Enter Telegram Bot Token (Press Enter to keep currently configured {k_prev}): ")
                        if new_tg.strip():
                            telegram_token = new_tg.strip()
                    raw_users = input(f"  Allowed Telegram User IDs / Names (Press Enter to keep '{telegram_allowed}'): ").strip()
                    if raw_users:
                        telegram_allowed = raw_users
                if chosen_msg in ("discord", "both"):
                    if not discord_token:
                        discord_token = prompt_secret("  Enter Discord Bot Token: ")
                    else:
                        k_prev = f"...{discord_token[-4:]}" if len(discord_token) >= 4 else "configured"
                        new_dc = prompt_secret(f"  Enter Discord Bot Token (Press Enter to keep currently configured {k_prev}): ")
                        if new_dc.strip():
                            discord_token = new_dc.strip()
                    raw_hook = input(f"  Discord Webhook URL (Press Enter to keep '{discord_webhook}'): ").strip()
                    if raw_hook:
                        discord_webhook = raw_hook
                print()

    # Write credentials securely
    creds_content = [
        "# HEX Secure Credentials Environment File",
        f"HEX_CLOUD_PROVIDER={cloud_provider}",
        f"HEX_CLOUD_MODEL_ID={cloud_model_id}",
        f"HEX_ANTIGRAVITY_TOKEN={antigravity_token}",
        f"HEX_ANTIGRAVITY_REFRESH_TOKEN={ag_cred.refresh_token if ag_cred and ag_cred.refresh_token else existing_creds.get('HEX_ANTIGRAVITY_REFRESH_TOKEN', '')}",
        f"ANTHROPIC_API_KEY={anthropic_key}",
        f"OPENAI_API_KEY={openai_key}",
        f"GEMINI_API_KEY={gemini_key}",
        f"HEX_WEB_SEARCH_PROVIDER={web_search_provider}",
        f"TAVILY_API_KEY={tavily_key}",
        f"BRAVE_API_KEY={brave_key}",
        f"HEX_TELEGRAM_BOT_TOKEN={telegram_token}",
        f"HEX_TELEGRAM_ALLOWED_USERS={telegram_allowed}",
        f"HEX_DISCORD_BOT_TOKEN={discord_token}",
        f"HEX_DISCORD_WEBHOOK_URL={discord_webhook}",
    ]
    credentials_file.write_text("\n".join(creds_content) + "\n", encoding="utf-8")
    try:
        os.chmod(credentials_file, 0o600)
    except Exception:
        pass

    # Also update project-level .hex/credentials.env if present in current working directory and not using custom config_dir
    local_creds = Path.cwd() / ".hex" / "credentials.env"
    if config_dir is None and local_creds.is_file() and local_creds.resolve() != credentials_file.resolve():
        try:
            local_creds.write_text("\n".join(creds_content) + "\n", encoding="utf-8")
        except Exception:
            pass

    # Storage configuration
    db_backend = (existing_config.get("storage") or {}).get("backend") or "sqlite"
    data_dir_str = (existing_config.get("storage") or {}).get("data_dir") or str(cfg_dir / "data")
    data_dir = Path(data_dir_str)
    data_dir.mkdir(parents=True, exist_ok=True)
    cow_backend = (existing_config.get("cow") or {}).get("backend") or profile.cow_backend
    sandbox_backend = (existing_config.get("sandbox") or {}).get("backend") or profile.sandbox_backend

    # Write config.yaml
    local_model_id = cloud_model_id if cloud_provider == "local" else (existing_config.get("model") or {}).get("local_model_id", "")
    config_yaml = f"""# HEX Autonomous AI Workbench Configuration
version: "1.0"
model:
  profile: "{model_profile}"
  cloud_provider: "{cloud_provider}"
  compressor_mode: "local"
  cloud_model_id: "{cloud_model_id}"
  local_model_id: "{local_model_id}"
web_search:
  provider: "{web_search_provider}"
messaging:
  telegram_enabled: {str(bool(telegram_token)).lower()}
  discord_enabled: {str(bool(discord_token)).lower()}
storage:
  backend: "{db_backend}"
  data_dir: "{data_dir}"
cow:
  backend: "{cow_backend}"
sandbox:
  backend: "{sandbox_backend}"
webhooks:
  dry_run: true
  public_base_url: "http://127.0.0.1:8420"
"""
    config_file.write_text(config_yaml, encoding="utf-8")

    # If current working directory has a project-scoped .hex/config.yaml, synchronize model settings
    local_proj_cfg = Path.cwd() / ".hex" / "config.yaml"
    if config_dir is None and local_proj_cfg.is_file() and local_proj_cfg.resolve() != config_file.resolve():
        update_config_yaml_model(
            local_proj_cfg,
            cloud_provider=cloud_provider,
            cloud_model_id=cloud_model_id,
            profile=model_profile,
        )

    # Synchronize model settings and credentials across all registered projects in ProjectRegistry
    try:
        from .project_registry import ProjectRegistry

        reg_candidates = [
            cfg_dir / "projects.json",
            Path.home() / ".hex" / "projects.json",
        ]
        registered_paths: set[Path] = set()
        for r_cand in reg_candidates:
            if r_cand.is_file():
                try:
                    reg = ProjectRegistry(r_cand)
                    for prj in reg.list_projects():
                        registered_paths.add(Path(prj.path))
                except Exception:
                    pass
        for p_dir in registered_paths:
            if p_dir.is_dir():
                p_hex = p_dir / ".hex"
                p_cfg = p_hex / "config.yaml"
                p_cr = p_hex / "credentials.env"
                if p_cfg.is_file() and p_cfg.resolve() != config_file.resolve():
                    update_config_yaml_model(
                        p_cfg,
                        cloud_provider=cloud_provider,
                        cloud_model_id=cloud_model_id,
                        profile=model_profile,
                    )
                if p_cr.is_file() and p_cr.resolve() != credentials_file.resolve():
                    try:
                        p_cr.write_text("\n".join(creds_content) + "\n", encoding="utf-8")
                    except Exception:
                        pass
    except Exception:
        pass

    # Hydrate current running process environment immediately
    load_hex_environment(config_dir=cfg_dir, override=True)

    # Return early if specific component stage was targeted
    if is_model_stage:
        print("=" * 64)
        print("✓ Model setup completed successfully!")
        print(f"  • Primary Provider: {cloud_provider}")
        print(f"  • Selected Model:   {cloud_model_id}")
        print(f"  • Profile:          {model_profile}")
        print(f"  • Configuration:    {config_file}")
        print("=" * 64)
        print()
        return {
            "status": "ready",
            "stage": "model",
            "config_file": str(config_file),
            "credentials_file": str(credentials_file),
            "cloud_provider": cloud_provider,
            "cloud_model_id": cloud_model_id,
            "model": cloud_model_id,
        }

    if is_search_stage:
        print("=" * 64)
        print("✓ Web search setup completed successfully!")
        print(f"  • Search Engine:    {web_search_provider}")
        print(f"  • Configuration:    {config_file}")
        print("=" * 64)
        print()
        return {
            "status": "ready",
            "stage": "search",
            "config_file": str(config_file),
            "credentials_file": str(credentials_file),
            "web_search_provider": web_search_provider,
        }

    if is_messaging_stage:
        print("=" * 64)
        print("✓ Messaging gateways setup completed successfully!")
        print(f"  • Telegram Bot:     {'Configured' if telegram_token else 'Disabled'}")
        print(f"  • Discord Bot:      {'Configured' if discord_token else 'Disabled'}")
        print(f"  • Configuration:    {config_file}")
        print("=" * 64)
        print()
        return {
            "status": "ready",
            "stage": "messaging",
            "config_file": str(config_file),
            "credentials_file": str(credentials_file),
        }

    # Initialize ledger and seed built-in skills with SkillSpector for full setup
    repo_candidate = Path(__file__).resolve().parents[2]
    workspace_root = repo_candidate if (repo_candidate / "pyproject.toml").is_file() else Path.cwd()
    orch = HarnessOrchestrator(data_dir=data_dir, default_workspace=workspace_root)
    skills_root = Path(__file__).resolve().parent / "skills"
    if not skills_root.is_dir():
        skills_root = repo_candidate / "skills"
    seeded: list[dict[str, Any]] = []
    if skills_root.is_dir():
        for sub in sorted(skills_root.iterdir()):
            if (sub / "SKILL.md").is_file():
                res = orch.scanner.scan_and_register_skill(sub)
                seeded.append(res)

    print("✓ HEX has been successfully initialized!")
    print(f"  • Configuration: {config_file}")
    print(f"  • Credentials:   {credentials_file} (chmod 0600)")
    print(f"  • Model Provider: {cloud_provider} ({cloud_model_id})")
    print(f"  • Web Search:    {web_search_provider}")
    if telegram_token:
        print("  • Telegram Bot:  Configured & ready for `hex gateway start`")
    if discord_token:
        print("  • Discord Bot:   Configured & ready for `hex gateway start`")
    print(f"  • Storage Dir:   {data_dir}")
    print(f"  • Seeded Skills: {len(seeded)} verified skills registered")
    print()
    print("Commands:")
    print("  • Start Cockpit TUI:            hex   (or hex chat)")
    print("  • Start Messaging Gateways:     hex gateway start")
    print("  • Run autonomous coding task:   hex run \"Your goal here\"")
    print()

    return {
        "status": "ready",
        "config_file": str(config_file),
        "credentials_file": str(credentials_file),
        "cloud_provider": cloud_provider,
        "cloud_model_id": cloud_model_id,
        "model": cloud_model_id,
        "web_search_provider": web_search_provider,
        "data_dir": str(data_dir),
        "hardware": {
            "os": profile.os_name,
            "arch": profile.arch,
            "model": profile.machine_model,
            "ram_gb": profile.ram_gb,
            "cow": profile.cow_backend,
            "sandbox": profile.sandbox_backend,
        },
        "seeded_skills": len(seeded),
    }


def detect_project_stack(repo_root: Path | str) -> dict[str, Any]:
    """Scan repo_root for canonical stack marker files and return stack configuration."""
    root = Path(repo_root).resolve()
    if (
        (root / "pyproject.toml").is_file()
        or (root / "setup.py").is_file()
        or (root / "requirements.txt").is_file()
        or (root / "pytest.ini").is_file()
    ):
        return {
            "stack": "python",
            "test_cmd": "pytest",
            "src_dirs": ["src", "lib"],
            "watch_extensions": [".py"],
        }
    if (root / "package.json").is_file() or (root / "tsconfig.json").is_file():
        return {
            "stack": "node",
            "test_cmd": "npm test",
            "src_dirs": ["src", "lib"],
            "watch_extensions": [".js", ".ts", ".jsx", ".tsx"],
        }
    if (root / "Cargo.toml").is_file():
        return {
            "stack": "rust",
            "test_cmd": "cargo test",
            "src_dirs": ["src"],
            "watch_extensions": [".rs"],
        }
    if (root / "go.mod").is_file():
        return {
            "stack": "go",
            "test_cmd": "go test ./...",
            "src_dirs": ["."],
            "watch_extensions": [".go"],
        }
    return {
        "stack": "generic",
        "test_cmd": "make test",
        "src_dirs": ["."],
        "watch_extensions": [],
    }


def init_project(repo_root: Path | str, *, name: str | None = None, force: bool = False) -> dict[str, Any]:
    """Initialize project-scoped .hex configuration and gitignore based on detected stack."""
    repo = Path(repo_root).resolve()
    hex_dir = repo / ".hex"
    hex_dir.mkdir(parents=True, exist_ok=True)

    stack_info = detect_project_stack(repo)
    config_file = hex_dir / "config.yaml"
    gitignore_file = hex_dir / ".gitignore"

    proj_name = name or repo.name

    global_model = os.environ.get("HEX_CLOUD_MODEL_ID", "claude-opus-4-6-thinking")
    global_provider = os.environ.get("HEX_CLOUD_PROVIDER", "antigravity")

    created = False
    if not config_file.exists() or force:
        config_yaml = f"""# HEX Project Configuration
version: "1.0"
project:
  name: "{proj_name}"
  stack: "{stack_info['stack']}"
model:
  cloud_provider: "{global_provider}"
  cloud_model_id: "{global_model}"
  compressor_mode: "local"
verification:
  test_command: "{stack_info['test_cmd']}"
  timeout_seconds: 120
graphify:
  max_blast_depth: 2
  pinned_test_filter: "tests/"
sandbox:
  isolated: true
  allow_network: false
"""
        config_file.write_text(config_yaml, encoding="utf-8")
        created = True

    if not gitignore_file.exists():
        gitignore_content = """# HEX Project Ignored Data
/data/
tmp/
*.log
"""
        gitignore_file.write_text(gitignore_content, encoding="utf-8")

    reflections_dir = hex_dir / "reflections"
    reflections_dir.mkdir(parents=True, exist_ok=True)
    lessons_file = reflections_dir / "LESSONS.md"
    if not lessons_file.exists():
        lessons_file.write_text(
            f"# Project Reflections & Work Memory: {proj_name}\n\n## Lessons Learned\n",
            encoding="utf-8",
        )

    data_dir = hex_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    try:
        from .project_registry import ProjectRegistry

        ProjectRegistry().register_project(repo, name=proj_name, stack=stack_info["stack"])
    except Exception:
        pass

    return {
        "project_name": proj_name,
        "stack": stack_info["stack"],
        "test_command": stack_info["test_cmd"],
        "config_path": str(config_file),
        "gitignore_path": str(gitignore_file),
        "created": created,
    }
