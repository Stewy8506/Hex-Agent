"""Initial 16-table authoritative ledger schema

Revision ID: 0001_initial_schema
Revises: None
Create Date: 2026-10-31 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. tasks
    op.create_table(
        "tasks",
        sa.Column("task_id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("parent_task_id", sa.Text(), nullable=True),
        sa.Column("project_id", sa.Text(), nullable=True),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("execution_mode", sa.Text(), nullable=False, server_default="standard"),
        sa.Column("status", sa.Text(), nullable=False, server_default="SUBMITTED"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("50")),
        sa.Column("deadline_at", sa.Text(), nullable=True),
        sa.Column("budget_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("constraints_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("deliverable", sa.Text(), nullable=True),
        sa.Column("policy_snapshot_id", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), unique=True, nullable=True),
        sa.Column("active_workspace_tree_sha", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.Column("terminal_reason", sa.Text(), nullable=True),
    )

    # 2. task_events
    op.create_table(
        "task_events",
        sa.Column("event_id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("task_id", sa.Text(), sa.ForeignKey("tasks.task_id"), nullable=False),
        sa.Column("attempt_id", sa.Text(), nullable=True),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("prior_state", sa.Text(), nullable=True),
        sa.Column("next_state", sa.Text(), nullable=True),
        sa.Column("reason_code", sa.Text(), nullable=True),
        sa.Column("workspace_tree_sha", sa.Text(), nullable=True),
        sa.Column("policy_version", sa.Text(), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.Text(), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column("event_sha256", sa.Text(), nullable=False),
        sa.UniqueConstraint("task_id", "sequence_no", name="uq_task_events_task_seq"),
    )

    # 3. attempts
    op.create_table(
        "attempts",
        sa.Column("attempt_id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("task_id", sa.Text(), sa.ForeignKey("tasks.task_id"), nullable=False),
        sa.Column("parent_attempt_id", sa.Text(), nullable=True),
        sa.Column("branch_label", sa.Text(), nullable=True),
        sa.Column("worker_id", sa.Text(), nullable=False),
        sa.Column("lease_id", sa.Text(), nullable=True),
        sa.Column("fencing_token", sa.Integer(), nullable=False),
        sa.Column("base_tree_sha", sa.Text(), nullable=True),
        sa.Column("head_tree_sha", sa.Text(), nullable=True),
        sa.Column("workspace_path", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="RUNNING"),
        sa.Column("started_at", sa.Text(), nullable=False),
        sa.Column("ended_at", sa.Text(), nullable=True),
        sa.Column("failure_class", sa.Text(), nullable=True),
        sa.Column("retry_index", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )

    # 4. plans
    op.create_table(
        "plans",
        sa.Column("plan_id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("task_id", sa.Text(), sa.ForeignKey("tasks.task_id"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("dag_json", sa.Text(), nullable=False),
        sa.Column("blast_radius_checks_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("supersedes_revision", sa.Integer(), nullable=True),
        sa.UniqueConstraint("task_id", "revision", name="uq_plans_task_revision"),
    )

    # 5. checkpoints
    op.create_table(
        "checkpoints",
        sa.Column("checkpoint_id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("task_id", sa.Text(), sa.ForeignKey("tasks.task_id"), nullable=False),
        sa.Column("attempt_id", sa.Text(), sa.ForeignKey("attempts.attempt_id"), nullable=False),
        sa.Column("plan_revision", sa.Integer(), nullable=False),
        sa.Column("completed_steps_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("pending_steps_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("workspace_tree_sha", sa.Text(), nullable=False),
        sa.Column("snapshot_path", sa.Text(), nullable=False),
        sa.Column("artifact_uri", sa.Text(), nullable=True),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False, server_default="1.0"),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("validity", sa.Text(), nullable=False, server_default="VALID"),
    )

    # 6. tool_calls
    op.create_table(
        "tool_calls",
        sa.Column("call_id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("task_id", sa.Text(), sa.ForeignKey("tasks.task_id"), nullable=False),
        sa.Column("attempt_id", sa.Text(), nullable=False),
        sa.Column("step_id", sa.Text(), nullable=False),
        sa.Column("sequence_in_step", sa.Integer(), nullable=False),
        sa.Column("tool_id", sa.Text(), nullable=False),
        sa.Column("risk_class", sa.Text(), nullable=False),
        sa.Column("canonical_args_hash", sa.Text(), nullable=False),
        sa.Column("arguments_redacted_json", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("result_ref", sa.Text(), nullable=True),
        sa.Column("replayed_from_call_id", sa.Text(), nullable=True),
        sa.Column("policy_decision", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("started_at", sa.Text(), nullable=False),
        sa.Column("ended_at", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="PENDING"),
    )
    op.create_index(
        "idx_tool_calls_lookup",
        "tool_calls",
        ["task_id", "step_id", "tool_id", "canonical_args_hash", "status"],
    )

    # 7. approvals
    op.create_table(
        "approvals",
        sa.Column("approval_id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("task_id", sa.Text(), sa.ForeignKey("tasks.task_id"), nullable=False),
        sa.Column("attempt_id", sa.Text(), nullable=True),
        sa.Column("step_id", sa.Text(), nullable=True),
        sa.Column("action_type", sa.Text(), nullable=False),
        sa.Column("action_hash", sa.Text(), nullable=False),
        sa.Column("action_params_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("blast_radius_summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("decision", sa.Text(), nullable=False, server_default="PENDING"),
        sa.Column("expires_at", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("decided_at", sa.Text(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
    )

    # 8. artifacts
    op.create_table(
        "artifacts",
        sa.Column("artifact_id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("task_id", sa.Text(), sa.ForeignKey("tasks.task_id"), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("uri", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False, server_default="application/json"),
        sa.Column("sensitivity", sa.Text(), nullable=False, server_default="internal"),
        sa.Column("retention_until", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
    )

    # 9. proof_bundles
    op.create_table(
        "proof_bundles",
        sa.Column("proof_id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("task_id", sa.Text(), sa.ForeignKey("tasks.task_id"), nullable=False),
        sa.Column("attempt_id", sa.Text(), nullable=False),
        sa.Column("git_diff_sha", sa.Text(), nullable=False),
        sa.Column("git_diff_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("workspace_tree_sha", sa.Text(), nullable=False),
        sa.Column("runtime_digest", sa.Text(), nullable=False),
        sa.Column("ast_impact_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("verification_results_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("ledger_signature", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
    )

    # 10. skills
    op.create_table(
        "skills",
        sa.Column("skill_id", sa.Text(), nullable=False),
        sa.Column("revision_id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("disk_path", sa.Text(), nullable=False),
        sa.Column("git_commit_sha", sa.Text(), nullable=False, server_default="local"),
        sa.Column("bundle_sha256", sa.Text(), nullable=False),
        sa.Column("risk_class", sa.Text(), nullable=False, server_default="R1"),
        sa.Column("allowed_tools_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("status", sa.Text(), nullable=False, server_default="candidate"),
        sa.Column("latest_scan_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
    )

    # 11. skill_scans
    op.create_table(
        "skill_scans",
        sa.Column("scan_id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("skill_id", sa.Text(), nullable=False),
        sa.Column("revision_id", sa.Text(), nullable=False),
        sa.Column("bundle_sha256", sa.Text(), nullable=False),
        sa.Column("scanner_version", sa.Text(), nullable=False),
        sa.Column("semantic_model_id", sa.Text(), nullable=False),
        sa.Column("risk_score", sa.Integer(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("matched_rules_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("osv_vulns_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("semantic_alignment_passed", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("semantic_notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("gate_decision", sa.Text(), nullable=False),
        sa.Column("scanned_at", sa.Text(), nullable=False),
    )

    # 12. workers
    op.create_table(
        "workers",
        sa.Column("worker_id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("machine_class", sa.Text(), nullable=False),
        sa.Column("capabilities_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("cow_backend", sa.Text(), nullable=False),
        sa.Column("local_llm_models_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("trust_level", sa.Text(), nullable=False, server_default="trusted_local"),
        sa.Column("health", sa.Text(), nullable=False, server_default="HEALTHY"),
        sa.Column("last_heartbeat_at", sa.Text(), nullable=False),
        sa.Column("registered_at", sa.Text(), nullable=False),
    )

    # 13. leases
    op.create_table(
        "leases",
        sa.Column("lease_id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("task_id", sa.Text(), sa.ForeignKey("tasks.task_id"), nullable=False),
        sa.Column("attempt_id", sa.Text(), nullable=False),
        sa.Column("worker_id", sa.Text(), sa.ForeignKey("workers.worker_id"), nullable=False),
        sa.Column("fencing_token", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.Column("renewed_at", sa.Text(), nullable=False),
        sa.Column("released_at", sa.Text(), nullable=True),
    )

    # 14. memories
    op.create_table(
        "memories",
        sa.Column("memory_id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("project_id", sa.Text(), nullable=True),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("provenance_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default=sa.text("1.0")),
        sa.Column("sensitivity", sa.Text(), nullable=False, server_default="internal"),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("expires_at", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
    )

    # 15. harness_revisions
    op.create_table(
        "harness_revisions",
        sa.Column("revision_id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("parent_revision_id", sa.Text(), nullable=True),
        sa.Column("bundle_hash", sa.Text(), nullable=False),
        sa.Column("bundle_uri", sa.Text(), nullable=False),
        sa.Column("editable_diff_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("skill_scan_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="candidate"),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("promoted_at", sa.Text(), nullable=True),
    )

    # 16. evaluations
    op.create_table(
        "evaluations",
        sa.Column("evaluation_id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("revision_id", sa.Text(), sa.ForeignKey("harness_revisions.revision_id"), nullable=False),
        sa.Column("suite_id", sa.Text(), nullable=False),
        sa.Column("dataset_version", sa.Text(), nullable=False),
        sa.Column("model_pins_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("results_uri", sa.Text(), nullable=False),
        sa.Column("metrics_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("gate_decision", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("reviewer_id", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    # Drop in reverse foreign key order
    op.drop_table("evaluations")
    op.drop_table("harness_revisions")
    op.drop_table("memories")
    op.drop_table("leases")
    op.drop_table("workers")
    op.drop_table("skill_scans")
    op.drop_table("skills")
    op.drop_table("proof_bundles")
    op.drop_table("artifacts")
    op.drop_table("approvals")
    op.drop_index("idx_tool_calls_lookup", table_name="tool_calls")
    op.drop_table("tool_calls")
    op.drop_table("checkpoints")
    op.drop_table("plans")
    op.drop_table("attempts")
    op.drop_table("task_events")
    op.drop_table("tasks")
