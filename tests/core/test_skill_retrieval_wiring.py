"""4.5: Hybrid skill retrieval is wired into orchestrator context assembly.

The HybridLearningRetriever previously had no production caller; the orchestrator
only loaded a whole-skill index. These tests lock in that run_agent_loop retrieves
goal-relevant skills and injects them into the system prompt (via the scanned
extra_rules slot), without modifying cognition/prompt_builder.py.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness_core.core.orchestrator import HarnessOrchestrator


def _write_skill(ws: Path, name: str, description: str, body: str) -> None:
    skill_dir = ws / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}\n",
        encoding="utf-8",
    )


class TestSkillRetrievalWiring(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="skill_retrieval_")
        self.ws = Path(self.temp_dir) / "workspace"
        self.ws.mkdir(parents=True, exist_ok=True)
        self.data_dir = Path(self.temp_dir) / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        _write_skill(
            self.ws,
            "jwt-auth-validator",
            "Validate and refresh expired JWT authentication tokens.",
            "# JWT Auth Validator\nUse when authentication tokens expire or fail validation.",
        )
        _write_skill(
            self.ws,
            "docker-deploy",
            "Deploy containers with docker compose.",
            "# Docker Deploy\nBuild and ship container images to a registry.",
        )
        self.orch = HarnessOrchestrator(
            data_dir=self.data_dir,
            default_workspace=self.ws,
        )

    def tearDown(self) -> None:
        try:
            self.orch.shutdown()
        except Exception:
            pass
        try:
            self.orch.ledger.close()
        except Exception:
            pass
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_retrieve_task_skills_returns_relevant_skill(self) -> None:
        block = self.orch.retrieve_task_skills(
            "Validate JWT authentication tokens", workspace_root=self.ws
        )
        self.assertIn("<retrieved_skills>", block)
        self.assertIn("jwt-auth-validator", block)
        self.assertIn("</retrieved_skills>", block)

    def test_retrieve_task_skills_excludes_irrelevant_skill(self) -> None:
        block = self.orch.retrieve_task_skills(
            "Validate JWT authentication tokens", workspace_root=self.ws
        )
        self.assertNotIn("docker-deploy", block)

    def test_retrieve_task_skills_empty_when_no_skills(self) -> None:
        empty_ws = Path(self.temp_dir) / "empty"
        empty_ws.mkdir(parents=True, exist_ok=True)
        block = self.orch.retrieve_task_skills("Fix JWT authentication", workspace_root=empty_ws)
        self.assertEqual(block, "")

    def test_retrieve_task_skills_empty_goal(self) -> None:
        self.assertEqual(self.orch.retrieve_task_skills("", workspace_root=self.ws), "")
        self.assertEqual(self.orch.retrieve_task_skills("   ", workspace_root=self.ws), "")

    def test_retrieve_task_skills_nonfatal_on_error(self) -> None:
        with patch.object(
            self.orch, "_get_skill_retriever", side_effect=RuntimeError("index unavailable")
        ):
            block = self.orch.retrieve_task_skills("Fix JWT authentication", workspace_root=self.ws)
        self.assertEqual(block, "")

    def _run_loop_capturing_prompt(self, goal: str, extra_rules: str = "") -> dict:
        captured: dict = {}
        orig = self.orch.prompt_builder.build_system_prompt

        def spy(**kwargs):
            captured.update(kwargs)
            return orig(**kwargs)

        mock_result = MagicMock(
            success=False,
            friction_info={},
            retry_count=0,
            final_answer="",
            total_turns=1,
            termination_reason="max_turns",
            turns=[],
            compaction_result=None,
        )
        with patch.object(self.orch.prompt_builder, "build_system_prompt", side_effect=spy), \
                patch("harness_core.core.react_loop.ReActLoop.run", return_value=mock_result):
            self.orch.run_agent_loop(
                goal=goal,
                workspace_root=self.ws,
                max_turns=1,
                extra_rules=extra_rules,
            )
        return captured

    def test_run_agent_loop_injects_retrieved_skills(self) -> None:
        captured = self._run_loop_capturing_prompt("Validate JWT authentication tokens")
        rules = captured.get("extra_rules", "")
        self.assertIn("<retrieved_skills>", rules)
        self.assertIn("jwt-auth-validator", rules)

    def test_run_agent_loop_preserves_caller_extra_rules(self) -> None:
        captured = self._run_loop_capturing_prompt(
            "Validate JWT authentication tokens", extra_rules="Custom rule XYZ"
        )
        rules = captured.get("extra_rules", "")
        self.assertIn("Custom rule XYZ", rules)
        self.assertIn("<retrieved_skills>", rules)


if __name__ == "__main__":
    unittest.main()