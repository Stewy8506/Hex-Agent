"""B03 regression: provider failure must not become fabricated ReAct success.

Inference unavailable (no router, no active provider, outage, empty response)
must yield an explicit failure. The deterministic planner is reachable only
under explicit demo mode, and even then a failed tool observation or a
zero-tool implementation request must not report verified success.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness_core.core.orchestrator import (
    HarnessOrchestrator,
    _goal_requires_action,
    _observation_indicates_failure,
)
from harness_core.gateway import RiskClass, ToolSpec


def _probe(tool_id: str, result: dict) -> ToolSpec:
    return ToolSpec(
        tool_id=tool_id,
        description="B03 probe",
        risk_class=RiskClass.R0,
        handler=lambda ws, args: dict(result),
    )


class _OrchestratorCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.ws = Path(self.temp_dir) / "workspace"
        self.ws.mkdir(parents=True)
        self.orch = HarnessOrchestrator(
            data_dir=Path(self.temp_dir) / "data",
            default_workspace=self.ws,
        )
        self.orch.moa_config = None

    def tearDown(self) -> None:
        self.orch.ledger.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _status(self, result: dict) -> str:
        return self.orch.ledger.get_task(result["task_id"])["status"]


class TestProviderFailureFailsClosed(_OrchestratorCase):
    def test_no_router_fails_without_demo(self) -> None:
        self.orch.router = None
        res = self.orch.run_agent_loop("Implement rate limiting for authentication", max_turns=4)
        self.assertFalse(res["success"])
        self.assertEqual(res["termination_reason"], "error")
        self.assertEqual(self._status(res), "FAILED")

    def test_cloud_unavailable_fails_without_demo(self) -> None:
        self.orch.router.cloud_provider = None
        res = self.orch.run_agent_loop("Implement rate limiting for authentication", max_turns=4)
        self.assertFalse(res["success"])
        self.assertEqual(res["termination_reason"], "error")

    def test_provider_outage_fails_without_demo(self) -> None:
        provider = MagicMock()
        provider.is_available.return_value = True
        with patch.object(self.orch.router, "cloud_provider", provider), patch.object(
            self.orch.router,
            "execute_routed_request",
            side_effect=RuntimeError("simulated outage"),
        ):
            res = self.orch.run_agent_loop("Implement rate limiting for authentication", max_turns=4)
        self.assertFalse(res["success"])
        self.assertEqual(res["termination_reason"], "error")
        self.assertEqual(self._status(res), "FAILED")

    def test_empty_provider_response_fails_without_demo(self) -> None:
        provider = MagicMock()
        provider.is_available.return_value = True
        with patch.object(self.orch.router, "cloud_provider", provider), patch.object(
            self.orch.router,
            "execute_routed_request",
            return_value=(MagicMock(), MagicMock(text="   ")),
        ):
            res = self.orch.run_agent_loop("Implement rate limiting for authentication", max_turns=4)
        self.assertFalse(res["success"])
        self.assertEqual(res["termination_reason"], "error")


class TestDemoModeOptIn(_OrchestratorCase):
    def test_explicit_demo_enables_deterministic_planner(self) -> None:
        self.orch.router = None
        self.orch.gateway.register_tool(_probe("probe_ok", {"ok": True}))
        res = self.orch.run_agent_loop("probe_ok please", max_turns=5, demo_mode=True)
        self.assertTrue(res["success"])
        self.assertEqual(self._status(res), "SUCCEEDED")

    def test_demo_param_overrides_instance_default(self) -> None:
        self.orch.router = None
        self.orch.demo_mode = False
        self.orch.gateway.register_tool(_probe("probe_ok", {"ok": True}))
        res = self.orch.run_agent_loop("probe_ok please", max_turns=5, demo_mode=True)
        self.assertTrue(res["success"])

    def test_env_var_enables_demo(self) -> None:
        self.orch.ledger.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.temp_dir = tempfile.mkdtemp()
        ws = Path(self.temp_dir) / "workspace"
        ws.mkdir(parents=True)
        with patch.dict(os.environ, {"HEX_DEMO_MODE": "1"}, clear=False):
            orch = HarnessOrchestrator(data_dir=Path(self.temp_dir) / "data", default_workspace=ws)
        try:
            orch.moa_config = None
            orch.router = None
            orch.gateway.register_tool(_probe("probe_ok", {"ok": True}))
            res = orch.run_agent_loop("probe_ok please", max_turns=5)
            self.assertTrue(res["success"])
        finally:
            orch.ledger.close()


class TestDemoModeHonesty(_OrchestratorCase):
    def setUp(self) -> None:
        super().setUp()
        self.orch.router = None
        self.orch.demo_mode = True

    def test_failed_observation_not_success(self) -> None:
        self.orch.gateway.register_tool(_probe("probe_fail", {"ok": False, "exit_code": 1}))
        res = self.orch.run_agent_loop("probe_fail check", max_turns=5)
        self.assertFalse(res["success"])
        self.assertEqual(res["termination_reason"], "error")
        self.assertEqual(self._status(res), "FAILED")

    def test_successful_observation_completes(self) -> None:
        self.orch.gateway.register_tool(_probe("probe_ok", {"ok": True}))
        res = self.orch.run_agent_loop("probe_ok please", max_turns=5)
        self.assertTrue(res["success"])

    def test_zero_tool_implementation_not_success(self) -> None:
        res = self.orch.run_agent_loop("Implement rate limiting for the login flow", max_turns=5)
        self.assertFalse(res["success"])
        self.assertEqual(res["termination_reason"], "error")
        self.assertEqual(self._status(res), "FAILED")

    def test_conversational_goal_no_fabricated_completion(self) -> None:
        res = self.orch.run_agent_loop("Hello, what is your mission?", max_turns=5)
        self.assertNotIn("Completed goal", res["final_answer"] or "")


class TestFailureHelpers(unittest.TestCase):
    def test_observation_failure_detection(self) -> None:
        self.assertTrue(_observation_indicates_failure('{"ok": false, "exit_code": 1}'))
        self.assertTrue(_observation_indicates_failure('{"ok": true, "exit_code": 2}'))
        self.assertTrue(_observation_indicates_failure('{"timed_out": true}'))
        self.assertTrue(_observation_indicates_failure('{"violation_detected": true}'))
        self.assertFalse(_observation_indicates_failure('{"ok": true, "exit_code": 0}'))
        self.assertFalse(_observation_indicates_failure("plain text output"))
        self.assertFalse(_observation_indicates_failure(""))

    def test_goal_action_detection(self) -> None:
        self.assertTrue(_goal_requires_action("Implement rate limiting"))
        self.assertTrue(_goal_requires_action("fix the bug"))
        self.assertFalse(_goal_requires_action("Hello, what is your mission?"))
        self.assertFalse(_goal_requires_action("explain the architecture"))


if __name__ == "__main__":
    unittest.main()