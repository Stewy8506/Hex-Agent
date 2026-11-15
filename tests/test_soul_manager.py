import tempfile
import unittest
from pathlib import Path

from harness_core.soul import (
    DEFAULT_SOUL_MD,
    DEFAULT_SOUL_NAME,
    DEFAULT_USER_MD,
    MAX_SOUL_CHARS,
    SoulManager,
    _LEGACY_TEMPLATE_SOULS,
    normalize_soul,
)


class TestSoulManager(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._temp_dir.name)
        self.temp_hex_home = self.tmp_path / ".hex"
        self.temp_hex_home.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def test_soul_manager_initialization_seeds_defaults(self) -> None:
        mgr = SoulManager(global_home=self.temp_hex_home)
        self.assertTrue(mgr.global_soul_path.exists())
        self.assertTrue(mgr.global_user_path.exists())

        content = mgr.global_soul_path.read_text(encoding="utf-8")
        self.assertIn("You are HEX", content)
        self.assertEqual(normalize_soul(content), normalize_soul(DEFAULT_SOUL_MD))

        dna = mgr.load_developer_dna()
        self.assertIn("Developer Profile & Conventions", dna)

    def test_soul_manager_resolution_hierarchy(self) -> None:
        mgr = SoulManager(global_home=self.temp_hex_home)
        ws = self.tmp_path / "my_project"
        ws.mkdir()

        # 1. Without workspace .hex/SOUL.md, resolves global
        content, source = mgr.resolve_soul(workspace_root=ws)
        self.assertIn("You are HEX", content)
        self.assertEqual(source, "global")

        # 2. With workspace .hex/SOUL.md, workspace takes precedence
        ws_hex = ws / ".hex"
        ws_hex.mkdir()
        ws_soul = ws_hex / "SOUL.md"
        ws_soul.write_text("You are ProjectBot, tuned for this specific repo.", encoding="utf-8")

        content, source = mgr.resolve_soul(workspace_root=ws)
        self.assertIn("You are ProjectBot", content)
        self.assertEqual(source, "workspace")

    def test_soul_manager_upgrades_legacy_template(self) -> None:
        # Seed a legacy template
        legacy_text = _LEGACY_TEMPLATE_SOULS[0]
        soul_file = self.temp_hex_home / "SOUL.md"
        soul_file.write_text(legacy_text, encoding="utf-8")

        mgr = SoulManager(global_home=self.temp_hex_home)
        # The constructor calls _ensure_global_files -> maybe_upgrade_soul
        updated = soul_file.read_text(encoding="utf-8")
        self.assertEqual(normalize_soul(updated), normalize_soul(DEFAULT_SOUL_MD))
        self.assertIn("You are HEX", updated)

    def test_soul_manager_preserves_custom_user_soul(self) -> None:
        custom_text = "You are CustomAssistant. Speak entirely in pirate dialect: Ahoy!"
        soul_file = self.temp_hex_home / "SOUL.md"
        soul_file.write_text(custom_text, encoding="utf-8")

        mgr = SoulManager(global_home=self.temp_hex_home)
        # Must NOT be overwritten
        current = soul_file.read_text(encoding="utf-8")
        self.assertEqual(current, custom_text)
        content, source = mgr.resolve_soul()
        self.assertEqual(content, custom_text)

    def test_soul_manager_enforces_budget(self) -> None:
        # Oversized content > MAX_SOUL_CHARS
        oversized = "A" * (MAX_SOUL_CHARS + 500)
        soul_file = self.temp_hex_home / "SOUL.md"
        soul_file.write_text(oversized, encoding="utf-8")

        mgr = SoulManager(global_home=self.temp_hex_home)
        content, _ = mgr.resolve_soul()
        self.assertLessEqual(len(content), MAX_SOUL_CHARS + 100)
        self.assertIn("... [truncated to fit 500-token budget]", content)

    def test_soul_manager_rename_identity(self) -> None:
        mgr = SoulManager(global_home=self.temp_hex_home)
        self.assertEqual(mgr.get_active_name(), DEFAULT_SOUL_NAME)

        # Rename globally to "Athena"
        mgr.rename_identity("Athena", scope="global")
        self.assertEqual(mgr.get_active_name(), "Athena")
        content, _ = mgr.resolve_soul()
        self.assertIn("You are Athena,", content)

        # Rename in workspace scope
        ws = self.tmp_path / "workspace_test"
        ws.mkdir()
        mgr.rename_identity("Jarvis", scope="workspace", workspace_root=ws)
        self.assertEqual(mgr.get_active_name(workspace_root=ws), "Jarvis")

        ws_content, source = mgr.resolve_soul(workspace_root=ws)
        self.assertEqual(source, "workspace")
        self.assertIn("You are Jarvis,", ws_content)


if __name__ == "__main__":
    unittest.main()
