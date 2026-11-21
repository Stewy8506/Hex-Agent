"""B64 regression: the resolved identity must propagate into the guidance slot,
and the dead Developer DNA / distribution-provenance surface must be removed.

Before B64 the Slot #3 capabilities guidance hardcoded "You are HEX" (module-level
``HEX_CAPABILITIES_GUIDANCE``), so a renamed or workspace-custom persona never
reached the guidance block. ``load_developer_dna`` and the REPOSITORY /
REMOTE_PROFILE / DISTRIBUTION provenance members (plus the
``is_distribution_profile`` / ``soul_provenance`` params) had zero production
callers — the memory system already surfaces USER.md via the frozen snapshot.
"""

from __future__ import annotations

import inspect
import shutil
import tempfile
import unittest
from pathlib import Path

from harness_core.cognition.memory import MemorySnapshot
from harness_core.cognition.soul import SoulManager, SoulProvenance


class TestIdentityPropagation(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="hex_identity_prop_"))
        self.hex_home = self.tmp_dir / ".hex"
        self.hex_home.mkdir(parents=True, exist_ok=True)
        self.workspace = self.tmp_dir / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        MemorySnapshot.clear_all_snapshots()

    def tearDown(self) -> None:
        MemorySnapshot.clear_all_snapshots()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _build_prompt(self, soul_text: str | None) -> str:
        from harness_core.cognition.prompt_builder import PromptBuilder

        if soul_text is not None:
            (self.hex_home / "SOUL.md").write_text(soul_text, encoding="utf-8")
        soul_mgr = SoulManager(global_home=self.hex_home)
        builder = PromptBuilder(soul_manager=soul_mgr)
        return builder.build_system_prompt(workspace_root=self.workspace, mode="agent")

    def test_capabilities_guidance_uses_renamed_identity(self) -> None:
        """B64 core: a renamed persona reaches the Slot #3 guidance block."""
        from harness_core.cognition.prompt_builder import build_capabilities_guidance

        # Unit: the guidance builder carries any identity it is given.
        self.assertIn(
            "You are ATLAS, an autonomous AI agent and pairing partner",
            build_capabilities_guidance("ATLAS"),
        )

        # Integration: a custom global SOUL persona reaches the built prompt's guidance slot.
        prompt = self._build_prompt("You are ATLAS, a meticulous autonomous repair specialist.")
        self.assertIn("[Capabilities & Interaction Guidance]\nYou are ATLAS", prompt)

    def test_guidance_slot_uses_default_identity(self) -> None:
        """Guard: without a custom soul the guidance still reads as the default HEX identity."""
        prompt = self._build_prompt(None)
        self.assertIn(
            "[Capabilities & Interaction Guidance]\nYou are HEX, an autonomous AI agent",
            prompt,
        )

    def test_dead_dna_and_distribution_provenance_removed(self) -> None:
        """B64 cleanup: dead DNA accessor, provenance members, and params are gone."""
        from harness_core.cognition.prompt_builder import PromptBuilder

        self.assertFalse(hasattr(SoulManager, "load_developer_dna"))
        self.assertFalse(hasattr(SoulProvenance, "REPOSITORY"))
        self.assertFalse(hasattr(SoulProvenance, "REMOTE_PROFILE"))
        self.assertFalse(hasattr(SoulProvenance, "DISTRIBUTION"))
        params = inspect.signature(PromptBuilder.build_system_prompt).parameters
        self.assertNotIn("is_distribution_profile", params)
        self.assertNotIn("soul_provenance", params)

    def test_user_md_still_seeded_for_memory_snapshot(self) -> None:
        """Guard: removing load_developer_dna must not break USER.md seeding — the
        frozen memory snapshot (Slot #4) reads USER.md directly."""
        SoulManager(global_home=self.hex_home)
        user_md = self.hex_home / "USER.md"
        self.assertTrue(user_md.exists())
        self.assertIn("Developer Profile & Conventions", user_md.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
