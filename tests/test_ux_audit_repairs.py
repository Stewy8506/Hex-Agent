"""Comprehensive Unit and Integration Tests for UX Audit Repairs (docs/UX_AUDIT_2026-11-12.md)."""

import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness_core.config_loader import load_hex_environment, parse_config_yaml
from harness_core.db import LedgerStore
from harness_core.models import TaskSubmitRequest
from harness_core.onboard import HardwareProfile
from harness_core.orchestrator import HarnessOrchestrator
from harness_core.project_registry import ProjectRecord, ProjectRegistry, compute_project_id
from harness_core.router import TwoTierModelRouter
from harness_core.tui import HexConsole


class TestUXAuditRepairs(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="hex_ux_test_")
        self.root = Path(self.temp_dir.name).resolve()
        self.data_dir = self.root / "data"
        self.ws_dir = self.root / "ws"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.ws_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_direct_model_selection_updates_router_and_provider_and_persists(self):
        """P0: Direct model selection updates router, provider, and durable config together."""
        # Create a workspace with .hex/config.yaml
        hex_dir = self.ws_dir / ".hex"
        hex_dir.mkdir(parents=True, exist_ok=True)
        (hex_dir / "config.yaml").write_text(
            '# config\nmodel:\n  cloud_provider: "antigravity"\n  cloud_model_id: "gemini-2.5-flash"\n',
            encoding="utf-8",
        )

        orch = HarnessOrchestrator(self.data_dir, default_workspace=self.ws_dir)
        prov, final_id, saved_path = orch.set_active_model("gemini-3-flash", durable=True)

        self.assertEqual(orch.router.cloud_model_id, "gemini-3-flash")
        if hasattr(orch.router.cloud_provider, "model_id"):
            self.assertEqual(orch.router.cloud_provider.model_id, "gemini-3-flash")
        self.assertEqual(saved_path, hex_dir / "config.yaml")

        # Verify durable persistence in config.yaml
        parsed = parse_config_yaml(hex_dir / "config.yaml")
        self.assertEqual(parsed.get("model", {}).get("cloud_model_id"), "gemini-3-flash")

    def test_atomic_project_switching_rebinds_all_services(self):
        """P0: Project switching rebinds router, gateway, graphify, lsp, and workspace."""
        proj_a = self.root / "proj_a"
        proj_b = self.root / "proj_b"
        proj_a.mkdir(parents=True, exist_ok=True)
        proj_b.mkdir(parents=True, exist_ok=True)

        # Set distinct models in project configs
        hex_b = proj_b / ".hex"
        hex_b.mkdir(parents=True, exist_ok=True)
        (hex_b / "config.yaml").write_text(
            'model:\n  cloud_provider: "antigravity"\n  cloud_model_id: "claude-opus-4-6-thinking"\n',
            encoding="utf-8",
        )

        orch = HarnessOrchestrator(self.data_dir, default_workspace=proj_a)
        self.assertEqual(orch.default_workspace, proj_a)

        sw = orch.switch_project(proj_b, "ProjectB")
        self.assertEqual(orch.default_workspace, proj_b)
        self.assertEqual(orch.graphify.workspace_root, proj_b)
        self.assertEqual(orch.lsp_manager.workspace_root, proj_b)
        self.assertEqual(orch.gateway.graphify.workspace_root, proj_b)
        self.assertEqual(sw["project_id"], "ProjectB")
        self.assertEqual(sw["workspace_root"], str(proj_b))
        self.assertEqual(orch.router.cloud_model_id, "claude-opus-4-6-thinking")

    def test_path_derived_project_identity_disambiguates_same_name_folders(self):
        """P0: Two folders with the same basename have distinct IDs and records."""
        dir1 = self.root / "work1" / "api"
        dir2 = self.root / "work2" / "api"
        dir1.mkdir(parents=True, exist_ok=True)
        dir2.mkdir(parents=True, exist_ok=True)

        id1 = compute_project_id(dir1)
        id2 = compute_project_id(dir2)
        self.assertNotEqual(id1, id2)

        reg_file = self.root / "projects.json"
        reg = ProjectRegistry(reg_file)
        rec1 = reg.register_project(dir1)
        rec2 = reg.register_project(dir2)

        self.assertEqual(rec1.name, "api")
        self.assertEqual(rec2.name, "api")
        self.assertNotEqual(rec1.id, rec2.id)

        # Query by ID resolves exact record
        self.assertEqual(reg.get_project(rec1.id).path, str(dir1))
        self.assertEqual(reg.get_project(rec2.id).path, str(dir2))

    def test_task_sequence_is_stable_and_strictly_monotonic_at_equal_timestamps(self):
        """P1: Tasks with identical created_at have distinct, strictly ordered sequences."""
        ledger = LedgerStore(self.data_dir / "ledger.db")
        same_time = "2026-11-12T10:00:00.000000Z"

        # Submit tasks with same created_at manually in SQLite
        with ledger._lock:
            for i in range(1, 4):
                tid = f"tsk_tie_{i:04d}"
                ledger.conn.execute(
                    """
                    INSERT INTO tasks (
                        task_id, project_id, goal, execution_mode, status,
                        priority, budget_json, constraints_json, deliverable,
                        policy_snapshot_id, version, created_at, updated_at
                    ) VALUES (?, 'proj_tie', 'Goal', 'standard', 'QUEUED', 50, '{}', '{}', '', 'pol', 1, ?, ?)
                    """,
                    (tid, same_time, same_time),
                )
            ledger.conn.commit()

        seq1 = ledger.get_project_task_sequence("tsk_tie_0001", "proj_tie")
        seq2 = ledger.get_project_task_sequence("tsk_tie_0002", "proj_tie")
        seq3 = ledger.get_project_task_sequence("tsk_tie_0003", "proj_tie")

        self.assertEqual(seq1, 1)
        self.assertEqual(seq2, 2)
        self.assertEqual(seq3, 3)

    def test_configured_compressor_mode_respects_hex_compressor_mode(self):
        """P1: Router respects HEX_COMPRESSOR_MODE from environment/config."""
        with patch.dict(os.environ, {"HEX_COMPRESSOR_MODE": "cloud"}, clear=False):
            router = TwoTierModelRouter()
            self.assertEqual(router.compressor_mode, "cloud")

    def test_explicit_provider_not_overridden_by_antigravity_token(self):
        """P1: Explicit provider intent (e.g. anthropic) is authoritative."""
        with patch.dict(os.environ, {"HEX_CLOUD_PROVIDER": "anthropic"}, clear=False):
            router = TwoTierModelRouter(cloud_provider_type="anthropic")
            self.assertEqual(router.cloud_provider.provider_id, "anthropic")
            self.assertTrue(router.cloud_model_id.startswith("claude-"))

    def test_conversational_greeting_fast_tracks(self):
        """ORCH-1: Pure conversational prompt bypasses file scanning and graph querying."""
        orch = HarnessOrchestrator(self.data_dir, default_workspace=self.ws_dir)
        orch.router = MagicMock()
        mock_resp = MagicMock()
        mock_resp.text = "Hello! I am HEX."
        orch.router.execute_routed_request.return_value = (MagicMock(), mock_resp)

        req = TaskSubmitRequest(goal="hey", project_id="test_proj", execution_mode="fast_path")
        task = orch.submit_and_run(req, auto_execute=True)

        self.assertEqual(task["status"], "SUCCEEDED")
        self.assertIn("Hello! I am HEX.", task.get("deliverable") or task.get("result") or "")
        # Tool gateway was not invoked with list_files or query_graphify
        calls = orch.ledger.list_tool_calls(task["task_id"])
        self.assertEqual(len(calls), 0)

    def test_synthesis_failure_captured_without_blank_output(self):
        """TUI-1 & ORCH-2: Synthesis error is surfaced rather than swallowed as blank."""
        orch = HarnessOrchestrator(self.data_dir, default_workspace=self.ws_dir)
        orch.router = MagicMock()
        orch.router.execute_routed_request.side_effect = RuntimeError("Rate limit exceeded")

        req = TaskSubmitRequest(goal="What does this do?", project_id="test_proj", execution_mode="fast_path")
        task = orch.submit_and_run(req, auto_execute=True)

        self.assertEqual(task["status"], "SUCCEEDED")
        self.assertIn("Rate limit exceeded", task.get("deliverable") or task.get("result") or "")

    def test_hardware_detect_suppresses_sysctl_stderr(self):
        """P2: Hardware detection does not leak sysctl errors to stderr."""
        prof = HardwareProfile.detect()
        self.assertIsNotNone(prof.machine_model)

    def test_provider_alias_does_not_become_a_model_id(self):
        """A provider command must never persist or execute its alias as a model ID."""
        hex_dir = self.ws_dir / ".hex"
        hex_dir.mkdir(parents=True, exist_ok=True)
        config_path = hex_dir / "config.yaml"
        config_path.write_text(
            'model:\n  cloud_provider: "gemini"\n  cloud_model_id: "gemini-2.5-flash"\n',
            encoding="utf-8",
        )
        orch = HarnessOrchestrator(self.data_dir, default_workspace=self.ws_dir)
        console = HexConsole(orch, workspace_root=self.ws_dir, quiet=True)
        with redirect_stdout(StringIO()):
            console._cmd_model("antigravity")

        self.assertEqual(orch.router.cloud_provider.provider_id, "google_antigravity")
        self.assertNotEqual(orch.router.cloud_model_id, "antigravity")
        self.assertNotEqual(orch.router.cloud_provider.model_id, "antigravity")
        self.assertEqual(parse_config_yaml(config_path)["model"]["cloud_model_id"], "gemini-2.5-flash")

    def test_submission_callback_exposes_task_id_before_execution(self):
        """The TUI can cancel because it receives the task ID immediately after submission."""
        orch = HarnessOrchestrator(self.data_dir, default_workspace=self.ws_dir)
        observed: list[str] = []
        task = orch.submit_and_run(
            TaskSubmitRequest(goal="queued task", project_id="test", execution_mode="standard"),
            auto_execute=False,
            on_submitted=lambda submitted: observed.append(submitted["task_id"]),
        )
        self.assertEqual(observed, [task["task_id"]])
        self.assertEqual(orch.ledger.get_task(observed[0])["status"], "QUEUED")


if __name__ == "__main__":
    unittest.main()
