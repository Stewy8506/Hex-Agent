"""DX3 settings-surface ACP tests: config/get sanitization, config/set
validation, safe atomic writes, and secret rejection.

The settings modal edits only whitelisted config.yaml keys; secrets live in
credentials.env and must never be writable through this surface.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hex_cli.acp import CONFIG_REVISION_CONFLICT, CONFIG_SCHEMA, ACPServer

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

        expected_revision = "sha256:" + hashlib.sha256(
            SAMPLE_CONFIG.encode("utf-8")
        ).hexdigest()
        self.assertEqual(view["revision"], expected_revision)
        self.assertEqual(view["schema"]["version"], 1)
        fields = view["schema"]["fields"]
        self.assertEqual(
            fields["model.cloudProvider"],
            {"kind": "text", "required": True, "maxLength": 500},
        )
        self.assertEqual(
            fields["runtime.maxTurns"],
            {"kind": "int", "min": 1, "max": 1000},
        )
        self.assertEqual(
            fields["model.reasoningEffort"]["options"],
            list(CONFIG_SCHEMA["model"]["reasoning_effort"]["options"]),
        )
        self.assertEqual(
            set(fields),
            {
                f"{ACPServer._camel(section)}.{ACPServer._camel(key)}"
                for section, keys in CONFIG_SCHEMA.items()
                for key in keys
            },
        )

    def test_config_get_hides_absent_keys(self) -> None:
        self.cfg.write_text("model:\n  cloud_provider: openai\n", encoding="utf-8")
        view = self._get()
        self.assertEqual(set(view["config"]["model"]), {"cloudProvider"})
        self.assertNotIn("runtime", view["config"])
        self.assertNotIn("learning", view["config"])

    def test_config_set_round_trip_and_preserves_unrelated_sections(self) -> None:
        loaded_revision = self._get()["revision"]
        resp = self.server.handle_request(
            _req(
                2,
                "config/set",
                {
                    "expectedRevision": loaded_revision,
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
        self.assertNotEqual(result["revision"], loaded_revision)
        self.assertEqual(result["revision"], self._get()["revision"])
        self.assertEqual(result["schema"], self._get()["schema"])

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
            ({"model": {"cloudBaseUrl": "https://user:pass@example.test"}}, "url userinfo"),
            ({"model": {"cloudBaseUrl": "https://example.test?token=secret"}}, "url query"),
            ({"model": {"cloudBaseUrl": "ftp://example.test"}}, "url scheme"),
        ]
        for updates, label in cases:
            with self.subTest(label):
                resp = self.server.handle_request(_req(3, "config/set", {"updates": updates}))
                self.assertIn("error", resp, label)
        # None of the rejected writes touched the file.
        self.assertIn("reasoning_effort: low", self.cfg.read_text(encoding="utf-8"))

    def test_config_set_rejects_stale_revision_without_writing(self) -> None:
        loaded_revision = self._get()["revision"]
        changed_config = SAMPLE_CONFIG.replace("reasoning_effort: low", "reasoning_effort: high")
        changed_config = changed_config.replace("keep_me: true", "keep_me: false")
        self.cfg.write_text(changed_config, encoding="utf-8")
        actual_revision = self._get()["revision"]

        resp = self.server.handle_request(
            _req(
                2,
                "config/set",
                {
                    "expectedRevision": loaded_revision,
                    "updates": {"runtime": {"maxTurns": 120}},
                },
            )
        )
        self.assertEqual(resp["error"]["code"], CONFIG_REVISION_CONFLICT)
        self.assertEqual(
            resp["error"]["data"],
            {
                "reason": "config_revision_conflict",
                "expectedRevision": loaded_revision,
                "actualRevision": actual_revision,
            },
        )
        self.assertEqual(self.cfg.read_text(encoding="utf-8"), changed_config)

    def test_concurrent_writes_with_same_revision_allow_only_one(self) -> None:
        loaded_revision = self._get()["revision"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    self.server.handle_request,
                    _req(
                        request_id,
                        "config/set",
                        {
                            "expectedRevision": loaded_revision,
                            "updates": {"runtime": {"maxTurns": max_turns}},
                        },
                    ),
                )
                for request_id, max_turns in [(2, 111), (3, 222)]
            ]
            responses = [future.result() for future in futures]

        self.assertEqual(sum("result" in response for response in responses), 1)
        conflict = next(response for response in responses if "error" in response)
        self.assertEqual(conflict["error"]["code"], CONFIG_REVISION_CONFLICT)
        raw = self.cfg.read_text(encoding="utf-8")
        self.assertNotEqual("max_turns: 111" in raw, "max_turns: 222" in raw)
        self.assertEqual(self._get()["revision"], conflict["error"]["data"]["actualRevision"])

    def test_config_set_rejects_invalid_expected_revision(self) -> None:
        original = self.cfg.read_text(encoding="utf-8")
        for revision in [1, True, "", "   ", [], {}]:
            with self.subTest(revision=revision):
                resp = self.server.handle_request(
                    _req(
                        2,
                        "config/set",
                        {
                            "expectedRevision": revision,
                            "updates": {"runtime": {"maxTurns": 120}},
                        },
                    )
                )
                self.assertEqual(resp["error"]["code"], -32602)
                self.assertIn("expectedRevision", resp["error"]["message"])
                self.assertEqual(self.cfg.read_text(encoding="utf-8"), original)

    def test_config_set_accepts_snake_case_wire_keys(self) -> None:
        resp = self.server.handle_request(
            _req(2, "config/set", {"updates": {"web_search": {"provider": "tavily"}}})
        )
        self.assertEqual(resp["result"]["changed"], ["webSearch.provider"])
        self.assertEqual(resp["result"]["config"]["webSearch"]["provider"], "tavily")

    def test_config_set_profile_vocabulary_and_local_model_id(self) -> None:
        loaded_revision = self._get()["revision"]
        resp = self.server.handle_request(
            _req(
                2,
                "config/set",
                {
                    "expectedRevision": loaded_revision,
                    "updates": {
                        "model": {"profile": "offline", "localModelId": "llama3.2"}
                    },
                },
            )
        )
        result = resp["result"]
        self.assertEqual(result["changed"], ["model.localModelId", "model.profile"])
        self.assertEqual(result["config"]["model"]["profile"], "offline")
        self.assertEqual(result["config"]["model"]["localModelId"], "llama3.2")

        view = self._get()
        self.assertEqual(view["config"]["model"]["profile"], "offline")
        self.assertEqual(view["config"]["model"]["localModelId"], "llama3.2")
        self.assertEqual(
            view["options"]["model.profile"],
            list(CONFIG_SCHEMA["model"]["profile"]["options"]),
        )

        raw = self.cfg.read_text(encoding="utf-8")
        self.assertIn("profile: offline", raw)
        self.assertIn("local_model_id: llama3.2", raw)

        # The empty optional value clears local_model_id from the file.
        resp = self.server.handle_request(
            _req(3, "config/set", {"updates": {"model": {"localModelId": ""}}})
        )
        self.assertEqual(resp["result"]["changed"], ["model.localModelId"])
        self.assertNotIn("localModelId", resp["result"]["config"]["model"])
        self.assertNotIn("local_model_id", self.cfg.read_text(encoding="utf-8"))

        for profile in ("ultra", "", 1):
            with self.subTest(profile=profile):
                resp = self.server.handle_request(
                    _req(4, "config/set", {"updates": {"model": {"profile": profile}}})
                )
                self.assertIn("error", resp)
        self.assertIn("profile: offline", self.cfg.read_text(encoding="utf-8"))

    def test_model_endpoint_save_updates_environment_and_active_provider(self) -> None:
        runtime_provider = SimpleNamespace(provider_id="gemini", base_url="https://old.invalid")
        self.server._orchestrator = SimpleNamespace(
            router=SimpleNamespace(cloud_provider=runtime_provider)
        )
        with patch.dict(os.environ, {}, clear=True):
            response = self.server.handle_request(
                _req(2, "config/set", {"updates": {"model": {"cloudBaseUrl": "https://new.invalid/v1"}}})
            )
            self.assertIn("result", response)
            self.assertEqual(os.environ["HEX_CLOUD_BASE_URL"], "https://new.invalid/v1")
            self.assertEqual(runtime_provider.base_url, "https://new.invalid/v1")

            cleared = self.server.handle_request(
                _req(3, "config/set", {"updates": {"model": {"cloudBaseUrl": ""}}})
            )
            self.assertIn("result", cleared)
            self.assertNotIn("HEX_CLOUD_BASE_URL", os.environ)
            self.assertEqual(runtime_provider.base_url, "https://generativelanguage.googleapis.com/v1beta")

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
