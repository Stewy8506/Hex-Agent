"""PTY Process Supervisor & Circular Stream Registry.

Matches Hermes Agent background process architecture:
- Thread-safe background process lifecycle management (spawn, poll, read_output, wait, kill, list)
- POSIX pseudo-terminal (PTY) allocation with transparent pipe fallback
- Rolling 200 KB circular ring buffer (CircularOutputBuffer) with non-blocking background reader threads
- Monotonic timestamps and comprehensive process states (RUNNING, COMPLETED, FAILED, KILLED)
- atexit cleanup hook to prevent orphan or zombie processes on the host
"""

from __future__ import annotations

from dataclasses import dataclass, field
import enum
import logging
import os
import re
import threading
import time
from typing import Any

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

            end_in_buf = min(buf_len, start_in_buf + max(0, limit))
            slice_text = self._buffer[start_in_buf:end_in_buf]
            has_more = end_in_buf < buf_len
            return (slice_text, total, has_more)

    def get_tail(self, n: int = 2000, strip_ansi_codes: bool = False) -> str:
        """Get the last n characters of the buffer."""
        with self._lock:
            tail = self._buffer[-n:] if n > 0 else ""
        if strip_ansi_codes:
            return strip_ansi(tail)
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
