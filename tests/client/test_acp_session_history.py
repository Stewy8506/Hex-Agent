"""Durable lazy ACP transcripts and bounded, session-scoped history reads."""

from __future__ import annotations

import json
import sqlite3
import threading
import tracemalloc
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from harness_core.client.acp import ACPSession, ACPServer, SQLiteHistory
from tests.client.test_acp_session_lifecycle import RecordingOrchestrator
from tests.client.test_acp_tool_failure_and_subagents import FakeSubagentManager, running_record


def request(server, method, params):
    return server.handle_request({"id": 6, "method": method, "params": params})


def seed(server, sid, records, **kwargs):
    sess = ACPSession(sid, server.workspace_root, history=records, **kwargs)
    server.sessions[sid] = sess
    assert server._persist_session(sess)
    assert isinstance(sess.history, SQLiteHistory)
    return sess


def legacy(server, sid, records):
    server._sessions_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": sid, "workspace_root": str(server.workspace_root),
        "model": "test-model", "created_at": 42.5,
        "last_task_id": "current", "history": records,
    }
    path = server._session_path(sid)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_exact_legacy_migration_restart_list_and_sequence(tmp_path):
    server = ACPServer(orchestrator=RecordingOrchestrator(), workspace_root=tmp_path)
    records = [
        {"prompt": f"question {i} é😀", "response": f"answer {i}\n\\\"",
         "task_id": f"old_{i}", "duration": i / 3, "status": "completed",
         "custom": {"nested": [i, None, False]}}
        for i in range(14)
    ] + [{"role": "user", "content": "other shape", "response": None}]
    path = legacy(server, "legacy", records)
    listed = request(server, "session/list", {})["result"]["sessions"]
    assert listed == [{
        "sessionId": "legacy",
        "cwd": str(tmp_path.resolve()),
        "model": "test-model",
        "created_at": 42.5,
        "turns": len(records),
        "title": "",
        "archived": False,
        "pinned": False,
    }]
    assert not server.sessions
    metadata = json.loads(path.read_text())
    assert "history" not in metadata
    assert metadata["history_store"] == "sqlite"
    assert (server._sessions_dir / "history.db").is_file()

    restarted = ACPServer(orchestrator=RecordingOrchestrator(), workspace_root=tmp_path)
    result = request(restarted, "session/load", {"sessionId": "legacy"})["result"]
    assert type(result["history"]) is list
    assert result["history"] == records
    history = restarted.sessions["legacy"].history
    assert isinstance(history, SQLiteHistory)
    assert history == records and records == history
    assert list(history) == records
    assert history[-1] == records[-1]
    assert history[0] == records[0]
    assert history[-10:] == records[-10:]
    assert history[2:12:3] == records[2:12:3]
    assert history[::-1] == records[::-1]
    assert history[:0] == []
    with pytest.raises(IndexError):
        history[len(records)]
    with pytest.raises(IndexError):
        history[-len(records) - 1]
    with pytest.raises(ValueError):
        history[::0]
    assert history.has_task("old_0")
    assert not history.has_task("foreign")
    assert history.task_ids() == {f"old_{i}" for i in range(14)}
    history.append({"prompt": "next", "response": "durable immediately"})
    # No metadata rewrite is necessary for the newly appended record.
    third = ACPServer(workspace_root=tmp_path)
    assert third._load_session_from_disk("legacy").history[-1]["response"] == "durable immediately"
    assert set(vars(history)) == {"path", "session_id"}


def test_model_context_remains_exact_last_ten_not_page_prefixes(tmp_path):
    orch = RecordingOrchestrator()
    server = ACPServer(orchestrator=orch, workspace_root=tmp_path)
    records = [{"prompt": f"p{i}" + "é" * 5000, "response": f"r{i}" + "😀" * 5000}
               for i in range(18)]
    sess = seed(server, "context", records)
    server._emit = lambda _: None
    result = request(server, "session/prompt", {"sessionId": "context", "prompt": "continue"})
    assert "result" in result
    expected = [message for turn in records[-10:] for message in (
        {"role": "user", "content": turn["prompt"]},
        {"role": "assistant", "content": turn["response"]},
    )]
    assert orch.requests[0].constraints["conversation_history"] == expected
    assert len(sess.history) == 19


