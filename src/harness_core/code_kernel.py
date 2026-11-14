"""Persistent REPL CodeKernel Subprocess Engine & KernelRegistry.

Delivers isolated, session-persistent Python subprocess execution:
- Maintained execution state (variables, functions, imports) across consecutive cells
  in a persistent GLOBALS namespace.
- Sentinel framed protocol (<SENTINEL> <byte-length>\n<json>) separating protocol
  responses from raw subprocess stream output on stdin/stdout.
- 1 MB in-memory output capture cap with automatic disk spill fallback for large outputs.
- Stale module tracking: detects on-disk source changes after module import.
- Process tree termination via SIGKILL on execution timeouts to prevent wedged kernels.
- Thread-safe, bounded KernelRegistry with LRU capacity and idle eviction.
"""

from __future__ import annotations

import atexit
import contextlib
import io
import json
import logging
import os
import queue
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
import uuid

logger = logging.getLogger(__name__)

# Output capture limits
_RUNNER_CAPTURE_BYTES = 1_000_000  # 1 MB in-memory capture cap
_SPILL_CAP = 5_000_000  # 5 MB max disk spill cap
_MAX_STDOUT_BYTES = 1_000_000
_MAX_STDERR_BYTES = 250_000

# Shared runner cell source executed inside child runner
RUNNER_CELL_SOURCE = '''\
GLOBALS = {"__name__": "__main__", "__builtins__": __builtins__}
GLOBALS["GLOBALS"] = GLOBALS


def _clip(text):
    return (text, False) if len(text) <= _CAPTURE_LIMIT else (text[:_CAPTURE_LIMIT], True)


_MODULE_RECORDS = {}
_STDLIB_PREFIX = os.path.dirname(os.__file__) + os.sep
_INSTALLED_MARKERS = (os.sep + "site-packages" + os.sep, os.sep + "dist-packages" + os.sep)


def _edited_modules(report):
    """Record source mtime and import spec of imported project modules.
    When report=True, detect modules whose file mtime changed while the import spec remained identical.
    """
    edited = []
    for name, module in list(sys.modules.items()):
        try:
            fields = object.__getattribute__(module, "__dict__")
            path, spec = fields.get("__file__"), fields.get("__spec__")
            if (not isinstance(name, str) or not isinstance(path, str) or not path.endswith(".py")
                    or path.startswith(_STDLIB_PREFIX) or any(marker in path for marker in _INSTALLED_MARKERS)):
                continue
            mtime = os.stat(path).st_mtime_ns
        except Exception:
            continue
        seen = _MODULE_RECORDS.get(name)
        if report and seen is not None and seen[0] != mtime and seen[1] is spec:
            edited.append(name)
        if report or seen is None or seen[1] is not spec:
            _MODULE_RECORDS[name] = (mtime, spec)
    return sorted(edited)


def _scan_edited_modules(report):
    """Scan imported modules safely without failing cell execution."""
    try:
        return _edited_modules(report)
    except Exception:
        return []


def run_cell(request, execution_count):
    """Exec one cell; returns (response payload, FULL stdout text)."""
    out, err = io.StringIO(), io.StringIO()
    status, trace = "ok", ""
    stale_modules = _scan_edited_modules(True)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            exec(compile(request["code"], "<cell>", "exec"), GLOBALS)
    except SystemExit as exc:
        status, trace = "exit", "SystemExit: " + repr(exc.code)
    except BaseException:
        status, trace = "error", traceback.format_exc()
    _scan_edited_modules(False)
    stdout_text, stdout_clipped = _clip(out.getvalue())
    stderr_text, stderr_clipped = _clip(err.getvalue())
    return {
        "stale_modules": stale_modules,
        "id": request.get("id", ""),
        "status": status,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "stdout_clipped": stdout_clipped,
        "stderr_clipped": stderr_clipped,
        "traceback": trace,
        "execution_count": execution_count,
    }, out.getvalue()
'''

