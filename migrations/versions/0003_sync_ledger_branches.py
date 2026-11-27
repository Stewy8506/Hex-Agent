"""Preserve divergent personal-sync ledger chains as isolated branches."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0003_sync_ledger_branches"
down_revision: Union[str, None] = "0002_feature_schema_parity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sync_ledger_branches",
        sa.Column("branch_id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("task_id", sa.Text(), sa.ForeignKey("tasks.task_id"), nullable=False),
        sa.Column("sender_device_record_id", sa.Text(), nullable=False),
        sa.Column("local_tip", sa.Text(), nullable=False),
        sa.Column("remote_tip", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
    )
    op.create_index(
        "idx_sync_ledger_branches_task", "sync_ledger_branches", ["task_id"]
    )
    op.create_table(
        "sync_ledger_branch_events",
        sa.Column("branch_id", sa.Text(), sa.ForeignKey("sync_ledger_branches.branch_id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_id", sa.Text(), nullable=False),
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
        sa.PrimaryKeyConstraint("branch_id", "event_id"),
        sa.UniqueConstraint("branch_id", "sequence_no", name="uq_sync_branch_sequence"),
    )


def downgrade() -> None:
    op.drop_table("sync_ledger_branch_events")
    op.drop_index("idx_sync_ledger_branches_task", table_name="sync_ledger_branches")
    op.drop_table("sync_ledger_branches")
