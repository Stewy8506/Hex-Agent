"""Runtime execution subpackage: process supervisor, persistent REPL code kernel, system daemons, distributed workers, and scheduler."""

from __future__ import annotations

import importlib
from typing import Any

_SUBMODULES: set[str] = {'code_kernel', 'scheduler', 'worker_daemon', 'process_registry', 'daemon'}
_SYMBOL_MAP: dict[str, str] = {'strip_ansi': 'process_registry', 'ProcessStatus': 'process_registry', 'CircularOutputBuffer': 'process_registry', 'ProcessSession': 'process_registry', 'ProcessRegistry': 'process_registry', 'get_process_registry': 'process_registry', 'reset_process_registry': 'process_registry', 'clean_text': 'code_kernel', 'stale_modules_warning': 'code_kernel', 'CodeKernel': 'code_kernel', 'KernelRegistry': 'code_kernel', 'get_kernel_registry': 'code_kernel', 'reset_kernel_registry': 'code_kernel', 'DaemonConfig': 'daemon', 'DaemonInstaller': 'daemon', 'inspect_machine_profile': 'worker_daemon', 'WorkerClient': 'worker_daemon', 'DirectLedgerWorkerClient': 'worker_daemon', 'RestWorkerClient': 'worker_daemon', 'WorkerDaemon': 'worker_daemon', 'CronParser': 'scheduler', 'ScheduledJob': 'scheduler', 'TaskScheduler': 'scheduler'}

__all__ = [
    "process_registry",
    "code_kernel",
    "daemon",
    "worker_daemon",
    "scheduler",
    "strip_ansi",
    "ProcessStatus",
    "CircularOutputBuffer",
    "ProcessSession",
    "ProcessRegistry",
    "get_process_registry",
    "reset_process_registry",
    "clean_text",
    "stale_modules_warning",
    "CodeKernel",
    "KernelRegistry",
    "get_kernel_registry",
    "reset_kernel_registry",
    "DaemonConfig",
    "DaemonInstaller",
    "inspect_machine_profile",
    "WorkerClient",
    "DirectLedgerWorkerClient",
    "RestWorkerClient",
    "WorkerDaemon",
    "CronParser",
    "ScheduledJob",
    "TaskScheduler",
]

def __getattr__(name: str) -> Any:
    if name in _SUBMODULES:
        mod = importlib.import_module(f".{name}", __name__)
        globals()[name] = mod
        return mod
    if name in _SYMBOL_MAP:
        target_mod = _SYMBOL_MAP[name]
        mod = importlib.import_module(f".{target_mod}", __name__)
        val = getattr(mod, name)
        globals()[name] = val
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

def __dir__() -> list[str]:
    return list(__all__)
