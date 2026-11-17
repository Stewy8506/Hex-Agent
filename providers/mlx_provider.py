"""Backward-compatible Apple Silicon MLX provider shim re-exporting LocalModelProvider and Tier1LogCompressor."""

from __future__ import annotations

import subprocess
import sys
from typing import Any

from .local_provider import (
    FailureRecord,
    LocalModelProvider,
    Tier1LogCompressor,
)


class LocalMLXModelProvider(LocalModelProvider):
    """Backward-compatible Apple Silicon MLX provider."""

    provider_id: str = "local_mlx"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._compressor = Tier1LogCompressor(mlx_available=True)

    def get_unified_memory_bytes(self) -> int | None:
        """Detect unified memory size in bytes on Darwin via sysctl, or None if unavailable."""
        if sys.platform != "darwin":
            return None
        try:
            res = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True,
                text=True,
                check=True,
                timeout=2,
            )
            return int(res.stdout.strip())
        except Exception:
            return None


__all__ = [
    "FailureRecord",
    "LocalMLXModelProvider",
    "LocalModelProvider",
    "Tier1LogCompressor",
]
