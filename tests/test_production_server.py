"""Unit and integration tests for production ASGI server, health checks, and graceful draining (Phase 07 Plan 02)."""

from __future__ import annotations

import asyncio
import json
import signal
import tempfile
import threading
import time
import unittest
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

from harness_core.asgi import HarnessASGIAdapter, get_asgi_app
from harness_core.orchestrator import HarnessOrchestrator
from harness_core.server import GracefulServerRunner, HarnessAPIApp


class TestProductionHealthChecks(unittest.TestCase):
    """Validates /livez and /healthz endpoint behaviors across healthy, degraded, and draining states."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.orch = HarnessOrchestrator(self.temp_dir.name)
        self.app = HarnessAPIApp(self.orch)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_livez_endpoint(self) -> None:
        """Verifies /livez returns 200 OK with uptime and version."""
        status, payload, content_type = self.app.handle_request("GET", "/livez")
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(content_type, "application/json")
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload["status"], "alive")
        self.assertEqual(payload["version"], "1.0.0")
        self.assertGreaterEqual(payload["uptime_seconds"], 0.0)

    def test_healthz_healthy_state(self) -> None:
        """Verifies /healthz returns 200 OK with active database, cow storage, and worker metrics."""
        # Register a worker with recent heartbeat
        self.orch.ledger.register_worker(
            worker_id="test-worker-01",
            machine_class="m4_air",
            cow_backend="apfs_clone",
        )
        status, payload, content_type = self.app.handle_request("GET", "/healthz")
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(content_type, "application/json")
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload["status"], "healthy")

        components = payload["components"]
        self.assertEqual(components["database"], "ok")
        self.assertEqual(components["cow_storage"], "ok")
        self.assertIsInstance(components["workers"], dict)
        self.assertEqual(components["workers"]["status"], "ok")
        self.assertGreaterEqual(components["workers"]["total"], 1)
        self.assertGreaterEqual(components["workers"]["healthy"], 1)
        self.assertEqual(components["workers"]["total"], components["workers"]["healthy"])

    def test_healthz_degraded_database(self) -> None:
        """Verifies /healthz returns 503 when the database fails."""
        with patch.object(self.orch.ledger, "list_workers", side_effect=RuntimeError("DB connection lost")):
            status, payload, _ = self.app.handle_request("GET", "/healthz")
            self.assertEqual(status, HTTPStatus.SERVICE_UNAVAILABLE)
            self.assertEqual(payload["status"], "unhealthy")
            self.assertEqual(payload["components"]["database"]["status"], "error")
            self.assertIn("DB connection lost", payload["components"]["database"]["error"])

    def test_healthz_degraded_cow_storage(self) -> None:
        """Verifies /healthz returns 503 when CoW storage write canary fails."""
        with patch.object(Path, "write_text", side_effect=PermissionError("Read-only filesystem")):
            status, payload, _ = self.app.handle_request("GET", "/healthz")
            self.assertEqual(status, HTTPStatus.SERVICE_UNAVAILABLE)
            self.assertEqual(payload["status"], "unhealthy")
            self.assertEqual(payload["components"]["cow_storage"]["status"], "error")
            self.assertIn("Read-only filesystem", payload["components"]["cow_storage"]["error"])

    def test_healthz_and_livez_when_draining(self) -> None:
        """Verifies /healthz returns 503 when draining while /livez remains 200."""
        self.app.is_draining = True

        # /healthz must immediately return 503 Service Unavailable
        s_health, p_health, _ = self.app.handle_request("GET", "/healthz")
        self.assertEqual(s_health, HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertEqual(p_health["status"], "draining")
        self.assertIn("shutting down", p_health["error"])

        # /livez must continue returning 200 OK
        s_live, p_live, _ = self.app.handle_request("GET", "/livez")
        self.assertEqual(s_live, HTTPStatus.OK)
        self.assertEqual(p_live["status"], "alive")


class TestASGIAdapter(unittest.TestCase):
    """Validates ASGI 3.0 adapter handling HTTP requests, SSE streaming, and lifespan protocols."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.orch = HarnessOrchestrator(self.temp_dir.name)
        self.adapter = get_asgi_app(orchestrator=self.orch)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _run_async(self, coro):
        return asyncio.run(coro)

    async def _make_asgi_request(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
    ):
        req_headers = []
        if headers:
            for k, v in headers.items():
                req_headers.append((k.lower().encode("latin-1"), v.encode("latin-1")))

        parsed = urlparse(path)
        scope = {
            "type": "http",
            "method": method,
            "path": parsed.path,
            "query_string": parsed.query.encode("latin-1"),
            "headers": req_headers,
        }

        body_sent = False

        async def receive():
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        sent_messages = []

        async def send(message):
            sent_messages.append(message)

        await self.adapter(scope, receive, send)

        start_msg = next((m for m in sent_messages if m["type"] == "http.response.start"), None)
        body_msg = next((m for m in sent_messages if m["type"] == "http.response.body"), None)
        return start_msg, body_msg

    def test_asgi_get_livez(self) -> None:
        """Tests GET /livez via ASGI protocol."""
        async def run():
            start, body = await self._make_asgi_request("GET", "/livez")
            self.assertIsNotNone(start)
            self.assertEqual(start["status"], 200)
            data = json.loads(body["body"].decode("utf-8"))
            self.assertEqual(data["status"], "alive")
            self.assertEqual(data["version"], "1.0.0")

        self._run_async(run())

    def test_asgi_get_healthz(self) -> None:
        """Tests GET /healthz via ASGI protocol."""
        async def run():
            start, body = await self._make_asgi_request("GET", "/healthz")
            self.assertIsNotNone(start)
            self.assertEqual(start["status"], 200)
            data = json.loads(body["body"].decode("utf-8"))
            self.assertEqual(data["status"], "healthy")
            self.assertEqual(data["components"]["database"], "ok")

        self._run_async(run())

    def test_asgi_get_dashboard_root(self) -> None:
        """Tests GET / dashboard via ASGI protocol."""
        async def run():
            start, body = await self._make_asgi_request("GET", "/")
            self.assertIsNotNone(start)
            self.assertEqual(start["status"], 200)
            content = body["body"].decode("utf-8")
            self.assertIn("<!DOCTYPE html>", content)

        self._run_async(run())

    def test_asgi_post_task_submission(self) -> None:
        """Tests POST /v1/tasks submission via ASGI protocol."""
        async def run():
            payload = json.dumps({"goal": "Test ASGI submission", "auto_execute": False}).encode("utf-8")
            headers = {"content-type": "application/json"}
            start, body = await self._make_asgi_request("POST", "/v1/tasks", headers=headers, body=payload)
            self.assertIsNotNone(start)
            self.assertEqual(start["status"], 201)
            data = json.loads(body["body"].decode("utf-8"))
            self.assertIn("task_id", data)
            self.assertEqual(data["goal"], "Test ASGI submission")

        self._run_async(run())

    def test_asgi_lifespan_startup_and_shutdown(self) -> None:
        """Tests ASGI lifespan startup verification and graceful shutdown draining."""
        async def run():
            scope = {"type": "lifespan"}
            messages = [
                {"type": "lifespan.startup"},
                {"type": "lifespan.shutdown"},
            ]
            idx = 0

            async def receive():
                nonlocal idx
                msg = messages[idx]
                idx += 1
                return msg

            sent = []

            async def send(msg):
                sent.append(msg)

            await self.adapter(scope, receive, send)

            self.assertEqual(len(sent), 2)
            self.assertEqual(sent[0]["type"], "lifespan.startup.complete")
            self.assertEqual(sent[1]["type"], "lifespan.shutdown.complete")
            self.assertTrue(self.adapter.api_app.is_draining)

        self._run_async(run())


