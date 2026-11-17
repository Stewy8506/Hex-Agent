"""Tool-failure events and session/subagents telemetry that feed the TUI cockpit.

The diagnostic card and the agents drill-down render only what the server
reports, so these tests pin the wire contract rather than the pixels.
"""

from __future__ import annotations

import json
import os
import tracemalloc
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
    assert len(error["payload"].encode("utf-8")) <= 4000
    diagnostics = json.loads(error["payload"])
    assert diagnostics["exit_code"] == 1
    assert diagnostics["_truncated"] is True
    assert "assert ['b.ts'] == ['a.ts']" in diagnostics["stderr"]
    assert error["durationMs"] == 2500
    # The card is attached to the call that produced it.
    assert error["callId"] == updates[0]["callId"]
    assert updates[3]["callId"] == updates[2]["callId"]
    assert len(updates[3]["payload"].encode("utf-8")) <= 300
    assert json.loads(updates[3]["payload"])["_truncated"] is True


@pytest.mark.parametrize("error", [False, True])
def test_five_megabyte_previews_have_bounded_allocations(tmp_path: Path, error: bool) -> None:
    from harness_core.client.acp import _bounded_tool_preview

    huge = "x" * (5 * 1024 * 1024)
    payload = {"stdout": huge, "error": "real diagnostic", "stderr": huge, "exit_code": 7}
    tracemalloc.start()
    try:
        text = _bounded_tool_preview(payload, error=error)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 256 * 1024
    assert len(text.encode("utf-8")) <= (4000 if error else 300)
    preview = json.loads(text)
    assert preview["exit_code"] == 7
    if error:
        assert preview["error"] == "real diagnostic"
        assert preview["stderr"].startswith("x")
    assert payload["stdout"] is huge  # The engine's result is untouched.
    assert payload["stderr"] is huge


def test_previews_bound_visits_depth_keys_and_unicode() -> None:
    from harness_core.client.acp import _bounded_tool_preview

    class CountingDict(dict):
        visited = 0

        def items(self):
            for item in super().items():
                self.visited += 1
                yield item

    wide = CountingDict((str(i), None) for i in range(10000))
    deep = []
    for _ in range(100):
        deep = [deep]
    wide["deep"] = deep
    wide["stderr"] = "😀\n\\\"" * 1000000
    wide["error"] = "é" * 1000000
    wide["exit_code"] = 1
    for value in (wide, deep, {"\n" * 1000000: wide}):
        text = _bounded_tool_preview(value, error=True)
        assert len(text.encode("utf-8")) <= 4000
        json.loads(text)
    assert wide.visited <= 256
    diagnostics = json.loads(_bounded_tool_preview(wide, error=True))
    assert diagnostics["exit_code"] == 1
    assert diagnostics["stderr"].startswith("😀")
    assert diagnostics["error"].startswith("é")


def test_non_json_objects_do_not_run_unbounded_string_conversion() -> None:
    from harness_core.client.acp import _bounded_tool_preview

    class NoString:
        def __str__(self):
            raise AssertionError("preview must not call user __str__")

    assert json.loads(_bounded_tool_preview(NoString(), error=True))["error"] == "<NoString>"
    assert json.loads(_bounded_tool_preview("failure", error=True)) == {"error": "failure"}


@pytest.fixture
def result_server(tmp_path: Path):
    from harness_core.client.acp import ACPSession
    from harness_core.storage.db import LedgerStore

    ledger = LedgerStore(":memory:")
    task_ids = [ledger.submit_task(TaskSubmitRequest(goal=f"task {i}"))["task_id"] for i in range(3)]
    calls = []
    for task_id in task_ids:
        call_id = ledger.record_tool_call(task_id=task_id, tool_name="terminal")
        ledger.seal_tool_call(call_id, {"stdout": f"full result {task_id}"})
        calls.append(call_id)
    server = ACPServer(orchestrator=SimpleNamespace(ledger=ledger), workspace_root=tmp_path)
    sess = ACPSession("owned", tmp_path, last_task_id=task_ids[1], history=[{"task_id": task_ids[0]}])
    server.sessions[sess.session_id] = sess
    try:
        yield server, task_ids, calls
    finally:
        ledger.close()


def result_request(server, task_id, call_id, **params):
    return server.handle_request({
        "jsonrpc": "2.0", "id": 91, "method": "session/toolResult",
        "params": {"sessionId": "owned", "taskId": task_id, "callId": call_id, **params},
    })


def test_tool_result_authorizes_current_history_and_durable_session(result_server) -> None:
    server, tasks, calls = result_server
    for i in (0, 1):
        result = result_request(server, tasks[i], calls[i])["result"]
        assert json.loads(result["content"])["stdout"] == f"full result {tasks[i]}"
        assert result["totalChars"] == len(result["content"])
        assert "nextOffset" not in result
    server._persist_session(server.sessions["owned"])
    server.sessions.clear()
    assert "result" in result_request(server, tasks[0], calls[0], limit=10)
    assert "owned" not in server.sessions  # This read never creates sessions.


