"""Two-instance sync engine regressions over loopback, WebSocket, and QUIC transports."""

from __future__ import annotations

import asyncio
import datetime
import ipaddress
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from cloud.cognition_sync import CognitionSyncStore
from cloud.p2p_transport import (
    InMemoryNetwork,
    PeerIdentity,
    QuicPeerServer,
    WebSocketPeerServer,
    connect_quic_peer,
    connect_websocket_peer,
)
from cloud.sync_engine import SyncEngine, SyncEngineError, SyncResult
from harness_core.storage.db import LedgerStore


def _write_quic_certificate(root: Path) -> tuple[Path, Path]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            ]),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )
    key_path = root / "quic-key.pem"
    certificate_path = root / "quic-cert.pem"
    key_path.write_bytes(private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return key_path, certificate_path


class SyncEngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="hex_sync_engine_")
        self.root = Path(self.temporary.name)
        self.workspaces: dict[str, Path] = {}
        self.homes: dict[str, Path] = {}
        self.ledgers: dict[str, LedgerStore] = {}
        self.cognition: dict[str, CognitionSyncStore] = {}
        self.engines: dict[str, SyncEngine] = {}
        for device_id in ("dev_alice", "dev_bob"):
            workspace = self.root / device_id / "workspace"
            home = self.root / device_id / "home"
            workspace.mkdir(parents=True)
            home.mkdir()
            ledger = LedgerStore(home / "ledger.db")
            cognition = CognitionSyncStore(
                workspace,
                global_home=home,
                workspace_id="workspace-a",
                ledger=ledger,
            )
            self.workspaces[device_id] = workspace
            self.homes[device_id] = home
            self.ledgers[device_id] = ledger
            self.cognition[device_id] = cognition
            self.engines[device_id] = SyncEngine(
                ledger,
                cognition,
                device_id,
                state_path=home / ".sync" / "engine" / "workspace-a.json",
                page_size=1,
                timeout=10,
            )

    def tearDown(self) -> None:
        for ledger in self.ledgers.values():
            ledger.close()
        self.temporary.cleanup()

    def _write_document(self, device_id: str, document_id: str, content: str) -> Path:
        store = self.cognition[device_id]
        path, _root = store._document_path(document_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def _write_skill(self, device_id: str, name: str, body: str) -> Path:
        bundle = self.homes[device_id] / "skills" / name
        bundle.mkdir(parents=True, exist_ok=True)
        (bundle / "SKILL.md").write_text(
            "---\n"
            f"name: {name}\n"
            "description: Perform the documented workflow.\n"
            "purpose: Perform the documented workflow.\n"
            "risk_class: R1\n"
            "---\n"
            f"# {name}\n\n{body}\n",
            encoding="utf-8",
        )
        return bundle

    async def _sync(
        self, *, connector: str = "dev_alice"
    ) -> dict[str, SyncResult | BaseException]:
        network = InMemoryNetwork()
        alice = network.endpoint(PeerIdentity.generate("dev_alice"))
        bob = network.endpoint(PeerIdentity.generate("dev_bob"))
        if connector == "dev_alice":
            alice_session = await alice.connect(bob)
            bob_session = await bob.accept()
        else:
            bob_session = await bob.connect(alice)
            alice_session = await alice.accept()
        try:
            alice_result, bob_result = await asyncio.gather(
                self.engines["dev_alice"].sync_with(alice_session),
                self.engines["dev_bob"].sync_with(bob_session),
                return_exceptions=True,
            )
        finally:
            await asyncio.gather(
                alice_session.close(), bob_session.close(), return_exceptions=True
            )
        return {"dev_alice": alice_result, "dev_bob": bob_result}

    async def _sync_websocket(
        self, *, connector: str
    ) -> dict[str, SyncResult | BaseException]:
        alice_identity = PeerIdentity.generate("dev_alice")
        bob_identity = PeerIdentity.generate("dev_bob")
        if connector == "dev_alice":
            server_identity = bob_identity
            client_identity = alice_identity
        else:
            server_identity = alice_identity
            client_identity = bob_identity
        server = WebSocketPeerServer(server_identity)
        await server.start()
        try:
            client_task = asyncio.create_task(connect_websocket_peer(
                server.uri,
                client_identity,
                expected_peer=server_identity.address,
            ))
            server_session, client_session = await asyncio.wait_for(
                asyncio.gather(
                    server.accept(expected_peer=client_identity.address),
                    client_task,
                ),
                timeout=10,
            )
            if connector == "dev_alice":
                alice_session, bob_session = client_session, server_session
            else:
                alice_session, bob_session = server_session, client_session
            try:
                alice_result, bob_result = await asyncio.gather(
                    self.engines["dev_alice"].sync_with(alice_session),
                    self.engines["dev_bob"].sync_with(bob_session),
                    return_exceptions=True,
                )
            finally:
                await asyncio.gather(
                    alice_session.close(), bob_session.close(), return_exceptions=True
                )
        finally:
            await server.close()
        return {"dev_alice": alice_result, "dev_bob": bob_result}

    async def _sync_quic(
        self, *, connector: str
    ) -> dict[str, SyncResult | BaseException]:
        alice_identity = PeerIdentity.generate("dev_alice")
        bob_identity = PeerIdentity.generate("dev_bob")
        if connector == "dev_alice":
            server_identity = bob_identity
            client_identity = alice_identity
        else:
            server_identity = alice_identity
            client_identity = bob_identity
        with tempfile.TemporaryDirectory(prefix="hex_quic_sync_") as temporary:
            key_path, certificate_path = _write_quic_certificate(Path(temporary))
            server = QuicPeerServer(
                server_identity,
                certificate=certificate_path,
                private_key=key_path,
            )
            await server.start()
            try:
                client_task = asyncio.create_task(connect_quic_peer(
                    server.host,
                    server.port,
                    client_identity,
                    expected_peer=server_identity.address,
                    cafile=certificate_path,
                ))
                server_session, client_session = await asyncio.wait_for(
                    asyncio.gather(
                        server.accept(expected_peer=client_identity.address),
                        client_task,
                    ),
                    timeout=10,
                )
                if connector == "dev_alice":
                    alice_session, bob_session = client_session, server_session
                else:
                    alice_session, bob_session = server_session, client_session
                try:
                    alice_result, bob_result = await asyncio.gather(
                        self.engines["dev_alice"].sync_with(alice_session),
                        self.engines["dev_bob"].sync_with(bob_session),
                        return_exceptions=True,
                    )
                finally:
                    await asyncio.gather(
                        alice_session.close(), bob_session.close(), return_exceptions=True
                    )
            finally:
                await server.close()
        return {"dev_alice": alice_result, "dev_bob": bob_result}

    def _assert_synced(self, outcomes: dict[str, SyncResult | BaseException]) -> dict[str, SyncResult]:
        for device_id, outcome in outcomes.items():
            if not isinstance(outcome, SyncResult):
                raise self.failureException(f"{device_id} sync failed: {outcome!r}")
        return {device_id: outcome for device_id, outcome in outcomes.items() if isinstance(outcome, SyncResult)}

    async def test_ledger_dialogue_documents_and_skills_sync_bidirectionally(self) -> None:
        alice_task = self.ledgers["dev_alice"].submit_task({"goal": "alice task"})
        self.ledgers["dev_alice"].record_event(
            alice_task["task_id"], "RUNNING", reason_code="STARTED"
        )
        self.ledgers["dev_alice"].record_dialogue_turn(
            alice_task["task_id"], 0, "user", "hello from alice"
        )
        self._write_document("dev_alice", "global/user", "Alice profile.\n")
        self._write_skill("dev_alice", "shared-skill", "Use the shared workflow.")

        bob_task = self.ledgers["dev_bob"].submit_task({"goal": "bob task"})
        self.ledgers["dev_bob"].record_event(
            bob_task["task_id"], "VERIFYING", reason_code="CHECK"
        )
        self._write_document("dev_bob", "workspace/memory", "Bob project memory.\n")

        outcomes = await self._sync(connector="dev_bob")
        results = self._assert_synced(outcomes)

        self.assertIsNotNone(self.ledgers["dev_bob"].get_task(alice_task["task_id"]))
        self.assertIsNotNone(self.ledgers["dev_alice"].get_task(bob_task["task_id"]))
        self.assertEqual(
            self.ledgers["dev_bob"].get_active_dialogue(alice_task["task_id"])[0]["content"],
            "hello from alice",
        )
        self.assertEqual(
            self.cognition["dev_bob"]._read_document("global/user"), b"Alice profile.\n"
        )
        self.assertEqual(
            self.cognition["dev_alice"]._read_document("workspace/memory"),
            b"Bob project memory.\n",
        )
        self.assertTrue(
            (self.homes["dev_bob"] / "skills" / "shared-skill" / "SKILL.md").is_file()
        )
        self.assertTrue(self.ledgers["dev_alice"].verify_chain_integrity(bob_task["task_id"]))
        self.assertTrue(self.ledgers["dev_bob"].verify_chain_integrity(alice_task["task_id"]))
        self.assertEqual(results["dev_alice"].ledger_pages_sent, 2)
        self.assertEqual(results["dev_alice"].ledger_pages_received, 2)
        self.assertTrue(results["dev_alice"].cognition_sent)
        self.assertTrue(results["dev_alice"].cognition_received)
        self.assertEqual(results["dev_bob"].conflicts, [])

    async def test_websocket_sync_engine_transfers_data_and_reconnects_incrementally(self) -> None:
        alice_task = self.ledgers["dev_alice"].submit_task({"goal": "network task"})["task_id"]
        self.ledgers["dev_alice"].record_event(
            alice_task, "RUNNING", reason_code="STARTED"
        )
        self.ledgers["dev_alice"].record_dialogue_turn(
            alice_task, 0, "user", "private network dialogue"
        )
        self._write_document("dev_alice", "global/user", "Alice network profile.\n")
        bob_task = self.ledgers["dev_bob"].submit_task({"goal": "bob network task"})["task_id"]
        self._write_document("dev_bob", "workspace/memory", "Bob network memory.\n")

        first = self._assert_synced(await self._sync_websocket(connector="dev_bob"))

        self.assertEqual(first["dev_alice"].ledger_events_sent, 2)
        self.assertEqual(first["dev_bob"].ledger_events_received, 2)
        self.assertTrue(self.ledgers["dev_bob"].verify_chain_integrity(alice_task))
        self.assertIsNotNone(self.ledgers["dev_alice"].get_task(bob_task))
        self.assertEqual(
            self.ledgers["dev_bob"].get_active_dialogue(alice_task)[0]["content"],
            "private network dialogue",
        )
        self.assertEqual(
            self.cognition["dev_bob"]._read_document("global/user"),
            b"Alice network profile.\n",
        )
        self.assertEqual(
            self.cognition["dev_alice"]._read_document("workspace/memory"),
            b"Bob network memory.\n",
        )

        self.ledgers["dev_alice"].record_event(
            alice_task, "VERIFYING", reason_code="CHECK"
        )
        self._write_document("dev_alice", "global/user", "Alice updated profile.\n")
        second = self._assert_synced(await self._sync_websocket(connector="dev_alice"))

        self.assertEqual(second["dev_alice"].ledger_events_sent, 2)
        self.assertEqual(second["dev_bob"].ledger_events_received, 1)
        state = json.loads(self.engines["dev_alice"].state_path.read_text())
        self.assertEqual(state["peers"]["dev_bob"]["ledger"][alice_task], 3)
        self.assertEqual(
            self.cognition["dev_bob"]._read_document("global/user"),
            b"Alice updated profile.\n",
        )
        self.assertTrue(self.ledgers["dev_bob"].verify_chain_integrity(alice_task))

    async def test_quic_sync_engine_transfers_data_and_reconnects_incrementally(self) -> None:
        alice_task = self.ledgers["dev_alice"].submit_task({"goal": "QUIC task"})["task_id"]
        self.ledgers["dev_alice"].record_event(
            alice_task, "RUNNING", reason_code="STARTED"
        )
        self.ledgers["dev_alice"].record_dialogue_turn(
            alice_task, 0, "user", "private QUIC dialogue"
        )
        self._write_document("dev_alice", "global/user", "Alice QUIC profile.\n")
        bob_task = self.ledgers["dev_bob"].submit_task({"goal": "bob QUIC task"})["task_id"]
        self._write_document("dev_bob", "workspace/memory", "Bob QUIC memory.\n")

        first = self._assert_synced(await self._sync_quic(connector="dev_bob"))

        self.assertEqual(first["dev_alice"].ledger_events_sent, 2)
        self.assertEqual(first["dev_bob"].ledger_events_received, 2)
        self.assertTrue(self.ledgers["dev_bob"].verify_chain_integrity(alice_task))
        self.assertIsNotNone(self.ledgers["dev_alice"].get_task(bob_task))
        self.assertEqual(
            self.ledgers["dev_bob"].get_active_dialogue(alice_task)[0]["content"],
            "private QUIC dialogue",
        )
        self.assertEqual(
            self.cognition["dev_bob"]._read_document("global/user"),
            b"Alice QUIC profile.\n",
        )
        self.assertEqual(
            self.cognition["dev_alice"]._read_document("workspace/memory"),
            b"Bob QUIC memory.\n",
        )

        self.ledgers["dev_alice"].record_event(
            alice_task, "VERIFYING", reason_code="CHECK"
        )
        self._write_document("dev_alice", "global/user", "Alice updated QUIC profile.\n")
        second = self._assert_synced(await self._sync_quic(connector="dev_alice"))

        self.assertEqual(second["dev_alice"].ledger_events_sent, 2)
        self.assertEqual(second["dev_bob"].ledger_events_received, 1)
        state = json.loads(self.engines["dev_alice"].state_path.read_text())
        self.assertEqual(state["peers"]["dev_bob"]["ledger"][alice_task], 3)
        self.assertEqual(
            self.cognition["dev_bob"]._read_document("global/user"),
            b"Alice updated QUIC profile.\n",
        )
        self.assertTrue(self.ledgers["dev_bob"].verify_chain_integrity(alice_task))

    async def test_incremental_sync_uses_persistent_cursors_and_replays_safely(self) -> None:
        task_id = self.ledgers["dev_alice"].submit_task({"goal": "paged task"})["task_id"]
        self.ledgers["dev_alice"].record_event(task_id, "RUNNING", reason_code="STARTED")
        self.ledgers["dev_alice"].record_event(task_id, "VERIFYING", reason_code="CHECK")

        first = self._assert_synced(await self._sync())
        self.assertEqual(first["dev_alice"].ledger_events_sent, 3)
        self.assertEqual(first["dev_bob"].ledger_events_received, 3)
        state_path = self.engines["dev_alice"].state_path
        state = json.loads(state_path.read_text())
        self.assertEqual(state["peers"]["dev_bob"]["ledger"][task_id], 3)

        replay = self._assert_synced(await self._sync())
        self.assertEqual(replay["dev_alice"].ledger_events_sent, 0)
        self.assertEqual(replay["dev_bob"].ledger_events_received, 0)
        self.assertEqual(len(self.ledgers["dev_bob"].list_events(task_id)), 3)

        self.ledgers["dev_alice"].record_event(task_id, "SUCCEEDED", reason_code="DONE")
        incremental = self._assert_synced(await self._sync())
        self.assertEqual(incremental["dev_alice"].ledger_events_sent, 1)
        self.assertEqual(incremental["dev_bob"].ledger_events_received, 1)
        self.assertEqual(len(self.ledgers["dev_bob"].list_events(task_id)), 4)
        self.assertEqual(self.ledgers["dev_bob"].get_task(task_id)["status"], "SUCCEEDED")

    async def test_interrupted_page_resumes_from_acknowledged_cursor(self) -> None:
        task_id = self.ledgers["dev_alice"].submit_task({"goal": "resume task"})["task_id"]
        self.ledgers["dev_alice"].record_event(task_id, "RUNNING", reason_code="STARTED")
        self.ledgers["dev_alice"].record_event(task_id, "VERIFYING", reason_code="CHECK")
        original = self.ledgers["dev_alice"].export_sync_delta_async
        calls = 0

        async def fail_second_page(task_id_arg: str, since_seq: int, *, limit: int) -> dict:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated interruption")
            return await original(task_id_arg, since_seq, limit=limit)

        with mock.patch.object(
            self.ledgers["dev_alice"], "export_sync_delta_async", new=fail_second_page
        ):
            interrupted = await self._sync()
        self.assertTrue(all(isinstance(item, BaseException) for item in interrupted.values()))
        self.assertEqual(len(self.ledgers["dev_bob"].list_events(task_id)), 1)

        resumed = self._assert_synced(await self._sync())
        self.assertEqual(resumed["dev_alice"].ledger_events_sent, 2)
        self.assertEqual(resumed["dev_bob"].ledger_events_received, 2)
        self.assertEqual(len(self.ledgers["dev_bob"].list_events(task_id)), 3)
        self.assertTrue(self.ledgers["dev_bob"].verify_chain_integrity(task_id))

    async def test_ledger_divergence_preserves_main_chains_and_reports_conflicts(self) -> None:
        task_id = self.ledgers["dev_alice"].submit_task({"goal": "shared task"})["task_id"]
        self._assert_synced(await self._sync())
        self.ledgers["dev_alice"].record_event(task_id, "SOURCE_PROGRESS", reason_code="SOURCE")
        self.ledgers["dev_alice"].record_event(task_id, "SOURCE_VERIFIED", reason_code="SOURCE")
        self.ledgers["dev_bob"].record_event(task_id, "TARGET_PROGRESS", reason_code="TARGET")
        alice_tip = self.ledgers["dev_alice"].list_events(task_id)[-1]["event_sha256"]
        bob_tip = self.ledgers["dev_bob"].list_events(task_id)[-1]["event_sha256"]

        results = self._assert_synced(await self._sync())

        self.assertEqual(self.ledgers["dev_alice"].list_events(task_id)[-1]["event_sha256"], alice_tip)
        self.assertEqual(self.ledgers["dev_bob"].list_events(task_id)[-1]["event_sha256"], bob_tip)
        self.assertEqual(len(self.ledgers["dev_alice"].list_sync_branches(task_id)), 1)
        self.assertGreaterEqual(len(self.ledgers["dev_bob"].list_sync_branches(task_id)), 1)
        self.assertTrue(any(item["kind"] == "ledger" for item in results["dev_alice"].conflicts))
        self.assertTrue(any(item["kind"] == "ledger" for item in results["dev_bob"].conflicts))
        alice_branch_count = len(self.ledgers["dev_alice"].list_sync_branches(task_id))
        bob_branch_count = len(self.ledgers["dev_bob"].list_sync_branches(task_id))

        replay = self._assert_synced(await self._sync())
        self.assertEqual(replay["dev_alice"].ledger_events_received, 0)
        self.assertEqual(replay["dev_bob"].ledger_events_received, 0)
        self.assertEqual(len(self.ledgers["dev_alice"].list_sync_branches(task_id)), alice_branch_count)
        self.assertEqual(len(self.ledgers["dev_bob"].list_sync_branches(task_id)), bob_branch_count)

    async def test_cognition_divergence_preserves_both_versions_and_reports_paths(self) -> None:
        self._write_document("dev_alice", "global/user", "shared profile\n")
        self._assert_synced(await self._sync())
        self._write_document("dev_alice", "global/user", "alice version\n")
        self._write_document("dev_bob", "global/user", "bob version\n")

        results = self._assert_synced(await self._sync())

        self.assertEqual(
            self.cognition["dev_alice"]._read_document("global/user"), b"alice version\n"
        )
        self.assertEqual(
            self.cognition["dev_bob"]._read_document("global/user"), b"bob version\n"
        )
        for device_id in ("dev_alice", "dev_bob"):
            conflicts = [item for item in results[device_id].conflicts if item["kind"] == "cognition"]
            self.assertEqual(len(conflicts), 1)
            self.assertTrue(Path(conflicts[0]["path"]).is_dir())

    async def test_workspace_mismatch_fails_before_any_import(self) -> None:
        task_id = self.ledgers["dev_alice"].submit_task({"goal": "private task"})["task_id"]
        self._write_document("dev_alice", "global/user", "private profile\n")
        other_cognition = CognitionSyncStore(
            self.workspaces["dev_bob"],
            global_home=self.homes["dev_bob"],
            workspace_id="workspace-b",
            ledger=self.ledgers["dev_bob"],
        )
        self.engines["dev_bob"] = SyncEngine(
            self.ledgers["dev_bob"],
            other_cognition,
            "dev_bob",
            state_path=self.homes["dev_bob"] / ".sync" / "engine" / "workspace-b.json",
            timeout=10,
        )

        outcomes = await self._sync()

        self.assertTrue(all(isinstance(item, SyncEngineError) for item in outcomes.values()))
        self.assertIsNone(self.ledgers["dev_bob"].get_task(task_id))
        self.assertIsNone(other_cognition._read_document("global/user"))

    async def test_corrupt_cursor_state_fails_closed(self) -> None:
        state_path = self.engines["dev_alice"].state_path
        state_path.parent.mkdir(parents=True)
        state_path.write_text('{"version":1,"workspace_id":"workspace-a","peers":', encoding="utf-8")
        with self.assertRaisesRegex(SyncEngineError, "unreadable"):
            SyncEngine(
                self.ledgers["dev_alice"],
                self.cognition["dev_alice"],
                "dev_alice",
                state_path=state_path,
            )

        state_path.write_text(
            json.dumps({
                "version": 1,
                "workspace_id": "workspace-a",
                "peers": {
                    "dev_bob": {"ledger": {"task": -1}, "cognition": {}}
                },
            }),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(SyncEngineError, "[Ii]nvalid"):
            SyncEngine(
                self.ledgers["dev_alice"],
                self.cognition["dev_alice"],
                "dev_alice",
                state_path=state_path,
            )

    async def test_device_roles_do_not_depend_on_connection_caller(self) -> None:
        self.ledgers["dev_alice"].submit_task({"goal": "alice"})
        self.ledgers["dev_bob"].submit_task({"goal": "bob"})

        results = self._assert_synced(await self._sync(connector="dev_bob"))

        self.assertEqual(results["dev_alice"].ledger_events_sent, 1)
        self.assertEqual(results["dev_alice"].ledger_events_received, 1)
        self.assertEqual(results["dev_bob"].ledger_events_sent, 1)
        self.assertEqual(results["dev_bob"].ledger_events_received, 1)


if __name__ == "__main__":
    unittest.main()
