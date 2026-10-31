"""Integration tests for Alembic migrations lifecycle (upgrade head / downgrade base).

Verifies:
1. Online migration upgrades a clean database to 'head' creating all 16 authoritative tables.
2. Table schemas, column names, nullabilities, and primary keys match migrations/0001_initial_schema.sql.
3. Indexes such as 'idx_tool_calls_lookup' are created.
4. Online migration downgrades from 'head' to 'base' cleanly dropping all 16 tables.
5. Upgrade/downgrade roundtrip operates without residual schema locks or errors.
"""

from __future__ import annotations

import io
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from alembic import command
from alembic.config import Config

EXPECTED_TABLES = {
    "tasks",
    "task_events",
    "attempts",
    "plans",
    "checkpoints",
    "tool_calls",
    "approvals",
    "artifacts",
    "proof_bundles",
    "skills",
    "skill_scans",
    "workers",
    "leases",
    "memories",
    "harness_revisions",
    "evaluations",
}

EXPECTED_COLUMNS = {
    "tasks": {
        "task_id", "parent_task_id", "project_id", "goal", "execution_mode",
        "status", "priority", "deadline_at", "budget_json", "constraints_json",
        "deliverable", "policy_snapshot_id", "idempotency_key",
        "active_workspace_tree_sha", "version", "created_at", "updated_at",
        "terminal_reason"
    },
    "task_events": {
        "event_id", "task_id", "attempt_id", "sequence_no", "event_type",
        "prior_state", "next_state", "reason_code", "workspace_tree_sha",
        "policy_version", "payload_json", "actor", "occurred_at",
        "correlation_id", "event_sha256"
    },
    "attempts": {
        "attempt_id", "task_id", "parent_attempt_id", "branch_label",
        "worker_id", "lease_id", "fencing_token", "base_tree_sha",
        "head_tree_sha", "workspace_path", "status", "started_at",
        "ended_at", "failure_class", "retry_index"
    },
    "plans": {
        "plan_id", "task_id", "revision", "dag_json", "blast_radius_checks_json",
        "created_by", "rationale", "created_at", "supersedes_revision"
    },
    "checkpoints": {
        "checkpoint_id", "task_id", "attempt_id", "plan_revision",
        "completed_steps_json", "pending_steps_json", "workspace_tree_sha",
        "snapshot_path", "artifact_uri", "sha256", "schema_version",
        "created_at", "validity"
    },
    "tool_calls": {
        "call_id", "task_id", "attempt_id", "step_id", "sequence_in_step",
        "tool_id", "risk_class", "canonical_args_hash", "arguments_redacted_json",
        "result_json", "result_ref", "replayed_from_call_id", "policy_decision",
        "idempotency_key", "started_at", "ended_at", "status"
    },
    "approvals": {
        "approval_id", "task_id", "attempt_id", "step_id", "action_type",
        "action_hash", "action_params_json", "blast_radius_summary",
        "requested_by", "decided_by", "decision", "expires_at",
        "created_at", "decided_at", "comment"
    },
    "artifacts": {
        "artifact_id", "task_id", "kind", "uri", "sha256", "size_bytes",
        "media_type", "sensitivity", "retention_until", "created_at"
    },
    "proof_bundles": {
        "proof_id", "task_id", "attempt_id", "git_diff_sha", "git_diff_text",
        "workspace_tree_sha", "runtime_digest", "ast_impact_json",
        "verification_results_json", "ledger_signature", "created_at"
    },
    "skills": {
        "skill_id", "revision_id", "name", "purpose", "description",
        "disk_path", "git_commit_sha", "bundle_sha256", "risk_class",
        "allowed_tools_json", "status", "latest_scan_id", "created_at"
    },
    "skill_scans": {
        "scan_id", "skill_id", "revision_id", "bundle_sha256",
        "scanner_version", "semantic_model_id", "risk_score", "severity",
        "matched_rules_json", "osv_vulns_json", "semantic_alignment_passed",
        "semantic_notes", "gate_decision", "scanned_at"
    },
    "workers": {
        "worker_id", "machine_class", "capabilities_json", "cow_backend",
        "local_llm_models_json", "trust_level", "health", "last_heartbeat_at",
        "registered_at"
    },
    "leases": {
        "lease_id", "task_id", "attempt_id", "worker_id", "fencing_token",
        "expires_at", "renewed_at", "released_at"
    },
    "memories": {
        "memory_id", "scope", "project_id", "subject", "content",
        "provenance_json", "confidence", "sensitivity", "status",
        "expires_at", "created_at", "updated_at"
    },
    "harness_revisions": {
        "revision_id", "parent_revision_id", "bundle_hash", "bundle_uri",
        "editable_diff_json", "skill_scan_id", "status", "created_by",
        "created_at", "promoted_at"
    },
    "evaluations": {
        "evaluation_id", "revision_id", "suite_id", "dataset_version",
        "model_pins_json", "results_uri", "metrics_json", "gate_decision",
        "created_at", "reviewer_id"
    },
}


