"""F05 session lifecycle tests: conversation continuity, durable resume, cancellation.

Proves the audit's done-when criteria against the real ACP server:
- a two-turn conversation carries prior dialogue into the next task request,
- a restart can resume a session with its transcript intact,
- session/cancel reaches an in-flight execution while the stdio loop keeps reading,
- a cancelled turn does not poison the session for subsequent turns.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness_core.client.acp import ACPServer


class RecordingOrchestrator:
    """Scripted orchestrator capturing submitted requests and cancels."""

    def __init__(self) -> None:
        self.requests = []
        self.executed: list[str] = []
        self.cancelled: list[tuple[str, str]] = []
        self.ledger = None

    def submit_task(self, req):
        self.requests.append(req)
        return {"task_id": f"tsk_rec_{len(self.requests)}"}

    def execute_task(self, task_id, stream_callback=None, tool_callback=None):
        self.executed.append(task_id)
        goal = self.requests[-1].goal if self.requests else task_id
        return {"status": "SUCCEEDED", "payload": {"result": f"answer to: {goal}"}}

    def cancel_task(self, task_id, actor="owner"):
        self.cancelled.append((task_id, actor))
        return {"status": "CANCELED"}


class BlockingOrchestrator(RecordingOrchestrator):
    """execute_task blocks until cancelled, emulating a long-running turn."""

    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def execute_task(self, task_id, stream_callback=None, tool_callback=None):
        self.executed.append(task_id)
        self.started.set()
        if not self.release.wait(timeout=10):
            raise TimeoutError("cancel never reached the blocking execution")
        return {"status": "SUCCEEDED", "payload": {"result": "late answer"}}

    def cancel_task(self, task_id, actor="owner"):
        self.cancelled.append((task_id, actor))
        self.release.set()
        return {"status": "CANCELED"}


def _req(id_, method, params):
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params}


class TestSessionContinuity(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.orch = RecordingOrchestrator()
        self.server = ACPServer(orchestrator=self.orch, workspace_root=self.ws)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _new_session(self) -> str:
        resp = self.server.handle_request(_req(1, "session/new", {"cwd": str(self.ws)}))
        return resp["result"]["sessionId"]

    def _prompt(self, sid: str, text: str, id_: int = 2):
        return self.server.handle_request(
            _req(id_, "session/prompt", {"sessionId": sid, "prompt": text})
        )

    def test_two_turn_conversation_carries_context(self) -> None:
        sid = self._new_session()
        self._prompt(sid, "Remember the codeword blueberry", id_=2)
        self._prompt(sid, "What codeword did I give you?", id_=3)

        self.assertEqual(len(self.orch.requests), 2)
        second = self.orch.requests[1]
        self.assertEqual(second.goal, "What codeword did I give you?")
        self.assertEqual(second.constraints["conversation_id"], sid)
        history = second.constraints["conversation_history"]
        self.assertEqual(history[0], {"role": "user", "content": "Remember the codeword blueberry"})
        self.assertEqual(history[1]["role"], "assistant")
        self.assertIn("blueberry", history[1]["content"])

    def test_session_resumes_after_server_restart(self) -> None:
        sid = self._new_session()
        self._prompt(sid, "first turn prompt")

        # A brand-new server process (fresh in-memory state) must resume from disk.
        orch2 = RecordingOrchestrator()
        server2 = ACPServer(orchestrator=orch2, workspace_root=self.ws)
        load = server2.handle_request(_req(1, "session/load", {"sessionId": sid}))
        self.assertIn("result", load)
        self.assertEqual(load["result"]["historyCount"], 1)
        self.assertEqual(load["result"]["history"][0]["prompt"], "first turn prompt")

        listed = server2.handle_request(_req(2, "session/list", {}))
        self.assertIn(sid, [s["sessionId"] for s in listed["result"]["sessions"]])

        resp = server2.handle_request(
            _req(3, "session/prompt", {"sessionId": sid, "prompt": "second turn"})
        )
        self.assertEqual(resp["result"]["status"], "completed")
        history = orch2.requests[0].constraints["conversation_history"]
        self.assertEqual(history[0]["content"], "first turn prompt")

    def test_load_unknown_session_is_an_error(self) -> None:
        resp = self.server.handle_request(_req(1, "session/load", {"sessionId": "acp_sess_missing"}))
        self.assertIn("error", resp)
        self.assertIn("Session not found", resp["error"]["message"])

    def test_cancelled_turn_does_not_poison_session(self) -> None:
        sid = self._new_session()
        cancel = self.server.handle_request(_req(2, "session/cancel", {"sessionId": sid}))
        self.assertTrue(cancel["result"]["cancelled"])
        resp = self._prompt(sid, "fresh turn after cancel", id_=3)
        self.assertEqual(resp["result"]["status"], "completed")
        self.assertEqual(len(self.orch.executed), 1)

    def test_concurrent_prompt_on_busy_session_is_rejected(self) -> None:
        sid = self._new_session()
        self.server.sessions[sid].running = True
        resp = self._prompt(sid, "overlapping turn")
        self.assertIn("error", resp)
        self.assertIn("already running", resp["error"]["message"])


class TestConcurrentCancellation(unittest.TestCase):
    def test_cancel_reaches_inflight_execution_over_stdio(self) -> None:
        """run_stdio must keep reading while a turn executes, so session/cancel
        is handled mid-turn and reaches the orchestrator, not just a flag."""
        with tempfile.TemporaryDirectory() as tmp:
            orch = BlockingOrchestrator()
            server = ACPServer(orchestrator=orch, workspace_root=tmp)

            in_r, in_w = os.pipe()
            out_r, out_w = os.pipe()
            stdin = os.fdopen(in_r, "r", encoding="utf-8")
            stdout = os.fdopen(out_w, "w", encoding="utf-8")
            writer = os.fdopen(in_w, "w", encoding="utf-8")

            received: list[dict] = []
            reader_done = threading.Event()

            def _read_output() -> None:
                with os.fdopen(out_r, "r", encoding="utf-8") as reader:
                    for line in reader:
                        if line.strip():
                            received.append(json.loads(line))
                reader_done.set()

            reader_thread = threading.Thread(target=_read_output, daemon=True)
            reader_thread.start()

            def _send(obj: dict) -> None:
                writer.write(json.dumps(obj) + "\n")
                writer.flush()

            with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
                loop_thread = threading.Thread(target=server.run_stdio, daemon=True)
                loop_thread.start()
                try:
                    _send(_req(1, "session/new", {"cwd": tmp}))
                    sid = None
                    for _ in range(200):
                        sid = next(
                            (
                                m["result"]["sessionId"]
                                for m in received
                                if m.get("id") == 1 and "result" in m
                            ),
                            None,
                        )
                        if sid:
                            break
                        threading.Event().wait(0.05)
                    self.assertIsNotNone(sid, f"session/new never answered: {received}")

                    _send(_req(2, "session/prompt", {"sessionId": sid, "prompt": "long task"}))
                    self.assertTrue(orch.started.wait(timeout=5), "execution never started")

                    # The turn is still blocking; cancel must be read and handled now.
                    _send(_req(3, "session/cancel", {"sessionId": sid}))

                    def _wait_for_responses() -> bool:
                        ids = {m.get("id") for m in received if "result" in m or "error" in m}
                        return {2, 3}.issubset(ids)

                    for _ in range(200):
                        if _wait_for_responses():
                            break
                        threading.Event().wait(0.05)
                    self.assertTrue(_wait_for_responses(), f"missing responses: {received}")

                    responses = [m for m in received if m.get("id") in (2, 3)]
                    self.assertEqual(responses[0]["id"], 3, "cancel must be answered before the turn ends")
                    self.assertTrue(responses[0]["result"]["cancelled"])
                    prompt_resp = responses[1]["result"]
                    self.assertEqual(prompt_resp["status"], "cancelled")
                    self.assertEqual(orch.cancelled, [(prompt_resp["taskId"], "acp")])

                    cancelled_notifs = [
                        m for m in received
                        if m.get("method") == "session/update"
                        and m["params"].get("updateType") == "status"
                        and m["params"].get("status") == "Cancelled"
                    ]
                    self.assertEqual(len(cancelled_notifs), 1)
                finally:
                    writer.close()  # EOF shuts the stdio loop down
                    loop_thread.join(timeout=5)
                    stdout.close()
                    reader_done.wait(timeout=5)
                    stdin.close()

    def test_orchestrator_cooperative_cancel_stops_execution(self) -> None:
        """A cancelled task must not execute tools when execute_task is called."""
        from harness_core.core.models import TaskSubmitRequest
        from harness_core.core.orchestrator import HarnessOrchestrator

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "ws"
            ws.mkdir()
            with patch.dict(os.environ, {"HEX_TESTING": "1"}):
                orch = HarnessOrchestrator(data_dir=root / "data", default_workspace=ws)
                req = TaskSubmitRequest(
                    goal="list the files",
                    execution_mode="fast_path",
                    workspace_root=str(ws),
                    constraints={
                        "requested_steps": [
                            {"step_id": "s1", "tool_id": "list_files", "args": {}, "risk_class": "R0"}
                        ]
                    },
                )
                tid = orch.submit_task(req)["task_id"]
                orch.cancel_task(tid)
                res = orch.execute_task(tid)
                self.assertEqual(res["status"], "CANCELED")
                self.assertEqual(orch.ledger.list_attempts(tid), [])

    def test_fast_path_prompt_includes_conversation_history(self) -> None:
        from harness_core.core.models import TaskSubmitRequest
        from harness_core.core.orchestrator import HarnessOrchestrator

        class StubRouter:
            cloud_provider = None

            def __init__(self) -> None:
                self.prompts: list[str] = []

            def execute_routed_request(self, prompt, **kwargs):
                self.prompts.append(prompt)
                return None, SimpleNamespace(text="stub answer")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "ws"
            ws.mkdir()
            with patch.dict(os.environ, {"HEX_TESTING": "1"}):
                orch = HarnessOrchestrator(data_dir=root / "data", default_workspace=ws)
                stub = StubRouter()
                orch.router = stub
                req = TaskSubmitRequest(
                    goal="continue please",
                    execution_mode="fast_path",
                    workspace_root=str(ws),
                    constraints={
                        "surface": "acp",
                        "conversation_history": [
                            {"role": "user", "content": "earlier question"},
                            {"role": "assistant", "content": "earlier answer"},
                        ],
                    },
                )
                tid = orch.submit_task(req)["task_id"]
                res = orch.execute_task(tid)
                self.assertEqual(res["status"], "SUCCEEDED")
                self.assertEqual(len(stub.prompts), 1)
                self.assertIn("Conversation so far:", stub.prompts[0])
                self.assertIn("earlier question", stub.prompts[0])
                self.assertIn("earlier answer", stub.prompts[0])


class TestStreamRetentionAndTerminalOrdering(unittest.TestCase):
    def test_streaming_does_not_retain_five_megabytes_of_chunks(self) -> None:
        import tracemalloc
        from harness_core.client.acp import ACPSession

        class StreamingOrchestrator(RecordingOrchestrator):
            peak = 0
            def execute_task(inner, task_id, stream_callback=None, tool_callback=None):
                tracemalloc.start()
                try:
                    for i in range(160):
                        stream_callback(str(i) + "x" * 32768)
                    _, inner.peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
                return {"status": "SUCCEEDED", "result": "final answer"}

        with tempfile.TemporaryDirectory() as tmp:
            orch = StreamingOrchestrator()
            server = ACPServer(orchestrator=orch, workspace_root=tmp)
            sess = ACPSession("stream", Path(tmp))
            updates = []
            server._emit = lambda obj: updates.append((obj["params"]["updateType"], "delta" in obj["params"]))
            result = server._execute_prompt_turn(sess, {}, "stream")
            self.assertLess(orch.peak, 512 * 1024)
            self.assertEqual(updates[-2:], [("stream_end", False), ("status", False)])
            self.assertEqual(result["content"][0]["text"], "final answer")

    def test_empty_stream_and_cancellation_keep_terminal_behavior(self) -> None:
        from harness_core.client.acp import ACPSession

        for chunks, cancel_at, expected in (
            (["", ""], None, ["status"]),
            (["a", "", "b"], None, ["agent_response", "agent_response", "stream_end", "status"]),
            (["a", "b"], 0, ["status"]),
            (["a", "b"], 1, ["agent_response", "status"]),
        ):
            with self.subTest(chunks=chunks, cancel_at=cancel_at), tempfile.TemporaryDirectory() as tmp:
                sess = ACPSession("stream", Path(tmp))
                class ScriptedOrchestrator(RecordingOrchestrator):
                    def execute_task(inner, task_id, stream_callback=None, tool_callback=None):
                        for i, chunk in enumerate(chunks):
                            if i == cancel_at:
                                sess.cancelled = True
                            stream_callback(chunk)
                            if sess.cancelled:
                                tool_callback("complete", "terminal", {"stdout": "suppressed"})
                        return {"status": "SUCCEEDED", "result": "final answer"}
                server = ACPServer(orchestrator=ScriptedOrchestrator(), workspace_root=tmp)
                messages = []
                server._emit = messages.append
                result = server._execute_prompt_turn(sess, {}, "stream")
                updates = [m["params"] for m in messages]
                self.assertEqual([u["updateType"] for u in updates], expected)
                if cancel_at is not None:
                    self.assertEqual(result["status"], "cancelled")
                    self.assertEqual(updates[-1]["status"], "Cancelled")
                    self.assertNotIn("delta", updates[-1])
                elif not any(chunks):
                    self.assertEqual(updates[-1]["delta"], "final answer")
                else:
                    self.assertNotIn("delta", updates[-1])


if __name__ == "__main__":
    unittest.main()