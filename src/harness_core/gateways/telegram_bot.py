"""HEX Telegram Bot Gateway.

Provides bidirectional Telegram bot interface for conversational task delegation,
operator whitelisting, live progress notifications, and interactive inline keyboard
approvals for R3/R4 operations.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, Sequence

logger = logging.getLogger("harness_core.gateways.telegram_bot")


@dataclass
class TelegramConfig:
    bot_token: str | None = None
    allowed_user_ids: set[str] = field(default_factory=set)
    allowed_chat_ids: set[str] = field(default_factory=set)
    poll_interval: float = 1.0
    poll_timeout: int = 30
    webhook_url: str | None = None
    base_api_url: str = "https://api.telegram.org"
    dry_run: bool = False

    def __post_init__(self) -> None:
        if self.allowed_user_ids is not None:
            self.allowed_user_ids = {
                str(x).strip() for x in self.allowed_user_ids if str(x).strip()
            }
        else:
            self.allowed_user_ids = set()

        if self.allowed_chat_ids is not None:
            self.allowed_chat_ids = {
                str(x).strip() for x in self.allowed_chat_ids if str(x).strip()
            }
        else:
            self.allowed_chat_ids = set()

    @classmethod
    def from_env(cls) -> TelegramConfig:
        bot_token = (
            os.getenv("HEX_TELEGRAM_BOT_TOKEN")
            or os.getenv("HARNESS_TELEGRAM_BOT_TOKEN")
            or None
        )

        def _parse_csv(val: str | None) -> set[str]:
            if not val:
                return set()
            return {x.strip() for x in val.split(",") if x.strip()}

        raw_users = os.getenv("HEX_TELEGRAM_ALLOWED_USERS") or os.getenv(
            "HARNESS_TELEGRAM_ALLOWED_USERS"
        )
        allowed_users = _parse_csv(raw_users)

        raw_chats = os.getenv("HEX_TELEGRAM_ALLOWED_CHATS") or os.getenv(
            "HARNESS_TELEGRAM_ALLOWED_CHATS"
        )
        allowed_chats = _parse_csv(raw_chats)

        raw_interval = os.getenv("HEX_TELEGRAM_POLL_INTERVAL") or os.getenv(
            "HARNESS_TELEGRAM_POLL_INTERVAL"
        )
        poll_interval = float(raw_interval) if raw_interval else 1.0

        raw_timeout = os.getenv("HEX_TELEGRAM_POLL_TIMEOUT") or os.getenv(
            "HARNESS_TELEGRAM_POLL_TIMEOUT"
        )
        poll_timeout = int(raw_timeout) if raw_timeout else 30

        webhook_url = (
            os.getenv("HEX_TELEGRAM_WEBHOOK_URL")
            or os.getenv("HARNESS_TELEGRAM_WEBHOOK_URL")
            or None
        )
        base_api_url = (
            os.getenv("HEX_TELEGRAM_BASE_API_URL")
            or os.getenv("HARNESS_TELEGRAM_BASE_API_URL")
            or "https://api.telegram.org"
        )
        raw_dry_run = os.getenv("HEX_TELEGRAM_DRY_RUN") or os.getenv(
            "HARNESS_TELEGRAM_DRY_RUN"
        )
        dry_run = str(raw_dry_run).lower() in ("1", "true", "yes")

        return cls(
            bot_token=bot_token,
            allowed_user_ids=allowed_users,
            allowed_chat_ids=allowed_chats,
            poll_interval=poll_interval,
            poll_timeout=poll_timeout,
            webhook_url=webhook_url,
            base_api_url=base_api_url,
            dry_run=dry_run,
        )


class TelegramTransport(Protocol):
    def call_api(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Call a Telegram Bot API method."""
        ...


