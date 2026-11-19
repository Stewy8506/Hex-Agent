import json
import os
import queue
import struct
import subprocess
import textwrap
import sys
import threading
import time
from pathlib import Path

import pytest

try:
    import pyte as _pyte
except ImportError:  # pragma: no cover - optional dependency
    _pyte = None

if _pyte is not None:
    # WHY: pyte's renderer asserts on wide-character stub cells left by ConPTY
    # wraps; joining cell data preserves identical text/column semantics.
    def _safe_display(self):
        return ["".join(self.buffer[y][x].data for x in range(self.columns))
                for y in range(self.lines)]

    _pyte.screens.Screen.display = property(_safe_display)


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


# Production interpreter probing must not start the fixture's ACP loop.
_PHASE32_ACP_FIXTURE = r'''
import json
import sys
import threading
import time
from pathlib import Path


def main():
    assert sys.argv[1] == "acp"
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    root = Path(__file__).resolve().parents[2]
    lock = threading.Lock()
    session = "phase32-session"
    prompts = {}
    attempts = {}
    cancelled_turns = set()
    counters = {"model": 0, "cancel": 0}

    def record(event, message):
        with lock:
            with (root / "requests.jsonl").open("a", encoding="utf-8") as log:
                log.write(json.dumps(dict(event=event, **message), ensure_ascii=False) + "\n")

    def send(message):
        record("sent", message)
        with lock:
            print(json.dumps(dict(jsonrpc="2.0", **message), ensure_ascii=False), flush=True)

    def reply(request, result=None, error=None):
        send(dict(id=request["id"], **({"error": {"code": -32001, "message": error}}
                                     if error else {"result": result})))

    def gate(name):
        deadline = time.monotonic() + 30
        while not (root / name).exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("Fixture gate timed out: " + name)
            threading.Event().wait(0.02)

    def terminal(name, cancelled=False):
        gate("finish-" + name)
        send({"method": "session/update", "params": {
            "sessionId": session, "updateType": "status",
            "status": "Cancelled" if cancelled else "Ready",
        }})
        reply(prompts[name], {"sessionId": session, "status": "completed",
                             "content": [{"type": "text", "text": "FINISHED " + name}]})

    def decision(request, name, attempt):
        gate("decision-" + name + "-" + str(attempt))
        params = request["params"]
        if name == "approval-once" and attempt == 1:
            reply(request, error="temporary approval failure")
            return
        result = {"approvalId": name, "status": "applied", "decision": params["decision"]}
        if name == "approval-once":
            if attempt == 2:
                result["status"] = "acknowledged"
            elif attempt == 3:
                result["approvalId"] = "not-this-approval"
            elif attempt == 4:
                result["decision"] = "reject"
        reply(request, result)
        if name not in cancelled_turns and result == {
            "approvalId": name, "status": "applied", "decision": params["decision"],
        }:
            terminal(name)

    def model(request, attempt):
        gate("model-" + str(attempt))
        if attempt == 1:
            reply(request, error="model unavailable")
        else:
            rejected = attempt == 2
            reply(request, {
                "sessionId": session, "command": "/model", "isError": rejected,
                "output": "MODEL_REJECTED" if rejected else "MODEL_APPLIED",
                "model": "model-rejected" if rejected else "model-canonical",
                "provider": "provider-rejected" if rejected else "provider-canonical",
            })

    def cancel(request, attempt):
        gate("cancel-" + str(attempt))
        if attempt == 1:
            reply(request, error="temporary cancellation failure")
        else:
            cancelled_turns.add("approval-cancel")
            reply(request, {"sessionId": session, "cancelled": True})
            terminal("approval-cancel", cancelled=True)

    def worker(function, *args):
        threading.Thread(target=function, args=args, daemon=True).start()

    for line in sys.stdin:
        request = json.loads(line)
        record("request", request)
        method = request["method"]
        params = request.get("params") or {}
        if method == "initialize":
            reply(request, {
                "protocolVersion": "2024-11-05", "serverInfo": {"name": "fixture", "version": "1"},
                "capabilities": {"session": {"streaming": True, "cancel": True, "history": False},
                                 "tools": {"listChanged": False}},
            })
        elif method == "session/new":
            # Deliberately omit toolsCount/skillsCount: unknown must not become 26/83.
            reply(request, {"sessionId": session, "cwd": params.get("cwd", str(root)),
                            "model": "model-prior", "provider": "provider-prior"})
        elif method == "session/prompt":
            name = params["prompt"]
            if name == "ready":
                reply(request, {"sessionId": session, "status": "completed",
                                "content": [{"type": "text", "text": "FIXTURE_READY"}]})
                continue
            assert name in {"approval-once", "approval-scope", "approval-deny", "approval-cancel"}
            prompts[name] = request
            send({"method": "session/approvalRequest", "params": {
                "sessionId": session, "approvalId": name, "toolName": "fixture_patch",
                "riskLevel": "R3 - ELEVATED", "cwd": "C:/fixture/workspace",
                "parameters": "path=fixtures/世界.txt\ncontent=hello 🙂\nsecond line stays separate",
                "reason": "Review café change\nKeep both lines visible.",
                "blastRadius": "Only fixtures/世界.txt\nNo tools executed.",
            }})
        elif method == "session/approvalDecision":
            name = params["approvalId"]
            attempts[name] = attempts.get(name, 0) + 1
            worker(decision, request, name, attempts[name])
        elif method == "session/command" and params["command"] == "/model":
            counters["model"] += 1
            worker(model, request, counters["model"])
        elif method == "session/cancel":
            counters["cancel"] += 1
            worker(cancel, request, counters["cancel"])
        elif method == "ping":
            reply(request, {"pong": True})
        elif request.get("id") is not None:
            reply(request, error="Unexpected fixture method: " + method)


if __name__ == "__main__":
    main()
'''


