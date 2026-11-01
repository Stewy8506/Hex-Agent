"""Production ASGI 3.0 Adapter for Autonomous AI Harness (PRD Section 15.1, 15.2).

Enables running HarnessAPIApp with standard ASGI servers such as Uvicorn or Hypercorn,
supporting HTTP request handling, SSE event streaming, and ASGI lifespan protocol (startup/shutdown).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .orchestrator import HarnessOrchestrator
from .server import HarnessAPIApp


class HarnessASGIAdapter:
    """ASGI 3.0 Adapter wrapping HarnessAPIApp for uvicorn and ASGI runtimes."""

    def __init__(
        self,
        orchestrator: HarnessOrchestrator | None = None,
        api_app: HarnessAPIApp | None = None,
        auth_token: str | None = None,
        data_dir: str | Path | None = None,
    ):
        self._orchestrator = orchestrator
        self._api_app = api_app
        self.auth_token = auth_token
        self.data_dir = data_dir

    @property
    def api_app(self) -> HarnessAPIApp:
        if self._api_app is None:
            if self._orchestrator is None:
                d = self.data_dir or (Path.home() / ".autonomous_harness")
                self._orchestrator = HarnessOrchestrator(d)
            self._api_app = HarnessAPIApp(self._orchestrator, auth_token=self.auth_token)
        return self._api_app

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Any],
        send: Callable[[dict[str, Any]], Any],
    ) -> None:
        """ASGI 3.0 interface entrypoint."""
        scope_type = scope.get("type")

        if scope_type == "lifespan":
            await self._handle_lifespan(scope, receive, send)
        elif scope_type == "http":
            await self._handle_http(scope, receive, send)
        else:
            raise NotImplementedError(f"Unsupported ASGI scope type: {scope_type}")

    async def _handle_lifespan(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Any],
        send: Callable[[dict[str, Any]], Any],
    ) -> None:
        while True:
            message = await receive()
            msg_type = message.get("type")
            if msg_type == "lifespan.startup":
                try:
                    # Actively verify database readiness
                    self.api_app.orch.ledger.list_workers()
                    await send({"type": "lifespan.startup.complete"})
                except Exception as exc:
                    await send({"type": "lifespan.startup.failed", "message": str(exc)})
            elif msg_type == "lifespan.shutdown":
                try:
                    self.api_app.is_draining = True
                    await send({"type": "lifespan.shutdown.complete"})
                    break
                except Exception as exc:
                    await send({"type": "lifespan.shutdown.failed", "message": str(exc)})
                    break

    async def _handle_http(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Any],
        send: Callable[[dict[str, Any]], Any],
    ) -> None:
        # 1. Read request body
        body_bytes = bytearray()
        while True:
            message = await receive()
            if message.get("type") == "http.request":
                body_bytes.extend(message.get("body", b""))
                if not message.get("more_body", False):
                    break
            elif message.get("type") == "http.disconnect":
                return

        method = scope.get("method", "GET").upper()
        path = scope.get("path", "/")
        query_string = scope.get("query_string", b"").decode("latin-1")
        path_with_query = f"{path}?{query_string}" if query_string else path

        headers_dict: dict[str, str] = {}
        for k, v in scope.get("headers", []):
            try:
                headers_dict[k.decode("latin-1").lower()] = v.decode("latin-1")
            except Exception:
                pass

        content_type = headers_dict.get("content-type", "")
        raw = bytes(body_bytes)
        body_input: dict[str, Any] | bytes = {}

        if "application/json" in content_type:
            if raw:
                try:
                    body_input = json.loads(raw.decode("utf-8"))
                except Exception:
                    body_input = {}
        elif "gzip" in content_type or "octet-stream" in content_type or "/v1/artifacts/delta" in path:
            body_input = raw
        else:
            if raw:
                try:
                    body_input = json.loads(raw.decode("utf-8"))
                except Exception:
                    body_input = raw

        # Optional SSE streaming
        if method == "GET" and "stream=true" in path_with_query:
            status, payload, _ = self.api_app.handle_request(method, path_with_query, body_input, headers_dict)
            sse_headers = [
                (b"content-type", b"text/event-stream"),
                (b"cache-control", b"no-cache"),
            ]
            await send({"type": "http.response.start", "status": int(status), "headers": sse_headers})
            data_str = json.dumps(payload, default=str)
            chunk = f"event: task_events\ndata: {data_str}\n\n".encode("utf-8")
            await send({"type": "http.response.body", "body": chunk, "more_body": False})
            return

        status, payload, res_type = self.api_app.handle_request(method, path_with_query, body_input, headers_dict)

        response_headers: list[tuple[bytes, bytes]] = [
            (b"content-type", res_type.encode("latin-1")),
        ]
        if res_type == "application/gzip":
            response_headers.append((b"content-disposition", b'attachment; filename="artifact.tar.gz"'))

        if isinstance(payload, bytes):
            resp_body = payload
        elif isinstance(payload, str):
            resp_body = payload.encode("utf-8")
        else:
            resp_body = json.dumps(payload, indent=2, default=str).encode("utf-8")

        response_headers.append((b"content-length", str(len(resp_body)).encode("latin-1")))

        await send({
            "type": "http.response.start",
            "status": int(status),
            "headers": response_headers,
        })
        await send({
            "type": "http.response.body",
            "body": resp_body,
            "more_body": False,
        })


def get_asgi_app(
    data_dir: str | Path | None = None,
    auth_token: str | None = None,
    orchestrator: HarnessOrchestrator | None = None,
) -> HarnessASGIAdapter:
    """Factory creating a HarnessASGIAdapter."""
    return HarnessASGIAdapter(orchestrator=orchestrator, auth_token=auth_token, data_dir=data_dir)


# Module-level ASGI application entrypoint
app = get_asgi_app()
