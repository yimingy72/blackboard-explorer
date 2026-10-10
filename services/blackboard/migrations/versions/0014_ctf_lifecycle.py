"""Persist CTF member stop intent and remote execution identity."""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in ("pending_operation", "execution"):
        op.execute(f"ALTER TABLE ctf_members ADD COLUMN IF NOT EXISTS {name} jsonb")
    op.execute(
        "ALTER TABLE ctf_members ADD COLUMN IF NOT EXISTS operation_history "
        "jsonb NOT NULL DEFAULT '[]'::jsonb"
    )
    op.execute("ALTER TABLE ctf_members ADD COLUMN IF NOT EXISTS removed_at timestamptz")


def downgrade() -> None:
    for name in ("removed_at", "operation_history", "execution", "pending_operation"):
        op.execute(f"ALTER TABLE ctf_members DROP COLUMN {name}")
