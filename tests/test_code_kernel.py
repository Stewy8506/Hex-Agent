"""Comprehensive unit and integration test suite for persistent REPL CodeKernel and KernelRegistry."""

import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest

from harness_core.code_kernel import (
    CodeKernel,
    KernelRegistry,
    _BoundedBuffer,
    _RUNNER_CAPTURE_BYTES,
    get_kernel_registry,
    reset_kernel_registry,
    stale_modules_warning,
)


class TestBoundedBuffer(unittest.TestCase):
    """Test memory-capped byte buffer behavior."""

    def test_bounded_buffer_cap_and_drain(self):
        buf = _BoundedBuffer(cap=10)
        buf.append(b"12345")
        buf.append(b"67890")
        buf.append(b"extra")
        self.assertEqual(buf.total, 10)
        text = buf.drain()
        self.assertEqual(text, "1234567890")
        self.assertEqual(buf.total, 0)
        self.assertEqual(buf.drain(), "")


class TestCodeKernel(unittest.TestCase):
    """Test standalone CodeKernel subprocess engine capabilities."""

    def setUp(self):
        self.kernel = CodeKernel()

    def tearDown(self):
        self.kernel.close()

    def test_cross_call_variable_persistence(self):
        """Verify variables and functions defined in cell 1 survive in cell 2."""
        res1 = self.kernel.execute(
            """
def compute_total(items):
    return sum(items)

base_items = [10, 20, 30]
"""
        )
        self.assertEqual(res1["status"], "ok")

        res2 = self.kernel.execute(
            """
base_items.append(40)
print(f"Total: {compute_total(base_items)}")
"""
        )
        self.assertEqual(res2["status"], "ok")
        self.assertIn("Total: 100", res2["stdout"])

    def test_stdout_and_stderr_separation(self):
        """Verify stdout and stderr write streams land in distinct fields."""
        code = """
import sys
sys.stdout.write("standard output message\\n")
sys.stderr.write("standard error diagnostic\\n")
"""
        res = self.kernel.execute(code)
        self.assertEqual(res["status"], "ok")
        self.assertIn("standard output message", res["stdout"])
        self.assertNotIn("standard error diagnostic", res["stdout"])
        self.assertIn("standard error diagnostic", res["stderr"])
        self.assertNotIn("standard output message", res["stderr"])

    def test_output_capping_and_spill(self):
        """Verify outputs exceeding 1 MB are clipped in memory and spilled to disk."""
        target_len = 1_200_000
        code = f"""
print("X" * {target_len})
"""
        res = self.kernel.execute(code, timeout=60.0)
        self.assertEqual(res["status"], "ok")
        self.assertTrue(res["stdout_clipped"])
        self.assertEqual(len(res["stdout"]), _RUNNER_CAPTURE_BYTES)

        spill_path = res["stdout_spill_path"]
        self.assertTrue(spill_path, "stdout_spill_path should be non-empty")
        self.assertTrue(os.path.exists(spill_path), f"Spill file {spill_path} should exist on disk")

        with open(spill_path, "r", encoding="utf-8") as f:
            spilled_content = f.read()
        self.assertGreaterEqual(len(spilled_content), target_len)
        self.assertIn("Cell stdout exceeded the inline cap", res.get("warning", ""))

    def test_stale_module_detection(self):
        """Verify modules imported by kernel and subsequently edited on disk trigger stale warnings."""
        tmp_dir = tempfile.mkdtemp(prefix="hex_stale_mod_")
        mod_name = "hex_dynamic_test_mod"
        mod_file = os.path.join(tmp_dir, f"{mod_name}.py")
        try:
            with open(mod_file, "w", encoding="utf-8") as f:
                f.write("VALUE = 42\n")

            # Cell 1: import the dynamic module
            cell1 = f"""
import sys
if {repr(tmp_dir)} not in sys.path:
    sys.path.insert(0, {repr(tmp_dir)})
import {mod_name}
print({mod_name}.VALUE)
"""
            res1 = self.kernel.execute(cell1)
            self.assertEqual(res1["status"], "ok")
            self.assertIn("42", res1["stdout"])
            self.assertEqual(res1["stale_modules"], [])

            # Modify module on disk with advanced mtime
            time.sleep(0.05)
            with open(mod_file, "w", encoding="utf-8") as f:
                f.write("VALUE = 999\n")
            future_t = time.time() + 5.0
            os.utime(mod_file, (future_t, future_t))

            # Cell 2: run again; stale scanner should report module
            cell2 = f"""
print({mod_name}.VALUE)
"""
            res2 = self.kernel.execute(cell2)
            self.assertEqual(res2["status"], "ok")
            self.assertIn(mod_name, res2["stale_modules"])
            self.assertIn("Edited on disk after this kernel imported it", res2.get("warning", ""))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_execution_timeout_kills_process(self):
        """Verify wedged execution is terminated via SIGKILL and subsequent call recovers."""
        # Cell 1: infinite loop with short 0.4s timeout
        res1 = self.kernel.execute(
            """
import time
while True:
    time.sleep(0.1)
""",
            timeout=0.4,
        )
        self.assertEqual(res1["status"], "timeout")
        self.assertIn("TimeoutError", res1["traceback"])
        self.assertFalse(self.kernel.alive())

        # Cell 2: Next execution should respawn fresh kernel and succeed
        res2 = self.kernel.execute("print('recovered successfully')", timeout=5.0)
        self.assertEqual(res2["status"], "ok")
        self.assertIn("recovered successfully", res2["stdout"])
        self.assertTrue(self.kernel.alive())

    def test_kernel_reset_clears_state(self):
        """Verify reset() terminates existing child and clears namespace."""
        res1 = self.kernel.execute("persisted_token = 'secret_abc_123'")
        self.assertEqual(res1["status"], "ok")

        self.kernel.reset()

        res2 = self.kernel.execute("print(persisted_token)")
        self.assertEqual(res2["status"], "error")
        self.assertIn("NameError", res2["traceback"])

    def test_syntax_and_runtime_exception_formatting(self):
        """Verify exceptions return tracebacks cleanly without terminating the kernel."""
        # 1. SyntaxError
        res1 = self.kernel.execute("def invalid_syntax(")
        self.assertEqual(res1["status"], "error")
        self.assertIn("SyntaxError", res1["traceback"])
        self.assertTrue(self.kernel.alive())

        # 2. Runtime ZeroDivisionError
        self.kernel.execute("valid_num = 100")
        res2 = self.kernel.execute("1 / 0")
        self.assertEqual(res2["status"], "error")
        self.assertIn("ZeroDivisionError", res2["traceback"])
        self.assertTrue(self.kernel.alive())

        # 3. Kernel still alive and state retained
        res3 = self.kernel.execute("print(valid_num * 2)")
        self.assertEqual(res3["status"], "ok")
        self.assertIn("200", res3["stdout"])

    def test_system_exit_handling(self):
        """Verify SystemExit is caught and reported cleanly."""
        res = self.kernel.execute("import sys; sys.exit(42)")
        self.assertEqual(res["status"], "exit")
        self.assertIn("SystemExit: 42", res["traceback"])