def test_new_session_becomes_durable_before_first_turn(tmp_path):
    server = ACPServer(orchestrator=RecordingOrchestrator(), workspace_root=tmp_path)
    sid = request(server, "session/new", {"cwd": str(tmp_path)})["result"]["sessionId"]
    history = server.sessions[sid].history
    assert isinstance(history, SQLiteHistory)
    assert history == []
    history.append({"prompt": "sent", "response": "saved"})
    restarted = ACPServer(workspace_root=tmp_path)
    assert request(restarted, "session/load", {"sessionId": sid})["result"]["history"] == [
        {"prompt": "sent", "response": "saved"}
    ]


@pytest.mark.parametrize("nul", ["\x00", ""])
def test_unicode_pages_keep_all_turns_and_content_reconstructs(tmp_path, nul):
    server = ACPServer(workspace_root=tmp_path)
    text = ("😀é中" + nul + "\n\\\"e\u0301") * 1300
    records = [{"prompt": text + str(i), "response": text[::-1] + str(i),
                "task_id": f"t{i}", "custom": [i, True]} for i in range(67)]
    seed(server, "unicode", records)
    restarted = ACPServer(workspace_root=tmp_path)
    load = request(restarted, "session/load", {"sessionId": "unicode", "paged": True})["result"]
    assert load["sessionId"] == "unicode" and load["cwd"] == str(tmp_path)
    assert len(load["history"]) == 32
    assert len(json.dumps(load).encode("utf-8")) <= 64 * 1024
    assert len(json.dumps(load, ensure_ascii=False).encode("utf-8")) <= 64 * 1024
    seen = []
    page = load
    while True:
        seen = page["history"] + seen
        assert len(page["history"]) <= 32
        assert page["historyCount"] == 67
        assert len(json.dumps(page).encode("utf-8")) <= 64 * 1024
        indices = [record["turnIndex"] for record in page["history"]]
        assert indices == sorted(indices)
        for record in page["history"]:
            original = records[record["turnIndex"]]
            assert record["custom"] == original["custom"]
            assert record["task_id"] == original["task_id"]
            for field in ("prompt", "response"):
                assert len(record[field]) <= 4096
                assert original[field].startswith(record[field])
                assert record[field + "Chars"] == len(original[field])
        if "nextBefore" not in page:
            break
        assert page["nextBefore"] == page["history"][0]["turnIndex"]
        page = request(restarted, "session/history", {
            "sessionId": "unicode", "before": page["nextBefore"], "limit": 32,
        })["result"]
    assert [record["turnIndex"] for record in seen] == list(range(67))
    assert request(restarted, "session/history", {"sessionId": "unicode", "before": 0})["result"] == {
        "history": [], "historyCount": 67,
    }
    for index in (0, 66):
        for field in ("prompt", "response"):
            parts, offset = [], 0
            while True:
                page = request(restarted, "session/historyContent", {
                    "sessionId": "unicode", "turnIndex": index, "field": field,
                    "offset": offset, "limit": 4096,
                })["result"]
                assert page["totalChars"] == len(records[index][field])
                assert len(page["content"]) <= 4096
                parts.append(page["content"])
                if "nextOffset" not in page:
                    break
                assert page["nextOffset"] == offset + len(page["content"])
                offset = page["nextOffset"]
            assert "".join(parts) == records[index][field]
    assert request(restarted, "session/historyContent", {
        "sessionId": "unicode", "turnIndex": 0, "field": "prompt", "offset": len(records[0]["prompt"]),
    })["result"] == {"content": "", "totalChars": len(records[0]["prompt"])}
    assert request(restarted, "session/load", {"sessionId": "unicode", "paged": False})["result"]["history"] == records


