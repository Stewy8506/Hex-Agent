"""Unit tests for Setup Ergonomics, Curated Provider Tiers, and First-Task Verification."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from harness_core.config_loader import parse_config_yaml, parse_env_file
from harness_core.storage.db import LedgerStore
from harness_core.client.onboard import run_first_task_verification, run_onboard_wizard


class TestOnboardingErgonomics(unittest.TestCase):
    """Test --quick, --blank-slate, --reset, curated tiers, and first-task verification beat."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_quick_mode_skips_when_all_configured(self) -> None:
        """When provider, search, and messaging are already configured, --quick exits early."""
        cfg_file = self.root / "config.yaml"
        cred_file = self.root / "credentials.env"
        cfg_file.write_text(
            """version: "1.0"
model:
  profile: "cloud"
  cloud_provider: "anthropic"
  cloud_model_id: "claude-3-7-sonnet"
  compressor_mode: "deterministic"
  reasoning_effort: "high"
web_search:
  provider: "tavily"
messaging:
  telegram_enabled: true
  discord_enabled: false
storage:
  backend: "sqlite"
  data_dir: "{data_dir}"
cow:
  backend: "auto"
sandbox:
  backend: "auto"
webhooks:
  dry_run: true
""".replace("{data_dir}", str(self.root / "data")),
            encoding="utf-8",
        )
        cred_file.write_text(
            "HEX_CLOUD_PROVIDER=anthropic\nHEX_CLOUD_MODEL_ID=claude-3-7-sonnet\nHEX_WEB_SEARCH_PROVIDER=tavily\nHEX_TELEGRAM_BOT_TOKEN=123:ABC\n",
            encoding="utf-8",
        )

        res = run_onboard_wizard(
            config_dir=self.root,
            quick=True,
            non_interactive=True,
            first_task_beat=False,
        )
        self.assertEqual(res.get("status"), "ready")
        self.assertEqual(res.get("mode"), "quick")
        self.assertEqual(res.get("cloud_provider"), "anthropic")
        self.assertEqual(res.get("cloud_model_id"), "claude-3-7-sonnet")

    def test_blank_slate_mode_sets_minimal_profile(self) -> None:
        """--blank-slate initializes minimal agent footprint with zero overhead."""
        res = run_onboard_wizard(
            config_dir=self.root,
            blank_slate=True,
            non_interactive=True,
            first_task_beat=False,
        )
        self.assertEqual(res.get("status"), "ready")

        cfg_file = self.root / "config.yaml"
        self.assertTrue(cfg_file.is_file())
        raw_yaml = cfg_file.read_text(encoding="utf-8")
        self.assertIn("essential_tools_only: true", raw_yaml)
        self.assertIn("telemetry: false", raw_yaml)
        self.assertIn("max_turns: 90", raw_yaml)

        parsed_cfg = parse_config_yaml(cfg_file)
        self.assertEqual(parsed_cfg["model"]["compressor_mode"], "deterministic")
        self.assertEqual(parsed_cfg["web_search"]["provider"], "none")
        self.assertIn(str(parsed_cfg["messaging"]["telegram_enabled"]).lower(), ("false", "0"))
        self.assertIn(str(parsed_cfg["messaging"]["discord_enabled"]).lower(), ("false", "0"))

    def test_reset_flag_cleans_files_and_resets(self) -> None:
        """--reset cleanly wipes existing config and credentials before executing setup."""
        cfg_file = self.root / "config.yaml"
        cred_file = self.root / "credentials.env"
        cfg_file.write_text("dummy: true", encoding="utf-8")
        cred_file.write_text("DUMMY_KEY=123", encoding="utf-8")

        res = run_onboard_wizard(
            config_dir=self.root,
            reset=True,
            auto_mode=True,
        )
        self.assertEqual(res.get("status"), "success")

        # Confirm new files replaced dummy content
        parsed = parse_config_yaml(cfg_file)
        self.assertIn("model", parsed)
        self.assertNotIn("dummy", parsed)

    def test_first_task_verification_beat_execution(self) -> None:
        """run_first_task_verification generates diagnostic artifact, CoW workspace, and records ledger event."""
        target_diag = self.root / "custom_first_task"
        res = run_first_task_verification(
            config_dir=self.root,
            target_dir=target_diag,
        )
        self.assertEqual(res.get("status"), "passed")
        self.assertTrue(res.get("task_id", "").startswith("tsk_"))

        # 1. CoW workspace validation (real clone + isolation + rollback probe)
        ws_path = Path(res["cow_workspace"])
        self.assertTrue(ws_path.is_dir())
        self.assertTrue((ws_path / ".hex_workspace").is_file())
        self.assertTrue((ws_path / "marker.txt").is_file())
        self.assertFalse((ws_path / "dirty_probe.txt").exists())
        self.assertNotEqual(res.get("cow_backend"), "unknown")
        cow_check = next(c for c in res["checks"] if c["name"] == "cow_isolation")
        self.assertEqual(cow_check["status"], "passed")

        # 2. Diagnostic report validation reflects real check outcomes
        art_path = Path(res["artifact_path"])
        self.assertTrue(art_path.is_file())
        report_text = art_path.read_text(encoding="utf-8")
        self.assertIn("HEX Agent System Diagnostic", report_text)
        self.assertIn("ALL VERIFIED SUBSYSTEMS OPERATIONAL", report_text)
        self.assertIn("Subsystem Check Results", report_text)

        # 3. CAS blob validation
        sha256 = res["cas_sha256"]
        cas_blob = self.root / "data" / "cas" / sha256
        cas_art_blob = self.root / "data" / "cas_artifacts" / sha256
        self.assertTrue(cas_blob.is_file())
        self.assertTrue(cas_art_blob.is_file())
        self.assertEqual(cas_blob.read_bytes(), art_path.read_bytes())

        # 4. Ledger DB journal validation
        ledger_db = self.root / "data" / "ledger.db"
        self.assertTrue(ledger_db.is_file())
        ledger = LedgerStore(ledger_db)
        try:
            task = ledger.get_task(res["task_id"])
            self.assertIsNotNone(task)
            self.assertEqual(task.get("status"), "SUCCEEDED")
            arts = ledger.list_artifacts(res["task_id"])
            self.assertTrue(any(a.get("sha256") == sha256 for a in arts))
        finally:
            ledger.close()

        # 5. Provider connectivity is honestly skipped, chain actually verified
        provider_check = next(c for c in res["checks"] if c["name"] == "provider_connectivity")
        self.assertEqual(provider_check["status"], "skipped")
        self.assertGreater(res.get("chain_verified_events", 0), 0)

    def test_first_task_verification_preserves_marker_after_overlay_cleanup(self) -> None:
        from unittest.mock import patch

        from harness_core.storage.cow import OverlayFSSnapshotProvider

        provider = OverlayFSSnapshotProvider(
            self.root / "data" / "cow_snapshots", simulate=True, mode="fuse",
        )
        real_unmount = provider._unmount

        def unmount(merged):
            real_unmount(merged)
            for entry in Path(merged).iterdir():
                if entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()

        with patch(
            "harness_core.storage.cow.get_default_snapshot_provider", return_value=provider,
        ), patch.object(provider, "_unmount", side_effect=unmount) as unmount_mock:
            res = run_first_task_verification(
                config_dir=self.root,
                target_dir=self.root / "ft_overlay",
            )

        self.assertEqual(res["status"], "passed")
        self.assertEqual(unmount_mock.call_count, 2)
        self.assertFalse(provider._active_mounts)
        workspace = Path(res["cow_workspace"])
        self.assertEqual((workspace / "marker.txt").read_text(encoding="utf-8"), "hex-first-task-seed")
        self.assertTrue((workspace / ".hex_workspace").is_file())
        self.assertFalse((workspace / "dirty_probe.txt").exists())

    def test_first_task_verification_fails_when_required_tool_missing(self) -> None:
        """Missing git must fail the beat honestly instead of reporting PASS."""
        from unittest.mock import patch

        real_which = shutil.which

        def fake_which(name: str, *args, **kwargs):
            if name == "git":
                return None
            return real_which(name, *args, **kwargs)

        with patch("harness_core.client.onboard.shutil.which", side_effect=fake_which):
            res = run_first_task_verification(
                config_dir=self.root,
                target_dir=self.root / "ft_missing_git",
            )
        self.assertEqual(res["status"], "failed")
        devtools = next(c for c in res["checks"] if c["name"] == "devtools")
        self.assertEqual(devtools["status"], "failed")
        self.assertIn("git", devtools["detail"])
        report_text = Path(res["artifact_path"]).read_text(encoding="utf-8")
        self.assertIn("VERIFICATION FAILED", report_text)
        self.assertNotIn("ALL VERIFIED SUBSYSTEMS OPERATIONAL", report_text)
        ledger = LedgerStore(self.root / "data" / "ledger.db")
        try:
            self.assertEqual(ledger.get_task(res["task_id"]).get("status"), "FAILED")
        finally:
            ledger.close()

    def test_first_task_verification_fails_when_cow_probe_fails(self) -> None:
        """A broken CoW provider must fail the beat and mark the ledger task FAILED."""
        from unittest.mock import patch

        def broken_provider(*args, **kwargs):
            raise RuntimeError("simulated CoW backend failure")

        with patch(
            "harness_core.storage.cow.get_default_snapshot_provider",
            side_effect=broken_provider,
        ):
            res = run_first_task_verification(
                config_dir=self.root,
                target_dir=self.root / "ft_broken_cow",
            )
        self.assertEqual(res["status"], "failed")
        cow_check = next(c for c in res["checks"] if c["name"] == "cow_isolation")
        self.assertEqual(cow_check["status"], "failed")
        self.assertIn("simulated CoW backend failure", cow_check["detail"])
        ledger = LedgerStore(self.root / "data" / "ledger.db")
        try:
            self.assertEqual(ledger.get_task(res["task_id"]).get("status"), "FAILED")
        finally:
            ledger.close()

    def test_first_task_verification_fails_when_chain_invalid(self) -> None:
        """Even with all probes passing, an invalid hash chain must block SUCCEEDED."""
        from unittest.mock import patch

        from harness_core.storage.db import ChainIntegrityReport

        with patch.object(
            LedgerStore,
            "verify_chain_integrity",
            return_value=ChainIntegrityReport(valid=False, errors=["simulated tamper"], verified_events=0),
        ):
            res = run_first_task_verification(
                config_dir=self.root,
                target_dir=self.root / "ft_bad_chain",
            )
        self.assertEqual(res["status"], "failed")
        ledger_check = next(c for c in res["checks"] if c["name"] == "ledger")
        self.assertEqual(ledger_check["status"], "failed")
        self.assertIn("simulated tamper", ledger_check["detail"])
        ledger = LedgerStore(self.root / "data" / "ledger.db")
        try:
            self.assertEqual(ledger.get_task(res["task_id"]).get("status"), "FAILED")
        finally:
            ledger.close()

    def test_curated_provider_tiers_coverage(self) -> None:
        """Ensure all 4 curated tiers and flat list preserve all 26 supported providers."""
        from harness_core.providers.registry import list_providers

        all_specs = list_providers()
        self.assertGreaterEqual(len(all_specs), 20)

        recommended_tier = {"antigravity", "anthropic", "openai", "gemini"}
        gateways_tier = {"openrouter", "ai-gateway"}
        local_tier = {"ollama", "lmstudio", "local", "custom"}
        enterprise_tier = {
            "deepseek", "groq", "cerebras", "xai", "together", "mistral",
            "fireworks", "nvidia", "alibaba", "minimax", "kimi", "zai",
            "azure", "bedrock", "vertex", "huggingface",
        }

        combined_tiered = recommended_tier | gateways_tier | local_tier | enterprise_tier
        self.assertEqual(len(combined_tiered), 26)

        # Confirm target_provider bypasses tier prompts cleanly
        res = run_onboard_wizard(
            config_dir=self.root,
            target_provider="deepseek",
            target_model="deepseek-reasoner",
            non_interactive=True,
            first_task_beat=False,
        )
        self.assertEqual(res.get("cloud_provider"), "deepseek")
        self.assertEqual(res.get("cloud_model_id"), "deepseek-reasoner")


if __name__ == "__main__":
    unittest.main()