class TestGracefulServerRunner(unittest.TestCase):
    """Validates GracefulServerRunner signal interception, drain transition, and clean shutdown."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.orch = HarnessOrchestrator(self.temp_dir.name)
        self.app = HarnessAPIApp(self.orch)

        # Allocate ephemeral port
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        self.runner = GracefulServerRunner(self.server, self.app, drain_timeout=0.05)

    def tearDown(self) -> None:
        try:
            self.server.server_close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def test_signal_handling_initiates_drain(self) -> None:
        """Verifies _on_signal marks app as draining and sets stop event."""
        self.assertFalse(self.app.is_draining)
        self.runner._on_signal(signal.SIGTERM, None)
        self.assertTrue(self.app.is_draining)
        self.assertTrue(self.runner._stop_event.is_set())

    def test_runner_run_and_clean_stop(self) -> None:
        """Verifies runner starts server thread and shuts down cleanly on stop()."""
        server_thread = threading.Thread(target=self.runner.run, daemon=True)
        server_thread.start()

        # Give server thread a moment to start
        time.sleep(0.1)
        self.assertTrue(self.runner.is_running)

        # Trigger programmatic stop
        self.runner.stop()
        server_thread.join(timeout=2.0)

        self.assertFalse(server_thread.is_alive())
        self.assertFalse(self.runner.is_running)
        self.assertTrue(self.app.is_draining)


if __name__ == "__main__":
    unittest.main()
