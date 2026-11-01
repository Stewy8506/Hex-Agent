"""Unit & Integration Tests for WorkerDaemon Architecture and Transports (Plan 06-01).

Proves:
1. Host hardware and machine profiling auto-detection (`inspect_machine_profile`).
2. Worker registration and persistence in the PostgreSQL / SQLite authoritative ledger.
3. Background heartbeat thread continually advancing lease expiration and worker health.
4. Graceful shutdown on stop / signal releasing active leases for fast failover.
5. Single-task execution loop (`--once`) claiming and executing a queued task.
6. REST API and Direct Ledger client parity over HTTP with `X-Worker-Token` authentication.
7. PermissionError on invalid worker tokens (HTTP 401).
8. ConcurrencyFenceError on stale fencing tokens (HTTP 409).
9. Preemption detection setting `daemon.fenced = True` when fencing token is superseded.
"""

from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from unittest.mock import patch

from harness_core.db import ConcurrencyFenceError, LedgerStore
from harness_core.models import TaskState, TaskSubmitRequest
from harness_core.orchestrator import HarnessOrchestrator
from harness_core.server import HarnessAPIApp
from harness_core.worker_daemon import (
    DirectLedgerWorkerClient,
    RestWorkerClient,
    WorkerDaemon,
    inspect_machine_profile,
)


class MockHTTPResponse:
    """Mock HTTP response wrapping JSON payloads for hermetic REST client tests."""

    def __init__(self, status: int, payload: Any, content_type: str = "application/json"):
        self.status = status
        self.code = status
        if isinstance(payload, str):
            self._data = payload.encode("utf-8")
        else:
            self._data = json.dumps(payload, default=str).encode("utf-8")
        self.headers = {"Content-Type": content_type}

    def read(self) -> bytes:
        return self._data

    def __enter__(self) -> MockHTTPResponse:
        return self

    def __exit__(self, *args: object) -> None:
        pass


def make_mock_urlopen(app: HarnessAPIApp):
    """Creates a mock urllib.request.urlopen dispatcher connected directly to HarnessAPIApp."""

    def _mock_urlopen(req: urllib.request.Request, timeout: float = 15.0) -> MockHTTPResponse:
        url = req.full_url
        parsed = urllib.parse.urlparse(url)
        path = parsed.path
        if parsed.query:
            path = f"{path}?{parsed.query}"
        method = req.get_method()
        raw_data = req.data
        body: dict[str, Any] = {}
        if raw_data:
            try:
                body = json.loads(raw_data.decode("utf-8"))
            except Exception:
                pass
        headers = {k: v for k, v in req.headers.items()}
        status, payload, content_type = app.handle_request(method, path, body, headers)
        if status >= 400:
            err_bytes = (
                payload.encode("utf-8")
                if isinstance(payload, str)
                else json.dumps(payload, default=str).encode("utf-8")
            )
            fp = io.BytesIO(err_bytes)
            raise urllib.error.HTTPError(url, status, "HTTP Error", {}, fp)
        return MockHTTPResponse(status, payload, content_type)

    return _mock_urlopen


