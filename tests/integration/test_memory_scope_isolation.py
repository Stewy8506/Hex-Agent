"""B62 regression: memory scope isolation across workspace / global boundaries.

Proves the three Phase 5.1 fixes against the prior behavior:
  (a) ACP derives ``project_id`` from the resolved workspace (was hardcoded
      ``"acp_session"``, leaking governed project memory across workspaces).
  (b) ``run_demotion_sweep`` defaults to workspace-scoped notes and never prunes
      the shared global ``user`` profile unless ``include_user=True``.
  (c) the gateway routes model-driven ``memory`` mutations targeting the global
      ``user`` profile through the human-approval boundary.

Each test fails against the pre-fix source (red proof).
"""

from __future__ import annotations

import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness_core.client.acp import ACPServer, ACPSession
from harness_core.cognition.memory import (
    AMEMController,
    CuratedMemoryStore,
    MemoryLifecycleManager,
)
from harness_core.core.router import GovernedMemoryService
from harness_core.gateway import PolicyDecisionType, ToolGateway
from harness_core.storage import compute_project_id
from harness_core.storage.db import LedgerStore


class _CapturingMemory:
    """Records the project_id each governed-memory call is scoped to."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def write_memory(self, **kw):
        self.calls.append(("write", kw.get("project_id")))
        return {"memory_id": "mem_0001", "subject": kw.get("subject")}

    def retrieve_memories(self, **kw):
        self.calls.append(("retrieve", kw.get("project_id")))
        return []


class TestACPWorkspaceProjectScope:
    """Fix (a): ACP memory commands key governed memory by workspace, not a constant."""

    def test_remember_derives_project_id_from_workspace(self, tmp_path: Path) -> None:
        ws_a = tmp_path / "proj_a"
        ws_b = tmp_path / "proj_b"
        ws_a.mkdir()
        ws_b.mkdir()
        mem = _CapturingMemory()
        stub = SimpleNamespace(orchestrator=SimpleNamespace(memory=mem))

        sess_a = ACPSession(session_id="a", workspace_root=ws_a)
        sess_b = ACPSession(session_id="b", workspace_root=ws_b)
        ACPServer._cmd_remember(stub, sess_a, "release: ship on Fridays")
        ACPServer._cmd_remember(stub, sess_b, "release: ship on Mondays")

        pid_a = mem.calls[0][1]
        pid_b = mem.calls[1][1]
        assert pid_a == compute_project_id(ws_a)
        assert pid_b == compute_project_id(ws_b)
        assert pid_a != pid_b, "distinct workspaces must not share a governed-memory scope"
        assert pid_a != "acp_session" and pid_b != "acp_session"

    def test_memory_retrieve_derives_project_id_from_workspace(self, tmp_path: Path) -> None:
        ws = tmp_path / "proj"
        ws.mkdir()
        mem = _CapturingMemory()
        stub = SimpleNamespace(orchestrator=SimpleNamespace(memory=mem))
        sess = ACPSession(session_id="s", workspace_root=ws)

        ACPServer._cmd_memory(stub, sess, "release")
        assert mem.calls[-1] == ("retrieve", compute_project_id(ws))


class TestGovernedMemoryCrossWorkspaceIsolation:
    """The isolation mechanism the ACP fix now keys correctly: distinct project_ids do not leak."""

    def test_project_memory_does_not_leak_across_workspaces(self, tmp_path: Path) -> None:
        ws_a = tmp_path / "a"
        ws_b = tmp_path / "b"
        ws_a.mkdir()
        ws_b.mkdir()
        pid_a = compute_project_id(ws_a)
        pid_b = compute_project_id(ws_b)
        assert pid_a != pid_b

        with closing(LedgerStore(db_path=str(tmp_path / "ledger.db"))) as ledger:
            service = GovernedMemoryService(ledger=ledger)
            service.write_memory(
                scope="project",
                project_id=pid_a,
                subject="db port",
                content="Workspace A uses port 5432",
                workspace_dir=ws_a,
            )
            # A different workspace must not see workspace A's project memory.
            leaked = service.retrieve_memories(project_id=pid_b, workspace_dir=ws_b)
            assert leaked == []
            # The owning workspace still sees it.
            owned = service.retrieve_memories(project_id=pid_a, workspace_dir=ws_a)
            assert any("port 5432" in m.get("content", "") for m in owned)


class TestDemotionSweepScopeIsolation:
    """Fix (b): a per-workspace sweep must not prune the shared global user profile."""

    def _setup(self, tmp_path: Path):
        ws = tmp_path / "ws"
        gh = tmp_path / "global_home"
        ws.mkdir()
        gh.mkdir()
        store = CuratedMemoryStore(workspace_root=ws, global_home=gh)
        controller = AMEMController(workspace_root=ws, global_home=gh, store=store)
        lifecycle = MemoryLifecycleManager(
            workspace_root=ws, global_home=gh, controller=controller
        )
        r_mem = controller.add_memory("Workspace rule: pin dependencies. [[deps]]", target="memory")
        r_user = controller.add_memory("User profile: prefers concise answers. [[style]]", target="user")
        assert r_mem["ok"] and r_user["ok"]
        mem_id = r_mem["item"]["id"]
        user_id = r_user["item"]["id"]
        for mid in (mem_id, user_id):
            item = controller.get_memory(mid)
            item.last_referenced_at = time.time() - (40 * 86400)
        controller._save_metadata()
        return lifecycle, controller, gh, mem_id, user_id

    def test_default_sweep_preserves_global_user(self, tmp_path: Path) -> None:
        lifecycle, controller, gh, mem_id, user_id = self._setup(tmp_path)

        demoted = lifecycle.run_demotion_sweep(retention_days=30)
        targets = {d["target"] for d in demoted}
        assert "memory" in targets, "stale workspace note should still be demoted"
        assert "user" not in targets, "default sweep must not touch the global user profile"

        # Global user note survives in metadata and on disk.
        assert controller.get_memory(user_id) is not None
        user_md = (gh / "USER.md").read_text(encoding="utf-8")
        assert "prefers concise answers" in user_md

    def test_explicit_include_user_sweeps_global_user(self, tmp_path: Path) -> None:
        lifecycle, controller, gh, mem_id, user_id = self._setup(tmp_path)

        demoted = lifecycle.run_demotion_sweep(retention_days=30, include_user=True)
        assert any(d["target"] == "user" for d in demoted)
        assert controller.get_memory(user_id) is None


class TestGatewayUserWriteApprovalGate:
    """Fix (c): model-driven global-user memory mutations require human approval."""

    @pytest.fixture
    def gw_env(self, tmp_path: Path):
        with closing(LedgerStore(db_path=str(tmp_path / "ledger.db"))) as ledger:
            gw = ToolGateway(ledger=ledger)
            task = ledger.create_task(goal="memory policy", constraints={})
            yield gw, task, gw.tools["memory"]

    def test_user_write_requires_approval(self, gw_env) -> None:
        gw, task, spec = gw_env
        decision, _ = gw.evaluate_policy(
            task, spec, {"action": "add", "target": "user", "content": "prefers X"}
        )
        assert decision == PolicyDecisionType.REQUIRE_APPROVAL

    def test_user_batch_mutation_requires_approval(self, gw_env) -> None:
        gw, task, spec = gw_env
        decision, _ = gw.evaluate_policy(
            task,
            spec,
            {"target": "user", "operations": [{"action": "add", "content": "prefers X"}]},
        )
        assert decision == PolicyDecisionType.REQUIRE_APPROVAL

    def test_user_read_is_allowed(self, gw_env) -> None:
        gw, task, spec = gw_env
        decision, _ = gw.evaluate_policy(task, spec, {"action": "list", "target": "user"})
        assert decision == PolicyDecisionType.ALLOW

    def test_workspace_memory_write_is_allowed(self, gw_env) -> None:
        gw, task, spec = gw_env
        decision, _ = gw.evaluate_policy(
            task, spec, {"action": "add", "target": "memory", "content": "port 8080"}
        )
        assert decision == PolicyDecisionType.ALLOW