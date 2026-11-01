"""HEX Bidirectional Multi-Platform Chat Gateways.

Pillar 3: Telegram and Discord bot interfaces for autonomous agent oversight,
conversational task delegation, and gated R3/R4 human-in-the-loop approvals.
"""

from .discord_bot import (
    DiscordBotGateway,
    DiscordConfig,
    HttpDiscordTransport,
    MockDiscordTransport,
)
from .manager import GatewayManager
from .telegram_bot import TelegramBotGateway, TelegramConfig, MockTelegramTransport

__all__ = [
    "TelegramBotGateway",
    "TelegramConfig",
    "MockTelegramTransport",
    "DiscordBotGateway",
    "DiscordConfig",
    "HttpDiscordTransport",
    "MockDiscordTransport",
    "GatewayManager",
]

