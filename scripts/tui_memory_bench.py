"""Sample current RSS of an owned terminal process tree; no model requests are sent."""

from __future__ import annotations

import argparse
import errno
import json
import os
import platform
import selectors
import signal
import struct
import subprocess
import sys
import time
from pathlib import Path


def parse_processes(text: str) -> dict[int, dict]:
    processes = {}
    for line in text.splitlines():
        fields = line.strip().split(None, 3)
        if len(fields) < 3:
            continue
        pid, parent, rss = map(int, fields[:3])
        processes[pid] = {
            "pid": pid,
            "parent_pid": parent,
            "rss_bytes": rss * 1024,
            "command": fields[3] if len(fields) > 3 else "",
        }
    return processes


def process_tree(processes: dict[int, dict], root_pid: int) -> list[dict]:
    included = {root_pid}
    while True:
        descendants = {pid for pid, item in processes.items() if item["parent_pid"] in included}
        expanded = included | descendants
        if expanded == included:
            break
        included = expanded
    return [processes[pid] for pid in sorted(included) if pid in processes]


def summarize(samples: list[dict]) -> dict:
    peaks = {}
    for sample in samples:
        for proc in sample["processes"]:
            key = str(proc["pid"])
            peaks[key] = max(peaks.get(key, 0), proc["rss_bytes"])
    return {
        "simultaneous_tree_peak_rss_bytes": max((s["tree_rss_bytes"] for s in samples), default=0),
        "last_tree_rss_bytes": samples[-1]["tree_rss_bytes"] if samples else 0,
        "sampled_process_peaks_rss_bytes": peaks,
    }


def run(command: list[str], seconds: float, interval: float, width: int, height: int) -> dict:
    import fcntl
    import pty
    import termios

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", height, width, 0, 0))
    env = os.environ.copy()
    env.setdefault("TERM", "xterm-256color")
    env.setdefault("HEX_PYTHON_BIN", sys.executable)
    env.setdefault("HEX_REPO_ROOT", str(Path(__file__).resolve().parents[1]))
    child = subprocess.Popen(
        command, stdin=slave, stdout=slave, stderr=slave, env=env,
        start_new_session=True,
        preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0),
    )
    os.close(slave)
    os.set_blocking(master, False)
    poller = selectors.DefaultSelector()
    poller.register(master, selectors.EVENT_READ)
    samples = []
    terminal_bytes = 0
    start = time.monotonic()
    next_sample = start
    try:
        while child.poll() is None and time.monotonic() - start < seconds:
            now = time.monotonic()
            if now >= next_sample:
                ps = subprocess.run(
                    ["ps", "-axo", "pid=,ppid=,rss=,command="],
                    capture_output=True, text=True, check=True,
                )
                tree = process_tree(parse_processes(ps.stdout), child.pid)
                samples.append({
                    "elapsed_seconds": time.monotonic() - start,
                    "tree_rss_bytes": sum(p["rss_bytes"] for p in tree),
                    "processes": tree,
                })
                next_sample = now + interval
            for key, _ in poller.select(max(0, min(next_sample - time.monotonic(), 0.05))):
                try:
                    data = os.read(key.fd, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    data = b""
                terminal_bytes += len(data)
                if not data:
                    poller.unregister(master)
        if child.poll() is None:
            os.write(master, b"\x03")
            deadline = time.monotonic() + 5
            while child.poll() is None and time.monotonic() < deadline:
                for key, _ in poller.select(0.05):
                    try:
                        terminal_bytes += len(os.read(key.fd, 65536))
                    except OSError as exc:
                        if exc.errno != errno.EIO:
                            raise
                        poller.unregister(master)
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            child.wait(timeout=5)
        poller.close()
        os.close(master)
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True,
    )
    return {
        "schema_version": 1,
        "source_revision": revision.stdout.strip() if revision.returncode == 0 else None,
        "working_directory": str(Path.cwd()),
        "measurement": "sampled current RSS; sums can double-count shared pages, not unique physical memory",
        "platform": platform.platform(),
        "command": command,
        "terminal": {"width": width, "height": height, "output_bytes": terminal_bytes},
        "duration_seconds": time.monotonic() - start,
        "exit_code": child.returncode,
        "samples": samples,
        **summarize(samples),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--interval", type=float, default=0.1)
    parser.add_argument("--width", type=int, default=120)
    parser.add_argument("--height", type=int, default=40)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or args.seconds <= 0 or args.interval <= 0 or args.width <= 0 or args.height <= 0:
        parser.error("provide a command and positive duration, interval, and terminal dimensions")
    print(json.dumps(run(command, args.seconds, args.interval, args.width, args.height), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
