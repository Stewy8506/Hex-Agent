# `runtime` (`harness_core.runtime`)

**Stateful Python code-execution kernels, PTY / Job-Object background process supervision, zero-dependency cron scheduling, cross-platform interval daemon services, and distributed task worker daemons.**

Mapped to `harness_core.runtime` in [`pyproject.toml`](../pyproject.toml), `runtime/` handles long-lived execution environments, background tasks, and host service orchestration.

## Modules

| File | Purpose |
| :--- | :--- |
| `__init__.py` | PEP 562 lazy exports for 5 public submodules (`process_registry`, `code_kernel`, `daemon`, `worker_daemon`, `scheduler`). Note: `windows_job.py` is internal helper code omitted from `__all__` and imported directly by `code_kernel.py` and `process_registry.py`. |
| `code_kernel.py` | `CodeKernel`, `KernelRegistry` (`max_kernels=4`, `idle_timeout=1800.0`), `get_kernel_registry()`, `reset_kernel_registry()`, `clean_text()`, `stale_modules_warning()`, `RUNNER_CELL_SOURCE`, `KERNEL_RUNNER_SOURCE`. Spawns persistent Python REPL subprocesses framed with sentinel headers (`\n<sentinel> <byte-length>\n<json>`), tracks imported workspace module mtimes (`stale_modules`), enforces `_RUNNER_CAPTURE_BYTES=1_000_000`, `_SPILL_CAP=5_000_000`, `_MAX_STDOUT_BYTES=1_000_000`, `_MAX_STDERR_BYTES=250_000`, and terminates whole process groups on timeout. |
| `process_registry.py` | `ProcessRegistry`, `ProcessSession`, `CircularOutputBuffer` (`max_chars=200_000`), `ProcessStatus` (`running`, `completed`, `failed`, `killed`), `strip_ansi()`, `get_process_registry()`, `reset_process_registry()`. Manages background shell processes with POSIX `pty` allocation (pipe fallback) or Windows Job Objects, async stream reader threads, and `atexit` teardown. |
| `windows_job.py` | `WindowsJob`, `_WindowsProcess`, `spawn_windows_process()`, `close_windows_job()`, `close_windows_process()`. Uses `ctypes` `kernel32` to spawn processes suspended (`0x4`), assign kill-on-close (`0x2000`) Job Objects, and resume the thread race-free so child trees cannot leak. |
| `scheduler.py` | `TaskScheduler`, `ScheduledJob`, and `CronParser`. Zero-dependency 5-field Vixie-style cron parser, atomic JSON persistence (`$HEX_SCHEDULES_FILE` > `$HEX_DATA_DIR/schedules.json` > `~/.hex/schedules.json`), non-blocking `.lock` advisory locking (`fcntl.flock` / `msvcrt.locking`), durable pre-execution claim tracking, execution through `HarnessOrchestrator.submit_and_run()`, `WebhookNotifier` failure alerts, and idle `run_cognition_maintenance()`. |
| `daemon.py` | `DaemonConfig` (`service_name="ai.hex.daemon"`, `hex_args=["schedule", "run-due"]`, `interval_seconds=60`, `keep_alive=False`) and `DaemonInstaller`. Generates, installs, inspects, and removes interval-triggered services (non-`KeepAlive` one-shot timers) across macOS `launchd` (`~/Library/LaunchAgents/<service>.plist`), Linux `systemd` (`~/.config/systemd/user/` or `/etc/systemd/system/` `.service` + `.timer`), and Windows `schtasks` XML, writing logs/state under `~/.hex/logs` and `~/.hex/data`. |
| `worker_daemon.py` | `WorkerDaemon`, `WorkerClient`, `DirectLedgerWorkerClient` (in-process `LedgerStore`/`HarnessOrchestrator`), `RestWorkerClient` (stdlib `urllib.request` client against `/v1/workers/*`, `/v1/tasks/*`, `/v1/leases/*`, `/v1/artifacts/*`), and `inspect_machine_profile()` (`m4_air`, `desktop_gpu`, `server`). Claims tasks, renews CAS leases every `lease_ttl / 3.0`, fences cleanly on `ConcurrencyFenceError`, uploads CAS delta archives (`<data_dir>/cas/<sha>.tar.gz`), and runs throttled (`7200s`) idle GC sweeps. |

## Verification

Tests live in [`tests/runtime/`](../tests/runtime) and [`tests/tools/`](../tests/tools).

```bash
python -m pytest tests/runtime/ -v
```
