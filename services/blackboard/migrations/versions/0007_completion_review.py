"""Persist completion review mode and finished event version."""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS derive_review "
        "boolean NOT NULL DEFAULT false"
    )
    op.execute("ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS finished_version bigint")


def downgrade() -> None:
    op.execute("ALTER TABLE agent_runs DROP COLUMN IF EXISTS finished_version")
    op.execute("ALTER TABLE agent_runs DROP COLUMN IF EXISTS derive_review")