def test_tool_result_rejects_foreign_unknown_tasks_calls_and_sessions(result_server) -> None:
    server, tasks, calls = result_server
    for task, call in ((tasks[2], calls[2]), (tasks[0], calls[1]), (tasks[0], "missing"), ("missing", calls[0])):
        assert "error" in result_request(server, task, call)
    assert "error" in result_request(server, tasks[0], calls[0], sessionId="missing")
    assert "missing" not in server.sessions
    assert not server._session_path("missing").exists()
    assert "error" in result_request(server, tasks[0], calls[0], sessionId="../owned")


@pytest.mark.parametrize("params", [
    {"offset": -1}, {"offset": True}, {"offset": "0"}, {"offset": 1.5},
    {"offset": 2**31}, {"limit": 0}, {"limit": -1}, {"limit": True},
    {"limit": "4096"}, {"limit": None}, {"taskId": []}, {"callId": ""},
    {"sessionId": None}, {"result_ref": "/etc/passwd"},
])
def test_tool_result_invalid_arguments(result_server, params) -> None:
    server, tasks, calls = result_server
    response = result_request(server, tasks[0], calls[0], **params)
    assert response["error"]["code"] == -32602
    assert response["id"] == 91


def test_tool_result_missing_and_nonobject_params(result_server) -> None:
    server, _, _ = result_server
    for params in ({}, ["not an object"], {"sessionId": "owned"}):
        response = server.handle_request({"id": 91, "method": "session/toolResult", "params": params})
        assert response["error"]["code"] == -32602


class JournalOrchestrator:
    """Real gateway/storage, with no production orchestrator/provider construction."""

    def __init__(self, ledger, handler, *, replay=False):
        from harness_core.gateway import ToolGateway

        self.ledger = ledger
        self.gateway = ToolGateway(ledger)
        self.gateway.approval_bridge = object()
        self.gateway.register_tool(ToolSpec("fixture", "fixture", RiskClass.R0, handler))
        self.replay = replay

    def submit_task(self, req):
        return self.ledger.submit_task(req)

    def execute_task(self, task_id, stream_callback, tool_callback):
        for _ in range(2 if self.replay else 1):
            attempt = self.ledger.acquire_lease_and_attempt(task_id, "fixture")
            tool_callback("start", "fixture", {})
            try:
                result = self.gateway.invoke(
                    task_id=task_id, attempt_id=attempt["attempt_id"], step_id="fixture",
                    sequence_in_step=1, tool_id="fixture", args={}, workspace_dir=".",
                )
            except Exception as exc:
                tool_callback("error", "fixture", {"ok": False, "error": str(exc)})
                raise
            tool_callback("complete", "fixture", result)
        return {"status": "SUCCEEDED", "result": "done"}


def test_replay_notification_references_its_own_durable_call(tmp_path: Path) -> None:
    from harness_core.storage.db import LedgerStore

    ledger = LedgerStore(":memory:")
    invocations = []
    def handler(ws, args):
        invocations.append(1)
        return {"ok": True, "stdout": "full output"}
    try:
        server = ACPServer(orchestrator=JournalOrchestrator(ledger, handler, replay=True), workspace_root=tmp_path)
        server.emitted = []
        server._emit = server.emitted.append
        updates = tool_updates(server, new_session(server))
        first, replay = updates[1], updates[3]
        assert first["ledgerCallId"] != replay["ledgerCallId"]
        assert first["callId"] == updates[0]["callId"]
        assert replay["callId"] == updates[2]["callId"]
        page = ledger.get_tool_result_page(replay["taskId"], replay["ledgerCallId"])
        result = json.loads(page["content"])
        assert result["_call_id"] == replay["ledgerCallId"]
        assert result["_replayed_from_call_id"] == first["ledgerCallId"]
        assert result["stdout"] == "full output"
        assert invocations == [1]
    finally:
        ledger.close()


def test_exception_reference_preserves_the_original_agent_error(tmp_path: Path) -> None:
    from harness_core.storage.db import LedgerStore

    failure = RuntimeError("original failure")
    def handler(ws, args):
        raise failure
    ledger = LedgerStore(":memory:")
    try:
        server = ACPServer(orchestrator=JournalOrchestrator(ledger, handler), workspace_root=tmp_path)
        server.emitted = []
        server._emit = server.emitted.append
        updates = tool_updates(server, new_session(server))
        error = updates[1]
        assert error["eventType"] == "error"
        assert error["callId"] == updates[0]["callId"]
        assert error["ledgerCallId"] == failure._hex_ledger_call_id
        result = ledger.get_tool_result_page(error["taskId"], error["ledgerCallId"])
        assert json.loads(result["content"]) == {"ok": False, "error": "original failure"}
        assert str(failure) == "original failure"
        assert type(failure) is RuntimeError
    finally:
        ledger.close()