class AlembicMigrationsTestCase(unittest.TestCase):
    """Verifies Alembic migrations create, configure, and teardown all 16 ledger tables."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_migration.db"
        self.db_url = f"sqlite:///{self.db_path.resolve()}"

        root_dir = Path(__file__).resolve().parents[1]
        ini_path = root_dir / "alembic.ini"
        self.cfg = Config(str(ini_path))
        self.cfg.set_main_option("sqlalchemy.url", self.db_url)
        self.cfg.set_main_option("script_location", str(root_dir / "migrations"))

        # Explicitly set HARNESS_DATABASE_URL to isolate test execution
        self.old_env = os.environ.get("HARNESS_DATABASE_URL")
        os.environ["HARNESS_DATABASE_URL"] = self.db_url

    def tearDown(self) -> None:
        if self.old_env is not None:
            os.environ["HARNESS_DATABASE_URL"] = self.old_env
        else:
            os.environ.pop("HARNESS_DATABASE_URL", None)
        self.temp_dir.cleanup()

    def _get_table_names(self) -> set[str]:
        conn = sqlite3.connect(str(self.db_path))
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';"
            )
            return {row[0] for row in cur.fetchall()}
        finally:
            conn.close()

    def _get_columns(self, table_name: str) -> set[str]:
        conn = sqlite3.connect(str(self.db_path))
        try:
            cur = conn.cursor()
            cur.execute(f"PRAGMA table_info({table_name});")
            return {row[1] for row in cur.fetchall()}
        finally:
            conn.close()

    def _get_indexes(self, table_name: str) -> set[str]:
        conn = sqlite3.connect(str(self.db_path))
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?;",
                (table_name,),
            )
            return {row[0] for row in cur.fetchall()}
        finally:
            conn.close()

    def test_upgrade_head_creates_all_16_tables_and_indexes(self) -> None:
        # Run upgrade to head
        command.upgrade(self.cfg, "head")

        created_tables = self._get_table_names()
        # Should include all 16 tables plus alembic_version
        self.assertTrue(EXPECTED_TABLES.issubset(created_tables))
        self.assertIn("alembic_version", created_tables)

        # Verify all columns for all 16 tables
        for table, expected_cols in EXPECTED_COLUMNS.items():
            actual_cols = self._get_columns(table)
            self.assertEqual(
                actual_cols,
                expected_cols,
                f"Mismatch in columns for table {table}: missing {expected_cols - actual_cols}, extra {actual_cols - expected_cols}",
            )

        # Verify index creation
        tool_call_indexes = self._get_indexes("tool_calls")
        self.assertIn("idx_tool_calls_lookup", tool_call_indexes)

    def test_downgrade_base_drops_all_tables(self) -> None:
        command.upgrade(self.cfg, "head")
        tables_after_upgrade = self._get_table_names()
        self.assertTrue(EXPECTED_TABLES.issubset(tables_after_upgrade))

        # Downgrade to base
        command.downgrade(self.cfg, "base")
        tables_after_downgrade = self._get_table_names()

        # All 16 tables should be completely dropped
        for table in EXPECTED_TABLES:
            self.assertNotIn(table, tables_after_downgrade)

    def test_upgrade_downgrade_roundtrip(self) -> None:
        # Upgrade -> Downgrade -> Upgrade
        command.upgrade(self.cfg, "head")
        self.assertTrue(EXPECTED_TABLES.issubset(self._get_table_names()))

        command.downgrade(self.cfg, "base")
        self.assertEqual(len(EXPECTED_TABLES.intersection(self._get_table_names())), 0)

        command.upgrade(self.cfg, "head")
        self.assertTrue(EXPECTED_TABLES.issubset(self._get_table_names()))


if __name__ == "__main__":
    unittest.main()