KERNEL_RUNNER_SOURCE = f'''\
"""Auto-generated HEX session-kernel runner. One exec cell per request."""
import contextlib
import io
import json
import os
import sys
import threading
import traceback

_SENTINEL = os.environ["HEX_KERNEL_SENTINEL"]
_CAPTURE_LIMIT = {_RUNNER_CAPTURE_BYTES}
_SPILL_DIR = os.environ.get("HEX_KERNEL_SPILL_DIR", "")
_SPILL_CAP = {_SPILL_CAP}

_real_stdout = sys.stdout

{RUNNER_CELL_SOURCE}

def _spill(text, spill_name):
    """Write unclipped stdout to disk spill file; return its path or empty string."""
    if not _SPILL_DIR:
        return ""
    try:
        spill_path = os.path.join(_SPILL_DIR, spill_name)
        with open(spill_path, "w", encoding="utf-8", errors="replace") as f:
            f.write(text[:_SPILL_CAP])
            if len(text) > _SPILL_CAP:
                f.write("\\n\\n[... spill capped ...]")
        return spill_path
    except Exception:
        return ""


def _reply(payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    _real_stdout.buffer.write(("\\n" + _SENTINEL + " " + str(len(body)) + "\\n").encode("utf-8"))
    _real_stdout.buffer.write(body)
    _real_stdout.buffer.flush()


def main():
    execution_count = 0
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        execution_count += 1
        payload, full_stdout = run_cell(request, execution_count)
        payload["stdout_spill_path"] = (
            _spill(full_stdout, "cell_%06d_stdout.txt" % execution_count)
            if payload["stdout_clipped"] else ""
        )
        _reply(payload)
        if payload["status"] == "exit":
            break


if __name__ == "__main__":
    main()
'''


def clean_text(text: str) -> str:
    """Strip ANSI sequences and redact secrets from output text."""
    if not text:
        return ""
    try:
        from harness_core.process_registry import strip_ansi
        text = strip_ansi(text)
    except Exception:
        pass
    try:
        from harness_core.gateway import redact_secrets
        text = redact_secrets(text)
    except Exception:
        pass
    return text


def stale_modules_warning(stale_modules: list[str] | dict[str, Any]) -> str:
    """Format an advisory string for modules edited on disk after import."""
    if isinstance(stale_modules, dict):
        reported = stale_modules.get("stale_modules", [])
    else:
        reported = stale_modules
    names = [
        name for name in (reported if isinstance(reported, list) else [])
        if isinstance(name, str) and len(name) <= 200 and all(part.isidentifier() for part in name.split("."))
    ]
    if not names:
        return ""
    shown = clean_text(", ".join(names[:8])) + (f" and {len(names) - 8} more" if len(names) > 8 else "")
    return (
        f"Edited on disk after this kernel imported it: {shown}. If this cell used it without "
        "reloading, it ran the OLD code. Run importlib.reload(<module>) (and re-import any names "
        "taken from it), or call again with reset=True to start from fresh imports."
    )


class _BoundedBuffer:
    """Thread-safe byte buffer capping total stored bytes at cap (default 1 MB)."""

    def __init__(self, cap: int = 1_000_000) -> None:
        self.cap = cap
        self.chunks: list[bytes] = []
        self.total = 0
        self._lock = threading.Lock()

    def append(self, data: bytes, cap: int | None = None) -> None:
        if not data:
            return
        limit = self.cap if cap is None else cap
        with self._lock:
            keep = data[: max(0, limit - self.total)]
            if keep:
                self.chunks.append(keep)
                self.total += len(keep)

    def drain(self) -> str:
        with self._lock:
            chunks, self.chunks, self.total = self.chunks, [], 0
        return b"".join(chunks).decode("utf-8", errors="replace")


def _kill_process_tree(proc: subprocess.Popen, force: bool = False) -> None:
    """Terminate a process and its whole process group cleanly."""
    if proc.poll() is not None:
        return
    pid = proc.pid
    sig = signal.SIGKILL if force else signal.SIGTERM
    if sys.platform != "win32":
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, sig)
        except (ProcessLookupError, OSError):
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, OSError):
                pass
    else:
        try:
            proc.kill() if force else proc.terminate()
        except OSError:
            pass

    try:
        proc.wait(timeout=0.3 if force else 0.5)
    except (subprocess.TimeoutExpired, Exception):
        if not force:
            _kill_process_tree(proc, force=True)

    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass


