"""ACP Teams lifecycle and event contract."""

from __future__ import annotations

import concurrent.futures
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hex_cli.acp import ACPServer, _SUPPORTED_METHODS


class _Runtime:
    project_session_id = "proj_team"

    def __init__(self) -> None:
        self.stopped = False
        self.flushed = False

    async def start(self):
        return self.status()

    async def stop(self):
        self.stopped = True

    async def flush(self):
        self.flushed = True
        return {"delivered": 0, "queued": 0}

    async def disconnect(self, device_id):
        return {"disconnected": True, "device_record_id": device_id}

    def status(self):
        return {"running": not self.stopped, "project_session_id": self.project_session_id}


class _Broker:
    def submit(self, factory):
        future = concurrent.futures.Future()
        try:
            future.set_result(asyncio.run(factory()))
        except BaseException as exc:
            future.set_exception(exc)
        return future


class ACPTeamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.orchestrator = SimpleNamespace(ledger=object(), team_turn_hook=None)
        self.server = ACPServer(orchestrator=self.orchestrator, workspace_root=self.tmp.name)
        self.runtime = _Runtime()
        self.broker = _Broker()

    def tearDown(self) -> None:
        self.server._team_stop({})
        self.tmp.cleanup()

    def test_start_status_flush_disconnect_and_stop(self) -> None:
        self.server._signal_start = lambda: {"running": True}
        self.server._ensure_transport_broker = lambda: self.broker
        with patch("cloud.team_runtime.TeamRuntime.create", new=AsyncMock(return_value=self.runtime)):
            result = self.server.handle_request({
                "jsonrpc": "2.0", "id": 1, "method": "team/start",
                "params": {"team_id": "team_1", "project_session_id": "proj_team"},
            })
        self.assertEqual(result["result"]["running"], True)
        self.assertIs(self.orchestrator.team_turn_hook, self.runtime)
        self.assertIn("team/start", _SUPPORTED_METHODS)
        self.assertEqual(self.server._team_flush({"project_session_id": "proj_team"}),
                         {"delivered": 0, "queued": 0})
        self.assertTrue(self.runtime.flushed)
        self.server._transport_broker = self.broker
        self.assertTrue(self.server._team_disconnect({
            "project_session_id": "proj_team", "device_record_id": "dev_peer"
        })["disconnected"])
        self.assertEqual(self.server._team_stop({"project_session_id": "proj_team"}), {"stopped": 1})
        self.assertTrue(self.runtime.stopped)
        self.assertIsNone(self.orchestrator.team_turn_hook)

    def test_runtime_events_are_public_cloud_events(self) -> None:
        captured = []
        self.server._emit = captured.append
        callback = None

        async def create(**kwargs):
            nonlocal callback
            callback = kwargs["on_event"]
            return self.runtime

        self.server._signal_start = lambda: {"running": True}
        self.server._ensure_transport_broker = lambda: self.broker
        with patch("cloud.team_runtime.TeamRuntime.create", new=create):
            self.server._team_start({"team_id": "team_1", "project_session_id": "proj_team"})
        callback({"kind": "team/turn_committed", "turn_seq": 1, "project_session_id": "proj_team"})
        self.assertEqual(captured, [{
            "jsonrpc": "2.0", "method": "cloud/event",
            "params": {"kind": "team/turn_committed", "project_session_id": "proj_team", "turn_seq": 1},
        }])


if __name__ == "__main__":
    unittest.main()
