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

from harness_core.cow import compute_workspace_diff
from harness_core.gateway import redact_secrets
from harness_core.models import TaskSubmitRequest
from harness_core.webhooks import _extract_approval_fields

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

    def handle_message(self, message: dict[str, Any]) -> dict[str, Any]:
        """Dispatch conversational messages and slash commands."""
        user_id = (message.get("from") or {}).get("id")
        chat_id = (message.get("chat") or {}).get("id")
        text = str(message.get("text") or "").strip()

        ok, reason = self.check_access(user_id, chat_id)
        if not ok:
            refusal_text = "⛔ Unauthorized operator. Access denied."
            if chat_id is not None:
                self.transport.call_api("sendMessage", {
                    "chat_id": chat_id,
                    "text": refusal_text,
                })
            return {"ok": False, "reason": reason, "text": refusal_text}

        if not text:
            return {"ok": False, "reason": "empty_text"}

        # Route /start and /help
        if text in ("/start", "/help") or text.startswith(("/start ", "/help ")):
            help_text = (
                "🤖 *HEX Autonomous AI Workbench*\n\n"
                "Available commands:\n"
                "• `/task <goal>` — Submit a new autonomous task\n"
                "• `/status [id]` — Check status of recent tasks or specific task\n"
                "• `/diff [id]` — View CoW workspace unified diff\n"
                "• `/cancel [id]` — Cancel task & roll back workspace\n"
                "• `/help` — Display this command reference\n\n"
                "_Or simply type any message to delegate a task immediately._"
            )
            self.transport.call_api("sendMessage", {
                "chat_id": chat_id,
                "text": help_text,
                "parse_mode": "Markdown",
            })
            return {"ok": True, "action": "help", "text": help_text}

        # Route /status [id]
        if text == "/status" or text.startswith("/status "):
            arg = text[len("/status"):].strip()
            if arg:
                if not self.orchestrator:
                    card = f"❌ Orchestrator not connected."
                    self.transport.call_api("sendMessage", {"chat_id": chat_id, "text": card})
                    return {"ok": False, "reason": "no_orchestrator", "text": card}

                task = self.orchestrator.ledger.get_task(arg)
                if not task:
                    card = f"❌ *Task Not Found:* `{arg}`"
                else:
                    attempts = self.orchestrator.ledger.list_attempts(arg)
                    step = attempts[-1].get("active_step_id", "N/A") if attempts else "N/A"
                    reason = task.get("terminal_reason") or "None"
                    created = task.get("created_at") or "N/A"
                    updated = task.get("updated_at") or "N/A"
                    card = (
                        f"📋 *Task Status: `{arg}`*\n\n"
                        f"*Status:* `{task['status']}`\n"
                        f"*Goal:* {task.get('goal', '')}\n"
                        f"*Step:* `{step}`\n"
                        f"*Reason:* {reason}\n"
                        f"*Created:* `{created}`\n"
                        f"*Updated:* `{updated}`"
                    )
                self.transport.call_api("sendMessage", {
                    "chat_id": chat_id,
                    "text": card,
                    "parse_mode": "Markdown",
                })
                return {"ok": True, "action": "status", "text": card, "task": task}
            else:
                if not self.orchestrator:
                    card = "❌ Orchestrator not connected."
                    self.transport.call_api("sendMessage", {"chat_id": chat_id, "text": card})
                    return {"ok": False, "reason": "no_orchestrator", "text": card}

                tasks = self.orchestrator.ledger.list_tasks(limit=5)
                if not tasks:
                    card = "📋 *No recent tasks found.*"
                else:
                    lines = ["📋 *Recent Tasks:*\n"]
                    for t in tasks:
                        tid = t.get("task_id", "")
                        st = t.get("status", "")
                        gl = t.get("goal", "")
                        if len(gl) > 40:
                            gl = gl[:37] + "..."
                        lines.append(f"• `{tid}`: *{st}* — {gl}")
                    card = "\n".join(lines)
                self.transport.call_api("sendMessage", {
                    "chat_id": chat_id,
                    "text": card,
                    "parse_mode": "Markdown",
                })
                return {"ok": True, "action": "status_list", "text": card, "tasks": tasks}

        # Route /diff [id]
        if text == "/diff" or text.startswith("/diff "):
            arg = text[len("/diff"):].strip()
            if not arg:
                if not self.orchestrator:
                    card = "❌ Orchestrator not connected."
                    self.transport.call_api("sendMessage", {"chat_id": chat_id, "text": card})
                    return {"ok": False, "reason": "no_orchestrator", "text": card}
                recent = self.orchestrator.ledger.list_tasks(limit=1)
                if recent:
                    arg = recent[0]["task_id"]
                else:
                    card = "⚠️ No task ID provided and no recent tasks found."
                    self.transport.call_api("sendMessage", {"chat_id": chat_id, "text": card})
                    return {"ok": False, "reason": "no_task", "text": card}

            diff_text = self._compute_task_diff(arg)
            if not diff_text or not diff_text.strip():
                card = f"ℹ️ *No workspace diff found for task* `{arg}`."
            else:
                if len(diff_text) > 3500:
                    diff_text = diff_text[:3450] + "\n... [truncated]"
                card = f"🔍 *Workspace Diff for Task `{arg}`:*\n```diff\n{diff_text}\n```"

            self.transport.call_api("sendMessage", {
                "chat_id": chat_id,
                "text": card,
                "parse_mode": "Markdown",
            })
            return {"ok": True, "action": "diff", "text": card, "task_id": arg}

        # Route /cancel [id]
        if text == "/cancel" or text.startswith("/cancel "):
            arg = text[len("/cancel"):].strip()
            if not arg:
                card = "⚠️ Please specify a task ID to cancel, e.g.: `/cancel <task_id>`"
                self.transport.call_api("sendMessage", {
                    "chat_id": chat_id,
                    "text": card,
                    "parse_mode": "Markdown",
                })
                return {"ok": False, "reason": "missing_task_id", "text": card}

            if not self.orchestrator:
                card = "❌ Orchestrator not connected."
                self.transport.call_api("sendMessage", {"chat_id": chat_id, "text": card})
                return {"ok": False, "reason": "no_orchestrator", "text": card}

            try:
                self.orchestrator.cancel_task(arg, actor=f"tg:{user_id}")
                card = f"⏹ *Task Canceled:* `{arg}`\nWorkspace uncommitted changes rolled back."
            except Exception as exc:
                card = f"❌ *Failed to cancel task `{arg}`:* {exc}"

            self.transport.call_api("sendMessage", {
                "chat_id": chat_id,
                "text": card,
                "parse_mode": "Markdown",
            })
            return {"ok": True, "action": "cancel", "text": card, "task_id": arg}

        # Route /task <goal> or direct conversational text prompt
        goal = ""
        if text.startswith("/task"):
            goal = text[len("/task"):].strip()
            if not goal:
                card = "⚠️ Please specify a task goal, e.g.: `/task Fix memory leak in worker`"
                self.transport.call_api("sendMessage", {
                    "chat_id": chat_id,
                    "text": card,
                    "parse_mode": "Markdown",
                })
                return {"ok": False, "reason": "empty_goal", "text": card}
        else:
            goal = text

        if not self.orchestrator:
            card = "❌ Orchestrator not connected."
            self.transport.call_api("sendMessage", {"chat_id": chat_id, "text": card})
            return {"ok": False, "reason": "no_orchestrator", "text": card}

        steps = message.get("steps")
        req = TaskSubmitRequest(
            goal=goal,
            project_id="telegram",
            workspace_root=str(self.orchestrator.default_workspace),
            steps=steps,
        )
        task = self.orchestrator.submit_and_run(req)
        task_id = task.get("task_id", "unknown")
        status = task.get("status", "SUBMITTED")

        card = (
            f"🚀 *Task Submitted: `{task_id}`*\n\n"
            f"*Status:* `{status}`\n"
            f"*Goal:* {goal}"
        )
        self.transport.call_api("sendMessage", {
            "chat_id": chat_id,
            "text": card,
            "parse_mode": "Markdown",
        })

        if status == "WAITING_APPROVAL":
            approvals = self.orchestrator.ledger.list_approvals(
                task_id=task_id, decision="PENDING"
            )
            if approvals:
                self.send_approval_request(approvals[0], chat_id=chat_id)

        return {
            "ok": True,
            "action": "task_submitted",
            "task_id": task_id,
            "task": task,
            "text": card,
        }

    def send_approval_request(
        self,
        approval_record: dict[str, Any],
        chat_id: str | int | None = None,
    ) -> dict[str, Any]:
        """Format approval request and send interactive inline keyboard card."""
        target_chat_id = chat_id
        if target_chat_id is None:
            if self.config.allowed_chat_ids:
                target_chat_id = next(iter(self.config.allowed_chat_ids))
            elif self.config.allowed_user_ids:
                target_chat_id = next(iter(self.config.allowed_user_ids))
            else:
                logger.error("No chat_id available to send approval request")
                return {"ok": False, "description": "No destination chat_id configured"}

        data = _extract_approval_fields(approval_record)
        approval_id = data["approval_id"]
        task_id = data["task_id"]
        step_id = data["step_id"] or "N/A"
        action_type = data["action_type"]
        risk_level = data["risk_level"]
        scrubbed_summary = data["scrubbed_summary"]

        try:
            params_str = json.dumps(data["scrubbed_params"], indent=2, sort_keys=True)
        except Exception:
            params_str = str(data["scrubbed_params"])
        if len(params_str) > 1000:
            params_str = params_str[:997] + "..."

        text = (
            f"🚨 *HEX Approval Request ({risk_level})*\n\n"
            f"*Task ID:* `{task_id}`\n"
            f"*Step ID:* `{step_id}`\n"
            f"*Tool Name:* `{action_type}`\n"
            f"*Risk Class:* `{risk_level}`\n"
            f"*Blast Radius:* {scrubbed_summary}\n\n"
            f"*Parameters:*\n```json\n{params_str}\n```\n\n"
            f"⚠️ *Privileged operation requires operator authorization.*"
        )

        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "✅ Approve", "callback_data": f"approve:{approval_id}"},
                    {"text": "❌ Reject", "callback_data": f"reject:{approval_id}"},
                ],
                [
                    {"text": "🔍 View Diff", "callback_data": f"diff:{approval_id}"},
                    {"text": "⏹ Cancel Task", "callback_data": f"cancel:{approval_id}"},
                ],
            ]
        }

        payload = {
            "chat_id": target_chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "reply_markup": reply_markup,
        }
        return self.transport.call_api("sendMessage", payload)

    def handle_callback_query(self, callback_query: dict[str, Any]) -> dict[str, Any]:
        """Handle interactive inline keyboard callbacks."""
        user_id = (callback_query.get("from") or {}).get("id")
        callback_id = str(callback_query.get("id") or "cb_mock")
        data = str(callback_query.get("data") or "")
        msg = callback_query.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        message_id = msg.get("message_id")

        ok, reason = self.check_access(user_id, chat_id)
        if not ok:
            self.transport.call_api("answerCallbackQuery", {
                "callback_query_id": callback_id,
                "text": "⛔ Unauthorized operator",
                "show_alert": True,
            })
            return {"ok": False, "reason": "unauthorized"}

        action, _, target_id = data.partition(":")
        target_id = target_id.strip()

        if action == "approve":
            if not self.orchestrator:
                self.transport.call_api("answerCallbackQuery", {
                    "callback_query_id": callback_id,
                    "text": "Orchestrator not connected",
                    "show_alert": True,
                })
                return {"ok": False, "reason": "no_orchestrator"}

            rec = self.orchestrator.ledger.get_approval(target_id)
            if not rec:
                self.transport.call_api("answerCallbackQuery", {
                    "callback_query_id": callback_id,
                    "text": f"Approval {target_id} not found",
                    "show_alert": True,
                })
                return {"ok": False, "reason": "not_found"}

            self.orchestrator.ledger.decide_approval(
                target_id,
                "APPROVED",
                decided_by=f"tg:{user_id}",
            )
            task_id = rec.get("task_id")
            if task_id:
                try:
                    self.orchestrator.resume_task(task_id)
                    resumed_task = self.orchestrator.ledger.get_task(task_id)
                    if (
                        resumed_task
                        and resumed_task.get("status") == "SUCCEEDED"
                        and chat_id is not None
                    ):
                        comp_card = (
                            f"🏁 *Task Completed Successfully: `{task_id}`*\n\n"
                            f"*Status:* `SUCCEEDED`\n"
                            f"*Goal:* {resumed_task.get('goal', '')}"
                        )
                        self.transport.call_api("sendMessage", {
                            "chat_id": chat_id,
                            "text": comp_card,
                            "parse_mode": "Markdown",
                        })
                except Exception as exc:
                    logger.error("Failed resuming task %s on approval: %s", task_id, exc)

            orig_text = msg.get("text", "")
            new_text = f"{orig_text}\n\n✅ *APPROVED by @{user_id}* — Task execution resumed."
            self.transport.call_api("editMessageText", {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": new_text,
                "parse_mode": "Markdown",
                "reply_markup": {"inline_keyboard": []},
            })
            self.transport.call_api("answerCallbackQuery", {
                "callback_query_id": callback_id,
                "text": "Approval recorded! Task resumed.",
            })
            return {"ok": True, "action": "approve", "approval_id": target_id}

        elif action == "reject":
            if not self.orchestrator:
                self.transport.call_api("answerCallbackQuery", {
                    "callback_query_id": callback_id,
                    "text": "Orchestrator not connected",
                    "show_alert": True,
                })
                return {"ok": False, "reason": "no_orchestrator"}

            self.orchestrator.ledger.decide_approval(
                target_id,
                "REJECTED",
                decided_by=f"tg:{user_id}",
            )
            orig_text = msg.get("text", "")
            new_text = f"{orig_text}\n\n❌ *REJECTED by @{user_id}* — Action rejected."
            self.transport.call_api("editMessageText", {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": new_text,
                "parse_mode": "Markdown",
                "reply_markup": {"inline_keyboard": []},
            })
            self.transport.call_api("answerCallbackQuery", {
                "callback_query_id": callback_id,
                "text": "Action rejected.",
            })
            return {"ok": True, "action": "reject", "approval_id": target_id}

        elif action == "diff":
            if not self.orchestrator:
                self.transport.call_api("answerCallbackQuery", {
                    "callback_query_id": callback_id,
                    "text": "Orchestrator not connected",
                    "show_alert": True,
                })
                return {"ok": False, "reason": "no_orchestrator"}

            rec = self.orchestrator.ledger.get_approval(target_id)
            task_id = rec.get("task_id") if rec else target_id
            diff_text = self._compute_task_diff(task_id)
            if not diff_text or not diff_text.strip():
                diff_preview = f"🔍 *Workspace Diff for Task `{task_id}`:*\n_No uncommitted changes._"
            else:
                if len(diff_text) > 3500:
                    diff_text = diff_text[:3450] + "\n... [truncated]"
                diff_preview = f"🔍 *Workspace Diff for Task `{task_id}`:*\n```diff\n{diff_text}\n```"

            self.transport.call_api("sendMessage", {
                "chat_id": chat_id,
                "text": diff_preview,
                "parse_mode": "Markdown",
            })
            self.transport.call_api("answerCallbackQuery", {
                "callback_query_id": callback_id,
                "text": "Displaying workspace diff.",
            })
            return {"ok": True, "action": "diff", "target_id": target_id}

        elif action == "cancel":
            if not self.orchestrator:
                self.transport.call_api("answerCallbackQuery", {
                    "callback_query_id": callback_id,
                    "text": "Orchestrator not connected",
                    "show_alert": True,
                })
                return {"ok": False, "reason": "no_orchestrator"}

            rec = self.orchestrator.ledger.get_approval(target_id)
            task_id = rec.get("task_id") if rec else target_id
            try:
                self.orchestrator.cancel_task(task_id, actor=f"tg:{user_id}")
            except Exception as exc:
                logger.error("Failed canceling task %s on callback: %s", task_id, exc)

            orig_text = msg.get("text", "")
            new_text = f"{orig_text}\n\n⏹ *CANCELED by @{user_id}* — Workspace rolled back."
            self.transport.call_api("editMessageText", {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": new_text,
                "parse_mode": "Markdown",
                "reply_markup": {"inline_keyboard": []},
            })
            self.transport.call_api("answerCallbackQuery", {
                "callback_query_id": callback_id,
                "text": "Task canceled.",
            })
            return {"ok": True, "action": "cancel", "target_id": target_id}

        # Unknown action
        self.transport.call_api("answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text": f"Unknown action: {action}",
        })
        return {"ok": False, "reason": f"unknown_action:{action}"}
