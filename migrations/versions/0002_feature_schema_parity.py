"""Feature schema parity: dialogue turns, approval leases, and task flags

Revision ID: 0002_feature_schema_parity
Revises: 0001_initial_schema
Create Date: 2026-11-18 00:00:00.000000

B37: the runtime added these objects ad hoc on SQLite while Alembic head (used
to initialize PostgreSQL) omitted them, so a clean PostgreSQL install lacked
dialogue and approval-lease storage. This revision makes the migration head the
authoritative schema for both drivers.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0002_feature_schema_parity"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("pinned", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "tasks",
        sa.Column("title_provenance", sa.Text(), nullable=False, server_default="derived"),
    )

    op.create_table(
        "dialogue_turns",
        sa.Column("turn_id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("task_id", sa.Text(), sa.ForeignKey("tasks.task_id"), nullable=False),
        sa.Column("turn_index", sa.Integer(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("branch_id", sa.Text(), nullable=False, server_default="main"),
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.Text(), nullable=False),
    )
    op.create_index(
        "idx_dialogue_turns_lookup",
        "dialogue_turns",
        ["task_id", "is_active", "turn_index"],
    )

    op.create_table(
        "approval_leases",
        sa.Column("lease_id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("task_id", sa.Text(), sa.ForeignKey("tasks.task_id"), nullable=True),
        sa.Column("command_prefix", sa.Text(), nullable=True),
        sa.Column("allowed_paths_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("scope", sa.Text(), nullable=False, server_default="temporal"),
        sa.Column("ttl_seconds", sa.Integer(), nullable=False, server_default=sa.text("900")),
        sa.Column("granted_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("is_revoked", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("bindings_json", sa.Text(), nullable=False, server_default="{}"),
    )
    op.create_index(
        "idx_approval_leases_lookup",
        "approval_leases",
        ["is_revoked", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_approval_leases_lookup", table_name="approval_leases")
    op.drop_table("approval_leases")
    op.drop_index("idx_dialogue_turns_lookup", table_name="dialogue_turns")
    op.drop_table("dialogue_turns")
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_column("title_provenance")
        batch_op.drop_column("pinned")