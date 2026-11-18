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
