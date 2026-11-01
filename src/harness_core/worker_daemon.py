"""Distributed Worker Daemon & Client Transports (PRD Section 11, Plan 06-01).

Implements the standalone `WorkerDaemon` architecture, machine profiling,
heartbeat thread for CAS lease renewals, atomic task claiming loop, and two
transport adapters:
- `DirectLedgerWorkerClient`: in-process local execution via `LedgerStore` and `HarnessOrchestrator`.
- `RestWorkerClient`: out-of-process HTTP execution via standard library `urllib.request`.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Protocol, runtime_checkable

from .cow import get_default_snapshot_provider
from .db import ConcurrencyFenceError, LedgerStore
from .models import TaskState, TaskSubmitRequest
from .orchestrator import HarnessOrchestrator

logger = logging.getLogger(__name__)


def inspect_machine_profile(override_class: str | None = None) -> dict[str, Any]:
    """Auto-detects host hardware specs, capabilities, CoW backend, and local models."""
    sys_name = platform.system()
    machine = platform.machine().lower()

    if override_class and override_class != "auto":
        machine_class = override_class
    elif sys_name == "Darwin" and machine in ("arm64", "aarch64"):
        machine_class = "m4_air"
    elif sys_name == "Darwin":
        machine_class = "desktop_gpu"
    elif sys_name == "Linux":
        # Check if GPU is present or high CPU count
        has_gpu = False
        try:
            res = subprocess.run(["which", "nvidia-smi"], capture_output=True, text=True, check=False)
            has_gpu = res.returncode == 0
        except Exception:
            has_gpu = False
        machine_class = "desktop_gpu" if has_gpu else "server"
    else:
        machine_class = "server"

    # CoW backend detection
    try:
        cow_provider = get_default_snapshot_provider()
        cow_backend = cow_provider.backend_name
    except Exception:
        cow_backend = "apfs_clone" if sys_name == "Darwin" else "portable_copy"

    # CPU and Memory detection
    cpu_count = os.cpu_count() or 1
    memory_bytes = 16 * 1024 * 1024 * 1024  # Default 16 GB fallback
    if sys_name == "Darwin":
        try:
            out = subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True)
            memory_bytes = int(out.strip())
        except Exception:
            pass
    elif sys_name == "Linux":
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        memory_bytes = kb * 1024
                        break
        except Exception:
            pass

    capabilities = {
        "cpu_cores": cpu_count,
        "memory_bytes": memory_bytes,
        "os_name": sys_name,
        "os_release": platform.release(),
        "python_version": platform.python_version(),
        "arch": machine,
    }

    # Detect local models
    local_llm_models: list[str] = []
    if sys_name == "Darwin" and machine in ("arm64", "aarch64"):
        local_llm_models.append("mlx-community/Qwen2.5-Coder-7B-Instruct-4bit")

    return {
        "machine_class": machine_class,
        "cow_backend": cow_backend,
        "capabilities": capabilities,
        "local_llm_models": local_llm_models,
        "trust_level": "trusted_local",
    }


@runtime_checkable
class WorkerClient(Protocol):
    """Protocol for worker transport clients communicating with the harness ledger or control plane."""

    def register_worker(
        self,
        worker_id: str,
        machine_class: str = "m4_air",
        cow_backend: str = "apfs_clone",
        capabilities: dict[str, Any] | None = None,
        local_llm_models: list[str] | None = None,
        trust_level: str = "trusted_local",
    ) -> dict[str, Any]: ...

    def heartbeat(self, worker_id: str, health: str = "HEALTHY") -> dict[str, Any]: ...

    def claim_task(self, worker_id: str, ttl_seconds: int = 300) -> dict[str, Any] | None: ...

    def renew_lease(self, lease_id: str, fencing_token: int, ttl_seconds: int = 300) -> dict[str, Any]: ...

    def release_lease(self, lease_id: str, fencing_token: int | None = None) -> dict[str, Any]: ...

    def get_task(self, task_id: str) -> dict[str, Any] | None: ...

    def transition_task(
        self,
        task_id: str,
        next_state: TaskState | str,
        reason_code: str = "WORKER_UPDATE",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def execute_task(self, task_id: str, worker_id: str | None = None) -> dict[str, Any]: ...


class DirectLedgerWorkerClient:
    """In-process client interacting directly with `LedgerStore` and `HarnessOrchestrator`."""

    def __init__(self, orchestrator: HarnessOrchestrator | LedgerStore):
        if isinstance(orchestrator, HarnessOrchestrator):
            self.orch: HarnessOrchestrator | None = orchestrator
            self.ledger: LedgerStore = orchestrator.ledger
        else:
            self.orch = None
            self.ledger = orchestrator

    def register_worker(
        self,
        worker_id: str,
        machine_class: str = "m4_air",
        cow_backend: str = "apfs_clone",
        capabilities: dict[str, Any] | None = None,
        local_llm_models: list[str] | None = None,
        trust_level: str = "trusted_local",
    ) -> dict[str, Any]:
        return self.ledger.register_worker(
            worker_id=worker_id,
            machine_class=machine_class,
            cow_backend=cow_backend,
            capabilities=capabilities,
            local_llm_models=local_llm_models,
            trust_level=trust_level,
        )

    def heartbeat(self, worker_id: str, health: str = "HEALTHY") -> dict[str, Any]:
        return self.ledger.heartbeat_worker(worker_id=worker_id, health=health)

    def claim_task(self, worker_id: str, ttl_seconds: int = 300) -> dict[str, Any] | None:
        return self.ledger.claim_next_task(worker_id=worker_id, ttl_seconds=ttl_seconds)

    def renew_lease(self, lease_id: str, fencing_token: int, ttl_seconds: int = 300) -> dict[str, Any]:
        return self.ledger.renew_lease(lease_id=lease_id, fencing_token=fencing_token, ttl_seconds=ttl_seconds)

    def release_lease(self, lease_id: str, fencing_token: int | None = None) -> dict[str, Any]:
        return self.ledger.release_lease(lease_id=lease_id, fencing_token=fencing_token)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        return self.ledger.get_task(task_id)

    def transition_task(
        self,
        task_id: str,
        next_state: TaskState | str,
        reason_code: str = "WORKER_UPDATE",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = next_state if isinstance(next_state, TaskState) else TaskState(next_state)
        return self.ledger.transition_task(task_id=task_id, next_state=state, reason_code=reason_code, payload=payload)

    def execute_task(self, task_id: str, worker_id: str | None = None) -> dict[str, Any]:
        if self.orch is not None:
            return self.orch.execute_task(task_id, worker_id=worker_id)
        return self.transition_task(task_id, TaskState.SUCCEEDED, reason_code="WORKER_DIRECT_EXECUTION")


class RestWorkerClient:
    """Out-of-process client connecting to a remote control plane via HTTP `/v1/*` endpoints."""

    def __init__(self, server_url: str, token: str | None = None, timeout: float = 15.0):
        self.server_url = server_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.server_url}{path}"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.token:
            headers["X-Worker-Token"] = self.token
            headers["Authorization"] = f"Bearer {self.token}"

        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body) if body.strip() else {}
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8")
            err_json: dict[str, Any] = {}
            try:
                err_json = json.loads(err_body)
            except Exception:
                pass
            err_msg = err_json.get("detail") or err_json.get("error") or err_body or str(exc)

            if exc.code == 409:
                raise ConcurrencyFenceError(f"ConcurrencyFenceError: {err_msg}")
            if exc.code == 401:
                raise PermissionError(f"HTTP 401 Unauthorized: {err_msg}")
            if exc.code == 404:
                raise KeyError(f"HTTP 404 Not Found: {err_msg}")
            raise RuntimeError(f"HTTP {exc.code} Error: {err_msg}")

    def register_worker(
        self,
        worker_id: str,
        machine_class: str = "m4_air",
        cow_backend: str = "apfs_clone",
        capabilities: dict[str, Any] | None = None,
        local_llm_models: list[str] | None = None,
        trust_level: str = "trusted_local",
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/workers/register",
            {
                "worker_id": worker_id,
                "machine_class": machine_class,
                "cow_backend": cow_backend,
                "capabilities": capabilities or {},
                "local_llm_models": local_llm_models or [],
                "trust_level": trust_level,
            },
        )

    def heartbeat(self, worker_id: str, health: str = "HEALTHY") -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/workers/heartbeat",
            {"worker_id": worker_id, "health": health},
        )

    def claim_task(self, worker_id: str, ttl_seconds: int = 300) -> dict[str, Any] | None:
        res = self._request(
            "POST",
            "/v1/tasks/claim",
            {"worker_id": worker_id, "ttl_seconds": ttl_seconds},
        )
        if not res or ("task" in res and res["task"] is None) or "task_id" not in res:
            return None
        return res

    def renew_lease(self, lease_id: str, fencing_token: int, ttl_seconds: int = 300) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/leases/{lease_id}:renew",
            {"fencing_token": fencing_token, "ttl_seconds": ttl_seconds},
        )

    def release_lease(self, lease_id: str, fencing_token: int | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if fencing_token is not None:
            payload["fencing_token"] = fencing_token
        return self._request(
            "POST",
            f"/v1/leases/{lease_id}:release",
            payload,
        )

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        return self._request("GET", f"/v1/tasks/{task_id}")

    def transition_task(
        self,
        task_id: str,
        next_state: TaskState | str,
        reason_code: str = "WORKER_UPDATE",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state_str = next_state.value if isinstance(next_state, TaskState) else str(next_state)
        # Check if pause/resume/cancel endpoint
        action = None
        if state_str == TaskState.PAUSED.value:
            action = "pause"
        elif state_str == TaskState.CANCELED.value:
            action = "cancel"

        if action:
            return self._request("POST", f"/v1/tasks/{task_id}:{action}", payload or {})
        return self._request("GET", f"/v1/tasks/{task_id}")

    def execute_task(self, task_id: str, worker_id: str | None = None) -> dict[str, Any]:
        # For remote REST workers, check task status
        return self.get_task(task_id) or {}


class WorkerDaemon:
    """Autonomous worker daemon polling for queued tasks and maintaining CAS lease heartbeats."""

    def __init__(
        self,
        client: WorkerClient,
        worker_id: str | None = None,
        machine_class: str = "auto",
        poll_interval: float = 2.0,
        lease_ttl: int = 300,
        orchestrator: HarnessOrchestrator | None = None,
    ):
        self.client = client
        hostname = socket.gethostname().split(".")[0]
        self.worker_id = worker_id or f"worker-{hostname}-{uuid.uuid4().hex[:6]}"
        self.profile = inspect_machine_profile(override_class=machine_class)
        self.machine_class = self.profile["machine_class"]
        self.poll_interval = max(0.1, poll_interval)
        self.lease_ttl = max(5, lease_ttl)
        self.orchestrator = orchestrator or getattr(client, "orch", None)

        self._stop_event = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self.current_task_id: str | None = None
        self.current_lease_id: str | None = None
        self.current_fencing_token: int | None = None
        self.fenced: bool = False
        self._is_started: bool = False

    def start(self) -> None:
        """Registers with the control plane and starts the background heartbeat loop."""
        if self._is_started:
            return
        self.client.register_worker(
            worker_id=self.worker_id,
            machine_class=self.machine_class,
            cow_backend=self.profile["cow_backend"],
            capabilities=self.profile["capabilities"],
            local_llm_models=self.profile["local_llm_models"],
            trust_level=self.profile["trust_level"],
        )
        self._stop_event.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            daemon=True,
            name=f"heartbeat-{self.worker_id}",
        )
        self._heartbeat_thread.start()
        self._is_started = True

    def _heartbeat_loop(self) -> None:
        """Periodically reports health and renews active CAS leases."""
        beat_interval = max(0.2, self.lease_ttl / 3.0)
        while not self._stop_event.is_set():
            if self._stop_event.wait(timeout=beat_interval):
                break
            try:
                self.client.heartbeat(self.worker_id)
                if self.current_lease_id is not None and self.current_fencing_token is not None:
                    self.client.renew_lease(
                        lease_id=self.current_lease_id,
                        fencing_token=self.current_fencing_token,
                        ttl_seconds=self.lease_ttl,
                    )
            except ConcurrencyFenceError as exc:
                self.fenced = True
                logger.warning(
                    "Worker %s was fenced out on lease %s (token %s): %s",
                    self.worker_id,
                    self.current_lease_id,
                    self.current_fencing_token,
                    exc,
                )
            except Exception as exc:
                logger.error("Heartbeat error on worker %s: %s", self.worker_id, exc)

    def _claim_loop(self, once: bool = False) -> None:
        """Continuously claims tasks from the queue and executes them."""
        while not self._stop_event.is_set():
            try:
                claimed = self.client.claim_task(self.worker_id, ttl_seconds=self.lease_ttl)
                if claimed:
                    task_id = claimed["task_id"]
                    self.current_task_id = task_id
                    self.current_lease_id = claimed.get("lease_id")
                    self.current_fencing_token = claimed.get("fencing_token")
                    self.fenced = False

                    try:
                        self.execute_claimed_task(claimed)
                    finally:
                        self.current_task_id = None
                        self.current_lease_id = None
                        self.current_fencing_token = None

                    if once:
                        break
                else:
                    if once:
                        break
                    self._stop_event.wait(timeout=self.poll_interval)
            except Exception as exc:
                logger.error("Task claim loop error on %s: %s", self.worker_id, exc)
                if once:
                    break
                self._stop_event.wait(timeout=self.poll_interval)

    def execute_claimed_task(self, task: dict[str, Any]) -> dict[str, Any]:
        """Executes a claimed task using local orchestrator or delegated client."""
        task_id = task["task_id"]
        if self.orchestrator is not None:
            return self.orchestrator.execute_task(task_id, worker_id=self.worker_id)
        return self.client.execute_task(task_id, worker_id=self.worker_id)

    def stop(self) -> None:
        """Gracefully shuts down the daemon, releasing active leases."""
        self._stop_event.set()
        if self.current_lease_id is not None:
            try:
                self.client.release_lease(self.current_lease_id, self.current_fencing_token)
            except Exception as exc:
                logger.warning("Failed to release lease %s during shutdown: %s", self.current_lease_id, exc)
            self.current_lease_id = None
            self.current_fencing_token = None
            self.current_task_id = None

        if self._heartbeat_thread is not None and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=3.0)
        self._is_started = False

    def run(self, once: bool = False) -> None:
        """Runs the worker daemon lifecycle until stopped or interrupted."""
        try:
            if threading.current_thread() is threading.main_thread():
                def _handle_signal(sig: int, frame: Any) -> None:
                    self.stop()
                signal.signal(signal.SIGINT, _handle_signal)
                signal.signal(signal.SIGTERM, _handle_signal)
        except (ValueError, AttributeError):
            pass

        self.start()
        try:
            self._claim_loop(once=once)
        finally:
            self.stop()