def _conpty_native_key_sender(proc):
    """Inject paired Win32 KEY_EVENT_RECORDs, restoring pytest's original console."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateFileW": ([wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                         wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE], wintypes.HANDLE),
        "WriteConsoleInputW": ([wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        "FreeConsole": ([], wintypes.BOOL),
        "AttachConsole": ([wintypes.DWORD], wintypes.BOOL),
        "SetConsoleCP": ([wintypes.UINT], wintypes.BOOL),
        "SetConsoleOutputCP": ([wintypes.UINT], wintypes.BOOL),
        "GetConsoleProcessList": ([ctypes.POINTER(wintypes.DWORD), wintypes.DWORD], wintypes.DWORD),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, result
    pids = (wintypes.DWORD * 256)()
    count = kernel.GetConsoleProcessList(pids, len(pids))
    if count > len(pids):
        pids = (wintypes.DWORD * count)()
        count = kernel.GetConsoleProcessList(pids, len(pids))
    original_pid = next((pid for pid in pids[:count] if pid != os.getpid()), None)
    # Event type + union alignment, then BOOL/repeat/vk/scan/WCHAR/modifiers.
    record_format = "<HHIHHHHI"
    assert struct.calcsize(record_format) == 20

    def key(vk, char=0, modifiers=0):
        kernel.FreeConsole()
        handle = None
        try:
            if not kernel.AttachConsole(proc.pid):
                raise ctypes.WinError(ctypes.get_last_error())
            if not kernel.SetConsoleCP(65001) or not kernel.SetConsoleOutputCP(65001):
                raise ctypes.WinError(ctypes.get_last_error())
            handle = kernel.CreateFileW("CONIN$", 0xC0000000, 3, None, 3, 0, None)
            if handle in (None, ctypes.c_void_p(-1).value):
                raise ctypes.WinError(ctypes.get_last_error())
            records = ctypes.create_string_buffer(b"".join(
                struct.pack(record_format, 1, 0, down, 1, vk, 0, char, modifiers)
                for down in (1, 0)
            ))
            written = wintypes.DWORD()
            if not kernel.WriteConsoleInputW(handle, records, 2, ctypes.byref(written)):
                raise ctypes.WinError(ctypes.get_last_error())
            assert written.value == 2, "Native key records were only partially written"
        finally:
            if handle not in (None, ctypes.c_void_p(-1).value):
                kernel.CloseHandle(handle)
            kernel.FreeConsole()
            if original_pid is not None and not kernel.AttachConsole(original_pid):
                raise ctypes.WinError(ctypes.get_last_error())

    return key


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ConPTY approval/model regression")
def test_windows_conpty_approval_and_model_confirmation(tmp_path):
    root = Path(__file__).resolve().parents[2]
    binary = Path(os.environ.get("HEX_TUI_TEST_BINARY", root / "target/debug/hex-tui.exe"))
    if not binary.is_absolute():
        binary = root / binary
    if not binary.is_file():
        pytest.skip(f"HEX TUI binary absent: {binary}; set HEX_TUI_TEST_BINARY to a built executable")
    winpty = pytest.importorskip("winpty", reason="ConPTY regression requires optional pywinpty")
    pyte = pytest.importorskip("pyte", reason="ConPTY regression requires optional pyte")
    pytest.importorskip("yaml", reason="ACP interpreter probe requires PyYAML in the test venv")

    runtime = tmp_path / "runtime"
    client = runtime / "harness_core" / "client"
    client.mkdir(parents=True)
    (client.parent / "__init__.py").write_text("", encoding="utf-8")
    (client / "__init__.py").write_text("", encoding="utf-8")
    (client / "cli.py").write_text(textwrap.dedent(_PHASE32_ACP_FIXTURE), encoding="utf-8")
    workspace = tmp_path / "workspace"
    snapshots = tmp_path / "snapshots"
    workspace.mkdir()
    snapshots.mkdir()
    env = dict(os.environ, HEX_REPO_ROOT=str(runtime), HEX_PYTHON_BIN=sys.executable,
               PYTHONUTF8="1", PYTHONPATH=str(runtime))
    # Prove the exact production preflight import completes without entering stdin's loop.
    probe = subprocess.run(
        [sys.executable, "-c", "import harness_core.client.cli, yaml"],
        cwd=runtime, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
    )
    assert probe.returncode == 0, probe.stderr.decode("utf-8", errors="replace")
    ledger = runtime / "requests.jsonl"
    assert not ledger.exists(), "Import probe executed the ACP fixture"

    proc = winpty.PtyProcess.spawn(
        [str(binary), "--workspace", str(workspace)], cwd=str(root), env=env,
        dimensions=(48, 140), backend=0,
    )
    screen = pyte.Screen(140, 48)
    stream = pyte.Stream(screen)
    chunks = queue.Queue()
    raw = []
    eof_seen = False
    failed = True

    def reader():
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

    def pump(timeout=0.05):
        nonlocal eof_seen
        try:
            chunk = chunks.get(timeout=timeout)
        except queue.Empty:
            return
        if chunk is None:
            eof_seen = True
        elif isinstance(chunk, Exception):
            raise AssertionError("ConPTY output reader failed") from chunk
        else:
            raw.append(chunk)
            stream.feed(chunk)

    def present(text):
        return any(text in row for row in screen.display)

    def records(event):
        if not ledger.exists():
            return []
        # A concurrent append may leave a partial last line; only inspect flushed records.
        lines = ledger.read_text(encoding="utf-8").splitlines(keepends=True)
        return [record for line in lines if line.endswith("\n")
                for record in [json.loads(line)] if record["event"] == event]

    def requests(method=None):
        return [record for record in records("request")
                if method is None or record["method"] == method]

    def sent():
        return records("sent")

    def wait_for(label, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        matching_since = None
        while time.monotonic() < deadline:
            pump()
            while not chunks.empty():
                pump(0)
            assert not eof_seen, f"Terminal exited while waiting for {label}"
            if predicate():
                if matching_since is None:
                    matching_since = time.monotonic()
                if time.monotonic() - matching_since >= 0.25:
                    return
            else:
                matching_since = None
        raise AssertionError(f"Timed out waiting for {label}\n" + "\n".join(screen.display))

    def hold(label, predicate):
        # Negative assertions must survive queued native events and subsequent repaint frames.
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            pump()
            assert not eof_seen and predicate(), f"{label}\n" + "\n".join(screen.display)

    def capture(name):
        (snapshots / f"{name}.txt").write_text("\n".join(screen.display), encoding="utf-8")

    def release(name):
        (runtime / name).touch()

    def resize(rows, columns):
        proc.setwinsize(rows, columns)
        screen.resize(rows, columns)

    def submit(text):
        proc.write("\x1b[200~" + text + "\x1b[201~")
        wait_for("draft " + text, lambda: present("> " + text))
        key(13, 13)

    def model_label(name):
        # Never mistake a prior transcript, echoed command or fixture output for the live label.
        return any(" · " + name + " · " in row for row in screen.display[-3:])

    def approval(name):
        return present("Permission required (" + name + ")")

    def approval_contains(text):
        # Rejoin visible modal cells, since terminal wrapping is not word-based.
        rows = screen.display
        start = next((i for i, row in enumerate(rows) if "Permission required (" in row), None)
        if start is None:
            return False
        column = rows[start].index("Permission required (")
        content = []
        for row in rows[start:]:
            if column and row[column - 1:column] == "╰":
                break
            content.append(row[column:].partition("│")[0].rstrip())
        return text in "".join(content)

    def full_details():
        return all(present(text) for text in (
            "Tool: fixture_patch", "Risk: R3 - ELEVATED", "Cwd: C:/fixture/workspace",
            "Parameters: path=fixtures/世界.txt", "content=hello 🙂", "second line stays separate",
            "Reason: Review café change", "Keep both lines visible.",
            "Impact: Only fixtures/世界.txt", "No tools executed.",
        ))

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        key = _conpty_native_key_sender(proc)
        wait_for("connected initial composer", lambda: present("shift+enter") and present("model-prior"), 30)
        assert len(requests("session/new")) == 1, "Test accidentally ran the standalone/demo path"
        submit("ready")
        wait_for("real fixture session", lambda: present("FIXTURE_READY") and model_label("model-prior"))
        # Omitted handshake counts must not become the legacy fixed counts.
        assert not present("26 tools") and not present("83 skills")
        capture("session_without_invented_capabilities")

        for attempt, argument in enumerate(("unavailable", "rejected", "fixture-alias"), 1):
            submit(("/MODEL " if attempt == 3 else "/model ") + argument)
            wait_for("pending model request", lambda: len(requests("session/command")) == attempt)
            hold("Model changed before confirmation", lambda: model_label("model-prior"))
            capture(f"model_pending_{attempt}")
            release(f"model-{attempt}")
            if attempt < 3:
                marker = "model unavailable" if attempt == 1 else "MODEL_REJECTED"
                wait_for("failed model retains label", lambda: present(marker) and model_label("model-prior"))
                assert not model_label(argument) and not model_label("model-rejected")
            else:
                wait_for("canonical model label", lambda: present("MODEL_APPLIED") and model_label("model-canonical"))
                assert not model_label("fixture-alias")
            capture(f"model_result_{attempt}")
        assert [r["params"]["args"] for r in requests("session/command")] == [
            "unavailable", "rejected", "fixture-alias",
        ]
        assert all(r["params"]["command"] == "/model" for r in requests("session/command"))

        # Dedicated side-by-side diff is the base view; help must not conceal approval.
        submit("/sbs")
        wait_for("side-by-side enabled", lambda: present("Diff viewer mode: side-by-side"))
        submit("approval-once")
        wait_for("complete Unicode approval", lambda: approval("approval-once") and full_details())
        key(68, ord("d"))  # Approval's d opens the dedicated diff even without file changes.
        wait_for("approval above side-by-side diff", lambda: approval("approval-once")
                 and present("Diff Viewer (Old)") and present("Diff Viewer (New)") and full_details())
        key(112)  # F1 overlays help beneath approval.
        wait_for("approval above help", lambda: approval("approval-once")
                 and present("HEX AGENT HELP") and full_details())
        capture("approval_over_diff_and_help")
        key(112)
        wait_for("help closed beneath approval", lambda: approval("approval-once") and not present("HEX AGENT HELP"))

        for rows, columns in ((10, 40), (2, 12)):
            resize(rows, columns)
            wait_for("fail-closed short approval", lambda: present("n Deny") and not present("Allow once"))
            for vk, char in ((89, ord("y")), (65, ord("a")), (13, 13)):
                key(vk, char)
            hold("Tiny-screen allow sent an RPC", lambda: not requests("session/approvalDecision"))
            capture(f"approval_gated_{columns}x{rows}")
        resize(48, 140)
        wait_for("review restored after resize", lambda: approval("approval-once") and full_details()
                 and present("Allow once") and present("Allow matching calls in this task"))

        for attempt, diagnostic in enumerate((
            "approval failure", "acknowledged", "not-this-approval", "approval-once", None,
        ), 1):
            key(89, ord("y"))
            wait_for("pending approval", lambda: present("Submitting decision; please wait.")
                     and approval("approval-once") and len(requests("session/approvalDecision")) == attempt)
            for vk, char in ((89, ord("y")), (65, ord("a")), (13, 13), (78, ord("n")), (27, 0)):
                key(vk, char)
            hold("Pending decision duplicated or prompt closed", lambda:
                 approval("approval-once") and len(requests("session/approvalDecision")) == attempt
                 and not present("server confirmed") and not present("FINISHED approval-once"))
            capture(f"approval_pending_{attempt}")
            release(f"decision-approval-once-{attempt}")
            if diagnostic is not None:
                wait_for("retryable unconfirmed approval", lambda: approval("approval-once")
                         and approval_contains("Decision not confirmed:") and approval_contains(diagnostic)
                         and approval_contains("Retry or cancel the turn.") and present("Retry: choose a decision below."))
                assert not present("server confirmed") and not present("FINISHED approval-once")
                capture(f"approval_retry_{attempt}")
            else:
                wait_for("only matching applied closes approval", lambda: not approval("approval-once")
                         and present("Diff Viewer (Old)") and present("Diff Viewer (New)"))
        decisions = requests("session/approvalDecision")
        assert [r["params"] for r in decisions] == [
            {"approvalId": "approval-once", "decision": "approve"},
        ] * 5
        capture("approval_applied_before_prompt_finishes")
        release("finish-approval-once")
        wait_for("approved prompt response", lambda: any(
            r.get("id") == requests("session/prompt")[-1]["id"] and "result" in r
            for r in sent()
        ))
        # The dedicated diff has no composer; native keys can still toggle it off.
        for char in "/sbs":
            key(ord(char.upper()), ord(char))
        key(13, 13)
        wait_for("approved turn finishes", lambda: present("FINISHED approval-once")
                 and present("Approved once:") and present("server confirmed") and present("> Message hex"))

        for name, vk, char, action in (
            ("approval-scope", 65, ord("a"), "Approved matching calls in this task:"),
            ("approval-deny", 78, ord("n"), "Denied:"),
        ):
            submit(name)
            wait_for(name, lambda: approval(name) and full_details())
            if name == "approval-deny":
                resize(10, 40)
                wait_for("deny remains available on short screen", lambda: present("n Deny") and not present("Allow once"))
            key(vk, char)
            count = 6 if name == "approval-scope" else 7
            wait_for("scope/deny RPC", lambda: len(requests("session/approvalDecision")) == count)
            if name == "approval-deny":
                resize(48, 140)
            wait_for("scope/deny pending", lambda: approval(name) and present("Submitting decision; please wait."))
            release(f"decision-{name}-1")
            wait_for("scope/deny confirmed", lambda: not approval(name) and present(action))
            capture(name + "_applied")
            release("finish-" + name)
            wait_for("scope/deny turn finishes", lambda: present("FINISHED " + name) and present("> Message hex"))
        assert [r["params"]["decision"] for r in requests("session/approvalDecision")][-2:] == [
            "approve_scope", "reject",
        ]

        submit("approval-cancel")
        wait_for("cancellable approval", lambda: approval("approval-cancel") and full_details())
        key(67, 3, modifiers=8)  # Native left-Ctrl+C cancels a busy turn, not the TUI.
        wait_for("first cancellation RPC", lambda: len(requests("session/cancel")) == 1)
        release("cancel-1")
        # The rejection notice is behind the modal; observe it by resizing the modal smaller.
        resize(48, 160)
        wait_for("cancellation rejected without losing approval", lambda: approval("approval-cancel")
                 and present("Cancellation failed:") and present("temporary cancellation failure"))
        key(13, 13)  # The failed cancellation must leave the approval retryable and busy.
        wait_for("approval retry after cancel failure", lambda: len(requests("session/approvalDecision")) == 8
                 and approval("approval-cancel") and present("Submitting decision; please wait."))
        key(67, 3, modifiers=8)
        wait_for("second cancellation RPC", lambda: len(requests("session/cancel")) == 2)
        release("cancel-2")
        wait_for("accepted cancellation response", lambda: any(
            r.get("result", {}).get("cancelled") is True for r in sent()
        ))
        hold("Accepted cancellation prematurely cleared approval", lambda: approval("approval-cancel")
             and not present("FINISHED approval-cancel"))
        capture("cancel_accepted_still_waiting_for_terminal")
        release("finish-approval-cancel")
        wait_for("authoritative cancellation clears approval", lambda: not approval("approval-cancel")
                 and present("FINISHED approval-cancel") and present("> Message hex"))
        # Release the stale decision only after terminal status invalidated the approval generation.
        release("decision-approval-cancel-1")
        hold("Late approval revived a cancelled prompt", lambda: not approval("approval-cancel"))
        capture("cancel_terminal")
        key(67, 3, modifiers=8)
        deadline = time.monotonic() + 10
        while not eof_seen and time.monotonic() < deadline:
            pump()
        assert eof_seen, "Idle Ctrl+C did not drain ConPTY to EOF"
        thread.join(timeout=2)
        assert not thread.is_alive() and not proc.isalive()
        assert proc.exitstatus == 0, f"TUI exited with {proc.exitstatus}"
        failed = False
    finally:
        try:
            if proc.isalive():
                proc.terminate(force=True)  # Only the test-owned TUI and its fake ACP child.
        finally:
            thread.join(timeout=5)
            while not chunks.empty():
                chunk = chunks.get_nowait()
                if isinstance(chunk, str):
                    raw.append(chunk)
                    stream.feed(chunk)
            (snapshots / "terminal_output.txt").write_text("".join(raw), encoding="utf-8")
            if failed:
                capture("failure")
                print(f"\nConPTY failure snapshots: {snapshots}\nRPC ledger: {ledger}\n"
                      + "\n".join(screen.display))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ConPTY resume/final-response regression")
def test_windows_conpty_resume_and_canonical_prompt_history(tmp_path):
    import codecs
    import sqlite3

    root = Path(__file__).resolve().parents[2]
    binary = Path(os.environ.get("HEX_TUI_TEST_BINARY", root / "target/debug/hex-tui.exe"))
    if not binary.is_absolute():
        binary = root / binary
    if not binary.is_file():
        pytest.skip(f"HEX TUI binary absent: {binary}; set HEX_TUI_TEST_BINARY to a built executable")
    winpty = pytest.importorskip("winpty", reason="ConPTY regression requires optional pywinpty")
    pyte = pytest.importorskip("pyte", reason="ConPTY regression requires optional pyte")
    pytest.importorskip("yaml", reason="ACP interpreter probe requires PyYAML in the test venv")

    runtime = tmp_path / "runtime"
    client = runtime / "harness_core" / "client"
    client.mkdir(parents=True)
    (client.parent / "__init__.py").write_text("", encoding="utf-8")
    (client / "__init__.py").write_text("", encoding="utf-8")
    workspace = tmp_path / "workspace"
    keyboard_cwd = tmp_path / "keyboard-canonical-workspace"
    mouse_cwd = tmp_path / "mouse-canonical-workspace"
    snapshots = tmp_path / "snapshots"
    for directory in (workspace, keyboard_cwd, mouse_cwd, snapshots):
        directory.mkdir()
    db_dir = workspace / ".hex" / "data"
    db_dir.mkdir(parents=True)
    task_ids = ("tsk_phase33_keyboard", "tsk_phase33_mouse")
    titles = ("Keyboard ledger task", "Mouse ledger task")
    sessions = ("phase33-keyboard-session", "phase33-mouse-session")
    prior_session = "phase33-prior-session"
    models = ("model-keyboard-canonical", "model-mouse-canonical")
    with sqlite3.connect(db_dir / "ledger.db") as connection:
        connection.executescript("""
            CREATE TABLE tasks(task_id TEXT PRIMARY KEY, goal TEXT, created_at TEXT);
            CREATE TABLE task_events(task_id TEXT, event_type TEXT, payload_json TEXT,
                                     sequence_no INTEGER, occurred_at TEXT);
            CREATE TABLE dialogue_turns(task_id TEXT, turn_index INTEGER, role TEXT, content TEXT);
        """)
        for index, (task_id, title) in enumerate(zip(task_ids, titles)):
            connection.execute("INSERT INTO tasks VALUES (?, ?, ?)",
                               (task_id, title, f"2026-11-{19 - index:02d}T10:00:00Z"))
            connection.execute("INSERT INTO task_events VALUES (?, ?, ?, ?, ?)",
                               (task_id, "STATE_SUCCEEDED", json.dumps({"result": "LOCAL_LEDGER_POISON"}),
                                1, "2026-11-19T11:00:00Z"))
            connection.execute("INSERT INTO dialogue_turns VALUES (?, ?, ?, ?)",
                               (task_id, 0, "assistant", "LOCAL_DIALOGUE_POISON"))
    repeated = "IDENTICAL_ANSWER_WITH_A_SHARED_PREFIX_LONGER_THAN_FORTY_CHARACTERS_世界🙂_END"
    corrected = "CORRECTED_SHORT_FINAL_世界🙂"
    canonical = "CANONICAL_BEGIN\n" + "".join(
        f"row{index:04d} 世界🙂界 café α canonical content stays exact\n" for index in range(2200)
    ) + "CANONICAL_END_世界🙂"
    historical = "HISTORICAL_FULL_BEGIN\n" + "世界🙂界 café\n" * 900 + "HISTORICAL_FULL_END"
    assert len(repeated[:40]) == 40 and len(canonical.encode("utf-8")) > 64 * 1024
    config = {
        "priorSession": prior_session, "repeated": repeated, "corrected": corrected,
        "canonical": canonical, "historical": historical,
        "tasks": {task_id: {"sessionId": session, "cwd": str(cwd), "model": model,
                           "title": title}
                  for task_id, session, cwd, model, title in
                  zip(task_ids, sessions, (keyboard_cwd, mouse_cwd), models, titles)},
    }
    (runtime / "fixture.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    fixture = r'''
import json
import sys
import threading
import time
from pathlib import Path


def main():
    assert sys.argv[1] == "acp"
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    root = Path(__file__).resolve().parents[2]
    config = json.loads((root / "fixture.json").read_text(encoding="utf-8"))
    lock = threading.Lock()
    attempts = {}
    content_attempts = {}
    turns = {}
    last_prompt = None
    for task_id, identity in config["tasks"].items():
        turns[identity["sessionId"]] = [
            {"task_id": task_id, "prompt": "OLDER_RUNTIME_PROMPT", "response": "OLDER_RUNTIME_ANSWER",
             "status": "completed"},
            {"task_id": task_id, "prompt": "PAGED_RUNTIME_PROMPT", "response": config["historical"],
             "status": "completed"},
            {"task_id": task_id, "prompt": "RECENT_RUNTIME_PROMPT", "response": "RECENT_RUNTIME_ANSWER",
             "status": "completed"},
        ]
    turns[config["priorSession"]] = []

    def record(event, message):
        with lock:
            with (root / "requests.jsonl").open("a", encoding="utf-8") as log:
                log.write(json.dumps(dict(event=event, **message), ensure_ascii=False) + "\n")

    def send(message):
        with lock:
            with (root / "requests.jsonl").open("a", encoding="utf-8") as log:
                log.write(json.dumps(dict(event="sent", **message), ensure_ascii=False) + "\n")
            print(json.dumps(dict(jsonrpc="2.0", **message), ensure_ascii=False), flush=True)

    def reply(request, result=None, error=None):
        send(dict(id=request["id"], **({"error": {"code": -32001, "message": error}}
                                     if error else {"result": result})))

    def gate(name):
        deadline = time.monotonic() + 60
        while not (root / name).exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("Fixture gate timed out: " + name)
            threading.Event().wait(0.02)

    def worker(function, *args):
        threading.Thread(target=function, args=args, daemon=True).start()

    def page(session, before=None):
        history = turns[session]
        end = len(history) if before is None else min(before, len(history))
        start = max(0, end - 2)
        entries = []
        for index in range(start, end):
            turn = history[index]
            entries.append(dict(turn, prompt=turn["prompt"][:96], response=turn["response"][:96],
                                turnIndex=index, promptChars=len(turn["prompt"]),
                                responseChars=len(turn["response"])))
        return {"history": entries, "historyCount": len(history),
                "nextBefore": start if start else None}

    def load(request, task_id, attempt):
        gate("load-" + task_id + "-" + str(attempt))
        if attempt == 1:
            reply(request, error="load unavailable")
            return
        identity = config["tasks"][task_id]
        if task_id.endswith("mouse") and attempt == 2:
            reply(request, dict(identity, cwd=str(root / "nonexistent-cwd"),
                                **page(identity["sessionId"])))
            return
        reply(request, dict(identity, **page(identity["sessionId"])))

    def finish(request, name, index, answer):
        gate("finish-" + name)
        session = request["params"]["sessionId"]
        turns[session][index]["response"] = answer
        turns[session][index]["status"] = "completed"
        if name == "long-final":
            reply(request, {"sessionId": session, "status": "completed", "turnIndex": index,
                            "responseChars": len(answer), "content": [{"type": "text", "text":
                            "PREVIEW_ONLY_NOT_CANONICAL\nFull response: /history-turn "
                            + str(index) + " response\nNext page is not the full result."}]})
        else:
            reply(request, {"sessionId": session, "status": "completed",
                            "content": [{"type": "text", "text": answer}]})

    def content(request, attempt):
        params = request["params"]
        session, index, offset = params["sessionId"], params["turnIndex"], params["offset"]
        source = turns[session][index][params["field"]]
        if index == 1 and attempt == 1:
            gate("history-content-failure")
            reply(request, error="page unavailable")
            return
        if source == config["canonical"] and offset == 0:
            gate("canonical-first-page")
        limit = min(params["limit"], 4096)
        text = source[offset:offset + limit]
        end = offset + len(text)
        reply(request, {"content": text, "totalChars": len(source),
                        "nextOffset": end if end < len(source) else None})

    for line in sys.stdin:
        request = json.loads(line)
        record("request", request)
        method, params = request["method"], request.get("params") or {}
        if method == "initialize":
            reply(request, {"protocolVersion": "2024-11-05",
                            "serverInfo": {"name": "phase33-fixture", "version": "1"},
                            "capabilities": {"session": {"streaming": True, "cancel": True,
                                                         "history": True},
                                             "tools": {"listChanged": False}}})
        elif method == "session/new":
            reply(request, {"sessionId": config["priorSession"], "cwd": params["cwd"],
                            "model": "model-prior", "provider": "provider-prior"})
        elif method == "session/load":
            assert set(params) == {"taskId", "paged"} and params["paged"] is True
            task_id = params["taskId"]
            assert task_id in config["tasks"]
            attempts[task_id] = attempts.get(task_id, 0) + 1
            worker(load, request, task_id, attempts[task_id])
        elif method == "session/prompt":
            session, name = params["sessionId"], params["prompt"]
            assert session in turns and params["pagedResponse"] is True
            answer = {"prior-ready": "PRIOR_SESSION_READY", "mouse-ready": "MOUSE_SESSION_READY",
                      "repeat-one": config["repeated"], "repeat-two": config["repeated"],
                      "corrected-final": config["corrected"], "long-final": config["canonical"]}[name]
            index = len(turns[session])
            turns[session].append({"prompt": name, "response": "", "status": "running"})
            if name in {"corrected-final", "long-final"}:
                if last_prompt is not None:
                    send({"id": last_prompt, "result": {"sessionId": session, "status": "completed",
                          "content": [{"type": "text", "text": "STALE_REQUEST_POISON"}]}})
                send({"method": "session/update", "params": {"sessionId": "unrelated-session",
                      "updateType": "message", "delta": "WRONG_SESSION_POISON"}})
                send({"method": "session/update", "params": {"sessionId": session,
                      "updateType": "message", "delta": "OBSOLETE_STREAM_DRAFT_"}})
                send({"method": "session/update", "params": {"sessionId": session,
                      "updateType": "tool", "tool": {"eventType": "start", "toolId": "fixture_read",
                      "callId": name, "payload": "SIMULATED_ONLY_NO_TOOL_EXECUTION"}}})
                send({"method": "session/update", "params": {"sessionId": session,
                      "updateType": "status", "status": "INTERLEAVED_STATUS_UPDATE"}})
                send({"method": "session/update", "params": {"sessionId": session,
                      "updateType": "tool", "tool": {"eventType": "complete", "toolId": "fixture_read",
                      "callId": name, "payload": "SIMULATED_TOOL_COMPLETE", "durationMs": 1}}})
                send({"method": "session/update", "params": {"sessionId": session,
                      "updateType": "message", "delta": "TAIL_AFTER_TOOL_世界🙂"}})
            worker(finish, request, name, index, answer)
            last_prompt = request["id"]
        elif method == "session/history":
            assert params["sessionId"] in turns
            reply(request, page(params["sessionId"], params.get("before")))
        elif method == "session/historyContent":
            assert params["sessionId"] in turns and params["field"] in {"prompt", "response"}
            assert 0 < params["limit"] <= 4096
            identity = (params["sessionId"], params["turnIndex"], params["field"], params["offset"])
            content_attempts[identity] = content_attempts.get(identity, 0) + 1
            worker(content, request, content_attempts[identity])
        elif method == "ping":
            reply(request, {"pong": True})
        elif request.get("id") is not None:
            reply(request, error="Unexpected fixture method: " + method)


if __name__ == "__main__":
    main()
'''
    (client / "cli.py").write_text(textwrap.dedent(fixture), encoding="utf-8")
    env = dict(os.environ, HEX_REPO_ROOT=str(runtime), HEX_PYTHON_BIN=sys.executable,
               PYTHONUTF8="1", PYTHONPATH=str(runtime))
    probe = subprocess.run(
        [sys.executable, "-c", "import harness_core.client.cli, yaml"],
        cwd=runtime, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
    )
    assert probe.returncode == 0, probe.stderr.decode("utf-8", errors="replace")
    ledger = runtime / "requests.jsonl"
    assert not ledger.exists(), "Import probe executed the ACP fixture"

    proc = winpty.PtyProcess.spawn(
        [str(binary), "--workspace", str(workspace)], cwd=str(root), env=env,
        dimensions=(48, 241), backend=0,
    )
    screen = pyte.Screen(241, 48)
    stream = pyte.Stream(screen)
    chunks = queue.Queue()
    raw = []
    eof_seen = False
    failed = True

    def reader():
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

    def pump(timeout=0.05):
        nonlocal eof_seen
        try:
            chunk = chunks.get(timeout=timeout)
        except queue.Empty:
            return
        if chunk is None:
            eof_seen = True
        elif isinstance(chunk, Exception):
            raise AssertionError("ConPTY output reader failed") from chunk
        else:
            raw.append(chunk)
            stream.feed(chunk)

    def present(text):
        return any(text in row for row in screen.display)

    def draft_is(text):
        rows = screen.display
        index = next((i for i in range(len(rows) - 1, -1, -1) if "> " in rows[i]), None)
        return index is not None and rows[index].split("> ", 1)[1].partition("│")[0].rstrip() == text

    def records(event):
        if not ledger.exists():
            return []
        lines = ledger.read_text(encoding="utf-8").splitlines(keepends=True)
        return [item for line in lines if line.endswith("\n")
                for item in [json.loads(line)] if item["event"] == event]

    def requests(method):
        return [item for item in records("request") if item["method"] == method]

    def result_for(request):
        return next((item for item in reversed(records("sent"))
                     if item.get("id") == request["id"]), None)

    def wait_for(label, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        matching_since = None
        while time.monotonic() < deadline:
            pump()
            while not chunks.empty():
                pump(0)
            assert not eof_seen, f"Terminal exited while waiting for {label}"
            if predicate():
                if matching_since is None:
                    matching_since = time.monotonic()
                if time.monotonic() - matching_since >= 0.25:
                    return
            else:
                matching_since = None
        raise AssertionError(f"Timed out waiting for {label}\n" + "\n".join(screen.display))

    def hold(label, predicate):
        # WHY: Pending-state assertions must survive duplicate native events and repaint frames.
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            pump()
            while not chunks.empty():
                pump(0)
            assert not eof_seen and predicate(), f"{label}\n" + "\n".join(screen.display)

    def capture(name):
        (snapshots / f"{name}.txt").write_text("\n".join(screen.display), encoding="utf-8")

    def release(name):
        (runtime / name).touch()

    def resize(rows, columns):
        proc.setwinsize(rows, columns)
        screen.resize(rows, columns)

    def palette(command, selected=None):
        key(80, 16, modifiers=8)
        proc.write("\x1b[200~" + command + "\x1b[201~")
        wait_for("palette " + command, lambda: present("> " + command)
                 and (selected is None or present(selected)))
        key(13, 13)

    def switch_to_start(draft):
        proc.write("\x1b[200~" + draft + "\x1b[201~")
        wait_for("preserved session draft", lambda: draft_is(draft))
        palette("switch", "Switch session")
        wait_for("Start with prior draft", lambda: present(titles[0]) and present(titles[1])
                 and draft_is(draft) and not present("Runtime context restored."))

    def click(text):
        row = next(i for i, value in enumerate(screen.display) if text in value)
        column = screen.display[row].index(text)
        proc.write(f"\x1b[<0;{column + 1};{row + 1}M\x1b[<0;{column + 1};{row + 1}m")

    def model_label(model):
        return any(" · " + model in row for row in screen.display if "agent · " in row)

    def submit(text):
        proc.write("\x1b[200~" + text + "\x1b[201~")
        wait_for("draft " + text, lambda: draft_is(text))
        key(13, 13)

    def archive_path(cwd, session):
        return cwd / ".hex" / "data" / "tui_transcripts" / session

    def fingerprint(directory):
        return {str(path.relative_to(directory)): path.read_bytes()
                for path in directory.rglob("*") if path.is_file()} if directory.exists() else {}

    def archived(directory, role=None):
        items = []
        for path in sorted(directory.glob("*.json"), key=lambda value: int(value.stem)):
            metadata = json.loads(path.read_text(encoding="utf-8"))
            if metadata.get("kind") != "Message" or (role and metadata.get("role") != role):
                continue
            decoder = codecs.getincrementaldecoder("utf-8")()
            text = []
            with path.with_suffix(".txt").open("rb") as source:
                while chunk := source.read(4096):
                    text.append(decoder.decode(chunk))
            text.append(decoder.decode(b"", final=True))
            items.append((int(path.stem), "".join(text)))
        return items

    def completed(name, marker):
        submit(name)
        wait_for("prompt request " + name, lambda: bool(requests("session/prompt"))
                 and requests("session/prompt")[-1]["params"]["prompt"] == name)
        release("finish-" + name)
        wait_for("completed " + name, lambda: present(marker) and draft_is("Message hex"))

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        key = _conpty_native_key_sender(proc)
        wait_for("connected ledger Start", lambda: present(titles[0]) and present(titles[1])
                 and model_label("model-prior") and present("shift+enter"), 30)
        assert len(requests("session/new")) == 1, "Test accidentally ran the standalone/demo path"
        completed("prior-ready", "PRIOR_SESSION_READY")
        switch_to_start("keyboard-retained-draft_世界🙂")
        prior_archive = archive_path(workspace, prior_session)
        before = fingerprint(prior_archive)
        for attempt in (1, 2):
            key(13, 13)
            wait_for("keyboard session/load", lambda: len(requests("session/load")) == attempt
                     and present("Loading session context"))
            assert requests("session/load")[-1]["params"] == {"taskId": task_ids[0], "paged": True}
            key(13, 13)
            click(titles[0])
            hold("Keyboard resume adopted before reply or duplicated", lambda:
                 len(requests("session/load")) == attempt and present(titles[0])
                 and draft_is("keyboard-retained-draft_世界🙂") and model_label("model-prior")
                 and fingerprint(prior_archive) == before
                 and not archive_path(keyboard_cwd, sessions[0]).exists()
                 and not present("RECENT_RUNTIME_ANSWER") and not present("LOCAL_LEDGER_POISON"))
            capture(f"keyboard_pending_{attempt}")
            release(f"load-{task_ids[0]}-{attempt}")
            if attempt == 1:
                wait_for("actionable keyboard failure", lambda: present(titles[0]) and present("Resume failed:")
                         and present("load unavailable") and present("retry")
                         and draft_is("keyboard-retained-draft_世界🙂") and model_label("model-prior"))
                assert fingerprint(prior_archive) == before
                palette("session-history")
                wait_for("prior session retained after keyboard failure", lambda:
                         len(requests("session/history")) == 1 and result_for(requests("session/history")[-1]))
                assert requests("session/history")[-1]["params"] == {"sessionId": prior_session}
                assert draft_is("keyboard-retained-draft_世界🙂")
                capture("keyboard_failed_retryable")
            else:
                wait_for("canonical keyboard adoption", lambda: present("Runtime context restored.")
                         and present(sessions[0]) and present("RECENT_RUNTIME_ANSWER")
                         and present("Full response: /history-turn 1 response")
                         and model_label(models[0]) and draft_is("Message hex"))
                assert archive_path(keyboard_cwd, sessions[0]).is_dir()
                assert not archive_path(workspace, task_ids[0]).exists()
                assert not present("LOCAL_LEDGER_POISON") and not present("LOCAL_DIALOGUE_POISON")
                assert not present("PRIOR_SESSION_READY") and not present("keyboard-retained-draft")
                capture("keyboard_adopted_paged_runtime_history")

        completed("repeat-one", repeated)
        completed("repeat-two", repeated)
        assert sum(repeated in row for row in screen.display) == 2, "Identical completed turns were suppressed"
        assert [item["params"]["sessionId"] for item in requests("session/prompt")] == [
            prior_session, sessions[0], sessions[0],
        ], "Resumed prompts used a task ID instead of the runtime session ID"
        capture("two_identical_completed_answers")

        submit("corrected-final")
        wait_for("stream continues across simulated tool updates", lambda:
                 present("OBSOLETE_STREAM_DRAFT_TAIL_AFTER_TOOL_世界🙂") and present("fixture_read"))
        palette("session-history")
        wait_for("interleaved system history notice", lambda: present("Stored session history")
                 and result_for(requests("session/history")[-1]))
        hold("Stream was prematurely finalized or cross-correlated", lambda:
             not present(corrected) and not present("STALE_REQUEST_POISON")
             and not present("WRONG_SESSION_POISON")
             and result_for(requests("session/prompt")[-1]) is None)
        capture("stream_tool_system_interleaved")
        release("finish-corrected-final")
        wait_for("short canonical correction replaces draft", lambda:
                 present(corrected) and not present("OBSOLETE_STREAM_DRAFT") and draft_is("Message hex"))
        assert sum(corrected in row for row in screen.display) == 1
        capture("corrected_short_final")

        palette("history-turn 1 response")
        wait_for("history content request failure gate", lambda: len(requests("session/historyContent")) == 1)
        release("history-content-failure")
        wait_for("retryable history page failure", lambda: present("Could not read stored turn:")
                 and present("page unavailable"))
        palette("history-turn 1 response")
        wait_for("successful history page retry", lambda: len(requests("session/historyContent")) == 2
                 and present("Next page: /history-turn 1 response 4096"))
        assert requests("session/historyContent")[0]["params"] == requests("session/historyContent")[1]["params"] == {
            "sessionId": sessions[0], "turnIndex": 1, "field": "response", "offset": 0, "limit": 4096,
        }
        history_result = result_for(requests("session/historyContent")[-1])["result"]
        assert history_result == {"content": historical[:4096], "totalChars": len(historical), "nextOffset": 4096}
        capture("history_page_failure_and_retry")

        submit("long-final")
        wait_for("long turn streaming draft", lambda: present("OBSOLETE_STREAM_DRAFT_TAIL_AFTER_TOOL_世界🙂"))
        long_request = requests("session/prompt")[-1]
        release("finish-long-final")
        wait_for("structured long final awaits authoritative page", lambda: result_for(long_request)
                 and len(requests("session/historyContent")) == 3)
        long_index = result_for(long_request)["result"]["turnIndex"]
        assert result_for(long_request)["result"]["responseChars"] == len(canonical)
        assert len(canonical) != len(canonical.encode("utf-8"))
        resize(24, 100)
        wait_for("resized pending canonical response", lambda: present("Message hex")
                 and present("OBSOLETE_STREAM_DRAFT") and not present("Expand terminal"))
        hold("Preview was treated as full answer or completion", lambda:
             not present("PREVIEW_ONLY_NOT_CANONICAL") and not present("CANONICAL_END")
             and len(requests("session/historyContent")) == 3)
        capture("long_final_pending_resized")
        release("canonical-first-page")
        wait_for("full Unicode canonical response delivered", lambda: present("CANONICAL_END_世界🙂")
                 and draft_is("Message hex"), 30)
        assert not present("PREVIEW_ONLY_NOT_CANONICAL") and not present("OBSOLETE_STREAM_DRAFT")
        resize(48, 241)
        wait_for("canonical response after resize", lambda: present("CANONICAL_END_世界🙂")
                 and draft_is("Message hex"))
        content_requests = [item for item in requests("session/historyContent")
                            if item["params"]["turnIndex"] == long_index]
        assert len(content_requests) > 10
        reconstructed = []
        offset = 0
        for item in content_requests:
            assert item["params"] == {"sessionId": sessions[0], "turnIndex": long_index,
                                      "field": "response", "offset": offset, "limit": 4096}
            page = result_for(item)["result"]
            assert 0 < len(page["content"]) <= 4096 and page["totalChars"] == len(canonical)
            reconstructed.append(page["content"])
            offset += len(page["content"])
            assert page["nextOffset"] == (offset if offset < len(canonical) else None)
        assert "".join(reconstructed) == canonical and offset == len(canonical)
        keyboard_archive = archive_path(keyboard_cwd, sessions[0])
        wait_for("native canonical archive fully appended", lambda:
                 any(text == canonical for _, text in archived(keyboard_archive, "assistant")))
        canonical_ids = [record_id for record_id, text in archived(keyboard_archive, "assistant")
                         if text == canonical]
        assert len(canonical_ids) == 1, "Full canonical response was duplicated or archived as preview prose"
        canonical_id = canonical_ids[0]
        palette("history")
        wait_for("native archive navigation", lambda: present("Native archive (")
                 and present(f"/transcript {canonical_id}"))
        palette(f"transcript {canonical_id} 0")
        wait_for("bounded native first page", lambda: present("CANONICAL_BEGIN")
                 and present(f"Next page: /transcript {canonical_id}"))
        assert not present("PREVIEW_ONLY_NOT_CANONICAL")
        capture("canonical_native_archive_page")
        palette("session-history")
        wait_for("bounded recent runtime history", lambda: result_for(requests("session/history")[-1])
                 and present("Stored session history") and present("Older turns: /session-history"))
        page = result_for(requests("session/history")[-1])["result"]
        before_index = page["nextBefore"]
        assert before_index is not None and len(page["history"]) <= 2
        palette(f"session-history {before_index}")
        wait_for("older runtime history page", lambda: requests("session/history")[-1]["params"].get("before")
                 == before_index and result_for(requests("session/history")[-1]))
        for item in requests("session/history"):
            page = result_for(item)["result"]
            assert len(page["history"]) <= 2
            assert all(len(turn["prompt"]) <= 96 and len(turn["response"]) <= 96 for turn in page["history"])
        capture("bounded_runtime_history_navigation")

        switch_to_start("mouse-retained-draft_世界🙂")
        before = fingerprint(keyboard_archive)
        for attempt in (1, 2, 3):
            click(titles[1])
            expected_count = 2 + attempt
            wait_for("mouse session/load", lambda: len(requests("session/load")) == expected_count
                     and present("Loading session context"))
            assert requests("session/load")[-1]["params"] == {"taskId": task_ids[1], "paged": True}
            click(titles[1])
            key(13, 13)
            hold("Mouse resume adopted before reply or duplicated", lambda:
                 len(requests("session/load")) == expected_count and present(titles[1])
                 and draft_is("mouse-retained-draft_世界🙂") and model_label(models[0])
                 and fingerprint(keyboard_archive) == before
                 and not archive_path(mouse_cwd, sessions[1]).exists())
            capture(f"mouse_pending_{attempt}")
            release(f"load-{task_ids[1]}-{attempt}")
            if attempt < 3:
                wait_for("actionable mouse failure", lambda: present(titles[1]) and present("Resume failed:")
                         and present("retry") and draft_is("mouse-retained-draft_世界🙂")
                         and model_label(models[0]))
                assert not archive_path(mouse_cwd, sessions[1]).exists()
                assert fingerprint(keyboard_archive) == before
                palette("session-history")
                wait_for("keyboard identity retained after mouse failure", lambda:
                         result_for(requests("session/history")[-1])
                         and requests("session/history")[-1]["params"] == {"sessionId": sessions[0]})
                assert draft_is("mouse-retained-draft_世界🙂")
                capture(f"mouse_failed_retryable_{attempt}")
            else:
                wait_for("canonical mouse adoption", lambda: present(sessions[1])
                         and present("Runtime context restored.") and model_label(models[1])
                         and present("RECENT_RUNTIME_ANSWER") and draft_is("Message hex"))
                assert archive_path(mouse_cwd, sessions[1]).is_dir()
                assert not archive_path(keyboard_cwd, task_ids[1]).exists()
                assert not present("mouse-retained-draft") and not present("LOCAL_LEDGER_POISON")
                capture("mouse_adopted_canonical_identity")
        completed("mouse-ready", "MOUSE_SESSION_READY")
        assert requests("session/prompt")[-1]["params"]["sessionId"] == sessions[1]
        assert len(requests("session/new")) == 1
        assert [item["params"] for item in requests("session/load")] == [
            {"taskId": task_ids[0], "paged": True}, {"taskId": task_ids[0], "paged": True},
            *[{"taskId": task_ids[1], "paged": True}] * 3,
        ]
        keyboard_answers = [text for _, text in archived(keyboard_archive, "assistant")]
        assert keyboard_answers.count(repeated) == 2, "Repeated turn was lost in the native archive"
        assert keyboard_answers.count(corrected) == 1, "Stream correction was appended, lost or duplicated"
        assert keyboard_answers.count(canonical) == 1, "Unicode canonical final did not reconstruct exactly"
        assert all("PREVIEW_ONLY_NOT_CANONICAL" not in text for text in keyboard_answers)
        assert any(text == "PRIOR_SESSION_READY" for _, text in archived(prior_archive, "assistant"))
        assert not any(item["method"] in {"tools/call", "session/command", "session/cancel"}
                       for item in records("request"))
        capture("final_mouse_prompt_actual_session")
        key(67, 3, modifiers=8)
        deadline = time.monotonic() + 10
        while not eof_seen and time.monotonic() < deadline:
            pump()
        assert eof_seen, "Idle Ctrl+C did not drain ConPTY to EOF"
        thread.join(timeout=2)
        assert not thread.is_alive() and not proc.isalive()
        assert proc.exitstatus == 0, f"TUI exited with {proc.exitstatus}"
        mouse_answers = [text for _, text in archived(archive_path(mouse_cwd, sessions[1]), "assistant")]
        assert mouse_answers.count("MOUSE_SESSION_READY") == 1
        failed = False
    finally:
        try:
            if proc.isalive():
                proc.terminate(force=True)
        finally:
            thread.join(timeout=5)
            while not chunks.empty():
                chunk = chunks.get_nowait()
                if isinstance(chunk, str):
                    raw.append(chunk)
                    stream.feed(chunk)
            (snapshots / "terminal_output.txt").write_text("".join(raw), encoding="utf-8")
            if failed:
                capture("failure")
                print(f"\nConPTY failure snapshots: {snapshots}\nRPC ledger: {ledger}\n"
                      + "\n".join(screen.display))
