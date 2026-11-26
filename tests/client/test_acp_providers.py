"""Provider metadata and write-only credential APIs for the private ACP dialect."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
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
            for env_var in spec.base_url_env_vars:
                os.environ.pop(env_var, None)
        os.environ.pop("HEX_CLOUD_BASE_URL", None)

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
        self.assertEqual(result["provider"]["credentialSource"], "credentials_file")
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

    def test_provider_endpoint_status_reports_precedence_without_url_secrets(self) -> None:
        (self.config_dir / "config.yaml").write_text(
            "model:\n  cloud_provider: anthropic\n  cloud_base_url: "
            "https://user:password@model-proxy.invalid/v1?token=url-secret#fragment\n",
            encoding="utf-8",
        )
        self.credentials_file.write_text(
            "ANTHROPIC_BASE_URL=https://file-proxy.invalid/v1\n"
            "OPENAI_BASE_URL=https://file-openai.invalid/v1\n",
            encoding="utf-8",
        )
        os.environ["OPENAI_BASE_URL"] = "https://process-openai.invalid/v1"

        result = self.server.handle_request(_req(1, "providers/list"))["result"]
        serialized = json.dumps(result)
        self.assertNotIn("password", serialized)
        self.assertNotIn("url-secret", serialized)
        providers = {provider["id"]: provider for provider in result["providers"]}
        anthropic = providers["anthropic"]
        self.assertEqual(anthropic["effectiveBaseUrl"], "https://model-proxy.invalid/v1")
        self.assertEqual(anthropic["endpointSource"], "model_config")
        self.assertFalse(anthropic["endpointWritable"])
        self.assertEqual(
            providers["openai"]["effectiveBaseUrl"], "https://file-openai.invalid/v1"
        )
        self.assertEqual(providers["openai"]["endpointSource"], "credentials_file")
        self.assertTrue(providers["openai"]["endpointWritable"])
        self.assertEqual(providers["gemini"]["endpointSource"], "default")
        self.assertEqual(
            providers["gemini"]["effectiveBaseUrl"], providers["gemini"]["defaultBaseUrl"]
        )

    def test_active_global_endpoint_shadows_provider_override_and_is_not_writable(self) -> None:
        (self.config_dir / "config.yaml").write_text(
            "model:\n  cloud_provider: anthropic\n", encoding="utf-8"
        )
        original = "ANTHROPIC_BASE_URL=https://provider.invalid/v1\n"
        self.credentials_file.write_text(original, encoding="utf-8")
        os.environ["HEX_CLOUD_BASE_URL"] = "https://global.invalid/v1"

        result = self.server.handle_request(_req(1, "providers/list"))["result"]
        anthropic = next(
            provider for provider in result["providers"] if provider["id"] == "anthropic"
        )
        self.assertEqual(anthropic["effectiveBaseUrl"], "https://global.invalid/v1")
        self.assertEqual(anthropic["endpointSource"], "provider_environment")
        self.assertEqual(anthropic["endpointEnvVar"], "HEX_CLOUD_BASE_URL")
        self.assertFalse(anthropic["endpointWritable"])

        response = self.server.handle_request(
            _req(2, "providers/endpoint/set", {"provider": "anthropic", "baseUrl": "https://other.invalid"})
        )
        self.assertEqual(response["error"]["code"], -32602)
        self.assertIn("HEX_CLOUD_BASE_URL", response["error"]["message"])
        self.assertEqual(self.credentials_file.read_text(encoding="utf-8"), original)

    def test_provider_endpoint_set_and_reset_are_atomic_private_and_immediate(self) -> None:
        self.credentials_file.write_text(
            "# keep this comment\nUNRELATED=keep\nANTHROPIC_BASE_URL=https://old.invalid/v1\n",
            encoding="utf-8",
        )
        endpoint = "https://new-proxy.invalid/v1"

        response = self.server.handle_request(
            _req(2, "providers/endpoint/set", {"provider": "anthropic", "baseUrl": endpoint + "/"})
        )
        result = response["result"]
        self.assertTrue(result["changed"])
        self.assertEqual(result["provider"]["effectiveBaseUrl"], endpoint)
        self.assertEqual(result["provider"]["endpointSource"], "credentials_file")
        self.assertEqual(result["provider"]["endpointEnvVar"], "ANTHROPIC_BASE_URL")
        self.assertEqual(os.environ["ANTHROPIC_BASE_URL"], endpoint)
        raw = self.credentials_file.read_text(encoding="utf-8")
        self.assertIn("# keep this comment", raw)
        self.assertIn("UNRELATED=keep", raw)
        self.assertIn(f"ANTHROPIC_BASE_URL={endpoint}", raw)
        self.assertNotIn("old.invalid", raw)
        self.assertEqual(stat.S_IMODE(self.credentials_file.stat().st_mode), 0o600)

        repeated = self.server.handle_request(
            _req(3, "providers/endpoint/set", {"provider": "anthropic", "baseUrl": endpoint})
        )
        self.assertFalse(repeated["result"]["changed"])

        reset = self.server.handle_request(
            _req(4, "providers/endpoint/set", {"provider": "anthropic", "baseUrl": ""})
        )
        self.assertTrue(reset["result"]["changed"])
        self.assertEqual(reset["result"]["provider"]["endpointSource"], "default")
        self.assertNotIn("ANTHROPIC_BASE_URL", os.environ)
        self.assertNotIn("ANTHROPIC_BASE_URL", self.credentials_file.read_text(encoding="utf-8"))

    def test_provider_endpoint_reset_preserves_an_unmanaged_environment_value(self) -> None:
        self.credentials_file.write_text(
            "OPENAI_BASE_URL=https://managed.invalid/v1\n", encoding="utf-8"
        )
        os.environ["OPENAI_BASE_URL"] = "https://managed.invalid/v1"
        managed = self.server.handle_request(
            _req(2, "providers/endpoint/set", {"provider": "openai", "baseUrl": ""})
        )
        self.assertTrue(managed["result"]["changed"])
        self.assertNotIn("OPENAI_BASE_URL", os.environ)

        self.credentials_file.write_text(
            "OPENAI_BASE_URL=https://managed.invalid/v1\n", encoding="utf-8"
        )
        os.environ["OPENAI_BASE_URL"] = "https://external.invalid/v1"
        external = self.server.handle_request(
            _req(3, "providers/endpoint/set", {"provider": "openai", "baseUrl": ""})
        )
        self.assertTrue(external["result"]["changed"])
        self.assertEqual(os.environ["OPENAI_BASE_URL"], "https://external.invalid/v1")
        self.assertEqual(external["result"]["provider"]["endpointSource"], "provider_environment")

    def test_provider_endpoint_rejects_invalid_or_model_managed_requests(self) -> None:
        original = "# untouched\nUNRELATED=keep\n"
        self.credentials_file.write_text(original, encoding="utf-8")
        cases = [
            ([1], "provider params must be an object"),
            ({}, "provider must be a non-empty string"),
            ({"provider": "../../anthropic", "baseUrl": "https://proxy.invalid"}, "unknown provider"),
            ({"provider": "local", "baseUrl": "https://proxy.invalid"}, "does not support"),
            ({"provider": "anthropic"}, "baseUrl must be a string"),
            ({"provider": "anthropic", "baseUrl": 123}, "baseUrl must be a string"),
            ({"provider": "anthropic", "baseUrl": "has space"}, "whitespace"),
            ({"provider": "anthropic", "baseUrl": "ftp://proxy.invalid"}, "HTTP(S)"),
            ({"provider": "anthropic", "baseUrl": "https://user:pass@proxy.invalid"}, "user information"),
            ({"provider": "anthropic", "baseUrl": "https://proxy.invalid?token=secret"}, "query string"),
            ({"provider": "anthropic", "baseUrl": "https://proxy.invalid#secret"}, "fragment"),
            ({"provider": "anthropic", "baseUrl": "x" * 501}, "at most"),
        ]
        for params, message in cases:
            with self.subTest(params=params):
                response = self.server.handle_request(_req(2, "providers/endpoint/set", params))
                self.assertEqual(response["error"]["code"], -32602)
                self.assertIn(message, response["error"]["message"])
                self.assertEqual(self.credentials_file.read_text(encoding="utf-8"), original)

        (self.config_dir / "config.yaml").write_text(
            "model:\n  cloud_provider: anthropic\n  cloud_base_url: https://model.invalid/v1\n",
            encoding="utf-8",
        )
        managed = self.server.handle_request(
            _req(3, "providers/endpoint/set", {"provider": "anthropic", "baseUrl": "https://other.invalid"})
        )
        self.assertEqual(managed["error"]["code"], -32602)
        self.assertIn("model.cloudBaseUrl", managed["error"]["message"])

    def test_provider_endpoint_set_updates_the_active_runtime_provider(self) -> None:
        runtime_provider = SimpleNamespace(provider_id="anthropic", base_url="https://old.invalid")
        self.server._orchestrator = SimpleNamespace(
            router=SimpleNamespace(cloud_provider=runtime_provider)
        )
        response = self.server.handle_request(
            _req(2, "providers/endpoint/set", {"provider": "anthropic", "baseUrl": "https://new.invalid/v1"})
        )
        self.assertIn("result", response)
        self.assertEqual(runtime_provider.base_url, "https://new.invalid/v1")

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
