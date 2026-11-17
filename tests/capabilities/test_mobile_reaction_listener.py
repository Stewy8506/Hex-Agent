"""Tests for asynchronous mobile reaction listener bridge (reaction_listener.py),

TelegramBotGateway / DiscordBotGateway integration, and ToolGateway remote approval flows.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from harness_core.capabilities.gateways.reaction_listener import (
    ApprovalRequest,
    ApprovalDecision,
    MobileApprovalBridge,
)
from harness_core.capabilities.gateways.telegram_bot import (
    TelegramBotGateway,
    TelegramConfig,
    MockTelegramTransport,
)
from harness_core.capabilities.gateways.discord_bot import (
    DiscordBotGateway,
    DiscordConfig,
    MockDiscordTransport,
)
from harness_core.storage.db import LedgerStore
from harness_core.gateway import ToolGateway, ToolSpec, RiskClass, PolicyDecisionType, PolicyBlockedError, ApprovalRequiredInterrupt


def test_approval_request_and_decision_models() -> None:
    req = ApprovalRequest(
        request_id="req_101",
        task_id="tsk_202",
        tool_name="git_push",
        arguments={"remote": "origin", "branch": "main"},
        risk_class="R3",
        timeout_seconds=45.0,
    )
    assert req.request_id == "req_101"
    assert req.task_id == "tsk_202"
    assert req.tool_name == "git_push"
    assert req.timeout_seconds == 45.0

    dec = ApprovalDecision(
        decision="APPROVE",
        responder_id="user_admin",
        platform="telegram",
        reason="Approved via mobile phone",
    )
    assert dec.decision == "APPROVE"
    assert dec.responder_id == "user_admin"
    assert dec.platform == "telegram"
    assert dec.timestamp > 0


def test_mobile_approval_bridge_resolve_and_unblock() -> None:
    bridge = MobileApprovalBridge(timeout_seconds=5.0, allowed_user_ids=["user_777"])

    req = ApprovalRequest(
        request_id="req_sync_1",
        tool_name="run_command",
        arguments={"command": "cargo publish"},
        timeout_seconds=3.0,
    )

    def delayed_approver() -> None:
        time.sleep(0.1)
        assert bridge.has_pending("req_sync_1")
        # Attempt unauthorized first
        unauth_ok = bridge.resolve_approval("req_sync_1", "APPROVE", responder_id="intruder")
        assert not unauth_ok

        # Authorized approval
        auth_ok = bridge.resolve_approval("req_sync_1", "APPROVE", responder_id="user_777", platform="telegram")
        assert auth_ok

    t = threading.Thread(target=delayed_approver)
    t.start()

    res = bridge.request_approval(req)
    t.join()

    assert res.decision == "APPROVE"
    assert res.responder_id == "user_777"
    assert res.platform == "telegram"
    assert not bridge.has_pending("req_sync_1")


def test_mobile_approval_bridge_denial() -> None:
    bridge = MobileApprovalBridge(timeout_seconds=5.0, allowed_user_ids=["admin_99"])

    req = ApprovalRequest(
        request_id="req_deny_1",
        tool_name="delete_database",
        arguments={"target": "prod_db"},
        timeout_seconds=3.0,
    )

    def delayed_denier() -> None:
        time.sleep(0.1)
        bridge.resolve_approval("req_deny_1", "DENY", responder_id="admin_99", platform="discord")

    t = threading.Thread(target=delayed_denier)
    t.start()

    res = bridge.request_approval(req)
    t.join()

    assert res.decision == "DENY"
    assert res.responder_id == "admin_99"
    assert res.platform == "discord"


def test_mobile_approval_bridge_timeout_fallback() -> None:
    # Short timeout 0.2s
    bridge = MobileApprovalBridge(timeout_seconds=0.2)

    req = ApprovalRequest(
        request_id="req_timeout_1",
        tool_name="priv_tool",
        arguments={},
        timeout_seconds=0.2,
    )

    t0 = time.time()
    res = bridge.request_approval(req)
    duration = time.time() - t0

    assert res.decision == "TIMEOUT"
    assert duration >= 0.18
    assert not bridge.has_pending("req_timeout_1")


def test_telegram_callback_and_reaction_integration() -> None:
    tg_transport = MockTelegramTransport()
    tg_config = TelegramConfig(bot_token="mock_token", allowed_user_ids={"12345"}, allowed_chat_ids={"67890"})
    tg_gateway = TelegramBotGateway(orchestrator=None, config=tg_config, transport=tg_transport)

    bridge = MobileApprovalBridge(telegram_bot=tg_gateway, allowed_user_ids=["12345"])

    # 1. Test button callback
    req = ApprovalRequest(request_id="req_tg_cb", tool_name="bash", timeout_seconds=2.0)

    def trigger_callback() -> None:
        time.sleep(0.1)
        cb_payload = {
            "id": "cb_1",
            "from": {"id": 12345},
            "data": "approve:req_tg_cb",
            "message": {"message_id": 999, "chat": {"id": 67890}, "text": "Approval required"},
        }
        res = tg_gateway.handle_callback_query(cb_payload)
        assert res.get("ok")
        assert res.get("action") == "approve"

    t = threading.Thread(target=trigger_callback)
    t.start()
    decision = bridge.request_approval(req)
    t.join()

    assert decision.decision == "APPROVE"
    assert decision.responder_id == "12345"

    # 2. Test emoji reaction (👍 -> APPROVE)
    req2 = ApprovalRequest(request_id="req_tg_rx", tool_name="bash", timeout_seconds=2.0)

    def trigger_reaction() -> None:
        time.sleep(0.1)
        # Register message_id to pending item
        with bridge._lock:
            if "req_tg_rx" in bridge._pending_requests:
                bridge._pending_requests["req_tg_rx"]["telegram_msg_id"] = 1001
        res = tg_gateway.handle_reaction(chat_id=67890, message_id=1001, user_id=12345, emoji="👍")
        assert res.get("ok")

    t2 = threading.Thread(target=trigger_reaction)
    t2.start()
    decision2 = bridge.request_approval(req2)
    t2.join()

    assert decision2.decision == "APPROVE"

    # 3. Test emoji reaction (👎 -> DENY)
    req3 = ApprovalRequest(request_id="req_tg_deny_rx", tool_name="bash", timeout_seconds=2.0)

    def trigger_deny_reaction() -> None:
        time.sleep(0.1)
        with bridge._lock:
            if "req_tg_deny_rx" in bridge._pending_requests:
                bridge._pending_requests["req_tg_deny_rx"]["telegram_msg_id"] = 1002
        res = tg_gateway.handle_reaction(chat_id=67890, message_id=1002, user_id=12345, emoji="👎")
        assert res.get("ok")

    t3 = threading.Thread(target=trigger_deny_reaction)
    t3.start()
    decision3 = bridge.request_approval(req3)
    t3.join()

    assert decision3.decision == "DENY"


def test_discord_interaction_and_reaction_integration() -> None:
    discord_transport = MockDiscordTransport()
    discord_config = DiscordConfig(bot_token="mock_discord_token", allowed_user_ids={"discord_user_1"})
    discord_gateway = DiscordBotGateway(orchestrator=None, config=discord_config, transport=discord_transport)

    bridge = MobileApprovalBridge(discord_bot=discord_gateway, allowed_user_ids=["discord_user_1"])

    # 1. Test button component interaction
    req = ApprovalRequest(request_id="req_dc_btn", tool_name="priv_tool", timeout_seconds=2.0)

    def trigger_component() -> None:
        time.sleep(0.1)
        interaction_payload = {
            "type": 3,
            "id": "int_1",
            "token": "tok_1",
            "user": {"id": "discord_user_1"},
            "data": {"custom_id": "hex:approve:req_dc_btn"},
        }
        res = discord_gateway.handle_interaction(interaction_payload)
        assert res.get("type") == 7

    t = threading.Thread(target=trigger_component)
    t.start()
    decision = bridge.request_approval(req)
    t.join()

    assert decision.decision == "APPROVE"
    assert decision.responder_id == "discord_user_1"

    # 2. Test reaction addition (👍 -> APPROVE)
    req2 = ApprovalRequest(request_id="req_dc_rx", tool_name="priv_tool", timeout_seconds=2.0)

    def trigger_reaction() -> None:
        time.sleep(0.1)
        with bridge._lock:
            if "req_dc_rx" in bridge._pending_requests:
                bridge._pending_requests["req_dc_rx"]["discord_msg_id"] = 5555
        res = discord_gateway.handle_reaction_add(user_id="discord_user_1", channel_id="ch_1", message_id=5555, emoji="👍")
        assert res.get("ok")

    t2 = threading.Thread(target=trigger_reaction)
    t2.start()
    decision2 = bridge.request_approval(req2)
    t2.join()

    assert decision2.decision == "APPROVE"


def test_tool_gateway_mobile_bridge_approval_execution() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger = LedgerStore(db_path=os.path.join(tmpdir, "ledger.db"))
        task = ledger.create_task(goal="Deploy to production", constraints={})
        task_id = task["task_id"]

        bridge = MobileApprovalBridge(timeout_seconds=2.0, allowed_user_ids=["operator_x"])
        gw = ToolGateway(ledger=ledger, approval_bridge=bridge)

        executed = False

        def deploy_handler(ws: Path, args: dict[str, Any]) -> dict[str, Any]:
            nonlocal executed
            executed = True
            return {"status": "deployed", "tag": args.get("tag")}

        spec = ToolSpec(
            tool_id="deploy_prod",
            description="Production deployer (R3)",
            risk_class=RiskClass.R3,
            handler=deploy_handler,
        )
        gw.tools["deploy_prod"] = spec

        # Remote approver resolves with APPROVE
        def remote_approver() -> None:
            time.sleep(0.15)
            pending = bridge.get_pending_requests()
            assert len(pending) == 1
            bridge.resolve_approval(pending[0].request_id, "APPROVE", responder_id="operator_x", platform="telegram")

        t = threading.Thread(target=remote_approver)
        t.start()

        # Invoke through gateway: should be remote-approved and execute
        res = gw.invoke(
            task_id=task_id,
            attempt_id="att_1",
            step_id="step_1",
            sequence_in_step=0,
            tool_id="deploy_prod",
            args={"tag": "v2.0.0"},
            workspace_dir=tmpdir,
        )
        t.join()

        assert executed
        assert res.get("status") == "deployed"
        assert res.get("tag") == "v2.0.0"


def test_tool_gateway_mobile_bridge_denial_halts_execution() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger = LedgerStore(db_path=os.path.join(tmpdir, "ledger.db"))
        task = ledger.create_task(goal="Format disk", execution_mode="standard", constraints={})
        task_id = task["task_id"]

        bridge = MobileApprovalBridge(timeout_seconds=2.0, allowed_user_ids=["admin_sec"])
        gw = ToolGateway(ledger=ledger, approval_bridge=bridge)

        executed = False

        def danger_handler(ws: Path, args: dict[str, Any]) -> dict[str, Any]:
            nonlocal executed
            executed = True
            return {"formatted": True}

        spec = ToolSpec(
            tool_id="danger_action",
            description="Dangerous action (R3)",
            risk_class=RiskClass.R3,
            handler=danger_handler,
        )
        gw.tools["danger_action"] = spec

        # Remote operator resolves with DENY
        def remote_denier() -> None:
            time.sleep(0.15)
            pending = bridge.get_pending_requests()
            assert len(pending) == 1
            bridge.resolve_approval(pending[0].request_id, "DENY", responder_id="admin_sec", platform="discord")

        t = threading.Thread(target=remote_denier)
        t.start()

        # Invoke through gateway: should raise PolicyBlockedError
        with pytest.raises(PolicyBlockedError) as exc_info:
            gw.invoke(
                task_id=task_id,
                attempt_id="att_1",
                step_id="step_1",
                sequence_in_step=0,
                tool_id="danger_action",
                args={"device": "/dev/sda"},
                workspace_dir=tmpdir,
            )
        t.join()

        assert not executed
        assert "Remote operator denied action" in str(exc_info.value)


def test_tool_gateway_mobile_bridge_timeout_fallback_to_interrupt() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger = LedgerStore(db_path=os.path.join(tmpdir, "ledger.db"))
        task = ledger.create_task(goal="Sensitive check", execution_mode="standard", constraints={})
        task_id = task["task_id"]

        # Fast timeout 0.15s
        bridge = MobileApprovalBridge(timeout_seconds=0.15)
        gw = ToolGateway(ledger=ledger, approval_bridge=bridge)

        spec = ToolSpec(
            tool_id="priv_check",
            description="Privileged check (R3)",
            risk_class=RiskClass.R3,
            handler=lambda ws, args: {"ok": True},
        )
        gw.tools["priv_check"] = spec

        # No remote operator answers: bridge times out and falls back to ApprovalRequiredInterrupt
        with pytest.raises(ApprovalRequiredInterrupt) as exc_info:
            gw.invoke(
                task_id=task_id,
                attempt_id="att_1",
                step_id="step_1",
                sequence_in_step=0,
                tool_id="priv_check",
                args={"opt": "val"},
                workspace_dir=tmpdir,
            )

        assert "Approval required" in str(exc_info.value)
