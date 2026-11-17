"""Filesystem paths for package-bundled resources."""

from pathlib import Path


def bundled_skills_dir(optional: bool = False) -> Path:
    """Return bundled skill data in source, editable, and installed-wheel layouts."""
    return Path(__file__).resolve().parent / "skills" / ("optional" if optional else "builtin")
