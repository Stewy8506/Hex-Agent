"""Shared slash-command catalog (F06).

Single source of truth for command availability across the Python console,
Rust TUI, and ACP surfaces. The JSON catalog ships with the package and is
also embedded by the Rust TUI at build time; consistency tests on both sides
fail if a surface's help/dispatch drifts from it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SURFACES = ("console", "tui", "acp")

_CATALOG_PATH = Path(__file__).resolve().parent / "command_catalog.json"


@dataclass(frozen=True)
class CommandSpec:
    name: str
    usage: str
    description: str
    aliases: tuple[str, ...] = ()
    surfaces: dict[str, str] = field(default_factory=dict)
    tui_local: bool = False

    def supported_on(self, surface: str) -> bool:
        return self.surfaces.get(surface) == "supported"

    def all_names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)


def load_catalog(path: Path | None = None) -> list[CommandSpec]:
    data: dict[str, Any] = json.loads((path or _CATALOG_PATH).read_text(encoding="utf-8"))
    specs: list[CommandSpec] = []
    for entry in data.get("commands", []):
        specs.append(
            CommandSpec(
                name=entry["name"],
                usage=entry.get("usage", entry["name"]),
                description=entry.get("description", ""),
                aliases=tuple(entry.get("aliases", [])),
                surfaces=dict(entry.get("surfaces", {})),
                tui_local=bool(entry.get("tui_local", False)),
            )
        )
    return specs


def commands_for_surface(surface: str, catalog: list[CommandSpec] | None = None) -> list[CommandSpec]:
    if surface not in SURFACES:
        raise ValueError(f"Unknown surface: {surface}. Valid: {', '.join(SURFACES)}")
    return [c for c in (catalog if catalog is not None else load_catalog()) if c.supported_on(surface)]


def completion_names(surface: str, catalog: list[CommandSpec] | None = None) -> list[str]:
    """Canonical command names offered by completion on a surface."""
    return [c.name for c in commands_for_surface(surface, catalog)]


def render_help(surface: str, catalog: list[CommandSpec] | None = None) -> str:
    cmds = commands_for_surface(surface, catalog)
    lines = ["Available Commands:"]
    for c in cmds:
        lines.append(f"  {c.usage:<28} - {c.description}")
    return "\n".join(lines)