"""Provider metadata and write-only credential APIs for the private ACP dialect."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from harness_core.providers.registry import list_providers
from hex_cli.acp import ACPServer, PROVIDER_CREDENTIAL_MAX_LENGTH


def _req(id_, method, params=None):
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


class TestProviderCredentials(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.tmp.name)
        self.credentials_file = self.config_dir / "credentials.env"
        self.server = ACPServer(
            orchestrator=None,
            workspace_root=self.config_dir,
            config_dir=self.config_dir,
        )
        self.env_patch = patch.dict(os.environ, {}, clear=False)
        self.env_patch.start()
        for spec in list_providers():
            for env_var in spec.env_vars:
                os.environ.pop(env_var, None)

    def tearDown(self) -> None:
        self.env_patch.stop()
        self.tmp.cleanup()

    def test_provider_list_reports_metadata_and_status_without_secret_values(self) -> None:
        self.credentials_file.write_text(
            "# local credentials\nANTHROPIC_API_KEY=sk-file-secret\nUNRELATED=keep\n",
            encoding="utf-8",
        )
        os.environ["OPENAI_API_KEY"] = "sk-process-secret"

        response = self.server.handle_request(_req(1, "providers/list"))
        result = response["result"]
        serialized = json.dumps(result)
        self.assertNotIn("sk-file-secret", serialized)
        self.assertNotIn("sk-process-secret", serialized)
        self.assertNotIn("UNRELATED", serialized)
        self.assertEqual(
            result["storage"],
            {"path": str(self.credentials_file.resolve()), "scope": "config_dir"},
        )

        providers = {provider["id"]: provider for provider in result["providers"]}
        anthropic = providers["anthropic"]
        self.assertEqual(anthropic["credentialEnvVar"], "ANTHROPIC_API_KEY")
        self.assertTrue(anthropic["credentialConfigured"])
        self.assertEqual(anthropic["credentialSource"], "credentials_file")
        self.assertTrue(anthropic["credentialWritable"])
        self.assertEqual(anthropic["baseUrlEnvVar"], "ANTHROPIC_BASE_URL")
        self.assertEqual(anthropic["defaultBaseUrl"], "https://api.anthropic.com/v1")
        self.assertTrue(anthropic["signupUrl"].startswith("https://"))

        self.assertEqual(providers["openai"]["credentialSource"], "environment")
        self.assertEqual(providers["gemini"]["credentialSource"], "none")
        self.assertEqual(providers["bedrock"]["credentialSource"], "not_supported")
        self.assertFalse(providers["bedrock"]["credentialWritable"])
        self.assertIsNone(providers["bedrock"]["credentialEnvVar"])
        self.assertFalse(providers["local"]["requiresApiKey"])

    def test_credential_set_is_atomic_private_and_immediately_available(self) -> None:
        self.credentials_file.write_text(
            "# keep this comment\nUNRELATED=keep\nANTHROPIC_API_KEY=old-secret\n",
            encoding="utf-8",
        )
        secret = "sk-new-secret.with_punctuation"

        response = self.server.handle_request(
            _req(2, "providers/credentials/set", {"provider": "anthropic", "credential": secret})
        )
        result = response["result"]
        self.assertTrue(result["changed"])
        self.assertTrue(result["provider"]["credentialConfigured"])
        self.assertEqual(result["provider"]["credentialSource"], "environment")
        self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(os.environ["ANTHROPIC_API_KEY"], secret)

        raw = self.credentials_file.read_text(encoding="utf-8")
        self.assertIn("# keep this comment", raw)
        self.assertIn("UNRELATED=keep", raw)
        self.assertIn("ANTHROPIC_API_KEY=sk-new-secret.with_punctuation", raw)
        self.assertNotIn("old-secret", raw)
        self.assertEqual(stat.S_IMODE(self.credentials_file.stat().st_mode), 0o600)

        repeated = self.server.handle_request(
            _req(3, "providers/credentials/set", {"provider": "anthropic", "credential": secret})
        )
        self.assertFalse(repeated["result"]["changed"])

    def test_credential_delete_removes_only_the_selected_file_value(self) -> None:
        self.credentials_file.write_text(
            "ANTHROPIC_API_KEY=file-secret\nUNRELATED=keep\n", encoding="utf-8"
        )
        os.environ["ANTHROPIC_API_KEY"] = "file-secret"

        response = self.server.handle_request(
            _req(2, "providers/credentials/delete", {"provider": "anthropic"})
        )
        self.assertTrue(response["result"]["changed"])
        self.assertFalse(response["result"]["provider"]["credentialConfigured"])
        self.assertNotIn("ANTHROPIC_API_KEY", os.environ)
        self.assertEqual(
            self.credentials_file.read_text(encoding="utf-8"), "UNRELATED=keep\n"
        )

        repeated = self.server.handle_request(
            _req(3, "providers/credentials/delete", {"provider": "anthropic"})
        )
        self.assertFalse(repeated["result"]["changed"])

    def test_credential_delete_preserves_an_unmanaged_environment_value(self) -> None:
        os.environ["OPENAI_API_KEY"] = "external-secret"
        response = self.server.handle_request(
            _req(2, "providers/credentials/delete", {"provider": "openai"})
        )
        self.assertFalse(response["result"]["changed"])
        self.assertEqual(os.environ["OPENAI_API_KEY"], "external-secret")
        self.assertTrue(response["result"]["provider"]["credentialConfigured"])
        self.assertEqual(response["result"]["provider"]["credentialSource"], "environment")

    def test_credential_set_rejects_invalid_or_unsupported_requests(self) -> None:
        original = "# untouched\nUNRELATED=keep\n"
        self.credentials_file.write_text(original, encoding="utf-8")
        cases = [
            ([1], "provider params must be an object"),
            ({}, "provider must be a non-empty string"),
            ({"provider": "../../anthropic"}, "unknown provider"),
            ({"provider": "bedrock", "credential": "secret"}, "does not support"),
            ({"provider": "local", "credential": "secret"}, "does not support"),
            ({"provider": "anthropic"}, "credential must be a non-empty string"),
            ({"provider": "anthropic", "credential": 123}, "credential must be a non-empty string"),
            ({"provider": "anthropic", "credential": ""}, "credential must be a non-empty string"),
            ({"provider": "anthropic", "credential": "x" * (PROVIDER_CREDENTIAL_MAX_LENGTH + 1)}, "at most"),
            ({"provider": "anthropic", "credential": "has space"}, "whitespace"),
            ({"provider": "anthropic", "credential": "line\nbreak"}, "whitespace"),
            ({"provider": "anthropic", "credential": '"wrapped"'}, "wrapping quotes"),
        ]
        for params, message in cases:
            with self.subTest(params=params):
                response = self.server.handle_request(
                    _req(2, "providers/credentials/set", params)
                )
                self.assertEqual(response["error"]["code"], -32602)
                self.assertIn(message, response["error"]["message"])
                self.assertEqual(self.credentials_file.read_text(encoding="utf-8"), original)

    def test_concurrent_credential_updates_preserve_both_providers(self) -> None:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    self.server.handle_request,
                    _req(
                        request_id,
                        "providers/credentials/set",
                        {"provider": provider, "credential": f"secret-{provider}"},
                    ),
                )
                for request_id, provider in [(2, "anthropic"), (3, "openai")]
            ]
            responses = [future.result() for future in futures]

        self.assertTrue(all("result" in response for response in responses))
        raw = self.credentials_file.read_text(encoding="utf-8")
        self.assertIn("ANTHROPIC_API_KEY=secret-anthropic", raw)
        self.assertIn("OPENAI_API_KEY=secret-openai", raw)


if __name__ == "__main__":
    unittest.main()
