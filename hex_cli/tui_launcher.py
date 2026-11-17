"""Native Rust Ratatui Cockpit launcher and binary resolver for HEX.

Install contract (F03): the supported native delivery path is a release
artifact (or install.sh) placing the binary at ~/.hex/bin/hex-tui. Source
builds are an explicit developer option (--build / HEX_DEV_BUILD=1), never
an implicit rescue for a missing installation.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, List, Optional

BINARY_NAME = "hex-tui.exe" if os.name == "nt" else "hex-tui"

logger = logging.getLogger(__name__)


def installed_binary_dir() -> Path:
    """Directory where install.sh / release artifacts place native clients."""
    return Path.home() / ".hex" / "bin"


def native_missing_message(binary: str = "hex-tui") -> str:
    return (
        f"The native {binary} cockpit is not installed. HEX looked in "
        f"$HEX_TUI_BIN, this checkout's target/ directory, "
        f"{installed_binary_dir()}, PATH, and ~/.local/bin.\n"
        f"  • Install it: ./install.sh (downloads the release artifact for your platform)\n"
        f"  • Or download '{binary}' from the GitHub release and place it in {installed_binary_dir()}\n"
        f"  • Developer option: cargo build --release -p {binary}, then set HEX_TUI_BIN or re-run install.sh\n"
        f"  • The Python console remains fully supported: hex --legacy"
    )


def find_repo_root(start_dir: Optional[Path] = None) -> Path:
    """Locate the git repository root directory."""
    curr = (start_dir or Path.cwd()).resolve()
    for parent in [curr] + list(curr.parents):
        if (parent / ".git").exists() or (parent / "Cargo.toml").exists() or (parent / "pyproject.toml").exists():
            return parent
    for parent in Path(__file__).resolve().parents:
        if (parent / "Cargo.toml").exists() or (parent / "pyproject.toml").exists():
            return parent
    return curr


def find_hex_tui_binary(repo_root: Optional[Path] = None, release: bool = False) -> Optional[Path]:
    """Search candidate locations for the compiled hex-tui binary.

    Resolution order:
    1. HEX_TUI_BIN environment variable.
    2. A binary built for this checkout (so source changes are never shadowed
       by an unrelated globally installed older cockpit).
    3. The installed release artifact at ~/.hex/bin (the install contract).
    4. Standard PATH lookup and ~/.local/bin fallback.
    5. Alternate target profile (release or debug).
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
        root / "target" / profile / BINARY_NAME,
        root / "crates" / "hex-tui" / "target" / profile / BINARY_NAME,
        root / "target" / alt_profile / BINARY_NAME,
        root / "crates" / "hex-tui" / "target" / alt_profile / BINARY_NAME,
        installed_binary_dir() / BINARY_NAME,
    ]

    for p in candidate_paths:
        if p.is_file() and os.access(p, os.X_OK):
            return p

    which_path = shutil.which("hex-tui")
    if which_path:
        return Path(which_path).resolve()

    local_bin = Path.home() / ".local" / "bin" / BINARY_NAME
    if local_bin.is_file() and os.access(local_bin, os.X_OK):
        return local_bin

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

    workspace = getattr(args, "workspace", None) or str(find_repo_root())
    cmd.extend(["--workspace", str(workspace)])

    model = getattr(args, "model", None)
    provider = getattr(args, "provider", None)

    if not model:
        try:
            from harness_core.config_loader import parse_config_yaml
            global_cfg = Path.home() / ".hex" / "config.yaml"
            for cfg_path in [
                Path(workspace) / ".hex" / "config.yaml",
                global_cfg,
            ]:
                if cfg_path.is_file():
                    cfg_data = parse_config_yaml(cfg_path)
                    msec = cfg_data.get("model") or cfg_data.get("models") or {}
                    if isinstance(msec, dict):
                        # A project inherits the global model unless it opts in,
                        # matching config_loader and the router.
                        is_project = cfg_path != global_cfg
                        opted_in = msec.get("project_override") is True or str(
                            msec.get("project_override", "")
                        ).lower() == "true"
                        if is_project and not opted_in:
                            continue
                        model = msec.get("cloud_model_id") or msec.get("selected_model") or msec.get("model_id")
                        provider = provider or msec.get("cloud_provider") or msec.get("provider")
                        if model:
                            break
        except Exception as exc:
            logger.warning("Could not read model config (%s); launching without a model override.", exc)

    if model:
        cmd.extend(["--model", str(model)])

    max_tokens = getattr(args, "max_tokens", None) or getattr(args, "context_window", None)
    if not max_tokens and model:
        try:
            from harness_core.models.model_metadata import get_model_context_length
            max_tokens = get_model_context_length(str(model), provider=str(provider or ""))
        except Exception:
            pass

    if max_tokens:
        cmd.extend(["--max-tokens", str(max_tokens)])

    skin = getattr(args, "skin", None)
    if skin:
        cmd.extend(["--skin", str(skin)])

    cow = getattr(args, "cow", None)
    if cow:
        cmd.extend(["--cow", str(cow)])

    session = getattr(args, "session", None)
    if session:
        cmd.extend(["--session", str(session)])

    return cmd


def launch_hex_tui(args: Any, repo_root: Optional[Path] = None) -> int:
    """Locate the installed hex-tui and execute it.

    A source build happens only when explicitly requested (--build or
    HEX_DEV_BUILD=1); a missing installation is an honest error, not an
    implicit cargo rescue (F03 install contract).
    """
    root = repo_root or find_repo_root()
    release_mode = bool(getattr(args, "release", False))
    force_build = bool(getattr(args, "build", False))
    dev_build = force_build or bool(os.environ.get("HEX_DEV_BUILD"))

    if dev_build:
        bin_path = find_or_build_hex_tui(root, force_build=force_build, release=release_mode)
    else:
        found = find_hex_tui_binary(root, release=release_mode)
        if found is None:
            raise FileNotFoundError(native_missing_message("hex-tui"))
        bin_path = found
    cmd = build_tui_cmd_args(args, bin_path)

    # Pass Python interpreter and repo root so hex-tui's ACP client knows how to launch the backend
    env = os.environ.copy()
    env["HEX_PYTHON_BIN"] = sys.executable
    env["HEX_REPO_ROOT"] = str(root)
    if hasattr(sys, "base_prefix") and sys.prefix != sys.base_prefix:
        env["VIRTUAL_ENV"] = sys.prefix

    # If --check is passed, print or forward exit code
    result = subprocess.run(cmd, env=env, check=False)
    return result.returncode
