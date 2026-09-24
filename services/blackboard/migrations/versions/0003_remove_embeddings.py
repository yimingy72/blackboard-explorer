"""Remove derived vectors after moving duplicate judgment to agents.

Revision ID: 0003
Revises: 0002
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE facts DROP COLUMN IF EXISTS embedding")
    op.execute("ALTER TABLE intents DROP COLUMN IF EXISTS embedding")


def downgrade():
    op.execute("ALTER TABLE facts ADD COLUMN IF NOT EXISTS embedding vector(512)")
    op.execute("ALTER TABLE intents ADD COLUMN IF NOT EXISTS embedding vector(512)")
