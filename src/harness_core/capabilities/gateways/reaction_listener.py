"""Asynchronous Mobile Reaction Listener Bridge.

Enables remote human-in-the-loop approval workflows from Telegram and Discord
mobile clients with inline buttons, reaction emoji listeners, and safe fallback timeouts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
import threading
import time
from typing import Any
import uuid

logger = logging.getLogger(__name__)


@dataclass
class ApprovalRequest:
    """Structured human approval request dispatched to remote operator channels."""

    request_id: str
    task_id: str | None = None
    tool_name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    risk_class: str = "R3"
    blast_radius: str = ""
    created_at: float = field(default_factory=time.time)
    timeout_seconds: float | None = None


@dataclass
class ApprovalDecision:
    """Decision outcome returned by remote operator or fallback timer."""

    decision: str  # "APPROVE" | "DENY" | "TIMEOUT"
    responder_id: str | None = None
    platform: str = "local"  # "telegram" | "discord" | "local"
    timestamp: float = field(default_factory=time.time)
    reason: str | None = None


class MobileApprovalBridge:
    """Coordinates remote approval requests and interactive reaction/button responses."""

    def __init__(
        self,
        telegram_bot: Any | None = None,
        discord_bot: Any | None = None,
        timeout_seconds: float | None = None,
        allowed_user_ids: list[str] | set[str] | None = None,
    ) -> None:
        self.telegram_bot = telegram_bot
        self.discord_bot = discord_bot
        if timeout_seconds is not None:
            self.default_timeout = float(timeout_seconds)
        else:
            try:
                self.default_timeout = float(os.getenv("HEX_APPROVAL_TIMEOUT", "60.0"))
            except ValueError:
                self.default_timeout = 60.0

        self.allowed_user_ids: set[str] | None = (
            set(str(u) for u in allowed_user_ids) if allowed_user_ids is not None else None
        )
        self._pending_requests: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

        # Wire self into gateways if provided
        if self.telegram_bot is not None and hasattr(self.telegram_bot, "set_bridge"):
            self.telegram_bot.set_bridge(self)
        if self.discord_bot is not None and hasattr(self.discord_bot, "set_bridge"):
            self.discord_bot.set_bridge(self)

    def has_pending(self, request_id: str) -> bool:
        """Return True if request_id is actively awaiting a decision."""
        with self._lock:
            return request_id in self._pending_requests

    def get_pending_requests(self) -> list[ApprovalRequest]:
        """Return list of active pending approval requests."""
        with self._lock:
            return [v["request"] for v in self._pending_requests.values()]

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        """Synchronously dispatch approval request and block until operator response or timeout."""
        evt = threading.Event()
        timeout = (
            request.timeout_seconds
            if (request.timeout_seconds is not None and request.timeout_seconds > 0)
            else self.default_timeout
        )

        entry = {
            "request": request,
            "event": evt,
            "decision": None,
            "telegram_msg_id": None,
            "telegram_chat_id": None,
            "discord_msg_id": None,
            "discord_channel_id": None,
        }

        with self._lock:
            self._pending_requests[request.request_id] = entry

        # 1. Dispatch to Telegram
        if self.telegram_bot is not None:
            try:
                self._dispatch_telegram(entry)
            except Exception as exc:
                logger.warning("Failed dispatching approval request to Telegram: %s", exc)

        # 2. Dispatch to Discord
        if self.discord_bot is not None:
            try:
                self._dispatch_discord(entry)
            except Exception as exc:
                logger.warning("Failed dispatching approval request to Discord: %s", exc)

        # 3. Wait on event or timeout
        signaled = evt.wait(timeout)

        with self._lock:
            if signaled and entry["decision"] is not None:
                dec = entry["decision"]
                self._pending_requests.pop(request.request_id, None)
                return dec

            # Timed out
            timeout_dec = ApprovalDecision(
                decision="TIMEOUT",
                platform="local",
                timestamp=time.time(),
                reason=f"Mobile operator response timed out after {timeout:.1f}s",
            )
            # Update remote message cards with TIMED OUT status
            try:
                self._update_remote_cards(entry, timeout_dec)
            except Exception:
                pass

            self._pending_requests.pop(request.request_id, None)
            return timeout_dec

    async def request_approval_async(self, request: ApprovalRequest) -> ApprovalDecision:
        """Asynchronous wrapper for request_approval."""
        import asyncio

        return await asyncio.to_thread(self.request_approval, request)

    def resolve_approval(
        self,
        request_id: str,
        decision: str,
        responder_id: str,
        platform: str = "telegram",
        reason: str | None = None,
    ) -> bool:
        """Resolve a pending approval request with an operator's decision.

        Returns True if the decision was applied, False if unauthorized or request not found.
        """
        with self._lock:
            if request_id not in self._pending_requests:
                return False

            # Verify responder authorization whitelist
            if self.allowed_user_ids is not None:
                if str(responder_id) not in self.allowed_user_ids:
                    logger.warning("Unauthorized operator %s attempted decision for %s", responder_id, request_id)
                    return False

            entry = self._pending_requests[request_id]
            clean_dec = decision.strip().upper()
            if clean_dec in ("Y", "YES", "APPROVE", "APPROVED"):
                clean_dec = "APPROVE"
            elif clean_dec in ("N", "NO", "DENY", "DENIED", "REJECT", "REJECTED"):
                clean_dec = "DENY"
            else:
                clean_dec = "DENY"

            dec_obj = ApprovalDecision(
                decision=clean_dec,
                responder_id=str(responder_id),
                platform=platform,
                timestamp=time.time(),
                reason=reason or f"Resolved by @{responder_id} via {platform}",
            )
            entry["decision"] = dec_obj
            entry["event"].set()

            # Update remote cards in-place with outcome
            try:
                self._update_remote_cards(entry, dec_obj)
            except Exception as exc:
                logger.warning("Failed updating remote cards on resolve: %s", exc)

            return True

    def handle_reaction(
        self,
        platform: str,
        message_id: Any,
        user_id: str,
        emoji: str,
    ) -> bool:
        """Handle reaction emoji (thumbs up / thumbs down) from remote platforms."""
        with self._lock:
            req_id = None
            for rid, item in self._pending_requests.items():
                if platform == "telegram" and str(item.get("telegram_msg_id")) == str(message_id):
                    req_id = rid
                    break
                elif platform == "discord" and str(item.get("discord_msg_id")) == str(message_id):
                    req_id = rid
                    break
            if not req_id:
                return False

        clean_emoji = str(emoji).strip()
        approve_emojis = {"👍", "+1", "thumbsup", "check", "white_check_mark", "✅"}
        deny_emojis = {"👎", "-1", "thumbsdown", "x", "cross_mark", "❌"}

        if clean_emoji in approve_emojis:
            return self.resolve_approval(req_id, "APPROVE", user_id, platform=platform)
        elif clean_emoji in deny_emojis:
            return self.resolve_approval(req_id, "DENY", user_id, platform=platform)

        return False

    def handle_callback(
        self,
        platform: str,
        callback_data: str,
        user_id: str,
        message_id: Any | None = None,
    ) -> bool:
        """Handle button callback query from remote platforms (e.g. 'approve:<id>')."""
        clean_cb = callback_data.strip()
        action = ""
        req_id = ""

        if ":" in clean_cb:
            parts = clean_cb.split(":")
            if parts[0] == "hex" and len(parts) >= 3:
                action = parts[1]
                req_id = parts[2]
            else:
                action = parts[0]
                req_id = parts[1]
        else:
            action = clean_cb
            req_id = ""

        if not self.has_pending(req_id):
            return False

        decision_map = {
            "approve": "APPROVE",
            "deny": "DENY",
            "reject": "DENY",
        }
        dec = decision_map.get(action.lower())
        if not dec:
            return False

        return self.resolve_approval(req_id, dec, str(user_id), platform=platform)

    def _dispatch_telegram(self, entry: dict[str, Any]) -> None:
        """Send interactive approval notification card to Telegram."""
        req: ApprovalRequest = entry["request"]
        bot = self.telegram_bot
        if not bot or not getattr(bot, "transport", None):
            return

        chat_ids = getattr(bot.config, "allowed_chat_ids", [])
        if not chat_ids:
            return

        chat_id = list(chat_ids)[0]
        params_str = json.dumps(req.arguments, indent=2) if req.arguments else "{}"
        if len(params_str) > 400:
            params_str = params_str[:397] + "..."

        timeout_disp = req.timeout_seconds if req.timeout_seconds is not None else self.default_timeout
        text = (
            f"🚨 *HEX Approval Required: `{req.tool_name}`*\n\n"
            f"*Request ID:* `{req.request_id}`\n"
            f"*Task ID:* `{req.task_id or 'none'}`\n"
            f"*Risk Class:* `{req.risk_class}`\n"
            f"*Timeout:* `{timeout_disp:.0f}s`\n\n"
            f"*Parameters:*\n```json\n{params_str}\n```"
        )
        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "✅ Approve", "callback_data": f"approve:{req.request_id}"},
                    {"text": "❌ Deny", "callback_data": f"deny:{req.request_id}"},
                ]
            ]
        }

        resp = bot.transport.call_api(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "reply_markup": reply_markup,
            },
        )
        if isinstance(resp, dict) and resp.get("ok"):
            msg_id = (resp.get("result") or {}).get("message_id")
            entry["telegram_msg_id"] = msg_id
            entry["telegram_chat_id"] = chat_id

    def _dispatch_discord(self, entry: dict[str, Any]) -> None:
        """Send interactive approval notification card to Discord."""
        req: ApprovalRequest = entry["request"]
        bot = self.discord_bot
        if not bot or not getattr(bot, "transport", None):
            return

        channel_id = getattr(bot.config, "default_channel_id", None) or "channel_approvals"
        params_str = json.dumps(req.arguments, indent=2) if req.arguments else "{}"
        if len(params_str) > 400:
            params_str = params_str[:397] + "..."

        timeout_disp = req.timeout_seconds if req.timeout_seconds is not None else self.default_timeout
        embed = {
            "title": f"🚨 HEX Approval Required: {req.tool_name} ({req.risk_class})",
            "color": 0xFFA500 if req.risk_class == "R3" else 0xFF0000,
            "fields": [
                {"name": "Request ID", "value": f"`{req.request_id}`", "inline": True},
                {"name": "Task ID", "value": f"`{req.task_id or 'none'}`", "inline": True},
                {"name": "Timeout", "value": f"`{timeout_disp:.0f}s`", "inline": True},
                {"name": "Parameters", "value": f"```json\n{params_str}\n```", "inline": False},
            ],
        }

        components = [
            {
                "type": 1,  # Action Row
                "components": [
                    {
                        "type": 2,  # Button
                        "style": 3,  # Success (Green)
                        "label": "Approve",
                        "custom_id": f"hex:approve:{req.request_id}",
                    },
                    {
                        "type": 2,  # Button
                        "style": 4,  # Danger (Red)
                        "label": "Deny",
                        "custom_id": f"hex:deny:{req.request_id}",
                    },
                ],
            }
        ]

        if hasattr(bot.transport, "send_channel_message"):
            resp = bot.transport.send_channel_message(
                channel_id,
                content="🚨 **Approval Required for privileged operation**",
                embeds=[embed],
                components=components,
            )
            if isinstance(resp, dict):
                msg_id = resp.get("id")
                entry["discord_msg_id"] = msg_id
                entry["discord_channel_id"] = channel_id

    def _update_remote_cards(self, entry: dict[str, Any], dec: ApprovalDecision) -> None:
        """Update remote Telegram and Discord cards in-place with final outcome badge."""
        req: ApprovalRequest = entry["request"]

        # 1. Update Telegram
        if self.telegram_bot and entry.get("telegram_msg_id") and entry.get("telegram_chat_id"):
            badge = (
                f"✅ *APPROVED by @{dec.responder_id or 'operator'}*"
                if dec.decision == "APPROVE"
                else (
                    f"❌ *DENIED by @{dec.responder_id or 'operator'}*"
                    if dec.decision == "DENY"
                    else "⏳ *TIMED OUT — Request expired.*"
                )
            )
            updated_text = (
                f"🚨 *HEX Approval: `{req.tool_name}`*\n\n"
                f"*Request ID:* `{req.request_id}`\n"
                f"*Outcome:* {badge}\n"
                f"_{dec.reason or ''}_"
            )
            try:
                self.telegram_bot.transport.call_api(
                    "editMessageText",
                    {
                        "chat_id": entry["telegram_chat_id"],
                        "message_id": entry["telegram_msg_id"],
                        "text": updated_text,
                        "parse_mode": "Markdown",
                        "reply_markup": {"inline_keyboard": []},
                    },
                )
            except Exception as exc:
                logger.debug("Failed editing Telegram message: %s", exc)

        # 2. Update Discord
        if self.discord_bot and entry.get("discord_msg_id") and entry.get("discord_channel_id"):
            color = (
                0x00FF00
                if dec.decision == "APPROVE"
                else (0xFF0000 if dec.decision == "DENY" else 0x808080)
            )
            badge = (
                f"✅ APPROVED by <@{dec.responder_id}>"
                if dec.decision == "APPROVE"
                else (
                    f"❌ DENIED by <@{dec.responder_id}>"
                    if dec.decision == "DENY"
                    else "⏳ TIMED OUT"
                )
            )
            embed = {
                "title": f"HEX Approval: {req.tool_name} — {dec.decision}",
                "color": color,
                "fields": [
                    {"name": "Request ID", "value": f"`{req.request_id}`", "inline": True},
                    {"name": "Status", "value": badge, "inline": True},
                    {"name": "Reason", "value": str(dec.reason or ""), "inline": False},
                ],
            }
            if hasattr(self.discord_bot.transport, "edit_channel_message"):
                try:
                    self.discord_bot.transport.edit_channel_message(
                        entry["discord_channel_id"],
                        entry["discord_msg_id"],
                        content=f"Approval resolution: {badge}",
                        embeds=[embed],
                        components=[],
                    )
                except Exception as exc:
                    logger.debug("Failed editing Discord message: %s", exc)