@pytest.mark.parametrize("method,params", [
    ("session/history", {"before": v}) for v in (-1, True, "0", 1.2, None, 2**31, 2)
] + [
    ("session/history", {"limit": v}) for v in (0, -1, True, "32", None, 33)
] + [
    ("session/historyContent", {"turnIndex": v, "field": "prompt"})
    for v in (-1, True, "0", None, 2**31, 1)
] + [
    ("session/historyContent", {"turnIndex": 0, "field": "prompt", "offset": v})
    for v in (-1, True, "0", None, 2**31, 4)
] + [
    ("session/historyContent", {"turnIndex": 0, "field": "response", "limit": v})
    for v in (0, -1, True, "1", None, 4097)
] + [
    ("session/historyContent", {"turnIndex": 0, "field": v})
    for v in (None, [], "task_id", "prompt; DROP TABLE history")
] + [
    ("session/historyContent", {"field": "prompt"}),
    ("session/historyContent", {"turnIndex": 0}),
    ("session/load", {"paged": 1}), ("session/load", {"paged": None}),
    ("session/load", {"history": []}), ("session/history", {"path": "/etc/passwd"}),
    ("session/historyContent", {"turnIndex": 0, "field": "prompt", "extra": True}),
])
def test_strict_history_argument_validation(tmp_path, method, params):
    server = ACPServer(workspace_root=tmp_path)
    seed(server, "owned", [{"prompt": "abc", "response": "def"}])
    response = request(server, method, {"sessionId": "owned", **params})
    assert response["error"]["code"] == -32602
    assert response["id"] == 6


@pytest.mark.parametrize("method", ["session/load", "session/history", "session/historyContent"])
def test_history_scope_nonobjects_missing_and_invalid_sessions(tmp_path, method):
    server = ACPServer(workspace_root=tmp_path)
    extra = {"turnIndex": 0, "field": "prompt"} if method.endswith("Content") else {}
    for params in (None, [], False, "owned", {}, {"sessionId": None}, {"sessionId": []},
                   {"sessionId": "../owned"}, {"sessionId": "owned.json"}, {"sessionId": "x" * 257}):
        response = request(server, method, {**params, **extra} if isinstance(params, dict) else params)
        assert response["error"]["code"] == -32602
    assert "error" in request(server, method, {"sessionId": "missing", **extra})
    assert not server.sessions
    assert not server._sessions_dir.exists()
    other = ACPServer(workspace_root=tmp_path / "other")
    seed(server, "owned", [{"prompt": "secret", "response": "private"}])
    assert "error" in request(other, method, {"sessionId": "owned", **extra})
    assert not other.sessions and not other._sessions_dir.exists()


def test_indexed_task_ownership_never_fetches_records(tmp_path):
    manager = FakeSubagentManager([
        running_record("old", "old_task"), running_record("current", "current_task"),
        running_record("foreign", "other_task"),
    ])
    ledger = SimpleNamespace(get_tool_result_page=lambda task, call, **kwargs: {"content": task})
    server = ACPServer(orchestrator=SimpleNamespace(ledger=ledger, subagent_manager=manager), workspace_root=tmp_path)
    seed(server, "owned", [{"task_id": "old_task", "prompt": "p", "response": "r"}], last_task_id="current_task")
    server.sessions.clear()
    with patch.object(SQLiteHistory, "__getitem__", side_effect=AssertionError("record materialization")):
        for task in ("old_task", "current_task"):
            assert request(server, "session/toolResult", {
                "sessionId": "owned", "taskId": task, "callId": "call",
            })["result"] == {"content": task}
        assert "error" in request(server, "session/toolResult", {
            "sessionId": "owned", "taskId": "other_task", "callId": "call",
        })
        agents = request(server, "session/subagents", {"sessionId": "owned"})["result"]["subagents"]
        assert {agent["subagentId"] for agent in agents} == {"old", "current"}
        assert request(server, "session/list", {})["result"]["sessions"][0]["turns"] == 1
    assert not server.sessions


