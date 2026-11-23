"""DX2 session-management ACP methods: rename, delete, archive, pin.

Each operation mutates the durable session metadata under
<workspace>/.hex/data/acp_sessions/<id>.json so the desktop sidebar state
survives restarts; delete additionally removes the SQLite transcript rows.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness_core.client.acp import ACPServer


class RecordingOrchestrator:
    """Scripted orchestrator enough to drive a completed prompt turn."""

    def __init__(self) -> None:
        self.requests = []

    def submit_task(self, req):
        self.requests.append(req)
        return {"task_id": f"tsk_{len(self.requests)}"}

    def execute_task(self, task_id, stream_callback=None, tool_callback=None):
        goal = self.requests[-1].goal if self.requests else task_id
        return {"status": "SUCCEEDED", "payload": {"result": f"answer to: {goal}"}}

    def cancel_task(self, task_id, actor="owner"):
        return {"status": "CANCELED"}


def _req(id_, method, params):
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params}


class TestSessionManagement(unittest.TestCase):
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

    def _list_entries(self, server=None) -> list[dict]:
        resp = (server or self.server).handle_request(_req(2, "session/list", {}))
        return resp["result"]["sessions"]

    def _meta(self, sid: str, server=None) -> dict:
        return next(s for s in self._list_entries(server) if s["sessionId"] == sid)

    def test_rename_persists_title_across_restart(self) -> None:
        sid = self._new_session()
        self.assertEqual(self._meta(sid)["title"], "")

        resp = self.server.handle_request(
            _req(3, "session/rename", {"sessionId": sid, "title": "  Ops runbook  "})
        )
        self.assertEqual(resp["result"], {"sessionId": sid, "title": "Ops runbook"})
        self.assertEqual(self._meta(sid)["title"], "Ops runbook")

        orch2 = RecordingOrchestrator()
        server2 = ACPServer(orchestrator=orch2, workspace_root=self.ws)
        self.assertEqual(self._meta(sid, server2)["title"], "Ops runbook")

    def test_rename_rejects_blank_titles_and_unknown_sessions(self) -> None:
        sid = self._new_session()
        for bad in ("", "   "):
            resp = self.server.handle_request(
                _req(5, "session/rename", {"sessionId": sid, "title": bad})
            )
            self.assertIn("error", resp)
        # The blank attempts left the original (empty) title untouched.
        self.assertEqual(self._meta(sid)["title"], "")

        resp = self.server.handle_request(
            _req(6, "session/rename", {"sessionId": "acp_sess_missing", "title": "Nope"})
        )
        self.assertIn("error", resp)

    def test_delete_removes_metadata_and_history(self) -> None:
        sid = self._new_session()
        prompt = self.server.handle_request(
            _req(2, "session/prompt", {"sessionId": sid, "prompt": "history row"})
        )
        self.assertEqual(prompt["result"]["status"], "completed")
        path = self.server._session_path(sid)
        self.assertIsNotNone(path)
        self.assertTrue(path.is_file())

        resp = self.server.handle_request(_req(3, "session/delete", {"sessionId": sid}))
        self.assertEqual(resp["result"], {"sessionId": sid, "deleted": True})
        self.assertFalse(path.is_file())
        self.assertNotIn(sid, [s["sessionId"] for s in self._list_entries()])

        load = self.server.handle_request(_req(4, "session/load", {"sessionId": sid}))
        self.assertIn("error", load)

        # A restarted server cannot resurrect the deleted session either.
        orch2 = RecordingOrchestrator()
        server2 = ACPServer(orchestrator=orch2, workspace_root=self.ws)
        self.assertNotIn(sid, [s["sessionId"] for s in self._list_entries(server2)])

    def test_delete_unknown_session_is_an_error(self) -> None:
        resp = self.server.handle_request(
            _req(3, "session/delete", {"sessionId": "acp_sess_missing"})
        )
        self.assertIn("error", resp)

    def test_archive_and_pin_round_trip(self) -> None:
        sid = self._new_session()
        archive = self.server.handle_request(
            _req(3, "session/archive", {"sessionId": sid, "archived": True})
        )
        self.assertEqual(
            archive["result"], {"sessionId": sid, "archived": True, "pinned": False}
        )
        self.assertTrue(self._meta(sid)["archived"])

        unarchive = self.server.handle_request(
            _req(4, "session/archive", {"sessionId": sid, "archived": False})
        )
        self.assertFalse(unarchive["result"]["archived"])

        pin = self.server.handle_request(
            _req(5, "session/pin", {"sessionId": sid, "pinned": True})
        )
        self.assertEqual(
            pin["result"], {"sessionId": sid, "archived": False, "pinned": True}
        )
        self.assertTrue(self._meta(sid)["pinned"])

        # Flags survive a restart.
        orch2 = RecordingOrchestrator()
        server2 = ACPServer(orchestrator=orch2, workspace_root=self.ws)
        entry = self._meta(sid, server2)
        self.assertTrue(entry["pinned"])
        self.assertFalse(entry["archived"])

    def test_archive_defaults_to_true_without_explicit_param(self) -> None:
        sid = self._new_session()
        resp = self.server.handle_request(_req(2, "session/archive", {"sessionId": sid}))
        self.assertTrue(resp["result"]["archived"])

        resp = self.server.handle_request(_req(3, "session/pin", {"sessionId": sid}))
        self.assertTrue(resp["result"]["pinned"])


if __name__ == "__main__":
    unittest.main()
