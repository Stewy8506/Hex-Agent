"""DX3 settings-surface ACP tests: config/get sanitization, config/set
validation, safe atomic writes, and secret rejection.

The settings modal edits only whitelisted config.yaml keys; secrets live in
credentials.env and must never be writable through this surface.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hex_cli.acp import CONFIG_SCHEMA, ACPServer

SAMPLE_CONFIG = """\
model:
  cloud_provider: gemini
  cloud_model_id: gemini-2.5-pro
  reasoning_effort: low
  profile: cloud
  cloud_base_url: https://api.example.test/v1
runtime:
  max_turns: 90
web_search:
  provider: brave
totally_unrelated:
  keep_me: true
"""


def _req(id_, method, params=None):
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


class TestConfigSurface(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.cfg = self.ws / "config.yaml"
        self.cfg.write_text(SAMPLE_CONFIG, encoding="utf-8")
        self.server = ACPServer(orchestrator=None, workspace_root=self.ws, config_dir=self.ws)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _get(self) -> dict:
        return self.server.handle_request(_req(1, "config/get"))["result"]

    def test_config_get_returns_whitelisted_view_with_options(self) -> None:
        view = self._get()
        self.assertEqual(view["path"], str(self.cfg.resolve()))
        self.assertEqual(view["workspace"], str(self.ws.resolve()))
        self.assertEqual(
            view["dataDir"],
            str((self.ws / ".hex" / "data").resolve()),
        )

        self.assertEqual(view["config"]["model"]["cloudProvider"], "gemini")
        self.assertEqual(view["config"]["model"]["reasoningEffort"], "low")
        self.assertEqual(view["config"]["runtime"]["maxTurns"], 90)
        self.assertEqual(view["config"]["webSearch"]["provider"], "brave")

        # Choice vocabularies ride along for the desktop pickers.
        self.assertEqual(
            view["options"]["model.reasoningEffort"],
            list(CONFIG_SCHEMA["model"]["reasoning_effort"]["options"]),
        )

        # Unrelated config sections are never echoed.
        self.assertNotIn("totally_unrelated", json.dumps(view))

    def test_config_get_hides_absent_keys(self) -> None:
        self.cfg.write_text("model:\n  cloud_provider: openai\n", encoding="utf-8")
        view = self._get()
        self.assertEqual(set(view["config"]["model"]), {"cloudProvider"})
        self.assertNotIn("runtime", view["config"])
        self.assertNotIn("learning", view["config"])

    def test_config_set_round_trip_and_preserves_unrelated_sections(self) -> None:
        resp = self.server.handle_request(
            _req(
                2,
                "config/set",
                {
                    "updates": {
                        "model": {"reasoningEffort": "high"},
                        "cloud": {"baseUrl": "http://127.0.0.1:8787"},
                        "learning": {"autoReflect": "idle"},
                    }
                },
            )
        )
        result = resp["result"]
        self.assertEqual(
            result["changed"],
            ["cloud.baseUrl", "learning.autoReflect", "model.reasoningEffort"],
        )
        self.assertEqual(result["config"]["model"]["reasoningEffort"], "high")
        self.assertEqual(result["config"]["cloud"]["baseUrl"], "http://127.0.0.1:8787")
        self.assertEqual(result["config"]["learning"]["autoReflect"], "idle")

        raw = self.cfg.read_text(encoding="utf-8")
        self.assertIn("reasoning_effort: high", raw)
        self.assertIn("base_url: http://127.0.0.1:8787", raw)
        self.assertIn("auto_reflect: idle", raw)
        # Unrelated sections survive the merge.
        self.assertIn("totally_unrelated", raw)
        self.assertIn("keep_me: true", raw)
        # Pre-existing keys untouched by the update stay put.
        self.assertIn("cloud_model_id: gemini-2.5-pro", raw)

    def test_config_set_empty_optional_value_removes_key(self) -> None:
        resp = self.server.handle_request(
            _req(2, "config/set", {"updates": {"model": {"cloudBaseUrl": ""}}})
        )
        self.assertEqual(resp["result"]["changed"], ["model.cloudBaseUrl"])
        self.assertNotIn("cloudBaseUrl", resp["result"]["config"]["model"])
        self.assertNotIn("cloud_base_url", self.cfg.read_text(encoding="utf-8"))

    def test_config_set_rejects_invalid_payloads(self) -> None:
        cases = [
            ({"model": {"reasoningEffort": "ultra"}}, "invalid choice"),
            ({"model": {"apiKey": "sk-hunter2"}}, "secret-like key"),
            ({"model": {"cloudProvider": ""}}, "required text emptied"),
            ({"runtime": {"maxTurns": 0}}, "int below range"),
            ({"runtime": {"maxTurns": 5000}}, "int above range"),
            ({"runtime": {"maxTurns": "ninety"}}, "int wrong type"),
            ({"nope": {"x": 1}}, "unknown section"),
            ({"model": {"notAKey": 1}}, "unknown key"),
        ]
        for updates, label in cases:
            with self.subTest(label):
                resp = self.server.handle_request(_req(3, "config/set", {"updates": updates}))
                self.assertIn("error", resp, label)
        # None of the rejected writes touched the file.
        self.assertIn("reasoning_effort: low", self.cfg.read_text(encoding="utf-8"))

    def test_config_set_accepts_snake_case_wire_keys(self) -> None:
        resp = self.server.handle_request(
            _req(2, "config/set", {"updates": {"web_search": {"provider": "tavily"}}})
        )
        self.assertEqual(resp["result"]["changed"], ["webSearch.provider"])
        self.assertEqual(resp["result"]["config"]["webSearch"]["provider"], "tavily")

    def test_config_set_refuses_to_overwrite_unparseable_file(self) -> None:
        self.cfg.write_text("model: [broken\n  yaml: : :", encoding="utf-8")
        resp = self.server.handle_request(
            _req(2, "config/set", {"updates": {"model": {"reasoningEffort": "high"}}})
        )
        self.assertIn("error", resp)
        self.assertIn("unreadable", resp["error"]["message"])
        # The broken file was left untouched rather than clobbered.
        self.assertIn("broken", self.cfg.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