def test_giant_stored_history_paged_load_and_list_have_bounded_python_allocations(tmp_path):
    server = ACPServer(workspace_root=tmp_path)
    sess = seed(server, "giant", [])
    huge = "😀\n\\\"" * (128 * 1024)
    for i in range(40):
        sess.history.append({"task_id": f"t{i}", "prompt": huge, "response": huge, "duration": i})
    server.sessions.clear()
    del sess, huge
    restarted = ACPServer(workspace_root=tmp_path)
    inactive = ACPServer(workspace_root=tmp_path)
    tracemalloc.start()
    try:
        page = request(restarted, "session/load", {"sessionId": "giant", "paged": True})["result"]
        listing = request(inactive, "session/list", {})["result"]
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(page["history"]) == 32 and page["historyCount"] == 40
    assert listing["sessions"][0]["turns"] == 40
    assert peak < 2 * 1024 * 1024
    assert isinstance(restarted.sessions["giant"].history, SQLiteHistory)
    assert len(json.dumps(page).encode()) <= 64 * 1024
    # An inactive listing must not promote disk sessions into retained objects.
    with patch.object(SQLiteHistory, "__getitem__", side_effect=AssertionError("record fetch")):
        assert request(inactive, "session/list", {})["result"] == listing
    assert not inactive.sessions


@pytest.mark.parametrize("failure", ["insert", "replace"])
def test_failed_migration_keeps_list_and_original_json_and_restart_is_idempotent(tmp_path, failure):
    server = ACPServer(workspace_root=tmp_path)
    records = [{"prompt": f"p{i}", "response": f"r{i}", "task_id": f"t{i}"} for i in range(4)]
    path = legacy(server, "legacy", records)
    original = path.read_bytes()
    sess = ACPSession("legacy", tmp_path, history=records)
    insert = SQLiteHistory._insert

    def failing_insert(history, conn, index, record):
        if index == 2:
            raise sqlite3.OperationalError("disk full")
        insert(history, conn, index, record)

    target = patch.object(SQLiteHistory, "_insert", failing_insert) if failure == "insert" else patch.object(
        Path, "replace", side_effect=OSError("replace failed"),
    )
    with target:
        assert not server._persist_session(sess)
        assert sess.history is records
        assert path.read_bytes() == original
        loaded = ACPServer(workspace_root=tmp_path)._load_session_from_disk("legacy")
        assert type(loaded.history) is list and loaded.history == records
        assert path.read_bytes() == original
        failed_page = request(server, "session/load", {"sessionId": "legacy", "paged": True})
        assert "error" in failed_page
        assert type(server.sessions["legacy"].history) is list
        assert server.sessions["legacy"].history == records
        assert path.read_bytes() == original
    history = SQLiteHistory(server._sessions_dir / "history.db", "legacy")
    assert len(history) == (0 if failure == "insert" else 4)
    # A fresh process reruns legacy migration after the failed metadata switch.
    recovered = ACPServer(workspace_root=tmp_path)._load_session_from_disk("legacy")
    assert isinstance(recovered.history, SQLiteHistory)
    assert list(recovered.history) == records
    assert len(history) == 4
    sess.history.append({"prompt": "after failure", "response": "retained"})
    assert server._persist_session(sess)
    assert len(sess.history) == 5
    assert list(sess.history) == records
    restarted = ACPServer(workspace_root=tmp_path)
    assert list(restarted._load_session_from_disk("legacy").history) == records
    assert restarted._persist_session(restarted._load_session_from_disk("legacy"))
    assert len(history) == 5


def test_history_connections_close_after_reads_writes_and_failures(tmp_path):
    server = ACPServer(workspace_root=tmp_path)
    history = seed(server, "closed", [{"prompt": "é😀", "response": "ok", "task_id": "task"}]).history
    connections = []
    connect = sqlite3.connect

    def tracked_connect(*args, **kwargs):
        conn = connect(*args, **kwargs)
        connections.append(conn)
        return conn

    with patch("harness_core.client.acp.sqlite3.connect", side_effect=tracked_connect):
        assert len(history) == 1
        assert history[0]["prompt"] == "é😀"
        assert history.has_task("task")
        assert history.task_ids() == {"task"}
        assert history.page(1, 1)["historyCount"] == 1
        assert history.content(0, "prompt", 1, 1)["content"] == "😀"
        history.append({"prompt": "next", "response": "saved"})
        with pytest.raises(TypeError):
            history.append({"prompt": object()})
    for conn in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            conn.execute("SELECT 1")
    assert len(history) == 2


