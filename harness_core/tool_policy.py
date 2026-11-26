"""Persistent per-tool approval policy layered on top of gateway hard limits."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, TYPE_CHECKING

from harness_core.config_loader import parse_config_yaml

if TYPE_CHECKING:
    from harness_core.core.models import RiskClass

TOOL_POLICY_MODES = ("default", "allow", "ask", "deny")
TOOL_POLICY_NAME_MAX_LENGTH = 200


def normalize_tool_policy_mode(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("tool policy mode must be a string")
    mode = value.strip().lower()
    if mode not in TOOL_POLICY_MODES:
        raise ValueError(f"tool policy mode must be one of: {', '.join(TOOL_POLICY_MODES)}")
    return mode


def validate_tool_policy_name(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("tool policy name must be a string")
    name = value.strip()
    if not name or len(name) > TOOL_POLICY_NAME_MAX_LENGTH:
        raise ValueError(f"tool policy name must be 1–{TOOL_POLICY_NAME_MAX_LENGTH} characters")
    if name != value or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in name):
        raise ValueError("tool policy name must not contain surrounding whitespace or control characters")
    return name


def tool_policy_overrides(config: Mapping[str, Any]) -> dict[str, str]:
    tools = config.get("tools")
    if tools is None:
        return {}
    if not isinstance(tools, dict):
        raise ValueError("tools config must be a mapping")
    raw = tools.get("policy")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("tools.policy must be a mapping of tool name to mode")
    overrides: dict[str, str] = {}
    for raw_name, raw_mode in raw.items():
        name = validate_tool_policy_name(raw_name)
        mode = normalize_tool_policy_mode(raw_mode)
        if mode != "default":
            overrides[name] = mode
    return overrides


def load_tool_policy(config_file: Path) -> dict[str, str]:
    return tool_policy_overrides(parse_config_yaml(config_file))


def default_tool_policy_mode(risk_class: RiskClass | str) -> str:
    risk = getattr(risk_class, "value", str(risk_class))
    if risk == "R4":
        return "deny"
    if risk == "R3":
        return "ask"
    return "allow"


@dataclass(frozen=True)
class ToolPolicy:
    overrides: Mapping[str, str]

    def mode_for(self, tool_name: str, risk_class: RiskClass | str) -> str:
        risk = getattr(risk_class, "value", str(risk_class))
        if risk == "R4":
            return "deny"
        return self.overrides.get(tool_name, default_tool_policy_mode(risk_class))

    def is_default(self, tool_name: str) -> bool:
        return tool_name not in self.overrides
