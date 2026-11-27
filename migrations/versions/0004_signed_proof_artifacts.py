"""Persist device-signed proof envelopes alongside ledger proof records."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0004_signed_proof_artifacts"
down_revision: Union[str, None] = "0003_sync_ledger_branches"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("proof_bundles", sa.Column("signed_proof_json", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("proof_bundles", "signed_proof_json")