class HttpTelegramTransport:
    """Production HTTP transport communicating with api.telegram.org."""

    def __init__(self, bot_token: str, base_api_url: str = "https://api.telegram.org"):
        self.bot_token = bot_token
        self.base_api_url = base_api_url.rstrip("/")

    def call_api(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        url = f"{self.base_api_url}/bot{self.bot_token}/{method}"
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(
            url,
            data=data,
            headers=headers,
            method="POST" if data is not None else "GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8") if exc.fp else ""
            logger.error("Telegram API HTTP %d error calling %s: %s", exc.code, method, body)
            try:
                return json.loads(body)
            except Exception:
                return {"ok": False, "error_code": exc.code, "description": body}
        except Exception as exc:
            logger.error("Telegram API transport error calling %s: %s", method, exc)
            return {"ok": False, "description": str(exc)}


class MockTelegramTransport:
    """In-memory mock transport for hermetic testing."""

    def __init__(self) -> None:
        self.sent_messages: list[dict[str, Any]] = []
        self.edited_messages: list[dict[str, Any]] = []
        self.answered_callbacks: list[dict[str, Any]] = []
        self.api_calls: list[tuple[str, dict[str, Any] | None]] = []
        self.updates_queue: list[dict[str, Any]] = []
        self.custom_responses: dict[str, Any] = {}
        self._next_message_id = 100

    def enqueue_updates(self, updates: Sequence[dict[str, Any]]) -> None:
        self.updates_queue.extend(updates)

    def call_api(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        self.api_calls.append((method, payload))
        if method in self.custom_responses:
            return self.custom_responses[method]

        if method == "sendMessage":
            data = dict(payload or {})
            self._next_message_id += 1
            msg_id = self._next_message_id
            data["message_id"] = msg_id
            self.sent_messages.append(data)
            return {"ok": True, "result": data}

        if method == "editMessageText":
            data = dict(payload or {})
            self.edited_messages.append(data)
            return {"ok": True, "result": data}

        if method == "answerCallbackQuery":
            data = dict(payload or {})
            self.answered_callbacks.append(data)
            return {"ok": True, "result": True}

        if method == "getUpdates":
            offset = (payload or {}).get("offset", 0)
            eligible = [u for u in self.updates_queue if u.get("update_id", 0) >= offset]
            # Drain returned updates from queue
            remaining = [u for u in self.updates_queue if u.get("update_id", 0) < offset]
            self.updates_queue = remaining
            return {"ok": True, "result": eligible}

        return {"ok": True, "result": {}}


class TelegramBotGateway:
    """Telegram Bot Gateway orchestrating bidirectional interaction."""

    def __init__(
        self,
        orchestrator: Any | None,
        config: TelegramConfig,
        transport: TelegramTransport | None = None,
    ) -> None:
        self.orchestrator = orchestrator
        self.config = config
        if transport is not None:
            self.transport = transport
        elif config.dry_run or not config.bot_token:
            self.transport = MockTelegramTransport()
        else:
            self.transport = HttpTelegramTransport(
                bot_token=config.bot_token,
                base_api_url=config.base_api_url,
            )
        self._last_update_id: int = 0
        self._running: bool = False

    def is_user_authorized(self, user_id: str | int | None) -> bool:
        """Verify user is in whitelist (strict default-deny)."""
        if not self.config.allowed_user_ids:
            return False
        if user_id is None:
            return False
        return str(user_id).strip() in self.config.allowed_user_ids

    def is_chat_authorized(self, chat_id: str | int | None) -> bool:
        """Verify chat is in whitelist (if allowed_chat_ids configured)."""
        if not self.config.allowed_chat_ids:
            return True
        if chat_id is None:
            return False
        return str(chat_id).strip() in self.config.allowed_chat_ids

    def check_access(
        self,
        user_id: str | int | None,
        chat_id: str | int | None = None,
    ) -> tuple[bool, str]:
        """Operator whitelist access control with security audit logging."""
        if not self.is_user_authorized(user_id):
            logger.warning(
                "SECURITY_AUDIT: Dropped unauthorized Telegram update from user=%s chat=%s",
                user_id,
                chat_id,
            )
            return False, "Unauthorized operator"

        if not self.is_chat_authorized(chat_id):
            logger.warning(
                "SECURITY_AUDIT: Dropped unauthorized Telegram update from user=%s chat=%s (chat unauthorized)",
                user_id,
                chat_id,
            )
            return False, "Unauthorized chat"

        return True, "Authorized"

    def poll_once(self) -> int:
        """Poll getUpdates once, dispatch updates, and return count processed."""
        payload: dict[str, Any] = {
            "offset": self._last_update_id + 1,
            "timeout": self.config.poll_timeout,
        }
        resp = self.transport.call_api("getUpdates", payload)
        if not resp.get("ok"):
            logger.warning("Telegram getUpdates returned error: %s", resp)
            return 0

        updates = resp.get("result", [])
        count = 0
        for update in updates:
            up_id = update.get("update_id", 0)
            if up_id > self._last_update_id:
                self._last_update_id = up_id
            try:
                self.process_update(update)
            except Exception as exc:
                logger.error("Error processing Telegram update %d: %s", up_id, exc, exc_info=True)
            count += 1
        return count

    def start_polling(self, stop_event: threading.Event | None = None) -> None:
        """Run continuous polling loop with backoff until stopped."""
        self._running = True
        logger.info("TelegramBotGateway polling started.")
        while self._running:
            if stop_event and stop_event.is_set():
                break
            try:
                self.poll_once()
            except Exception as exc:
                logger.error("Unexpected error in Telegram polling loop: %s", exc)
                time.sleep(min(self.config.poll_interval * 2, 10.0))
            time.sleep(self.config.poll_interval)
        logger.info("TelegramBotGateway polling stopped.")

    def stop_polling(self) -> None:
        """Signal polling loop to stop."""
        self._running = False

    def process_update(self, update: dict[str, Any]) -> dict[str, Any] | None:
        """Route message or callback query update."""
        if "message" in update:
            return self.handle_message(update["message"])
        if "callback_query" in update:
            return self.handle_callback_query(update["callback_query"])
        return None

    def handle_message(self, message: dict[str, Any]) -> dict[str, Any]:
        """Placeholder for message handling (implemented in Task 2)."""
        user_id = (message.get("from") or {}).get("id")
        chat_id = (message.get("chat") or {}).get("id")
        ok, reason = self.check_access(user_id, chat_id)
        if not ok:
            return {"ok": False, "reason": reason}
        return {"ok": True, "text": "Received"}

    def handle_callback_query(self, callback_query: dict[str, Any]) -> dict[str, Any]:
        """Placeholder for callback handling (implemented in Task 2)."""
        user_id = (callback_query.get("from") or {}).get("id")
        chat_id = ((callback_query.get("message") or {}).get("chat") or {}).get("id")
        ok, reason = self.check_access(user_id, chat_id)
        if not ok:
            return {"ok": False, "reason": reason}
        return {"ok": True, "action": "acknowledged"}
