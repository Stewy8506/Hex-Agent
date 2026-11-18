"""End-to-end ACP operator approval: request notification, blocking turn, decision, leases."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from harness_core.capabilities.gateways.reaction_listener import ApprovalRequest
from harness_core.client.acp import ACPServer
from harness_core.client.acp_approval import AcpApprovalBridge
from harness_core.core.approval_lease import ApprovalLeaseManager
from harness_core.core.models import RiskClass, TaskSubmitRequest
from harness_core.gateway import PolicyBlockedError, PolicyDecisionType, ToolGateway, ToolSpec
from harness_core.storage.db import LedgerStore


class EmitCapture:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.event = threading.Event()

    def __call__(self, obj: dict[str, Any]) -> None:
        self.messages.append(obj)
        self.event.set()

    def wait_for_request(self, timeout: float = 5.0) -> dict[str, Any]:
        assert self.event.wait(timeout), "server never emitted session/approvalRequest"
        for msg in self.messages:
            if msg.get("method") == "session/approvalRequest":
                return msg["params"]
        raise AssertionError("no approvalRequest among emitted messages")


def make_gateway(tmp_path: Path) -> tuple[LedgerStore, ToolGateway, str]:
    ledger = LedgerStore(tmp_path / "ledger.db")
    gateway = ToolGateway(ledger)
    gateway.register_tool(
        ToolSpec(
            tool_id="deploy",
            description="Privileged deployment used to exercise approval gating",
            risk_class=RiskClass.R3,
            handler=lambda ws, args: {"ok": True, "target": args.get("target", "")},
        )
    )
    task_id = ledger.submit_task(
        TaskSubmitRequest(goal="Deploy the build", workspace_root=str(tmp_path))
    )["task_id"]
    return ledger, gateway, task_id


def invoke_in_thread(
    gateway: ToolGateway,
    task_id: str,
    args: dict[str, Any],
    outcome: dict[str, Any],
    workspace_dir: Path,
) -> threading.Thread:
    def run() -> None:
        try:
            outcome["result"] = gateway.invoke(
                task_id=task_id,
                attempt_id="att-1",
                step_id="step-1",
                sequence_in_step=0,
                tool_id="deploy",
                args=args,
                workspace_dir=workspace_dir,
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced to the asserting thread
            outcome["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_bridge_emits_request_and_blocks_until_decision(tmp_path: Path) -> None:
    capture = EmitCapture()
    bridge = AcpApprovalBridge(capture, lambda tid: "sess_1", tmp_path, timeout_seconds=5.0)
    request = ApprovalRequest(
        request_id="appr_1",
        task_id="task_1",
        tool_name="terminal",
        arguments={"command": "rm -rf ./dist"},
        risk_class="R3",
        blast_radius="Deletes build output",
    )
    outcome: dict[str, Any] = {}

    def run() -> None:
        outcome["decision"] = bridge.request_approval(request)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    params = capture.wait_for_request()
    assert params["sessionId"] == "sess_1"
    assert params["approvalId"] == "appr_1"
    assert params["toolName"] == "terminal"
    assert params["riskLevel"] == "R3"
    assert params["parameters"] == "rm -rf ./dist"
    assert params["blastRadius"] == "Deletes build output"
    assert params["cwd"] == str(tmp_path)
    assert thread.is_alive(), "turn must stay suspended while the operator decides"
    assert bridge.has_pending("appr_1")

    assert bridge.resolve("appr_1", "approve")
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert outcome["decision"].decision == "APPROVE"
    assert not bridge.has_pending("appr_1")


def test_bridge_timeout_denies(tmp_path: Path) -> None:
    capture = EmitCapture()
    bridge = AcpApprovalBridge(capture, lambda tid: None, tmp_path, timeout_seconds=0.05)
    decision = bridge.request_approval(ApprovalRequest(request_id="appr_2", tool_name="terminal"))
    assert decision.decision == "DENY"
    assert "denied by default" in (decision.reason or "")
    assert not bridge.has_pending("appr_2")


def test_unknown_approval_id_is_not_applied(tmp_path: Path) -> None:
    bridge = AcpApprovalBridge(EmitCapture(), lambda tid: None, tmp_path)
    assert bridge.resolve("appr_missing", "approve") is False


def test_scope_decision_grants_narrow_lease_for_the_exact_action(tmp_path: Path) -> None:
    ledger, gateway, task_id = make_gateway(tmp_path)
    try:
        leases = ApprovalLeaseManager(ledger)
        gateway.approval_lease_manager = leases
        capture = EmitCapture()
        bridge = AcpApprovalBridge(capture, lambda tid: None, tmp_path, leases, timeout_seconds=5.0)
        gateway.approval_bridge = bridge

        outcome: dict[str, Any] = {}
        thread = invoke_in_thread(gateway, task_id, {"command": "rm -rf ./dist"}, outcome, tmp_path)
        params = capture.wait_for_request()
        assert bridge.resolve(params["approvalId"], "approve_scope")
        thread.join(timeout=5.0)
        assert outcome.get("error") is None, outcome.get("error")
        assert outcome["result"]["ok"] is True

        granted = leases.list_active_leases()
        assert len(granted) == 1
        # B45: the grant binds to the full command, tool, and task — not the
        # binary basename, and not the whole workspace.
        assert granted[0].command_prefix == "rm -rf ./dist"
        assert granted[0].match_mode == "exact"
        assert granted[0].tool_name == "deploy"
        assert granted[0].task_id == task_id
        assert granted[0].allowed_paths == []

        # The identical action in the same task is auto-approved.
        decision, reason = gateway.evaluate_policy(
            ledger.get_task(task_id), gateway.tools["deploy"], {"command": "rm -rf ./dist"}
        )
        assert decision == PolicyDecisionType.ALLOW
        assert reason.startswith("LEASE_APPROVED:")

        # A different command with the same binary is NOT covered.
        decision, _ = gateway.evaluate_policy(
            ledger.get_task(task_id), gateway.tools["deploy"], {"command": "rm -rf ./build"}
        )
        assert decision == PolicyDecisionType.REQUIRE_APPROVAL

        # The same command in a different task is NOT covered.
        next_task = ledger.submit_task(
            TaskSubmitRequest(goal="Clean another build", workspace_root=str(tmp_path))
        )["task_id"]
        decision, _ = gateway.evaluate_policy(
            ledger.get_task(next_task), gateway.tools["deploy"], {"command": "rm -rf ./dist"}
        )
        assert decision == PolicyDecisionType.REQUIRE_APPROVAL
    finally:
        ledger.close()


def test_acp_server_decision_reaches_the_waiting_turn(tmp_path: Path) -> None:
    ledger, gateway, task_id = make_gateway(tmp_path)
    try:
        server = ACPServer(orchestrator=SimpleNamespace(gateway=gateway, ledger=ledger), workspace_root=tmp_path)
        capture = EmitCapture()
        server._emit = capture  # type: ignore[method-assign]
        assert server.orchestrator.gateway is gateway
        assert gateway.approval_bridge is server.approval_bridge

        outcome: dict[str, Any] = {}
        thread = invoke_in_thread(gateway, task_id, {"target": "staging"}, outcome, tmp_path)
        params = capture.wait_for_request()

        response = server.handle_request({
            "jsonrpc": "2.0",
            "id": 7,
            "method": "session/approvalDecision",
            "params": {"approvalId": params["approvalId"], "decision": "approve"},
        })
        assert response["result"]["status"] == "applied"
        thread.join(timeout=5.0)
        assert outcome.get("error") is None, outcome.get("error")
        assert outcome["result"]["target"] == "staging"

        approvals = ledger.list_approvals(task_id=task_id, decision="APPROVED")
        assert len(approvals) == 1
        assert approvals[0]["decided_by"].startswith("acp:")
    finally:
        ledger.close()


def test_deny_blocks_the_tool_and_records_rejection(tmp_path: Path) -> None:
    ledger, gateway, task_id = make_gateway(tmp_path)
    try:
        server = ACPServer(orchestrator=SimpleNamespace(gateway=gateway, ledger=ledger), workspace_root=tmp_path)
        capture = EmitCapture()
        server._emit = capture  # type: ignore[method-assign]
        assert server.orchestrator is not None

        outcome: dict[str, Any] = {}
        thread = invoke_in_thread(gateway, task_id, {"target": "production"}, outcome, tmp_path)
        params = capture.wait_for_request()
        server.handle_request({
            "jsonrpc": "2.0",
            "id": 8,
            "method": "session/approvalDecision",
            "params": {"approvalId": params["approvalId"], "decision": "reject", "reason": "too risky"},
        })
        thread.join(timeout=5.0)
        assert isinstance(outcome.get("error"), PolicyBlockedError)
        assert "too risky" in str(outcome["error"])
        assert ledger.list_approvals(task_id=task_id, decision="REJECTED")
    finally:
        ledger.close()


def test_decision_for_an_already_finished_request_is_acknowledged(tmp_path: Path) -> None:
    ledger, gateway, task_id = make_gateway(tmp_path)
    try:
        server = ACPServer(orchestrator=SimpleNamespace(gateway=gateway, ledger=ledger), workspace_root=tmp_path)
        assert server.orchestrator is not None
        response = server.handle_request({
            "jsonrpc": "2.0",
            "id": 9,
            "method": "session/approvalDecision",
            "params": {"approvalId": "appr_gone", "decision": "approve"},
        })
        assert response["result"]["status"] == "acknowledged"
        assert server.pending_approvals["appr_gone"]["decision"] == "approve"
    finally:
        ledger.close()