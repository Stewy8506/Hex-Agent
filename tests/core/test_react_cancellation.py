"""B47 regression: effective cancellation of the ReAct turn loop.

A canceled task must stop producing side effects at the next execution
boundary (between turns and before each tool call), and late finalization
must never overwrite an already-terminal CANCELED state with a manufactured
SUCCEEDED/FAILED outcome (terminal-state CAS).
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from harness_core.core.orchestrator import HarnessOrchestrator
from harness_core.core.react_loop import ReActConfig, ReActLoop
from harness_core.gateway import RiskClass, ToolGateway, ToolSpec
from harness_core.storage.db import LedgerStore


def _probe(tool_id: str, effects: list) -> ToolSpec:
    def handler(ws: Path, args: dict) -> dict:
        effects.append(tool_id)
        return {"ok": True}

    return ToolSpec(tool_id=tool_id, description="cancel probe", risk_class=RiskClass.R0, handler=handler)


def _tool_response(name: str, args: dict | None = None) -> dict:
    return {"thought": "probe", "tool_calls": [{"name": name, "arguments": args or {}}]}


class TestReActLoopCancellation(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.ws = Path(self.temp_dir) / "workspace"
        self.ws.mkdir(parents=True)
        self.effects: list = []
        self.ledger = LedgerStore(db_path=Path(self.temp_dir) / "ledger.db")
        self.gateway = ToolGateway(ledger=self.ledger)
        self.gateway.register_tool(_probe("probe", self.effects))

    def tearDown(self) -> None:
        self.ledger.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_cancel_between_turns_stops_model_and_tools(self) -> None:
        state = {"calls": 0}

        def model(messages: list[dict]):
            state["calls"] += 1
            return _tool_response("probe")

        loop = ReActLoop(
            model_caller=model,
            gateway=self.gateway,
            workspace_root=self.ws,
            config=ReActConfig(max_turns=10),
            ledger=self.ledger,
            # Cancel once turn 1's tool has run: turn 2 must never start.
            cancel_check=lambda: len(self.effects) >= 1,
        )
        loop.title_synthesizer = None
        session = loop.run("keep probing", task_id="task_between")

        self.assertFalse(session.success)
        self.assertEqual(session.termination_reason, "canceled")
        self.assertEqual(state["calls"], 1)
        self.assertEqual(self.effects, ["probe"])

    def test_cancel_before_first_turn_zero_side_effects(self) -> None:
        loop = ReActLoop(
            model_caller=lambda messages: _tool_response("probe"),
            gateway=self.gateway,
            workspace_root=self.ws,
            config=ReActConfig(max_turns=10),
            ledger=self.ledger,
            cancel_check=lambda: True,
        )
        loop.title_synthesizer = None
        session = loop.run("already canceled", task_id="task_pre")

        self.assertFalse(session.success)
        self.assertEqual(session.termination_reason, "canceled")
        self.assertEqual(session.total_turns, 0)
        self.assertEqual(self.effects, [])

    def test_cancel_mid_turn_skips_remaining_tool_calls(self) -> None:
        executed: list = []
        flags = {"cancel": False}

        def handler(ws: Path, args: dict) -> dict:
            executed.append(args.get("tag"))
            flags["cancel"] = True
            return {"ok": True}

        self.gateway.register_tool(
            ToolSpec(tool_id="multi", description="", risk_class=RiskClass.R0, handler=handler)
        )

        def model(messages: list[dict]):
            return {
                "thought": "two calls",
                "tool_calls": [
                    {"name": "multi", "arguments": {"tag": "a"}},
                    {"name": "multi", "arguments": {"tag": "b"}},
                ],
            }

        loop = ReActLoop(
            model_caller=model,
            gateway=self.gateway,
            workspace_root=self.ws,
            config=ReActConfig(max_turns=5),
            ledger=self.ledger,
            cancel_check=lambda: flags["cancel"],
        )
        loop.title_synthesizer = None
        session = loop.run("two calls", task_id="task_mid")

        self.assertEqual(session.termination_reason, "canceled")
        self.assertEqual(executed, ["a"])

    def test_no_cancel_check_runs_to_completion(self) -> None:
        state = {"calls": 0}

        def model(messages: list[dict]):
            state["calls"] += 1
            if state["calls"] == 1:
                return _tool_response("probe")
            return "All done."

        loop = ReActLoop(
            model_caller=model,
            gateway=self.gateway,
            workspace_root=self.ws,
            config=ReActConfig(max_turns=5),
            ledger=self.ledger,
        )
        loop.title_synthesizer = None
        session = loop.run("finish naturally", task_id="task_control")

        self.assertTrue(session.success)
        self.assertEqual(session.termination_reason, "no_tool_calls")
        self.assertEqual(self.effects, ["probe"])

    def test_cancel_check_exception_does_not_crash_loop(self) -> None:
        def bad_check() -> bool:
            raise RuntimeError("probe failure")

        state = {"calls": 0}

        def model(messages: list[dict]):
            state["calls"] += 1
            if state["calls"] == 1:
                return _tool_response("probe")
            return "All done."

        loop = ReActLoop(
            model_caller=model,
            gateway=self.gateway,
            workspace_root=self.ws,
            config=ReActConfig(max_turns=5),
            ledger=self.ledger,
            cancel_check=bad_check,
        )
        loop.title_synthesizer = None
        session = loop.run("resilient", task_id="task_badcheck")

        self.assertTrue(session.success)
        self.assertEqual(session.termination_reason, "no_tool_calls")


class TestOrchestratorCancelFinalization(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.ws = Path(self.temp_dir) / "workspace"
        self.ws.mkdir(parents=True)
        self.orch = HarnessOrchestrator(
            data_dir=Path(self.temp_dir) / "data",
            default_workspace=self.ws,
        )
        # Force the deterministic tool planner (no cloud router / MoA) so the
        # goal reliably selects the registered probe tool.
        self.orch.router = None
        self.orch.moa_config = None

    def tearDown(self) -> None:
        self.orch.ledger.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_finalization_cannot_overwrite_canceled(self) -> None:
        """A tool that cancels its own task mid-run leaves the ledger CANCELED.

        Late finalization must not transition the task to SUCCEEDED/FAILED.
        """
        captured: dict = {}
        effects: list = []
        orig_submit = self.orch.ledger.submit_task

        def spy_submit(req):
            rec = orig_submit(req)
            if isinstance(rec, dict) and rec.get("task_id"):
                captured["task_id"] = rec["task_id"]
            return rec

        self.orch.ledger.submit_task = spy_submit

        def cancelling_handler(ws: Path, args: dict) -> dict:
            effects.append("ran")
            self.orch.cancel_task(captured["task_id"], actor="test")
            return {"ok": True}

        self.orch.gateway.register_tool(
            ToolSpec(
                tool_id="probe_cancel",
                description="cancels its task",
                risk_class=RiskClass.R0,
                handler=cancelling_handler,
            )
        )

        result = self.orch.run_agent_loop(goal="probe_cancel the system", max_turns=5)

        task_id = captured["task_id"]
        task = self.orch.ledger.get_task(task_id)
        self.assertEqual(task["status"], "CANCELED")
        self.assertFalse(result["success"])
        self.assertEqual(result["termination_reason"], "canceled")
        self.assertEqual(effects, ["ran"])


if __name__ == "__main__":
    unittest.main()