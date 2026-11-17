"""Shared system diagnostics for host runtime, hardware, storage, and isolation."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
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


class SystemDiagnostics:
    """System checks shared by the doctor command and support dumps."""

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
            except Exception as exc:
                results.append(
                    DoctorCheckResult(
                        name=f"Dependency: {pkg}",
                        category="python",
                        status="warning",
                        message=f"Missing or broken package '{pkg}' ({desc}): {exc}",
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
            except Exception:
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
                            name="Local AI Acceleration (Metal / MLX)",
                            category="hardware",
                            status="ok",
                            message=f"Native MLX framework active with Metal backend",
                        )
                    )
                except ImportError:
                    results.append(
                        DoctorCheckResult(
                            name="Local AI Acceleration (Metal / MLX)",
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
        from harness_core.storage.cow import get_default_snapshot_provider

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
                from harness_core.storage.cow import BtrfsSnapshotProvider, OverlayFSSnapshotProvider
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
        from harness_core.storage.sandbox import BubblewrapSandbox, DockerPodmanSandbox, get_default_sandbox

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
