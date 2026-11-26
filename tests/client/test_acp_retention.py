"""Retention settings ACP tests: the config/set surface writes real YAML
types under the [retention] section that storage/gc.py consumes through
load_retention_config, so the desktop retention pane drives the GC directly.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from hex_cli.acp import CONFIG_SCHEMA, ACPServer
from harness_core.config_loader import load_retention_config
from storage.gc import GarbageCollector


def _req(id_, method, params=None):
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


class TestRetentionSurface(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        # Mirror the production layout: the server owns ~/.hex/config.yaml
        # while the GC resolves retention from the workspace first.
        self.cfg_dir = self.ws / ".hex"
        self.cfg = self.cfg_dir / "config.yaml"
        self.cfg_dir.mkdir(parents=True)
        self.cfg.write_text("model:\n  cloud_provider: gemini\n", encoding="utf-8")
        self.server = ACPServer(orchestrator=None, workspace_root=self.ws, config_dir=self.cfg_dir)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _set(self, updates: dict) -> dict:
        return self.server.handle_request(_req(1, "config/set", {"updates": updates}))

    def _get(self) -> dict:
        return self.server.handle_request(_req(2, "config/get"))["result"]

    def test_schema_declares_retention_fields(self) -> None:
        fields = self._get()["schema"]["fields"]
        self.assertEqual(
            fields["retention.workspaceTtl"],
            {"kind": "choice", "options": list(CONFIG_SCHEMA["retention"]["workspace_ttl"]["options"])},
        )
        self.assertEqual(
            fields["retention.casTtl"],
            {"kind": "choice", "options": list(CONFIG_SCHEMA["retention"]["cas_ttl"]["options"])},
        )
        self.assertEqual(fields["retention.autoVacuum"], {"kind": "bool"})

    def test_view_omits_unset_retention_keys(self) -> None:
        self.assertNotIn("retention", self._get()["config"])

    def test_set_persists_yaml_types_and_drives_the_gc(self) -> None:
        resp = self._set(
            {
                "retention": {
                    "workspaceTtl": "7d",
                    "casTtl": "30d",
                    "autoVacuum": False,
                }
            }
        )
        self.assertEqual(
            resp["result"]["changed"],
            ["retention.autoVacuum", "retention.casTtl", "retention.workspaceTtl"],
        )

        view = self._get()["config"]["retention"]
        self.assertEqual(view, {"workspaceTtl": "7d", "casTtl": "30d", "autoVacuum": False})

        # auto_vacuum must be a real YAML boolean, not the string "False".
        stored = yaml.safe_load(self.cfg.read_text(encoding="utf-8"))
        self.assertEqual(stored["retention"]["auto_vacuum"], False)

        loaded = load_retention_config(self.ws)
        self.assertEqual(loaded["workspace_ttl"], "7d")
        self.assertEqual(loaded["cas_ttl"], "30d")
        self.assertIs(loaded["auto_vacuum"], False)

        gc = GarbageCollector(
            data_dir=self.ws / ".hex" / "data",
            workspace_root=self.ws,
        )
        self.assertEqual(gc.workspace_ttl_seconds, 7 * 86400)
        self.assertEqual(gc.cas_ttl_seconds, 30 * 86400)
        self.assertFalse(gc.auto_vacuum)

    def test_set_accepts_string_bool_wire_values(self) -> None:
        resp = self._set({"retention": {"autoVacuum": "false"}})
        self.assertEqual(resp["result"]["changed"], ["retention.autoVacuum"])
        stored = yaml.safe_load(self.cfg.read_text(encoding="utf-8"))
        self.assertIs(stored["retention"]["auto_vacuum"], False)

    def test_set_rejects_invalid_retention_values_without_writing(self) -> None:
        before = self.cfg.read_text(encoding="utf-8")
        cases = [
            ({"retention": {"workspaceTtl": "10m"}}, "ttl outside choice set"),
            ({"retention": {"workspaceTtl": "garbage"}}, "ttl garbage"),
            ({"retention": {"casTtl": 30}}, "ttl wrong type"),
            ({"retention": {"autoVacuum": "yes"}}, "bool wrong word"),
            ({"retention": {"autoVacuum": 1}}, "bool wrong type"),
            ({"retention": {"notAKey": True}}, "unknown key"),
        ]
        for updates, label in cases:
            with self.subTest(label):
                resp = self._set(updates)
                self.assertIn("error", resp, label)
        self.assertEqual(self.cfg.read_text(encoding="utf-8"), before)

    def test_load_retention_config_coerces_hand_edited_string_booleans(self) -> None:
        self.cfg.write_text(
            "retention:\n  auto_vacuum: \"false\"\n  workspace_ttl: 72h\n",
            encoding="utf-8",
        )
        loaded = load_retention_config(self.ws)
        self.assertIs(loaded["auto_vacuum"], False)
        self.assertEqual(loaded["workspace_ttl"], "72h")


if __name__ == "__main__":
    unittest.main()
