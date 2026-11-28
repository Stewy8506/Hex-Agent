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
    team_id = "team_1"

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()
        self.stopped = False
        self.running = True
        self.start_count = 0
        self.flushed = False
        self.turns = []

    async def start(self):
        self.start_count += 1
        self.running = True
        return self.status()

    async def stop(self):
        self.stopped = True
        self.running = False

    async def flush(self):
        self.flushed = True
        return {"delivered": 0, "queued": 0}

    async def disconnect(self, device_id):
        return {"disconnected": True, "device_record_id": device_id}

    def __call__(self, turn):
        self.turns.append(turn)
        return {"status": "queued"}

    def status(self):
        return {"running": self.running, "project_session_id": self.project_session_id}


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
        self.runtime = _Runtime(Path(self.tmp.name))
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
        self.assertEqual(self.orchestrator.team_turn_hook, self.server._dispatch_team_turn)
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

    def test_start_recovers_an_existing_failed_closed_runtime(self) -> None:
        self.runtime.running = False
        self.server._team_runtimes = {self.runtime.project_session_id: self.runtime}
        self.server._signal_start = lambda: {"running": True}
        self.server._ensure_transport_broker = lambda: self.broker

        self.assertFalse(self.server._team_status({})["running"])
        with self.assertRaisesRegex(ValueError, "not running"):
            self.server._team_prompt_session(
                {"project_session_id": self.runtime.project_session_id}, Path(self.tmp.name)
            )
        result = self.server._team_start({
            "team_id": "team_1", "project_session_id": "proj_team"
        })

        self.assertTrue(result["running"])
        self.assertEqual(self.runtime.start_count, 1)
        with self.assertRaisesRegex(ValueError, "different team"):
            self.server._team_start({
                "team_id": "team_other", "project_session_id": "proj_team"
            })

    def test_turn_hook_routes_by_task_session_and_rejects_ambiguity(self) -> None:
        other = _Runtime(Path(self.tmp.name))
        other.project_session_id = "proj_other"
        self.server._team_runtimes = {
            self.runtime.project_session_id: self.runtime,
            other.project_session_id: other,
        }
        self.server._select_team_hook()
        self.orchestrator.ledger = SimpleNamespace(
            get_task=lambda _task_id: {
                "constraints": {
                    "team_project_session_id": other.project_session_id,
                    "workspace_root": self.tmp.name,
                }
            }
        )
        turn = SimpleNamespace(task_id="task_1")

        result = self.orchestrator.team_turn_hook(turn)

        self.assertEqual(result, {"status": "queued"})
        self.assertEqual(other.turns, [turn])
        self.assertEqual(self.runtime.turns, [])
        self.orchestrator.ledger.get_task = lambda _task_id: {
            "constraints": {"workspace_root": self.tmp.name}
        }
        with self.assertRaisesRegex(ValueError, "required"):
            self.orchestrator.team_turn_hook(turn)
        self.orchestrator.ledger.get_task = lambda _task_id: {
            "constraints": {
                "team_project_session_id": other.project_session_id,
                "workspace_root": str(Path(self.tmp.name) / "other"),
            }
        }
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.orchestrator.team_turn_hook(turn)

    def test_prompt_session_must_match_a_running_team_workspace(self) -> None:
        self.server._team_runtimes = {self.runtime.project_session_id: self.runtime}
        workspace = Path(self.tmp.name)

        self.assertIsNone(self.server._team_prompt_session({}, workspace))
        self.assertEqual(
            self.server._team_prompt_session(
                {"project_session_id": self.runtime.project_session_id}, workspace
            ),
            self.runtime.project_session_id,
        )
        with self.assertRaisesRegex(ValueError, "non-empty"):
            self.server._team_prompt_session({"project_session_id": ""}, workspace)
        with self.assertRaisesRegex(ValueError, "different workspace"):
            self.server._team_prompt_session(
                {"project_session_id": self.runtime.project_session_id},
                workspace / "other",
            )

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
