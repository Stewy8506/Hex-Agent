"""Filesystem paths for package-bundled resources."""

from pathlib import Path

from harness_core.resources import skills
from harness_core.resources.skills import optional as optional_skills


def bundled_skills_dir(optional: bool = False) -> Path:
    """Return bundled skill data in source, editable, and installed-wheel layouts."""
    if optional:
        return Path(optional_skills.__file__).resolve().parent
    return Path(skills.__file__).resolve().parent / "builtin"
