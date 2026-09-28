"""Preserve an explicit, auditable task billing override after reconciliation."""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS billing_mode text")


def downgrade() -> None:
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS billing_mode")
