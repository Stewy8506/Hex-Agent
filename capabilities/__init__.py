"""Capabilities and tool integrations subpackage: browser, vision, voice, search, git autopilot, GitHub actions, proofs, recipes, and webhooks."""

from __future__ import annotations

import importlib
from typing import Any

_SUBMODULES: set[str] = {'webhooks', 'browser', 'web_search', 'proof', 'github_action', 'recipes', 'vision', 'search', 'git_autopilot', 'voice', 'gateways'}
_SYMBOL_MAP: dict[str, str] = {
    'ApprovalRequest': 'gateways',
    'ApprovalDecision': 'gateways',
    'MobileApprovalBridge': 'gateways',
    'BrowserAction': 'browser', 'BrowserActionResult': 'browser', 'HeadlessBrowserSession': 'browser', 'BrowserManager': 'browser', 'SUPPORTED_IMAGE_EXTENSIONS': 'vision', 'get_image_metadata': 'vision', 'VisionEngine': 'vision', 'VoiceConfig': 'voice', 'clean_text_for_speech': 'voice', 'probe_audio_hardware': 'voice', 'BaseTTSProvider': 'voice', 'MacOSTTSProvider': 'voice', 'LinuxTTSProvider': 'voice', 'MockTTSProvider': 'voice', 'get_tts_provider': 'voice', 'VoiceEngine': 'voice', 'sanitize_fts5_query': 'search', 'TaskSearchResult': 'search', 'TaskSearchIndex': 'search', 'execute_web_search': 'web_search', 'GitAutopilotConfig': 'git_autopilot', 'GitHubTransport': 'git_autopilot', 'MockGitHubTransport': 'git_autopilot', 'SubprocessGitHubTransport': 'git_autopilot', 'GitAutopilot': 'git_autopilot', 'GitHubActionConfig': 'github_action', 'TriggerMatch': 'github_action', 'GitHubActionRunner': 'github_action', 'VENDOR_HARNESS2': 'proof', 'VerificationAndProofService': 'proof', 'PROTECTED_POLICY_PATHS': 'proof', 'Harness2ControlledAdapter': 'proof', 'Recipe': 'recipes', 'detect_recipe': 'recipes', 'WebhookConfig': 'webhooks', 'format_telegram_approval': 'webhooks', 'format_discord_approval': 'webhooks', 'format_decision_broadcast': 'webhooks', 'WebhookNotifier': 'webhooks'}

__all__ = [
    "gateways",
    "ApprovalRequest",
    "ApprovalDecision",
    "MobileApprovalBridge",
    "browser",
    "vision",
    "voice",
    "search",
    "web_search",
    "git_autopilot",
    "github_action",
    "proof",
    "recipes",
    "webhooks",
    "BrowserAction",
    "BrowserActionResult",
    "HeadlessBrowserSession",
    "BrowserManager",
    "SUPPORTED_IMAGE_EXTENSIONS",
    "get_image_metadata",
    "VisionEngine",
    "VoiceConfig",
    "clean_text_for_speech",
    "probe_audio_hardware",
    "BaseTTSProvider",
    "MacOSTTSProvider",
    "LinuxTTSProvider",
    "MockTTSProvider",
    "get_tts_provider",
    "VoiceEngine",
    "sanitize_fts5_query",
    "TaskSearchResult",
    "TaskSearchIndex",
    "execute_web_search",
    "GitAutopilotConfig",
    "GitHubTransport",
    "MockGitHubTransport",
    "SubprocessGitHubTransport",
    "GitAutopilot",
    "GitHubActionConfig",
    "TriggerMatch",
    "GitHubActionRunner",
    "VENDOR_HARNESS2",
    "VerificationAndProofService",
    "PROTECTED_POLICY_PATHS",
    "Harness2ControlledAdapter",
    "Recipe",
    "detect_recipe",
    "WebhookConfig",
    "format_telegram_approval",
    "format_discord_approval",
    "format_decision_broadcast",
    "WebhookNotifier",
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
