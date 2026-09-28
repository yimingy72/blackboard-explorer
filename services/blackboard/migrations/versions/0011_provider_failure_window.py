"""Persist the transient provider failure window on tasks."""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS failure_window_kind text")
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS failure_window_started_at timestamptz")


def downgrade() -> None:
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS failure_window_started_at")
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS failure_window_kind")
