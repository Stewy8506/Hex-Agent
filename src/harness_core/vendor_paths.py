"""Vendor resolution helper for HEX engines (Graphify, SkillSpector, Harness2).

Enables HEX to run seamlessly whether in a source checkout, installed via pip/pipx/wheel,
or installed into ~/.hex via install.sh.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def get_vendor_dir() -> Path:
    """Resolve the vendor directory across environment, package, repository, or user cache."""
    # 1. Environment variable override
    env_dir = os.environ.get("HEX_VENDOR_DIR") or os.environ.get("HARNESS_VENDOR_DIR")
    if env_dir:
        p = Path(env_dir).resolve()
        if p.is_dir():
            return p

    # 2. Bundled inside package (e.g. site-packages/harness_core/vendor)
    pkg_vendor = Path(__file__).resolve().parent / "vendor"
    if pkg_vendor.is_dir():
        return pkg_vendor

    # 3. Development git repository root (vendor/ at project root)
    repo_vendor = Path(__file__).resolve().parents[2] / "vendor"
    if repo_vendor.is_dir():
        return repo_vendor

    # 4. Check if current working directory or any parent has vendor/ (e.g. tests running in repo)
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        cand = parent / "vendor"
        if cand.is_dir() and ((cand / "graphify").is_dir() or (cand / "skillspector").is_dir()):
            return cand

    # 5. Walk up parents from this file to find repository root with vendor/
    for parent in Path(__file__).resolve().parents:
        cand = parent / "vendor"
        if cand.is_dir() and ((cand / "graphify").is_dir() or (cand / "skillspector").is_dir()):
            return cand

    # 6. User home cache: ~/.hex/vendor
    home_vendor = Path.home() / ".hex" / "vendor"
    if home_vendor.is_dir():
        return home_vendor

    # 7. Shared system directory: sys.prefix / share / hex / vendor
    sys_vendor = Path(sys.prefix) / "share" / "hex" / "vendor"
    if sys_vendor.is_dir():
        return sys_vendor

    # Fallback to repo candidate even if not existing yet
    return repo_vendor


def get_graphify_dir() -> Path:
    """Return the resolved path to upstream Graphify engine."""
    vendor_root = get_vendor_dir()
    candidate = vendor_root / "graphify"
    if (candidate / "graphify").is_dir():
        return candidate
    return candidate


def get_skillspector_dir() -> Path:
    """Return the resolved path to upstream NVIDIA SkillSpector src engine."""
    vendor_root = get_vendor_dir()
    candidate = vendor_root / "skillspector"
    if (candidate / "src").is_dir():
        return candidate / "src"
    return candidate


def get_harness2_dir() -> Path:
    """Return the resolved path to cloned Google Research Harness2."""
    vendor_root = get_vendor_dir()
    return vendor_root / "harness2"
