"""Interactive Terminal Selection and Secret Input Utilities.

Provides a clean, zero-dependency POSIX terminal selector supporting:
- Arrow keys (↑ / ↓) and Vim keys (k / j) navigation.
- Radio selection indicator: `[•]` for selected, `[ ]` for unselected.
- Multi-select checkbox indicator: `[✓]` / `[ ]` with Space toggling.
- Masked secret input (`••••••••`) for secure API key and token entry.
- Automatic non-TTY fallback for CI/CD, redirected stdin, and test harnesses.
"""

from __future__ import annotations

import getpass
import os
import shutil
import sys
from typing import Any, Sequence


class SelectorCancelledError(Exception):
    """Raised when the operator cancels selection via Ctrl+C or 'q'."""


def _read_key(stream: Any = None) -> str:
    """Read a single key or ANSI escape sequence from stdin."""
    in_stream = stream or sys.stdin

    # Check if stream is a real TTY
    if not hasattr(in_stream, "isatty") or not in_stream.isatty():
        line = in_stream.readline()
        if not line:
            return "\n"
        return line.strip()

    import termios
    import tty

    fd = in_stream.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            # Possible ANSI escape sequence
            ch2 = sys.stdin.read(1)
            if ch2 in ("[", "O"):
                seq = [ch, ch2]
                while True:
                    next_ch = sys.stdin.read(1)
                    seq.append(next_ch)
                    if next_ch.isalpha() or next_ch in ("~", "\n", "\r") or not next_ch:
                        break
                return "".join(seq)
            return f"\x1b{ch2}"
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def _adjust_window(current: int, window_start: int, window_size: int, total: int) -> int:
    """Ensure current highlighted option is visible within [window_start, window_start + window_size)."""
    if current < window_start:
        window_start = current
    elif current >= window_start + window_size:
        window_start = current - window_size + 1
    return max(0, min(window_start, max(0, total - window_size)))


# Standard ANSI / Terminal Key Mappings
_UP_KEYS = ("\x1b[A", "\x1bOA", "k", "K")
_DOWN_KEYS = ("\x1b[B", "\x1bOB", "j", "J")
_PGUP_KEYS = ("\x1b[5~", "\x15")  # PageUp, Ctrl+U
_PGDN_KEYS = ("\x1b[6~", "\x04")  # PageDown, Ctrl+D
_HOME_KEYS = ("\x1b[H", "\x1b[1~", "\x1bOH", "\x01")  # Home, Ctrl+A
_END_KEYS = ("\x1b[F", "\x1b[4~", "\x1bOF", "\x05")  # End, Ctrl+E


