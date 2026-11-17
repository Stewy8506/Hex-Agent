"""F03 install-contract tests.

Part A proves the launcher resolves the installed artifact and never invokes
an implicit compiler rescue. Part B installs the actual wheel into an
isolated prefix, places the native binary at ~/.hex/bin, and completes the
documented first task (`hex tui --check`) outside the source checkout with a
minimal PATH (no cargo, no pre-existing hex), then exercises upgrade and
uninstall against the same contract.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness_core.client import tui_launcher

from tests import REPO_ROOT
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"


def _make_executable(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _launcher_args(**overrides) -> argparse.Namespace:
    base = dict(
        check=True, project="default", workspace=None, model=None,
        cow=None, session=None, build=False, release=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


class TestLauncherResolution(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.fake_root = self.root / "checkout"
        self.fake_root.mkdir()
        self.empty_bin = self.root / "empty-bin"
        self.empty_bin.mkdir()

        env = mock.patch.dict(
            os.environ, {"HOME": str(self.home), "PATH": str(self.empty_bin)}, clear=False
        )
        env.start()
        self.addCleanup(env.stop)
        for var in ("HEX_TUI_BIN", "HEX_DEV_BUILD"):
            patcher = mock.patch.dict(os.environ, {var: ""})
            patcher.start()
            self.addCleanup(patcher.stop)
            os.environ.pop(var, None)

    def test_installed_artifact_is_resolved(self) -> None:
        installed = _make_executable(self.home / ".hex" / "bin" / "hex-tui")
        found = tui_launcher.find_hex_tui_binary(self.fake_root)
        self.assertEqual(found, installed)

    def test_checkout_build_shadows_installed_artifact(self) -> None:
        _make_executable(self.home / ".hex" / "bin" / "hex-tui")
        checkout_bin = _make_executable(self.fake_root / "target" / "debug" / "hex-tui")
        found = tui_launcher.find_hex_tui_binary(self.fake_root)
        self.assertEqual(found, checkout_bin)

    def test_env_override_wins(self) -> None:
        _make_executable(self.home / ".hex" / "bin" / "hex-tui")
        override = _make_executable(self.root / "custom" / "hex-tui")
        with mock.patch.dict(os.environ, {"HEX_TUI_BIN": str(override)}):
            self.assertEqual(tui_launcher.find_hex_tui_binary(self.fake_root), override.resolve())

    def test_missing_install_is_an_honest_error_not_a_cargo_rescue(self) -> None:
        with mock.patch.object(
            tui_launcher.subprocess, "run", side_effect=AssertionError("implicit build attempted")
        ) as run_mock:
            with self.assertRaises(FileNotFoundError) as ctx:
                tui_launcher.launch_hex_tui(_launcher_args(), self.fake_root)
        run_mock.assert_not_called()
        message = str(ctx.exception)
        self.assertIn("install.sh", message)
        self.assertIn("--legacy", message)

    def test_explicit_developer_build_is_opt_in(self) -> None:
        stub_bin = _make_executable(self.root / "built" / "hex-tui")
        with mock.patch.dict(os.environ, {"HEX_DEV_BUILD": "1"}):
            with mock.patch.object(
                tui_launcher, "build_hex_tui", return_value=stub_bin
            ) as build_mock:
                with mock.patch.object(
                    tui_launcher.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0),
                ):
                    rc = tui_launcher.launch_hex_tui(_launcher_args(), self.fake_root)
        self.assertEqual(rc, 0)
        build_mock.assert_called_once()


@unittest.skipIf(os.name == "nt", "POSIX install contract test")
class TestInstalledWheelContract(unittest.TestCase):
    """Wheel + native artifact install, first task, upgrade, uninstall."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.binary = None
        for profile in ("release", "debug"):
            candidate = REPO_ROOT / "target" / profile / "hex-tui"
            if candidate.is_file():
                cls.binary = candidate
                break
        if cls.binary is None:
            raise unittest.SkipTest("hex-tui binary not built (cargo build -p hex-tui)")
        if not VENV_PYTHON.is_file():
            raise unittest.SkipTest("development venv not present")

        cls._tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls._tmp.name)
        cls.wheel_dir = cls.base / "dist"
        cls.site = cls.base / "site"
        cls.home = cls.base / "home"
        cls.ws = cls.base / "ws"
        cls.ws.mkdir()

        wheel_build = subprocess.run(
            [str(VENV_PYTHON), "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
             "-w", str(cls.wheel_dir), str(REPO_ROOT)],
            capture_output=True, text=True,
        )
        if wheel_build.returncode != 0:
            cls._tmp.cleanup()
            raise unittest.SkipTest(f"wheel build unavailable: {wheel_build.stderr[-400:]}")
        wheels = list(cls.wheel_dir.glob("hex_ai-*.whl"))
        if not wheels:
            cls._tmp.cleanup()
            raise unittest.SkipTest("wheel build produced no hex_ai wheel")
        cls.wheel = wheels[0]

        deps_site = subprocess.run(
            [str(VENV_PYTHON), "-c", "import site; print(site.getsitepackages()[0])"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        cls.pythonpath = f"{cls.site}:{deps_site}"

        install = subprocess.run(
            [str(VENV_PYTHON), "-m", "pip", "install", "--no-deps", "--no-index",
             "--target", str(cls.site), str(cls.wheel)],
            capture_output=True, text=True,
        )
        if install.returncode != 0:
            cls._tmp.cleanup()
            raise unittest.SkipTest(f"wheel install failed: {install.stderr[-400:]}")

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "_tmp"):
            cls._tmp.cleanup()

    def _env(self) -> dict[str, str]:
        return {
            "HOME": str(self.home),
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": self.pythonpath,
            "HEX_TESTING": "1",
            "LANG": "en_US.UTF-8",
        }

    def _run_hex(self, argv: list[str]) -> subprocess.CompletedProcess:
        code = (
            "import sys; from harness_core.client.cli import main; "
            f"sys.exit(main({argv!r}))"
        )
        return subprocess.run(
            [str(VENV_PYTHON), "-c", code],
            cwd=str(self.ws), env=self._env(), capture_output=True, text=True, timeout=120,
        )

    def _install_native(self) -> Path:
        target = self.home / ".hex" / "bin" / "hex-tui"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.binary, target)
        target.chmod(0o755)
        return target

    def test_packaged_imports_and_skill_resources(self) -> None:
        code = """
from pathlib import Path
import harness_core
from harness_core import HarnessOrchestrator, LedgerStore, ToolGateway
from harness_core.client.cli import main
from harness_core.client.onboard import HardwareProfile, init_project, update_config_yaml_model
from harness_core.platform.hardware import HardwareProfile as SharedHardwareProfile
from harness_core.storage.project_init import init_project as shared_init_project
from harness_core.config_loader import update_config_yaml_model as shared_model_writer
from harness_core.client.tui import strip_ansi, render_unified_diff
from harness_core.client.terminal.ansi import strip_ansi as shared_strip_ansi
from harness_core.client.terminal.review import render_unified_diff as shared_render_diff
from harness_core.resources import bundled_skills_dir
from harness_core.skills import SkillCompiler, SkillExecutionEngine
package = Path(harness_core.__file__).resolve().parent
assert HardwareProfile is SharedHardwareProfile
assert init_project is shared_init_project
assert update_config_yaml_model is shared_model_writer
assert strip_ansi is shared_strip_ansi
assert render_unified_diff is shared_render_diff
for optional in (False, True):
    bundle = bundled_skills_dir(optional)
    assert bundle.is_relative_to(package)
    assert list(bundle.rglob('SKILL.md'))
    assert list(bundle.rglob('*.py'))
    assert list(bundle.rglob('*.sh'))
print(package)
"""
        result = subprocess.run(
            [str(VENV_PYTHON), "-c", code], cwd=str(self.ws), env=self._env(),
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), str((self.site / "harness_core").resolve()))

    def test_install_first_task_upgrade_uninstall(self) -> None:
        native = self._install_native()
        (self.home / ".hex").mkdir(parents=True, exist_ok=True)
        (self.home / ".hex" / "config.yaml").write_text("model: {}\n", encoding="utf-8")
        local_bin = self.home / ".local" / "bin"
        local_bin.mkdir(parents=True, exist_ok=True)
        (local_bin / "hex-tui").symlink_to(native)

        # Documented first task: installed wheel + artifact, outside the
        # checkout, isolated home, PATH without cargo or a pre-existing hex.
        first = self._run_hex(["tui", "--check"])
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertIn(str(self.site), first.stdout + first.stderr + self.pythonpath)

        # Upgrade: reinstall the wheel and replace the native artifact.
        upgrade = subprocess.run(
            [str(VENV_PYTHON), "-m", "pip", "install", "--no-deps", "--no-index",
             "--upgrade", "--target", str(self.site), str(self.wheel)],
            capture_output=True, text=True,
        )
        self.assertEqual(upgrade.returncode, 0, upgrade.stderr[-400:])
        self._install_native()
        again = self._run_hex(["tui", "--check"])
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)

        # Uninstall removes native program files but preserves user data.
        uninst = self._run_hex(["uninstall", "--yes", "--keep-data"])
        self.assertEqual(uninst.returncode, 0, uninst.stdout + uninst.stderr)
        self.assertFalse(native.exists(), "uninstall must remove the native binary")
        self.assertFalse((local_bin / "hex-tui").exists())
        self.assertTrue((self.home / ".hex" / "config.yaml").is_file())

        # After uninstall the launcher finds nothing and reports honestly.
        probe = subprocess.run(
            [str(VENV_PYTHON), "-c",
             "from harness_core.client.tui_launcher import find_hex_tui_binary; "
             "print(find_hex_tui_binary())"],
            cwd=str(self.ws), env=self._env(), capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(probe.stdout.strip(), "None")


if __name__ == "__main__":
    unittest.main()