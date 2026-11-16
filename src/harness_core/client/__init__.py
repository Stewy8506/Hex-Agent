"""Client interfaces subpackage: ACP server, TUI console, TUI launcher, UI selectors, web dashboard, ASGI/HTTP server, CLI, onboarding, auto-setup, and diagnostics."""

from __future__ import annotations

import importlib
from typing import Any

_SUBMODULES: set[str] = {'doctor', 'asgi', 'server', 'tui', 'acp', 'auto_setup', 'ui_selector', 'tui_launcher', 'onboard', 'dashboard', 'cli'}
_SYMBOL_MAP: dict[str, str] = {'ACPSession': 'acp', 'ACPServer': 'acp', 'render_unified_diff': 'tui', 'render_side_by_side_diff': 'tui', 'render_blast_radius_tree': 'tui', 'format_terminal_markdown': 'tui', 'StreamingTokenRenderer': 'tui', 'render_hex_splash': 'tui', 'HexConsole': 'tui', 'find_repo_root': 'tui_launcher', 'find_hex_tui_binary': 'tui_launcher', 'build_hex_tui': 'tui_launcher', 'find_or_build_hex_tui': 'tui_launcher', 'build_tui_cmd_args': 'tui_launcher', 'launch_hex_tui': 'tui_launcher', 'SelectorCancelledError': 'ui_selector', 'select_option': 'ui_selector', 'select_multiple': 'ui_selector', 'prompt_secret': 'ui_selector', 'DASHBOARD_HTML': 'dashboard', 'HarnessASGIAdapter': 'asgi', 'get_asgi_app': 'asgi', 'HarnessAPIApp': 'server', 'GracefulServerRunner': 'server', 'run_http_server': 'server', 'DEFAULT_DATA_DIR': 'cli', 'seed_default_skills': 'cli', 'main': 'cli', 'build_parser': 'cli', 'HEX_BANNER': 'onboard', 'HardwareProfile': 'onboard', 'update_config_yaml_model': 'onboard', 'run_first_task_verification': 'onboard', 'run_onboard_wizard': 'onboard', 'detect_project_stack': 'onboard', 'init_project': 'onboard', 'EnvironmentSnapshot': 'auto_setup', 'detect_environment': 'auto_setup', 'synthesize_optimal_config': 'auto_setup', 'execute_auto_setup': 'auto_setup', 'DoctorCheckResult': 'doctor', 'DoctorReport': 'doctor', 'HexDoctor': 'doctor'}

__all__ = [
    "acp",
    "tui",
    "tui_launcher",
    "ui_selector",
    "dashboard",
    "asgi",
    "server",
    "cli",
    "onboard",
    "auto_setup",
    "doctor",
    "ACPSession",
    "ACPServer",
    "render_unified_diff",
    "render_side_by_side_diff",
    "render_blast_radius_tree",
    "format_terminal_markdown",
    "StreamingTokenRenderer",
    "render_hex_splash",
    "HexConsole",
    "find_repo_root",
    "find_hex_tui_binary",
    "build_hex_tui",
    "find_or_build_hex_tui",
    "build_tui_cmd_args",
    "launch_hex_tui",
    "SelectorCancelledError",
    "select_option",
    "select_multiple",
    "prompt_secret",
    "DASHBOARD_HTML",
    "HarnessASGIAdapter",
    "get_asgi_app",
    "HarnessAPIApp",
    "GracefulServerRunner",
    "run_http_server",
    "DEFAULT_DATA_DIR",
    "seed_default_skills",
    "main",
    "build_parser",
    "HEX_BANNER",
    "HardwareProfile",
    "update_config_yaml_model",
    "run_first_task_verification",
    "run_onboard_wizard",
    "detect_project_stack",
    "init_project",
    "EnvironmentSnapshot",
    "detect_environment",
    "synthesize_optimal_config",
    "execute_auto_setup",
    "DoctorCheckResult",
    "DoctorReport",
    "HexDoctor",
]

def __getattr__(name: str) -> Any:
    if name in _SUBMODULES:
        mod = importlib.import_module(f".{name}", __name__)
        globals()[name] = mod
        return mod
    if name in _SYMBOL_MAP:
        target_mod = _SYMBOL_MAP[name]
        mod = importlib.import_module(f".{target_mod}", __name__)
        val = getattr(mod, name)
        globals()[name] = val
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

def __dir__() -> list[str]:
    return list(__all__)
