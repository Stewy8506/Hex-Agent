"""Tool-failure events and session/subagents telemetry that feed the TUI cockpit.

The diagnostic card and the agents drill-down render only what the server
reports, so these tests pin the wire contract rather than the pixels.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from harness_core.client.acp import ACPServer
from harness_core.core.models import RiskClass, TaskSubmitRequest
from harness_core.core.orchestrator import HarnessOrchestrator
from harness_core.gateway import PolicyBlockedError, ToolSpec


class EventCapture:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, Any, int]] = []

    def __call__(self, event_type: str, tool_id: str, payload: Any, duration_ms: int = 0) -> None:
        self.events.append((event_type, tool_id, payload, duration_ms))

    def of_type(self, event_type: str) -> list[tuple[str, str, Any, int]]:
        return [e for e in self.events if e[0] == event_type]


def make_orchestrator(tmp_path: Path, tool_id: str, handler: Any) -> HarnessOrchestrator:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    orch = HarnessOrchestrator(data_dir=tmp_path / "data", default_workspace=ws)
    orch.gateway.register_tool(
        ToolSpec(
            tool_id=tool_id,
            description="Fixture tool used to exercise failure reporting",
            risk_class=RiskClass.R0,
            handler=handler,
        )
    )
    return orch


def run_step(orch: HarnessOrchestrator, tool_id: str, capture: EventCapture) -> str:
    req = TaskSubmitRequest(
        goal="run the fixture step",
        execution_mode="fast_path",
        workspace_root=str(orch.default_workspace),
        constraints={
            "requested_steps": [
                {"step_id": "s1", "tool_id": tool_id, "args": {}, "risk_class": "R0"}
            ]
        },
    )
    task_id = orch.submit_task(req)["task_id"]
    orch.execute_task(task_id, tool_callback=capture)
    return task_id


@pytest.fixture(autouse=True)
def _testing_env() -> Any:
    with patch.dict(os.environ, {"HEX_TESTING": "1"}):
        yield


def test_soft_failure_is_reported_as_an_error_event(tmp_path: Path) -> None:
    orch = make_orchestrator(
        tmp_path,
        "flaky",
        lambda ws, args: {"ok": False, "error": "assertion failed", "exit_code": 1},
    )
    capture = EventCapture()
    try:
        run_step(orch, "flaky", capture)
        errors = capture.of_type("error")
        assert len(errors) == 1, capture.events
        _, tool_id, payload, duration = errors[0]
        assert tool_id == "flaky"
        assert payload["ok"] is False
        assert payload["exit_code"] == 1
        assert duration >= 0
        assert not capture.of_type("complete"), "a failed call must not report success"
        assert [e[0] for e in capture.events] == ["start", "error"]
    finally:
        orch.ledger.close()


def test_raising_handler_is_reported_as_an_error_event(tmp_path: Path) -> None:
    def boom(ws: Any, args: Any) -> dict[str, Any]:
        raise RuntimeError("gateway exploded")

    orch = make_orchestrator(tmp_path, "boom", boom)
    capture = EventCapture()
    try:
        with pytest.raises(RuntimeError, match="gateway exploded"):
            run_step(orch, "boom", capture)
        errors = capture.of_type("error")
        assert len(errors) == 1, capture.events
        assert errors[0][2] == {"ok": False, "error": "gateway exploded"}
        assert not capture.of_type("complete")
    finally:
        orch.ledger.close()


def test_policy_block_is_left_to_the_approval_flow(tmp_path: Path) -> None:
    def blocked(ws: Any, args: Any) -> dict[str, Any]:
        raise PolicyBlockedError("operator denied the write")

    orch = make_orchestrator(tmp_path, "blocked", blocked)
    capture = EventCapture()
    try:
        task_id = run_step(orch, "blocked", capture)
        # The denial is an operator decision, not a tool diagnostic.
        assert not capture.of_type("error"), capture.events
        assert orch.ledger.get_task(task_id)["status"] == "FAILED"
    finally:
        orch.ledger.close()


def test_successful_call_still_reports_complete(tmp_path: Path) -> None:
    orch = make_orchestrator(tmp_path, "fine", lambda ws, args: {"ok": True, "value": 3})
    capture = EventCapture()
    try:
        run_step(orch, "fine", capture)
        assert [e[0] for e in capture.events] == ["start", "complete"]
        assert capture.of_type("complete")[0][2]["value"] == 3
    finally:
        orch.ledger.close()


class FakeSubagentManager:
    """Records shaped like SubagentManager's, without running any agents."""

    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.records = {r["subagent_id"]: r for r in records}
        self.status_calls: list[str] = []

    def list_active_subagents(self) -> list[dict[str, Any]]:
        return [
            {k: v for k, v in r.items() if k != "result"} for r in self.records.values()
        ]

    def get_subagent_status(self, subagent_id: str) -> dict[str, Any]:
        self.status_calls.append(subagent_id)
        return self.records[subagent_id]


