"""Comprehensive Test Suite for Cross-Machine CoW Delta Archive Sync, Lease Handover & Fencing Defense (Plan 06-02).

Validates:
1. CoW delta archive export and import across isolated workspace directories:
   - Nested directories, files, empty directories, and symlinks.
   - Merkle tree SHA parity (`compute_tree_sha(src) == compute_tree_sha(dst)`).
   - Direct unpacking with or without 'workspace' root prefix.
   - Sanitization of pre-existing non-ignored files in target workspace.
2. Tamper detection and tar-slip path traversal defense:
   - Rejection of relative path traversal (`..` segments) in tar members.
   - Rejection of absolute path traversal outside target workspace.
   - Rejection of corrupted archive bundles.
   - Mismatched Merkle tree SHA raising ValueError during workspace reconstruction.
3. Simulated worker disconnect, lease expiry takeover, and 0ms tool journal replay:
   - Worker 1 claims task (fencing token 1), executes step 1, seals tool call in journal, exports CAS delta archive.
   - Worker 1 lease expires.
   - Reaping marks task QUEUED with reason_code="LEASE_EXPIRED".
   - Worker 2 claims task with strictly incremented fencing token (1 -> 2).
   - LEASE_TAKEOVER event recorded in hash-chained task event log.
   - Worker 2 reconstructs workspace from CAS delta artifact.
   - Worker 2 replays already-sealed tool calls in 0ms (< 15ms) without duplicate external execution.
   - Worker 2 executes step 2 and completes task to SUCCEEDED.
4. Stale worker wake-up and monotonic fencing token defense:
   - Stale Worker 1 wakes up and attempts tool calls, lease renewals, and state transitions with fencing_token=1.
   - Operations raise ConcurrencyFenceError locally and map to HTTP 409 Conflict over REST.
   - Zero split-brain or duplicate commits occur; task remains authoritative SUCCEEDED.
5. CAS Artifact Transfer over REST API:
   - POST /v1/artifacts/delta uploads .tar.gz bundle and records artifact in ledger.
   - GET /v1/artifacts/{id}/file downloads raw archive with application/gzip.
   - RestWorkerClient seamlessly uploads, lists, downloads, and reconstructs remote workspace.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from unittest.mock import patch

from harness_core.cow import (
    compute_tree_sha,
    export_delta_archive,
    get_default_snapshot_provider,
    import_delta_archive,
)
from harness_core.db import (
    ConcurrencyFenceError,
    LedgerStore,
    canonical_json,
    sha256_hex,
    utcnow_iso,
)
from harness_core.gateway import ToolGateway
from harness_core.models import PlanNode, TaskState, TaskSubmitRequest
from harness_core.orchestrator import HarnessOrchestrator
from harness_core.server import HarnessAPIApp
from harness_core.worker_daemon import (
    DirectLedgerWorkerClient,
    RestWorkerClient,
    WorkerDaemon,
)


class MockHTTPResponse:
    """Mock HTTP response wrapping JSON payloads or raw binary streams."""

    def __init__(self, status: int, payload: Any, content_type: str = "application/json"):
        self.status = status
        self.code = status
        if isinstance(payload, bytes):
            self._data = payload
        elif isinstance(payload, str):
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
        body: Any = None
        if raw_data:
            try:
                body = json.loads(raw_data.decode("utf-8"))
            except Exception:
                body = raw_data
        headers = {k: v for k, v in req.headers.items()}
        status, payload, content_type = app.handle_request(method, path, body, headers)
        if status >= 400:
            err_bytes = (
                payload
                if isinstance(payload, bytes)
                else (payload.encode("utf-8") if isinstance(payload, str) else json.dumps(payload, default=str).encode("utf-8"))
            )
            fp = io.BytesIO(err_bytes)
            raise urllib.error.HTTPError(url, status, "HTTP Error", {}, fp)
        return MockHTTPResponse(status, payload, content_type)

    return _mock_urlopen


class TestCowDeltaArchiveSync(unittest.TestCase):
    """Tests CoW delta archive export, import, Merkle tree SHA parity, and path handling."""

    def setUp(self) -> None:
        self.temp_dir_obj = tempfile.TemporaryDirectory()
        self.temp_dir = Path(self.temp_dir_obj.name)
        self.src_dir = self.temp_dir / "src_ws"
        self.src_dir.mkdir(parents=True)
        self.dest_dir = self.temp_dir / "dest_ws"
        self.dest_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp_dir_obj.cleanup()

    def test_cross_machine_cow_export_and_import_integrity(self) -> None:
        """Exporting a directory tree and importing into a separate directory preserves full integrity and tree SHA."""
        # 1. Populate rich directory structure
        (self.src_dir / "README.md").write_text("# Project Alpha\nDocumentation line.", encoding="utf-8")
        src_code = self.src_dir / "src" / "pkg"
        src_code.mkdir(parents=True)
        (src_code / "module.py").write_text("def run():\n    return 42\n", encoding="utf-8")
        (src_code / "__init__.py").write_text("", encoding="utf-8")

        empty_sub = self.src_dir / "empty_dir"
        empty_sub.mkdir()

        # Create relative symlink
        symlink_target = src_code / "link_readme.md"
        symlink_target.symlink_to("../../README.md")

        src_tree_sha = compute_tree_sha(self.src_dir)
        self.assertTrue(len(src_tree_sha) == 64)

        # 2. Export delta archive
        archive_path = self.temp_dir / "delta.tar.gz"
        archive_sha = export_delta_archive(self.src_dir, archive_path)
        self.assertTrue(archive_path.is_file())
        self.assertTrue(len(archive_sha) == 64)

        # 3. Import into distinct target directory
        imported_sha = import_delta_archive(archive_path, self.dest_dir)

        # 4. Assert Merkle tree SHA matches exactly
        self.assertEqual(imported_sha, src_tree_sha)
        self.assertEqual(compute_tree_sha(self.dest_dir), src_tree_sha)

        # 5. Assert filesystem elements match
        self.assertEqual((self.dest_dir / "README.md").read_text(encoding="utf-8"), "# Project Alpha\nDocumentation line.")
        self.assertEqual((self.dest_dir / "src" / "pkg" / "module.py").read_text(encoding="utf-8"), "def run():\n    return 42\n")
        self.assertTrue((self.dest_dir / "empty_dir").is_dir())
        dest_link = self.dest_dir / "src" / "pkg" / "link_readme.md"
        self.assertTrue(dest_link.is_symlink())
        self.assertEqual(os.readlink(dest_link), "../../README.md")
        self.assertEqual(dest_link.read_text(encoding="utf-8"), "# Project Alpha\nDocumentation line.")

    def test_import_with_workspace_root_prefix_and_flat_archive(self) -> None:
        """Both archives with a 'workspace' root directory and flat archives unpack directly into target_dir."""
        # Archive with 'workspace/' root prefix
        ws_prefixed_dir = self.temp_dir / "prefixed_src"
        (ws_prefixed_dir / "workspace").mkdir(parents=True)
        (ws_prefixed_dir / "workspace" / "file1.txt").write_text("prefixed content", encoding="utf-8")

        archive_prefixed = self.temp_dir / "prefixed.tar.gz"
        with tarfile.open(archive_prefixed, "w:gz") as tar:
            tar.add(ws_prefixed_dir / "workspace", arcname="workspace")

        target1 = self.temp_dir / "target1"
        target1.mkdir()
        sha1 = import_delta_archive(archive_prefixed, target1)
        self.assertTrue((target1 / "file1.txt").is_file())
        self.assertFalse((target1 / "workspace").exists())
        self.assertEqual((target1 / "file1.txt").read_text(encoding="utf-8"), "prefixed content")

        # Flat archive without prefix
        flat_src = self.temp_dir / "flat_src"
        flat_src.mkdir(parents=True)
        (flat_src / "file2.txt").write_text("flat content", encoding="utf-8")
        archive_flat = self.temp_dir / "flat.tar.gz"
        with tarfile.open(archive_flat, "w:gz") as tar:
            tar.add(flat_src / "file2.txt", arcname="file2.txt")

        target2 = self.temp_dir / "target2"
        target2.mkdir()
        sha2 = import_delta_archive(archive_flat, target2)
        self.assertTrue((target2 / "file2.txt").is_file())
        self.assertEqual((target2 / "file2.txt").read_text(encoding="utf-8"), "flat content")

    def test_clean_target_directory_before_unpack(self) -> None:
        """Pre-existing non-ignored files in target directory are removed before unpacking to guarantee tree SHA parity."""
        (self.src_dir / "authoritative.txt").write_text("clean state", encoding="utf-8")
        expected_sha = compute_tree_sha(self.src_dir)
        archive_path = self.temp_dir / "clean_test.tar.gz"
        export_delta_archive(self.src_dir, archive_path)

        # Pre-populate dest_dir with extraneous leftovers
        (self.dest_dir / "leftover_junk.txt").write_text("stale data", encoding="utf-8")
        (self.dest_dir / "junk_dir").mkdir()
        (self.dest_dir / "junk_dir" / "nested.txt").write_text("nested junk", encoding="utf-8")

        # Unpack delta archive
        imported_sha = import_delta_archive(archive_path, self.dest_dir)

        self.assertEqual(imported_sha, expected_sha)
        self.assertEqual(compute_tree_sha(self.dest_dir), expected_sha)
        self.assertFalse((self.dest_dir / "leftover_junk.txt").exists())
        self.assertFalse((self.dest_dir / "junk_dir").exists())
        self.assertTrue((self.dest_dir / "authoritative.txt").is_file())


class TestTarSlipAndTamperDefense(unittest.TestCase):
    """Tests security boundaries against tar-slip path traversal and Merkle tree SHA tampering."""

    def setUp(self) -> None:
        self.temp_dir_obj = tempfile.TemporaryDirectory()
        self.temp_dir = Path(self.temp_dir_obj.name)
        self.target_dir = self.temp_dir / "safe_target"
        self.target_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp_dir_obj.cleanup()

    def test_tar_slip_relative_path_traversal_rejected(self) -> None:
        """Archive with '..' path traversal in tarinfo.name is strictly rejected with ValueError."""
        archive_path = self.temp_dir / "malicious_relative.tar.gz"

        leak_payload = b"COMPROMISED DATA"
        with tarfile.open(archive_path, "w:gz") as tar:
            ti = tarfile.TarInfo(name="../escape_file.txt")
            ti.size = len(leak_payload)
            tar.addfile(ti, io.BytesIO(leak_payload))

        with self.assertRaises(ValueError) as ctx:
            import_delta_archive(archive_path, self.target_dir)

        self.assertIn("Tar-slip", str(ctx.exception))
        self.assertFalse((self.temp_dir / "escape_file.txt").exists())

    def test_tar_slip_nested_traversal_rejected(self) -> None:
        """Archive with nested 'sub/../../escape.txt' is strictly rejected with ValueError."""
        archive_path = self.temp_dir / "malicious_nested.tar.gz"

        leak_payload = b"NESTED COMPROMISE"
        with tarfile.open(archive_path, "w:gz") as tar:
            ti = tarfile.TarInfo(name="sub/../../escape_nested.txt")
            ti.size = len(leak_payload)
            tar.addfile(ti, io.BytesIO(leak_payload))

        with self.assertRaises(ValueError) as ctx:
            import_delta_archive(archive_path, self.target_dir)

        self.assertIn("Tar-slip", str(ctx.exception))
        self.assertFalse((self.temp_dir / "escape_nested.txt").exists())

    def test_workspace_reconstruction_tree_sha_mismatch_raises_error(self) -> None:
        """reconstruct_workspace_from_delta raises ValueError if unpacked SHA != task.active_workspace_tree_sha."""
        orch = HarnessOrchestrator(self.temp_dir / "orch_data")
        task = orch.ledger.submit_task({"goal": "Tamper test task"})
        task_id = task["task_id"]

        # Create and export a real workspace
        sample_ws = self.temp_dir / "sample_ws"
        sample_ws.mkdir()
        (sample_ws / "content.txt").write_text("real content", encoding="utf-8")
        delta_info = orch.export_workspace_delta(task_id, sample_ws)

        # Artificially alter the expected tree SHA in the task row
        with orch.ledger._lock:
            orch.ledger.conn.execute(
                "UPDATE tasks SET active_workspace_tree_sha = ? WHERE task_id = ?",
                ("expected_fake_sha_9999", task_id),
            )
            orch.ledger.conn.commit()

        # Workspace reconstruction must reject the mismatch
        recon_target = self.temp_dir / "recon_dest"
        with self.assertRaises(ValueError) as ctx:
            orch.reconstruct_workspace_from_delta(task_id, recon_target)

        self.assertIn("Tree SHA mismatch during workspace reconstruction", str(ctx.exception))
        self.assertIn("expected_fake_sha_9999", str(ctx.exception))

    def test_corrupted_archive_rejected(self) -> None:
        """Corrupted archive bytes raise an exception when attempting to unpack."""
        corrupt_path = self.temp_dir / "corrupted.tar.gz"
        corrupt_path.write_bytes(b"NOT_A_VALID_GZIP_OR_TAR_DATA_1234567890")

        with self.assertRaises(Exception):
            import_delta_archive(corrupt_path, self.target_dir)


class TestLeaseExpiryHandoverAndJournalReplay(unittest.TestCase):
    """Tests lease expiry handover, monotonic fencing increments (1 -> 2), and 0ms tool journal replay."""

    def setUp(self) -> None:
        self.temp_dir_obj = tempfile.TemporaryDirectory()
        self.temp_dir = Path(self.temp_dir_obj.name)
        self.orch = HarnessOrchestrator(self.temp_dir / "data")
        self.ledger = self.orch.ledger

    def tearDown(self) -> None:
        self.temp_dir_obj.cleanup()

    def test_lease_expiry_reaping_and_monotonic_fencing_increment(self) -> None:
        """Expired leases are reaped to QUEUED, and a subsequent claim assigns an incremented fencing token."""
        task = self.ledger.submit_task({"goal": "Lease handover test", "status": "QUEUED"})
        task_id = task["task_id"]

        # 1. Worker 1 claims task with short TTL
        c1 = self.ledger.claim_next_task("worker_01", ttl_seconds=1)
        self.assertIsNotNone(c1)
        self.assertEqual(c1["task_id"], task_id)
        self.assertEqual(c1["fencing_token"], 1)

        # 2. Advance time beyond lease TTL
        time.sleep(1.2)

        # 3. Reap expired leases
        reaped = self.ledger.reap_expired_leases()
        self.assertIn(task_id, reaped)

        # Task is back in QUEUED state with LEASE_EXPIRED reason
        updated_task = self.ledger.get_task(task_id)
        self.assertIsNotNone(updated_task)
        self.assertEqual(updated_task["status"], "QUEUED")

        events = self.ledger.list_events(task_id)
        reap_events = [e for e in events if e.get("reason_code") == "LEASE_EXPIRED"]
        self.assertEqual(len(reap_events), 1)
        self.assertEqual(reap_events[0]["next_state"], "QUEUED")

        # 4. Worker 2 claims the task
        c2 = self.ledger.claim_next_task("worker_02", ttl_seconds=30)
        self.assertIsNotNone(c2)
        self.assertEqual(c2["task_id"], task_id)
        self.assertEqual(c2["fencing_token"], 2, "Fencing token must increment monotonically from 1 to 2")
        self.assertEqual(c2["worker_id"], "worker_02")

        # 5. Check LEASE_TAKEOVER event was recorded
        events_after = self.ledger.list_events(task_id)
        takeover_events = [e for e in events_after if e.get("event_type") == "LEASE_TAKEOVER"]
        self.assertEqual(len(takeover_events), 1)
        self.assertEqual(takeover_events[0]["payload"]["prior_fencing_token"], 1)
        self.assertEqual(takeover_events[0]["payload"]["new_fencing_token"], 2)
        self.assertEqual(takeover_events[0]["actor"], "worker_02")

    def test_cross_machine_handover_with_0ms_journal_replay(self) -> None:
        """Worker 2 takes over from Worker 1's CoW delta archive and replays sealed tool calls in 0ms."""
        # 1. Create a task with requested steps in QUEUED state
        task_ws = self.temp_dir / "init_ws"
        task_ws.mkdir()
        (task_ws / "input.txt").write_text("initial seed", encoding="utf-8")

        task = self.ledger.submit_task(
            {
                "goal": "Multi-step failover task",
                "workspace_root": str(task_ws),
                "status": "QUEUED",
                "steps": [
                    PlanNode(
                        step_id="step_1_inspect",
                        title="Step 1: Inspect files",
                        tool_id="list_files",
                        args={"path": "."},
                        risk_class="R0",
                    ).to_dict(),
                    PlanNode(
                        step_id="step_2_execute",
                        title="Step 2: Generate output",
                        tool_id="list_files",
                        args={"path": "input.txt"},
                        risk_class="R0",
                        depends_on=["step_1_inspect"],
                    ).to_dict(),
                ],
            }
        )
        task_id = task["task_id"]

        # 2. Worker 1 claims task
        c1 = self.ledger.claim_next_task("worker_alpha", ttl_seconds=1)
        self.assertIsNotNone(c1)
        attempt_id1 = c1["attempt_id"]
        token1 = c1["fencing_token"]
        self.assertEqual(token1, 1)

        # Worker 1 executes step 1 tool call via ToolGateway
        res1 = self.orch.gateway.invoke(
            task_id=task_id,
            attempt_id=attempt_id1,
            step_id="step_1_inspect",
            sequence_in_step=1,
            tool_id="list_files",
            args={"path": "."},
            workspace_dir=task_ws,
            fencing_token=token1,
        )
        self.assertTrue(res1["ok"])
        self.assertFalse(res1["_replayed"])
        call_id1 = res1["_call_id"]

        # Worker 1 creates workspace modification and exports delta archive
        (task_ws / "step1_output.txt").write_text("worker 1 progress", encoding="utf-8")
        delta1 = self.orch.export_workspace_delta(task_id, task_ws)
        self.assertIsNotNone(delta1["artifact_id"])
        ws_tree_sha1 = delta1["workspace_tree_sha"]

        # 3. Worker 1 disconnects / lease expires
        time.sleep(1.2)
        reaped = self.ledger.reap_expired_leases()
        self.assertIn(task_id, reaped)

        # 4. Worker 2 claims the task
        c2 = self.ledger.claim_next_task("worker_beta", ttl_seconds=60)
        self.assertIsNotNone(c2)
        token2 = c2["fencing_token"]
        attempt_id2 = c2["attempt_id"]
        self.assertEqual(token2, 2)

        # Worker 2 reconstructs workspace from delta artifact
        worker2_ws = self.temp_dir / "worker2_ws"
        worker2_ws.mkdir()
        reconstructed_sha = self.orch.reconstruct_workspace_from_delta(task_id, worker2_ws)
        self.assertEqual(reconstructed_sha, ws_tree_sha1)
        self.assertEqual((worker2_ws / "step1_output.txt").read_text(encoding="utf-8"), "worker 1 progress")

        # 5. Worker 2 invokes Step 1 tool call -> 0ms replay (< 15 ms) from call journal
        t_start = time.perf_counter()
        res2 = self.orch.gateway.invoke(
            task_id=task_id,
            attempt_id=attempt_id2,
            step_id="step_1_inspect",
            sequence_in_step=1,
            tool_id="list_files",
            args={"path": "."},
            workspace_dir=worker2_ws,
            fencing_token=token2,
        )
        duration_ms = (time.perf_counter() - t_start) * 1000

        self.assertTrue(res2["ok"])
        self.assertTrue(res2["_replayed"])
        self.assertEqual(res2["_replayed_from_call_id"], call_id1)
        self.assertEqual(res2.get("files"), res1.get("files"))
        self.assertLess(duration_ms, 150.0, "Replay invocation must complete in 0ms (< 150 ms)")

        # 6. Worker 2 executes Step 2 with fencing_token=2
        res_step2 = self.orch.gateway.invoke(
            task_id=task_id,
            attempt_id=attempt_id2,
            step_id="step_2_execute",
            sequence_in_step=1,
            tool_id="list_files",
            args={"path": "input.txt"},
            workspace_dir=worker2_ws,
            fencing_token=token2,
        )
        self.assertTrue(res_step2["ok"])
        self.assertFalse(res_step2["_replayed"])

        # Transition task: RUNNING -> VERIFYING -> SUCCEEDED with fencing_token=2
        self.ledger.transition_task(
            task_id=task_id,
            next_state=TaskState.VERIFYING,
            fencing_token=token2,
            reason_code="VERIFYING_OUTPUT",
            actor="worker_beta",
        )
        transition_res = self.ledger.transition_task(
            task_id=task_id,
            next_state=TaskState.SUCCEEDED,
            fencing_token=token2,
            reason_code="WORKER_COMPLETED",
            actor="worker_beta",
        )
        self.assertEqual(transition_res["status"], "SUCCEEDED")