def _stdout_reader(kernel: CodeKernel) -> None:
    """Split child stdout into protocol frames and raw stream bytes."""
    assert kernel.proc is not None and kernel.proc.stdout is not None
    stream = kernel.proc.stdout
    marker = ("\n" + kernel.sentinel + " ").encode("utf-8")

    def raw(data: bytes) -> None:
        kernel.raw_buffer.append(data, _MAX_STDOUT_BYTES)

    buf = b""
    while not kernel.stop_event.is_set():
        try:
            chunk = stream.read1(4096) if hasattr(stream, "read1") else stream.read(4096)
        except Exception:
            break
        if not chunk:
            if buf:
                raw(buf)
            kernel.response_q.put({"status": "kernel-eof"})
            return
        buf += chunk
        while True:
            index = buf.find(marker)
            if index < 0:
                spill = buf[: -len(marker)] if len(buf) > len(marker) else b""
                if spill:
                    raw(spill)
                    buf = buf[len(spill):]
                break
            if index:
                raw(buf[:index])
            rest = buf[index + len(marker):]
            newline = rest.find(b"\n")
            if newline < 0:
                buf = buf[index:]
                break
            try:
                length = int(rest[:newline])
            except ValueError:
                raw(marker)
                buf = rest
                continue
            body = rest[newline + 1:]
            while len(body) < length:
                try:
                    more = stream.read1(length - len(body)) if hasattr(stream, "read1") else stream.read(length - len(body))
                except Exception:
                    more = b""
                if not more:
                    kernel.response_q.put({"status": "kernel-eof"})
                    return
                body += more
            try:
                kernel.response_q.put(json.loads(body[:length].decode("utf-8", errors="replace")))
            except ValueError:
                kernel.response_q.put({"status": "protocol-error"})
            buf = body[length:]


def _stderr_reader(kernel: CodeKernel) -> None:
    """Read raw stderr chunks into the kernel stderr buffer."""
    assert kernel.proc is not None and kernel.proc.stderr is not None
    stream = kernel.proc.stderr
    while not kernel.stop_event.is_set():
        try:
            chunk = stream.read1(4096) if hasattr(stream, "read1") else stream.read(4096)
        except Exception:
            break
        if not chunk:
            break
        kernel.stderr_buffer.append(chunk, _MAX_STDERR_BYTES)


