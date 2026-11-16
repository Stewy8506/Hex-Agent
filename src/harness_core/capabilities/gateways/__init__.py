"""HEX Bidirectional Multi-Platform Chat Gateways & Mobile Reaction Bridge.

Telegram and Discord bot interfaces for autonomous agent oversight,
conversational task delegation, and gated remote human-in-the-loop approvals.
"""

from __future__ import annotations

from .reaction_listener import (
    ApprovalDecision,
    ApprovalRequest,
    MobileApprovalBridge,
)
from .discord_bot import (
    DiscordBotGateway,
    DiscordConfig,
    HttpDiscordTransport,
    MockDiscordTransport,
)
from .manager import GatewayManager
from .telegram_bot import (
    MockTelegramTransport,
    TelegramBotGateway,
    TelegramConfig,
)

__all__ = [
    "ApprovalRequest",
    "ApprovalDecision",
    "MobileApprovalBridge",
    "TelegramBotGateway",
    "TelegramConfig",
    "MockTelegramTransport",
    "DiscordBotGateway",
    "DiscordConfig",
    "HttpDiscordTransport",
    "MockDiscordTransport",
    "GatewayManager",
]