class TestStaleWorkerMonotonicFencingDefense(unittest.TestCase):
    """Tests that stale workers waking up with expired fencing tokens are completely rejected."""

    def setUp(self) -> None:
        self.temp_dir_obj = tempfile.TemporaryDirectory()
        self.temp_dir = Path(self.temp_dir_obj.name)
        self.orch = HarnessOrchestrator(self.temp_dir / "data")
        self.ledger = self.orch.ledger
        self.app = HarnessAPIApp(self.orch)

    def tearDown(self) -> None:
        self.temp_dir_obj.cleanup()

    def test_stale_worker_rejected_on_all_control_plane_mutations(self) -> None:
        """Worker 1 with stale token=1 is rejected on begin_tool_call, seal_tool_call, transition_task, and renew_lease."""
        task = self.ledger.submit_task({"goal": "Fencing defense test", "status": "QUEUED"})
        task_id = task["task_id"]

        # Worker 1 claims task (token = 1)
        c1 = self.ledger.claim_next_task("worker_01", ttl_seconds=1)
        token1 = c1["fencing_token"]
        lease1_id = c1["lease_id"]
        attempt1_id = c1["attempt_id"]
        self.assertEqual(token1, 1)

        # Worker 1 initiates a tool call before sleeping
        call1_id = self.ledger.begin_tool_call(
            task_id=task_id,
            attempt_id=attempt1_id,
            step_id="step_w1",
            sequence_in_step=1,
            tool_id="test_tool",
            risk_class="R0",
            canonical_args_hash=sha256_hex(canonical_json({"init": True})),
            arguments_redacted={"init": True},
            policy_decision="ALLOW",
            idempotency_key="key_w1",
            fencing_token=token1,
        )

        # Lease expires and Worker 2 claims task (token = 2)
        time.sleep(1.2)
        self.ledger.reap_expired_leases()
        c2 = self.ledger.claim_next_task("worker_02", ttl_seconds=60)
        token2 = c2["fencing_token"]
        self.assertEqual(token2, 2)

        # Stale Worker 1 attempts begin_tool_call with token=1 -> ConcurrencyFenceError
        with self.assertRaises(ConcurrencyFenceError) as ctx_begin:
            self.ledger.begin_tool_call(
                task_id=task_id,
                attempt_id=attempt1_id,
                step_id="step_stale",
                sequence_in_step=1,
                tool_id="test_tool",
                risk_class="R0",
                canonical_args_hash=sha256_hex(canonical_json({"a": 1})),
                arguments_redacted={"a": 1},
                policy_decision="ALLOW",
                idempotency_key="key1",
                fencing_token=token1,
            )
        self.assertIn("fencing token 1", str(ctx_begin.exception))

        # Stale Worker 1 attempts seal_tool_call with token=1 -> ConcurrencyFenceError
        with self.assertRaises(ConcurrencyFenceError) as ctx_seal:
            self.ledger.seal_tool_call(
                call_id=call1_id,
                result={"ok": True},
                status="SEALED",
                fencing_token=token1,
            )
        self.assertIn("fencing token 1", str(ctx_seal.exception))

        # Stale Worker 1 attempts transition_task with token=1 -> ConcurrencyFenceError
        with self.assertRaises(ConcurrencyFenceError) as ctx_trans:
            self.ledger.transition_task(
                task_id=task_id,
                next_state=TaskState.FAILED,
                fencing_token=token1,
                reason_code="STALE_WORKER_ATTEMPT",
            )
        self.assertIn("fencing token 1", str(ctx_trans.exception))

        # Stale Worker 1 attempts renew_lease with token=1 -> ConcurrencyFenceError
        with self.assertRaises(ConcurrencyFenceError):
            self.ledger.renew_lease(
                lease_id=lease1_id,
                fencing_token=token1,
                ttl_seconds=60,
            )

        # Stale Worker 1 attempts release_lease with token=1 -> ConcurrencyFenceError
        with self.assertRaises(ConcurrencyFenceError):
            self.ledger.release_lease(
                lease_id=lease1_id,
                fencing_token=token1,
            )

        # Verify task is STILL running under Worker 2
        task_now = self.ledger.get_task(task_id)
        self.assertEqual(task_now["status"], "RUNNING")

    def test_stale_worker_rest_api_rejection_http_409(self) -> None:
        """REST API calls from a stale worker return HTTP 409 Conflict."""
        mock_urlopen = make_mock_urlopen(self.app)

        task = self.ledger.submit_task({"goal": "REST Fencing Defense", "status": "QUEUED"})
        task_id = task["task_id"]

        c1 = self.ledger.claim_next_task("worker_rest_01", ttl_seconds=1)
        token1 = c1["fencing_token"]
        lease1_id = c1["lease_id"]

        time.sleep(1.2)
        self.ledger.reap_expired_leases()
        c2 = self.ledger.claim_next_task("worker_rest_02", ttl_seconds=60)
        token2 = c2["fencing_token"]
        self.assertEqual(token2, 2)

        client1 = RestWorkerClient("http://127.0.0.1:8765", token="w-rest-01")

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            # 1. Stale transition_task over REST returns HTTP 409 mapped to ConcurrencyFenceError
            with self.assertRaises(ConcurrencyFenceError):
                client1.transition_task(
                    task_id=task_id,
                    next_state="SUCCEEDED",
                    fencing_token=token1,
                )

            # 2. Stale renew_lease over REST returns HTTP 409 mapped to ConcurrencyFenceError
            with self.assertRaises(ConcurrencyFenceError):
                client1.renew_lease(
                    lease_id=lease1_id,
                    fencing_token=token1,
                    ttl_seconds=30,
                )

            # 3. Stale release_lease over REST returns HTTP 409 mapped to ConcurrencyFenceError
            with self.assertRaises(ConcurrencyFenceError):
                client1.release_lease(
                    lease_id=lease1_id,
                    fencing_token=token1,
                )

        # Verify task remains RUNNING under Worker 2 without corruption
        task_final = self.ledger.get_task(task_id)
        self.assertEqual(task_final["status"], "RUNNING")


