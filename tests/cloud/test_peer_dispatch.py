"""First-frame protocol dispatch must preserve each protocol session."""

from __future__ import annotations

import unittest

from cloud.peer_dispatch import PeerDispatchError, dispatch_peer_session, read_hello


class _Session:
    def __init__(self, frames: list[bytes | None]) -> None:
        self.frames = list(frames)
        self.remote_device_id = "dev_peer"

    async def receive(self) -> bytes | None:
        return self.frames.pop(0) if self.frames else None


class PeerDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_team_hello_is_replayed_without_losing_following_frame(self) -> None:
        import json

        hello = json.dumps({
            "protocol_version": 1, "type": "team_hello", "device_id": "dev_peer",
            "role": "coordinator", "project_session_id": "proj_team",
            "host_epoch": 1, "committed_turn_seq": 0, "applied_turn_seq": 0,
        }).encode()
        session = _Session([hello, b"next"])
        received = []

        async def teams(session_id, replay, peer):
            received.append((session_id, await replay.receive(), await replay.receive()))
            return "team"

        result = await dispatch_peer_session(session, object(), None, teams)
        self.assertEqual(result, "team")
        self.assertEqual(received, [("proj_team", hello, b"next")])

    async def test_sync_hello_routes_to_personal_sync(self) -> None:
        import json

        hello = json.dumps({
            "protocol_version": 1, "type": "sync_hello", "device_id": "dev_peer",
            "workspace_id": "proj_personal", "capabilities": [],
        }).encode()
        session = _Session([hello])

        async def sync(replay, peer):
            return (await replay.receive(), replay.remote_device_id)

        result = await dispatch_peer_session(session, object(), sync, None)
        self.assertEqual(result, (hello, "dev_peer"))

    async def test_unknown_and_duplicate_hellos_are_rejected(self) -> None:
        with self.assertRaises(PeerDispatchError):
            read_hello(b'{"protocol_version":1,"type":"unknown"}')
        with self.assertRaises(PeerDispatchError):
            read_hello(b'{"protocol_version":1,"protocol_version":1,"type":"sync_hello",'
                       b'"device_id":"d","workspace_id":"w","capabilities":[]}')


if __name__ == "__main__":
    unittest.main()
