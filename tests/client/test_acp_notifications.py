"""Notification settings ACP tests: the notifications section of config.yaml
gates WebhookNotifier delivery categories through notification_categories().
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from hex_cli.acp import ACPServer
from capabilities.webhooks import (
    DEFAULT_NOTIFICATION_CATEGORIES,
    WebhookConfig,
    notification_categories,
)

def _req(id_, method, params=None):
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


class TestNotificationSurface(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.cfg = self.ws / "config.yaml"
        self.cfg.write_text("model:\n  cloud_provider: gemini\n", encoding="utf-8")
        self.server = ACPServer(orchestrator=None, workspace_root=self.ws, config_dir=self.ws)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _set(self, updates: dict) -> dict:
        return self.server.handle_request(_req(1, "config/set", {"updates": updates}))

    def _get(self) -> dict:
        return self.server.handle_request(_req(2, "config/get"))["result"]

    def test_schema_declares_notification_fields(self) -> None:
        fields = self._get()["schema"]["fields"]
        self.assertEqual(fields["notifications.approvals"], {"kind": "bool"})
        self.assertEqual(fields["notifications.alerts"], {"kind": "bool"})

    def test_view_omits_unset_notification_keys(self) -> None:
        self.assertNotIn("notifications", self._get()["config"])

    def test_set_persists_real_booleans(self) -> None:
        resp = self._set({"notifications": {"approvals": False, "alerts": True}})
        self.assertEqual(
            resp["result"]["changed"],
            ["notifications.alerts", "notifications.approvals"],
        )
        view = self._get()["config"]["notifications"]
        self.assertEqual(view, {"approvals": False, "alerts": True})
        stored = yaml.safe_load(self.cfg.read_text(encoding="utf-8"))
        self.assertIs(stored["notifications"]["approvals"], False)
        self.assertIs(stored["notifications"]["alerts"], True)

        self.assertEqual(
            notification_categories(yaml.safe_load(self.cfg.read_text(encoding="utf-8"))),
            frozenset({"alerts"}),
        )

    def test_set_rejects_invalid_values_without_writing(self) -> None:
        before = self.cfg.read_text(encoding="utf-8")
        cases = [
            ({"notifications": {"approvals": "yes"}}, "bool wrong word"),
            ({"notifications": {"approvals": 1}}, "bool wrong type"),
            ({"notifications": {"notAKey": True}}, "unknown key"),
        ]
        for updates, label in cases:
            with self.subTest(label):
                resp = self._set(updates)
                self.assertIn("error", resp, label)
        self.assertEqual(self.cfg.read_text(encoding="utf-8"), before)

    def test_notification_categories_defaults_and_coercion(self) -> None:
        self.assertEqual(notification_categories({}), DEFAULT_NOTIFICATION_CATEGORIES)
        self.assertEqual(
            notification_categories({"notifications": {}}), DEFAULT_NOTIFICATION_CATEGORIES
        )
        self.assertEqual(
            notification_categories({"notifications": {"approvals": False}}),
            frozenset({"alerts"}),
        )
        self.assertEqual(
            notification_categories({"notifications": {"approvals": "false", "alerts": "no"}}),
            frozenset(),
        )
        self.assertEqual(
            notification_categories({"notifications": {"approvals": "off", "alerts": "1"}}),
            frozenset({"alerts"}),
        )
        with self.assertRaises(ValueError):
            notification_categories({"notifications": "enabled"})
        with self.assertRaises(ValueError):
            notification_categories({"notifications": {"approvals": object()}})

    def test_webhook_config_categories_gate_default(self) -> None:
        config = WebhookConfig()
        self.assertEqual(config.categories, DEFAULT_NOTIFICATION_CATEGORIES)
        self.assertTrue(config.category_enabled("approvals"))
        scoped = config.with_categories(frozenset({"alerts"}))
        self.assertFalse(scoped.category_enabled("approvals"))
        self.assertTrue(config.category_enabled("approvals"))


if __name__ == "__main__":
    unittest.main()