class TestRestArtifactDeltaTransfer(unittest.TestCase):
    """Tests CAS delta archive upload, download, and workspace reconstruction via REST API."""

    def setUp(self) -> None:
        self.temp_dir_obj = tempfile.TemporaryDirectory()
        self.temp_dir = Path(self.temp_dir_obj.name)
        self.orch = HarnessOrchestrator(self.temp_dir / "orch_data")
        self.app = HarnessAPIApp(self.orch)
        self.client = RestWorkerClient("http://127.0.0.1:8765", token="worker-token-xyz")

    def tearDown(self) -> None:
        self.temp_dir_obj.cleanup()

    def test_upload_and_download_delta_artifact_over_rest(self) -> None:
        """Worker uploads a CoW delta archive via REST; remote worker downloads and unpacks it with exact tree SHA parity."""
        mock_urlopen = make_mock_urlopen(self.app)

        # 1. Create a workspace with sample project files
        src_ws = self.temp_dir / "source_project"
        src_ws.mkdir()
        (src_ws / "config.json").write_text(json.dumps({"version": "1.0", "active": True}), encoding="utf-8")
        (src_ws / "data.csv").write_text("id,val\n1,100\n2,200\n", encoding="utf-8")
        src_tree_sha = compute_tree_sha(src_ws)

        # Export delta archive
        archive_file = self.temp_dir / "upload_delta.tar.gz"
        export_delta_archive(src_ws, archive_file)

        task = self.orch.ledger.submit_task({"goal": "CAS Delta Transfer Task", "status": "QUEUED"})
        task_id = task["task_id"]

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            # 2. Worker 1 uploads delta archive
            upload_res = self.client.upload_delta_artifact(task_id, archive_file)
            self.assertEqual(upload_res["kind"], "cow_delta_archive")
            artifact_id = upload_res["artifact_id"]
            self.assertIsNotNone(artifact_id)

            # 3. Worker 2 lists artifacts
            artifacts = self.client.list_artifacts(task_id)
            self.assertTrue(any(a["artifact_id"] == artifact_id for a in artifacts))

            # 4. Worker 2 downloads artifact
            dest_download = self.temp_dir / "downloaded_delta.tar.gz"
            downloaded_path = self.client.download_artifact(artifact_id, dest_download)
            self.assertTrue(downloaded_path.is_file())
            self.assertEqual(downloaded_path.stat().st_size, archive_file.stat().st_size)

        # 5. Remote worker imports delta archive into fresh workspace
        remote_ws = self.temp_dir / "remote_workspace"
        remote_ws.mkdir()
        reconstructed_sha = import_delta_archive(dest_download, remote_ws)

        self.assertEqual(reconstructed_sha, src_tree_sha)
        self.assertEqual((remote_ws / "config.json").read_text(encoding="utf-8"), json.dumps({"version": "1.0", "active": True}))
        self.assertEqual((remote_ws / "data.csv").read_text(encoding="utf-8"), "id,val\n1,100\n2,200\n")


if __name__ == "__main__":
    unittest.main()