def select_option(
    title: str,
    options: Sequence[str | tuple[str, str]],
    *,
    default_index: int = 0,
    max_visible: int | None = None,
    stream_in: Any = None,
    stream_out: Any = None,
) -> int:
    """Present a radio-button selector navigateable via Up/Down arrow keys.

    Features scroll-locked viewport windowing when the list of options
    exceeds visible terminal height, preventing cursor drift and scroll overflow.

    Args:
        title: The prompt or header to display.
        options: Sequence of strings or (value, label) tuples.
        default_index: Initially highlighted option.
        max_visible: Maximum number of options visible in the viewport window.
        stream_in: Input stream (defaults to sys.stdin).
        stream_out: Output stream (defaults to sys.stdout).

    Returns:
        Index of the chosen option.
    """
    in_stream = stream_in or sys.stdin
    out_stream = stream_out or sys.stdout

    if not options:
        raise ValueError("Cannot select from an empty list of options.")

    labels: list[str] = []
    for opt in options:
        if isinstance(opt, tuple):
            labels.append(opt[1])
        else:
            labels.append(str(opt))

    total = len(labels)
    current = max(0, min(default_index, total - 1))

    # Fallback if not interactive TTY
    if not hasattr(in_stream, "isatty") or not in_stream.isatty():
        out_stream.write(f"◈ {title}\n")
        for i, lbl in enumerate(labels):
            marker = "[•]" if i == current else "[ ]"
            out_stream.write(f"  {marker} {i + 1}. {lbl}\n")
        out_stream.flush()
        try:
            line = in_stream.readline()
            if line:
                val = line.strip()
                if val.isdigit():
                    num = int(val)
                    if 1 <= num <= total:
                        return num - 1
        except Exception:
            pass
        return current

    # Viewport window dimensions
    term_size = shutil.get_terminal_size((80, 24))
    term_lines = term_size.lines
    term_cols = term_size.columns

    if max_visible is not None:
        window_size = min(total, max(3, max_visible))
    else:
        # Default viewport budget: up to 14 rows, leaving margins for title & shell prompt
        window_size = min(total, max(5, min(14, term_lines - 8)))

    is_windowed = total > window_size
    window_start = _adjust_window(current, 0, window_size, total) if is_windowed else 0

    def _render_frame() -> list[str]:
        lines: list[str] = []
        if is_windowed:
            # Top scroll indicator
            if window_start > 0:
                lines.append(f"  ▲ {window_start} more above (↑/k/PgUp)")
            else:
                lines.append("  ────────────────────────────────")

            # Visible window slice
            for idx in range(window_start, window_start + window_size):
                marker = "[•]" if idx == current else "[ ]"
                lbl = labels[idx]
                prefix = f"  {marker} "
                avail = max(10, term_cols - len(prefix) - 2)
                if len(lbl) > avail:
                    lbl = lbl[: avail - 3] + "..."
                lines.append(f"{prefix}{lbl}")

            # Bottom scroll indicator
            rem = total - (window_start + window_size)
            if rem > 0:
                lines.append(f"  ▼ {rem} more below (↓/j/PgDn)")
            else:
                lines.append("  ────────────────────────────────")
        else:
            for idx, lbl in enumerate(labels):
                marker = "[•]" if idx == current else "[ ]"
                prefix = f"  {marker} "
                avail = max(10, term_cols - len(prefix) - 2)
                if len(lbl) > avail:
                    lbl = lbl[: avail - 3] + "..."
                lines.append(f"{prefix}{lbl}")
        return lines

    # Interactive TTY loop
    out_stream.write(f"\033[?25l")  # Hide cursor
    try:
        out_stream.write(f"◈ {title}\n")
        frame_lines = _render_frame()
        for fl in frame_lines:
            out_stream.write(f"{fl}\n")
        out_stream.flush()

        while True:
            key = _read_key(in_stream)

            if key in _UP_KEYS:
                current = (current - 1) % total
            elif key in _DOWN_KEYS:
                current = (current + 1) % total
            elif key in _PGUP_KEYS:
                step = window_size if is_windowed else 5
                current = max(0, current - step)
            elif key in _PGDN_KEYS:
                step = window_size if is_windowed else 5
                current = min(total - 1, current + step)
            elif key in _HOME_KEYS:
                current = 0
            elif key in _END_KEYS:
                current = total - 1
            elif key.isdigit() and 1 <= int(key) <= min(9, total):
                current = int(key) - 1
            elif key in ("\r", "\n"):  # Enter
                break
            elif key in ("\x03", "q", "Q"):  # Ctrl+C or q
                out_stream.write("\n")
                out_stream.flush()
                raise SelectorCancelledError("Selection cancelled by user.")

            if is_windowed:
                window_start = _adjust_window(current, window_start, window_size, total)

            out_stream.write(f"\033[{len(frame_lines)}A")
            frame_lines = _render_frame()
            for fl in frame_lines:
                out_stream.write(f"\r\033[K{fl}\n")
            out_stream.flush()

    finally:
        out_stream.write(f"\033[?25h")  # Restore cursor
        out_stream.flush()

    return current


