"""Tests for temporal approval leasing engine (approval_lease.py),

LedgerStore persistence, ReActLoop integration, ToolGateway policy evaluation,
and HexConsole /lease slash command.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from harness_core.core.approval_lease import ApprovalLease, ApprovalLeaseManager
from harness_core.storage.db import LedgerStore
from harness_core.core.react_loop import ReActLoop, ToolCallRequest, ToolCallResult
from harness_core.gateway import ToolGateway, ToolSpec, RiskClass, PolicyDecisionType
from harness_core.client.tui import HexConsole


def test_approval_lease_dataclass_defaults_and_validation() -> None:
    now = time.time()
    lease = ApprovalLease(
        lease_id="lease_test1",
        command_prefix="pytest",
        ttl_seconds=300,
    )
    assert lease.lease_id == "lease_test1"
    assert lease.command_prefix == "pytest"
    assert lease.ttl_seconds == 300
    assert lease.expires_at >= now + 295
    assert not lease.is_revoked
    assert lease.is_valid()

    # Serialization roundtrip
    d = lease.to_dict()
    assert d["lease_id"] == "lease_test1"
    assert d["command_prefix"] == "pytest"
    assert d["ttl_seconds"] == 300
    assert not d["is_revoked"]

    reconstructed = ApprovalLease.from_dict(d)
    assert reconstructed.lease_id == lease.lease_id
    assert reconstructed.command_prefix == lease.command_prefix
    assert reconstructed.ttl_seconds == lease.ttl_seconds
    assert reconstructed.is_valid()


def test_approval_lease_manager_grant_and_list() -> None:
    mgr = ApprovalLeaseManager()
    lease1 = mgr.grant_lease(command_prefix="pytest", ttl_seconds=600, task_id="task_100")
    assert lease1.lease_id.startswith("lease_")
    assert lease1.command_prefix == "pytest"
    assert lease1.task_id == "task_100"
    assert lease1.ttl_seconds == 600

    active = mgr.list_active_leases()
    assert len(active) == 1
    assert active[0].lease_id == lease1.lease_id
    assert mgr.get_lease(lease1.lease_id) == lease1


def test_command_prefix_matching() -> None:
    mgr = ApprovalLeaseManager()
    lease = mgr.grant_lease(command_prefix="pytest", ttl_seconds=900)

    # 1. Exact match
    matched, lid = mgr.check_lease("run_command", {"command": "pytest"})
    assert matched
    assert lid == lease.lease_id

    # 2. Command line with arguments
    matched, lid = mgr.check_lease("run_command", {"CommandLine": "pytest tests/test_core.py -v"})
    assert matched
    assert lid == lease.lease_id

    # 3. Path-qualified executable
    matched, lid = mgr.check_lease("run_command", {"cmd": "/usr/local/bin/pytest -k foo"})
    assert matched
    assert lid == lease.lease_id

    # 4. python -m runner
    matched, lid = mgr.check_lease("run_command", {"command": "python -m pytest tests/"})
    assert matched
    assert lid == lease.lease_id

    # 5. Non-matching commands
    matched, lid = mgr.check_lease("run_command", {"command": "cargo test"})
    assert not matched
    assert lid is None

    matched, lid = mgr.check_lease("run_command", {"command": "git push origin main"})
    assert not matched

    # Boundary check: pytest_runner should not match single prefix pytest
    matched, lid = mgr.check_lease("run_command", {"command": "pytest_runner.sh"})
    assert not matched


def test_multi_word_prefix_matching() -> None:
    mgr = ApprovalLeaseManager()
    lease = mgr.grant_lease(command_prefix="cargo test", ttl_seconds=900)

    matched, lid = mgr.check_lease("run_command", {"command": "cargo test --workspace"})
    assert matched
    assert lid == lease.lease_id

    matched, lid = mgr.check_lease("run_command", {"command": "cargo build"})
    assert not matched


def test_path_based_lease_matching() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        src_dir = os.path.join(tmpdir, "src")
        tests_dir = os.path.join(tmpdir, "tests")
        os.makedirs(src_dir, exist_ok=True)
        os.makedirs(tests_dir, exist_ok=True)

        mgr = ApprovalLeaseManager()
        lease = mgr.grant_lease(allowed_paths=[src_dir, tests_dir], ttl_seconds=900)

        # Target inside src_dir
        target_file = os.path.join(src_dir, "harness_core", "approval_lease.py")
        matched, lid = mgr.check_lease("write_file", {"path": target_file, "content": "..."})
        assert matched
        assert lid == lease.lease_id

        # Target inside tests_dir
        target_test = os.path.join(tests_dir, "test_approval.py")
        matched, lid = mgr.check_lease("edit_file", {"Path": target_test, "replacement": "..."})
        assert matched
        assert lid == lease.lease_id

        # Target outside allowed dirs
        outside_file = os.path.join(tmpdir, "secrets.env")
        matched, lid = mgr.check_lease("write_file", {"path": outside_file})
        assert not matched
        assert lid is None


def test_lease_expiration_behavior() -> None:
    mgr = ApprovalLeaseManager()
    # Very short TTL (0.1s)
    lease = mgr.grant_lease(command_prefix="pytest", ttl_seconds=0.1)  # type: ignore[arg-type]
    # Set expires_at to 0.1s from now
    lease.expires_at = time.time() + 0.15

    # Immediately valid
    matched, lid = mgr.check_lease("run_command", {"command": "pytest -v"})
    assert matched
    assert lid == lease.lease_id

    # Wait for expiration
    time.sleep(0.2)
    assert not lease.is_valid()

    matched, lid = mgr.check_lease("run_command", {"command": "pytest -v"})
    assert not matched
    assert lid is None

    # Cleanup expired
    pruned = mgr.cleanup_expired()
    assert pruned >= 1
    assert len(mgr.list_active_leases()) == 0


def test_explicit_lease_revocation() -> None:
    mgr = ApprovalLeaseManager()
    lease = mgr.grant_lease(command_prefix="npm test", ttl_seconds=600)
    assert lease.is_valid()

    # Active
    matched, _ = mgr.check_lease("run_command", {"command": "npm test"})
    assert matched

    # Revoke
    revoked = mgr.revoke_lease(lease.lease_id)
    assert revoked
    assert lease.is_revoked
    assert not lease.is_valid()

    # No longer matches
    matched, lid = mgr.check_lease("run_command", {"command": "npm test"})
    assert not matched
    assert lid is None

    # Revoking non-existent lease
    assert not mgr.revoke_lease("lease_nonexistent")


def test_ledger_store_approval_lease_persistence() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "ledger.db")
        ledger = LedgerStore(db_path=db_path)

        # Grant lease through manager wired to ledger
        mgr = ApprovalLeaseManager(ledger=ledger)
        lease = mgr.grant_lease(command_prefix="pytest", allowed_paths=["/tmp/src"], ttl_seconds=800, task_id="task_audit")

        # Verify persisted in ledger
        leases_db = ledger.list_approval_leases(task_id="task_audit", active_only=True)
        assert len(leases_db) == 1
        assert leases_db[0]["lease_id"] == lease.lease_id
        assert leases_db[0]["command_prefix"] == "pytest"
        assert leases_db[0]["allowed_paths"] == ["/tmp/src"]
        assert leases_db[0]["is_revoked"] == 0

        # Hydrate a new manager from the same ledger
        mgr2 = ApprovalLeaseManager(ledger=ledger)
        assert len(mgr2.list_active_leases()) == 1
        # Task-bound lease only matches inside its task (B45).
        matched, lid = mgr2.check_lease("run_command", {"command": "pytest -k unit"}, task_id="task_audit")
        assert matched
        assert lid == lease.lease_id
        matched, lid = mgr2.check_lease("run_command", {"command": "pytest -k unit"}, task_id="task_other")
        assert not matched
        assert lid is None

        # Revoke through ledger
        ledger.revoke_approval_lease(lease.lease_id)
        active_after = ledger.list_approval_leases(task_id="task_audit", active_only=True)
        assert len(active_after) == 0

        # Cleanup expired in ledger
        count = ledger.cleanup_expired_approval_leases()
        assert count >= 1


def test_react_loop_auto_approval_with_active_lease() -> None:
    mgr = ApprovalLeaseManager()
    lease = mgr.grant_lease(command_prefix="pytest", ttl_seconds=900)

    mock_gateway = MagicMock()
    mock_gateway.tools = {
        "run_command": MagicMock(handler=lambda args: {"output": "10 passed in 0.05s", "exit_code": 0})
    }
    mock_gateway.approval_lease_manager = mgr

    loop = ReActLoop(
        gateway=mock_gateway,
        approval_lease_manager=mgr,
    )

    call = ToolCallRequest(
        call_id="call_test1",
        name="run_command",
        arguments={"command": "pytest tests/test_core.py -v"},
    )

    res = loop._execute_tool(call)
    assert not res.is_error
    assert res.output["output"] == "10 passed in 0.05s"
    assert getattr(call, "policy_decision", None) == "LEASE_APPROVED"


def test_tool_gateway_policy_evaluation_with_lease() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger = LedgerStore(db_path=os.path.join(tmpdir, "ledger.db"))
        task = ledger.create_task(goal="Run gated deployment or tests", constraints={})
        task_id = task["task_id"]

        mgr = ApprovalLeaseManager(ledger=ledger)
        gw = ToolGateway(ledger=ledger, approval_lease_manager=mgr)

        # Register custom R3 privileged tool
        spec = ToolSpec(
            tool_id="priv_deploy",
            description="Privileged deployment tool",
            risk_class=RiskClass.R3,
            handler=lambda ws, args: {"deployed": True},
        )
        gw.tools["priv_deploy"] = spec

        # Without lease: R3 requires approval
        decision, reason = gw.evaluate_policy(task, spec, {"command": "deploy --prod"})
        assert decision == PolicyDecisionType.REQUIRE_APPROVAL

        # Grant lease for deploy command
        lease = mgr.grant_lease(command_prefix="deploy", ttl_seconds=600, task_id=task_id)

        # With lease: R3 evaluated as ALLOW with LEASE_APPROVED
        decision, reason = gw.evaluate_policy(task, spec, {"command": "deploy --prod"})
        assert decision == PolicyDecisionType.ALLOW
        assert "LEASE_APPROVED" in reason
        assert lease.lease_id in reason


def test_hex_console_slash_lease(capsys: pytest.CaptureFixture[str]) -> None:
    mock_orch = MagicMock()
    mock_orch.ledger = None
    mock_orch.default_workspace = None
    mock_orch.approval_lease_manager = ApprovalLeaseManager()

    console = HexConsole(orchestrator=mock_orch, quiet=True)

    # 1. Initially empty
    console._cmd_lease("list")
    out = capsys.readouterr().out
    assert "No active temporal approval leases" in out

    # 2. Grant lease
    console._cmd_lease("grant pytest 600")
    out = capsys.readouterr().out
    assert "Granted temporal approval lease" in out
    assert "pytest" in out

    # 3. List
    console._cmd_lease("list")
    out = capsys.readouterr().out
    assert "Active Temporal Approval Leases (1)" in out
    assert "pytest" in out

    # Extract lease id
    active = console.approval_lease_manager.list_active_leases()
    assert len(active) == 1
    lease_id = active[0].lease_id

    # 4. Revoke
    console._cmd_lease(f"revoke {lease_id}")
    out = capsys.readouterr().out
    assert f"Revoked approval lease {lease_id}" in out

    # 5. List again
    console._cmd_lease("list")
    out = capsys.readouterr().out
    assert "No active temporal approval leases" in out

    # 6. Cleanup
    console._cmd_lease("cleanup")
    out = capsys.readouterr().out
    assert "Pruned" in out
