from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness_core.cognition.prompt_builder import build_skills_index
from harness_core.resources import bundled_skills_dir
from harness_core.skills import SkillCompiler, SkillExecutionEngine
from tests import REPO_ROOT


class TestBundledResources(unittest.TestCase):
    def test_bundles_are_separate_from_engine(self) -> None:
        for optional in (False, True):
            root = bundled_skills_dir(optional=optional)
            self.assertTrue(root.is_dir())
            expected = REPO_ROOT / "optional-skills" if optional else REPO_ROOT / "skills/builtin"
            self.assertEqual(root, expected)
            self.assertTrue(list(root.rglob("SKILL.md")))
        self.assertEqual(SkillCompiler.__module__, "harness_core.skills.compiler")
        self.assertEqual(SkillExecutionEngine.__module__, "harness_core.skills.executor")

    def test_bundle_paths_do_not_depend_on_working_directory(self) -> None:
        expected = bundled_skills_dir()
        with patch("pathlib.Path.cwd", return_value=Path("/unrelated/workspace")):
            self.assertEqual(bundled_skills_dir(), expected)

    def test_executable_and_binary_assets_are_present(self) -> None:
        extensions = {p.suffix for optional in (False, True)
                      for p in bundled_skills_dir(optional).rglob("*") if p.is_file()}
        for extension in (".py", ".sh", ".json", ".tmpl", ".png", ".pdf", ".tex", ""):
            self.assertIn(extension, extensions)

    def test_workspace_skills_take_precedence_over_builtin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            skill = workspace / ".hex/skills/airtable/SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("---\nname: airtable\ndescription: Workspace override\n---\n", encoding="utf-8")
            index = build_skills_index(workspace, global_home=workspace / "home")
            self.assertIn("- airtable: Workspace override", index)
            self.assertNotIn("Airtable REST API", index)


if __name__ == "__main__":
    unittest.main()
