"""F08 regression tests: typed config parsing, actionable errors, safe atomic writes."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness_core.config_loader import (
    ConfigParseError,
    load_hex_environment,
    parse_config_yaml,
    update_config_yaml,
    write_config_yaml_atomic,
)


class TestTypedConfigParsing(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write(self, text: str) -> Path:
        p = self.root / "config.yaml"
        p.write_text(text, encoding="utf-8")
        return p

    def test_values_keep_native_yaml_types(self) -> None:
        p = self._write(
            "flag: false\n"
            "count: 3\n"
            "ratio: 0.5\n"
            "surfaces: [file, terminal]\n"
            "nothing: null\n"
            "model:\n"
            "  project_override: true\n"
            "  max_turns: 90\n"
        )
        cfg = parse_config_yaml(p)
        self.assertIs(cfg["flag"], False)
        self.assertEqual(cfg["count"], 3)
        self.assertIsInstance(cfg["count"], int)
        self.assertEqual(cfg["ratio"], 0.5)
        self.assertEqual(cfg["surfaces"], ["file", "terminal"])
        self.assertIsNone(cfg["nothing"])
        self.assertIs(cfg["model"]["project_override"], True)
        self.assertEqual(cfg["model"]["max_turns"], 90)

    def test_malformed_yaml_raises_actionable_error(self) -> None:
        p = self._write("model:\n  cloud_provider: [unclosed\n")
        with self.assertRaises(ConfigParseError) as ctx:
            parse_config_yaml(p)
        message = str(ctx.exception)
        self.assertIn(str(p), message)
        self.assertIn("line", message)

    def test_non_mapping_document_raises(self) -> None:
        p = self._write("- just\n- a\n- list\n")
        with self.assertRaises(ConfigParseError) as ctx:
            parse_config_yaml(p)
        self.assertIn("mapping", str(ctx.exception))

    def test_missing_and_empty_files_are_empty_configs(self) -> None:
        self.assertEqual(parse_config_yaml(self.root / "absent.yaml"), {})
        self.assertEqual(parse_config_yaml(self._write("")), {})
        self.assertEqual(parse_config_yaml(self._write("# only a comment\n")), {})

    def test_update_deep_merges_and_preserves_unrelated_sections(self) -> None:
        p = self._write(
            "model:\n"
            "  cloud_provider: anthropic\n"
            "  cloud_model_id: old-model\n"
            "learning:\n"
            "  auto_reflect: idle\n"
            "retention:\n"
            "  cas_ttl: 30d\n"
            "custom_section:\n"
            "  keep_me: true\n"
        )
        merged = update_config_yaml(p, {"model": {"cloud_model_id": "new-model"}})
        self.assertEqual(merged["model"]["cloud_provider"], "anthropic")
        self.assertEqual(merged["model"]["cloud_model_id"], "new-model")
        self.assertEqual(merged["learning"]["auto_reflect"], "idle")
        self.assertEqual(merged["retention"]["cas_ttl"], "30d")
        self.assertIs(merged["custom_section"]["keep_me"], True)
        reread = parse_config_yaml(p)
        self.assertEqual(reread, merged)

    def test_update_refuses_to_overwrite_unreadable_config(self) -> None:
        p = self._write("model: [broken\n")
        before = p.read_bytes()
        with self.assertRaises(ConfigParseError):
            update_config_yaml(p, {"model": {"cloud_provider": "openai"}})
        self.assertEqual(p.read_bytes(), before)

    def test_atomic_write_leaves_no_temp_files(self) -> None:
        p = self.root / "config.yaml"
        write_config_yaml_atomic(p, {"model": {"cloud_provider": "gemini"}})
        self.assertEqual(parse_config_yaml(p)["model"]["cloud_provider"], "gemini")
        leftovers = [f for f in self.root.iterdir() if f.name.startswith(".config.yaml.")]
        self.assertEqual(leftovers, [])

    def test_load_environment_skips_malformed_config_with_warning(self) -> None:
        cfg_dir = self.root / "hex"
        cfg_dir.mkdir()
        (cfg_dir / "config.yaml").write_text("model: [broken\n", encoding="utf-8")
        (cfg_dir / "credentials.env").write_text("TAVILY_API_KEY=tvly-test\n", encoding="utf-8")
        ws = self.root / "ws"
        ws.mkdir()
        discovered = load_hex_environment(config_dir=cfg_dir, workspace_root=ws, reset_managed=True)
        self.assertEqual(discovered.get("TAVILY_API_KEY"), "tvly-test")


class TestConfigWriteSeams(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_model_patcher_preserves_comments_and_unrelated_sections(self) -> None:
        from harness_core.client.onboard import update_config_yaml_model

        p = self.root / "config.yaml"
        p.write_text(
            "# user comment\n"
            "model:\n"
            "  cloud_provider: \"anthropic\"\n"
            "learning:\n"
            "  auto_reflect: idle  # keep this comment too\n",
            encoding="utf-8",
        )
        ok = update_config_yaml_model(
            p, cloud_provider="openai", cloud_model_id="gpt-4o", profile="cloud"
        )
        self.assertTrue(ok)
        text = p.read_text(encoding="utf-8")
        self.assertIn("# user comment", text)
        self.assertIn("# keep this comment too", text)
        cfg = parse_config_yaml(p)
        self.assertEqual(cfg["model"]["cloud_provider"], "openai")
        self.assertEqual(cfg["model"]["cloud_model_id"], "gpt-4o")
        self.assertEqual(cfg["learning"]["auto_reflect"], "idle")

    def test_model_patcher_refuses_invalid_result(self) -> None:
        from harness_core.client.onboard import update_config_yaml_model

        p = self.root / "config.yaml"
        p.write_text("model: [not, a, mapping]\n", encoding="utf-8")
        before = p.read_bytes()
        ok = update_config_yaml_model(
            p, cloud_provider="openai", cloud_model_id="gpt-4o", profile="cloud"
        )
        self.assertFalse(ok)
        self.assertEqual(p.read_bytes(), before)

    def test_wizard_rerun_preserves_user_sections(self) -> None:
        from harness_core.client.onboard import run_onboard_wizard

        cfg_file = self.root / "config.yaml"
        cfg_file.write_text(
            "model:\n"
            "  cloud_provider: \"anthropic\"\n"
            "  cloud_model_id: \"claude-3-7-sonnet\"\n"
            "learning:\n"
            "  auto_reflect: idle\n"
            "retention:\n"
            "  cas_ttl: 30d\n"
            "my_custom:\n"
            "  nested:\n"
            "    value: 42\n",
            encoding="utf-8",
        )
        res = run_onboard_wizard(
            config_dir=self.root,
            target_provider="deepseek",
            target_model="deepseek-reasoner",
            non_interactive=True,
            first_task_beat=False,
        )
        self.assertEqual(res.get("cloud_provider"), "deepseek")
        cfg = parse_config_yaml(cfg_file)
        self.assertEqual(cfg["model"]["cloud_provider"], "deepseek")
        self.assertEqual(cfg["learning"]["auto_reflect"], "idle")
        self.assertEqual(cfg["retention"]["cas_ttl"], "30d")
        self.assertEqual(cfg["my_custom"]["nested"]["value"], 42)

    def test_auto_setup_rerun_preserves_user_sections(self) -> None:
        from harness_core.client.auto_setup import EnvironmentSnapshot, execute_auto_setup

        cfg_file = self.root / "config.yaml"
        cfg_file.write_text(
            "learning:\n"
            "  auto_reflect: idle\n"
            "my_custom:\n"
            "  keep: true\n",
            encoding="utf-8",
        )
        env = EnvironmentSnapshot(
            os_name="Linux",
            arch="x86_64",
            anthropic_key="sk-ant-test-key-1234",
        )
        execute_auto_setup(config_dir=self.root, custom_env=env)
        cfg = parse_config_yaml(cfg_file)
        self.assertEqual(cfg["model"]["cloud_provider"], "anthropic")
        self.assertEqual(cfg["learning"]["auto_reflect"], "idle")
        self.assertIs(cfg["my_custom"]["keep"], True)


if __name__ == "__main__":
    unittest.main()