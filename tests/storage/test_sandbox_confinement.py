"""B04 regression: sandbox honesty and fail-closed confinement.

The restricted_subprocess fallback is trusted local execution, not a
containment boundary. Results and policy surfaces must say so, and
require_confinement must refuse to fall back when no real containment
backend exists.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness_core.gateway import ToolGateway
from harness_core.storage.db import LedgerStore
from harness_core.storage.sandbox import (
    BubblewrapSandbox,
    ConfinementUnavailableError,
    DockerPodmanSandbox,
    RestrictedSubprocessSandbox,
    SandboxResult,
    get_default_sandbox,
)


class TestTrustedLocalHonesty(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.ws = Path(self.temp_dir) / "workspace"
        self.ws.mkdir()
        self.sentinel = Path(self.temp_dir) / "outside-sentinel.txt"
        self.sentinel.write_text("original", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_sibling_write_is_labeled_trusted_local_not_confined(self) -> None:
        """The fallback cannot stop sibling writes; it must not claim confinement."""
        sandbox = RestrictedSubprocessSandbox(allowed_root=self.ws)
        self.assertFalse(sandbox.provides_confinement)

        result = sandbox.execute(
            cmd=[
                sys.executable,
                "-c",
                "from pathlib import Path; Path('../outside-sentinel.txt').write_text('changed')",
            ],
            cwd=self.ws,
        )
        # Documents actual authority: the write happens with a valid cwd.
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(self.sentinel.read_text(encoding="utf-8"), "changed")
        # Honesty: the result must label this as unconfined execution.
        self.assertEqual(result.sandbox_backend, "restricted_subprocess")
        self.assertFalse(result.confined)

    def test_confined_backends_mark_results_confined(self) -> None:
        captured: list[dict] = []

        def fake_run(**kwargs):
            captured.append(kwargs)
            return SandboxResult(exit_code=0, stdout="", stderr="", sandbox_backend=kwargs["backend_name"])

        with patch("harness_core.storage.sandbox._run_confined_subprocess", side_effect=fake_run):
            BubblewrapSandbox().execute(cmd=["true"], cwd=self.ws)
            DockerPodmanSandbox().execute(cmd=["true"], cwd=self.ws)
        self.assertEqual(len(captured), 2)
        self.assertTrue(all(k["confined"] is True for k in captured))

        real = RestrictedSubprocessSandbox().execute(cmd=[sys.executable, "-c", "pass"], cwd=self.ws)
        self.assertFalse(real.confined)


class TestFailClosedConfinement(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        os.environ.pop("HEX_REQUIRE_CONFINEMENT", None)

    def _no_backends(self):
        return (
            patch.object(BubblewrapSandbox, "is_available", lambda: False),
            patch.object(DockerPodmanSandbox, "is_available", lambda: False),
        )

    def test_require_confinement_refuses_fallback(self) -> None:
        p1, p2 = self._no_backends()
        with p1, p2:
            with self.assertRaises(ConfinementUnavailableError):
                get_default_sandbox(self.temp_dir, require_confinement=True)

    def test_env_var_require_confinement_refuses_fallback(self) -> None:
        p1, p2 = self._no_backends()
        with p1, p2, patch.dict(os.environ, {"HEX_REQUIRE_CONFINEMENT": "1"}):
            with self.assertRaises(ConfinementUnavailableError):
                get_default_sandbox(self.temp_dir)

    def test_require_confinement_selects_confined_backend_when_available(self) -> None:
        with patch.object(BubblewrapSandbox, "is_available", lambda: True):
            sandbox = get_default_sandbox(self.temp_dir, require_confinement=True)
        self.assertIsInstance(sandbox, BubblewrapSandbox)
        self.assertTrue(sandbox.provides_confinement)

    def test_fallback_still_allowed_without_confinement_requirement(self) -> None:
        p1, p2 = self._no_backends()
        with p1, p2:
            sandbox = get_default_sandbox(self.temp_dir)
        self.assertIsInstance(sandbox, RestrictedSubprocessSandbox)
        self.assertFalse(sandbox.provides_confinement)


class TestGatewayConfinementSurface(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.ws = Path(self.temp_dir) / "workspace"
        self.ws.mkdir()
        self.ledger = LedgerStore(db_path=Path(self.temp_dir) / "ledger.db")

    def tearDown(self) -> None:
        self.ledger.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_terminal_result_reports_unconfined_fallback(self) -> None:
        gateway = ToolGateway(ledger=self.ledger, sandbox=RestrictedSubprocessSandbox())
        result = gateway.tools["terminal"].handler(self.ws, {"command": "echo hi"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["sandbox_backend"], "restricted_subprocess")
        self.assertFalse(result["confined"])

    def test_terminal_result_reports_confined_backend(self) -> None:
        class FakeConfined:
            backend_name = "bubblewrap"
            provides_confinement = True

            def execute(self, cmd, cwd, env=None, timeout=30.0):
                return SandboxResult(
                    exit_code=0, stdout="hi", stderr="", sandbox_backend=self.backend_name, confined=True
                )

        gateway = ToolGateway(ledger=self.ledger, sandbox=FakeConfined())
        result = gateway.tools["terminal"].handler(self.ws, {"command": "echo hi"})
        self.assertTrue(result["ok"])
        self.assertTrue(result["confined"])


if __name__ == "__main__":
    unittest.main()