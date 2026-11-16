"""Comprehensive unit test suite for OS Keychain secure vault and credential migration."""

from __future__ import annotations

import stat
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from harness_core.storage.vault_keychain import (
    SERVICE,
    KeychainUnavailable,
    _derive_account_name,
    _is_os_keychain,
    backend_label,
    delete_secret,
    get_secret,
    is_keychain_available,
    migrate_plaintext_credentials,
    os_keyring,
    store_secret,
)


class MockKeyringBackend:
    """Hermetic mock keyring backend emulating authentic OS credential stores."""

    def __init__(self, name: str = "MockOSKeyring"):
        self.name = name
        self.passwords: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, account: str) -> str | None:
        return self.passwords.get((service, account))

    def set_password(self, service: str, account: str, password: str) -> None:
        self.passwords[(service, account)] = password

    def delete_password(self, service: str, account: str) -> None:
        self.passwords.pop((service, account), None)


class NullKeyringBackend:
    pass
NullKeyringBackend.__module__ = "keyring.backends.null"

class FailKeyringBackend:
    pass
FailKeyringBackend.__module__ = "keyring.backends.fail"

class AltKeyringBackend:
    pass
AltKeyringBackend.__module__ = "keyrings.alt.file"


class TestVaultKeychain(unittest.TestCase):
    def test_derive_account_name(self):
        self.assertEqual(_derive_account_name("OPENAI_API_KEY"), "openai_api_key")
        self.assertEqual(_derive_account_name("ANTHROPIC_API_KEY"), "anthropic_api_key")
        self.assertEqual(_derive_account_name("TAVILY_KEY"), "tavily_api_key")
        self.assertEqual(_derive_account_name("TELEGRAM_BOT_TOKEN"), "telegram_bot_token")
        self.assertEqual(_derive_account_name("api_key", parent="deepseek"), "deepseek_api_key")

    def test_is_os_keychain_validation(self):
        valid = MockKeyringBackend()
        self.assertTrue(_is_os_keychain(valid))

        # Insecure / stub backends must be rejected
        self.assertFalse(_is_os_keychain(NullKeyringBackend()))
        self.assertFalse(_is_os_keychain(FailKeyringBackend()))
        self.assertFalse(_is_os_keychain(AltKeyringBackend()))

    def test_store_get_and_delete_secret(self):
        backend = MockKeyringBackend()
        with patch("keyring.get_keyring", return_value=backend):
            self.assertTrue(is_keychain_available())
            self.assertEqual(backend_label(), "MockOSKeyring")

            # Store secret
            store_secret(SERVICE, "openai_api_key", "sk-live-test-12345")
            self.assertEqual(backend.passwords[(SERVICE, "openai_api_key")], "sk-live-test-12345")

            # Retrieve secret
            retrieved = get_secret(SERVICE, "openai_api_key")
            self.assertEqual(retrieved, "sk-live-test-12345")

            # Non-existent secret returns None
            self.assertIsNone(get_secret(SERVICE, "non_existent"))

            # Delete secret
            deleted = delete_secret(SERVICE, "openai_api_key")
            self.assertTrue(deleted)
            self.assertIsNone(get_secret(SERVICE, "openai_api_key"))

            # Idempotent delete on missing key returns True
            self.assertTrue(delete_secret(SERVICE, "already_deleted"))

    def test_keychain_unavailable_behaviors(self):
        null_backend = NullKeyringBackend()

        with patch("keyring.get_keyring", return_value=null_backend):
            self.assertFalse(is_keychain_available())
            self.assertIn("unavailable:", backend_label())

            with self.assertRaises(KeychainUnavailable):
                store_secret(SERVICE, "openai_api_key", "sk-test")

            self.assertIsNone(get_secret(SERVICE, "openai_api_key"))
            self.assertFalse(delete_secret(SERVICE, "openai_api_key"))

    def test_readback_mismatch_raises_exception(self):
        backend = MockKeyringBackend()
        # Sabotage get_password to simulate silent write failure
        backend.get_password = lambda service, account: "corrupted_secret"
        with patch("keyring.get_keyring", return_value=backend):
            with self.assertRaises(KeychainUnavailable):
                store_secret(SERVICE, "openai_api_key", "sk-intended-secret")

    def test_migrate_plaintext_credentials_yaml(self, tmp_path=None):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "config.yaml"
            config_content = (
                "model:\n"
                "  provider: openai\n"
                "  name: gpt-4o\n"
                "OPENAI_API_KEY: \"sk-plaintext-123\"\n"
                "ANTHROPIC_API_KEY: 'sk-ant-456'\n"
            )
            config_file.write_text(config_content, encoding="utf-8")

            backend = MockKeyringBackend()
            with patch("keyring.get_keyring", return_value=backend):
                migrated = migrate_plaintext_credentials(config_path=config_file)

                # Both keys migrated
                self.assertIn("OPENAI_API_KEY", migrated)
                self.assertIn("ANTHROPIC_API_KEY", migrated)
                self.assertEqual(migrated["OPENAI_API_KEY"], "openai_api_key")
                self.assertEqual(migrated["ANTHROPIC_API_KEY"], "anthropic_api_key")

                # Secrets stored in OS keychain
                self.assertEqual(backend.get_password(SERVICE, "openai_api_key"), "sk-plaintext-123")
                self.assertEqual(backend.get_password(SERVICE, "anthropic_api_key"), "sk-ant-456")

                # Plaintext scrubbed from file
                scrubbed = config_file.read_text(encoding="utf-8")
                self.assertNotIn("sk-plaintext-123", scrubbed)
                self.assertNotIn("sk-ant-456", scrubbed)
                self.assertIn("OPENAI_API_KEY: keychain", scrubbed)
                self.assertIn("ANTHROPIC_API_KEY: keychain", scrubbed)

                # Permissions restricted to 0600
                mode = stat.S_IMODE(config_file.stat().st_mode)
                self.assertEqual(mode, 0o600)

    def test_migrate_plaintext_credentials_env(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_file = Path(tmp_dir) / "credentials.env"
            env_content = (
                "# Operator credentials\n"
                "OPENAI_API_KEY=sk-env-openai-123\n"
                "TAVILY_API_KEY=\"tvly-secret-999\"\n"
                "NON_SECRET_FLAG=true\n"
            )
            env_file.write_text(env_content, encoding="utf-8")

            backend = MockKeyringBackend()
            with patch("keyring.get_keyring", return_value=backend):
                migrated = migrate_plaintext_credentials(credentials_path=env_file)

                self.assertIn("OPENAI_API_KEY", migrated)
                self.assertIn("TAVILY_API_KEY", migrated)
                self.assertNotIn("NON_SECRET_FLAG", migrated)

                self.assertEqual(backend.get_password(SERVICE, "openai_api_key"), "sk-env-openai-123")
                self.assertEqual(backend.get_password(SERVICE, "tavily_api_key"), "tvly-secret-999")

                scrubbed = env_file.read_text(encoding="utf-8")
                self.assertNotIn("sk-env-openai-123", scrubbed)
                self.assertNotIn("tvly-secret-999", scrubbed)
                self.assertIn("OPENAI_API_KEY=keychain", scrubbed)
                self.assertIn("NON_SECRET_FLAG=true", scrubbed)

                mode = stat.S_IMODE(env_file.stat().st_mode)
                self.assertEqual(mode, 0o600)

    def test_migrate_plaintext_credentials_when_keychain_unavailable(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "config.yaml"
            original = "OPENAI_API_KEY: 'sk-plaintext-preserved'\n"
            config_file.write_text(original, encoding="utf-8")

            with patch("harness_core.storage.vault_keychain.is_keychain_available", return_value=False):
                migrated = migrate_plaintext_credentials(config_path=config_file)
                self.assertEqual(migrated, {})
                # Content must remain intact without errors
                self.assertEqual(config_file.read_text(encoding="utf-8"), original)
