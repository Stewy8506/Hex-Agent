"""B56 regression: skill hook environment hardening + platform shell resolution.

Frontmatter env overrides must not reintroduce dynamic-loader / interpreter
startup / shell-init variables into the claimed clean environment, and the
sandboxed verify hook must use the host's native shell rather than a hardcoded
``/bin/sh`` that native Windows cannot resolve.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness_core.platform import shell as shell_mod
from harness_core.skills.executor import (
    BLOCKED_LOADER_ENV_VARS,
    SkillExecutionEngine,
    SkillVerificationHook,
    build_minimal_skill_env,
)
from harness_core.storage.sandbox import SandboxResult


class TestSkillEnvLoaderPolicy(unittest.TestCase):
    def test_loader_overrides_are_stripped(self) -> None:
        env = build_minimal_skill_env(
            base_env={"PATH": "/usr/bin"},
            env_overrides={
                "LD_PRELOAD": "/evil.so",
                "NODE_OPTIONS": "--require ./evil.js",
                "BASH_ENV": "/evil.sh",
                "DYLD_INSERT_LIBRARIES": "/evil.dylib",
                "PYTHONSTARTUP": "/evil.py",
                "JAVA_TOOL_OPTIONS": "-agentlib:evil",
            },
        )
        for key in (
            "LD_PRELOAD",
            "NODE_OPTIONS",
            "BASH_ENV",
            "DYLD_INSERT_LIBRARIES",
            "PYTHONSTARTUP",
            "JAVA_TOOL_OPTIONS",
        ):
            self.assertNotIn(key, env)

    def test_loader_frontmatter_vars_are_stripped(self) -> None:
        env = build_minimal_skill_env(
            base_env={"PATH": "/usr/bin"},
            env_vars={"LD_AUDIT": "/evil.so", "ENV": "/evil.sh", "RUBYOPT": "-revil"},
        )
        self.assertNotIn("LD_AUDIT", env)
        self.assertNotIn("ENV", env)
        self.assertNotIn("RUBYOPT", env)

    def test_blocklist_covers_named_vectors(self) -> None:
        for key in ("LD_PRELOAD", "NODE_OPTIONS", "BASH_ENV"):
            self.assertIn(key, BLOCKED_LOADER_ENV_VARS)

    def test_benign_overrides_survive(self) -> None:
        env = build_minimal_skill_env(
            base_env={"PATH": "/usr/bin"},
            env_overrides={"MY_FLAG": "1", "LANG": "C"},
        )
        self.assertEqual(env["MY_FLAG"], "1")
        self.assertEqual(env["LANG"], "C")

    def test_secret_overrides_still_blocked(self) -> None:
        env = build_minimal_skill_env(
            base_env={"PATH": "/usr/bin"},
            env_overrides={"OPENAI_API_KEY": "sk-x", "AWS_SECRET_ACCESS_KEY": "y"},
        )
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)

    def test_path_fallback_is_platform_aware(self) -> None:
        with patch.object(shell_mod.platform, "system", return_value="Windows"):
            win_env = build_minimal_skill_env(base_env={})
        self.assertEqual(win_env["PATH"], r"C:\Windows\system32;C:\Windows")
        with patch.object(shell_mod.platform, "system", return_value="Linux"):
            posix_env = build_minimal_skill_env(base_env={})
        self.assertEqual(posix_env["PATH"], "/usr/bin:/bin:/usr/local/bin")


class _RecordingSandbox:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def execute(self, cmd, cwd, env, timeout):  # noqa: ANN001
        self.calls.append({"cmd": cmd, "cwd": cwd, "env": env, "timeout": timeout})
        return SandboxResult(exit_code=0, stdout="ok", stderr="")


class TestSkillHookShellResolution(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.ws = Path(self.temp_dir) / "ws"
        self.ws.mkdir(parents=True, exist_ok=True)
        self.engine = SkillExecutionEngine(default_workspace_root=self.ws)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_windows_compound_hook_uses_native_shell(self) -> None:
        comspec = r"C:\Windows\system32\cmd.exe"
        sandbox = _RecordingSandbox()
        hook = SkillVerificationHook(command="printf x | cat")
        with patch.dict(os.environ, {"COMSPEC": comspec}, clear=False), patch.object(
            shell_mod.platform, "system", return_value="Windows"
        ):
            result = self.engine.run_skill_verification(
                hook=hook, workspace_root=self.ws, sandbox=sandbox
            )
        self.assertTrue(result.passed)
        sent_cmd = sandbox.calls[-1]["cmd"]
        self.assertEqual(sent_cmd[0], comspec)
        self.assertNotIn("/bin/sh", sent_cmd)

    def test_posix_compound_hook_uses_sh(self) -> None:
        sandbox = _RecordingSandbox()
        hook = SkillVerificationHook(command="printf x | cat")
        with patch.object(shell_mod.platform, "system", return_value="Linux"):
            self.engine.run_skill_verification(hook=hook, workspace_root=self.ws, sandbox=sandbox)
        sent_cmd = sandbox.calls[-1]["cmd"]
        self.assertIn(Path(sent_cmd[0]).stem.lower(), {"sh", "bash", "dash", "zsh", "ash", "ksh"}, sent_cmd[0])
        self.assertEqual(sent_cmd[-1], "printf x | cat")


if __name__ == "__main__":
    unittest.main()