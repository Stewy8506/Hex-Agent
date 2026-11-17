"""F04 wire-contract tests: the real ACP server must emit the shared fixture shapes.

The JSONL fixtures in tests/fixtures/acp_wire/ are captured from the actual
Python server (scripted orchestrator, no live provider calls) and are also
consumed by the Rust client tests in crates/hex-desktop and crates/hex-tui,
so both sides of the wire are proven against one contract.

Regenerate fixtures after an intentional protocol change:
    python -m tests.client.test_acp_wire_contract --regenerate
"""

from __future__ import annotations

import io
import json
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from harness_core.client.acp import ACPServer

from tests import REPO_ROOT
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "acp_wire"


class WireFakeOrchestrator:
    """Scripted orchestrator driving deterministic stream/tool callbacks."""

    def __init__(self, *, stream: bool, tools: bool) -> None:
        self.stream = stream
        self.tools = tools
        self.ledger = None

    def submit_task(self, req):
        return {"task_id": "tsk_fixture"}

    def execute_task(self, task_id, stream_callback=None, tool_callback=None):
        if self.stream and stream_callback:
            stream_callback("Hello")
            stream_callback(" world")
        if self.tools and tool_callback:
            tool_callback("start", "list_files", {"path": "."})
            tool_callback("complete", "list_files", {"files": ["a.py"]}, 12)
        return {"status": "SUCCEEDED", "payload": {"result": "Fixture answer."}}


def capture_turn(*, stream: bool, tools: bool) -> list[str]:
    """Run a real prompt turn against the server and capture raw wire lines."""
    with tempfile.TemporaryDirectory() as tmp:
        server = ACPServer(
            orchestrator=WireFakeOrchestrator(stream=stream, tools=tools),
            workspace_root=tmp,
        )
        new_resp = server.handle_request(
            {"jsonrpc": "2.0", "id": 0, "method": "session/new", "params": {"cwd": tmp}}
        )
        session_id = new_resp["result"]["sessionId"]
        buf = io.StringIO()
        with redirect_stdout(buf):
            resp = server.handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "session/prompt",
                    "params": {"sessionId": session_id, "prompt": "fixture prompt"},
                }
            )
        lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
        lines.append(json.dumps(resp, ensure_ascii=False))
        return lines


def normalize(lines: list[str]) -> list[str]:
    """Replace nondeterministic IDs so captures compare against fixtures."""
    out = []
    for ln in lines:
        ln = re.sub(r'"sessionId": "acp_sess_[0-9a-f]+"', '"sessionId": "ses_fixture"', ln)
        ln = re.sub(r'"taskId": "tsk_acp_[0-9a-f]+"', '"taskId": "tsk_fixture"', ln)
        ln = re.sub(r'"callId": "list_files_\d+"', '"callId": "list_files_<ts>"', ln)
        out.append(ln)
    return out


def render(lines: list[str]) -> str:
    # Re-serialize with sorted keys so fixture comparison is key-order stable.
    return "\n".join(json.dumps(json.loads(ln), sort_keys=True, ensure_ascii=False) for ln in lines) + "\n"


class TestAcpWireContract(unittest.TestCase):
    def _assert_fixture(self, name: str, *, stream: bool, tools: bool) -> None:
        captured = normalize(capture_turn(stream=stream, tools=tools))
        fixture = FIXTURE_DIR / name
        self.assertTrue(fixture.is_file(), f"missing fixture {fixture}")
        self.assertEqual(render(captured), fixture.read_text(encoding="utf-8"))

    def test_streamed_turn_matches_fixture(self) -> None:
        self._assert_fixture("turn_streamed.jsonl", stream=True, tools=True)

    def test_nonstreamed_turn_matches_fixture(self) -> None:
        self._assert_fixture("turn_nonstreamed.jsonl", stream=False, tools=False)

    def test_streamed_turn_event_order(self) -> None:
        lines = [json.loads(ln) for ln in capture_turn(stream=True, tools=True)]
        updates = [ln["params"] for ln in lines[:-1] if ln.get("method") == "session/update"]
        types = [u["updateType"] for u in updates]
        self.assertEqual(
            types,
            ["agent_response", "agent_response", "tool", "tool", "stream_end", "status"],
        )
        tool_start, tool_end = updates[2]["tool"], updates[3]["tool"]
        self.assertEqual(tool_start["eventType"], "start")
        self.assertEqual(tool_end["eventType"], "complete")
        self.assertEqual(tool_start["callId"], tool_end["callId"])
        self.assertEqual(tool_end["durationMs"], 12)
        self.assertEqual(updates[4]["status"], "Ready")
        self.assertEqual(updates[5]["status"], "Ready")
        final = lines[-1]
        self.assertEqual(final["id"], 1)
        self.assertEqual(final["result"]["status"], "completed")
        self.assertEqual(final["result"]["content"][0]["text"], "Fixture answer.")
        self.assertTrue(final["result"]["taskId"])

    def test_nonstreamed_turn_carries_answer_in_status_delta(self) -> None:
        lines = [json.loads(ln) for ln in capture_turn(stream=False, tools=False)]
        updates = [ln["params"] for ln in lines[:-1] if ln.get("method") == "session/update"]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["updateType"], "status")
        self.assertEqual(updates[0]["status"], "Ready")
        self.assertEqual(updates[0]["delta"], "Fixture answer.")

    def test_cancel_response_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = ACPServer(orchestrator=WireFakeOrchestrator(stream=False, tools=False), workspace_root=tmp)
            new_resp = server.handle_request(
                {"jsonrpc": "2.0", "id": 0, "method": "session/new", "params": {"cwd": tmp}}
            )
            sid = new_resp["result"]["sessionId"]
            resp = server.handle_request(
                {"jsonrpc": "2.0", "id": 2, "method": "session/cancel", "params": {"sessionId": sid}}
            )
            self.assertEqual(resp["result"], {"sessionId": sid, "cancelled": True})


if __name__ == "__main__":
    if "--regenerate" in sys.argv:
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        (FIXTURE_DIR / "turn_streamed.jsonl").write_text(
            render(normalize(capture_turn(stream=True, tools=True))), encoding="utf-8"
        )
        (FIXTURE_DIR / "turn_nonstreamed.jsonl").write_text(
            render(normalize(capture_turn(stream=False, tools=False))), encoding="utf-8"
        )
        print(f"Fixtures regenerated in {FIXTURE_DIR}")
    else:
        unittest.main()