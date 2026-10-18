"""Index the latest terminal transition used by ordinary state cursors."""

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_events_task_closing_version "
        "ON events (task_id, version) WHERE type IN ('task.closing', 'task.stopped')"
    )


def downgrade() -> None:
    op.drop_index("ix_events_task_closing_version", table_name="events")
