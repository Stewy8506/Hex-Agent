"""Native Rust Ratatui Cockpit launcher and binary resolver for HEX."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, List, Optional


def find_repo_root(start_dir: Optional[Path] = None) -> Path:
    """Locate the git repository root directory."""
    curr = (start_dir or Path.cwd()).resolve()
    for parent in [curr] + list(curr.parents):
        if (parent / ".git").exists() or (parent / "Cargo.toml").exists() or (parent / "pyproject.toml").exists():
            return parent
    return curr


def find_hex_tui_binary(repo_root: Optional[Path] = None, release: bool = False) -> Optional[Path]:
    """Search candidate locations for the compiled hex-tui binary.

    Resolution order:
    1. HEX_TUI_BIN environment variable.
    2. repo_root / "target" / (release|debug) / "hex-tui"
    3. repo_root / "crates" / "hex-tui" / "target" / (release|debug) / "hex-tui"
    4. Alternate target profile (release or debug)
    5. Standard PATH lookup (shutil.which)
    """
    env_bin = os.environ.get("HEX_TUI_BIN")
    if env_bin:
        env_path = Path(env_bin).resolve()
        if env_path.is_file() and os.access(env_path, os.X_OK):
            return env_path

    root = repo_root or find_repo_root()
    profile = "release" if release else "debug"
    alt_profile = "debug" if release else "release"

    candidate_paths = [
        root / "target" / profile / "hex-tui",
        root / "crates" / "hex-tui" / "target" / profile / "hex-tui",
        root / "target" / alt_profile / "hex-tui",
        root / "crates" / "hex-tui" / "target" / alt_profile / "hex-tui",
    ]

    for p in candidate_paths:
        if p.is_file() and os.access(p, os.X_OK):
            return p

    which_path = shutil.which("hex-tui")
    if which_path:
        return Path(which_path).resolve()

    return None


def build_hex_tui(repo_root: Optional[Path] = None, release: bool = False) -> Path:
    """Compile the hex-tui binary on demand using cargo."""
    root = repo_root or find_repo_root()
    cargo_bin = shutil.which("cargo") or "cargo"

    cmd = [cargo_bin, "build", "-p", "hex-tui"]
    if release:
        cmd.append("--release")

    proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"Failed to build hex-tui via '{' '.join(cmd)}' (exit code {proc.returncode}):\n"
            f"STDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
        )

    bin_path = find_hex_tui_binary(root, release=release)
    if not bin_path:
        raise FileNotFoundError(
            f"cargo build succeeded but compiled hex-tui binary was not found under {root / 'target'}"
        )
    return bin_path


def find_or_build_hex_tui(
    repo_root: Optional[Path] = None, force_build: bool = False, release: bool = False
) -> Path:
    """Find existing hex-tui binary or compile it on demand."""
    root = repo_root or find_repo_root()
    if not force_build:
        found = find_hex_tui_binary(root, release=release)
        if found:
            return found
    return build_hex_tui(root, release=release)


def build_tui_cmd_args(args: Any, bin_path: Path) -> List[str]:
    """Construct command line arguments for spawning the hex-tui process."""
    cmd = [str(bin_path)]

    if getattr(args, "check", False):
        cmd.append("--check")

    project = getattr(args, "project", None)
    if project and project != "default":
        cmd.extend(["--project", str(project)])

    workspace = getattr(args, "workspace", None)
    if workspace:
        cmd.extend(["--workspace", str(workspace)])

    model = getattr(args, "model", None)
    if model:
        cmd.extend(["--model", str(model)])

    cow = getattr(args, "cow", None)
    if cow:
        cmd.extend(["--cow", str(cow)])

    session = getattr(args, "session", None)
    if session:
        cmd.extend(["--session", str(session)])

    return cmd


def launch_hex_tui(args: Any, repo_root: Optional[Path] = None) -> int:
    """Locate or build hex-tui, construct command line, and execute."""
    root = repo_root or find_repo_root()
    release_mode = bool(getattr(args, "release", False))
    force_build = bool(getattr(args, "build", False))

    bin_path = find_or_build_hex_tui(root, force_build=force_build, release=release_mode)
    cmd = build_tui_cmd_args(args, bin_path)

    # If --check is passed, print or forward exit code
    result = subprocess.run(cmd, check=False)
    return result.returncode
