"""HEX Onboarding Wizard (`hex onboard`).

Guides the operator through hardware discovery, snapshot & sandbox configuration,
Google Antigravity OAuth quota detection, model provider selection, web search engine,
remote messaging gateways (Telegram & Discord), credential storage, and ledger initialization.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config_loader import load_hex_environment
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
                        ["sysctl", "-n", "machdep.cpu.brand_string"], text=True
                    ).strip()
                    if out:
                        machine_model = out
                except Exception:
                    machine_model = "Apple Silicon Mac"

            try:
                mem_bytes = int(
                    subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip()
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
    url = "https://cloudaicompanion.googleapis.com/v1internal:loadCodeAssist"
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


def run_onboard_wizard(
    *,
    config_dir: Path | None = None,
    non_interactive: bool = False,
    defaults_only: bool = False,
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
    ag_cred = ag_mgr.resolve_credential(auto_refresh=False)

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

    # Default configuration state
    model_profile = "antigravity_hybrid" if profile.is_apple_silicon else "antigravity"
    cloud_provider = "antigravity"
    cloud_model_id = "gemini-2.5-pro"

    anthropic_key = os.environ.get("HEX_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
    openai_key = os.environ.get("HEX_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    gemini_key = os.environ.get("HEX_GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY", "")
    antigravity_token = (
        os.environ.get("HEX_ANTIGRAVITY_TOKEN")
        or os.environ.get("ANTIGRAVITY_OAUTH_TOKEN")
        or (ag_cred.access_token if ag_cred else "")
    )

    web_search_provider = "duckduckgo"
    tavily_key = os.environ.get("HEX_TAVILY_API_KEY") or os.environ.get("TAVILY_API_KEY", "")
    brave_key = os.environ.get("HEX_BRAVE_API_KEY") or os.environ.get("BRAVE_API_KEY", "")

    telegram_token = os.environ.get("HEX_TELEGRAM_BOT_TOKEN", "")
    telegram_allowed = os.environ.get("HEX_TELEGRAM_ALLOWED_USERS", "")
    discord_token = os.environ.get("HEX_DISCORD_BOT_TOKEN", "")
    discord_webhook = os.environ.get("HEX_DISCORD_WEBHOOK_URL", "")

    if not non_interactive and not defaults_only:
        # Step 1: Model Provider Selection
        provider_options = [
            ("antigravity", "Connect Google Antigravity OAuth ($0 Day-1 Free Tier / Auto-detect IDE session)"),
            ("anthropic", "Enter Anthropic API Key (Claude 3.5 Sonnet / Opus)"),
            ("openai", "Enter OpenAI API Key (GPT-4o / o1 / o3-mini)"),
            ("gemini", "Enter Google Gemini API Key (Gemini 2.5 Pro / Flash)"),
            ("local", "Use Local Offline Model (Apple Silicon MLX / Ollama — Zero egress)"),
        ]
        p_idx = select_option(
            "Select Primary Model Provider & Brain:",
            provider_options,
            default_index=0,
        )
        selected_provider_key = provider_options[p_idx][0]

        if selected_provider_key == "antigravity":
            cloud_provider = "antigravity"
            model_profile = "antigravity_hybrid" if profile.is_apple_silicon else "antigravity"
            provider_title = "Google Antigravity"

            is_valid_token = _probe_antigravity_token(antigravity_token)
            if is_valid_token:
                print("  ✓ Active Google Antigravity session verified ($0 Day-1 Free Tier)")
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
            discovered_models = fetch_available_models("antigravity", api_key=antigravity_token)
            if discovered_models:
                m_idx = select_option(f"Select Primary {provider_title} Model:", discovered_models, default_index=0)
                cloud_model_id = discovered_models[m_idx][0]
            else:
                cloud_model_id = "gemini-2.5-pro"
            print()

        elif selected_provider_key == "anthropic":
            cloud_provider = "anthropic"
            model_profile = "hybrid" if profile.is_apple_silicon else "cloud"
            provider_title = "Anthropic"

            if not anthropic_key:
                anthropic_key = prompt_secret("Enter Anthropic API Key (sk-ant-...): ")

            print("  ◈ Querying available models from Anthropic...")
            discovered_models = fetch_available_models("anthropic", api_key=anthropic_key)
            if discovered_models:
                m_idx = select_option(f"Select Primary {provider_title} Model:", discovered_models, default_index=0)
                cloud_model_id = discovered_models[m_idx][0]
            else:
                cloud_model_id = "claude-3-5-sonnet"
            print()

        elif selected_provider_key == "openai":
            cloud_provider = "openai"
            model_profile = "hybrid" if profile.is_apple_silicon else "cloud"
            provider_title = "OpenAI"

            if not openai_key:
                openai_key = prompt_secret("Enter OpenAI API Key (sk-...): ")

            print("  ◈ Querying available models from OpenAI...")
            discovered_models = fetch_available_models("openai", api_key=openai_key)
            if discovered_models:
                m_idx = select_option(f"Select Primary {provider_title} Model:", discovered_models, default_index=0)
                cloud_model_id = discovered_models[m_idx][0]
            else:
                cloud_model_id = "gpt-4o"
            print()

        elif selected_provider_key == "gemini":
            cloud_provider = "gemini"
            model_profile = "hybrid" if profile.is_apple_silicon else "cloud"
            provider_title = "Google Gemini"

            if not gemini_key:
                gemini_key = prompt_secret("Enter Gemini API Key (AIza...): ")

            print("  ◈ Querying available models from Google Gemini...")
            discovered_models = fetch_available_models("gemini", api_key=gemini_key)
            if discovered_models:
                m_idx = select_option(f"Select Primary {provider_title} Model:", discovered_models, default_index=0)
                cloud_model_id = discovered_models[m_idx][0]
            else:
                cloud_model_id = "gemini-2.5-pro"
            print()

        elif selected_provider_key == "local":
            cloud_provider = "local"
            model_profile = "local"
            provider_title = "Local"

            print("  ◈ Checking local model runtimes...")
            discovered_models = fetch_available_models("local")
            if profile.is_apple_silicon:
                mlx_models = [
                    ("mlx-community/Qwen2.5-Coder-7B-Instruct-4bit", "mlx-community/Qwen2.5-Coder-7B-Instruct-4bit (Apple Silicon MLX)"),
                    ("mlx-community/Llama-3.2-3B-Instruct-4bit", "mlx-community/Llama-3.2-3B-Instruct-4bit (Fast MLX)"),
                ]
                all_local = mlx_models + discovered_models
            else:
                all_local = discovered_models

            if all_local:
                m_idx = select_option(f"Select Primary {provider_title} Model:", all_local, default_index=0)
                cloud_model_id = all_local[m_idx][0]
            else:
                cloud_model_id = "mlx-community/Qwen2.5-Coder-7B-Instruct-4bit"
            print("  ✓ Local offline profile configured. Zero external network egress.")
            print()

        print()

        # Step 2: Web Search Provider Selection
        search_options = [
            ("duckduckgo", "DuckDuckGo Search (100% Free, zero API key required, instant ready)"),
            ("tavily", "Tavily Search API (Optimized for AI agents & clean markdown extraction)"),
            ("brave", "Brave Search API (Fast, independent web search index)"),
            ("none", "Disabled"),
        ]
        s_idx = select_option(
            "Select Web Search Engine for Autonomous Research:",
            search_options,
            default_index=0,
        )
        web_search_provider = search_options[s_idx][0]

        if web_search_provider == "tavily" and not tavily_key:
            tavily_key = prompt_secret("  Enter Tavily API Key (tvly-...): ")
        elif web_search_provider == "brave" and not brave_key:
            brave_key = prompt_secret("  Enter Brave Search API Key (BSA...): ")

        print()

        # Step 3: Remote Messaging & Bot Gateways
        messaging_options = [
            ("skip", "Skip for now (Terminal Cockpit and Web Surface only)"),
            ("telegram", "Configure Telegram Bot (Telegram bot token + allowed user IDs)"),
            ("discord", "Configure Discord Bot (Discord bot token + channel webhook)"),
            ("both", "Configure Both (Telegram + Discord)"),
        ]
        m_idx = select_option(
            "Configure Remote Messaging Gateways (Control HEX from Phone):",
            messaging_options,
            default_index=0,
        )
        chosen_msg = messaging_options[m_idx][0]

        if chosen_msg in ("telegram", "both") and not telegram_token:
            telegram_token = prompt_secret("  Enter Telegram Bot Token: ")
            raw_users = input("  Allowed Telegram User IDs / Names (comma-separated, or Enter for all): ").strip()
            if raw_users:
                telegram_allowed = raw_users

        if chosen_msg in ("discord", "both") and not discord_token:
            discord_token = prompt_secret("  Enter Discord Bot Token: ")
            raw_hook = input("  Discord Webhook URL (optional, press Enter to skip): ").strip()
            if raw_hook:
                discord_webhook = raw_hook

        print()

    # Write credentials securely
    creds_content = [
        "# HEX Secure Credentials Environment File",
        f"HEX_CLOUD_PROVIDER={cloud_provider}",
        f"HEX_CLOUD_MODEL_ID={cloud_model_id}",
        f"HEX_ANTIGRAVITY_TOKEN={antigravity_token}",
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

    # Storage configuration
    db_backend = "sqlite"
    data_dir = cfg_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    # Write config.yaml
    config_yaml = f"""# HEX Autonomous AI Workbench Configuration
