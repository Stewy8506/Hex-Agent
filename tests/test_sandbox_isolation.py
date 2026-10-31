"""Zero-escape and Sandbox Isolation Verification Test Suite.

Proves:
- Workspace boundary enforcement: path traversal and out-of-boundary CWDs are strictly blocked.
- Environment variable purging: sensitive credentials (API keys, tokens, secrets) are purged.
- Timeout enforcement: runaway and blocking processes are terminated cleanly via process groups.
- Resource limit enforcement: POSIX limits prevent denial-of-service without crashing the harness.
- Bubblewrap and Docker/Podman command assembly: correct isolation flags and read-only mounts.
- ToolGateway integration: run_tests executes within the sandbox and redacts secrets.
- Auto-detection factory: selects optimal sandbox per platform with safe fallback.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from harness_core.db import LedgerStore
from harness_core.gateway import PolicyBlockedError, ToolGateway
from harness_core.models import TaskSubmitRequest
from harness_core.sandbox import (
    BubblewrapSandbox,
    DockerPodmanSandbox,
    ExecutionSandbox,
    RestrictedSubprocessSandbox,
    SandboxResult,
    get_default_sandbox,
    sanitize_environment,
)


class TestRestrictedSubprocessSandboxIsolation(unittest.TestCase):
    """Verifies isolation, boundary containment, environment stripping, and timeouts."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name).resolve()
        self.workspace_dir = self.base_dir / "workspace"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.outside_dir = self.base_dir / "outside"
        self.outside_dir.mkdir(parents=True, exist_ok=True)

        self.sandbox = RestrictedSubprocessSandbox(allowed_root=self.workspace_dir)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_workspace_boundary_enforcement_blocks_traversal_path(self) -> None:
        """CWD containing path traversal (..) is flagged as a violation."""
        traversal_cwd = str(self.workspace_dir / "../outside")
        res = self.sandbox.execute(
            cmd=[sys.executable, "-c", "print('should not run')"],
            cwd=traversal_cwd,
        )
        self.assertTrue(res.violation_detected)
        self.assertIn("traversal", (res.violation_reason or "").lower())
        self.assertNotEqual(res.exit_code, 0)

    def test_workspace_boundary_enforcement_blocks_outside_directory(self) -> None:
        """CWD resolving outside allowed root is flagged as a violation."""
        res = self.sandbox.execute(
            cmd=[sys.executable, "-c", "print('should not run')"],
            cwd=self.outside_dir,
        )
        self.assertTrue(res.violation_detected)
        self.assertIn("escapes", (res.violation_reason or "").lower())
        self.assertNotEqual(res.exit_code, 0)

    def test_workspace_boundary_enforcement_blocks_nonexistent_directory(self) -> None:
        """Nonexistent CWD is flagged as a violation."""
        nonexistent = self.workspace_dir / "does_not_exist"
        res = self.sandbox.execute(
            cmd=[sys.executable, "-c", "print('should not run')"],
            cwd=nonexistent,
        )
        self.assertTrue(res.violation_detected)
        self.assertIn("does not exist", (res.violation_reason or "").lower())
        self.assertNotEqual(res.exit_code, 0)

    def test_workspace_valid_inside_execution(self) -> None:
        """Valid commands inside the workspace execute successfully."""
        sub_dir = self.workspace_dir / "subpkg"
        sub_dir.mkdir()
        res = self.sandbox.execute(
            cmd=[sys.executable, "-c", "print('sandbox_execution_success')"],
            cwd=sub_dir,
        )
        self.assertFalse(res.violation_detected)
        self.assertEqual(res.exit_code, 0)
        self.assertIn("sandbox_execution_success", res.stdout)
        self.assertEqual(res.sandbox_backend, "restricted_subprocess")

    def test_environment_variable_purging_strips_sensitive_credentials(self) -> None:
        """Injected sensitive credentials in parent and passed environment are completely purged."""
        sensitive_vars = {
            "OPENAI_API_KEY": "sk-secret12345678901234",
            "GITHUB_TOKEN": "ghp_token45678901234567890",
            "HARNESS_SECRET_KEY": "supersecretkey9999",
            "AWS_SECRET_ACCESS_KEY": "awssecretaccesskey123",
            "MY_PASSWORD": "supersecretpassword",
        }

        with mock.patch.dict(os.environ, sensitive_vars, clear=False):
            test_script = (
                "import os, json\n"
                "print(json.dumps(dict(os.environ)))\n"
            )
            passed_env = {
                "CUSTOM_SAFE_CONFIG": "safe_value_xyz",
                "USER_API_KEY": "evil_injected_api_key",
                "GH_SECRET": "evil_gh_secret",
            }
            res = self.sandbox.execute(
                cmd=[sys.executable, "-c", test_script],
                cwd=self.workspace_dir,
                env=passed_env,
            )
            self.assertEqual(res.exit_code, 0)
            env_dump = json.loads(res.stdout.strip())

            # Verify none of the sensitive keys exist in child environment
            for key in sensitive_vars:
                self.assertNotIn(key, env_dump)
            self.assertNotIn("USER_API_KEY", env_dump)
            self.assertNotIn("GH_SECRET", env_dump)

            # Verify safe passed variable is preserved
            self.assertEqual(env_dump.get("CUSTOM_SAFE_CONFIG"), "safe_value_xyz")

            # Verify none of the secrets appear anywhere in raw output
            for secret_val in sensitive_vars.values():
                self.assertNotIn(secret_val, res.stdout)
            self.assertNotIn("evil_injected_api_key", res.stdout)

    def test_timeout_enforcement_terminates_runaway_process(self) -> None:
        """Processes exceeding timeout are cleanly terminated via process groups."""
        sleep_script = "import time; time.sleep(10)"
        t0 = time.perf_counter()
        res = self.sandbox.execute(
            cmd=[sys.executable, "-c", sleep_script],
            cwd=self.workspace_dir,
            timeout=1.0,
        )
        elapsed = time.perf_counter() - t0
        self.assertTrue(res.timed_out)
        self.assertLess(elapsed, 2.5)
        self.assertNotEqual(res.exit_code, 0)

    def test_resource_limit_handling_terminates_cleanly(self) -> None:
        """Tight CPU timeout terminates compute-heavy loop cleanly without crashing."""
        spin_script = "import time\nwhile True:\n    pass"
        res = self.sandbox.execute(
            cmd=[sys.executable, "-c", spin_script],
            cwd=self.workspace_dir,
            timeout=1.0,
        )
        self.assertTrue(res.timed_out)
        self.assertFalse(res.violation_detected)


