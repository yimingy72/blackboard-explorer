"""Index existing CTF observation identities without scanning event JSON payloads."""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_events_ctf_observation "
        "ON events (task_id, object_id) WHERE type = 'agent.trace.recorded'"
    )


def downgrade() -> None:
    op.drop_index("ix_events_ctf_observation", table_name="events")
