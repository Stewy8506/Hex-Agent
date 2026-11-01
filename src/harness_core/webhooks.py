"""Outbound approval webhooks (Telegram Bot API and Discord Webhook) and notification management.

Provides rich, secret-scrubbed notifications with direct Approve/Reject URLs
for R3/R4 authorization gates.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .gateway import redact_secrets

logger = logging.getLogger(__name__)


@dataclass
class WebhookConfig:
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    discord_webhook_url: str | None = None
    public_base_url: str = "http://127.0.0.1:8420"
    dry_run: bool = False
    timeout: float = 5.0

    @classmethod
    def from_env(cls) -> WebhookConfig:
        return cls(
            telegram_bot_token=os.getenv("HEX_TELEGRAM_BOT_TOKEN") or os.getenv("HARNESS_TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=os.getenv("HEX_TELEGRAM_CHAT_ID") or os.getenv("HARNESS_TELEGRAM_CHAT_ID"),
            discord_webhook_url=os.getenv("HEX_DISCORD_WEBHOOK_URL") or os.getenv("HARNESS_DISCORD_WEBHOOK_URL"),
            public_base_url=(
                os.getenv("HEX_PUBLIC_BASE_URL")
                or os.getenv("HEX_BASE_URL")
                or os.getenv("HARNESS_PUBLIC_BASE_URL")
                or os.getenv("HARNESS_BASE_URL")
                or "http://127.0.0.1:8420"
            ),
            dry_run=(
                os.getenv("HEX_WEBHOOK_DRY_RUN", "").lower() in ("1", "true", "yes")
                or os.getenv("HARNESS_WEBHOOK_DRY_RUN", "").lower() in ("1", "true", "yes")
            ),
            timeout=float(os.getenv("HEX_WEBHOOK_TIMEOUT") or os.getenv("HARNESS_WEBHOOK_TIMEOUT", "5.0")),
        )


def _extract_approval_fields(approval_record: dict[str, Any]) -> dict[str, Any]:
    approval_id = str(approval_record.get("approval_id") or "unknown")
    task_id = str(approval_record.get("task_id") or "unknown")
    step_id = approval_record.get("step_id")
    action_type = str(approval_record.get("action_type") or "unknown")
    risk_level = str(
        approval_record.get("risk_class")
        or approval_record.get("risk_level")
        or "R3"
    ).upper()

    raw_params = approval_record.get("action_params")
    if raw_params is None and "action_params_json" in approval_record:
        p_json = approval_record["action_params_json"]
        if isinstance(p_json, str):
            try:
                raw_params = json.loads(p_json)
            except Exception:
                raw_params = p_json
        else:
            raw_params = p_json
    if raw_params is None:
        raw_params = {}

    scrubbed_params = redact_secrets(raw_params)
    raw_summary = str(approval_record.get("blast_radius_summary") or "Approval required for privileged action.")
    scrubbed_summary = redact_secrets(raw_summary)

    return {
        "approval_id": approval_id,
        "task_id": task_id,
        "step_id": step_id,
        "action_type": action_type,
        "risk_level": risk_level,
        "scrubbed_params": scrubbed_params,
        "scrubbed_summary": scrubbed_summary,
    }


def format_telegram_approval(approval_record: dict[str, Any], base_url: str) -> dict[str, Any]:
    """Formats an approval request for Telegram Bot API sendMessage."""
    data = _extract_approval_fields(approval_record)
    base = base_url.rstrip("/")
    approve_url = f"{base}/v1/approvals/{data['approval_id']}:decide?decision=approve"
    reject_url = f"{base}/v1/approvals/{data['approval_id']}:decide?decision=reject"

    try:
        params_str = json.dumps(data["scrubbed_params"], indent=2, sort_keys=True)
    except Exception:
        params_str = str(data["scrubbed_params"])
    if len(params_str) > 1000:
        params_str = params_str[:997] + "..."

    text = (
        f"🚨 *HEX Approval Request ({data['risk_level']})*\n\n"
        f"*Task ID:* `{data['task_id']}`\n"
        f"*Step ID:* `{data['step_id'] or 'N/A'}`\n"
        f"*Tool Name:* `{data['action_type']}`\n"
        f"*Risk Class:* `{data['risk_level']}`\n"
        f"*Blast Radius:* {data['scrubbed_summary']}\n\n"
        f"*Parameters:*\n```json\n{params_str}\n```\n\n"
        f"[✅ Approve]({approve_url})  |  [❌ Reject]({reject_url})"
    )

    payload: dict[str, Any] = {
        "text": text,
        "parse_mode": "Markdown",
        "reply_markup": {
            "inline_keyboard": [
                [
                    {"text": "✅ Approve", "url": approve_url},
                    {"text": "❌ Reject", "url": reject_url},
                ]
            ]
        },
        "approval_id": data["approval_id"],
    }
    return payload


def format_discord_approval(approval_record: dict[str, Any], base_url: str) -> dict[str, Any]:
    """Formats an approval request for Discord Webhook execution."""
    data = _extract_approval_fields(approval_record)
    base = base_url.rstrip("/")
    approve_url = f"{base}/v1/approvals/{data['approval_id']}:decide?decision=approve"
    reject_url = f"{base}/v1/approvals/{data['approval_id']}:decide?decision=reject"

    try:
        params_str = json.dumps(data["scrubbed_params"], indent=2, sort_keys=True)
    except Exception:
        params_str = str(data["scrubbed_params"])
    if len(params_str) > 1000:
        params_str = params_str[:997] + "..."

    color = 0xFF0000 if data["risk_level"] == "R4" else 0xFF9900

    embed = {
        "title": f"🚨 Approval Required: {data['action_type']} ({data['risk_level']})",
        "color": color,
        "description": f"[✅ Approve]({approve_url})  |  [❌ Reject]({reject_url})",
        "fields": [
            {"name": "Task ID", "value": data["task_id"], "inline": True},
            {"name": "Step ID", "value": str(data["step_id"] or "N/A"), "inline": True},
            {"name": "Risk Class", "value": data["risk_level"], "inline": True},
            {"name": "Blast Radius Summary", "value": data["scrubbed_summary"], "inline": False},
            {"name": "Parameters", "value": f"```json\n{params_str}\n```", "inline": False},
        ],
    }

    return {
        "content": f"🚨 **Approval Required ({data['risk_level']})**",
        "embeds": [embed],
        "approval_id": data["approval_id"],
    }


def format_decision_broadcast(approval_record: dict[str, Any], decision: str, comment: str = "") -> dict[str, Any]:
    """Formats a decision broadcast for channels after an approval has been decided."""
    data = _extract_approval_fields(approval_record)
    norm_decision = decision.upper()
    emoji = "✅" if norm_decision == "APPROVED" else "❌"
    comment_clean = redact_secrets(comment) if comment else ""
    comment_part = f"\n*Comment:* {comment_clean}" if comment_clean else ""

    tg_text = (
        f"{emoji} *Approval {data['approval_id']}: {norm_decision}*\n\n"
        f"*Task ID:* `{data['task_id']}`\n"
        f"*Tool:* `{data['action_type']}`\n"
        f"*Decision:* `{norm_decision}`{comment_part}"
    )

    dc_color = 0x00FF00 if norm_decision == "APPROVED" else 0xFF0000
    dc_fields = [
        {"name": "Task ID", "value": data["task_id"], "inline": True},
        {"name": "Tool", "value": data["action_type"], "inline": True},
        {"name": "Decision", "value": norm_decision, "inline": True},
    ]
    if comment_clean:
        dc_fields.append({"name": "Comment", "value": comment_clean, "inline": False})

    return {
        "telegram": {
            "text": tg_text,
            "parse_mode": "Markdown",
            "approval_id": data["approval_id"],
        },
        "discord": {
            "content": f"{emoji} Approval `{data['approval_id']}` was **{norm_decision}**",
            "embeds": [
                {
                    "title": f"{emoji} Approval {data['approval_id']}: {norm_decision}",
                    "color": dc_color,
                    "fields": dc_fields,
                }
            ],
            "approval_id": data["approval_id"],
        },
    }


class WebhookNotifier:
    """Manages outbound dispatch to Telegram Bot API and Discord Webhook endpoints."""

    def __init__(self, config: WebhookConfig | None = None):
        self.config = config or WebhookConfig.from_env()
        self.sent_payloads: list[dict[str, Any]] = []

    def _dispatch_http(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "HEX-WebhookNotifier/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout) as resp:
                body = resp.read().decode("utf-8")
                return {"status": resp.status, "body": body}
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
            logger.warning("Webhook dispatch HTTP error %s to %s: %s", e.code, url, body)
            return {"status": e.code, "error": str(e), "body": body}
        except Exception as e:
            logger.warning("Webhook dispatch failed to %s: %s", url, e)
            return {"status": 500, "error": str(e)}

    def send_approval_request(self, approval_record: dict[str, Any]) -> dict[str, Any]:
        """Dispatches rich approval notifications to Telegram and/or Discord."""
        base_url = self.config.public_base_url
        tg_payload = format_telegram_approval(approval_record, base_url)
        dc_payload = format_discord_approval(approval_record, base_url)

        if self.config.telegram_chat_id:
            tg_payload["chat_id"] = self.config.telegram_chat_id

        results: dict[str, Any] = {}

        if self.config.dry_run or (not self.config.telegram_bot_token and not self.config.discord_webhook_url):
            self.sent_payloads.append({"target": "telegram", "payload": tg_payload})
            self.sent_payloads.append({"target": "discord", "payload": dc_payload})
            status = "dry_run" if self.config.dry_run else "noop"
            return {"status": status, "telegram": tg_payload, "discord": dc_payload}

        if self.config.telegram_bot_token and self.config.telegram_chat_id:
            tg_url = f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendMessage"
            res_tg = self._dispatch_http(tg_url, tg_payload)
            results["telegram"] = res_tg
            self.sent_payloads.append({"target": "telegram", "payload": tg_payload, "response": res_tg})

        if self.config.discord_webhook_url:
            res_dc = self._dispatch_http(self.config.discord_webhook_url, dc_payload)
            results["discord"] = res_dc
            self.sent_payloads.append({"target": "discord", "payload": dc_payload, "response": res_dc})

        return results

    def send_decision_broadcast(self, approval_record: dict[str, Any], decision: str, comment: str = "") -> dict[str, Any]:
        """Dispatches decision broadcast notifications to Telegram and/or Discord."""
        broadcast_data = format_decision_broadcast(approval_record, decision, comment)
        tg_payload = broadcast_data["telegram"]
        dc_payload = broadcast_data["discord"]

        if self.config.telegram_chat_id:
            tg_payload["chat_id"] = self.config.telegram_chat_id

        results: dict[str, Any] = {}

        if self.config.dry_run or (not self.config.telegram_bot_token and not self.config.discord_webhook_url):
            self.sent_payloads.append({"target": "telegram_decision", "payload": tg_payload})
            self.sent_payloads.append({"target": "discord_decision", "payload": dc_payload})
            status = "dry_run" if self.config.dry_run else "noop"
            return {"status": status, "telegram": tg_payload, "discord": dc_payload}

        if self.config.telegram_bot_token and self.config.telegram_chat_id:
            tg_url = f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendMessage"
            res_tg = self._dispatch_http(tg_url, tg_payload)
            results["telegram"] = res_tg
            self.sent_payloads.append({"target": "telegram_decision", "payload": tg_payload, "response": res_tg})

        if self.config.discord_webhook_url:
            res_dc = self._dispatch_http(self.config.discord_webhook_url, dc_payload)
            results["discord"] = res_dc
            self.sent_payloads.append({"target": "discord_decision", "payload": dc_payload, "response": res_dc})

        return results