class TestWorkerDaemon(unittest.TestCase):
    """Test suite for distributed WorkerDaemon and transport clients."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.orch = HarnessOrchestrator(self.data_dir)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_machine_profiling_and_registration(self) -> None:
        """Verifies host profiling detects CPU/RAM/OS/CoW and registers worker in ledger."""
        prof = inspect_machine_profile()
        self.assertIn(prof["machine_class"], ("m4_air", "desktop_gpu", "server"))
        self.assertIn(prof["cow_backend"], ("apfs_clone", "btrfs_subvolume", "overlayfs", "portable_copy"))
        self.assertIn("cpu_cores", prof["capabilities"])
        self.assertIn("memory_bytes", prof["capabilities"])
        self.assertGreaterEqual(prof["capabilities"]["cpu_cores"], 1)
        self.assertEqual(prof["trust_level"], "trusted_local")

        # Test class override
        custom_prof = inspect_machine_profile(override_class="server")
        self.assertEqual(custom_prof["machine_class"], "server")

        # Test registration via DirectLedgerWorkerClient
        client = DirectLedgerWorkerClient(self.orch)
        w = client.register_worker(
            "w-profile-01",
            machine_class="m4_air",
            cow_backend="apfs_clone",
            capabilities={"test": True},
            local_llm_models=["custom-model"],
        )
        self.assertEqual(w["worker_id"], "w-profile-01")
        self.assertEqual(w["machine_class"], "m4_air")
        self.assertEqual(w["health"], "HEALTHY")
        self.assertIsNotNone(w["registered_at"])

        persisted = self.orch.ledger.get_worker("w-profile-01")
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted["worker_id"], "w-profile-01")

    def test_heartbeat_loop_and_lease_renewal(self) -> None:
        """Verifies background heartbeat thread renews active CAS lease periodically."""
        self.orch.ledger.submit_task({"goal": "Heartbeat test task", "status": "QUEUED"})
        client = DirectLedgerWorkerClient(self.orch)
        claimed = client.claim_task("w-hb-01", ttl_seconds=5)
        self.assertIsNotNone(claimed)

        lease_id = claimed["lease_id"]
        token = claimed["fencing_token"]
        initial_exp = claimed["expires_at"]

        daemon = WorkerDaemon(client, worker_id="w-hb-01", lease_ttl=5)
        daemon.current_task_id = claimed["task_id"]
        daemon.current_lease_id = lease_id
        daemon.current_fencing_token = token
        daemon.start()

        try:
            # Sleep long enough for at least one heartbeat cycle (~1.7s)
            time.sleep(2.0)
            renewed = self.orch.ledger.get_lease(lease_id)
            self.assertIsNotNone(renewed)
            self.assertGreater(renewed["expires_at"], initial_exp)
            self.assertIsNotNone(renewed["renewed_at"])

            w = self.orch.ledger.get_worker("w-hb-01")
            self.assertIsNotNone(w["last_heartbeat_at"])
        finally:
            daemon.stop()

    def test_graceful_shutdown_releases_lease(self) -> None:
        """Verifies stop() immediately calls release_lease so task can failover."""
        self.orch.ledger.submit_task({"goal": "Shutdown lease release test", "status": "QUEUED"})
        client = DirectLedgerWorkerClient(self.orch)
        claimed = client.claim_task("w-shutdown-01", ttl_seconds=300)
        self.assertIsNotNone(claimed)

        lease_id = claimed["lease_id"]
        token = claimed["fencing_token"]

        daemon = WorkerDaemon(client, worker_id="w-shutdown-01", lease_ttl=300)
        daemon.current_task_id = claimed["task_id"]
        daemon.current_lease_id = lease_id
        daemon.current_fencing_token = token
        daemon.start()

        # Before stop: lease is active
        lse_before = self.orch.ledger.get_lease(lease_id)
        self.assertIsNone(lse_before["released_at"])

        # Stop daemon
        daemon.stop()

        # After stop: lease is released
        lse_after = self.orch.ledger.get_lease(lease_id)
        self.assertIsNotNone(lse_after["released_at"])
        self.assertIsNone(daemon.current_lease_id)

    def test_single_task_execution_mode_once(self) -> None:
        """Verifies daemon.run(once=True) claims 1 task, executes to SUCCEEDED, and terminates."""
        req = TaskSubmitRequest(goal="Worker single task execution test", execution_mode="fast_path")
        task = self.orch.submit_task(req, auto_execute=False)
        t_id = task["task_id"]
        # task is placed into QUEUED state ready for worker claiming

        client = DirectLedgerWorkerClient(self.orch)
        daemon = WorkerDaemon(
            client=client,
            worker_id="w-once-01",
            poll_interval=0.1,
            lease_ttl=30,
            orchestrator=self.orch,
        )

        daemon.run(once=True)

        updated = self.orch.ledger.get_task(t_id)
        self.assertEqual(updated["status"], TaskState.SUCCEEDED.value)

        attempts = self.orch.ledger.list_attempts(t_id)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["worker_id"], "w-once-01")
        self.assertEqual(attempts[0]["status"], "SUCCEEDED")

    def test_rest_client_and_server_parity(self) -> None:
        """Verifies REST client against HarnessAPIApp over HTTP with valid X-Worker-Token."""
        app = HarnessAPIApp(self.orch, auth_token="test-secret-token")
        mock_urlopen = make_mock_urlopen(app)

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            client = RestWorkerClient("http://127.0.0.1:8420", token="test-secret-token")

            # 1. Register worker
            w = client.register_worker(
                "rest-w-01",
                machine_class="m4_air",
                cow_backend="apfs_clone",
                capabilities={"rest": True},
            )
            self.assertEqual(w["worker_id"], "rest-w-01")
            self.assertEqual(w["health"], "HEALTHY")

            # 2. Heartbeat
            hb = client.heartbeat("rest-w-01", health="HEALTHY")
            self.assertEqual(hb["health"], "HEALTHY")

            # 3. Claim empty queue
            empty_claim = client.claim_task("rest-w-01")
            self.assertIsNone(empty_claim)

            # 4. Submit task and claim over REST
            self.orch.ledger.submit_task({"goal": "REST queue claim test", "status": "QUEUED"})
            claimed = client.claim_task("rest-w-01", ttl_seconds=200)
            self.assertIsNotNone(claimed)
            self.assertIn("task_id", claimed)
            lease_id = claimed["lease_id"]
            token = claimed["fencing_token"]

            # 5. Renew lease over REST
            renewed = client.renew_lease(lease_id, token, ttl_seconds=300)
            self.assertGreaterEqual(renewed["expires_at"], claimed["expires_at"])

            # 6. Release lease over REST
            released = client.release_lease(lease_id, token)
            self.assertIsNotNone(released["released_at"])

            # 7. Get task over REST
            t = client.get_task(claimed["task_id"])
            self.assertEqual(t["task_id"], claimed["task_id"])

    def test_rest_client_auth_rejection(self) -> None:
        """Verifies HTTP 401 Unauthorized translated to PermissionError for invalid token."""
        app = HarnessAPIApp(self.orch, auth_token="correct-token")
        mock_urlopen = make_mock_urlopen(app)

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            bad_client = RestWorkerClient("http://127.0.0.1:8420", token="wrong-token")
            with self.assertRaises(PermissionError):
                bad_client.register_worker("w-bad")
            with self.assertRaises(PermissionError):
                bad_client.claim_task("w-bad")
            with self.assertRaises(PermissionError):
                bad_client.heartbeat("w-bad")

    def test_rest_client_concurrency_fencing_conflict(self) -> None:
        """Verifies HTTP 409 Conflict translated to ConcurrencyFenceError for stale tokens."""
        app = HarnessAPIApp(self.orch, auth_token=None)
        mock_urlopen = make_mock_urlopen(app)

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            client = RestWorkerClient("http://127.0.0.1:8420")
            self.orch.ledger.submit_task({"goal": "Fence conflict test", "status": "QUEUED"})
            claimed = client.claim_task("w-fenced", ttl_seconds=100)
            self.assertIsNotNone(claimed)

            lease_id = claimed["lease_id"]
            token = claimed["fencing_token"]

            # Advance task fencing token with another claim or lease
            self.orch.ledger.acquire_lease_and_attempt(claimed["task_id"], "w-second")

            # Now renew_lease with original stale token should raise ConcurrencyFenceError
            with self.assertRaises(ConcurrencyFenceError):
                client.renew_lease(lease_id, token, ttl_seconds=100)

    def test_fencing_detected_in_heartbeat_loop(self) -> None:
        """Verifies heartbeat loop sets daemon.fenced = True when lease renewal is rejected."""
        self.orch.ledger.submit_task({"goal": "Daemon preemption test", "status": "QUEUED"})
        client = DirectLedgerWorkerClient(self.orch)
        claimed = client.claim_task("w-fenced-hb", ttl_seconds=5)
        self.assertIsNotNone(claimed)

        lease_id = claimed["lease_id"]
        token = claimed["fencing_token"]

        daemon = WorkerDaemon(client, worker_id="w-fenced-hb", lease_ttl=5)
        daemon.current_task_id = claimed["task_id"]
        daemon.current_lease_id = lease_id
        daemon.current_fencing_token = token
        daemon.start()

        try:
            # Advance fencing token on task by acquiring a new lease
            self.orch.ledger.acquire_lease_and_attempt(claimed["task_id"], "w-hijack")

            # Wait for heartbeat cycle to detect fencing
            time.sleep(2.0)
            self.assertTrue(daemon.fenced)
        finally:
            daemon.stop()


if __name__ == "__main__":
    unittest.main()