def running_record(subagent_id: str, parent_task_id: str) -> dict[str, Any]:
    return {
        "subagent_id": subagent_id,
        "parent_task_id": parent_task_id,
        "role": "researcher",
        "goal": "Map the approval flow.",
        "depth": 1,
        "status": "running",
        "started_at": 1000.0,
        "ended_at": None,
        "interrupted": False,
        "tool_calls_count": 7,
    }


def completed_record(subagent_id: str, parent_task_id: str) -> dict[str, Any]:
    rec = running_record(subagent_id, parent_task_id)
    rec.update(
        role="test-verifier",
        status="completed",
        ended_at=1042.0,
        tool_calls_count=3,
        result={
            "status": "completed",
            "summary": "All ordering assertions hold.",
            "findings": ["paths sort case-sensitively"],
            "diff_summary": "1 file changed",
            "artifacts": ["report.md"],
            "duration_seconds": 42.0,
            "error": None,
        },
    )
    return rec


def make_server(tmp_path: Path, manager: Any) -> ACPServer:
    return ACPServer(
        orchestrator=SimpleNamespace(subagent_manager=manager),
        workspace_root=tmp_path,
    )


def new_session(server: ACPServer) -> str:
    resp = server.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"cwd": str(server.workspace_root)}}
    )
    return resp["result"]["sessionId"]


def query_subagents(server: ACPServer, session_id: str) -> list[dict[str, Any]]:
    resp = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/subagents",
            "params": {"sessionId": session_id},
        }
    )
    assert "error" not in resp, resp
    assert resp["result"]["sessionId"] == session_id
    return resp["result"]["subagents"]


def test_subagents_are_scoped_to_the_sessions_own_tasks(tmp_path: Path) -> None:
    manager = FakeSubagentManager(
        [
            running_record("sub_mine", "tsk_mine"),
            running_record("sub_other", "tsk_other"),
        ]
    )
    server = make_server(tmp_path, manager)
    session_id = new_session(server)
    server.sessions[session_id].last_task_id = "tsk_mine"

    agents = query_subagents(server, session_id)
    assert [a["subagentId"] for a in agents] == ["sub_mine"]
    assert manager.status_calls == ["sub_mine"]


def test_subagents_include_history_task_ids(tmp_path: Path) -> None:
    manager = FakeSubagentManager(
        [
            running_record("sub_old", "tsk_old"),
            running_record("sub_new", "tsk_new"),
            running_record("sub_other", "tsk_other"),
        ]
    )
    server = make_server(tmp_path, manager)
    session_id = new_session(server)
    sess = server.sessions[session_id]
    sess.last_task_id = "tsk_new"
    sess.history.append({"role": "user", "content": "earlier", "task_id": "tsk_old"})

    agents = query_subagents(server, session_id)
    assert {a["subagentId"] for a in agents} == {"sub_old", "sub_new"}


def test_subagents_empty_when_the_session_has_no_tasks(tmp_path: Path) -> None:
    manager = FakeSubagentManager([running_record("sub_other", "tsk_other")])
    server = make_server(tmp_path, manager)
    session_id = new_session(server)

    assert query_subagents(server, session_id) == []
    assert manager.status_calls == []


def test_subagents_empty_without_a_manager(tmp_path: Path) -> None:
    server = ACPServer(orchestrator=SimpleNamespace(), workspace_root=tmp_path)
    session_id = new_session(server)
    server.sessions[session_id].last_task_id = "tsk_mine"
    assert query_subagents(server, session_id) == []


