"""B05 regression: platform-aware shell resolution for compound commands.

Native Windows process creation cannot resolve a hardcoded ``/bin/sh``. These
tests pin the shared resolver's POSIX/Windows branches and prove the gateway
terminal tool routes operator-bearing commands through the host shell instead
of a Unix path. The Windows branch is exercised deterministically via the
``system`` override and mocked shell discovery (no Windows host required).
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness_core.gateway import ToolGateway
from harness_core.platform import shell as shell_mod
from harness_core.platform.shell import (
    ShellResolutionError,
    build_command_argv,
    command_requires_shell,
    resolve_shell_argv,
)
from harness_core.storage.db import LedgerStore
from harness_core.storage.sandbox import SandboxResult


POSIX_SHELL_STEMS = {"sh", "bash", "dash", "zsh", "ash", "ksh"}


def is_posix_shell(path: str) -> bool:
    # $SHELL may point at Git Bash (bash.exe) when the POSIX branch is simulated on Windows.
    return Path(path).stem.lower() in POSIX_SHELL_STEMS


class TestShellResolver(unittest.TestCase):
    def test_posix_compound_uses_sh(self) -> None:
        argv = resolve_shell_argv("printf audit | cat", system="Linux")
        self.assertEqual(argv[-2:], ["-c", "printf audit | cat"])
        self.assertTrue(is_posix_shell(argv[0]), argv[0])
        self.assertNotIn("/bin/sh", argv[1:])

    def test_windows_compound_uses_comspec_not_bin_sh(self) -> None:
        comspec = r"C:\Windows\system32\cmd.exe"
        with patch.dict(os.environ, {"COMSPEC": comspec}, clear=False):
            argv = resolve_shell_argv("printf audit | cat", system="Windows")
        self.assertEqual(argv, [comspec, "/d", "/c", "printf audit | cat"])
        self.assertNotIn("/bin/sh", argv)

    def test_windows_falls_back_to_cmd_on_path(self) -> None:
        cmd_path = r"C:\Windows\system32\cmd.exe"
        env = {k: v for k, v in os.environ.items() if k != "COMSPEC"}
        with patch.dict(os.environ, env, clear=True), patch.object(
            shell_mod.shutil, "which", side_effect=lambda n: cmd_path if n in ("cmd.exe", "cmd") else None
        ):
            argv = resolve_shell_argv("a && b", system="Windows")
        self.assertEqual(argv[0], cmd_path)
        self.assertEqual(argv[1:3], ["/d", "/c"])

    def test_windows_falls_back_to_powershell(self) -> None:
        ps_path = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        env = {k: v for k, v in os.environ.items() if k != "COMSPEC"}

        def fake_which(name: str):
            return ps_path if name in ("powershell", "pwsh") else None

        with patch.dict(os.environ, env, clear=True), patch.object(
            shell_mod.shutil, "which", side_effect=fake_which
        ):
            argv = resolve_shell_argv("a | b", system="Windows")
        self.assertEqual(argv[0], ps_path)
        self.assertIn("-NoProfile", argv)
        self.assertIn("-Command", argv)

    def test_windows_missing_shell_raises_actionable(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "COMSPEC"}
        with patch.dict(os.environ, env, clear=True), patch.object(
            shell_mod.shutil, "which", return_value=None
        ):
            with self.assertRaises(ShellResolutionError) as ctx:
                resolve_shell_argv("a | b", system="Windows")
        self.assertIn("COMSPEC", str(ctx.exception))

    def test_posix_missing_shell_raises_actionable(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "SHELL"}
        with patch.dict(os.environ, env, clear=True), patch.object(
            shell_mod.Path, "exists", return_value=False
        ), patch.object(shell_mod.shutil, "which", return_value=None):
            with self.assertRaises(ShellResolutionError):
                resolve_shell_argv("a | b", system="Linux")

    def test_plain_command_avoids_shell(self) -> None:
        self.assertEqual(build_command_argv("echo hello", system="Linux"), ["echo", "hello"])

    def test_windows_plain_preserves_backslash_paths(self) -> None:
        argv = build_command_argv(r"C:\tools\app.exe --flag", system="Windows")
        self.assertIn(r"C:\tools\app.exe", argv)
        self.assertNotIn("-c", argv)

    def test_command_requires_shell_detection(self) -> None:
        for op in ("|", ">", "<", ";", "&&", "||", "$"):
            self.assertTrue(command_requires_shell(f"a {op} b"), op)
        self.assertFalse(command_requires_shell("echo hello"))


class _RecordingSandbox:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def execute(self, cmd, cwd, env, timeout):  # noqa: ANN001
        self.calls.append({"cmd": cmd, "cwd": cwd, "env": env, "timeout": timeout})
        return SandboxResult(exit_code=0, stdout="ok", stderr="")


class TestGatewayTerminalPlatform(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.workspace = Path(self.temp_dir) / "ws"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.ledger = LedgerStore(Path(self.temp_dir) / "ledger.db")
        self.gateway = ToolGateway(self.ledger)
        self.sandbox = _RecordingSandbox()
        self.gateway.sandbox = self.sandbox

    def tearDown(self) -> None:
        self.ledger.close()
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_terminal_windows_compound_uses_native_shell(self) -> None:
        comspec = r"C:\Windows\system32\cmd.exe"
        with patch.dict(os.environ, {"COMSPEC": comspec}, clear=False), patch.object(
            shell_mod.platform, "system", return_value="Windows"
        ):
            res = self.gateway._tool_terminal(self.workspace, {"command": "printf audit | cat"})
        self.assertTrue(res["ok"])
        sent_cmd = self.sandbox.calls[-1]["cmd"]
        self.assertEqual(sent_cmd[0], comspec)
        self.assertNotIn("/bin/sh", sent_cmd)

    def test_terminal_posix_compound_uses_sh(self) -> None:
        with patch.object(shell_mod.platform, "system", return_value="Linux"):
            self.gateway._tool_terminal(self.workspace, {"command": "printf audit | cat"})
        sent_cmd = self.sandbox.calls[-1]["cmd"]
        self.assertTrue(is_posix_shell(sent_cmd[0]), sent_cmd[0])
        self.assertEqual(sent_cmd[-1], "printf audit | cat")

    def test_terminal_list_command_passthrough(self) -> None:
        self.gateway._tool_terminal(self.workspace, {"command": ["echo", "hi"]})
        self.assertEqual(self.sandbox.calls[-1]["cmd"], ["echo", "hi"])


if __name__ == "__main__":
    unittest.main()