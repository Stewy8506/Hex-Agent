"""B58 regression: the standard-DAG repair model call must be grounded in the
resolved SOUL persona + frozen curated memory, not a hardcoded "You are HEX".

The only model inference on the deterministic standard-DAG path is
``HarnessOrchestrator.generate_repair_patch`` (the autonomous repair loop).
Before B58 it built a hardcoded identity prompt, so a workspace's custom SOUL
persona and frozen curated memory never reached the repair model — unlike the
ReAct and fast-path prompts which assemble the full 5-slot system prompt.

These tests drive ``generate_repair_patch`` directly with a mocked router that
captures the ``system_prompt`` kwarg, proving:
  1. The resolved workspace SOUL persona reaches the repair system prompt.
  2. The frozen curated memory note reaches the repair system prompt.
  3. Persona/memory resolve from the REAL source workspace even though the CoW
     attempt clone excludes ``.hex`` (so attempt_ws has no persona of its own).
  4. The repair-specific JSON write_file instructions are preserved.
  5. A non-fatal fallback to the hardcoded identity when prompt_builder is absent.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from harness_core.cognition.memory import CuratedMemoryStore, MemorySnapshot
from harness_core.core.models import TaskSubmitRequest
from harness_core.core.orchestrator import HarnessOrchestrator


PERSONA_TOKEN = "ATLAS"
MEMORY_TOKEN = "MIGRATION_RULE_42"


class TestRepairPersonaGrounding(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="hex_repair_persona_"))
        self.source_ws = self.tmp_dir / "source_repo"
        self.source_ws.mkdir(parents=True, exist_ok=True)
        # CoW-style attempt clone: deliberately WITHOUT .hex (persona/memory absent).
        self.attempt_ws = self.tmp_dir / "attempt_clone"
        self.attempt_ws.mkdir(parents=True, exist_ok=True)
        self.data_dir = self.tmp_dir / "harness_data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.global_home = self.tmp_dir / "global_home"
        self.global_home.mkdir(parents=True, exist_ok=True)
        MemorySnapshot.clear_all_snapshots()

        # Custom workspace persona + a curated project memory note.
        soul_dir = self.source_ws / ".hex"
        soul_dir.mkdir(parents=True, exist_ok=True)
        (soul_dir / "SOUL.md").write_text(
            f"You are {PERSONA_TOKEN}, a meticulous autonomous repair specialist.",
            encoding="utf-8",
        )
        CuratedMemoryStore(workspace_root=self.source_ws, global_home=self.global_home).add(
            f"{MEMORY_TOKEN}: always verify database migrations in both directions.",
            target="memory",
        )

        # Broken calculator + test live in the attempt clone (the repair target).
        (self.attempt_ws / "calculator.py").write_text(
            "def add(a: int, b: int) -> int:\n    return a - b  # bug\n",
            encoding="utf-8",
        )
        (self.attempt_ws / "test_calculator.py").write_text(
            "import unittest\nfrom calculator import add\n\n"
            "class TestCalculator(unittest.TestCase):\n"
            "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n",
            encoding="utf-8",
        )
        self.orch: HarnessOrchestrator | None = None

    def tearDown(self) -> None:
        if self.orch is not None:
            self.orch.shutdown()
        MemorySnapshot.clear_all_snapshots()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _build_orchestrator(self) -> HarnessOrchestrator:
        orch = HarnessOrchestrator(
            data_dir=self.data_dir,
            default_workspace=self.source_ws,
        )
        # Hermetic global home so the frozen snapshot never reads real user memory.
        orch.soul_manager.global_home = self.global_home
        # Capture the repair system prompt; return a valid write_file JSON patch.
        orch.router = MagicMock()
        mock_resp = MagicMock()
        mock_resp.text = (
            "```json\n"
            '[{"tool_id": "write_file", "args": {"path": "calculator.py", '
            '"content": "def add(a: int, b: int) -> int:\\n    return a + b\\n"}}]\n'
            "```"
        )
        orch.router.execute_routed_request.return_value = (MagicMock(), mock_resp)
        self.orch = orch
        return orch

    def _create_task(self, orch: HarnessOrchestrator) -> str:
        task = orch.ledger.create_task(
            TaskSubmitRequest(
                goal="Repair calculator addition bug",
                execution_mode="standard",
                workspace_root=str(self.source_ws),
            )
        )
        return task["task_id"]

    def _run_repair(self, orch: HarnessOrchestrator, task_id: str) -> tuple[str, list[dict[str, Any]]]:
        blast_report = SimpleNamespace(
            to_dict=lambda: {
                "modified_symbols": ["add"],
                "hop1_dependents": [],
                "warning_message": "add() is referenced by test_calculator.py",
            }
        )
        ops = orch.generate_repair_patch(
            task_id=task_id,
            attempt_ws=self.attempt_ws,
            failure_digest="AssertionError: 5 != -1 in test_calculator.py (calculator.py)",
            blast_report=blast_report,
            modified_files=["calculator.py"],
        )
        kwargs = orch.router.execute_routed_request.call_args.kwargs
        return kwargs["system_prompt"], ops

    def test_repair_prompt_carries_workspace_persona_and_frozen_memory(self) -> None:
        orch = self._build_orchestrator()
        task_id = self._create_task(orch)

        # The attempt clone genuinely lacks .hex, so persona cannot come from it.
        self.assertFalse((self.attempt_ws / ".hex").exists())

        system_prompt, ops = self._run_repair(orch, task_id)

        # 1. Resolved workspace SOUL persona reaches the repair model (B58 core).
        self.assertIn(PERSONA_TOKEN, system_prompt)
        # 2. Frozen curated memory note reaches the repair model.
        self.assertIn(MEMORY_TOKEN, system_prompt)
        self.assertIn("<memory-context>", system_prompt)
        # 3. Repair-specific JSON write_file instructions are preserved.
        self.assertIn("[Repair Task]", system_prompt)
        self.assertIn("write_file", system_prompt)
        # 4. The patch still parses end-to-end.
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["tool_id"], "write_file")
        self.assertIn("return a + b", ops[0]["args"]["content"])

    def test_repair_prompt_falls_back_when_prompt_builder_absent(self) -> None:
        orch = self._build_orchestrator()
        task_id = self._create_task(orch)
        # Simulate an orchestrator without a prompt builder: non-fatal fallback.
        orch.prompt_builder = None

        system_prompt, ops = self._run_repair(orch, task_id)

        self.assertIn("You are HEX", system_prompt)
        self.assertIn("write_file", system_prompt)
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["tool_id"], "write_file")


if __name__ == "__main__":
    unittest.main()