def test_subagent_wire_carries_the_recorded_result(tmp_path: Path) -> None:
    manager = FakeSubagentManager([completed_record("sub_done", "tsk_mine")])
    server = make_server(tmp_path, manager)
    session_id = new_session(server)
    server.sessions[session_id].last_task_id = "tsk_mine"

    agents = query_subagents(server, session_id)
    assert len(agents) == 1
    wire = agents[0]
    assert wire["subagentId"] == "sub_done"
    assert wire["parentTaskId"] == "tsk_mine"
    assert wire["role"] == "test-verifier"
    assert wire["status"] == "completed"
    assert wire["startedAt"] == 1000.0
    assert wire["endedAt"] == 1042.0
    assert wire["toolCallsCount"] == 3
    assert wire["interrupted"] is False
    result = wire["result"]
    assert result["summary"] == "All ordering assertions hold."
    assert result["findings"] == ["paths sort case-sensitively"]
    assert result["diffSummary"] == "1 file changed"
    assert result["artifacts"] == ["report.md"]
    assert result["durationSeconds"] == 42.0
    assert result["error"] is None


def test_subagent_wire_omits_result_while_running(tmp_path: Path) -> None:
    manager = FakeSubagentManager([running_record("sub_live", "tsk_mine")])
    server = make_server(tmp_path, manager)
    session_id = new_session(server)
    server.sessions[session_id].last_task_id = "tsk_mine"

    wire = query_subagents(server, session_id)[0]
    assert "result" not in wire
    assert wire["endedAt"] is None
    assert wire["status"] == "running"


def test_subagent_status_failure_falls_back_to_the_listed_record(tmp_path: Path) -> None:
    class BrokenStatus(FakeSubagentManager):
        def get_subagent_status(self, subagent_id: str) -> dict[str, Any]:
            raise KeyError(subagent_id)

    manager = BrokenStatus([running_record("sub_live", "tsk_mine")])
    server = make_server(tmp_path, manager)
    session_id = new_session(server)
    server.sessions[session_id].last_task_id = "tsk_mine"

    wire = query_subagents(server, session_id)[0]
    assert wire["subagentId"] == "sub_live"
    assert wire["toolCallsCount"] == 7

class ToolEmittingOrchestrator:
    """Drives the ACP tool callback the way a fast-path turn does."""

    def __init__(self, error_payload: str, complete_payload: str) -> None:
        self.error_payload = error_payload
        self.complete_payload = complete_payload

    def submit_task(self, req: Any) -> dict[str, Any]:
        return {"task_id": "tsk_wire"}

    def execute_task(self, task_id: str, **kwargs: Any) -> dict[str, Any]:
        cb = kwargs["tool_callback"]
        cb("start", "terminal", {"command": "pytest -q"})
        cb("error", "terminal", {"ok": False, "exit_code": 1, "stderr": self.error_payload}, 2500)
        cb("start", "read_file", {"path": "a.rs"})
        cb("complete", "read_file", {"ok": True, "stdout": self.complete_payload}, 10)
        return {"status": "SUCCEEDED", "result": "done"}


def tool_updates(server: ACPServer, session_id: str) -> list[dict[str, Any]]:
    server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "session/prompt",
            "params": {"sessionId": session_id, "prompt": [{"type": "text", "text": "run tests"}]},
        }
    )
    return [
        m["params"]["tool"]
        for m in server.emitted
        if m.get("method") == "session/update" and m["params"].get("updateType") == "tool"
    ]


def test_error_payload_keeps_detail_past_the_success_cap(tmp_path: Path) -> None:
    detail = "E   assert ['b.ts'] == ['a.ts']\n" + ("filler line\n" * 600)
    orch = ToolEmittingOrchestrator(detail, "x" * 5000)
    server = ACPServer(orchestrator=orch, workspace_root=tmp_path)
    server.emitted = []  # type: ignore[attr-defined]
    server._emit = lambda obj: server.emitted.append(obj)  # type: ignore[method-assign]
    session_id = new_session(server)

    updates = tool_updates(server, session_id)
    assert [u["eventType"] for u in updates] == ["start", "error", "start", "complete"]

    error = updates[1]
    assert len(error["payload"]) == 4000
    assert "assert ['b.ts'] == ['a.ts']" in error["payload"]
    assert error["durationMs"] == 2500
    # The card is attached to the call that produced it.
    assert error["callId"] == updates[0]["callId"]
    assert updates[3]["callId"] == updates[2]["callId"]
    assert len(updates[3]["payload"]) == 300
