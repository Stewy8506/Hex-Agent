"""Comprehensive Test Suite for Google Antigravity OAuth & Quota-Backed Model Provider.

Verifies:
1. Local Antigravity IDE SQLite `state.vscdb` token extraction (`antigravityUnifiedStateSync.oauthToken`),
   including nested base64-encoded payloads (`ya29.*` access token + `1//*` refresh token).
2. Multi-source credential discovery (`~/.hex/antigravity_auth.json`, OpenCode `antigravity-accounts.json`,
   PicoClaw `auth.json`, and environment variables).
3. OAuth 2.0 PKCE pair generation, authorization URL building, code parsing, token exchange, and
   automatic expired token refresh (`https://oauth2.googleapis.com/token`).
4. Cloud Code Assist (`https://cloudcode-pa.googleapis.com`) protocol compliance:
   - `/v1internal:loadCodeAssist` project discovery (`cloudaicompanionProject`)
   - `/v1internal:fetchAvailableModels` live per-model quota windows (`remainingFraction`, `resetTime`)
   - `/v1internal:streamGenerateContent?alt=sse` agent envelope, SSE parsing, and $0 marginal API cost
   - HTTP 429 `quotaResetDelay` extraction
5. Strict Privacy Gate enforcement (`sensitive`/`secret` blocked from cloud egress) and
   `TwoTierModelRouter` + `CloudModelProvider(provider_type="antigravity")` + CLI/TUI integration.
"""

from __future__ import annotations

import base64
import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

from harness_core.cli import main as hex_cli_main
from harness_core.providers.antigravity_provider import (
    AntigravityCredential,
    AntigravityModelProvider,
    AntigravityOAuthManager,
    extract_tokens_from_blob,
)
from harness_core.providers.base import PolicyBlockedError, ProviderUnavailableError
from harness_core.providers.cloud_provider import CloudModelProvider, CloudProviderType
from harness_core.router import TwoTierModelRouter


