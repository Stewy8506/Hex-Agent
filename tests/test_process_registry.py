"""Unit and integration tests for PTY ProcessRegistry and CircularOutputBuffer."""

import os
import sys
import time
import unittest

from harness_core.process_registry import (
    CircularOutputBuffer,
    ProcessRegistry,
    ProcessSession,
    ProcessStatus,
    get_process_registry,
    reset_process_registry,
    strip_ansi,
)


class TestCircularOutputBuffer(unittest.TestCase):
    """Test suite for CircularOutputBuffer ring buffer operations."""

    def test_buffer_rollover_and_cumulative_chars(self) -> None:
        buf = CircularOutputBuffer(max_chars=100)
        buf.append("a" * 60)
        self.assertEqual(buf.total_chars, 60)

        buf.append("b" * 60)
        self.assertEqual(buf.total_chars, 120)

        # Buffer should be trimmed to the last 100 characters (20 'a's and 60 'b's? Wait: 40 'a's and 60 'b's)
        slice_text, total, has_more = buf.read(offset=0, limit=200)
        self.assertEqual(len(slice_text), 100)
        self.assertEqual(total, 120)
        self.assertFalse(has_more)
        self.assertEqual(slice_text, "a" * 40 + "b" * 60)

    def test_buffer_read_slices(self) -> None:
        buf = CircularOutputBuffer(max_chars=50)
        buf.append("0123456789")

        # Read first 5 chars
        slice_text, total, has_more = buf.read(offset=0, limit=5)
        self.assertEqual(slice_text, "01234")
        self.assertEqual(total, 10)
        self.assertTrue(has_more)

        # Read next 5 chars
        slice_text, total, has_more = buf.read(offset=5, limit=5)
        self.assertEqual(slice_text, "56789")
        self.assertEqual(total, 10)
        self.assertFalse(has_more)

        # Read beyond end
        slice_text, total, has_more = buf.read(offset=10, limit=5)
        self.assertEqual(slice_text, "")
        self.assertFalse(has_more)

    def test_get_tail_and_strip_ansi(self) -> None:
        buf = CircularOutputBuffer(max_chars=100)
        text_with_ansi = "\x1b[31mError:\x1b[0m disk full"
        buf.append(text_with_ansi)

        raw_tail = buf.get_tail(50)
        self.assertIn("\x1b[31m", raw_tail)

        clean_tail = buf.get_tail(50, strip_ansi=True)
        self.assertEqual(clean_tail, "Error: disk full")

    def test_clear_buffer(self) -> None:
        buf = CircularOutputBuffer(max_chars=50)
        buf.append("hello world")
        self.assertEqual(buf.total_chars, 11)

        buf.clear()
        self.assertEqual(buf.total_chars, 0)
        slice_text, total, _ = buf.read(0, 100)
        self.assertEqual(slice_text, "")
        self.assertEqual(total, 0)


