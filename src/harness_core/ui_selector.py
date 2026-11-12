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
            if ch2 == "[":
                ch3 = sys.stdin.read(1)
                return f"\x1b[{ch3}"
            return f"\x1b{ch2}"
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def select_option(
    title: str,
    options: Sequence[str | tuple[str, str]],
    *,
    default_index: int = 0,
    stream_in: Any = None,
    stream_out: Any = None,
) -> int:
    """Present a radio-button selector navigateable via Up/Down arrow keys.

    Args:
        title: The prompt or header to display.
        options: Sequence of strings or (value, label) tuples.
        default_index: Initially highlighted option.
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

    # Interactive TTY loop
    out_stream.write(f"\033[?25l")  # Hide cursor
    try:
        out_stream.write(f"◈ {title}\n")
        for i, lbl in enumerate(labels):
            marker = "[•]" if i == current else "[ ]"
            out_stream.write(f"  {marker} {lbl}\n")
        out_stream.flush()

        while True:
            key = _read_key(in_stream)

            if key in ("\x1b[A", "k", "K"):  # Up
                current = (current - 1) % total
            elif key in ("\x1b[B", "j", "J"):  # Down
                current = (current + 1) % total
            elif key.isdigit() and 1 <= int(key) <= total:
                current = int(key) - 1
            elif key in ("\r", "\n"):  # Enter
                break
            elif key in ("\x03", "q", "Q"):  # Ctrl+C or q
                out_stream.write("\n")
                out_stream.flush()
                raise SelectorCancelledError("Selection cancelled by user.")

            # Move cursor up to overwrite options
            out_stream.write(f"\033[{total}A")
            for i, lbl in enumerate(labels):
                marker = "[•]" if i == current else "[ ]"
                out_stream.write(f"\r\033[K  {marker} {lbl}\n")
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

    out_stream.write(f"\033[?25l")  # Hide cursor
    try:
        out_stream.write(f"◈ {title} (Space to toggle, Enter to confirm)\n")
        for i, lbl in enumerate(labels):
            marker = "[✓]" if i in selected else "[ ]"
            highlight = "► " if i == current else "  "
            out_stream.write(f"{highlight}{marker} {lbl}\n")
        out_stream.flush()

        while True:
            key = _read_key(in_stream)

            if key in ("\x1b[A", "k", "K"):
                current = (current - 1) % total
            elif key in ("\x1b[B", "j", "J"):
                current = (current + 1) % total
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

            out_stream.write(f"\033[{total}A")
            for i, lbl in enumerate(labels):
                marker = "[✓]" if i in selected else "[ ]"
                highlight = "► " if i == current else "  "
                out_stream.write(f"\r\033[K{highlight}{marker} {lbl}\n")
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
