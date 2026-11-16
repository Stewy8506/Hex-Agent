"""Build hook: bundle the vendored runtime engines into the wheel.

The install contract (F03) requires `pip install hex-ai` to yield a working
CLI outside any source checkout. harness_core.vendor_paths already resolves
a package-bundled vendor/ first; this hook populates it at build time with
the runtime engines only (no tests, docs, assets, or caches).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py

ROOT = Path(__file__).resolve().parent

TRIM_DIRS = {"tests", "docs", "assets", "worked", "__pycache__", ".git", ".venv", "node_modules"}
TRIM_SUFFIXES = (".pyc", ".pyo")
TRIM_FILES = {"uv.lock"}


def _keep(entry: Path) -> bool:
    if entry.is_dir():
        return entry.name not in TRIM_DIRS
    return entry.suffix not in TRIM_SUFFIXES and entry.name not in TRIM_FILES


def _copy_vendor(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for entry in sorted(src.iterdir()):
        if not _keep(entry):
            continue
        target = dst / entry.name
        if entry.is_dir():
            _copy_vendor(entry, target)
        else:
            shutil.copy2(entry, target)


class BuildPyWithVendor(build_py):
    def run(self) -> None:
        super().run()
        vendor_src = ROOT / "vendor"
        if vendor_src.is_dir():
            _copy_vendor(vendor_src, Path(self.build_lib) / "harness_core" / "vendor")


setup(cmdclass={"build_py": BuildPyWithVendor})