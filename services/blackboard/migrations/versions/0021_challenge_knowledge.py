"""Add nullable challenge scope without rewriting ordinary or historical knowledge."""

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("facts", "intents"):
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS challenge_id text")
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS assignment_ref jsonb")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_fact_challenge ON facts (task_id, challenge_id, version)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_intent_challenge ON intents (task_id, challenge_id, version)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_fact_challenge")
    op.execute("DROP INDEX IF EXISTS ix_intent_challenge")
    for table in ("facts", "intents"):
        op.execute(f"ALTER TABLE {table} DROP COLUMN assignment_ref")
        op.execute(f"ALTER TABLE {table} DROP COLUMN challenge_id")