class TestBubblewrapSandboxCommandAssembly(unittest.TestCase):
    """Verifies BubblewrapSandbox command construction and execution contract."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace_dir = Path(self.temp_dir.name).resolve()
        self.sandbox = BubblewrapSandbox(
            bwrap_bin="/usr/bin/bwrap",
            allowed_root=self.workspace_dir,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_build_bwrap_args_includes_isolation_flags(self) -> None:
        """Validates network isolation, ro-bind system mounts, tmpfs, and workspace bind mounts."""
        cmd, env = self.sandbox.build_bwrap_args(
            cmd=["python3", "-m", "unittest"],
            cwd=self.workspace_dir,
            env={"CUSTOM_SETTING": "1"},
        )
        self.assertEqual(cmd[0], "/usr/bin/bwrap")
        self.assertIn("--unshare-net", cmd)
        self.assertIn("--unshare-pid", cmd)
        self.assertIn("--unshare-ipc", cmd)

        # System ro-bind mounts
        self.assertIn("--ro-bind", cmd)
        self.assertIn("/usr", cmd)
        self.assertIn("/bin", cmd)
        self.assertIn("/etc", cmd)

        # Temporary and device mounts
        self.assertIn("--proc", cmd)
        self.assertIn("/proc", cmd)
        self.assertIn("--dev", cmd)
        self.assertIn("/dev", cmd)
        self.assertIn("--tmpfs", cmd)
        self.assertIn("/tmp", cmd)

        # Workspace bind mount and chdir
        ws_str = str(self.workspace_dir.resolve())
        self.assertIn("--bind", cmd)
        self.assertIn(ws_str, cmd)
        self.assertIn("--chdir", cmd)

        # Environment stripping & passing
        self.assertIn("--clearenv", cmd)
        self.assertIn("--setenv", cmd)

        # Terminating -- and command
        self.assertIn("--", cmd)
        self.assertEqual(cmd[-3:], ["python3", "-m", "unittest"])

    def test_bubblewrap_mock_execution_returns_sandbox_result(self) -> None:
        """BubblewrapSandbox execute returns properly formatted SandboxResult on mock runner."""
        with mock.patch("subprocess.Popen") as mock_popen:
            mock_proc = mock.MagicMock()
            mock_proc.communicate.return_value = ("bwrap_output", "")
            mock_proc.returncode = 0
            mock_popen.return_value = mock_proc

            res = self.sandbox.execute(
                cmd=["echo", "1"],
                cwd=self.workspace_dir,
            )
            self.assertIsInstance(res, SandboxResult)
            self.assertEqual(res.exit_code, 0)
            self.assertEqual(res.stdout, "bwrap_output")
            self.assertEqual(res.sandbox_backend, "bubblewrap")


class TestDockerPodmanSandboxCommandAssembly(unittest.TestCase):
    """Verifies DockerPodmanSandbox container command assembly and execution contract."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace_dir = Path(self.temp_dir.name).resolve()
        self.sandbox = DockerPodmanSandbox(
            cli_binary="docker",
            image="python:3.11-slim",
            allowed_root=self.workspace_dir,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_build_container_args_includes_isolation_flags(self) -> None:
        """Validates network none, read-only root, tmpfs, volume mounts, and working dir."""
        cmd, env = self.sandbox.build_container_args(
            cmd=["pytest", "tests/"],
            cwd=self.workspace_dir,
            env={"ENV_TEST": "val"},
        )
        self.assertEqual(cmd[0], "docker")
        self.assertEqual(cmd[1], "run")
        self.assertIn("--rm", cmd)
        self.assertIn("--network", cmd)
        self.assertIn("none", cmd)

        ws_str = str(self.workspace_dir.resolve())
        self.assertIn("-v", cmd)
        self.assertIn(f"{ws_str}:{ws_str}:rw", cmd)
        self.assertIn("-w", cmd)
        self.assertIn(ws_str, cmd)

        self.assertIn("--read-only", cmd)
        self.assertIn("--tmpfs", cmd)
        self.assertIn("/tmp:rw,noexec,nosuid", cmd)

        self.assertIn("python:3.11-slim", cmd)
        self.assertEqual(cmd[-2:], ["pytest", "tests/"])

    def test_docker_mock_execution_returns_sandbox_result(self) -> None:
        """DockerPodmanSandbox execute returns properly formatted SandboxResult on mock runner."""
        with mock.patch("subprocess.Popen") as mock_popen:
            mock_proc = mock.MagicMock()
            mock_proc.communicate.return_value = ("docker_output", "")
            mock_proc.returncode = 0
            mock_popen.return_value = mock_proc

            res = self.sandbox.execute(
                cmd=["pytest"],
                cwd=self.workspace_dir,
            )
            self.assertIsInstance(res, SandboxResult)
            self.assertEqual(res.exit_code, 0)
            self.assertEqual(res.stdout, "docker_output")
            self.assertEqual(res.sandbox_backend, "docker")


class TestToolGatewaySandboxIntegration(unittest.TestCase):
    """Verifies that ToolGateway runs untrusted tests inside the sandbox and confines paths."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name).resolve()
        self.db_path = self.base_dir / "ledger.db"
        self.ledger = LedgerStore(self.db_path)
        self.workspace_dir = self.base_dir / "workspace"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)

        self.sandbox = RestrictedSubprocessSandbox(allowed_root=self.workspace_dir)
        self.gateway = ToolGateway(ledger=self.ledger, sandbox=self.sandbox)

        task = self.ledger.submit_task(
            TaskSubmitRequest(
                goal="Testing sandbox integration inside ToolGateway",
                project_id="proj_sandbox_test",
            )
        )
        self.task_id = task["task_id"]

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_tool_run_tests_executes_in_sandbox_and_reports_backend(self) -> None:
        """run_tests invocation succeeds and includes sandbox_backend in response payload."""
        # Create a passing unit test in workspace
        test_file = self.workspace_dir / "test_example.py"
        test_file.write_text(
            "import unittest\n"
            "class DummyTestCase(unittest.TestCase):\n"
            "    def test_pass(self):\n"
            "        self.assertTrue(True)\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n",
            encoding="utf-8",
        )

        res = self.gateway.invoke(
            task_id=self.task_id,
            attempt_id="att_1",
            step_id="stp_1",
            sequence_in_step=1,
            tool_id="run_tests",
            args={"targets": ["test_example.py"]},
            workspace_dir=self.workspace_dir,
        )

        self.assertTrue(res["ok"])
        self.assertEqual(res["exit_code"], 0)
        self.assertFalse(res["timed_out"])
        self.assertFalse(res["violation_detected"])
        self.assertEqual(res["sandbox_backend"], "restricted_subprocess")
        self.assertIn("Ran 1 test", res["output"])

    def test_tool_run_tests_redacts_secrets_in_output(self) -> None:
        """Secrets printed during test execution are redacted by redact_secrets."""
        test_file = self.workspace_dir / "test_secret_leak.py"
        test_file.write_text(
            "import unittest\n"
            "class LeakTestCase(unittest.TestCase):\n"
            "    def test_leaking_key(self):\n"
            "        print('Leaked key: sk-live1234567890abcdef')\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n",
            encoding="utf-8",
        )

        res = self.gateway.invoke(
            task_id=self.task_id,
            attempt_id="att_1",
            step_id="stp_1",
            sequence_in_step=2,
            tool_id="run_tests",
            args={"targets": ["test_secret_leak.py"]},
            workspace_dir=self.workspace_dir,
        )

        self.assertNotIn("sk-live1234567890abcdef", res["output"])
        self.assertIn("[REDACTED_SECRET]", res["output"])

    def test_destructive_file_action_path_escape_raises_policy_blocked_error(self) -> None:
        """write_file attempting path traversal outside workspace raises PolicyBlockedError."""
        with self.assertRaises(PolicyBlockedError):
            self.gateway.invoke(
                task_id=self.task_id,
                attempt_id="att_1",
                step_id="stp_1",
                sequence_in_step=3,
                tool_id="write_file",
                args={"path": "../escaped_destruct.txt", "content": "bad"},
                workspace_dir=self.workspace_dir,
            )

    def test_tool_run_tests_violation_raises_policy_blocked_error(self) -> None:
        """If sandbox detects boundary violation, run_tests raises PolicyBlockedError."""
        outside_dir = self.base_dir / "outside_dir"
        outside_dir.mkdir(parents=True, exist_ok=True)

        with self.assertRaises(PolicyBlockedError):
            self.gateway.invoke(
                task_id=self.task_id,
                attempt_id="att_1",
                step_id="stp_1",
                sequence_in_step=4,
                tool_id="run_tests",
                args={},
                workspace_dir=outside_dir,
            )


class TestSandboxAutoDetection(unittest.TestCase):
    """Verifies get_default_sandbox platform selection and fallbacks."""

    def test_linux_bubblewrap_detected_when_available(self) -> None:
        with mock.patch.object(BubblewrapSandbox, "is_available", return_value=True):
            sandbox = get_default_sandbox()
            self.assertIsInstance(sandbox, BubblewrapSandbox)
            self.assertEqual(sandbox.backend_name, "bubblewrap")

    def test_docker_podman_detected_when_bwrap_unavailable(self) -> None:
        with mock.patch.object(BubblewrapSandbox, "is_available", return_value=False):
            with mock.patch.object(DockerPodmanSandbox, "is_available", return_value=True):
                sandbox = get_default_sandbox()
                self.assertIsInstance(sandbox, DockerPodmanSandbox)

    def test_fallback_to_restricted_subprocess(self) -> None:
        with mock.patch.object(BubblewrapSandbox, "is_available", return_value=False):
            with mock.patch.object(DockerPodmanSandbox, "is_available", return_value=False):
                sandbox = get_default_sandbox()
                self.assertIsInstance(sandbox, RestrictedSubprocessSandbox)
                self.assertEqual(sandbox.backend_name, "restricted_subprocess")


if __name__ == "__main__":
    unittest.main()
