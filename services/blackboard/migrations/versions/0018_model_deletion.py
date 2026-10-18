"""Retain pinned model versions while removing connections from the catalog."""

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The initial platform migration also creates tables from current metadata.
    op.execute(
        "ALTER TABLE platform_configs ADD COLUMN IF NOT EXISTS "
        "deleted BOOLEAN NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    op.drop_column("platform_configs", "deleted")
