"""HEX Multi-Gateway Supervisor (GatewayManager).

Coordinates concurrent lifecycles, health reporting, polling loops,
and graceful shutdown (SIGINT/SIGTERM) of both Telegram and Discord gateways.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from typing import Any

from harness_core.gateways.discord_bot import DiscordBotGateway
from harness_core.gateways.telegram_bot import TelegramBotGateway

logger = logging.getLogger("harness_core.gateways.manager")


class GatewayManager:
    """Supervises bidirectional chat gateways (Telegram & Discord)."""

    def __init__(
        self,
        orchestrator: Any,
        telegram_gateway: TelegramBotGateway | None = None,
        discord_gateway: DiscordBotGateway | None = None,
    ) -> None:
        self.orchestrator = orchestrator
        self.telegram_gateway = telegram_gateway
        self.discord_gateway = discord_gateway

        self.is_running: bool = False
        self.start_time: float | None = None
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self.update_counts: dict[str, int] = {
            "telegram": 0,
            "discord": 0,
        }

    def _handle_signal(self, signum: int, frame: Any) -> None:
        logger.info("GatewayManager received signal %d; initiating shutdown...", signum)
        self.stop()

    def poll_once(self) -> dict[str, int]:
        """Execute a single poll cycle across active gateways."""
        if self.telegram_gateway:
            try:
                count = self.telegram_gateway.poll_once()
                self.update_counts["telegram"] += count
            except Exception as exc:
                logger.error("Error polling Telegram gateway: %s", exc)

        return dict(self.update_counts)

    def start(self, daemon: bool = True, poll_interval: float = 1.0) -> None:
        """Start supervised gateway polling loops."""
        if self.is_running:
            return

        self.is_running = True
        self.start_time = time.time()
        self._stop_event.clear()

        # Install signal handlers if running in main thread
        try:
            signal.signal(signal.SIGINT, self._handle_signal)
            signal.signal(signal.SIGTERM, self._handle_signal)
        except (ValueError, AttributeError):
            pass

        # Start Telegram polling thread if configured
        if self.telegram_gateway:
            t = threading.Thread(
                target=self.telegram_gateway.start_polling,
                kwargs={"stop_event": self._stop_event},
                name="GatewayManager-TelegramWorker",
                daemon=True,
            )
            t.start()
            self._threads.append(t)
            logger.info("Started Telegram gateway background polling thread.")

        if self.discord_gateway:
            logger.info("Discord Interaction gateway active and registered with supervisor.")

        logger.info("GatewayManager started successfully (daemon=%s).", daemon)

        if not daemon:
            try:
                while not self._stop_event.is_set():
                    time.sleep(poll_interval)
            except KeyboardInterrupt:
                logger.info("GatewayManager interrupted by operator.")
                self.stop()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal gateways to stop and wait for background threads to exit."""
        if not self.is_running and self._stop_event.is_set():
            return

        logger.info("Stopping GatewayManager and child gateways...")
        self._stop_event.set()

        if self.telegram_gateway:
            try:
                self.telegram_gateway.stop_polling()
            except Exception as exc:
                logger.warning("Error stopping Telegram gateway: %s", exc)

        # Wait for threads to terminate
        deadline = time.time() + timeout
        for t in self._threads:
            rem = max(0.1, deadline - time.time())
            t.join(timeout=rem)

        self._threads.clear()
        self.is_running = False
        logger.info("GatewayManager cleanly stopped.")

    def get_status(self) -> dict[str, Any]:
        """Return structured runtime and health metrics for active gateways."""
        uptime = (
            time.time() - self.start_time
            if (self.is_running and self.start_time is not None)
            else 0.0
        )

        gateways_info = []
        if self.telegram_gateway:
            gateways_info.append({
                "name": "telegram",
                "active": True,
                "allowed_users": sorted(list(self.telegram_gateway.config.allowed_user_ids)),
                "allowed_chats": sorted(list(self.telegram_gateway.config.allowed_chat_ids)),
                "dry_run": self.telegram_gateway.config.dry_run,
                "processed_updates": self.update_counts.get("telegram", 0),
            })
        if self.discord_gateway:
            gateways_info.append({
                "name": "discord",
                "active": True,
                "allowed_users": sorted(list(self.discord_gateway.config.allowed_user_ids)),
                "allowed_guilds": sorted(list(self.discord_gateway.config.allowed_guild_ids)),
                "allowed_channels": sorted(list(self.discord_gateway.config.allowed_channel_ids)),
                "dry_run": self.discord_gateway.config.dry_run,
                "processed_updates": self.update_counts.get("discord", 0),
            })

        tasks = []
        if self.orchestrator and hasattr(self.orchestrator, "ledger"):
            try:
                tasks = self.orchestrator.ledger.list_tasks(limit=100)
            except Exception:
                tasks = []

        active_tasks = sum(
            1 for t in tasks if t.get("status") in ("QUEUED", "RUNNING", "WAITING_APPROVAL", "NEEDS_ATTENTION")
        )

        return {
            "status": "running" if self.is_running else "stopped",
            "uptime_seconds": round(uptime, 2),
            "gateways": gateways_info,
            "orchestrator_tasks": {
                "active": active_tasks,
                "total": len(tasks),
            },
            "update_counts": dict(self.update_counts),
        }
