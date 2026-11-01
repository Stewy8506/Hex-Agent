"""HEX Discord Bot Gateway.

Provides bidirectional Discord Interaction Bot interface for conversational task delegation,
operator whitelisting, rich embed status notifications, and interactive message components
(buttons) for gated R3/R4 human-in-the-loop approvals.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from harness_core.cow import compute_workspace_diff
from harness_core.models import TaskSubmitRequest
from harness_core.webhooks import _extract_approval_fields

logger = logging.getLogger("harness_core.gateways.discord_bot")


@dataclass
class DiscordConfig:
    bot_token: str | None = None
    application_id: str | None = None
    public_key: str | None = None
    webhook_url: str | None = None
    allowed_user_ids: set[str] = field(default_factory=set)
    allowed_guild_ids: set[str] = field(default_factory=set)
    allowed_channel_ids: set[str] = field(default_factory=set)
    dry_run: bool = False

    def __post_init__(self) -> None:
        if self.allowed_user_ids is not None:
            self.allowed_user_ids = {
                str(x).strip() for x in self.allowed_user_ids if str(x).strip()
            }
        else:
            self.allowed_user_ids = set()

        if self.allowed_guild_ids is not None:
            self.allowed_guild_ids = {
                str(x).strip() for x in self.allowed_guild_ids if str(x).strip()
            }
        else:
            self.allowed_guild_ids = set()

        if self.allowed_channel_ids is not None:
            self.allowed_channel_ids = {
                str(x).strip() for x in self.allowed_channel_ids if str(x).strip()
            }
        else:
            self.allowed_channel_ids = set()

    @classmethod
    def from_env(cls) -> DiscordConfig:
        bot_token = (
            os.getenv("HEX_DISCORD_BOT_TOKEN")
            or os.getenv("HARNESS_DISCORD_BOT_TOKEN")
            or None
        )
        app_id = (
            os.getenv("HEX_DISCORD_APPLICATION_ID")
            or os.getenv("HARNESS_DISCORD_APPLICATION_ID")
            or None
        )
        pub_key = (
            os.getenv("HEX_DISCORD_PUBLIC_KEY")
            or os.getenv("HARNESS_DISCORD_PUBLIC_KEY")
            or None
        )
        webhook_url = (
            os.getenv("HEX_DISCORD_WEBHOOK_URL")
            or os.getenv("HARNESS_DISCORD_WEBHOOK_URL")
            or None
        )

        def _parse_csv(val: str | None) -> set[str]:
            if not val:
                return set()
            return {x.strip() for x in val.split(",") if x.strip()}

        allowed_users = _parse_csv(
            os.getenv("HEX_DISCORD_ALLOWED_USERS")
            or os.getenv("HARNESS_DISCORD_ALLOWED_USERS")
        )
        allowed_guilds = _parse_csv(
            os.getenv("HEX_DISCORD_ALLOWED_GUILDS")
            or os.getenv("HARNESS_DISCORD_ALLOWED_GUILDS")
        )
        allowed_channels = _parse_csv(
            os.getenv("HEX_DISCORD_ALLOWED_CHANNELS")
            or os.getenv("HARNESS_DISCORD_ALLOWED_CHANNELS")
        )

        raw_dry_run = os.getenv("HEX_DISCORD_DRY_RUN") or os.getenv(
            "HARNESS_DISCORD_DRY_RUN"
        )
        dry_run = str(raw_dry_run).lower() in ("1", "true", "yes")

        return cls(
            bot_token=bot_token,
            application_id=app_id,
            public_key=pub_key,
            webhook_url=webhook_url,
            allowed_user_ids=allowed_users,
            allowed_guild_ids=allowed_guilds,
            allowed_channel_ids=allowed_channels,
            dry_run=dry_run,
        )


class DiscordTransport(Protocol):
    def send_message(self, channel_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Send a message to a Discord channel."""
        ...

    def edit_message(
        self, channel_id: str, message_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Edit an existing Discord message."""
        ...

    def create_interaction_response(
        self,
        interaction_id: str,
        interaction_token: str,
        response: dict[str, Any],
    ) -> dict[str, Any]:
        """Send an interaction response callback."""
        ...


class HttpDiscordTransport:
    """Production HTTP transport communicating with discord.com/api/v10."""

    def __init__(self, bot_token: str, base_api_url: str = "https://discord.com/api/v10"):
        self.bot_token = bot_token
        self.base_api_url = base_api_url.rstrip("/")

    def send_message(
        self, channel_id: str, payload: dict[str, Any], timeout: float = 10.0
    ) -> dict[str, Any]:
        url = f"{self.base_api_url}/channels/{channel_id}/messages"
        return self._request("POST", url, payload, timeout)

    def edit_message(
        self,
        channel_id: str,
        message_id: str,
        payload: dict[str, Any],
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        url = f"{self.base_api_url}/channels/{channel_id}/messages/{message_id}"
        return self._request("PATCH", url, payload, timeout)

    def create_interaction_response(
        self,
        interaction_id: str,
        interaction_token: str,
        response: dict[str, Any],
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        url = f"{self.base_api_url}/interactions/{interaction_id}/{interaction_token}/callback"
        return self._request("POST", url, response, timeout)

    def _request(
        self, method: str, url: str, payload: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        import urllib.error
        import urllib.request

        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bot {self.bot_token}",
            "Content-Type": "application/json",
            "User-Agent": "HEX-DiscordGateway/1.0",
        }
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {"status": resp.status}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8") if exc.fp else ""
            logger.error("Discord API HTTP %d error on %s %s: %s", exc.code, method, url, body)
            try:
                return json.loads(body)
            except Exception:
                return {"error": True, "status": exc.code, "message": body}
        except Exception as exc:
            logger.error("Discord API transport error on %s %s: %s", method, url, exc)
            return {"error": True, "message": str(exc)}


class MockDiscordTransport:
    """In-memory mock transport for hermetic testing."""

    def __init__(self) -> None:
        self.sent_messages: list[dict[str, Any]] = []
        self.edited_messages: list[dict[str, Any]] = []
        self.interaction_responses: list[dict[str, Any]] = []
        self._next_message_id = 200

    def send_message(self, channel_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._next_message_id += 1
        msg = dict(payload)
        msg["id"] = str(self._next_message_id)
        msg["channel_id"] = str(channel_id)
        self.sent_messages.append(msg)
        return msg

    def edit_message(
        self, channel_id: str, message_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        msg = dict(payload)
        msg["id"] = str(message_id)
        msg["channel_id"] = str(channel_id)
        self.edited_messages.append(msg)
        return msg

    def create_interaction_response(
        self,
        interaction_id: str,
        interaction_token: str,
        response: dict[str, Any],
    ) -> dict[str, Any]:
        entry = {
            "interaction_id": str(interaction_id),
            "interaction_token": str(interaction_token),
            "response": dict(response),
        }
        self.interaction_responses.append(entry)
        return {"status": "ok", "interaction_id": interaction_id}


class DiscordBotGateway:
    """Bidirectional Discord Interaction Bot Gateway.

    Handles Discord interaction payloads (PING, slash commands, button components),
    enforces operator whitelists, dispatches tasks, and renders rich embeds and buttons.
    """

    def __init__(
        self,
        orchestrator: Any | None,
        config: DiscordConfig,
        transport: DiscordTransport | None = None,
    ) -> None:
        self.orchestrator = orchestrator
        self.config = config
        if transport is not None:
            self.transport = transport
        elif config.dry_run or not config.bot_token:
            self.transport = MockDiscordTransport()
        else:
            self.transport = HttpDiscordTransport(config.bot_token)

    def is_user_authorized(self, user_id: str | int | None) -> bool:
        """Verify user is in whitelist (strict default-deny)."""
        if not self.config.allowed_user_ids:
            return False
        if user_id is None:
            return False
        return str(user_id).strip() in self.config.allowed_user_ids

    def is_guild_authorized(self, guild_id: str | int | None) -> bool:
        """Verify guild is in whitelist (if allowed_guild_ids configured)."""
        if not self.config.allowed_guild_ids:
            return True
        if guild_id is None:
            return False
        return str(guild_id).strip() in self.config.allowed_guild_ids

    def is_channel_authorized(self, channel_id: str | int | None) -> bool:
        """Verify channel is in whitelist (if allowed_channel_ids configured)."""
        if not self.config.allowed_channel_ids:
            return True
        if channel_id is None:
            return False
        return str(channel_id).strip() in self.config.allowed_channel_ids

    def check_access(
        self,
        user_id: str | int | None,
        guild_id: str | int | None = None,
        channel_id: str | int | None = None,
    ) -> tuple[bool, str]:
        """Operator whitelist access control with security audit logging."""
        if not self.is_user_authorized(user_id):
            logger.warning(
                "SECURITY_AUDIT: Dropped unauthorized Discord interaction from user=%s guild=%s channel=%s (unauthorized user)",
                user_id,
                guild_id,
                channel_id,
            )
            return False, "Unauthorized operator"

        if not self.is_guild_authorized(guild_id):
            logger.warning(
                "SECURITY_AUDIT: Dropped unauthorized Discord interaction from user=%s guild=%s channel=%s (unauthorized guild)",
                user_id,
                guild_id,
                channel_id,
            )
            return False, "Unauthorized guild"

        if not self.is_channel_authorized(channel_id):
            logger.warning(
                "SECURITY_AUDIT: Dropped unauthorized Discord interaction from user=%s guild=%s channel=%s (unauthorized channel)",
                user_id,
                guild_id,
                channel_id,
            )
            return False, "Unauthorized channel"

        return True, "Authorized"

    def _compute_task_diff(self, task_id: str) -> str:
        """Compute workspace unified diff between base checkpoint and attempt workspace."""
        if not self.orchestrator:
            return ""
        chk = self.orchestrator.ledger.get_latest_checkpoint(task_id)
        attempts = self.orchestrator.ledger.list_attempts(task_id)
        if not attempts:
            return ""
        attempt_ws = attempts[-1].get("workspace_path")
        if not attempt_ws or not Path(attempt_ws).is_dir():
            attempt_ws = str(self.orchestrator.default_workspace)

        base_dir: str | None = None
        if chk and chk.get("snapshot_path") and Path(chk["snapshot_path"]).is_dir():
            base_dir = chk["snapshot_path"]
        else:
            base_dir = str(self.orchestrator.default_workspace)

        if str(base_dir) == str(attempt_ws):
            return ""
        return compute_workspace_diff(base_dir, attempt_ws)

    def handle_interaction(self, interaction: dict[str, Any]) -> dict[str, Any]:
        """Process incoming Discord interaction payload."""
        itype = interaction.get("type")

        # Type 1: PING -> PONG
        if itype == 1:
            resp = {"type": 1}
            self._record_callback(interaction, resp)
            return resp

        # Extract user / member
        user_data = interaction.get("user") or (interaction.get("member") or {}).get("user") or {}
        user_id = str(user_data.get("id")) if user_data.get("id") is not None else None
        guild_id = str(interaction.get("guild_id")) if interaction.get("guild_id") is not None else None
        channel_id = str(
            interaction.get("channel_id")
            or (interaction.get("channel") or {}).get("id")
            or ""
        ) or None

        # Type 2: APPLICATION_COMMAND
        if itype == 2:
            resp = self._handle_application_command(
                interaction, user_id=user_id, guild_id=guild_id, channel_id=channel_id
            )
            self._record_callback(interaction, resp)
            return resp

        # Type 3: MESSAGE_COMPONENT
        if itype == 3:
            resp = self._handle_message_component(
                interaction, user_id=user_id, guild_id=guild_id, channel_id=channel_id
            )
            self._record_callback(interaction, resp)
            return resp

        # Unknown interaction type
        resp = {
            "type": 4,
            "data": {
                "content": f"⚠️ Unsupported interaction type: {itype}",
                "flags": 64,
            },
        }
        self._record_callback(interaction, resp)
        return resp

    def _record_callback(self, interaction: dict[str, Any], resp: dict[str, Any]) -> None:
        iid = interaction.get("id")
        itoken = interaction.get("token")
        if iid and itoken:
            try:
                self.transport.create_interaction_response(str(iid), str(itoken), resp)
            except Exception as exc:
                logger.warning("Failed to record interaction response: %s", exc)

    def _handle_application_command(
        self,
        interaction: dict[str, Any],
        user_id: str | None,
        guild_id: str | None,
        channel_id: str | None,
    ) -> dict[str, Any]:
        ok, _ = self.check_access(user_id, guild_id, channel_id)
        if not ok:
            return {
                "type": 4,
                "data": {
                    "content": "⛔ Unauthorized operator. Access denied.",
                    "flags": 64,
                },
            }

        cmd_data = interaction.get("data") or {}
        cmd_name = str(cmd_data.get("name", "")).lower()
        options_list = cmd_data.get("options") or []
        options = {opt.get("name"): opt.get("value") for opt in options_list if isinstance(opt, dict)}

        if cmd_name == "help":
            help_text = (
                "🤖 **HEX Autonomous AI Workbench (Discord)**\n\n"
                "Available slash commands:\n"
                "• `/task <goal>` — Submit a new autonomous task\n"
                "• `/status [task_id]` — Check task status or list recent tasks\n"
                "• `/diff [task_id]` — View CoW workspace unified diff\n"
                "• `/cancel [task_id]` — Cancel task & roll back workspace\n"
                "• `/help` — Display this command reference"
            )
            return {"type": 4, "data": {"content": help_text}}

        if cmd_name == "task":
            goal = options.get("goal")
            if not goal and options_list and isinstance(options_list[0], dict):
                goal = options_list[0].get("value")
            if not goal:
                return {
                    "type": 4,
                    "data": {
                        "content": "❌ Please specify a goal for the task. Example: `/task goal: Fix lint errors`",
                        "flags": 64,
                    },
                }

            if not self.orchestrator:
                return {
                    "type": 4,
                    "data": {
                        "content": "❌ Orchestrator not connected.",
                        "flags": 64,
                    },
                }

            try:
                req = TaskSubmitRequest(goal=str(goal), project_id="discord")
                task = self.orchestrator.submit_and_run(req, auto_execute=True)
                task_id = task.get("task_id", "unknown")
                status = task.get("status", "QUEUED")
                content = (
                    f"🚀 **Task Submitted: `{task_id}`**\n\n"
                    f"**Goal:** {goal}\n"
                    f"**Status:** `{status}`\n"
                    f"**Project:** `discord`"
                )
                embed = {
                    "title": f"🚀 Task {task_id}",
                    "color": 0x3498DB,
                    "fields": [
                        {"name": "Task ID", "value": f"`{task_id}`", "inline": True},
                        {"name": "Status", "value": f"`{status}`", "inline": True},
                        {"name": "Goal", "value": str(goal), "inline": False},
                    ],
                }
                return {
                    "type": 4,
                    "data": {
                        "content": content,
                        "embeds": [embed],
                    },
                }
            except Exception as exc:
                logger.error("Failed to submit task from Discord command: %s", exc)
                return {
                    "type": 4,
                    "data": {
                        "content": f"❌ Error submitting task: {exc}",
                        "flags": 64,
                    },
                }

        if cmd_name == "status":
            task_id = options.get("task_id") or options.get("id")
            if not self.orchestrator:
                return {
                    "type": 4,
                    "data": {
                        "content": "❌ Orchestrator not connected.",
                        "flags": 64,
                    },
                }

            if task_id:
                task = self.orchestrator.ledger.get_task(str(task_id))
                if not task:
                    return {
                        "type": 4,
                        "data": {
                            "content": f"❌ Task `{task_id}` not found.",
                            "flags": 64,
                        },
                    }
                st = task.get("status")
                color = 0x00FF00 if st == "SUCCEEDED" else (0xFF0000 if st in ("FAILED", "CANCELED") else 0xFFA500)
                content = f"📊 **Task Status: `{task_id}`**\n\n**Status:** `{st}`\n**Goal:** {task.get('goal', '')}"
                embed = {
                    "title": f"📊 Task Status: {task_id}",
                    "color": color,
                    "fields": [
                        {"name": "Status", "value": f"`{st}`", "inline": True},
                        {"name": "Goal", "value": str(task.get("goal", "")), "inline": False},
                    ],
                }
                return {
                    "type": 4,
                    "data": {
                        "content": content,
                        "embeds": [embed],
                    },
                }
            else:
                tasks = self.orchestrator.ledger.list_tasks(limit=5)
                if not tasks:
                    return {
                        "type": 4,
                        "data": {"content": "📋 No tasks found in ledger."},
                    }
                lines = []
                for t in tasks:
                    emoji = "✅" if t.get("status") == "SUCCEEDED" else ("❌" if t.get("status") in ("FAILED", "CANCELED") else "⏳")
                    lines.append(f"{emoji} `{t.get('task_id')}` — `{t.get('status')}`: {t.get('goal', '')[:50]}")
                content = "📋 **Recent HEX Tasks**\n\n" + "\n".join(lines)
                embed = {
                    "title": "📋 Recent Tasks",
                    "color": 0x3498DB,
                    "description": "\n".join(lines),
                }
                return {
                    "type": 4,
                    "data": {
                        "content": content,
                        "embeds": [embed],
                    },
                }

        if cmd_name == "diff":
            if not self.orchestrator:
                return {
                    "type": 4,
                    "data": {"content": "❌ Orchestrator not connected.", "flags": 64},
                }
            task_id = options.get("task_id") or options.get("id")
            if not task_id:
                tasks = self.orchestrator.ledger.list_tasks(limit=1)
                task_id = tasks[0]["task_id"] if tasks else None

            if not task_id:
                return {
                    "type": 4,
                    "data": {"content": "ℹ️ No tasks found to compute diff.", "flags": 64},
                }

            diff = self._compute_task_diff(str(task_id))
            if diff:
                if len(diff) > 1900:
                    diff = diff[:1900] + "\n... (truncated)"
                content = f"```diff\n{diff}\n```"
            else:
                content = f"ℹ️ No workspace modifications detected for task `{task_id}`."
            return {"type": 4, "data": {"content": content}}

        if cmd_name == "cancel":
            if not self.orchestrator:
                return {
                    "type": 4,
                    "data": {"content": "❌ Orchestrator not connected.", "flags": 64},
                }
            task_id = options.get("task_id") or options.get("id")
            if not task_id:
                tasks = self.orchestrator.ledger.list_tasks(limit=1)
                task_id = tasks[0]["task_id"] if tasks else None

            if not task_id:
                return {
                    "type": 4,
                    "data": {"content": "ℹ️ No task specified to cancel.", "flags": 64},
                }

            try:
                self.orchestrator.cancel_task(str(task_id), actor=f"discord:{user_id}")
                return {
                    "type": 4,
                    "data": {
                        "content": f"⏹ Task `{task_id}` has been canceled and uncommitted CoW workspace rolled back."
                    },
                }
            except Exception as exc:
                return {
                    "type": 4,
                    "data": {"content": f"❌ Failed to cancel task `{task_id}`: {exc}", "flags": 64},
                }

        return {
            "type": 4,
            "data": {
                "content": f"❓ Unknown slash command: `/{cmd_name}`. Use `/help` for options.",
                "flags": 64,
            },
        }

    def _handle_message_component(
        self,
        interaction: dict[str, Any],
        user_id: str | None,
        guild_id: str | None,
        channel_id: str | None,
    ) -> dict[str, Any]:
        ok, _ = self.check_access(user_id, guild_id, channel_id)
        if not ok:
            return {
                "type": 4,
                "data": {
                    "content": "⛔ Unauthorized operator. Access denied.",
                    "flags": 64,
                },
            }

        custom_id = str((interaction.get("data") or {}).get("custom_id", ""))
        parts = custom_id.split(":")
        if len(parts) >= 3 and parts[0] == "hex":
            action = parts[1]
            approval_id = parts[2]
        elif len(parts) >= 2:
            action = parts[0]
            approval_id = parts[1]
        else:
            action = custom_id
            approval_id = ""

        if not self.orchestrator:
            return {
                "type": 4,
                "data": {"content": "❌ Orchestrator not connected.", "flags": 64},
            }

        approval_record = self.orchestrator.ledger.get_approval(approval_id)
        if not approval_record and action != "help":
            return {
                "type": 4,
                "data": {
                    "content": f"❌ Approval `{approval_id}` not found.",
                    "flags": 64,
                },
            }

        task_id = approval_record.get("task_id") if approval_record else None
        orig_embeds = (interaction.get("message") or {}).get("embeds") or []
        base_embed = dict(orig_embeds[0]) if orig_embeds else {}

        if action == "approve":
            self.orchestrator.ledger.decide_approval(
                approval_id,
                "APPROVED",
                decided_by=f"discord:{user_id}",
            )
            if task_id:
                try:
                    self.orchestrator.resume_task(task_id)
                except Exception as exc:
                    logger.error("Failed to resume task %s on Discord approval: %s", task_id, exc)

            base_embed["color"] = 0x00FF00
            fields = list(base_embed.get("fields", []))
            fields.append({"name": "Decision", "value": f"✅ APPROVED by <@{user_id}>", "inline": False})
            base_embed["fields"] = fields

            return {
                "type": 7,
                "data": {
                    "content": f"✅ Approval `{approval_id}` was **APPROVED** by <@{user_id}>. Execution resumed.",
                    "embeds": [base_embed],
                    "components": [],
                },
            }

        elif action == "reject":
            self.orchestrator.ledger.decide_approval(
                approval_id,
                "REJECTED",
                decided_by=f"discord:{user_id}",
            )
            base_embed["color"] = 0xFF0000
            fields = list(base_embed.get("fields", []))
            fields.append({"name": "Decision", "value": f"❌ REJECTED by <@{user_id}>", "inline": False})
            base_embed["fields"] = fields

            return {
                "type": 7,
                "data": {
                    "content": f"❌ Approval `{approval_id}` was **REJECTED** by <@{user_id}>.",
                    "embeds": [base_embed],
                    "components": [],
                },
            }

        elif action == "diff":
            diff = self._compute_task_diff(str(task_id)) if task_id else ""
            if diff:
                if len(diff) > 1900:
                    diff = diff[:1900] + "\n... (truncated)"
                content = f"```diff\n{diff}\n```"
            else:
                content = f"ℹ️ No workspace modifications detected for task `{task_id}`."
            return {
                "type": 4,
                "data": {
                    "content": content,
                    "flags": 64,
                },
            }

        elif action == "cancel":
            if task_id:
                try:
                    self.orchestrator.cancel_task(str(task_id), actor=f"discord:{user_id}")
                except Exception as exc:
                    logger.error("Failed to cancel task %s on Discord button: %s", task_id, exc)

            base_embed["color"] = 0x808080
            fields = list(base_embed.get("fields", []))
            fields.append({"name": "Status", "value": f"⏹ CANCELED by <@{user_id}> (CoW rolled back)", "inline": False})
            base_embed["fields"] = fields

            return {
                "type": 7,
                "data": {
                    "content": f"⏹ Task `{task_id}` was canceled by <@{user_id}> and workspace rolled back.",
                    "embeds": [base_embed],
                    "components": [],
                },
            }

        return {
            "type": 4,
            "data": {
                "content": f"⚠️ Unknown component action: `{action}`",
                "flags": 64,
            },
        }

    def send_approval_request(
        self, approval_record: dict[str, Any], channel_id: str
    ) -> dict[str, Any]:
        """Dispatch rich approval embed with interactive ActionRow buttons to a channel."""
        data = _extract_approval_fields(approval_record)
        risk_level = data["risk_level"]
        color = 0xFFA500 if risk_level == "R3" else 0xFF0000

        params_str = json.dumps(data["scrubbed_params"], indent=2)
        if len(params_str) > 1000:
            params_str = params_str[:1000] + "\n..."

        embed = {
            "title": f"🚨 Approval Required: {data['action_type']} ({risk_level})",
            "color": color,
            "description": f"Approval `{data['approval_id']}` requires human authorization before execution.",
            "fields": [
                {"name": "Task ID", "value": f"`{data['task_id']}`", "inline": True},
                {"name": "Step ID", "value": str(data["step_id"] or "N/A"), "inline": True},
                {"name": "Risk Class", "value": f"`{risk_level}`", "inline": True},
                {"name": "Blast Radius Summary", "value": data["scrubbed_summary"], "inline": False},
                {"name": "Parameters", "value": f"```json\n{params_str}\n```", "inline": False},
            ],
        }

        action_row = {
            "type": 1,
            "components": [
                {
                    "type": 2,
                    "style": 3,
                    "label": "Approve",
                    "custom_id": f"hex:approve:{data['approval_id']}",
                },
                {
                    "type": 2,
                    "style": 4,
                    "label": "Reject",
                    "custom_id": f"hex:reject:{data['approval_id']}",
                },
                {
                    "type": 2,
                    "style": 2,
                    "label": "View Diff",
                    "custom_id": f"hex:diff:{data['approval_id']}",
                },
                {
                    "type": 2,
                    "style": 2,
                    "label": "Cancel",
                    "custom_id": f"hex:cancel:{data['approval_id']}",
                },
            ],
        }

        payload = {
            "content": f"🚨 **Privileged Approval Required ({risk_level})**",
            "embeds": [embed],
            "components": [action_row],
        }
        return self.transport.send_message(channel_id, payload)
