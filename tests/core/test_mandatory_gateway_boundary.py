"""B02 regression: ToolGateway.invoke is the mandatory execution boundary.

ReAct dispatch (agent/react_loop.py) and ACP tools/call (hex_cli/acp.py) must
enforce the same allowlist / R3-approval / R4-denial policy as standard tasks.
A denied tool must produce zero side effects on every entry point.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from harness_core.client.acp import ACPServer
from harness_core.core.orchestrator import HarnessOrchestrator
from harness_core.core.react_loop import ReActConfig, ReActLoop
from harness_core.gateway import PolicyBlockedError, RiskClass, ToolGateway, ToolSpec
from harness_core.storage.db import LedgerStore


def _make_probe(tool_id: str, risk: RiskClass, marker: Path, effects: list) -> ToolSpec:
    def handler(ws: Path, args: dict) -> dict:
        effects.append((tool_id, dict(args)))
        marker.write_text("side effect happened", encoding="utf-8")
        return {"ok": True, "value": 42}

    return ToolSpec(tool_id=tool_id, description="policy probe", risk_class=risk, handler=handler)


def _probe_then_finish(tool_name: str):
    state = {"step": 0}

    def model(messages: list[dict]) -> str:
        if state["step"] == 0:
            state["step"] += 1
            return f'<tool_call>{{"name": "{tool_name}", "arguments": {{}}}}</tool_call>'
        return "<finish>done</finish>"

    return model


class TestReActGatewayBoundary(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.ws = Path(self.temp_dir) / "workspace"
        self.ws.mkdir(parents=True)
        self.marker = self.ws / "side_effect.txt"
        self.effects: list = []
        self.ledger = LedgerStore(db_path=Path(self.temp_dir) / "ledger.db")
        self.gateway = ToolGateway(ledger=self.ledger)

    def tearDown(self) -> None:
        self.ledger.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run_loop(self, tool_name: str, task_id: str = "task_react") -> tuple:
        loop = ReActLoop(
            model_caller=_probe_then_finish(tool_name),
            gateway=self.gateway,
            workspace_root=self.ws,
            config=ReActConfig(max_turns=5),
            ledger=self.ledger,
        )
        session = loop.run("probe the boundary", task_id=task_id)
        return loop, session

    def test_react_r4_denied_zero_side_effects(self) -> None:
        self.gateway.register_tool(_make_probe("probe_r4", RiskClass.R4, self.marker, self.effects))
        loop, session = self._run_loop("probe_r4")

        self.assertEqual(self.effects, [])
        self.assertFalse(self.marker.exists())
        tool_res = session.turns[0].tool_results[0]
        self.assertTrue(tool_res.is_error)
        self.assertIn("Denied by gateway policy", str(tool_res.output))

        resolved = loop._gateway_task_ids["task_react"]
        calls = self.ledger.list_tool_calls(resolved)
        self.assertTrue(any(c["status"] == "DENIED" for c in calls))

    def test_react_allowlist_denies_unlisted_tool(self) -> None:
        self.gateway.register_tool(_make_probe("probe_r1", RiskClass.R1, self.marker, self.effects))
        task = self.ledger.submit_task({
            "goal": "allowlist parity",
            "constraints": {"allowed_tools": ["read_file"]},
        })
        _, session = self._run_loop("probe_r1", task_id=task["task_id"])

        self.assertEqual(self.effects, [])
        self.assertFalse(self.marker.exists())
        tool_res = session.turns[0].tool_results[0]
        self.assertTrue(tool_res.is_error)
        self.assertIn("allowed_tools", str(tool_res.output))

    def test_react_r3_requires_approval_zero_side_effects(self) -> None:
        self.gateway.register_tool(_make_probe("probe_r3", RiskClass.R3, self.marker, self.effects))
        loop, session = self._run_loop("probe_r3")

        self.assertEqual(self.effects, [])
        self.assertFalse(self.marker.exists())
        tool_res = session.turns[0].tool_results[0]
        self.assertTrue(tool_res.is_error)
        self.assertIn("approval", str(tool_res.output).lower())

        resolved = loop._gateway_task_ids["task_react"]
        approvals = self.ledger.list_approvals(task_id=resolved)
        self.assertTrue(approvals)
        self.assertEqual(approvals[0]["decision"], "PENDING")

    def test_react_allowed_tool_executes_through_invoke_journal(self) -> None:
        self.gateway.register_tool(_make_probe("probe_r0", RiskClass.R0, self.marker, self.effects))
        _, session = self._run_loop("probe_r0")

        self.assertEqual(len(self.effects), 1)
        tool_res = session.turns[0].tool_results[0]
        self.assertFalse(tool_res.is_error)
        # invoke() stamps journal metadata on every result; direct handler
        # dispatch never does.
        self.assertIn("_call_id", tool_res.output)
        self.assertEqual(tool_res.output.get("value"), 42)

    def test_policy_parity_with_standard_invoke(self) -> None:
        """The standard-task path (direct invoke) denies identically."""
        self.gateway.register_tool(_make_probe("probe_r4", RiskClass.R4, self.marker, self.effects))
        task = self.ledger.submit_task({"goal": "standard parity"})
        with self.assertRaises(PolicyBlockedError):
            self.gateway.invoke(
                task_id=task["task_id"],
                attempt_id="att_1",
                step_id="step_1",
                sequence_in_step=0,
                tool_id="probe_r4",
                args={},
                workspace_dir=self.ws,
            )
        self.assertEqual(self.effects, [])
        self.assertFalse(self.marker.exists())


class TestACPGatewayBoundary(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.ws = Path(self.temp_dir) / "workspace"
        self.ws.mkdir(parents=True)
        (self.ws / "index.py").write_text("print('hello acp')\n", encoding="utf-8")
        self.marker = self.ws / "side_effect.txt"
        self.effects: list = []
        self.orchestrator = HarnessOrchestrator(
            data_dir=Path(self.temp_dir) / "data",
            default_workspace=self.ws,
        )
        self.server = ACPServer(orchestrator=self.orchestrator, workspace_root=self.ws)

    def tearDown(self) -> None:
        self.orchestrator.ledger.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _call(self, name: str) -> dict:
        resp = self.server.handle_request({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": {}},
        })
        self.assertIn("result", resp)
        return resp["result"]

    def test_acp_r4_denied_zero_side_effects(self) -> None:
        self.orchestrator.gateway.register_tool(
            _make_probe("probe_r4", RiskClass.R4, self.marker, self.effects)
        )
        result = self._call("probe_r4")

        self.assertEqual(self.effects, [])
        self.assertFalse(self.marker.exists())
        self.assertTrue(result.get("isError"))
        self.assertIn("Denied by gateway policy", result["content"][0]["text"])

    def test_acp_r3_requires_approval_zero_side_effects(self) -> None:
        self.orchestrator.gateway.register_tool(
            _make_probe("probe_r3", RiskClass.R3, self.marker, self.effects)
        )
        # Exercise the interrupt path: bridge reports TIMEOUT (no operator).
        from types import SimpleNamespace

        class TimeoutBridge:
            def request_approval(self, req):
                return SimpleNamespace(decision="TIMEOUT", platform="test", responder_id=None)

        self.server.orchestrator.gateway.approval_bridge = TimeoutBridge()
        result = self._call("probe_r3")

        self.assertEqual(self.effects, [])
        self.assertFalse(self.marker.exists())
        self.assertTrue(result.get("isError"))
        self.assertIn("approvalId", result["content"][0]["text"])

    def test_acp_r3_remote_denial_zero_side_effects(self) -> None:
        from types import SimpleNamespace

        self.orchestrator.gateway.register_tool(
            _make_probe("probe_r3", RiskClass.R3, self.marker, self.effects)
        )

        class DenyBridge:
            def request_approval(self, req):
                return SimpleNamespace(
                    decision="DENY", reason="operator said no", platform="test", responder_id="op"
                )

        self.server.orchestrator.gateway.approval_bridge = DenyBridge()
        result = self._call("probe_r3")

        self.assertEqual(self.effects, [])
        self.assertFalse(self.marker.exists())
        self.assertTrue(result.get("isError"))
        self.assertIn("denied", result["content"][0]["text"].lower())

    def test_acp_allowed_tool_still_executes(self) -> None:
        result = self.server.handle_request({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"path": "index.py"}},
        })["result"]
        self.assertFalse(result.get("isError"))
        self.assertIn("hello acp", result["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()