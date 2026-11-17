"""Host hardware profiling and default storage/sandbox selection."""

from __future__ import annotations

import platform
import subprocess
from dataclasses import dataclass
from pathlib import Path

from harness_core.storage.cow import get_default_snapshot_provider
from harness_core.storage.sandbox import get_default_sandbox


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
