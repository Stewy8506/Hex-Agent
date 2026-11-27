"""Personal sync CLI and ACP control-plane regressions."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from cloud.p2p_transport import PeerIdentity
from cloud.session import CloudAccountError
from cloud.sync_control import SyncControl
from cloud.sync_engine import SyncResult
from harness_core.client.acp import ACPServer
from harness_core.client.cli import build_parser
from harness_core.client.commands.sync import run_sync_command


class FakeAccount:
    def __init__(self, *, signed_in: bool = True) -> None:
        self.signed_in = signed_in
        self.identity = PeerIdentity.generate("dev_local")
        self.base_url = "http://127.0.0.1:8787"

    def device_record_id(self) -> str | None:
        return self.identity.device_id if self.signed_in else None

    def status(self) -> dict:
        return {
            "logged_in": self.signed_in,
            "device_record_id": self.device_record_id(),
        }

    def access_token(self) -> str | None:
        return "test-access-token" if self.signed_in else None

    def peer_identity(self) -> PeerIdentity:
        if not self.signed_in:
            raise CloudAccountError("Not signed in to HEX cloud.")
        return self.identity


class SyncControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="hex_sync_control_")
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_status_reports_persistent_cursor_without_credentials(self) -> None:
        account = FakeAccount()
        with SyncControl(
            self.workspace,
            data_dir=self.root / "data",
            global_home=self.root / "home",
            account=account,
        ) as control:
            state = control.status()
            state["peers"]["dev_remote"] = {
                "ledger": {"task-a": 12},
                "cognition": {"revision": 4, "manifest_sha256": "a" * 64},
            }
            control._engine()._advance_ledger_cursor("dev_remote", "task-a", 12)
            reloaded = control.status()
        self.assertEqual(reloaded["peers"]["dev_remote"]["ledger"], {"task-a": 12})
        self.assertEqual(reloaded["capabilities"], ["ledger", "cognition"])
        self.assertNotIn("test-access-token", json.dumps(reloaded))
        self.assertNotIn("private_key", json.dumps(reloaded))

    def test_run_requires_authenticated_enrolled_identity(self) -> None:
        account = FakeAccount(signed_in=False)
        with SyncControl(
            self.workspace,
            data_dir=self.root / "data",
            global_home=self.root / "home",
            account=account,
        ) as control:
            with self.assertRaisesRegex(CloudAccountError, "Not signed in"):
                asyncio.run(control.run_websocket(
                    "ws://127.0.0.1:1234", "dev_remote", "ed25519:" + "ab" * 32
                ))

    def test_run_pins_peer_and_closes_session(self) -> None:
        account = FakeAccount()
        session = mock.Mock()
        session.close = mock.AsyncMock()
        connector = mock.AsyncMock(return_value=session)
        engine = mock.Mock()
        engine.sync_with = mock.AsyncMock(return_value=SyncResult("dev_remote"))
        peer_key = "ed25519:" + "cd" * 32
        with SyncControl(
            self.workspace,
            data_dir=self.root / "data",
            global_home=self.root / "home",
            account=account,
        ) as control:
            with mock.patch("cloud.sync_control.connect_websocket_peer", connector), \
                 mock.patch.object(control, "_engine", return_value=engine):
                result = asyncio.run(control.run_websocket(
                    "ws://127.0.0.1:1234", "dev_remote", peer_key
                ))
        self.assertEqual(result["remote_device_id"], "dev_remote")
        kwargs = connector.await_args.kwargs
        self.assertEqual(kwargs["expected_peer"].device_id, "dev_remote")
        self.assertEqual(kwargs["expected_peer"].signing_public_key, peer_key)
        session.close.assert_awaited_once()

    def test_run_signaled_pins_peer_and_uses_broker_operation(self) -> None:
        account = FakeAccount()
        session = mock.Mock()
        engine = mock.Mock()
        engine.sync_with = mock.AsyncMock(return_value=SyncResult("dev_remote"))
        broker = mock.Mock()
        broker.device_id = "dev_local"
        peer_key = "ed25519:" + "cd" * 32

        def run(peer: Any, operation: Any, *, transport: str, timeout: float) -> dict:
            self.assertEqual(peer.device_id, "dev_remote")
            self.assertEqual(peer.signing_public_key, peer_key)
            self.assertEqual(transport, "auto")
            self.assertEqual(timeout, 120.0)
            return asyncio.run(operation(session))

        broker.run_with_peer.side_effect = run
        with SyncControl(
            self.workspace,
            data_dir=self.root / "data",
            global_home=self.root / "home",
            account=account,
        ) as control:
            with mock.patch.object(control, "_engine", return_value=engine):
                result = control.run_signaled(broker, "dev_remote", peer_key)
        self.assertEqual(result["transport"], "signaled-auto")
        self.assertEqual(result["remote_device_id"], "dev_remote")
        engine.sync_with.assert_awaited_once_with(session)

    def test_cli_status_and_run_argument_validation(self) -> None:
        args = build_parser(str(self.root / "default-data")).parse_args([
            "sync", "run", "wss://peer.example", "--peer-id", "dev_remote",
            "--peer-key", "ed25519:" + "ab" * 32,
            "--workspace", str(self.workspace), "--json",
        ])
        self.assertEqual(args.sync_cmd, "run")
        self.assertEqual(args.url, "wss://peer.example")
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = run_sync_command(argparse.Namespace(
                sync_cmd="run", url="ws://127.0.0.1:1", peer_id=None,
                peer_key=None, workspace=str(self.workspace),
                data_dir=str(self.root / "data"), config_dir=str(self.root / "home"),
                base_url=None, timeout=1, json=True,
            ))
        self.assertEqual(code, 1)
        self.assertIn("--peer-id", errors.getvalue())

    def test_acp_status_and_run_emit_sync_event(self) -> None:
        account = FakeAccount()
        server = ACPServer(workspace_root=self.workspace, config_dir=self.root / "home")
        server._cloud_account = account
        events: list[dict] = []
        server._emit = events.append
        params = {"workspace": str(self.workspace), "data_dir": str(self.root / "data")}
        response = server.handle_request({
            "id": 1, "method": "cloud/sync/status", "params": params,
        })
        self.assertEqual(response["result"]["device_id"], "dev_local")
        control = mock.Mock()
        control.__enter__ = mock.Mock(return_value=control)
        control.__exit__ = mock.Mock(return_value=None)
        control.run_websocket = mock.AsyncMock(return_value={
            "transport": "websocket", **SyncResult("dev_remote").to_dict(),
        })
        with mock.patch.object(server, "_sync_control", return_value=control):
            response = server.handle_request({
                "id": 2, "method": "cloud/sync/run",
                "params": {
                    **params, "url": "ws://127.0.0.1:1234", "peer_id": "dev_remote",
                    "peer_signing_public_key": "ed25519:" + "ab" * 32,
                },
            })
        self.assertEqual(response["result"]["remote_device_id"], "dev_remote")
        self.assertEqual(events[-1]["method"], "cloud/event")
        self.assertEqual(events[-1]["params"]["kind"], "sync_completed")
        self.assertNotIn("test-access-token", json.dumps(events))


    def test_acp_signaled_run_uses_persistent_broker(self) -> None:
        account = FakeAccount()
        server = ACPServer(workspace_root=self.workspace, config_dir=self.root / "home")
        server._cloud_account = account
        events: list[dict] = []
        server._emit = events.append
        broker = mock.Mock()
        control = mock.Mock()
        control.__enter__ = mock.Mock(return_value=control)
        control.__exit__ = mock.Mock(return_value=None)
        control.run_signaled.return_value = {
            "transport": "signaled-relay", **SyncResult("dev_remote").to_dict(),
        }
        params = {"workspace": str(self.workspace), "data_dir": str(self.root / "data")}
        with mock.patch.object(server, "_signal_start", return_value={}) as start, \
             mock.patch.object(server, "_ensure_transport_broker", return_value=broker), \
             mock.patch.object(server, "_sync_control", return_value=control):
            response = server.handle_request({
                "id": 3, "method": "cloud/sync/run",
                "params": {
                    **params, "transport": "relay", "peer_id": "dev_remote",
                    "peer_signing_public_key": "ed25519:" + "ab" * 32,
                },
            })
        self.assertEqual(response["result"]["transport"], "signaled-relay")
        start.assert_called_once_with()
        self.assertEqual(control.run_signaled.call_args.kwargs["transport"], "relay")
        self.assertEqual(events[-1]["params"]["kind"], "sync_completed")

    def test_acp_signal_lifecycle_starts_and_stops_transport_broker(self) -> None:
        account = FakeAccount()
        server = ACPServer(workspace_root=self.workspace, config_dir=self.root / "home")
        server._cloud_account = account
        link = mock.Mock()
        link.status.return_value = {
            "running": True, "connected": True, "attempts": 0,
            "last_error": None, "base_url": account.base_url,
        }
        broker = mock.Mock()
        broker.status.return_value = {
            "running": True, "local_device_id": "dev_local", "active_tunnels": [],
        }
        with mock.patch("cloud.signaling.SignalLink", return_value=link), \
             mock.patch.object(server, "_new_transport_broker", return_value=broker):
            status = server._signal_start()
            self.assertTrue(status["transport_broker"]["running"])
            link.start.assert_called_once_with()
            broker.start.assert_called_once_with()
            stopped = server._signal_stop()
        self.assertFalse(stopped["running"])
        broker.stop.assert_called_once_with()
        link.stop.assert_called_once_with()
        self.assertIsNone(server._signal_link)
        self.assertIsNone(server._transport_broker)
        self.assertEqual(server._default_relay_uri(), "ws://127.0.0.1:8787/hex/relay/v1")


if __name__ == "__main__":
    unittest.main()