version: "1.0"
model:
  profile: "{model_profile}"
  cloud_provider: "{cloud_provider}"
  compressor_mode: "local"
  cloud_model_id: "{cloud_model_id}"
  local_model_id: "mlx-community/Qwen2.5-Coder-7B-Instruct-4bit"
web_search:
  provider: "{web_search_provider}"
messaging:
  telegram_enabled: {str(bool(telegram_token)).lower()}
  discord_enabled: {str(bool(discord_token)).lower()}
storage:
  backend: "{db_backend}"
  data_dir: "{data_dir}"
cow:
  backend: "{profile.cow_backend}"
sandbox:
  backend: "{profile.sandbox_backend}"
webhooks:
  dry_run: true
  public_base_url: "http://127.0.0.1:8420"
"""
    config_file.write_text(config_yaml, encoding="utf-8")

    # Hydrate current running process environment immediately
    load_hex_environment(config_dir=cfg_dir, override=True)

    # Initialize ledger and seed built-in skills with SkillSpector
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


def init_project(repo_root: Path | str, *, force: bool = False) -> dict[str, Any]:
    """Initialize project-scoped .hex configuration and gitignore based on detected stack."""
    repo = Path(repo_root).resolve()
    hex_dir = repo / ".hex"
    hex_dir.mkdir(parents=True, exist_ok=True)

    stack_info = detect_project_stack(repo)
    config_file = hex_dir / "config.yaml"
    gitignore_file = hex_dir / ".gitignore"

    created = False
    if not config_file.exists() or force:
        config_yaml = f"""# HEX Project Configuration
version: "1.0"
project:
  name: "{repo.name}"
  stack: "{stack_info['stack']}"
model:
  cloud_provider: "antigravity"
  cloud_model_id: "gemini-2.5-pro"
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

    return {
        "project_name": repo.name,
        "stack": stack_info["stack"],
        "test_command": stack_info["test_cmd"],
        "config_path": str(config_file),
        "gitignore_path": str(gitignore_file),
        "created": created,
    }
