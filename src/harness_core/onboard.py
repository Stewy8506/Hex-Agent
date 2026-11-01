"""HEX Onboarding Wizard (`hex onboard`).

Guides the operator through hardware discovery, snapshot & sandbox configuration,
model provider selection, credential storage, and ledger initialization.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .cow import get_default_snapshot_provider
from .orchestrator import HarnessOrchestrator
from .sandbox import get_default_sandbox


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
                    out = subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"], text=True).strip()
                    if out:
                        machine_model = out
                except Exception:
                    machine_model = "Apple Silicon Mac"

            try:
                mem_bytes = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip())
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

            # Check for nvidia-smi
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


def run_onboard_wizard(
    *,
    config_dir: Path | None = None,
    non_interactive: bool = False,
    defaults_only: bool = False,
) -> dict[str, Any]:
    """Execute the interactive or automated onboarding sequence for HEX."""
    cfg_dir = config_dir or (Path.home() / ".hex")
    cfg_dir.mkdir(parents=True, exist_ok=True)
    credentials_file = cfg_dir / "credentials.env"
    config_file = cfg_dir / "config.yaml"

    profile = HardwareProfile.detect()

    print(HEX_BANNER)
    print("Welcome to HEX — The Autonomous AI Workbench.")
    print("=" * 60)
    print(f"  OS / Architecture:  {profile.os_name} ({profile.arch})")
    print(f"  Hardware Model:     {profile.machine_model}")
    print(f"  System Memory:      {profile.ram_gb} GB RAM")
    print(f"  CoW Snapshot Mode:  {profile.cow_backend}")
    print(f"  Execution Sandbox:  {profile.sandbox_backend}")
    if profile.is_apple_silicon:
        print("  Apple Silicon MLX:  Accelerated (Metal Unified Memory available)")
    print("=" * 60)
    print()

    # Model configuration
    model_profile = "hybrid" if profile.is_apple_silicon else "cloud"
    anthropic_key = os.environ.get("HEX_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
    openai_key = os.environ.get("HEX_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    gemini_key = os.environ.get("HEX_GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY", "")

    if not non_interactive and not defaults_only:
        print("1. Select Model Routing Profile:")
        print("   [1] Hybrid (Local M4 Log Compressor + Cloud Frontier Reasoner - Recommended)")
        print("   [2] Local Only (100% private, Apple Silicon MLX / Ollama, zero API cost)")
        print("   [3] Cloud Frontier (Anthropic Claude, OpenAI GPT-4o, Google Gemini)")
        choice = input("Select profile [1/2/3, default=1]: ").strip()
        if choice == "2":
            model_profile = "local"
        elif choice == "3":
            model_profile = "cloud"
        else:
            model_profile = "hybrid"

        if model_profile in ("hybrid", "cloud") and not (anthropic_key or openai_key or gemini_key):
            print("\nEnter Cloud API Keys (leave blank to skip or configure later):")
            k_ant = input("Anthropic API Key (sk-ant-...): ").strip()
            if k_ant:
                anthropic_key = k_ant
            k_oai = input("OpenAI API Key (sk-...): ").strip()
            if k_oai:
                openai_key = k_oai
            k_gem = input("Gemini API Key: ").strip()
            if k_gem:
                gemini_key = k_gem

    # Write credentials securely
    creds_content = [
        "# HEX Secure Credentials Environment File",
        f"ANTHROPIC_API_KEY={anthropic_key}",
        f"OPENAI_API_KEY={openai_key}",
        f"GEMINI_API_KEY={gemini_key}",
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
  compressor_mode: "local"
  cloud_model_id: "claude-3-5-sonnet-20241022"
  local_model_id: "mlx-community/Qwen2.5-Coder-7B-Instruct-4bit"
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

    # Initialize ledger and seed built-in skills with SkillSpector
    repo_root = Path(__file__).resolve().parents[2]
    orch = HarnessOrchestrator(data_dir=data_dir, default_workspace=repo_root)
    skills_root = repo_root / "skills"
    seeded: list[dict[str, Any]] = []
    if skills_root.is_dir():
        for sub in sorted(skills_root.iterdir()):
            if (sub / "SKILL.md").is_file():
                res = orch.scanner.scan_and_register_skill(sub)
                seeded.append(res)

    print()
    print("✓ HEX has been successfully initialized!")
    print(f"  • Configuration: {config_file}")
    print(f"  • Credentials:   {credentials_file} (chmod 0600)")
    print(f"  • Data Store:    {data_dir}")
    print(f"  • Seeded Skills: {len(seeded)} verified skills registered")
    print()
    print("Quick Start:")
    print("  • Start Web Control Surface & API:  hex up")
    print("  • Run an autonomous coding task:     hex submit \"Your goal here\"")
    print("  • Start a distributed worker daemon: hex worker")
    print()

    return {
        "status": "ready",
        "config_file": str(config_file),
        "credentials_file": str(credentials_file),
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

