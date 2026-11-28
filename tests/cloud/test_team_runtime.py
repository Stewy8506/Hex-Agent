"""Production team runtime authorization, publication, failover, and recovery."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cloud.p2p_transport import InMemoryNetwork, PeerAddress, PeerIdentity
from cloud.team_mesh import SignedTurnCommitBundle
from cloud.team_outbox import VerifiedTeamTurn
from cloud.team_runtime import TeamRuntime, TeamRuntimeError
from cloud.team_transport import TeamTransportError
from cloud.updater import canonical_json_bytes
from harness_core.core.models import TaskSubmitRequest
from harness_core.storage.cow import compute_tree_sha
from harness_core.storage.db import LedgerStore


class _ControlPlane:
    def __init__(self, identities: list[PeerIdentity]) -> None:
        self._private = Ed25519PrivateKey.generate()
        self.public_key_hex = self._private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        ).hex()
        self.identities = {identity.device_id: identity for identity in identities}
        self.primary = identities[0].device_id
        self.epoch = 1
        self.sequence = 0
        self.receipts: dict[int, dict[str, Any]] = {}
        self.commits: list[SignedTurnCommitBundle] = []

    def _envelope(self, payload: dict[str, Any]) -> dict[str, Any]:
        signature = base64.urlsafe_b64encode(
            self._private.sign(canonical_json_bytes(payload))
        ).rstrip(b"=").decode("ascii")
        return {"payload": payload, "signature": f"ed25519:{signature}", "etag": "test"}

    def lease(self) -> dict[str, Any]:
        return self._envelope({
            "project_session_id": "proj_runtime",
            "team_id": "team_runtime",
            "host_epoch": self.epoch,
            "primary_device_record_id": self.primary,
            "last_committed_turn_seq": self.sequence,
            "updated_at": "2026-11-28T00:00:00Z",
        })

    def roster(self) -> list[dict[str, Any]]:
        records = []
        for index, identity in enumerate(self.identities.values()):
            records.append({
                "device_record_id": identity.device_id,
                "user_id": f"user_{index}",
                "role": "observer" if identity.device_id == "dev_observer" else "developer",
                "device_pubkey": identity.signing_public_key,
                "host_eligible": 0 if identity.device_id == "dev_observer" else 1,
                "trust_state": "trusted",
                "last_seen_at": "2026-11-28T00:00:00Z",
            })
        return records

    def failover(self, primary: str) -> None:
        self.primary = primary
        self.epoch += 1


class _Account:
    def __init__(self, control: _ControlPlane) -> None:
        self.control = control

    def peer_identity(self) -> PeerIdentity:
        raise AssertionError("Tests construct runtimes with an explicit local identity.")

    def server_public_key(self) -> str:
        return self.control.public_key_hex

    def list_team_devices(self, _team_id: str) -> list[dict[str, Any]]:
        return copy.deepcopy(self.control.roster())

    def get_session_lease(self, _session_id: str) -> dict[str, Any]:
        return self.control.lease()

    def commit_session_turn(
        self,
        _session_id: str,
        coordinator_device_id: str,
        signed_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        signed = SignedTurnCommitBundle.from_dict(signed_bundle)
        if coordinator_device_id != self.control.primary:
            raise ValueError("Stale Coordinator.")
        author_key = self.control.identities[signed.author_device_record_id].signing_public_key
        if not signed.verify(author_key):
            raise ValueError("Invalid author signature.")
        bundle = signed.bundle
        if bundle.host_epoch != self.control.epoch:
            raise ValueError("Stale epoch.")
        existing = self.control.receipts.get(bundle.turn_seq)
        if existing is not None:
            if existing["payload"]["bundle_digest"] != bundle.compute_digest():
                raise ValueError("Conflicting sequence reuse.")
            return existing
        if bundle.turn_seq != self.control.sequence + 1:
            raise ValueError("Non-contiguous sequence.")
        self.control.sequence = bundle.turn_seq
        self.control.commits.append(signed)
        receipt = self.control._envelope({
            "project_session_id": bundle.project_session_id,
            "host_epoch": bundle.host_epoch,
            "primary_device_record_id": coordinator_device_id,
            "author_device_record_id": signed.author_device_record_id,
            "turn_seq": bundle.turn_seq,
            "bundle_digest": bundle.compute_digest(),
            "proof_merkle_root": bundle.proof_merkle_root,
            "committed_at": "2026-11-28T00:00:00Z",
        })
        self.control.receipts[bundle.turn_seq] = receipt
        return receipt


class TeamRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.host = PeerIdentity.generate("dev_host")
        self.replica = PeerIdentity.generate("dev_replica")
        self.catchup = PeerIdentity.generate("dev_catchup")
        self.observer = PeerIdentity.generate("dev_observer")
        self.control = _ControlPlane([self.host, self.replica, self.catchup, self.observer])
        self.account = _Account(self.control)
        self.ledgers: dict[str, LedgerStore] = {}
        self.runtimes: list[TeamRuntime] = []
        self.serve_tasks: list[asyncio.Task[Any]] = []

    async def asyncTearDown(self) -> None:
        for task in self.serve_tasks:
            task.cancel()
        await asyncio.gather(*self.serve_tasks, return_exceptions=True)
        for runtime in reversed(self.runtimes):
            await runtime.stop()
        for ledger in self.ledgers.values():
            ledger.close()
        self.tmp.cleanup()

    def _workspace(self, name: str, *, value: int = 1) -> Path:
        workspace = self.root / name
        workspace.mkdir()
        (workspace / "app.py").write_text(f"value = {value}\n", encoding="utf-8")
        (workspace / ".env").write_text(f"{name.upper()}_SECRET=local-only\n", encoding="utf-8")
        return workspace

    def _ledger(self, name: str) -> LedgerStore:
        ledger = LedgerStore(self.root / f"{name}.db")
        self.ledgers[name] = ledger
        return ledger

    async def _runtime(
        self,
        identity: PeerIdentity,
        name: str,
        *,
        workspace: Path | None = None,
        data_dir: Path | None = None,
        ledger: LedgerStore | None = None,
    ) -> TeamRuntime:
        workspace = workspace or self._workspace(f"{name}-workspace")
        ledger = ledger or self._ledger(name)
        runtime = await TeamRuntime.create(
            account=self.account,
            signer=identity,
            team_id="team_runtime",
            project_session_id="proj_runtime",
            workspace_root=workspace,
            data_dir=data_dir or self.root / f"{name}-data",
            ledger=ledger,
            global_home=self.root / f"{name}-home",
            maintenance_interval=60,
            lease_max_age=120,
        )
        self.runtimes.append(runtime)
        await runtime.start()
        return runtime

    async def _link(
        self, left: TeamRuntime, right: TeamRuntime
    ) -> tuple[PeerIdentity, PeerIdentity]:
        left_identity = next(
            identity for identity in (self.host, self.replica, self.catchup)
            if identity.device_id == left.local_device_id
        )
        right_identity = next(
            identity for identity in (self.host, self.replica, self.catchup)
            if identity.device_id == right.local_device_id
        )
        network = InMemoryNetwork()
        left_endpoint = network.endpoint(left_identity)
        right_endpoint = network.endpoint(right_identity)
        left_session = await left_endpoint.connect(right_endpoint)
        right_session = await right_endpoint.accept()
        left_task = asyncio.create_task(
            left.serve_session(left_session, PeerAddress(right.local_device_id, right_identity.signing_public_key))
        )
        right_task = asyncio.create_task(
            right.serve_session(right_session, PeerAddress(left.local_device_id, left_identity.signing_public_key))
        )
        self.serve_tasks.extend([left_task, right_task])
        await self._wait_for(
            lambda: any(
                peer["device_record_id"] == right.local_device_id
                for peer in left.status()["peers"]
            )
            and any(
                peer["device_record_id"] == left.local_device_id
                for peer in right.status()["peers"]
            )
        )
        return left_identity, right_identity

    def _verified_turn(
        self, runtime: TeamRuntime, name: str, *, value: int = 2
    ) -> VerifiedTeamTurn:
        base = self.root / f"{name}-base"
        head = self.root / f"{name}-head"
        base.mkdir()
        head.mkdir()
        (base / "app.py").write_text("value = 1\n", encoding="utf-8")
        (head / "app.py").write_text(f"value = {value}\n", encoding="utf-8")
        (runtime.cognition.global_home / "USER.md").write_text(
            f"Cognition from {name}\n", encoding="utf-8"
        )
        task = runtime.ledger.submit_task(TaskSubmitRequest(goal=f"verified {name} team turn"))
        runtime.ledger.record_dialogue_turn(task["task_id"], 0, "user", "private team dialogue")
        tree = compute_tree_sha(head)
        runtime.ledger.record_event(
            task["task_id"],
            "TURN_VERIFIED",
            reason_code="TEAM_TURN",
            attempt_id="att_1",
            workspace_tree_sha=tree,
            payload={"verified": True},
        )
        proof = runtime.ledger.save_proof_bundle(
            task_id=task["task_id"],
            attempt_id="att_1",
            git_diff_sha=hashlib.sha256(b"").hexdigest(),
            git_diff_text="",
            workspace_tree_sha=tree,
            runtime_digest="b" * 64,
            ast_impact={"modified_files": ["app.py"]},
            verification_results={"passed": True, "exit_code": 0},
        )
        return VerifiedTeamTurn(
            task_id=task["task_id"],
            attempt_id="att_1",
            workspace_dir=head,
            base_snapshot_path=base,
            proof=proof,
        )

    async def _wait_for(self, predicate: Any, timeout: float = 5.0) -> None:
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.01)

    async def test_roster_rejects_observers_and_unenrolled_peers(self) -> None:
        runtime = await self._runtime(self.host, "auth")
        self.assertEqual(runtime.status()["roster_devices"], 4)
        network = InMemoryNetwork()
        host_endpoint = network.endpoint(self.host)
        outsider_endpoint = network.endpoint(self.observer)
        host_session = await host_endpoint.connect(outsider_endpoint)
        outsider_session = await outsider_endpoint.accept()
        try:
            with self.assertRaises(TeamRuntimeError):
                await runtime.serve_session(
                    host_session,
                    PeerAddress(self.observer.device_id, self.observer.signing_public_key),
                )
        finally:
            await host_session.close()
            await outsider_session.close()

    async def test_invalid_lease_signature_revokes_runtime_authorization(self) -> None:
        host = await self._runtime(self.host, "host")
        replica = await self._runtime(self.replica, "replica")
        await self._link(host, replica)
        invalid = self.control.lease()
        invalid["signature"] = "ed25519:" + "00" * 64
        self.control.lease = lambda: copy.deepcopy(invalid)

        with self.assertRaises(TeamRuntimeError):
            await host.refresh()

        self.assertFalse(host.status()["running"])
        self.assertEqual(host.status()["roster_devices"], 0)
        self.assertEqual(host.status()["peers"], [])
        self.assertIsNone(host._signed_lease)
        with self.assertRaises(TeamRuntimeError):
            await host.publish(self._verified_turn(host, "revoked-turn"))

    async def test_removed_local_device_fails_closed(self) -> None:
        host = await self._runtime(self.host, "host")
        replica = await self._runtime(self.replica, "replica")
        await self._link(host, replica)
        roster = [record for record in self.control.roster() if record["device_record_id"] != self.host.device_id]
        self.control.roster = lambda: copy.deepcopy(roster)

        with self.assertRaises(TeamRuntimeError):
            await host.refresh()

        self.assertFalse(host.status()["running"])
        self.assertEqual(host.status()["roster_devices"], 0)
        self.assertEqual(host.status()["peers"], [])
        with self.assertRaises(TeamRuntimeError):
            host.connect(self.replica.device_id)

    async def test_coordinator_turn_is_committed_applied_archived_and_replicated(self) -> None:
        host = await self._runtime(self.host, "host")
        replica = await self._runtime(self.replica, "replica")
        await self._link(host, replica)
        turn = self._verified_turn(host, "host-turn")

        result = await host.publish(turn)

        self.assertEqual(result["status"], "delivered")
        self.assertEqual(len(self.control.commits), 1)
        self.assertEqual((host.workspace_root / "app.py").read_text(), "value = 2\n")
        self.assertEqual((replica.workspace_root / "app.py").read_text(), "value = 2\n")
        self.assertEqual((replica.workspace_root / ".env").read_text(), "REPLICA-WORKSPACE_SECRET=local-only\n")
        self.assertEqual(host.mesh.last_applied_turn_seq, 1)
        self.assertEqual(replica.mesh.last_applied_turn_seq, 1)
        self.assertEqual(host.history.status()["records"], 1)
        self.assertEqual((replica.cognition.global_home / "USER.md").read_text(), "Cognition from host-turn\n")

    async def test_replica_submission_commits_once_and_completes_after_relay_ack(self) -> None:
        host = await self._runtime(self.host, "host")
        replica = await self._runtime(self.replica, "replica")
        await self._link(host, replica)
        turn = self._verified_turn(replica, "replica-turn")

        result = await replica.publish(turn)

        self.assertEqual(result["status"], "delivered")
        self.assertEqual(len(self.control.commits), 1)
        self.assertEqual(self.control.commits[0].author_device_record_id, self.replica.device_id)
        self.assertEqual((host.workspace_root / "app.py").read_text(), "value = 2\n")
        self.assertEqual((replica.workspace_root / "app.py").read_text(), "value = 2\n")
        self.assertEqual(replica.outbox.pending_count, 0)
        self.assertEqual(host.history.status()["records"], 1)

    async def test_coordinator_turn_remains_queued_until_every_replica_acknowledges(self) -> None:
        host = await self._runtime(self.host, "host")
        replica = await self._runtime(self.replica, "replica")
        await self._link(host, replica)
        failing_link = next(iter(host._links.values()))
        send_turn = failing_link.send_turn

        async def fail_send(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise TeamTransportError("Injected peer delivery failure.")

        failing_link.send_turn = fail_send
        turn = self._verified_turn(host, "coordinator-partial-fanout")
        failed = await host.publish(turn)
        self.assertEqual(failed["status"], "queued")
        self.assertEqual(host.outbox.pending_count, 1)
        self.assertEqual(len(self.control.commits), 1)
        self.assertEqual((host.workspace_root / "app.py").read_text(), "value = 2\n")
        self.assertEqual((replica.workspace_root / "app.py").read_text(), "value = 1\n")

        failing_link.send_turn = send_turn
        delivered = await host.publish(turn)

        self.assertEqual(delivered["status"], "delivered")
        self.assertEqual(host.outbox.pending_count, 0)
        self.assertEqual(len(self.control.commits), 1)
        self.assertEqual((replica.workspace_root / "app.py").read_text(), "value = 2\n")

    async def test_peer_submission_retries_exactly_after_partial_fanout_failure(self) -> None:
        host = await self._runtime(self.host, "host")
        author = await self._runtime(self.replica, "replica")
        other = await self._runtime(self.catchup, "catchup")
        await self._link(host, author)
        await self._link(host, other)
        failing_link = next(
            link
            for link in host._links.values()
            if link.remote_device_id == other.local_device_id
        )
        send_turn = failing_link.send_turn

        async def fail_send(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise TeamTransportError("Injected peer delivery failure.")

        failing_link.send_turn = fail_send
        turn = self._verified_turn(author, "partial-fanout")
        failed = await author.publish(turn)
        self.assertEqual(failed["status"], "queued")
        self.assertEqual(author.outbox.pending_count, 1)
        self.assertEqual(len(self.control.commits), 1)
        self.assertEqual((author.workspace_root / "app.py").read_text(), "value = 2\n")
        self.assertEqual((other.workspace_root / "app.py").read_text(), "value = 1\n")

        failing_link.send_turn = send_turn
        delivered = await author.publish(turn)

        self.assertEqual(delivered["status"], "delivered")
        self.assertEqual(author.outbox.pending_count, 0)
        self.assertEqual(len(self.control.commits), 1)
        self.assertEqual((other.workspace_root / "app.py").read_text(), "value = 2\n")
        self.assertEqual(host.history.status()["records"], 1)

    async def test_synchronous_hook_stages_durably_for_async_flush(self) -> None:
        host = await self._runtime(self.host, "host")
        turn = self._verified_turn(host, "queued-turn")

        result = await asyncio.to_thread(host, turn)

        self.assertEqual(result["status"], "queued")
        self.assertEqual(host.outbox.pending_count, 1)
        flushed = await host.flush()
        self.assertEqual(flushed["delivered"], 1)
        self.assertEqual(host.outbox.pending_count, 0)
        self.assertEqual((host.workspace_root / "app.py").read_text(), "value = 2\n")

    async def test_restart_restores_progress_and_serves_verified_catchup(self) -> None:
        host = await self._runtime(self.host, "host")
        replica = await self._runtime(self.replica, "replica")
        await self._link(host, replica)
        await host.publish(self._verified_turn(host, "initial-turn"))
        await replica.stop()
        await host.stop()
        host_data = self.root / "host-data"
        host_workspace = host.workspace_root
        host_ledger = host.ledger

        restarted_host = await self._runtime(
            self.host,
            "host-restarted",
            workspace=host_workspace,
            data_dir=host_data,
            ledger=host_ledger,
        )
        fresh_replica = await self._runtime(self.catchup, "catchup")
        await self._link(restarted_host, fresh_replica)
        await self._wait_for(lambda: fresh_replica.mesh.last_applied_turn_seq == 1)

        self.assertEqual(restarted_host.mesh.last_applied_turn_seq, 1)
        self.assertEqual((fresh_replica.workspace_root / "app.py").read_text(), "value = 2\n")
        self.assertEqual(len(self.control.commits), 1)

    async def test_epoch_change_reconnects_roles_and_allows_new_coordinator_submission(self) -> None:
        host = await self._runtime(self.host, "host")
        replica = await self._runtime(self.replica, "replica")
        await self._link(host, replica)
        self.control.failover(self.replica.device_id)

        await asyncio.gather(host.refresh(), replica.refresh())
        await self._wait_for(lambda: not host.status()["peers"] and not replica.status()["peers"])
        self.assertEqual(host.status()["mode"], "TEAM_REPLICA_PEER")
        self.assertEqual(replica.status()["mode"], "TEAM_PRIMARY_HOST")
        await self._link(host, replica)

        result = await host.publish(self._verified_turn(host, "post-failover"))

        self.assertEqual(result["status"], "delivered")
        self.assertEqual(len(self.control.commits), 1)
        self.assertEqual(self.control.commits[0].bundle.host_epoch, 2)
        self.assertEqual((replica.workspace_root / "app.py").read_text(), "value = 2\n")
        self.assertEqual((host.workspace_root / "app.py").read_text(), "value = 2\n")


if __name__ == "__main__":
    unittest.main()