class CodeKernel:
    """Subprocess supervisor executing code cells in a persistent Python namespace."""

    def __init__(
        self,
        owner: str = "default",
        cwd: str | None = None,
        kernel_id: str | None = None,
        registry: "KernelRegistry" | None = None,
    ) -> None:
        self.kernel_id: str = kernel_id or uuid.uuid4().hex
        self.owner: str = owner
        self.cwd: str = cwd or os.getcwd()
        self.registry: "KernelRegistry" | None = registry
        self.tmpdir: str = ""
        self.sentinel: str = ""
        self.proc: subprocess.Popen | None = None
        self.response_q: queue.Queue[dict[str, Any]] = queue.Queue()
        self.raw_buffer: _BoundedBuffer = _BoundedBuffer(_MAX_STDOUT_BYTES)
        self.stderr_buffer: _BoundedBuffer = _BoundedBuffer(_MAX_STDERR_BYTES)
        self.execution_count: int = 0
        self.created_at: float = time.monotonic()
        self.last_used: float = time.monotonic()
        self.lock: threading.RLock = threading.RLock()
        self.stop_event: threading.Event = threading.Event()
        self.pinned: bool = False
        self.attached: int = 0

    def alive(self) -> bool:
        """Return True if the child process is alive and running."""
        return self.proc is not None and self.proc.poll() is None

    def dead(self) -> bool:
        """Return True if the child process was spawned and has exited."""
        return self.proc is not None and self.proc.poll() is not None

    def spawn(self) -> None:
        """Spawn the child runner process in an isolated environment."""
        if self.alive():
            return

        self.tmpdir = tempfile.mkdtemp(prefix="hex_kernel_")
        runner_path = os.path.join(self.tmpdir, "hex_kernel_runner.py")
        with open(runner_path, "w", encoding="utf-8") as f:
            f.write(KERNEL_RUNNER_SOURCE)

        self.sentinel = "@@HEX-KERNEL-" + secrets.token_urlsafe(16) + "@@"

        child_env = dict(os.environ)
        child_env["HEX_KERNEL_SENTINEL"] = self.sentinel
        child_env["HEX_KERNEL_SPILL_DIR"] = self.tmpdir
        child_env["PYTHONUNBUFFERED"] = "1"

        popen_kwargs: dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "cwd": self.cwd or self.tmpdir,
            "env": child_env,
        }
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            popen_kwargs["start_new_session"] = True

        self.stop_event.clear()
        self.response_q = queue.Queue()
        self.raw_buffer.drain()
        self.stderr_buffer.drain()

        self.proc = subprocess.Popen([sys.executable, runner_path], **popen_kwargs)

        threading.Thread(target=_stdout_reader, args=(self,), daemon=True, name=f"hex-k-out-{self.kernel_id[:6]}").start()
        threading.Thread(target=_stderr_reader, args=(self,), daemon=True, name=f"hex-k-err-{self.kernel_id[:6]}").start()

    def execute(self, code: str, timeout: float = 30.0) -> dict[str, Any]:
        """Execute a code cell in the persistent Python namespace with timeout guards."""
        with self.lock:
            self.attached += 1
            try:
                if not self.alive():
                    self.spawn()

                exec_start = time.monotonic()
                self.last_used = exec_start
                self.execution_count += 1
                req_id = uuid.uuid4().hex

                # Drain raw buffers before cell starts
                self.raw_buffer.drain()
                self.stderr_buffer.drain()

                assert self.proc is not None and self.proc.stdin is not None
                req_data = json.dumps({"id": req_id, "code": code}) + "\n"
                try:
                    self.proc.stdin.write(req_data.encode("utf-8"))
                    self.proc.stdin.flush()
                except (BrokenPipeError, OSError) as exc:
                    _kill_process_tree(self.proc, force=True)
                    return {
                        "status": "error",
                        "stdout": "",
                        "stderr": self.stderr_buffer.drain(),
                        "execution_count": self.execution_count,
                        "stale_modules": [],
                        "stdout_spill_path": "",
                        "stdout_clipped": False,
                        "stderr_clipped": False,
                        "traceback": f"BrokenPipeError: Child process closed stdin ({exc})",
                        "duration_seconds": round(time.monotonic() - exec_start, 3),
                    }

                deadline = time.monotonic() + timeout if timeout else None
                payload: dict[str, Any] | None = None
                status = "ok"

                while True:
                    if deadline is not None and time.monotonic() > deadline:
                        status = "timeout"
                        break
                    try:
                        resp = self.response_q.get(timeout=0.05)
                    except queue.Empty:
                        if self.proc.poll() is not None:
                            status = "error"
                            payload = {"status": "error", "traceback": "Child process exited unexpectedly."}
                            break
                        continue

                    resp_status = resp.get("status")
                    if resp_status in ("kernel-eof", "protocol-error"):
                        status = "error"
                        payload = resp
                        break

                    if resp.get("id") == req_id or not resp.get("id"):
                        payload = resp
                        status = resp.get("status", "ok")
                        break

                duration = round(time.monotonic() - exec_start, 3)

                if status == "timeout":
                    if self.proc is not None:
                        _kill_process_tree(self.proc, force=True)
                    self.stop_event.set()
                    raw_out = self.raw_buffer.drain()
                    raw_err = self.stderr_buffer.drain()
                    if self.registry is not None:
                        self.registry.discard_kernel(self)
                    return {
                        "status": "timeout",
                        "stdout": clean_text(raw_out),
                        "stderr": clean_text(raw_err),
                        "execution_count": self.execution_count,
                        "stale_modules": [],
                        "stdout_spill_path": "",
                        "stdout_clipped": False,
                        "stderr_clipped": False,
                        "traceback": f"TimeoutError: Cell execution exceeded timeout of {timeout}s",
                        "duration_seconds": duration,
                    }

                if payload is None:
                    payload = {}

                raw_out = self.raw_buffer.drain()
                raw_err = self.stderr_buffer.drain()

                cell_stdout = str(payload.get("stdout", "")) + raw_out
                cell_stderr = str(payload.get("stderr", "")) + raw_err

                result: dict[str, Any] = {
                    "status": status,
                    "stdout": clean_text(cell_stdout),
                    "stderr": clean_text(cell_stderr),
                    "execution_count": int(payload.get("execution_count", self.execution_count)),
                    "stale_modules": payload.get("stale_modules", []),
                    "stdout_spill_path": payload.get("stdout_spill_path", ""),
                    "stdout_clipped": bool(payload.get("stdout_clipped", False)),
                    "stderr_clipped": bool(payload.get("stderr_clipped", False)),
                    "traceback": clean_text(str(payload.get("traceback", ""))),
                    "duration_seconds": duration,
                }

                warnings: list[str] = []
                if result["stdout_clipped"] and result["stdout_spill_path"]:
                    spill = result["stdout_spill_path"]
                    warnings.append(
                        f"Cell stdout exceeded the inline cap; head shown. FULL output saved to {spill} "
                        f'— page it with read_file(path="{spill}", offset=...) instead of re-running. '
                        "(Kernel state persists: printing a narrower slice next call is often cheaper.)"
                    )
                if result["stale_modules"]:
                    stale_warn = stale_modules_warning(result["stale_modules"])
                    if stale_warn:
                        warnings.append(stale_warn)
                if warnings:
                    result["warning"] = " ".join(warnings)

                if status == "exit":
                    if self.proc is not None:
                        _kill_process_tree(self.proc, force=True)
                    if self.registry is not None:
                        self.registry.discard_kernel(self)

                return result
            finally:
                self.attached = max(0, self.attached - 1)
                self.last_used = time.monotonic()

    def reset(self) -> None:
        """Reset the execution environment, clearing global variables and state."""
        with self.lock:
            if self.proc is not None:
                _kill_process_tree(self.proc, force=True)
                self.proc = None
            self.stop_event.set()
            if self.tmpdir and os.path.exists(self.tmpdir):
                shutil.rmtree(self.tmpdir, ignore_errors=True)
                self.tmpdir = ""
            self.raw_buffer.drain()
            self.stderr_buffer.drain()
            self.execution_count = 0

    def close(self) -> None:
        """Teardown the child process and cleanup temporary directories."""
        self.teardown()

    def teardown(self) -> None:
        """Teardown the child process and cleanup temporary directories."""
        with self.lock:
            if self.proc is not None:
                _kill_process_tree(self.proc, force=True)
                self.proc = None
            self.stop_event.set()
            if self.tmpdir and os.path.exists(self.tmpdir):
                shutil.rmtree(self.tmpdir, ignore_errors=True)
                self.tmpdir = ""
            self.raw_buffer.drain()
            self.stderr_buffer.drain()


