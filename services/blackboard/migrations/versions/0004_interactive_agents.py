"""Persist Agent conversations and parallel derive registration.

Revision ID: 0004
Revises: 0003
"""

from alembic import op
from bbx_blackboard.store.schema import agent_messages, agent_sessions

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS deleting boolean NOT NULL DEFAULT false")
    op.execute("ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS derive_from_version bigint")
    op.execute(
        "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS derive_parallel "
        "boolean NOT NULL DEFAULT false"
    )
    bind = op.get_bind()
    agent_sessions.create(bind, checkfirst=True)
    agent_messages.create(bind, checkfirst=True)
    for index in agent_messages.indexes:
        index.create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    agent_messages.drop(bind, checkfirst=True)
    agent_sessions.drop(bind, checkfirst=True)
    op.execute("ALTER TABLE agent_runs DROP COLUMN IF EXISTS derive_parallel")
    op.execute("ALTER TABLE agent_runs DROP COLUMN IF EXISTS derive_from_version")
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS deleting")
