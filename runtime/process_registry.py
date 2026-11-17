"""PTY Process Supervisor & Circular Stream Registry.

Matches Hermes Agent background process architecture:
- Thread-safe background process lifecycle management (spawn, poll, read_output, wait, kill, list)
- POSIX pseudo-terminal (PTY) allocation with transparent pipe fallback
- Rolling 200 KB circular ring buffer (CircularOutputBuffer) with non-blocking background reader threads
- Monotonic timestamps and comprehensive process states (RUNNING, COMPLETED, FAILED, KILLED)
- atexit cleanup hook to prevent orphan or zombie processes on the host
"""

from __future__ import annotations

import atexit
import codecs
from dataclasses import dataclass, field
import enum
import logging
import os
import re
import select
import signal
import subprocess
import threading
import time
from typing import Any
import uuid

logger = logging.getLogger(__name__)

ANSI_REGEX = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def strip_ansi(text: str) -> str:
    """Strip ANSI escape sequences from terminal output."""
    return ANSI_REGEX.sub("", text)


class ProcessStatus(str, enum.Enum):
    """Execution status for background supervised processes."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    KILLED = "killed"


class CircularOutputBuffer:
    """Thread-safe circular ring buffer capping memory at max_chars (default 200 KB)."""

    def __init__(self, max_chars: int = 200_000) -> None:
        self.max_chars = max(1, max_chars)
        self._buffer: str = ""
        self._total_chars: int = 0
        self._lock = threading.Lock()

    @property
    def total_chars(self) -> int:
        with self._lock:
            return self._total_chars

    def append(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            self._buffer += text
            self._total_chars += len(text)
            if len(self._buffer) > self.max_chars:
                self._buffer = self._buffer[-self.max_chars:]

    def read(self, offset: int = 0, limit: int = 2000) -> tuple[str, int, bool]:
        """Read a slice of output.

        Args:
            offset: Cumulative stream character offset. If offset is earlier than
                the start of the retained ring buffer, reads from buffer start.
            limit: Maximum characters to return.

        Returns:
            tuple[str, int, bool]: (slice_text, total_chars, has_more)
        """
        offset = max(0, int(offset))
        limit = max(0, int(limit))

        with self._lock:
            buf_len = len(self._buffer)
            total = self._total_chars
            buffer_start = max(0, total - buf_len)

            if offset < buffer_start:
                start_in_buf = 0
            else:
                start_in_buf = offset - buffer_start

            if start_in_buf >= buf_len:
                return ("", total, False)

            end_in_buf = min(buf_len, start_in_buf + limit)
            slice_text = self._buffer[start_in_buf:end_in_buf]
            has_more = end_in_buf < buf_len
            return (slice_text, total, has_more)

    def get_tail(
        self,
        n: int = 2000,
        strip_ansi: bool = False,
        strip_ansi_codes: bool = False,
    ) -> str:
        """Get the last n characters of the buffer."""
        should_strip = strip_ansi or strip_ansi_codes
        with self._lock:
            tail = self._buffer[-n:] if n > 0 else ""
        if should_strip:
            return ANSI_REGEX.sub("", tail)
        return tail

    def clear(self) -> None:
        """Reset the buffer and counters."""
        with self._lock:
            self._buffer = ""
            self._total_chars = 0


@dataclass
class ProcessSession:
    """Supervised process session tracking execution metadata and stream output."""

    id: str
    command: str
    cwd: str
    pid: int | None = None
    status: ProcessStatus = ProcessStatus.RUNNING
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    exit_code: int | None = None
    pty_enabled: bool = False
    buffer: CircularOutputBuffer = field(default_factory=CircularOutputBuffer)
    pty_master_fd: int | None = None
    process: Any = None
    _reader_thread: threading.Thread | None = field(default=None, repr=False)
    _stop_event: threading.Event = field(default_factory=threading.Event, repr=False)

    def to_dict(self) -> dict[str, Any]:
        """Return serializable dictionary representation."""
        now = time.time()
        uptime = (self.finished_at or now) - self.started_at if self.started_at else 0.0
        return {
            "id": self.id,
            "command": self.command,
            "cwd": self.cwd,
            "pid": self.pid,
            "status": self.status.value if isinstance(self.status, ProcessStatus) else str(self.status),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "uptime": round(uptime, 2),
            "exit_code": self.exit_code,
            "pty_enabled": self.pty_enabled,
            "total_output_chars": self.buffer.total_chars,
        }

    def mark_finished(self, exit_code: int, status: ProcessStatus | None = None) -> None:
        """Mark session as finished, setting exit code and termination status."""
        if self.finished_at is None:
            self.finished_at = time.time()
        self.exit_code = exit_code
        if status is not None:
            self.status = status
        else:
            self.status = ProcessStatus.COMPLETED if exit_code == 0 else ProcessStatus.FAILED
        self._stop_event.set()


class ProcessRegistry:
    """Thread-safe background process supervisor with PTY and ring buffer stream management."""

    def __init__(self) -> None:
        self._processes: dict[str, ProcessSession] = {}
        self._lock = threading.RLock()
        self._atexit_hook = atexit.register(self.cleanup_all)

    def spawn(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        use_pty: bool = True,
    ) -> ProcessSession:
        """Spawn a background process using PTY or pipe fallback.

        Args:
            command: Shell command string to execute.
            cwd: Working directory (defaults to current working directory).
            env: Optional environment variables dictionary.
            use_pty: True to request pseudo-terminal allocation on POSIX systems.

        Returns:
            ProcessSession: The initialized and tracked process session.
        """
        if not command or not command.strip():
            raise ValueError("Command cannot be empty")

        working_dir = os.path.abspath(cwd or os.getcwd())
        if not os.path.isdir(working_dir):
            raise NotADirectoryError(f"Working directory does not exist: {working_dir}")

        proc_id = f"proc_{uuid.uuid4().hex[:12]}"

        spawn_env = dict(os.environ)
        if env:
            spawn_env.update(env)
        spawn_env.setdefault("TERM", "xterm-256color")
        spawn_env.setdefault("PAGER", "cat")
        spawn_env.setdefault("GIT_PAGER", "cat")

        master_fd: int | None = None
        slave_fd: int | None = None
        pty_enabled = False
        proc: subprocess.Popen | None = None

        if use_pty:
            try:
                import pty
                master_fd, slave_fd = pty.openpty()
                pty_enabled = True
            except (ImportError, OSError, AttributeError) as exc:
                logger.warning("PTY allocation unavailable (%s); falling back to standard pipe", exc)
                master_fd = None
                slave_fd = None
                pty_enabled = False

        if pty_enabled and slave_fd is not None:
            try:
                popen_kwargs: dict[str, Any] = {
                    "close_fds": True,
                }
                if hasattr(os, "setsid"):
                    popen_kwargs["preexec_fn"] = os.setsid
                else:
                    popen_kwargs["start_new_session"] = True

                proc = subprocess.Popen(
                    command,
                    shell=True,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    cwd=working_dir,
                    env=spawn_env,
                    **popen_kwargs,
                )
                # Parent must close the slave file descriptor immediately
                os.close(slave_fd)
                slave_fd = None
            except Exception as exc:
                logger.warning("PTY subprocess launch failed (%s); falling back to pipe mode", exc)
                if slave_fd is not None:
                    try:
                        os.close(slave_fd)
                    except OSError:
                        pass
                    slave_fd = None
                if master_fd is not None:
                    try:
                        os.close(master_fd)
                    except OSError:
                        pass
                    master_fd = None
                pty_enabled = False

        if not pty_enabled:
            popen_kwargs = {"close_fds": True}
            if hasattr(os, "setsid"):
                popen_kwargs["preexec_fn"] = os.setsid
            else:
                popen_kwargs["start_new_session"] = True

            proc = subprocess.Popen(
                command,
                shell=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=working_dir,
                env=spawn_env,
                text=False,
                **popen_kwargs,
            )

        session = ProcessSession(
            id=proc_id,
            command=command,
            cwd=working_dir,
            pid=proc.pid if proc else None,
            status=ProcessStatus.RUNNING,
            started_at=time.time(),
            pty_enabled=pty_enabled,
            pty_master_fd=master_fd,
            process=proc,
        )

        with self._lock:
            self._processes[session.id] = session

        reader_thread = threading.Thread(
            target=self._reader_loop,
            args=(session,),
            name=f"proc-reader-{session.id}",
            daemon=True,
        )
        session._reader_thread = reader_thread
        reader_thread.start()

        return session

    def _reader_loop(self, session: ProcessSession) -> None:
        """Background daemon reader thread streaming child output into the circular buffer."""
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

        if session.pty_enabled and session.pty_master_fd is not None:
            fd = session.pty_master_fd
        elif session.process and session.process.stdout:
            fd = session.process.stdout.fileno()
        else:
            return

        try:
            while not session._stop_event.is_set():
                try:
                    rlist, _, _ = select.select([fd], [], [], 0.1)
                except (ValueError, OSError):
                    break

                if rlist:
                    try:
                        chunk = os.read(fd, 4096)
                        if not chunk:
                            # EOF reached
                            break
                        text = decoder.decode(chunk, final=False)
                        session.buffer.append(text)
                    except OSError:
                        # EIO on Linux PTY or closed fd
                        break
                else:
                    # Timeout check: if child process exited, drain and break
                    if session.process and session.process.poll() is not None:
                        break

            # Final drain of any remaining buffered bytes
            try:
                for _ in range(50):
                    rlist, _, _ = select.select([fd], [], [], 0.02)
                    if not rlist:
                        break
                    chunk = os.read(fd, 4096)
                    if not chunk:
                        break
                    text = decoder.decode(chunk, final=False)
                    session.buffer.append(text)
            except (ValueError, OSError):
                pass

            final_text = decoder.decode(b"", final=True)
            if final_text:
                session.buffer.append(final_text)

        except Exception as exc:
            logger.error("Error in process reader loop for %s: %s", session.id, exc)
        finally:
            # Reap exit code
            exit_code = 0
            if session.process:
                try:
                    exit_code = session.process.wait()
                except Exception:
                    exit_code = session.process.poll() or 0

            # Close master file descriptor
            if session.pty_master_fd is not None:
                try:
                    os.close(session.pty_master_fd)
                except OSError:
                    pass
                session.pty_master_fd = None

            # Close stdout in pipe mode
            if session.process and session.process.stdout:
                try:
                    session.process.stdout.close()
                except Exception:
                    pass

            # Update final status
            if session.status == ProcessStatus.RUNNING:
                status = ProcessStatus.COMPLETED if exit_code == 0 else ProcessStatus.FAILED
                session.mark_finished(exit_code, status)
            else:
                session.mark_finished(exit_code, session.status)

    def get(self, process_id: str) -> ProcessSession:
        """Retrieve process session by ID, or raise KeyError."""
        with self._lock:
            session = self._processes.get(process_id)
            if session is None:
                raise KeyError(f"Process not found: {process_id}")
            return session

    def poll(self, process_id: str) -> dict[str, Any]:
        """Check if process has terminated, update status, and return session dict."""
        session = self.get(process_id)
        if session.status == ProcessStatus.RUNNING and session.process is not None:
            ret = session.process.poll()
            if ret is not None:
                if session._reader_thread and session._reader_thread.is_alive():
                    session._reader_thread.join(timeout=0.2)
                if session.status == ProcessStatus.RUNNING:
                    status = ProcessStatus.COMPLETED if ret == 0 else ProcessStatus.FAILED
                    session.mark_finished(ret, status)
        return session.to_dict()

    def read_output(
        self,
        process_id: str,
        offset: int = 0,
        limit: int = 2000,
    ) -> dict[str, Any]:
        """Read a slice of output from the circular buffer.

        Returns:
            dict containing process_id, status, output, offset, limit, total_chars, has_more, exit_code.
        """
        session = self.get(process_id)
        if session.status == ProcessStatus.RUNNING:
            self.poll(process_id)
        slice_text, total_chars, has_more = session.buffer.read(offset=offset, limit=limit)
        return {
            "process_id": session.id,
            "status": session.status.value if isinstance(session.status, ProcessStatus) else str(session.status),
            "output": slice_text,
            "offset": offset,
            "limit": limit,
            "total_chars": total_chars,
            "has_more": has_more,
            "exit_code": session.exit_code,
        }

    def get_tail_output(
        self,
        process_id: str,
        n: int = 2000,
        strip_ansi_codes: bool = False,
    ) -> str:
        """Get the last n characters of output for the specified process."""
        session = self.get(process_id)
        return session.buffer.get_tail(n=n, strip_ansi=strip_ansi_codes)

    def wait(self, process_id: str, timeout: float | None = None) -> dict[str, Any]:
        """Block until process finishes or timeout expires; return session dict."""
        session = self.get(process_id)
        if session.status != ProcessStatus.RUNNING:
            return session.to_dict()

        signaled = session._stop_event.wait(timeout=timeout)
        if signaled and session._reader_thread and session._reader_thread.is_alive():
            session._reader_thread.join(timeout=0.5)

        self.poll(process_id)
        return session.to_dict()

    def kill(self, process_id: str, force: bool = False) -> dict[str, Any]:
        """Terminate a background process and its entire process group."""
        session = self.get(process_id)
        if session.status != ProcessStatus.RUNNING:
            return session.to_dict()

        pid = session.pid
        if pid is not None and session.process is not None:
            sig = signal.SIGKILL if force else signal.SIGTERM
            try:
                if hasattr(os, "killpg") and hasattr(os, "getpgid"):
                    try:
                        pgid = os.getpgid(pid)
                        os.killpg(pgid, sig)
                    except (ProcessLookupError, OSError):
                        try:
                            os.kill(pid, sig)
                        except (ProcessLookupError, OSError):
                            pass
                else:
                    os.kill(pid, sig)
            except (ProcessLookupError, OSError):
                pass

            # Wait briefly for graceful exit
            try:
                session.process.wait(timeout=1.0 if not force else 0.3)
            except (subprocess.TimeoutExpired, Exception):
                if not force:
                    try:
                        if hasattr(os, "killpg") and hasattr(os, "getpgid"):
                            try:
                                pgid = os.getpgid(pid)
                                os.killpg(pgid, signal.SIGKILL)
                            except (ProcessLookupError, OSError):
                                os.kill(pid, signal.SIGKILL)
                        else:
                            os.kill(pid, signal.SIGKILL)
                        session.process.wait(timeout=0.5)
                    except Exception:
                        pass

        session.status = ProcessStatus.KILLED
        exit_code = session.process.poll() if session.process else -9
        if exit_code is None:
            exit_code = -9
        session.mark_finished(exit_code, ProcessStatus.KILLED)

        # Signal stop event and join reader thread
        session._stop_event.set()
        if session._reader_thread and session._reader_thread.is_alive():
            session._reader_thread.join(timeout=0.5)

        if session.pty_master_fd is not None:
            try:
                os.close(session.pty_master_fd)
            except OSError:
                pass
            session.pty_master_fd = None

        return session.to_dict()

    def list_processes(self, status_filter: str | None = None) -> list[dict[str, Any]]:
        """List tracked processes sorted by start time, optionally filtered by status."""
        with self._lock:
            sessions = list(self._processes.values())

        for s in sessions:
            if s.status == ProcessStatus.RUNNING:
                self.poll(s.id)

        if status_filter:
            filter_val = status_filter.lower().strip()
            sessions = [
                s for s in sessions
                if (s.status.value if isinstance(s.status, ProcessStatus) else str(s.status)).lower() == filter_val
            ]

        sessions.sort(key=lambda s: s.started_at)
        return [s.to_dict() for s in sessions]

    def cleanup_all(self) -> None:
        """Terminate all running background processes and release system resources."""
        with self._lock:
            sessions = list(self._processes.values())

        for session in sessions:
            if session.status == ProcessStatus.RUNNING and session.process is not None:
                try:
                    self.kill(session.id, force=True)
                except Exception as exc:
                    logger.debug("Error killing process %s during cleanup: %s", session.id, exc)

            if session.pty_master_fd is not None:
                try:
                    os.close(session.pty_master_fd)
                except OSError:
                    pass
                session.pty_master_fd = None

            if session._reader_thread and session._reader_thread.is_alive():
                session._stop_event.set()
                session._reader_thread.join(timeout=0.5)

    def close(self) -> None:
        """Unregister atexit hook and clean up all processes."""
        try:
            atexit.unregister(self.cleanup_all)
        except Exception:
            pass
        self.cleanup_all()


_GLOBAL_REGISTRY: ProcessRegistry | None = None
_GLOBAL_REGISTRY_LOCK = threading.Lock()


def get_process_registry() -> ProcessRegistry:
    """Retrieve the singleton ProcessRegistry instance."""
    global _GLOBAL_REGISTRY
    if _GLOBAL_REGISTRY is None:
        with _GLOBAL_REGISTRY_LOCK:
            if _GLOBAL_REGISTRY is None:
                _GLOBAL_REGISTRY = ProcessRegistry()
    return _GLOBAL_REGISTRY


def reset_process_registry() -> None:
    """Reset and clean up the singleton ProcessRegistry (used in testing)."""
    global _GLOBAL_REGISTRY
    with _GLOBAL_REGISTRY_LOCK:
        if _GLOBAL_REGISTRY is not None:
            _GLOBAL_REGISTRY.close()
            _GLOBAL_REGISTRY = None