class TestKernelRegistry(unittest.TestCase):
    """Test KernelRegistry pooling, LRU capacity, and idle eviction."""

    def setUp(self):
        reset_kernel_registry()
        self.tmp_a = tempfile.mkdtemp(prefix="hex_reg_a_")
        self.tmp_b = tempfile.mkdtemp(prefix="hex_reg_b_")
        self.tmp_c = tempfile.mkdtemp(prefix="hex_reg_c_")

    def tearDown(self):
        reset_kernel_registry()
        for d in (self.tmp_a, self.tmp_b, self.tmp_c):
            shutil.rmtree(d, ignore_errors=True)

    def test_registry_isolation_and_lru_eviction(self):
        """Verify isolation between distinct keys and LRU eviction when max_kernels exceeded."""
        registry = KernelRegistry(max_kernels=2, idle_timeout=3600.0)

        # 1. Isolation between owners / cwds
        k1, reset1 = registry.get_or_create("user_alpha", self.tmp_a)
        k2, reset2 = registry.get_or_create("user_beta", self.tmp_b)
        self.assertFalse(reset1)
        self.assertFalse(reset2)

        k1.execute("shared_val = 'from_alpha'")
        res2 = k2.execute("print('shared_val' in globals())")
        self.assertEqual(res2["status"], "ok")
        self.assertIn("False", res2["stdout"])

        # 2. LRU eviction with max_kernels=2
        # Update k2 last_used so k1 is oldest
        time.sleep(0.01)
        k2.execute("keep_alive = True")

        # Create 3rd kernel -> should evict k1
        k3, reset3 = registry.get_or_create("user_gamma", self.tmp_c)
        self.assertFalse(reset3)

        abs_a = os.path.abspath(self.tmp_a)
        abs_b = os.path.abspath(self.tmp_b)
        abs_c = os.path.abspath(self.tmp_c)

        self.assertNotIn(("user_alpha", abs_a), registry._kernels)
        self.assertIn(("user_beta", abs_b), registry._kernels)
        self.assertIn(("user_gamma", abs_c), registry._kernels)

        registry.stop()

    def test_registry_idle_sweep(self):
        """Verify idle kernels past idle_timeout are swept."""
        registry = KernelRegistry(max_kernels=4, idle_timeout=0.05)
        k1, _ = registry.get_or_create("user_idle", self.tmp_a)
        k1.execute("x = 1")

        # Wait past idle timeout
        time.sleep(0.1)
        evicted = registry.sweep_idle()
        self.assertIn(k1, evicted)
        self.assertEqual(len(registry._kernels), 0)

        registry.stop()

    def test_stale_modules_warning_helper(self):
        """Verify stale_modules_warning helper formats advisory correctly."""
        self.assertEqual(stale_modules_warning([]), "")
        warn = stale_modules_warning(["pkg.foo", "pkg.bar"])
        self.assertIn("pkg.foo, pkg.bar", warn)
        self.assertIn("importlib.reload", warn)
        self.assertIn("reset=True", warn)


if __name__ == "__main__":
    unittest.main()
