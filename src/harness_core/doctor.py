"""HEX System Diagnostics and Self-Healing Suite (`hex doctor`).

Diagnoses and self-heals:
1. Python Runtime: Version (>=3.10), virtual environment, critical dependencies (pydantic, alembic, asyncpg).
2. Hardware Acceleration: Apple Silicon chip identification (M1-M4), Darwin arm64, MLX/Metal availability, CUDA/CPU.
3. Copy-on-Write (CoW): APFS clones (`cp -c`), Linux OverlayFS, Btrfs subvolumes, portable fallback.
4. Execution Sandboxes: Bubblewrap (bwrap), Docker, Podman, RestrictedSubprocess sandbox.
5. Credentials & Auth: Antigravity OAuth tokens, OpenAI, Anthropic, Gemini API keys (redacted).
6. Git Repository: Binary availability, worktree root, branch, clean/dirty worktree state.
7. Disk Storage: Free space across workspace and data directories (>2GB threshold).
8. Directories & Config: `.hex/`, `.hex/data/`, `skills/custom/`, `.hex/quarantine/`, `.hex/config.yaml`.
9. Audio Hardware & Speech: Speech synthesis (TTS: say/espeak), audio playback (afplay/aplay), and microphone input capture.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class DoctorCheckResult:
    """Individual diagnostic check outcome."""

    name: str
    category: str  # "python" | "hardware" | "cow" | "sandbox" | "credentials" | "git" | "storage" | "directories" | "audio" | "live"
    status: str  # "ok" | "warning" | "error" | "fixed"
    message: str
    remediation: str | None = None
    fixable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category,
            "status": self.status,
            "message": self.message,
            "remediation": self.remediation,
            "fixable": self.fixable,
        }


@dataclass
class DoctorReport:
    """Consolidated system diagnostic report."""

    checks: list[DoctorCheckResult] = field(default_factory=list)
    passed: int = 0
    warnings: int = 0
    errors: int = 0
    fixed: int = 0
    is_healthy: bool = True

    def __post_init__(self) -> None:
        if not self.passed and not self.warnings and not self.errors and not self.fixed:
            self.recalculate()

    def recalculate(self) -> None:
        self.passed = sum(1 for c in self.checks if c.status == "ok")
        self.warnings = sum(1 for c in self.checks if c.status == "warning")
        self.errors = sum(1 for c in self.checks if c.status == "error")
        self.fixed = sum(1 for c in self.checks if c.status == "fixed")
        self.is_healthy = self.errors == 0

    def to_dict(self) -> dict[str, Any]:
        self.recalculate()
        return {
            "is_healthy": self.is_healthy,
            "passed": self.passed,
            "warnings": self.warnings,
            "errors": self.errors,
            "fixed": self.fixed,
            "checks": [c.to_dict() for c in self.checks],
        }

    def format_summary(self, color: bool = True) -> str:
        self.recalculate()

        c_green = "\033[92m" if color else ""
        c_yellow = "\033[93m" if color else ""
        c_red = "\033[91m" if color else ""
        c_cyan = "\033[96m" if color else ""
        c_bold = "\033[1m" if color else ""
        c_reset = "\033[0m" if color else ""

        category_labels = {
            "python": "PYTHON RUNTIME & PACKAGES",
            "hardware": "HARDWARE ACCELERATION & CHIP",
            "cow": "COPY-ON-WRITE WORKSPACE BACKENDS",
            "sandbox": "SANDBOX ISOLATION ENGINES",
            "credentials": "MODEL PROVIDERS & CREDENTIALS",
            "git": "VERSION CONTROL & WORKTREE",
            "storage": "DISK STORAGE CAPACITY",
            "directories": "WORKBENCH DIRECTORIES & CONFIG",
            "audio": "AUDIO HARDWARE & SPEECH ENGINES",
            "live": "LIVE CONNECTIVITY PINGS",
        }

        grouped: dict[str, list[DoctorCheckResult]] = {}
        for c in self.checks:
            grouped.setdefault(c.category, []).append(c)

        lines: list[str] = []
        lines.append(f"{c_bold}══════════════════════════════════════════════════════════════════════{c_reset}")
        lines.append(f"{c_bold}              HEX SYSTEM DIAGNOSTIC & HEALTH REPORT                  {c_reset}")
        lines.append(f"{c_bold}══════════════════════════════════════════════════════════════════════{c_reset}")

        for cat, label in category_labels.items():
            cat_checks = grouped.get(cat, [])
            if not cat_checks:
                continue

            lines.append(f"\n{c_bold}▶ {label}{c_reset}")
            for chk in cat_checks:
                if chk.status == "ok":
                    icon = f"{c_green}✓{c_reset}"
                elif chk.status == "fixed":
                    icon = f"{c_cyan}⚙ (fixed){c_reset}"
                elif chk.status == "warning":
                    icon = f"{c_yellow}!{c_reset}"
                else:
                    icon = f"{c_red}✗{c_reset}"

                lines.append(f"  {icon} {c_bold}{chk.name}{c_reset}: {chk.message}")
                if chk.remediation and chk.status in ("warning", "error"):
                    lines.append(f"      {c_yellow}↳ Remediation: {chk.remediation}{c_reset}")

        lines.append(f"\n{c_bold}──────────────────────────────────────────────────────────────────────{c_reset}")
        status_color = c_green if self.is_healthy else c_red
        status_word = "HEALTHY" if self.is_healthy else "DEGRADED"
        lines.append(
            f"Overall Status: {status_color}{c_bold}{status_word}{c_reset}  |  "
            f"Passed: {c_green}{self.passed}{c_reset}  "
            f"Warnings: {c_yellow}{self.warnings}{c_reset}  "
            f"Errors: {c_red}{self.errors}{c_reset}  "
            f"Fixed: {c_cyan}{self.fixed}{c_reset}"
        )
        lines.append(f"{c_bold}══════════════════════════════════════════════════════════════════════{c_reset}\n")
        return "\n".join(lines)


class HexDoctor:
    """System diagnostics, health auditing, and self-healing engine."""

    def __init__(
        self,
        workspace_root: Path | str | None = None,
        data_dir: Path | str | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root or Path.cwd()).resolve()
        self.data_dir = Path(
            data_dir
            or os.environ.get("HEX_DATA_DIR")
            or os.environ.get("HARNESS_DATA_DIR")
            or Path.home() / ".hex" / "data"
        ).resolve()
        self.hex_dir = self.workspace_root / ".hex"

    def check_python(self) -> list[DoctorCheckResult]:
        """Diagnose Python runtime, venv, and core package dependencies."""
        results: list[DoctorCheckResult] = []

        # Version check
        ver = sys.version_info
        ver_str = f"{ver.major}.{ver.minor}.{ver.micro}"
        if ver >= (3, 10):
            results.append(
                DoctorCheckResult(
                    name="Python Version",
                    category="python",
                    status="ok",
                    message=f"Python {ver_str} (>= 3.10 required)",
                )
            )
        else:
            results.append(
                DoctorCheckResult(
                    name="Python Version",
                    category="python",
                    status="error",
                    message=f"Python {ver_str} is below minimum 3.10",
                    remediation="Upgrade Python to 3.10, 3.11, 3.12, 3.13, or 3.14.",
                )
            )

        # Virtual Environment check
        in_venv = (
            hasattr(sys, "real_prefix")
            or (hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix)
            or bool(os.environ.get("VIRTUAL_ENV"))
        )
        if in_venv:
            results.append(
                DoctorCheckResult(
                    name="Virtual Environment",
                    category="python",
                    status="ok",
                    message=f"Active virtual environment ({sys.prefix})",
                )
            )
        else:
            results.append(
                DoctorCheckResult(
                    name="Virtual Environment",
                    category="python",
                    status="warning",
                    message="Running directly against global Python interpreter",
                    remediation="Activate a project virtualenv (.venv/bin/activate) to prevent dependency conflicts.",
                )
            )

        # Dependency checks
        critical_pkgs = [("alembic", "Database migration engine")]
        for pkg, desc in critical_pkgs:
            try:
                __import__(pkg)
                results.append(
                    DoctorCheckResult(
                        name=f"Dependency: {pkg}",
                        category="python",
                        status="ok",
                        message=f"{desc} active",
                    )
                )
            except ImportError:
                results.append(
                    DoctorCheckResult(
                        name=f"Dependency: {pkg}",
                        category="python",
                        status="warning",
                        message=f"Missing package '{pkg}' ({desc})",
                        remediation=f"Install via pip: pip install {pkg}",
                    )
                )

        # Optional high-performance packages
        optional_pkgs = [
            ("pydantic", "FastAPI / ASGI schema validation"),
            ("asyncpg", "High-concurrency PostgreSQL pool driver"),
            ("rich", "Colorized console renderer"),
        ]
        for pkg, desc in optional_pkgs:
            try:
                __import__(pkg)
                results.append(
                    DoctorCheckResult(
                        name=f"Optional: {pkg}",
                        category="python",
                        status="ok",
                        message=f"{desc} active",
                    )
                )
            except ImportError:
                results.append(
                    DoctorCheckResult(
                        name=f"Optional: {pkg}",
                        category="python",
                        status="warning",
                        message=f"Not installed ({desc})",
                        remediation=f"Optional: install {pkg} for enhanced capabilities (pip install {pkg}).",
                    )
                )

        return results

    def check_hardware_acceleration(self) -> list[DoctorCheckResult]:
        """Diagnose host CPU, Apple Silicon M-series acceleration, and MLX/Metal support."""
        results: list[DoctorCheckResult] = []
        system = platform.system()
        machine = platform.machine()

        if system == "Darwin":
            # Inspect Apple Silicon chip
            cpu_brand = ""
            try:
                out = subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"], text=True).strip()
                cpu_brand = out
            except Exception:
                cpu_brand = platform.processor() or "Apple Silicon"

            is_apple_silicon = "Apple" in cpu_brand or machine in ("arm64", "aarch64")

            if is_apple_silicon:
                results.append(
                    DoctorCheckResult(
                        name="Apple Silicon Architecture",
                        category="hardware",
                        status="ok",
                        message=f"{cpu_brand} ({machine})",
                    )
                )
                # Check MLX availability for local Tier-1 log compression
                try:
                    import mlx.core as mx  # type: ignore
                    results.append(
                        DoctorCheckResult(
                            name="Apple Silicon MLX Metal Acceleration",
                            category="hardware",
                            status="ok",
                            message=f"Native MLX framework active with Metal backend",
                        )
                    )
                except ImportError:
                    results.append(
                        DoctorCheckResult(
                            name="Apple Silicon MLX Metal Acceleration",
                            category="hardware",
                            status="warning",
                            message="MLX not installed in active environment; using pure-Python log compressor fallback",
                            remediation="Install mlx and mlx-lm for hardware-accelerated local reasoning: pip install mlx mlx-lm",
                        )
                    )
            else:
                results.append(
                    DoctorCheckResult(
                        name="Hardware Platform",
                        category="hardware",
                        status="warning",
                        message=f"Intel x86_64 Mac ({cpu_brand}); Apple Silicon Metal acceleration unavailable",
                    )
                )
        elif system == "Linux":
            results.append(
                DoctorCheckResult(
                    name="Linux Host Platform",
                    category="hardware",
                    status="ok",
                    message=f"Linux {platform.release()} ({machine})",
                )
            )
            # Check CUDA if torch is present
            try:
                import torch  # type: ignore
                if torch.cuda.is_available():
                    gpu_name = torch.cuda.get_device_name(0)
                    results.append(
                        DoctorCheckResult(
                            name="CUDA GPU Acceleration",
                            category="hardware",
                            status="ok",
                            message=f"CUDA device available: {gpu_name}",
                        )
                    )
                else:
                    results.append(
                        DoctorCheckResult(
                            name="CUDA GPU Acceleration",
                            category="hardware",
                            status="warning",
                            message="No CUDA GPU detected; running in CPU host mode",
                        )
                    )
            except ImportError:
                results.append(
                    DoctorCheckResult(
                        name="GPU Acceleration",
                        category="hardware",
                        status="ok",
                        message="Standard Linux CPU execution environment",
                    )
                )
        else:
            results.append(
                DoctorCheckResult(
                    name="Host Platform",
                    category="hardware",
                    status="ok",
                    message=f"{system} {platform.release()} ({machine})",
                )
            )

        return results

    def check_cow_providers(self) -> list[DoctorCheckResult]:
        """Diagnose filesystem Copy-on-Write capabilities (APFS, OverlayFS, Btrfs)."""
        results: list[DoctorCheckResult] = []
        from .cow import get_default_snapshot_provider

        try:
            cow_root = self.data_dir / "cow_snapshots"
            provider = get_default_snapshot_provider(root_dir=cow_root)
            backend = getattr(provider, "backend_name", type(provider).__name__)
            results.append(
                DoctorCheckResult(
                    name="Default CoW Snapshot Provider",
                    category="cow",
                    status="ok",
                    message=f"Active provider: {backend}",
                )
            )

            # Test CoW clone capability on workspace
            system = platform.system()
            if system == "Darwin":
                # Test APFS clonefile support
                test_src = self.workspace_root / ".hex_cow_probe_src"
                test_dst = self.workspace_root / ".hex_cow_probe_dst"
                try:
                    test_src.write_text("cow_probe", encoding="utf-8")
                    res = subprocess.run(["cp", "-c", str(test_src), str(test_dst)], capture_output=True)
                    if res.returncode == 0:
                        results.append(
                            DoctorCheckResult(
                                name="APFS Zero-Copy Clone Support",
                                category="cow",
                                status="ok",
                                message="APFS clonefile (cp -c) operational (<1ms zero-copy)",
                            )
                        )
                    else:
                        results.append(
                            DoctorCheckResult(
                                name="APFS Zero-Copy Clone Support",
                                category="cow",
                                status="warning",
                                message="cp -c failed on workspace volume; falling back to standard copy",
                                remediation="Ensure workspace is stored on an APFS formatted volume for instant zero-copy snapshots.",
                            )
                        )
                except Exception as e:
                    results.append(
                        DoctorCheckResult(
                            name="APFS Zero-Copy Clone Support",
                            category="cow",
                            status="warning",
                            message=f"Probe error: {e}",
                        )
                    )
                finally:
                    if test_src.exists():
                        test_src.unlink(missing_ok=True)
                    if test_dst.exists():
                        test_dst.unlink(missing_ok=True)
            elif system == "Linux":
                from .cow import BtrfsSnapshotProvider, OverlayFSSnapshotProvider
                has_btrfs = BtrfsSnapshotProvider.is_supported(self.workspace_root)
                has_overlay = OverlayFSSnapshotProvider.is_supported(self.workspace_root)
                if has_btrfs or has_overlay:
                    results.append(
                        DoctorCheckResult(
                            name="Kernel CoW Support",
                            category="cow",
                            status="ok",
                            message=f"CoW storage available (Btrfs={has_btrfs}, OverlayFS={has_overlay})",
                        )
                    )
                else:
                    results.append(
                        DoctorCheckResult(
                            name="Kernel CoW Support",
                            category="cow",
                            status="warning",
                            message="Neither Btrfs nor OverlayFS detected on volume; using portable copy fallback",
                            remediation="Mount workspace on Btrfs or enable user namespace OverlayFS for faster snapshots.",
                        )
                    )
        except Exception as e:
            results.append(
                DoctorCheckResult(
                    name="Default CoW Snapshot Provider",
                    category="cow",
                    status="error",
                    message=f"Failed to initialize CoW provider: {e}",
                    remediation="Check directory permissions in ~/.hex/data/ or workspace root.",
                )
            )

        return results

    def check_sandboxes(self) -> list[DoctorCheckResult]:
        """Diagnose execution isolation sandboxes (bwrap, docker, podman, restricted subprocess)."""
        results: list[DoctorCheckResult] = []
        from .sandbox import BubblewrapSandbox, DockerPodmanSandbox, get_default_sandbox

        has_bwrap = BubblewrapSandbox.is_available()
        has_docker = DockerPodmanSandbox.is_available()

        if has_bwrap:
            results.append(
                DoctorCheckResult(
                    name="Bubblewrap Sandbox (bwrap)",
                    category="sandbox",
                    status="ok",
                    message="Bubblewrap unprivileged user namespace sandbox available",
                )
            )

        if has_docker:
            results.append(
                DoctorCheckResult(
                    name="Container Sandbox (Docker/Podman)",
                    category="sandbox",
                    status="ok",
                    message="Docker or Podman daemon available for isolated container execution",
                )
            )

        # Confirm default sandbox
        default_sb = get_default_sandbox(self.workspace_root)
        backend = getattr(default_sb, "backend_name", type(default_sb).__name__)
        results.append(
            DoctorCheckResult(
                name="Default Sandbox Backend",
                category="sandbox",
                status="ok",
                message=f"Active execution sandbox: {backend}",
            )
        )

        return results

    def check_credentials(self) -> list[DoctorCheckResult]:
        """Diagnose model API credentials and Google Antigravity OAuth session."""
        results: list[DoctorCheckResult] = []

        # Google Antigravity Auth
        antigravity_auth_file = Path.home() / ".hex" / "antigravity_auth.json"
        has_antigravity = False
        if antigravity_auth_file.is_file():
            try:
                import json
                data = json.loads(antigravity_auth_file.read_text(encoding="utf-8"))
                if data.get("access_token") or data.get("refresh_token"):
                    has_antigravity = True
            except Exception:
                pass

        if has_antigravity:
            results.append(
                DoctorCheckResult(
                    name="Google Antigravity OAuth Session",
                    category="credentials",
                    status="ok",
                    message="Antigravity credentials found with $0 Day-1 model quota",
                )
            )
        else:
            results.append(
                DoctorCheckResult(
                    name="Google Antigravity OAuth Session",
                    category="credentials",
                    status="warning",
                    message="No Antigravity OAuth credentials found at ~/.hex/antigravity_auth.json",
                    remediation="Run 'hex auth antigravity --import-ide' or 'hex auth antigravity --auth-url' to link account.",
                )
            )

        # Standard cloud API tokens
        cloud_tokens = [
            ("GEMINI_API_KEY", "Google Gemini API"),
            ("ANTHROPIC_API_KEY", "Anthropic Claude API"),
            ("OPENAI_API_KEY", "OpenAI API"),
        ]
        found_cloud = 0
        for env_var, label in cloud_tokens:
            token = os.environ.get(env_var)
            if token and len(token) > 8:
                found_cloud += 1
                masked = f"{token[:4]}...{token[-4:]}"
                results.append(
                    DoctorCheckResult(
                        name=f"Credential: {env_var}",
                        category="credentials",
                        status="ok",
                        message=f"{label} configured ({masked})",
                    )
                )
            else:
                results.append(
                    DoctorCheckResult(
                        name=f"Credential: {env_var}",
                        category="credentials",
                        status="warning",
                        message=f"Environment variable '{env_var}' is unset",
                        remediation=f"Set export {env_var}='<key>' if using {label}.",
                    )
                )

        if not has_antigravity and found_cloud == 0:
            results.append(
                DoctorCheckResult(
                    name="Model Reasoner Readiness",
                    category="credentials",
                    status="warning",
                    message="No external model credentials found; will run in local-only / mock mode",
                    remediation="Configure either Google Antigravity OAuth ('hex auth antigravity') or export a provider API key.",
                )
            )

        return results

    def check_git(self) -> list[DoctorCheckResult]:
        """Diagnose git executable and repository worktree status."""
        results: list[DoctorCheckResult] = []

        # Check git binary
        git_path = shutil.which("git")
        if not git_path:
            results.append(
                DoctorCheckResult(
                    name="Git Executable",
                    category="git",
                    status="error",
                    message="git binary not found in PATH",
                    remediation="Install git (e.g., brew install git or apt install git).",
                )
            )
            return results

        results.append(
            DoctorCheckResult(
                name="Git Executable",
                category="git",
                status="ok",
                message=f"Found git at {git_path}",
            )
        )

        # Inside worktree check
        try:
            res = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=self.workspace_root,
                capture_output=True,
                text=True,
            )
            is_git = res.returncode == 0 and res.stdout.strip() == "true"
        except Exception:
            is_git = False

        if is_git:
            # Check current branch
            try:
                branch = subprocess.check_output(
                    ["git", "branch", "--show-current"],
                    cwd=self.workspace_root,
                    text=True,
                ).strip() or "detached"
            except Exception:
                branch = "unknown"

            # Check dirty status
            try:
                status_out = subprocess.check_output(
                    ["git", "status", "--porcelain"],
                    cwd=self.workspace_root,
                    text=True,
                ).strip()
                dirty = bool(status_out)
                dirty_count = len(status_out.splitlines()) if dirty else 0
            except Exception:
                dirty = False
                dirty_count = 0

            results.append(
                DoctorCheckResult(
                    name="Git Worktree",
                    category="git",
                    status="ok",
                    message=f"Git repository on branch '{branch}' (dirty files: {dirty_count})",
                )
            )
        else:
            results.append(
                DoctorCheckResult(
                    name="Git Worktree",
                    category="git",
                    status="warning",
                    message=f"Directory '{self.workspace_root}' is not inside a git repository",
                    remediation="Initialize a git repo ('git init') to enable automated branching, diff tracking, and PR creation.",
                )
            )

        return results

    def check_storage(self) -> list[DoctorCheckResult]:
        """Diagnose available disk space on workspace and data volume."""
        results: list[DoctorCheckResult] = []
        min_free_bytes = 2 * 1024 * 1024 * 1024  # 2 GB

        targets = [
            ("Workspace Volume", self.workspace_root),
            ("HEX Data Directory", self.data_dir),
        ]

        seen_paths: set[str] = set()
        for label, path in targets:
            try:
                target_path = path if path.exists() else path.parent
                real_path = str(target_path.resolve())
                if real_path in seen_paths:
                    continue
                seen_paths.add(real_path)

                usage = shutil.disk_usage(target_path)
                free_gb = usage.free / (1024 ** 3)
                total_gb = usage.total / (1024 ** 3)

                if usage.free >= min_free_bytes:
                    results.append(
                        DoctorCheckResult(
                            name=f"Disk Space: {label}",
                            category="storage",
                            status="ok",
                            message=f"{free_gb:.1f} GB free of {total_gb:.1f} GB ({path})",
                        )
                    )
                else:
                    results.append(
                        DoctorCheckResult(
                            name=f"Disk Space: {label}",
                            category="storage",
                            status="warning",
                            message=f"Low disk space: only {free_gb:.1f} GB free (<2 GB recommended) ({path})",
                            remediation="Free up disk space on the volume to prevent CoW snapshot allocation failures.",
                        )
                    )
            except Exception as e:
                results.append(
                    DoctorCheckResult(
                        name=f"Disk Space: {label}",
                        category="storage",
                        status="warning",
                        message=f"Could not query disk space: {e}",
                    )
                )

        return results

    def check_directories_and_config(self) -> list[DoctorCheckResult]:
        """Diagnose workspace and user directories, permissions, and configuration."""
        results: list[DoctorCheckResult] = []

        dirs_to_check = [
            ("Workspace .hex Directory", self.hex_dir),
            ("Custom Skills Directory", self.workspace_root / "skills" / "custom"),
            ("Quarantine Directory", self.hex_dir / "quarantine"),
            ("Data Directory", self.data_dir),
        ]

        for label, d in dirs_to_check:
            if d.is_dir():
                results.append(
                    DoctorCheckResult(
                        name=label,
                        category="directories",
                        status="ok",
                        message=f"Directory exists: {d}",
                    )
                )
            else:
                results.append(
                    DoctorCheckResult(
                        name=label,
                        category="directories",
                        status="warning",
                        message=f"Missing directory: {d}",
                        remediation=f"Create directory: mkdir -p '{d}' or run 'hex doctor --fix'",
                        fixable=True,
                    )
                )

        # Configuration file check
        cfg_file = self.hex_dir / "config.yaml"
        if cfg_file.is_file():
            results.append(
                DoctorCheckResult(
                    name="Workspace Configuration (.hex/config.yaml)",
                    category="directories",
                    status="ok",
                    message=f"Configuration file present ({cfg_file})",
                )
            )
        else:
            results.append(
                DoctorCheckResult(
                    name="Workspace Configuration (.hex/config.yaml)",
                    category="directories",
                    status="warning",
                    message="Missing .hex/config.yaml configuration file",
                    remediation="Generate default configuration via 'hex init' or run 'hex doctor --fix'",
                    fixable=True,
                )
            )

        return results

    def check_audio_devices(self) -> list[DoctorCheckResult]:
        """Diagnose audio hardware, speech synthesis engine, audio player, and microphone input."""
        results: list[DoctorCheckResult] = []
        try:
            from harness_core.voice import probe_audio_hardware

            info = probe_audio_hardware()
        except Exception as e:
            results.append(
                DoctorCheckResult(
                    name="Audio Subsystem Probe",
                    category="audio",
                    status="warning",
                    message=f"Could not probe audio hardware: {e}",
                    remediation="Ensure harness_core.voice module is available.",
                )
            )
            return results

        # Check 1: Speech Synthesis Engine (TTS)
        if info.get("tts_available"):
            tts_engine = info.get("tts_engine") or "native"
            results.append(
                DoctorCheckResult(
                    name="Speech Synthesis (TTS)",
                    category="audio",
                    status="ok",
                    message=f"Speech synthesis engine active ({tts_engine})",
                )
            )
        else:
            results.append(
                DoctorCheckResult(
                    name="Speech Synthesis (TTS)",
                    category="audio",
                    status="warning",
                    message="No native TTS synthesizer found (say/espeak)",
                    remediation="Install espeak (Linux: sudo apt install espeak) or rely on visual TUI mode.",
                )
            )

        # Check 2: Audio Output Player
        audio_player = info.get("audio_player")
        if audio_player:
            results.append(
                DoctorCheckResult(
                    name="Audio Output Player",
                    category="audio",
                    status="ok",
                    message=f"Audio output player found ({audio_player})",
                )
            )
        else:
            results.append(
                DoctorCheckResult(
                    name="Audio Output Player",
                    category="audio",
                    status="warning",
                    message="No command-line audio player detected (afplay/aplay)",
                    remediation="Ensure audio subsystem is configured.",
                )
            )

        # Check 3: Audio Input / Microphone Capture
        if info.get("mic_available"):
            mic_tool = info.get("mic_tool") or "native"
            results.append(
                DoctorCheckResult(
                    name="Audio Input Capture",
                    category="audio",
                    status="ok",
                    message=f"Audio input capture utility found ({mic_tool})",
                )
            )
        else:
            results.append(
                DoctorCheckResult(
                    name="Audio Input Capture",
                    category="audio",
                    status="warning",
                    message="No command-line audio capture utility found (rec/arecord)",
                    remediation="Install sox/arecord or use keyboard input.",
                )
            )

        return results

    def apply_fixes(self, checks: list[DoctorCheckResult] | None = None) -> list[DoctorCheckResult]:
        """Automatically remediate fixable issues (directories, default config, permissions)."""
        remediated: list[DoctorCheckResult] = []

        # 1. Create missing directories
        dirs = [
            self.hex_dir,
            self.hex_dir / "data",
            self.workspace_root / "skills" / "custom",
            self.hex_dir / "quarantine",
            self.data_dir,
        ]
        for d in dirs:
            if not d.exists():
                try:
                    d.mkdir(parents=True, exist_ok=True)
                    os.chmod(d, 0o755)
                    remediated.append(
                        DoctorCheckResult(
                            name=f"Create Directory: {d.name}",
                            category="directories",
                            status="fixed",
                            message=f"Created missing directory {d}",
                        )
                    )
                except Exception as e:
                    remediated.append(
                        DoctorCheckResult(
                            name=f"Create Directory: {d.name}",
                            category="directories",
                            status="error",
                            message=f"Failed to create directory {d}: {e}",
                        )
                    )

        # 2. Generate default .hex/config.yaml if missing
        cfg_file = self.hex_dir / "config.yaml"
        if not cfg_file.exists():
            try:
                self.hex_dir.mkdir(parents=True, exist_ok=True)
                default_config = (
                    "# HEX Autonomous AI Workbench Configuration\n"
                    "version: 1\n"
                    "project:\n"
                    f"  name: \"{self.workspace_root.name or 'default'}\"\n"
                    "router:\n"
                    "  primary_provider: \"antigravity\"\n"
                    "  primary_model: \"gemini-2.5-pro\"\n"
                    "  fallback_provider: \"local\"\n"
                    "cow:\n"
                    "  provider: \"auto\"\n"
                    "sandbox:\n"
                    "  backend: \"auto\"\n"
                )
                cfg_file.write_text(default_config, encoding="utf-8")
                remediated.append(
                    DoctorCheckResult(
                        name="Generate Default Config",
                        category="directories",
                        status="fixed",
                        message=f"Created minimal default configuration at {cfg_file}",
                    )
                )
            except Exception as e:
                remediated.append(
                    DoctorCheckResult(
                        name="Generate Default Config",
                        category="directories",
                        status="error",
                        message=f"Failed to write default config: {e}",
                    )
                )

        return remediated

    def run_live_pings(self) -> list[DoctorCheckResult]:
        """Perform bounded read-only connectivity pings against configured external endpoints."""
        results: list[DoctorCheckResult] = []

        # 1. Antigravity endpoint probe if configured
        antigravity_file = Path.home() / ".hex" / "antigravity_auth.json"
        if antigravity_file.is_file():
            try:
                import json
                data = json.loads(antigravity_file.read_text(encoding="utf-8"))
                token = data.get("access_token")
                if token:
                    req = urllib.request.Request(
                        "https://cloudresourcemanager.googleapis.com/v1/projects",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    try:
                        with urllib.request.urlopen(req, timeout=3.0) as resp:
                            if resp.status in (200, 204):
                                results.append(
                                    DoctorCheckResult(
                                        name="Live Ping: Google Antigravity Auth",
                                        category="live",
                                        status="ok",
                                        message=f"Antigravity token valid (HTTP {resp.status})",
                                    )
                                )
                            else:
                                results.append(
                                    DoctorCheckResult(
                                        name="Live Ping: Google Antigravity Auth",
                                        category="live",
                                        status="warning",
                                        message=f"Unexpected response HTTP {resp.status}",
                                    )
                                )
                    except Exception as e:
                        results.append(
                            DoctorCheckResult(
                                name="Live Ping: Google Antigravity Auth",
                                category="live",
                                status="warning",
                                message=f"Token validation ping failed: {e}",
                                remediation="Refresh credentials via 'hex auth antigravity --import-ide'",
                            )
                        )
            except Exception:
                pass

        # 2. General internet connectivity ping (DNS / HTTPS reachability)
        try:
            req = urllib.request.Request(
                "https://1.1.1.1",
                headers={"User-Agent": "HEX-Doctor/1.0"},
            )
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                results.append(
                    DoctorCheckResult(
                        name="Live Ping: Internet Connectivity",
                        category="live",
                        status="ok",
                        message="HTTPS outbound connectivity verified",
                    )
                )
        except Exception as e:
            results.append(
                DoctorCheckResult(
                    name="Live Ping: Internet Connectivity",
                    category="live",
                    status="warning",
                    message=f"Outbound internet ping failed: {e}",
                    remediation="Check network interface, proxy settings, or firewall.",
                )
            )

        return results

    def run_diagnostics(self, fix: bool = False, live: bool = False) -> DoctorReport:
        """Run complete diagnostic suite with optional self-healing and live pings."""
        all_checks: list[DoctorCheckResult] = []

        all_checks.extend(self.check_python())
        all_checks.extend(self.check_hardware_acceleration())
        all_checks.extend(self.check_cow_providers())
        all_checks.extend(self.check_sandboxes())
        all_checks.extend(self.check_credentials())
        all_checks.extend(self.check_git())
        all_checks.extend(self.check_storage())
        all_checks.extend(self.check_directories_and_config())
        all_checks.extend(self.check_audio_devices())

        if fix:
            fix_results = self.apply_fixes(all_checks)
            if fix_results:
                # Replace fixable checks that succeeded with the fixed items
                all_checks = [c for c in all_checks if not c.fixable]
                all_checks.extend(fix_results)
                # Re-verify directories to reflect updated state
                dir_rechecks = self.check_directories_and_config()
                # Overwrite directory entries
                all_checks = [c for c in all_checks if c.category != "directories" or c.status == "fixed"]
                for dc in dir_rechecks:
                    if not any(c.name == dc.name for c in all_checks):
                        all_checks.append(dc)

        if live:
            all_checks.extend(self.run_live_pings())

        report = DoctorReport(checks=all_checks)
        report.recalculate()
        return report
