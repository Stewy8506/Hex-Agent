"""Race-free Windows process-tree ownership using kill-on-close Job Objects."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import subprocess
import threading
from typing import Any


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IOCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IOCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _ThreadEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
    ]


class WindowsJob:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._api = ctypes.WinDLL("kernel32", use_last_error=True)
        for name, args, result in (
            ("CreateJobObjectW", [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            ("SetInformationJobObject", [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            ("AssignProcessToJobObject", [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
            ("CreateToolhelp32Snapshot", [wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
            ("Thread32First", [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)], wintypes.BOOL),
            ("Thread32Next", [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)], wintypes.BOOL),
            ("OpenThread", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            ("ResumeThread", [wintypes.HANDLE], wintypes.DWORD),
        ):
            function = getattr(self._api, name)
            function.argtypes = args
            function.restype = result
        self._handle = self._api.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000
        if not self._api.SetInformationJobObject(self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, process: subprocess.Popen) -> None:
        if not self._api.AssignProcessToJobObject(self._handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def resume(self, process: subprocess.Popen) -> None:
        # Popen closes the primary thread handle, so reopen the suspended thread by its owner PID.
        snapshot = self._api.CreateToolhelp32Snapshot(0x4, 0)
        if snapshot == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = _ThreadEntry()
            entry.dwSize = ctypes.sizeof(entry)
            found = self._api.Thread32First(snapshot, ctypes.byref(entry))
            while found:
                if entry.th32OwnerProcessID == process.pid:
                    thread = self._api.OpenThread(0x2, False, entry.th32ThreadID)
                    if not thread:
                        raise ctypes.WinError(ctypes.get_last_error())
                    try:
                        if self._api.ResumeThread(thread) == 0xFFFFFFFF:
                            raise ctypes.WinError(ctypes.get_last_error())
                        return
                    finally:
                        self._api.CloseHandle(thread)
                entry.dwSize = ctypes.sizeof(entry)
                found = self._api.Thread32Next(snapshot, ctypes.byref(entry))
            raise OSError("Suspended process primary thread not found")
        finally:
            self._api.CloseHandle(snapshot)

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                if not self._api.CloseHandle(self._handle):
                    raise ctypes.WinError(ctypes.get_last_error())
                self._handle = None


def close_windows_job(process: subprocess.Popen) -> None:
    job = getattr(process, "_hex_job", None)
    if job is not None:
        job.close()


class _WindowsProcess(subprocess.Popen):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._handle_condition = threading.Condition()
        self._handle_users = 0
        super().__init__(*args, **kwargs)

    def _wait(self, timeout: float | None) -> int:
        with self._handle_condition:
            if self.returncode is not None:
                return self.returncode
            self._handle_users += 1
        try:
            return super()._wait(timeout)
        finally:
            with self._handle_condition:
                self._handle_users -= 1
                self._handle_condition.notify_all()

    def _internal_poll(self, _deadstate: Any = None) -> int | None:
        with self._handle_condition:
            if self.returncode is not None:
                return self.returncode
            self._handle_users += 1
        try:
            return super()._internal_poll(_deadstate)
        finally:
            with self._handle_condition:
                self._handle_users -= 1
                self._handle_condition.notify_all()

    def close_handle(self) -> None:
        self.wait()
        with self._handle_condition:
            # Concurrent native waits must finish before their shared handle is released.
            self._handle_condition.wait_for(lambda: self._handle_users == 0)
            self._handle.Close()


def close_windows_process(process: subprocess.Popen) -> None:
    close_windows_job(process)
    process.wait(timeout=3.0)
    process.close_handle()


def spawn_windows_process(*args: Any, **kwargs: Any) -> subprocess.Popen:
    job = WindowsJob()
    process = None
    try:
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | 0x4
        process = _WindowsProcess(*args, **kwargs)
        job.assign(process)
        process._hex_job = job
        job.resume(process)
    except BaseException:
        if process is not None:
            process.kill()
            process.wait()
            process.close_handle()
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
        job.close()
        raise

    def reap_tree() -> None:
        process.wait()
        job.close()
        process.close_handle()

    threading.Thread(target=reap_tree, daemon=True, name=f"windows-job-{process.pid}").start()
    return process
