import os
import queue
import sys
import threading
import time
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ConPTY input regression")
def test_windows_conpty_paste_and_native_input(tmp_path):
    import ctypes
    from ctypes import wintypes

    root = Path(__file__).resolve().parents[2]
    binary = Path(os.environ.get("HEX_TUI_TEST_BINARY", root / "target/debug/hex-tui.exe"))
    if not binary.is_absolute():
        binary = root / binary
    if not binary.is_file():
        pytest.skip(f"HEX TUI binary absent: {binary}; set HEX_TUI_TEST_BINARY to a built executable")
    winpty = pytest.importorskip("winpty", reason="ConPTY regression requires optional pywinpty")
    pyte = pytest.importorskip("pyte", reason="ConPTY regression requires optional pyte")

    workspace = tmp_path / "workspace"
    snapshots = tmp_path / "snapshots"
    workspace.mkdir()
    snapshots.mkdir()
    missing_python = workspace / "hex-no-runtime-test.exe"
    assert not missing_python.exists()
    env = dict(
        os.environ,
        HEX_PYTHON_BIN=str(missing_python),
        PATH=str(Path(os.environ["SystemRoot"]) / "System32"),
    )

    class Key(ctypes.Structure):
        _fields_ = [
            ("down", wintypes.BOOL),
            ("repeat", wintypes.WORD),
            ("vk", wintypes.WORD),
            ("scan", wintypes.WORD),
            ("char", ctypes.c_uint16),
            ("modifiers", wintypes.DWORD),
        ]

    class Data(ctypes.Union):
        _fields_ = [("key", Key), ("padding", ctypes.c_byte * 16)]

    class Record(ctypes.Structure):
        _fields_ = [("kind", wintypes.WORD), ("data", Data)]

    assert ctypes.sizeof(Key) == 16 and ctypes.sizeof(Record) == 20
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.WriteConsoleInputW.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(Record), wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel.WriteConsoleInputW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.FreeConsole.argtypes = []
    kernel.FreeConsole.restype = wintypes.BOOL
    kernel.AttachConsole.argtypes = [wintypes.DWORD]
    kernel.AttachConsole.restype = wintypes.BOOL
    kernel.SetConsoleCP.argtypes = [wintypes.UINT]
    kernel.SetConsoleCP.restype = wintypes.BOOL
    kernel.SetConsoleOutputCP.argtypes = [wintypes.UINT]
    kernel.SetConsoleOutputCP.restype = wintypes.BOOL
    kernel.GetConsoleProcessList.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
    kernel.GetConsoleProcessList.restype = wintypes.DWORD
    console_pids = (wintypes.DWORD * 256)()
    count = kernel.GetConsoleProcessList(console_pids, len(console_pids))
    if count > len(console_pids):
        console_pids = (wintypes.DWORD * count)()
        count = kernel.GetConsoleProcessList(console_pids, len(console_pids))
    original_console_pid = next(
        (pid for pid in console_pids[:count] if pid != os.getpid()), None,
    )

    proc = winpty.PtyProcess.spawn(
        [str(binary), "--workspace", str(workspace)],
        cwd=str(root), env=env, dimensions=(36, 120), backend=0,
    )
    screen = pyte.Screen(120, 36)
    stream = pyte.Stream(screen)
    chunks = queue.Queue()
    raw = []
    eof_seen = False
    failed = False

    def reader():
        # Do not stop at isalive(): ConPTY can still have buffered output after exit.
        try:
            while True:
                chunk = proc.read(65536)
                if chunk:
                    chunks.put(chunk)
        except EOFError:
            pass
        except Exception as exc:
            chunks.put(exc)
        finally:
            chunks.put(None)

    def pump(timeout):
        nonlocal eof_seen
        try:
            chunk = chunks.get(timeout=timeout)
        except queue.Empty:
            return False
        if chunk is None:
            eof_seen = True
            return False
        if isinstance(chunk, Exception):
            raise AssertionError("ConPTY output reader failed") from chunk
        raw.append(chunk)
        stream.feed(chunk)
        return True

    def present(text):
        return any(text in row for row in screen.display)

    def draft_is(*lines):
        # Only the bottommost prompt is the composer, not an old transcript row.
        rows = screen.display
        index = next((i for i in range(len(rows) - 1, -1, -1) if "> " in rows[i]), None)
        if index is None or index + len(lines) > len(rows):
            return False
        column = rows[index].index("> ") + 2
        return all(
            rows[index + offset][column:].partition("│")[0].rstrip() == text
            for offset, text in enumerate(lines)
        )

    def consecutive_rows(*lines):
        # The two-row start composer scrolls its first line away after Shift+Enter.
        rows = screen.display
        for index, row in enumerate(rows):
            column = row.find(lines[0])
            if column >= 0 and index + len(lines) <= len(rows) and all(
                rows[index + offset][column:].partition("│")[0].rstrip() == text
                for offset, text in enumerate(lines)
            ):
                return True
        return False

    def wait_for(label, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        matching_since = None
        saw_output = False
        while time.monotonic() < deadline:
            saw_output |= pump(min(0.1, max(0, deadline - time.monotonic())))
            while time.monotonic() < deadline and pump(0):
                saw_output = True
            assert not eof_seen, f"Terminal exited unexpectedly while waiting for {label}"
            now = time.monotonic()
            if saw_output and predicate():
                if matching_since is None:
                    matching_since = now
                # Consume more frames to detect duplicated release text or submissions.
                if now - matching_since >= 0.3:
                    return
            else:
                matching_since = None
        raise AssertionError(f"Timed out waiting for {label}\n" + "\n".join(screen.display))

    def capture(name):
        (snapshots / f"{name}.txt").write_text("\n".join(screen.display), encoding="utf-8")

    def native_key(vk, char=0, modifiers=0, down=True):
        kernel.FreeConsole()  # A headless pytest process need not have a console.
        handle = None
        try:
            if not kernel.AttachConsole(proc.pid):
                raise ctypes.WinError(ctypes.get_last_error())
            if not kernel.SetConsoleCP(65001) or not kernel.SetConsoleOutputCP(65001):
                raise ctypes.WinError(ctypes.get_last_error())
            handle = kernel.CreateFileW("CONIN$", 0xC0000000, 3, None, 3, 0, None)
            if handle in (None, ctypes.c_void_p(-1).value):
                raise ctypes.WinError(ctypes.get_last_error())
            entries = [Record(1, Data(key=Key(down, 1, vk, 0, char, modifiers)))]
            if down:
                # Pair releases so subsequent injected presses are not held repeats.
                entries.append(Record(1, Data(key=Key(False, 1, vk, 0, char, modifiers))))
            records = (Record * len(entries))(*entries)
            written = wintypes.DWORD()
            if not kernel.WriteConsoleInputW(handle, records, len(records), ctypes.byref(written)):
                raise ctypes.WinError(ctypes.get_last_error())
            assert written.value == len(records), "Native key records were only partially written"
        finally:
            if handle not in (None, ctypes.c_void_p(-1).value):
                kernel.CloseHandle(handle)
            kernel.FreeConsole()
            if original_console_pid is not None:
                if not kernel.AttachConsole(original_console_pid):
                    raise ctypes.WinError(ctypes.get_last_error())

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()
    try:
        wait_for("initial composer", lambda: present("commands") and present("shift+enter"), timeout=30)
        capture("initial")
        native_key(112)  # F1
        wait_for("F1 help", lambda: present("HEX AGENT HELP"))
        proc.write("\x1b[200~ignored-modal\n\x03\x1b[201~")
        # Drain queued paste before closing help so F1 cannot race its control characters.
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            pump(min(0.1, max(0, deadline - time.monotonic())))
            assert not eof_seen, "Pasted Ctrl-C escaped the help modal"
            assert present("HEX AGENT HELP"), "Pasted controls closed the help modal"
            assert not present("ignored-modal") and not present("Received:")
        native_key(112)
        wait_for("closed help", lambda: not present("HEX AGENT HELP") and present("shift+enter"))
        assert not present("ignored-modal") and not present("Received:")
        assert not present("[200~") and not present("[201~")
        capture("modal_boundary")

        proc.write("\x1b[200~first-line\nworld-line\x1b[201~")
        wait_for("multiline draft", lambda: draft_is("first-line", "world-line"))
        assert not present("Received:"), "Bracketed paste submitted before Enter"
        assert not present("[200~") and not present("[201~"), "Paste markers leaked into the draft"
        capture("multiline_paste")

        native_key(13, 13, modifiers=0x10)  # Native Shift+Enter
        proc.write("last-line")
        wait_for("Shift+Enter newline", lambda: consecutive_rows("world-line", "last-line"))
        assert not present("Received:"), "Shift+Enter submitted instead of inserting a newline"
        capture("shift_enter")

        native_key(13, 13, down=False)
        native_key(88, ord("x"), down=False)
        native_key(88, ord("x"))
        wait_for("ignored native releases", lambda: consecutive_rows("world-line", "last-linex"))
        assert not present("last-linexx") and not present("Received:"), "Release events changed the draft"
        capture("native_release_filter")

        proc.write("\r")
        wait_for("plain Enter submission", lambda: present("Received:") and draft_is("Message hex"))
        assert sum("Received:" in row for row in screen.display) == 1, "Enter submitted more than once"
        assert present("> first-line") and consecutive_rows("first-line", "world-line", "last-linex"), (
            "Submitted multiline text did not preserve the draft"
        )
        # Markdown folds soft breaks, but the user transcript must retain submitted newlines.
        assert present('Received: "first-line world-line last-linex".')
        capture("single_submission")

        row = next(i for i, text in enumerate(screen.display) if "▾ context" in text)
        column = screen.display[row].index("▾ context")
        click = f"\x1b[<0;{column + 1};{row + 1}M\x1b[<0;{column + 1};{row + 1}m"
        proc.write(click)
        wait_for("sidebar mouse collapse", lambda: present("▸ context"))
        proc.write(click)
        wait_for("sidebar mouse expansion", lambda: present("▾ context"))
        capture("mouse_sidebar")

        proc.write("\x1b[200~世界🙂\x1b[201~")
        wait_for("CJK and emoji draft", lambda: draft_is("世界🙂"))
        assert sum("Received:" in row for row in screen.display) == 1
        native_key(37)  # Left: move before the emoji.
        native_key(8, 8)  # Backspace: remove 界, not a byte or the emoji.
        wait_for("Unicode cursor/backspace", lambda: draft_is("世🙂"))
        capture("unicode_edit")
        proc.write("\r")
        wait_for("Unicode submission", lambda: present('Received: "世🙂"') and draft_is("Message hex"))
        assert sum("Received:" in row for row in screen.display) == 2
        # Empty-composer arrows scroll the transcript; history navigation starts with text.
        proc.write("recall")
        wait_for("history navigation draft", lambda: draft_is("recall"))
        native_key(38)
        wait_for("Unicode history", lambda: draft_is("世🙂"))
        native_key(8, 8)
        wait_for("Unicode history backspace", lambda: draft_is("世"))
        capture("unicode_history")

        # Below the minimum height, stale emulator rows can masquerade as an editable composer.
        proc.setwinsize(24, 80)
        screen.resize(24, 80)
        proc.write("z")
        wait_for("editable resized draft", lambda: draft_is("世z"))
        assert (screen.lines, screen.columns) == (24, 80)
        assert not present("Expand terminal"), "Resize hid the editable composer"
        capture("resize")

        proc.write("\x03")
        deadline = time.monotonic() + 10
        while not eof_seen and time.monotonic() < deadline:
            pump(min(0.2, max(0, deadline - time.monotonic())))
        assert eof_seen, "Ctrl-C did not drain ConPTY to EOF within 10 seconds"
        reader_thread.join(timeout=2)
        assert not reader_thread.is_alive(), "ConPTY reader did not finish after EOF"
        assert not proc.isalive(), "Ctrl-C did not stop the TUI and its input thread"
        assert proc.exitstatus == 0, f"TUI exited with {proc.exitstatus}"
    except BaseException:
        failed = True
        raise
    finally:
        try:
            if failed and proc.isalive():
                proc.terminate(force=True)  # Only this test's TUI, never another console process.
        finally:
            reader_thread.join(timeout=5)
            while not chunks.empty():
                chunk = chunks.get_nowait()
                if isinstance(chunk, str):
                    raw.append(chunk)
                    stream.feed(chunk)
            (snapshots / "terminal_output.txt").write_text("".join(raw), encoding="utf-8")
            if failed:
                capture("failure")
                print(f"\nConPTY failure snapshots: {snapshots}\n" + "\n".join(screen.display))
                print("Raw ConPTY output: " + repr("".join(raw)))
