"""Track derive rounds and their starting board versions."""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS derive_round integer NOT NULL DEFAULT 1"
    )
    op.execute(
        "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS "
        "round_start_version bigint NOT NULL DEFAULT 0"
    )
    op.execute("ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS previous_receipt jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE agent_runs DROP COLUMN previous_receipt")
    op.execute("ALTER TABLE agent_runs DROP COLUMN round_start_version")
    op.execute("ALTER TABLE agent_runs DROP COLUMN derive_round")
