"""B10/B13 regression: real child tool loop and bounded spawn concurrency.

B10: a CODER/TESTER child must be able to edit and run tools through the shared
gateway, while the role tool subset is enforced *before* dispatch so a
RESEARCHER/TESTER child cannot mutate the workspace. B13: depth is server-owned
and async spawns are bounded rather than creating unbounded daemon threads.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from harness_core.core.subagent import (
    MaxRecursionDepthExceededError,
    SubagentConcurrencyError,
    SubagentManager,
    SubagentRole,
)
from harness_core.gateway import ToolGateway
from harness_core.storage.cow import get_default_snapshot_provider
from harness_core.storage.db import LedgerStore


class ScriptedRouter:
    """Router that replays a scripted sequence of child turns."""

    def __init__(self, responses: list[str], delay: float = 0.0) -> None:
        self.responses = list(responses)
        self.delay = delay
        self.calls = 0
        self.prompts: list[str] = []

    def execute_routed_request(self, **kwargs):
        self.prompts.append(kwargs.get("prompt", ""))
        if self.delay:
            time.sleep(self.delay)
        idx = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return None, SimpleNamespace(content=self.responses[idx])


def tool_call(name: str, **arguments) -> str:
    return f"<tool_call>{json.dumps({'name': name, 'arguments': arguments})}</tool_call>"


class TestSubagentToolLoop(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.ws = (Path(self.temp_dir) / "workspace").resolve()
        self.ws.mkdir(parents=True)
        (self.ws / "app.py").write_text("x = 1\n", encoding="utf-8")
        self.ledger = LedgerStore(Path(self.temp_dir) / "ledger.db")
        self.cow = get_default_snapshot_provider(Path(self.temp_dir) / "cow")
        self.gateway = ToolGateway(self.ledger)

    def tearDown(self) -> None:
        self.ledger.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _mgr(self, router=None, **kwargs) -> SubagentManager:
        mgr = SubagentManager(
            ledger=self.ledger,
            cow=self.cow,
            router=router,
            gateway=self.gateway,
            max_depth=2,
            **kwargs,
        )
        self.gateway.subagent_manager = mgr
        return mgr

    def test_coder_child_edits_workspace_through_gateway(self) -> None:
        router = ScriptedRouter(
            [
                tool_call("write_file", path="child_made.py", content="answer = 42\n"),
                "Created child_made.py with answer = 42.",
            ]
        )
        mgr = self._mgr(router)
        res = mgr.spawn_subagent(
            parent_task_id="tsk_b10",
            goal="create child_made.py",
            role=SubagentRole.CODER,
            parent_ws=self.ws,
            wait=True,
        )

        self.assertEqual(res.status, "completed")
        self.assertEqual(res.tool_calls_count, 1)
        self.assertIn("child_made.py", res.artifacts)
        self.assertEqual(res.findings, ["write_file: ok"])
        self.assertEqual(res.summary, "Created child_made.py with answer = 42.")

        record = mgr._active_subagents[res.subagent_id]
        self.assertTrue((record.workspace_path / "child_made.py").is_file())
        # The edit stays in the isolated child branch.
        self.assertFalse((self.ws / "child_made.py").exists())
        # The tool result is fed back to the child on its next turn.
        self.assertIn("Tool result (write_file, ok)", router.prompts[1])

    def test_researcher_role_denies_write_before_dispatch(self) -> None:
        router = ScriptedRouter(
            [
                tool_call("write_file", path="should_not_exist.py", content="x = 1\n"),
                "Write denied; reporting instead.",
            ]
        )
        mgr = self._mgr(router)
        res = mgr.spawn_subagent(
            parent_task_id="tsk_b10_denied",
            goal="write a file",
            role=SubagentRole.RESEARCHER,
            parent_ws=self.ws,
            wait=True,
        )

        self.assertEqual(res.status, "completed")
        self.assertEqual(res.tool_calls_count, 1)
        self.assertEqual(res.artifacts, [])
        self.assertEqual(res.findings, ["write_file: error"])
        record = mgr._active_subagents[res.subagent_id]
        self.assertFalse((record.workspace_path / "should_not_exist.py").exists())
        self.assertFalse((self.ws / "should_not_exist.py").exists())
        self.assertIn("Tool result (write_file, error)", router.prompts[1])
        self.assertIn("outside the researcher role tool subset", router.prompts[1])

    def test_child_cannot_delegate_recursively(self) -> None:
        router = ScriptedRouter(
            [
                tool_call("delegate", goal="spawn a grandchild"),
                "Delegation denied; finished.",
            ]
        )
        mgr = self._mgr(router)
        res = mgr.spawn_subagent(
            parent_task_id="tsk_b10_delegate",
            goal="delegate",
            role=SubagentRole.CODER,
            parent_ws=self.ws,
            wait=True,
        )

        self.assertEqual(res.status, "completed")
        self.assertEqual(res.findings, ["delegate: error"])
        # Only the child itself is registered; no grandchild was created.
        self.assertEqual(len(mgr.list_active_subagents()), 1)

    def test_gateway_policy_still_denies_child_tool(self) -> None:
        parent = self.ledger.submit_task(
            {
                "goal": "read-only parent",
                "execution_mode": "standard",
                "constraints": {"allowed_tools": ["read_file", "list_files"]},
            }
        )
        router = ScriptedRouter(
            [
                tool_call("write_file", path="policy_blocked.py", content="x = 1\n"),
                "Blocked by policy.",
            ]
        )
        mgr = self._mgr(router)
        res = mgr.spawn_subagent(
            parent_task_id=parent["task_id"],
            goal="write despite policy",
            role=SubagentRole.CODER,
            parent_ws=self.ws,
            wait=True,
        )

        self.assertEqual(res.status, "completed")
        self.assertEqual(res.findings, ["write_file: error"])
        record = mgr._active_subagents[res.subagent_id]
        self.assertFalse((record.workspace_path / "policy_blocked.py").exists())
        self.assertIn("Denied by gateway policy", router.prompts[1])

    def test_tool_loop_turn_bound_fails_closed(self) -> None:
        router = ScriptedRouter(
            [tool_call("read_file", path="app.py"), tool_call("read_file", path="app.py")]
        )
        mgr = self._mgr(router, max_child_tool_turns=2)
        res = mgr.spawn_subagent(
            parent_task_id="tsk_b10_turns",
            goal="loop forever",
            role=SubagentRole.RESEARCHER,
            parent_ws=self.ws,
            wait=True,
        )

        self.assertEqual(res.status, "failed")
        self.assertIn("tool-loop turns", res.error or "")

    def test_depth_is_server_owned_ignoring_caller_value(self) -> None:
        mgr = self._mgr(ScriptedRouter(["done"]))

        # A caller-supplied depth far beyond the cap cannot inflate the check.
        first = mgr.spawn_subagent(
            parent_task_id="tsk_depth",
            goal="level 1",
            role=SubagentRole.RESEARCHER,
            parent_ws=self.ws,
            current_depth=99,
            wait=True,
        )
        self.assertEqual(first.status, "completed")

        # Depth 2 is still allowed (trusted parent depth 1 -> 2).
        second = mgr.spawn_subagent(
            parent_task_id=first.subagent_id,
            goal="level 2",
            role=SubagentRole.RESEARCHER,
            parent_ws=self.ws,
            current_depth=0,
            wait=True,
        )
        self.assertEqual(second.status, "completed")

        # A third level is refused even though the caller claims depth 0.
        with self.assertRaises(MaxRecursionDepthExceededError):
            mgr.spawn_subagent(
                parent_task_id=second.subagent_id,
                goal="level 3",
                role=SubagentRole.RESEARCHER,
                parent_ws=self.ws,
                current_depth=0,
                wait=True,
            )

    def test_bounded_spawn_concurrency_releases_slots(self) -> None:
        mgr = self._mgr(
            ScriptedRouter(["done"], delay=0.5),
            max_concurrent=1,
            spawn_wait_seconds=0.05,
        )
        first = mgr.spawn_subagent(
            parent_task_id="tsk_conc_a",
            goal="slow child",
            role=SubagentRole.RESEARCHER,
            parent_ws=self.ws,
            wait=False,
        )
        self.assertIsInstance(first, str)

        with self.assertRaises(SubagentConcurrencyError):
            mgr.spawn_subagent(
                parent_task_id="tsk_conc_b",
                goal="rejected child",
                role=SubagentRole.RESEARCHER,
                parent_ws=self.ws,
                wait=True,
            )

        mgr.wait_subagent(first, timeout=5.0)
        # The slot is released when the child finishes.
        after = mgr.spawn_subagent(
            parent_task_id="tsk_conc_c",
            goal="after slot release",
            role=SubagentRole.RESEARCHER,
            parent_ws=self.ws,
            wait=True,
        )
        self.assertEqual(after.status, "completed")


if __name__ == "__main__":
    unittest.main()
