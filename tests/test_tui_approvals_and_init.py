"""Unit & Integration Test Suite for Keyboard Approvals, Subshell, Stack Detection & Project Scaffolding.

Validates:
1. Keyboard approval prompt loop in HexConsole ([y] approve, [n] reject, [d] diff, [s] shell, [q] abort).
2. Safe fallback on prompt EOF or interruption without unhandled exceptions.
3. Repository stack detection for Python, Node/TS, Rust, Go, and generic projects.
4. Project scaffolding (hex init) writing .hex/config.yaml and .hex/.gitignore with force preservation.
5. CLI `hex init --workspace <dir> --data-dir :memory:` integration.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness_core.cli import main
from harness_core.gateway import ApprovalRequiredInterrupt
from harness_core.onboard import detect_project_stack, init_project
from harness_core.orchestrator import HarnessOrchestrator
from harness_core.tui import HexConsole


class TestKeyboardApprovalsAndSubshell(unittest.TestCase):
    """Tests for interactive keyboard approval handling and isolated subshell in HexConsole."""

    def _setup_approval_fixture(self):
        orch = HarnessOrchestrator(":memory:")
        t = orch.ledger.submit_task({"goal": "test_approval_goal"})
        apr = orch.ledger.create_approval(
            task_id=t["task_id"],
            attempt_id="att1",
            step_id="step1",
            action_type="git_push",
            action_params={"remote": "origin", "branch": "main", "token": "secret_token_123"},
            blast_radius_summary="Pushes changes to remote repository",
            risk_class="R3",
        )
        console = HexConsole(orch)
        return orch, t, apr, console

    def test_approval_y_resumes_task(self):
        orch, t, apr, console = self._setup_approval_fixture()
        apr_int = ApprovalRequiredInterrupt(apr)
        res = console.handle_approval_prompt(apr_int, prompt_fn=lambda _: "y")
        self.assertEqual(res["decision"], "APPROVED")
        rec = orch.ledger.get_approval(apr["approval_id"])
        self.assertIsNotNone(rec)
        self.assertEqual(rec["decision"], "APPROVED")

    def test_approval_n_rejects_with_comment(self):
        orch, t, apr, console = self._setup_approval_fixture()
        apr_int = ApprovalRequiredInterrupt(apr)
        inputs = iter(["n", "unsafe operation"])
        res = console.handle_approval_prompt(apr_int, prompt_fn=lambda _: next(inputs))
        self.assertEqual(res["decision"], "REJECTED")
        rec = orch.ledger.get_approval(apr["approval_id"])
        self.assertIsNotNone(rec)
        self.assertEqual(rec["decision"], "REJECTED")
        self.assertEqual(rec["comment"], "unsafe operation")

    def test_approval_d_displays_diff_then_approves(self):
        orch, t, apr, console = self._setup_approval_fixture()
        tmpdir = Path(tempfile.mkdtemp())
        try:
            base_dir = tmpdir / "base"
            attempt_dir = tmpdir / "attempt"
            base_dir.mkdir()
            attempt_dir.mkdir()
            (base_dir / "foo.py").write_text("print('old')\n")
            (attempt_dir / "foo.py").write_text("print('new')\n")

            lease_info = orch.ledger.acquire_lease_and_attempt(t["task_id"], "w1", workspace_path=str(attempt_dir))
            orch.ledger.create_checkpoint(
                task_id=t["task_id"],
                attempt_id=lease_info["attempt_id"],
                plan_revision=1,
                completed_steps=[],
                pending_steps=[],
                workspace_tree_sha="sha1",
                snapshot_path=str(base_dir),
            )

            inputs = iter(["d", "y"])
            apr_int = ApprovalRequiredInterrupt(apr)
            with patch("sys.stdout", new_callable=StringIO) as fake_out:
                res = console.handle_approval_prompt(apr_int, prompt_fn=lambda _: next(inputs))
                out_str = fake_out.getvalue()
                self.assertIn("Approval Workspace Diff", out_str)
                self.assertIn("foo.py", out_str)

            self.assertEqual(res["decision"], "APPROVED")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_approval_s_launches_sandbox_subshell(self):
        orch, t, apr, console = self._setup_approval_fixture()
        tmpdir = Path(tempfile.mkdtemp())
        try:
            attempt_dir = tmpdir / "attempt"
            attempt_dir.mkdir()
            orch.ledger.acquire_lease_and_attempt(t["task_id"], "w1", workspace_path=str(attempt_dir))

            shell_invoked_with: list[str] = []

            def dummy_shell(path):
                shell_invoked_with.append(str(path))

            inputs = iter(["s", "y"])
            apr_int = ApprovalRequiredInterrupt(apr)
            res = console.handle_approval_prompt(
                apr_int,
                prompt_fn=lambda _: next(inputs),
                shell_runner=dummy_shell,
            )
            self.assertEqual(len(shell_invoked_with), 1)
            self.assertEqual(shell_invoked_with[0], str(attempt_dir))
            self.assertEqual(res["decision"], "APPROVED")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_approval_q_aborts_and_rolls_back(self):
        orch, t, apr, console = self._setup_approval_fixture()
        apr_int = ApprovalRequiredInterrupt(apr)
        res = console.handle_approval_prompt(apr_int, prompt_fn=lambda _: "q")
        self.assertEqual(res["decision"], "ABORTED")
        task = orch.ledger.get_task(t["task_id"])
        self.assertEqual(task["status"], "CANCELED")

    def test_approval_eof_safe_fallback(self):
        orch, t, apr, console = self._setup_approval_fixture()
        apr_int = ApprovalRequiredInterrupt(apr)

        def raise_eof(_):
            raise EOFError("stdin closed")

        res = console.handle_approval_prompt(apr_int, prompt_fn=raise_eof)
        self.assertEqual(res["decision"], "ABORTED")
        task = orch.ledger.get_task(t["task_id"])
        self.assertEqual(task["status"], "CANCELED")


class TestStackDetection(unittest.TestCase):
    """Tests for repository stack auto-discovery across heterogeneous tech stacks."""

    def test_detect_python_stack(self):
        tmpdir = Path(tempfile.mkdtemp())
        try:
            (tmpdir / "pyproject.toml").write_text("[project]\nname='test'\n")
            (tmpdir / "pytest.ini").write_text("[pytest]\n")
            stack = detect_project_stack(tmpdir)
            self.assertEqual(stack["stack"], "python")
            self.assertEqual(stack["test_cmd"], "pytest")
            self.assertIn(".py", stack["watch_extensions"])
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_detect_node_stack(self):
        tmpdir = Path(tempfile.mkdtemp())
        try:
            (tmpdir / "package.json").write_text('{"name": "test"}')
            stack = detect_project_stack(tmpdir)
            self.assertEqual(stack["stack"], "node")
            self.assertEqual(stack["test_cmd"], "npm test")
            self.assertIn(".ts", stack["watch_extensions"])
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_detect_rust_stack(self):
        tmpdir = Path(tempfile.mkdtemp())
        try:
            (tmpdir / "Cargo.toml").write_text('[package]\nname = "test"\n')
            stack = detect_project_stack(tmpdir)
            self.assertEqual(stack["stack"], "rust")
            self.assertEqual(stack["test_cmd"], "cargo test")
            self.assertIn(".rs", stack["watch_extensions"])
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_detect_go_stack(self):
        tmpdir = Path(tempfile.mkdtemp())
        try:
            (tmpdir / "go.mod").write_text("module example.com/test\n")
            stack = detect_project_stack(tmpdir)
            self.assertEqual(stack["stack"], "go")
            self.assertEqual(stack["test_cmd"], "go test ./...")
            self.assertIn(".go", stack["watch_extensions"])
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_detect_generic_stack(self):
        tmpdir = Path(tempfile.mkdtemp())
        try:
            stack = detect_project_stack(tmpdir)
            self.assertEqual(stack["stack"], "generic")
            self.assertEqual(stack["test_cmd"], "make test")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestProjectInitialization(unittest.TestCase):
    """Tests for `hex init` and `init_project` project configuration scaffolding."""

    def test_init_project_creates_files(self):
        tmpdir = Path(tempfile.mkdtemp())
        try:
            (tmpdir / "pyproject.toml").write_text("[project]\nname='my-python-app'\n")
            res = init_project(tmpdir)
            self.assertTrue(res["created"])
            self.assertEqual(res["stack"], "python")
            self.assertEqual(res["test_command"], "pytest")

            cfg_path = tmpdir / ".hex" / "config.yaml"
            git_path = tmpdir / ".hex" / ".gitignore"
            self.assertTrue(cfg_path.is_file())
            self.assertTrue(git_path.is_file())

            cfg_text = cfg_path.read_text()
            self.assertIn(f'name: "{tmpdir.name}"', cfg_text)
            self.assertIn('stack: "python"', cfg_text)
            self.assertIn('test_command: "pytest"', cfg_text)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_init_project_preserves_existing_without_force(self):
        tmpdir = Path(tempfile.mkdtemp())
        try:
            hex_dir = tmpdir / ".hex"
            hex_dir.mkdir()
            custom_cfg = hex_dir / "config.yaml"
            custom_cfg.write_text("# Custom config\nfoo: bar\n")

            res = init_project(tmpdir, force=False)
            self.assertFalse(res["created"])
            self.assertEqual(custom_cfg.read_text(), "# Custom config\nfoo: bar\n")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_init_project_overwrites_with_force(self):
        tmpdir = Path(tempfile.mkdtemp())
        try:
            (tmpdir / "Cargo.toml").write_text('[package]\nname = "rust-app"\n')
            hex_dir = tmpdir / ".hex"
            hex_dir.mkdir()
            custom_cfg = hex_dir / "config.yaml"
            custom_cfg.write_text("# Custom config\nfoo: bar\n")

            res = init_project(tmpdir, force=True)
            self.assertTrue(res["created"])
            new_text = custom_cfg.read_text()
            self.assertIn('stack: "rust"', new_text)
            self.assertIn('test_command: "cargo test"', new_text)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_cli_init_command_with_workspace(self):
        tmpdir = Path(tempfile.mkdtemp())
        try:
            (tmpdir / "package.json").write_text('{"name": "cli-test"}')
            with patch("sys.stdout", new_callable=StringIO) as fake_out:
                ret = main(["init", "--workspace", str(tmpdir), "--data-dir", ":memory:"])
                self.assertEqual(ret, 0)
                out_str = fake_out.getvalue()
                data = json.loads(out_str)
                self.assertEqual(data["status"], "initialized")
                self.assertIn("project", data)
                self.assertEqual(data["project"]["stack"], "node")
                self.assertEqual(data["project"]["test_command"], "npm test")
                self.assertIn("seeded_skills", data)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