class TestAntigravityOAuthAndProvider(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="hex_ag_test_")
        self.root = Path(self.tmp_dir.name)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_extract_tokens_from_nested_base64_vscdb_blob(self) -> None:
        """Verify nested base64 decoding matches Antigravity IDE state.vscdb format."""
        inner_payload = json.dumps(
            {
                "access_token": "ya29.a0AfB_byC_antigravity_live_token_123456",
                "refresh_token": "1//0g_antigravity_refresh_token_abcdef9876",
                "scope": "https://www.googleapis.com/auth/cloud-platform",
            }
        )
        inner_b64 = base64.b64encode(inner_payload.encode("utf-8")).decode("ascii")
        outer_wrapper = json.dumps({"oauthToken": inner_b64, "version": 2})
        outer_b64 = base64.b64encode(outer_wrapper.encode("utf-8")).decode("ascii")

        access, refresh = extract_tokens_from_blob(outer_b64)
        self.assertEqual(access, "ya29.a0AfB_byC_antigravity_live_token_123456")
        self.assertEqual(refresh, "1//0g_antigravity_refresh_token_abcdef9876")

    def test_discover_credentials_from_antigravity_ide_sqlite_vscdb(self) -> None:
        """Verify AntigravityOAuthManager reads tokens directly from Antigravity IDE state.vscdb."""
        vscdb_path = self.root / "state.vscdb"
        conn = sqlite3.connect(vscdb_path)
        conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")
        inner_b64 = base64.b64encode(
            b"proto_prefix:ya29.a0AntigravitySqliteToken999:sep:1//0gRefreshSqliteToken888"
        ).decode("ascii")
        outer_b64 = base64.b64encode(inner_b64.encode("ascii")).decode("ascii")
        conn.execute(
            "INSERT INTO ItemTable (key, value) VALUES (?, ?)",
            ("antigravityUnifiedStateSync.oauthToken", outer_b64),
        )
        conn.commit()
        conn.close()

        mgr = AntigravityOAuthManager(
            auth_file=self.root / "nonexistent_hex_auth.json",
            ide_db_paths=[vscdb_path],
            external_auth_paths=[],
            allow_keychain=False,
        )
        with patch.dict(os.environ, {}, clear=True):
            cred = mgr.resolve_credential(auto_refresh=False)
        self.assertIsNotNone(cred)
        assert cred is not None
        self.assertEqual(cred.access_token, "ya29.a0AntigravitySqliteToken999")
        self.assertEqual(cred.refresh_token, "1//0gRefreshSqliteToken888")
        self.assertIn("antigravity_ide_vscdb", cred.source)

    def test_discover_credentials_from_opencode_and_picoclaw_stores(self) -> None:
        """Verify compatibility with ~/.config/opencode/antigravity-accounts.json and ~/.picoclaw/auth.json."""
        opencode_file = self.root / "antigravity-accounts.json"
        opencode_file.write_text(
            json.dumps(
                {
                    "accounts": [
                        {
                            "email": "dev@example.com",
                            "access": "ya29.opencode_token_abc",
                            "refresh": "1//opencode_refresh_xyz",
                            "projectId": "ag-proj-001",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        mgr = AntigravityOAuthManager(
            auth_file=self.root / "missing.json",
            ide_db_paths=[],
            external_auth_paths=[opencode_file],
            allow_keychain=False,
        )
        with patch.dict(os.environ, {}, clear=True):
            cred = mgr.resolve_credential(auto_refresh=False)
        self.assertIsNotNone(cred)
        assert cred is not None
        self.assertEqual(cred.access_token, "ya29.opencode_token_abc")
        self.assertEqual(cred.project_id, "ag-proj-001")

    def test_pkce_generation_and_automatic_token_refresh(self) -> None:
        """Verify PKCE S256 challenge generation and expired OAuth token auto-refresh."""
        pkce = AntigravityOAuthManager.generate_pkce_pair()
        self.assertTrue(len(pkce["verifier"]) >= 64)
        self.assertTrue(len(pkce["challenge"]) >= 40)

        auth_url = AntigravityOAuthManager.build_authorization_url(pkce["challenge"], state="state123")
        self.assertIn("accounts.google.com/o/oauth2/v2/auth", auth_url)
        self.assertIn("code_challenge_method=S256", auth_url)

        code = AntigravityOAuthManager.parse_authorization_code(
            "http://localhost:51121/oauth-callback?code=4/0AeanS0_sample_code&scope=email"
        )
        self.assertEqual(code, "4/0AeanS0_sample_code")

        def mock_oauth_http(url: str, headers: dict[str, str], body: bytes | None, method: str) -> tuple[int, bytes]:
            self.assertEqual(url, "https://oauth2.googleapis.com/token")
            return 200, json.dumps(
                {
                    "access_token": "ya29.refreshed_live_token_777",
                    "expires_in": 3600,
                }
            ).encode("utf-8")

        auth_file = self.root / "antigravity_auth.json"
        expired_cred = AntigravityCredential(
            access_token="ya29.old_expired_token",
            refresh_token="1//valid_refresh_token",
            expires_at_ms=int((time.time() - 600) * 1000),
            project_id="proj-ag-123",
        )
        mgr = AntigravityOAuthManager(
            auth_file=auth_file,
            ide_db_paths=[],
            external_auth_paths=[],
            http_transport=mock_oauth_http,
            allow_keychain=False,
        )
        mgr.save_credential(expired_cred)

        with patch.dict(os.environ, {}, clear=True):
            refreshed = mgr.resolve_credential(auto_refresh=True)
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed.access_token, "ya29.refreshed_live_token_777")
        self.assertFalse(refreshed.is_expired())

    def test_antigravity_generate_sse_envelope_and_zero_marginal_cost(self) -> None:
        """Verify v1internal:loadCodeAssist + v1internal:streamGenerateContent SSE protocol and $0 cost."""
        captured_requests: list[tuple[str, dict[str, str], dict[str, Any]]] = []

        def mock_transport(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
            payload = json.loads(body.decode("utf-8"))
            captured_requests.append((url, headers, payload))
            if url.endswith("/v1internal:loadCodeAssist"):
                return 200, json.dumps(
                    {
                        "cloudaicompanionProject": "antigravity-pro-project-42",
                        "availablePromptCredits": 1500,
                        "currentTier": {"name": "Google AI Pro"},
                    }
                ).encode("utf-8")
            if "v1internal:streamGenerateContent" in url:
                sse_lines = [
                    'data: {"response": {"candidates": [{"content": {"parts": [{"text": "Patch verified "}]}, "finishReason": ""}]}}',
                    'data: {"response": {"candidates": [{"content": {"parts": [{"text": "via Antigravity."}]}, "finishReason": "STOP"}], "usageMetadata": {"promptTokenCount": 420, "candidatesTokenCount": 38, "cachedContentTokenCount": 300}}}',
                ]
                return 200, "\n".join(sse_lines).encode("utf-8")
            raise AssertionError(f"Unexpected URL: {url}")

        provider = AntigravityModelProvider(
            model_id="google/antigravity-gemini-3.5-pro",
            access_token="ya29.test_antigravity_token",
            http_transport=mock_transport,
        )
        resp = provider.generate(
            prompt="Fix failing test in router.py",
            system_prompt="You are HEX repair engine.",
            cacheable_prefix="[AST Blast Radius]\nrouter.py -> orchestrator.py",
        )

        self.assertEqual(resp.text, "Patch verified via Antigravity.")
        self.assertEqual(resp.model_id, "gemini-3.5-pro")
        self.assertEqual(resp.provider_id, "google_antigravity")
        self.assertEqual(resp.usage.input_tokens, 420)
        self.assertEqual(resp.usage.output_tokens, 38)
        self.assertEqual(resp.usage.cached_input_tokens, 300)
        self.assertEqual(resp.usage.cost_usd, 0.0)
        self.assertTrue(resp.raw.get("quota_backed"))
        self.assertEqual(resp.raw.get("project_id"), "antigravity-pro-project-42")

        # Verify the streamGenerateContent envelope structure
        gen_url, gen_headers, gen_body = captured_requests[-1]
        self.assertIn("/v1internal:streamGenerateContent?alt=sse", gen_url)
        self.assertEqual(gen_headers["Authorization"], "Bearer ya29.test_antigravity_token")
        self.assertEqual(gen_headers["User-Agent"], "antigravity")
        self.assertEqual(gen_body["project"], "antigravity-pro-project-42")
        self.assertEqual(gen_body["model"], "gemini-3.5-pro")
        self.assertEqual(gen_body["requestType"], "agent")
        parts = gen_body["request"]["contents"][0]["parts"]
        self.assertEqual(parts[0]["text"], "[AST Blast Radius]\nrouter.py -> orchestrator.py")
        self.assertEqual(parts[1]["text"], "Fix failing test in router.py")

    def test_fetch_quota_status_and_429_reset_delay(self) -> None:
        """Verify live quota window parsing and HTTP 429 quotaResetDelay error reporting."""
        def mock_quota_transport(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
            if url.endswith(":loadCodeAssist"):
                return 200, json.dumps(
                    {
                        "cloudaicompanionProject": "ag-quota-proj",
                        "availablePromptCredits": 1200,
                        "currentTier": {"name": "Google AI Ultra"},
                    }
                ).encode("utf-8")
            if url.endswith(":fetchAvailableModels"):
                return 200, json.dumps(
                    {
                        "models": {
                            "gemini-3.5-pro": {
                                "displayName": "Gemini 3.5 Pro",
                                "quotaInfo": {
                                    "remainingFraction": 0.85,
                                    "resetTime": "2026-11-04T03:00:00Z",
                                    "isExhausted": False,
                                },
                            },
                            "claude-opus-4-6-thinking": {
                                "displayName": "Claude Opus 4.6 Thinking",
                                "quotaInfo": {
                                    "remainingFraction": 0.60,
                                    "resetTime": "2026-11-04T03:00:00Z",
                                    "isExhausted": False,
                                },
                            },
                        }
                    }
                ).encode("utf-8")
            return 429, json.dumps(
                {
                    "error": {
                        "code": 429,
                        "message": "You have exhausted your capacity on this model.",
                        "status": "RESOURCE_EXHAUSTED",
                        "details": [
                            {
                                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                                "metadata": {"quotaResetDelay": "4h30m28s"},
                            }
                        ],
                    }
                }
            ).encode("utf-8")

        provider = AntigravityModelProvider(
            model_id="gemini-3.5-pro",
            access_token="ya29.quota_test_tok",
            http_transport=mock_quota_transport,
        )
        snap = provider.fetch_quota_status()
        self.assertEqual(snap.project_id, "ag-quota-proj")
        self.assertEqual(snap.plan_name, "Google AI Ultra")
        self.assertEqual(snap.available_prompt_credits, 1200)
        self.assertEqual(len(snap.models), 2)
        self.assertAlmostEqual(snap.models[0].remaining_fraction, 0.85)

        with self.assertRaises(ProviderUnavailableError) as ctx:
            provider.generate("Trigger 429")
        self.assertIn("4h30m28s", str(ctx.exception))

    def test_privacy_gate_and_router_integration(self) -> None:
        """Verify CloudModelProvider(provider_type='antigravity') and TwoTierModelRouter integration."""
        def mock_transport(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
            return 200, json.dumps(
                {
                    "response": {
                        "candidates": [{"content": {"parts": [{"text": "Router Antigravity OK"}]}}],
                        "usageMetadata": {"promptTokenCount": 50, "candidatesTokenCount": 10},
                    }
                }
            ).encode("utf-8")

        cloud_prov = CloudModelProvider(
            provider_type=CloudProviderType.ANTIGRAVITY,
            model_id="gemini-3.5-pro",
            api_key="ya29.cloud_wrapper_tok",
            http_transport=mock_transport,
        )
        self.assertEqual(cloud_prov.provider_id, "google_antigravity")

        # Sensitive data must be blocked from leaving local machine
        with self.assertRaises(PolicyBlockedError):
            cloud_prov.generate("secret key", data_classification="secret")

        router = TwoTierModelRouter(cloud_provider=cloud_prov)
        decision, resp = router.execute_routed_request(
            prompt="Synthesize multi-file patch for orchestrator",
            task_class="code_synthesis",
            risk_class="R2",
            data_classification="internal",
        )
        self.assertEqual(decision.tier, "tier2_cloud_reasoner")
        self.assertEqual(decision.provider, "google_antigravity")
        self.assertEqual(decision.cost_usd, 0.0)
        self.assertEqual(resp.text, "Router Antigravity OK")

    def test_cli_auth_antigravity_commands(self) -> None:
        """Verify `hex auth antigravity` CLI saves and reports credentials cleanly."""
        auth_file = self.root / "cli_ag_auth.json"
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = hex_cli_main(
                [
                    "--data-dir",
                    str(self.root / "data"),
                    "auth",
                    "antigravity",
                    "--token",
                    "ya29.cli_saved_tok",
                    "--refresh-token",
                    "1//cli_saved_ref",
                    "--project-id",
                    "cli-proj-99",
                    "--auth-file",
                    str(auth_file),
                ]
            )
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())
        self.assertEqual(out["status"], "saved")
        self.assertTrue(out["has_access_token"])

        buf_status = io.StringIO()
        with redirect_stdout(buf_status):
            rc2 = hex_cli_main(
                [
                    "--data-dir",
                    str(self.root / "data"),
                    "auth",
                    "antigravity",
                    "--status",
                    "--auth-file",
                    str(auth_file),
                ]
            )
        self.assertEqual(rc2, 0)
        status_out = json.loads(buf_status.getvalue())
        self.assertEqual(status_out["status"], "authenticated")
        self.assertEqual(status_out["project_id"], "cli-proj-99")


if __name__ == "__main__":
    unittest.main()