def test_concurrent_append_uses_unique_durable_ordinals(tmp_path):
    server = ACPServer(workspace_root=tmp_path)
    history = seed(server, "threads", []).history
    errors = []

    def append(worker):
        try:
            handle = SQLiteHistory(history.path, "threads")
            for i in range(8):
                handle.append({"prompt": f"{worker}:{i}", "response": "ok"})
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=append, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert not errors
    assert len(history) == 32
    assert len({record["prompt"] for record in history}) == 32
    assert [record["turnIndex"] for record in history.page(32, 32)["history"]] == list(range(32))


@pytest.mark.parametrize("streaming", [True, False], ids=["single-chunk", "deliverable"])
@pytest.mark.parametrize("options", [{}, {"pagedResponse": False}, {"pagedResponse": True}],
                         ids=["legacy-default", "legacy-false", "paged"])
def test_giant_prompt_response_wire_and_durable_original(tmp_path, streaming, options):
    unit = "😀é中\n\\\"e\u0301"
    source = unit * (8 * 1024 * 1024 // len(unit.encode("utf-8")) + 1) + "exact tail"
    assert len(source.encode("utf-8")) > 8 * 1024 * 1024
    paged = options.get("pagedResponse", False)
    orch = RecordingOrchestrator()
    server = ACPServer(orchestrator=orch, workspace_root=tmp_path)
    records = [{"prompt": f"p{i}", "response": f"r{i}"} for i in range(13)]
    sess = seed(server, "response", records)
    frames = []
    checked_durable = False

    class WireSink:
        def write(self, wire):
            nonlocal checked_durable
            assert wire.endswith("\n")
            frame = json.loads(wire)
            if paged:
                assert len(wire.encode("utf-8")) < 64 * 1024
                assert len(json.dumps(frame).encode("utf-8")) + 1 < 64 * 1024
            frames.append(frame)
            # Synthesized deltas must follow the committed full record, not a preview.
            if not streaming and frame.get("method") == "session/update" and not checked_durable:
                stored = ACPServer(workspace_root=tmp_path)._load_session_from_disk("response")
                assert stored.history[-1]["response"] == source
                checked_durable = True

        def flush(self):
            pass

    def execute(task_id, stream_callback=None, tool_callback=None):
        orch.executed.append(task_id)
        stream_callback(source if streaming else "")
        return {"status": "SUCCEEDED", "deliverable": source}

    with patch.object(orch, "execute_task", side_effect=execute), patch("sys.stdout", WireSink()):
        response = request(server, "session/prompt", {
            "sessionId": "response", "prompt": "continue", **options,
        })
        server._emit(response)

    result = response["result"]
    updates = [frame["params"] for frame in frames if frame.get("method") == "session/update"]
    deltas = [update["delta"] for update in updates if "delta" in update]
    assert "".join(deltas) == source
    assert updates[-1]["updateType"] == "status" and updates[-1]["status"] == "Ready"
    if paged:
        assert all(0 < len(delta) <= 4096 for delta in deltas)
        assert all(update["updateType"] == "agent_response" for update in updates if "delta" in update)
        assert "delta" not in updates[-1]
        assert result["content"] == [{
            "type": "text", "text": source[:4096] + "\n[Full response: /history-turn 13 response]",
        }]
    else:
        assert deltas == [source]
        assert [update["updateType"] for update in updates] == (
            ["agent_response", "stream_end", "status"] if streaming else ["status"]
        )
        assert result["content"] == [{"type": "text", "text": source}]
    assert frames[-1] == response
    assert result["status"] == "completed" and result["taskId"] == "tsk_rec_1"
    expected = [message for turn in records[-10:] for message in (
        {"role": "user", "content": turn["prompt"]},
        {"role": "assistant", "content": turn["response"]},
    )]
    assert orch.requests[0].constraints["conversation_history"] == expected
    assert isinstance(sess.history, SQLiteHistory) and len(sess.history) == 14
    restarted = ACPServer(workspace_root=tmp_path)._load_session_from_disk("response")
    assert restarted.history[-1] == sess.history[-1]
    assert restarted.history[-1]["response"] == source
    assert restarted.history[-1]["prompt"] == "continue"


@pytest.mark.parametrize("streaming", [True, False])
@pytest.mark.parametrize("length", [17, 4096])
def test_short_paged_prompt_preserves_legacy_wire(tmp_path, streaming, length):
    source = ("😀é\n\\\"" * length)[:length]
    captures = []
    for label, options in (("default", {}), ("false", {"pagedResponse": False}),
                           ("true", {"pagedResponse": True})):
        orch = RecordingOrchestrator()
        server = ACPServer(orchestrator=orch, workspace_root=tmp_path / label)
        frames = []
        server._emit = frames.append

        def execute(task_id, stream_callback=None, tool_callback=None):
            for chunk in ("", source[:7], "", source[7:]) if streaming else ("",):
                stream_callback(chunk)
            return {"status": "SUCCEEDED", "result": source}

        with patch.object(orch, "execute_task", side_effect=execute):
            response = request(server, "session/prompt", {
                "sessionId": "short", "prompt": "answer", **options,
            })
        assert response["result"]["content"] == [{"type": "text", "text": source}]
        assert server.sessions["short"].history[-1]["response"] == source
        captures.append((frames, response))
    assert captures[0] == captures[1] == captures[2]


@pytest.mark.parametrize("value", [None, 0, 1, 1.0, "true", "false", [], {}])
@pytest.mark.parametrize("session_kind", ["auto", "active", "disk"])
def test_invalid_paged_response_precedes_all_session_side_effects(tmp_path, value, session_kind):
    orch = RecordingOrchestrator()
    server = ACPServer(orchestrator=orch, workspace_root=tmp_path)
    if session_kind == "auto":
        params = {}
    else:
        params = {"sessionId": "existing"}
        if session_kind == "active":
            sess = seed(server, "existing", [{"prompt": "p", "response": "r"}])
            sess.cancelled = True
            sess.cancel_ack.clear()
        else:
            path = legacy(server, "existing", [{"prompt": "p", "response": "r"}])
            original = path.read_bytes()
    emitted = []
    server._emit = emitted.append
    with patch.object(server, "_load_session_from_disk", wraps=server._load_session_from_disk) as load, \
         patch.object(server, "_persist_session", wraps=server._persist_session) as persist:
        response = request(server, "session/prompt", {"prompt": "must not run", "pagedResponse": value, **params})
    assert "error" in response and response["id"] == 6
    assert response["error"]["message"] == "pagedResponse must be a boolean"
    assert not orch.requests and not orch.executed and not emitted
    load.assert_not_called()
    persist.assert_not_called()
    if session_kind == "active":
        assert server.sessions == {"existing": sess}
        assert sess.cancelled and not sess.running and not sess.cancel_ack.is_set()
        assert sess.history == [{"prompt": "p", "response": "r"}]
    else:
        assert not server.sessions
        if session_kind == "auto":
            assert not server._sessions_dir.exists()
        else:
            assert path.read_bytes() == original
            assert not (server._sessions_dir / "history.db").exists()


def test_paged_stream_callback_does_not_allocate_a_whole_slice_list(tmp_path):
    source = "😀" * (2 * 1024 * 1024 + 1)
    orch = RecordingOrchestrator()
    server = ACPServer(orchestrator=orch, workspace_root=tmp_path)
    chars_sent = 0
    peak = 0

    def consume(frame):
        nonlocal chars_sent
        assert len(json.dumps(frame).encode("utf-8")) < 64 * 1024
        params = frame["params"]
        if "delta" in params:
            delta = params["delta"]
            assert 0 < len(delta) <= 4096
            assert source.startswith(delta, chars_sent)
            chars_sent += len(delta)

    server._emit = consume

    def execute(task_id, stream_callback=None, tool_callback=None):
        nonlocal peak
        tracemalloc.start()
        try:
            stream_callback(source)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        return {"status": "SUCCEEDED", "result": "short final"}

    with patch.object(orch, "execute_task", side_effect=execute):
        response = request(server, "session/prompt", {
            "sessionId": "allocation", "prompt": "stream", "pagedResponse": True,
        })
    assert "result" in response
    assert chars_sent == len(source)
    assert peak < 512 * 1024


@pytest.mark.parametrize("cancel_before", [False, True])
def test_paged_stream_cancellation_stops_between_slices_and_session_recovers(tmp_path, cancel_before):
    source = "😀é中" * 5000
    orch = RecordingOrchestrator()
    server = ACPServer(orchestrator=orch, workspace_root=tmp_path)
    sess = seed(server, "cancel", [])
    frames = []
    cancel_replies = []

    def cancel():
        cancel_replies.append(request(server, "session/cancel", {"sessionId": "cancel"}))
        sess.cancel_ack.set()

    def consume(frame):
        frames.append(frame)
        if frame["params"]["updateType"] == "agent_response":
            cancel()

    server._emit = consume

    def execute(task_id, stream_callback=None, tool_callback=None):
        if cancel_before:
            cancel()
        stream_callback(source)
        stream_callback("suppressed")
        tool_callback("complete", "terminal", {"stdout": "suppressed"})
        return {"status": "SUCCEEDED", "result": source}

    with patch.object(orch, "execute_task", side_effect=execute):
        response = request(server, "session/prompt", {
            "sessionId": "cancel", "prompt": "stream", "pagedResponse": True,
        })
    updates = [frame["params"] for frame in frames]
    assert [update["updateType"] for update in updates] == (
        ["status"] if cancel_before else ["agent_response", "status"]
    )
    assert "".join(update.get("delta", "") for update in updates) == ("" if cancel_before else source[:4096])
    assert updates[-1] == {"sessionId": "cancel", "updateType": "status", "status": "Cancelled"}
    assert response["result"]["status"] == "cancelled"
    assert response["result"]["content"] == [{"type": "text", "text": "Session execution was cancelled."}]
    assert sess.history[-1]["response"] == "Session execution was cancelled."
    assert cancel_replies[0]["result"]["cancelled"] is True
    assert orch.cancelled == [("tsk_rec_1", "acp")]
    server._emit = frames.append
    followup = request(server, "session/prompt", {
        "sessionId": "cancel", "prompt": "fresh", "pagedResponse": True,
    })
    assert followup["result"]["content"] == [{"type": "text", "text": "answer to: fresh"}]
    assert not sess.cancelled and not sess.running and len(sess.history) == 2


@pytest.mark.parametrize("cancel_before_slices", [False, True])
def test_cancel_during_paged_final_delivery_never_restores_a_huge_ready_delta(tmp_path, cancel_before_slices):
    source = "\U0001f600é中" * 5000
    orch = RecordingOrchestrator()
    server = ACPServer(orchestrator=orch, workspace_root=tmp_path)
    sess = seed(server, "final_cancel", [])
    frames = []
    append_history = server._append_history

    def cancel():
        assert request(server, "session/cancel", {"sessionId": "final_cancel"})["result"]["cancelled"]
        sess.cancel_ack.set()

    def append_then_cancel(session, record):
        append_history(session, record)
        if cancel_before_slices:
            cancel()

    def consume(frame):
        assert len(json.dumps(frame).encode("utf-8")) < 64 * 1024
        frames.append(frame)
        if frame["params"]["updateType"] == "agent_response":
            cancel()

    server._emit = consume
    with patch.object(server, "_append_history", side_effect=append_then_cancel), \
         patch.object(orch, "execute_task", return_value={"status": "SUCCEEDED", "deliverable": source}):
        response = request(server, "session/prompt", {
            "sessionId": "final_cancel", "prompt": "answer", "pagedResponse": True,
        })
    updates = [frame["params"] for frame in frames]
    assert "".join(update.get("delta", "") for update in updates) == (
        "" if cancel_before_slices else source[:4096]
    )
    assert updates[-1] == {"sessionId": "final_cancel", "updateType": "status", "status": "Ready"}
    # Execution already completed and committed before this late delivery cancel.
    assert response["result"]["status"] == "completed"
    assert response["result"]["content"][0]["text"] == source[:4096] + "\n[Full response: /history-turn 0 response]"
    assert len(json.dumps(response).encode("utf-8")) < 64 * 1024
    assert sess.history[-1]["response"] == source
