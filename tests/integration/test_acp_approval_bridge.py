"""End-to-end ACP operator approval: request notification, blocking turn, decision, leases."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from harness_core.capabilities.gateways.reaction_listener import ApprovalRequest
from harness_core.client.acp import ACPServer, ACPSession
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

    assert bridge.resolve("appr_1", "approve")["applied"] is True
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
    assert bridge.resolve("appr_missing", "approve")["applied"] is False


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
        assert bridge.resolve(params["approvalId"], "approve_scope")["applied"] is True
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


def test_cancel_pending_wakes_blocking_approval_promptly(tmp_path: Path) -> None:
    """B48: cancel_pending denies and wakes a waiting turn well before the timeout."""
    capture = EmitCapture()
    bridge = AcpApprovalBridge(capture, lambda tid: "sess_1", tmp_path, timeout_seconds=30.0)
    request = ApprovalRequest(
        request_id="appr_cancel",
        task_id="task_1",
        tool_name="terminal",
        arguments={"command": "rm -rf ./dist"},
        risk_class="R3",
    )
    outcome: dict[str, Any] = {}

    def run() -> None:
        outcome["decision"] = bridge.request_approval(request)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    capture.wait_for_request()
    assert bridge.has_pending("appr_cancel")

    start = time.monotonic()
    woken = bridge.cancel_pending(task_id="task_1")
    thread.join(timeout=2.0)
    elapsed = time.monotonic() - start

    assert woken == 1
    assert not thread.is_alive(), "cancel must wake the waiter immediately, not after the timeout"
    assert elapsed < 2.0
    assert outcome["decision"].decision == "DENY"
    assert "Canceled" in (outcome["decision"].reason or "")
    assert not bridge.has_pending("appr_cancel")


def test_cancel_pending_ignores_unrelated_tasks(tmp_path: Path) -> None:
    bridge = AcpApprovalBridge(EmitCapture(), lambda tid: "sess_1", tmp_path, timeout_seconds=30.0)
    outcome: dict[str, Any] = {}

    def run() -> None:
        outcome["decision"] = bridge.request_approval(
            ApprovalRequest(request_id="appr_other", task_id="task_other", tool_name="terminal")
        )

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert bridge.has_pending("appr_other") or thread.is_alive()
    # Wait until the request is actually registered.
    for _ in range(100):
        if bridge.has_pending("appr_other"):
            break
        time.sleep(0.01)

    assert bridge.cancel_pending(task_id="task_unrelated") == 0
    assert bridge.has_pending("appr_other")
    # Clean up the waiter so it does not linger.
    bridge.resolve("appr_other", "reject")
    thread.join(timeout=2.0)


def test_acp_session_cancel_withdraws_pending_approval(tmp_path: Path) -> None:
    """B48: session/cancel wakes a turn blocked on approval instead of a 60s wait."""
    ledger, gateway, task_id = make_gateway(tmp_path)
    try:
        server = ACPServer(orchestrator=SimpleNamespace(gateway=gateway, ledger=ledger), workspace_root=tmp_path)
        capture = EmitCapture()
        server._emit = capture  # type: ignore[method-assign]
        assert server.orchestrator.gateway is gateway  # wires the approval bridge

        sess = ACPSession(session_id="sess_cancel", workspace_root=tmp_path)
        sess.last_task_id = task_id
        sess.running = True
        server.sessions["sess_cancel"] = sess

        outcome: dict[str, Any] = {}
        thread = invoke_in_thread(gateway, task_id, {"target": "staging"}, outcome, tmp_path)
        capture.wait_for_request()
        assert thread.is_alive()

        start = time.monotonic()
        response = server.handle_request({
            "jsonrpc": "2.0",
            "id": 11,
            "method": "session/cancel",
            "params": {"sessionId": "sess_cancel"},
        })
        thread.join(timeout=2.0)
        elapsed = time.monotonic() - start

        assert response["result"]["cancelled"] is True
        assert not thread.is_alive(), "canceled turn must not block until the approval timeout"
        assert elapsed < 2.0
        assert isinstance(outcome.get("error"), PolicyBlockedError)
        assert sess.cancelled is True
    finally:
        ledger.close()


def test_once_approval_never_grants_a_lease_even_with_ttl(tmp_path: Path) -> None:
    """B30: 'Once' is a single execution; a TTL must not silently widen it."""
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
        result = bridge.resolve(params["approvalId"], "approve", lease_ttl_seconds=900)
        thread.join(timeout=5.0)
        assert result == {
            "applied": True,
            "outcome": "APPROVE",
            "lease_id": None,
            "lease_ttl_seconds": None,
        }
        assert leases.list_active_leases() == []
    finally:
        ledger.close()


def test_scope_lease_honors_a_validated_ttl(tmp_path: Path) -> None:
    """B30: the operator-visible 15-minute lease is the lease the server grants."""
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
        result = bridge.resolve(params["approvalId"], "approve_scope", lease_ttl_seconds=900)
        thread.join(timeout=5.0)
        assert result["applied"] is True
        assert result["outcome"] == "APPROVE"
        assert result["lease_ttl_seconds"] == 900
        granted = leases.list_active_leases()
        assert len(granted) == 1
        assert granted[0].ttl_seconds == 900
        assert granted[0].lease_id == result["lease_id"]
    finally:
        ledger.close()


@pytest.mark.parametrize("bad_ttl", [0, 59, 86401, -1, "900", 1.5, True])
def test_invalid_ttl_is_rejected_and_leaves_the_turn_waiting(tmp_path: Path, bad_ttl: Any) -> None:
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
        with pytest.raises(ValueError):
            bridge.resolve(params["approvalId"], "approve_scope", lease_ttl_seconds=bad_ttl)
        assert thread.is_alive(), "an invalid decision must not resolve the waiting turn"
        assert leases.list_active_leases() == []
        assert bridge.resolve(params["approvalId"], "deny")["applied"] is True
        thread.join(timeout=5.0)
        assert isinstance(outcome.get("error"), PolicyBlockedError)
    finally:
        ledger.close()


def test_unknown_decision_is_a_parameter_error_not_a_silent_denial(tmp_path: Path) -> None:
    """B27: the legacy 'allow_session' string must not masquerade as a denial."""
    ledger, gateway, task_id = make_gateway(tmp_path)
    try:
        capture = EmitCapture()
        bridge = AcpApprovalBridge(capture, lambda tid: None, tmp_path, timeout_seconds=5.0)
        gateway.approval_bridge = bridge

        outcome: dict[str, Any] = {}
        thread = invoke_in_thread(gateway, task_id, {"command": "rm -rf ./dist"}, outcome, tmp_path)
        params = capture.wait_for_request()
        with pytest.raises(ValueError):
            bridge.resolve(params["approvalId"], "allow_session")
        assert thread.is_alive()
        assert bridge.resolve(params["approvalId"], "approve_scope")["outcome"] == "APPROVE"
        thread.join(timeout=5.0)
        assert outcome.get("error") is None, outcome.get("error")
    finally:
        ledger.close()


def test_server_reports_typed_outcome_and_real_lease_ttl(tmp_path: Path) -> None:
    ledger, gateway, task_id = make_gateway(tmp_path)
    try:
        server = ACPServer(orchestrator=SimpleNamespace(gateway=gateway, ledger=ledger), workspace_root=tmp_path)
        capture = EmitCapture()
        server._emit = capture  # type: ignore[method-assign]
        assert server.orchestrator.gateway is gateway

        outcome: dict[str, Any] = {}
        thread = invoke_in_thread(gateway, task_id, {"command": "rm -rf ./dist"}, outcome, tmp_path)
        params = capture.wait_for_request()
        response = server.handle_request({
            "jsonrpc": "2.0",
            "id": 20,
            "method": "session/approvalDecision",
            "params": {
                "approvalId": params["approvalId"],
                "decision": "approve_scope",
                "leaseTtlSeconds": 900,
            },
        })
        result = response["result"]
        assert result["status"] == "applied"
        assert result["applied"] is True
        assert result["outcome"] == "APPROVE"
        assert result["leaseTtlSeconds"] == 900
        assert result["leaseId"]
        thread.join(timeout=5.0)
        assert outcome.get("error") is None, outcome.get("error")
    finally:
        ledger.close()


def test_server_rejects_invalid_ttl_and_unknown_decision_as_invalid_params(tmp_path: Path) -> None:
    ledger, gateway, task_id = make_gateway(tmp_path)
    try:
        server = ACPServer(orchestrator=SimpleNamespace(gateway=gateway, ledger=ledger), workspace_root=tmp_path)
        capture = EmitCapture()
        server._emit = capture  # type: ignore[method-assign]
        assert server.orchestrator.gateway is gateway

        outcome: dict[str, Any] = {}
        thread = invoke_in_thread(gateway, task_id, {"command": "rm -rf ./dist"}, outcome, tmp_path)
        params = capture.wait_for_request()
        for bad_params in (
            {"approvalId": params["approvalId"], "decision": "approve_scope", "leaseTtlSeconds": 5},
            {"approvalId": params["approvalId"], "decision": "allow_session"},
        ):
            response = server.handle_request({
                "jsonrpc": "2.0",
                "id": 21,
                "method": "session/approvalDecision",
                "params": bad_params,
            })
            assert response["error"]["code"] == -32602, response
        assert thread.is_alive()
        bridge = server.approval_bridge
        assert bridge is not None
        bridge.resolve(params["approvalId"], "deny")
        thread.join(timeout=5.0)
    finally:
        ledger.close()


def test_acknowledged_decision_reports_no_outcome(tmp_path: Path) -> None:
    ledger, gateway, task_id = make_gateway(tmp_path)
    try:
        server = ACPServer(orchestrator=SimpleNamespace(gateway=gateway, ledger=ledger), workspace_root=tmp_path)
        response = server.handle_request({
            "jsonrpc": "2.0",
            "id": 22,
            "method": "session/approvalDecision",
            "params": {"approvalId": "appr_gone", "decision": "approve"},
        })
        result = response["result"]
        assert result["status"] == "acknowledged"
        assert result["applied"] is False
        assert result["outcome"] is None
        assert result["leaseId"] is None
        assert result["leaseTtlSeconds"] is None
    finally:
        ledger.close()