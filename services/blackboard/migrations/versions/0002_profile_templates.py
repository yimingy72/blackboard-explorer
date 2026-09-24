"""Persist immutable prompt bodies in profile versions.

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agent_profiles ADD COLUMN IF NOT EXISTS "
        "prompt_templates jsonb NOT NULL DEFAULT '{}'::jsonb"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE agent_profiles DROP COLUMN IF EXISTS prompt_templates")