def select_multiple(
    title: str,
    options: Sequence[str | tuple[str, str]],
    *,
    default_selected: Sequence[int] | None = None,
    max_visible: int | None = None,
    stream_in: Any = None,
    stream_out: Any = None,
) -> list[int]:
    """Present a multi-select checkbox selector (Space to toggle, Enter to confirm)."""
    in_stream = stream_in or sys.stdin
    out_stream = stream_out or sys.stdout

    if not options:
        raise ValueError("Cannot select from an empty list of options.")

    labels: list[str] = []
    for opt in options:
        if isinstance(opt, tuple):
            labels.append(opt[1])
        else:
            labels.append(str(opt))

    total = len(labels)
    current = 0
    selected: set[int] = set(default_selected or [])

    if not hasattr(in_stream, "isatty") or not in_stream.isatty():
        out_stream.write(f"◈ {title}\n")
        for i, lbl in enumerate(labels):
            marker = "[✓]" if i in selected else "[ ]"
            out_stream.write(f"  {marker} {i + 1}. {lbl}\n")
        out_stream.flush()
        return sorted(selected)

    # Viewport window dimensions
    term_size = shutil.get_terminal_size((80, 24))
    term_lines = term_size.lines
    term_cols = term_size.columns

    if max_visible is not None:
        window_size = min(total, max(3, max_visible))
    else:
        window_size = min(total, max(5, min(14, term_lines - 8)))

    is_windowed = total > window_size
    window_start = _adjust_window(current, 0, window_size, total) if is_windowed else 0

    def _render_frame() -> list[str]:
        lines: list[str] = []
        if is_windowed:
            if window_start > 0:
                lines.append(f"  ▲ {window_start} more above (↑/k/PgUp)")
            else:
                lines.append("  ────────────────────────────────")

            for idx in range(window_start, window_start + window_size):
                marker = "[✓]" if idx in selected else "[ ]"
                highlight = "► " if idx == current else "  "
                lbl = labels[idx]
                prefix = f"{highlight}{marker} "
                avail = max(10, term_cols - len(prefix) - 2)
                if len(lbl) > avail:
                    lbl = lbl[: avail - 3] + "..."
                lines.append(f"{prefix}{lbl}")

            rem = total - (window_start + window_size)
            if rem > 0:
                lines.append(f"  ▼ {rem} more below (↓/j/PgDn)")
            else:
                lines.append("  ────────────────────────────────")
        else:
            for idx, lbl in enumerate(labels):
                marker = "[✓]" if idx in selected else "[ ]"
                highlight = "► " if idx == current else "  "
                lbl = labels[idx]
                prefix = f"{highlight}{marker} "
                avail = max(10, term_cols - len(prefix) - 2)
                if len(lbl) > avail:
                    lbl = lbl[: avail - 3] + "..."
                lines.append(f"{prefix}{lbl}")
        return lines

    out_stream.write(f"\033[?25l")  # Hide cursor
    try:
        out_stream.write(f"◈ {title} (Space to toggle, Enter to confirm)\n")
        frame_lines = _render_frame()
        for fl in frame_lines:
            out_stream.write(f"{fl}\n")
        out_stream.flush()

        while True:
            key = _read_key(in_stream)

            if key in _UP_KEYS:
                current = (current - 1) % total
            elif key in _DOWN_KEYS:
                current = (current + 1) % total
            elif key in _PGUP_KEYS:
                step = window_size if is_windowed else 5
                current = max(0, current - step)
            elif key in _PGDN_KEYS:
                step = window_size if is_windowed else 5
                current = min(total - 1, current + step)
            elif key in _HOME_KEYS:
                current = 0
            elif key in _END_KEYS:
                current = total - 1
            elif key == " ":  # Space toggle
                if current in selected:
                    selected.remove(current)
                else:
                    selected.add(current)
            elif key in ("\r", "\n"):  # Enter
                break
            elif key in ("\x03", "q", "Q"):
                out_stream.write("\n")
                out_stream.flush()
                raise SelectorCancelledError("Multi-selection cancelled by user.")

            if is_windowed:
                window_start = _adjust_window(current, window_start, window_size, total)

            out_stream.write(f"\033[{len(frame_lines)}A")
            frame_lines = _render_frame()
            for fl in frame_lines:
                out_stream.write(f"\r\033[K{fl}\n")
            out_stream.flush()

    finally:
        out_stream.write(f"\033[?25h")
        out_stream.flush()

    return sorted(selected)


def prompt_secret(
    prompt: str = "Enter API Key / Token: ",
    *,
    mask_char: str = "•",
    stream_in: Any = None,
    stream_out: Any = None,
) -> str:
    """Prompt the operator for a secret credential with visual masking."""
    in_stream = stream_in or sys.stdin
    out_stream = stream_out or sys.stdout

    if not hasattr(in_stream, "isatty") or not in_stream.isatty():
        try:
            return getpass.getpass(prompt=prompt)
        except Exception:
            line = in_stream.readline()
            return line.strip()

    import termios
    import tty

    out_stream.write(prompt)
    out_stream.flush()

    fd = in_stream.fileno()
    old_settings = termios.tcgetattr(fd)
    chars: list[str] = []

    try:
        tty.setraw(fd)
        while True:
            ch = sys.stdin.read(1)
            if ch in ("\r", "\n"):
                out_stream.write("\n")
                out_stream.flush()
                break
            elif ch in ("\x7f", "\x08"):  # Backspace
                if chars:
                    chars.pop()
                    out_stream.write("\b \b")
                    out_stream.flush()
            elif ch in ("\x03", "\x04"):  # Ctrl+C / Ctrl+D
                out_stream.write("\n")
                out_stream.flush()
                raise SelectorCancelledError("Input cancelled by user.")
            elif ch == "\x1b":  # Ignore escape sequences
                _ = sys.stdin.read(1)
                _ = sys.stdin.read(1)
            else:
                chars.append(ch)
                out_stream.write(mask_char)
                out_stream.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    return "".join(chars).strip()