class TestProcessRegistry(unittest.TestCase):
    """Test suite for ProcessRegistry process supervision and lifecycle."""

    def setUp(self) -> None:
        reset_process_registry()
        self.registry = ProcessRegistry()

    def tearDown(self) -> None:
        self.registry.cleanup_all()
        self.registry.close()
        reset_process_registry()

    def test_fast_exiting_command_pty(self) -> None:
        """Verify fast-exiting command terminates with status completed and exit code 0."""
        session = self.registry.spawn('echo "hello from pty"', use_pty=True)
        self.assertTrue(session.id.startswith("proc_"))

        result = self.registry.wait(session.id, timeout=5.0)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["exit_code"], 0)

        output_res = self.registry.read_output(session.id)
        self.assertIn("hello from pty", output_res["output"])
        self.assertEqual(output_res["status"], "completed")

    def test_fast_exiting_command_pipe(self) -> None:
        """Verify fast-exiting command runs cleanly in pipe fallback mode."""
        session = self.registry.spawn('echo "hello from pipe"', use_pty=False)
        self.assertFalse(session.pty_enabled)

        result = self.registry.wait(session.id, timeout=5.0)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["exit_code"], 0)

        output_res = self.registry.read_output(session.id)
        self.assertIn("hello from pipe", output_res["output"])

    def test_failing_command(self) -> None:
        """Verify failing command updates status to failed and preserves nonzero exit code."""
        session = self.registry.spawn("sh -c 'exit 42'", use_pty=True)
        result = self.registry.wait(session.id, timeout=5.0)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exit_code"], 42)

    def test_streaming_background_command(self) -> None:
        """Verify background process streams output incrementally into ring buffer."""
        cmd = f'{sys.executable} -u -c "import time; print(\'chunk_one\'); time.sleep(0.3); print(\'chunk_two\')"'
        session = self.registry.spawn(cmd, use_pty=True)

        # Wait briefly for first chunk
        time.sleep(0.2)
        out1 = self.registry.read_output(session.id)
        self.assertIn("chunk_one", out1["output"])

        # Wait for process completion
        result = self.registry.wait(session.id, timeout=5.0)
        self.assertEqual(result["status"], "completed")

        out2 = self.registry.read_output(session.id)
        self.assertIn("chunk_two", out2["output"])

    def test_kill_process_graceful_and_force(self) -> None:
        """Verify killing running process sets status to killed and terminates child tree."""
        # Spawn long-running process
        cmd = f'{sys.executable} -c "import time; time.sleep(60)"'
        session = self.registry.spawn(cmd, use_pty=True)
        self.assertEqual(session.status, ProcessStatus.RUNNING)

        kill_res = self.registry.kill(session.id, force=False)
        self.assertEqual(kill_res["status"], "killed")

        poll_res = self.registry.poll(session.id)
        self.assertEqual(poll_res["status"], "killed")

        # Force kill on already killed should be safe and idempotent
        kill_force = self.registry.kill(session.id, force=True)
        self.assertEqual(kill_force["status"], "killed")

    def test_list_processes_and_filtering(self) -> None:
        """Verify list_processes lists tracked sessions and applies status filter."""
        s1 = self.registry.spawn('echo "p1"', use_pty=False)
        s2 = self.registry.spawn(f'{sys.executable} -c "import time; time.sleep(30)"', use_pty=False)

        self.registry.wait(s1.id, timeout=3.0)

        all_procs = self.registry.list_processes()
        self.assertEqual(len(all_procs), 2)

        completed = self.registry.list_processes(status_filter="completed")
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["id"], s1.id)

        running = self.registry.list_processes(status_filter="running")
        self.assertEqual(len(running), 1)
        self.assertEqual(running[0]["id"], s2.id)

        self.registry.kill(s2.id, force=True)

    def test_cleanup_all(self) -> None:
        """Verify cleanup_all terminates all running processes safely."""
        s1 = self.registry.spawn(f'{sys.executable} -c "import time; time.sleep(30)"')
        s2 = self.registry.spawn(f'{sys.executable} -c "import time; time.sleep(30)"')

        self.registry.cleanup_all()

        p1 = self.registry.poll(s1.id)
        p2 = self.registry.poll(s2.id)
        self.assertEqual(p1["status"], "killed")
        self.assertEqual(p2["status"], "killed")

    def test_singleton_registry(self) -> None:
        """Verify get_process_registry returns singleton instance and reset works."""
        r1 = get_process_registry()
        r2 = get_process_registry()
        self.assertIs(r1, r2)

        reset_process_registry()
        r3 = get_process_registry()
        self.assertIsNot(r1, r3)
        reset_process_registry()

    def test_invalid_spawn_parameters(self) -> None:
        """Verify invalid commands and nonexistent paths raise appropriate errors."""
        with self.assertRaises(ValueError):
            self.registry.spawn("")

        with self.assertRaises(NotADirectoryError):
            self.registry.spawn('echo "hi"', cwd="/path/does/not/exist/hex_pty_test")

        with self.assertRaises(KeyError):
            self.registry.get("proc_nonexistent")


if __name__ == "__main__":
    unittest.main()
