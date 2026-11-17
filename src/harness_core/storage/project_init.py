"""Project stack discovery and initialization of project-scoped state."""

from __future__ import annotations

from pathlib import Path
from typing import Any


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


def init_project(repo_root: Path | str, *, name: str | None = None, force: bool = False) -> dict[str, Any]:
    """Initialize project-scoped .hex configuration and gitignore based on detected stack."""
    repo = Path(repo_root).resolve()
    hex_dir = repo / ".hex"
    hex_dir.mkdir(parents=True, exist_ok=True)

    stack_info = detect_project_stack(repo)
    config_file = hex_dir / "config.yaml"
    gitignore_file = hex_dir / ".gitignore"

    proj_name = name or repo.name

    created = False
    if not config_file.exists() or force:
        config_yaml = f"""# HEX Project Configuration
version: "1.0"
project:
  name: "{proj_name}"
  stack: "{stack_info['stack']}"
model:
  # Inherits the operator's global model unless explicitly overridden.
  project_override: false
  compressor_mode: "deterministic"
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

    reflections_dir = hex_dir / "reflections"
    reflections_dir.mkdir(parents=True, exist_ok=True)
    lessons_file = reflections_dir / "LESSONS.md"
    if not lessons_file.exists():
        lessons_file.write_text(
            f"# Project Reflections & Work Memory: {proj_name}\n\n## Lessons Learned\n",
            encoding="utf-8",
        )

    data_dir = hex_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    try:
        from harness_core.storage.project_registry import ProjectRegistry

        ProjectRegistry().register_project(repo, name=proj_name, stack=stack_info["stack"])
    except Exception:
        pass

    return {
        "project_name": proj_name,
        "stack": stack_info["stack"],
        "test_command": stack_info["test_cmd"],
        "config_path": str(config_file),
        "gitignore_path": str(gitignore_file),
        "created": created,
    }