class KernelRegistry:
    """Thread-safe kernel registry managing CodeKernel lifecycles, LRU limits, and idle eviction."""

    def __init__(self, max_kernels: int = 4, idle_timeout: float = 1800.0) -> None:
        self.max_kernels = max(1, max_kernels)
        self.idle_timeout = idle_timeout
        self._kernels: dict[tuple[str, str], CodeKernel] = {}
        self._reset_keys: set[tuple[str, str]] = set()
        self._lock = threading.Lock()
        self._stop_reaper = threading.Event()
        self._reaper_thread = threading.Thread(
            target=self._reaper_loop, daemon=True, name="hex-kernel-idle-reaper"
        )
        self._reaper_thread.start()

    def _reaper_loop(self) -> None:
        while not self._stop_reaper.is_set():
            interval = min(300.0, max(1.0, self.idle_timeout / 6.0))
            if self._stop_reaper.wait(interval):
                break
            self.sweep_idle()

    def sweep_idle(self) -> list[CodeKernel]:
        """Evict and teardown all idle kernels past idle_timeout."""
        now = time.monotonic()
        with self._lock:
            expired_keys = [
                k for k, kern in self._kernels.items()
                if not kern.pinned and kern.attached == 0 and (now - kern.last_used > self.idle_timeout)
            ]
            doomed = [self._kernels.pop(k) for k in expired_keys]
        for kern in doomed:
            kern.teardown()
        return doomed

    def get_or_create(
        self, owner: str, cwd: str | None = None, reset: bool = False, pinned: bool = False
    ) -> tuple[CodeKernel, bool]:
        """Get an existing kernel or create a new one, applying LRU eviction and idle sweeps.

        Returns:
            tuple[CodeKernel, bool]: (kernel, state_reset)
        """
        cwd_resolved = os.path.abspath(cwd) if cwd else os.getcwd()
        key = (owner, cwd_resolved)
        state_reset = False

        with self._lock:
            # Idle sweep first
            now = time.monotonic()
            expired_keys = [
                k for k, kern in self._kernels.items()
                if not kern.pinned and kern.attached == 0 and (now - kern.last_used > self.idle_timeout)
            ]
            for k in expired_keys:
                dropped = self._kernels.pop(k, None)
                if dropped:
                    dropped.teardown()

            kernel = self._kernels.get(key)
            if kernel is not None and (reset or kernel.dead()):
                self._kernels.pop(key, None)
                state_reset = True
                if kernel.attached == 0:
                    kernel.teardown()
                kernel = None
            elif key in self._reset_keys or reset:
                self._reset_keys.discard(key)
                state_reset = True

            if kernel is None:
                kernel = CodeKernel(owner=owner, cwd=cwd_resolved, registry=self)
                kernel.pinned = pinned
                self._kernels[key] = kernel

            kernel.last_used = time.monotonic()

            # Enforce LRU capacity among unpinned kernels
            unpinned = [k for k, kern in self._kernels.items() if not kern.pinned]
            if len(unpinned) > self.max_kernels:
                by_age = sorted(
                    (k for k in unpinned if k != key and self._kernels[k].attached == 0),
                    key=lambda k: self._kernels[k].last_used,
                )
                to_evict = by_age[: max(0, len(unpinned) - self.max_kernels)]
                for k in to_evict:
                    evicted = self._kernels.pop(k, None)
                    if evicted:
                        evicted.teardown()

        return kernel, state_reset

    def discard_kernel(self, kernel: CodeKernel) -> None:
        """Remove and teardown a specific kernel, tracking state reset for its key."""
        with self._lock:
            keys_to_pop = [k for k, v in self._kernels.items() if v is kernel]
            for k in keys_to_pop:
                self._kernels.pop(k, None)
                self._reset_keys.add(k)
        kernel.teardown()

    def discard(self, owner: str | tuple[str, str], cwd: str | None = None) -> None:
        """Remove and teardown a registered kernel."""
        if isinstance(owner, tuple):
            cwd_resolved = os.path.abspath(owner[1]) if owner[1] else os.getcwd()
            key = (owner[0], cwd_resolved)
        else:
            cwd_resolved = os.path.abspath(cwd) if cwd else os.getcwd()
            key = (owner, cwd_resolved)

        with self._lock:
            kernel = self._kernels.pop(key, None)
            self._reset_keys.add(key)
        if kernel:
            kernel.teardown()

    def shutdown(self, owner: str | None = None) -> None:
        """Teardown all kernels, or all kernels belonging to owner."""
        with self._lock:
            if owner is None:
                doomed = list(self._kernels.values())
                self._kernels.clear()
                self._reset_keys.clear()
            else:
                matching_keys = [k for k in self._kernels if k[0] == owner]
                doomed = [self._kernels.pop(k) for k in matching_keys]
                for k in list(self._reset_keys):
                    if k[0] == owner:
                        self._reset_keys.discard(k)
        for kernel in doomed:
            kernel.teardown()

    def stop(self) -> None:
        """Stop background reaper and shutdown all kernels."""
        self._stop_reaper.set()
        self.shutdown()


_GLOBAL_REGISTRY: KernelRegistry | None = None
_GLOBAL_LOCK = threading.Lock()


def get_kernel_registry() -> KernelRegistry:
    """Return the global KernelRegistry singleton."""
    global _GLOBAL_REGISTRY
    with _GLOBAL_LOCK:
        if _GLOBAL_REGISTRY is None:
            _GLOBAL_REGISTRY = KernelRegistry()
        return _GLOBAL_REGISTRY


def reset_kernel_registry() -> None:
    """Shutdown and reset the global KernelRegistry singleton."""
    global _GLOBAL_REGISTRY
    with _GLOBAL_LOCK:
        if _GLOBAL_REGISTRY is not None:
            _GLOBAL_REGISTRY.stop()
            _GLOBAL_REGISTRY = None


def _shutdown_on_exit() -> None:
    if _GLOBAL_REGISTRY is not None:
        _GLOBAL_REGISTRY.stop()


atexit.register(_shutdown_on_exit)
