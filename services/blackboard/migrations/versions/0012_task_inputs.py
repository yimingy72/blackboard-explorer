"""Retain optional task names and atomically bind staged initial inputs."""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS name text")
    op.execute(
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS initial_attachments "
        "jsonb NOT NULL DEFAULT '[]'::jsonb"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS task_input_groups (id uuid PRIMARY KEY, "
        "owner text NOT NULL, expires_at timestamptz NOT NULL, "
        "files jsonb NOT NULL DEFAULT '[]'::jsonb, "
        "bound_task_id uuid REFERENCES tasks(id), "
        "deleting boolean NOT NULL DEFAULT false, create_request_hash text)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE task_input_groups")
    op.execute("ALTER TABLE tasks DROP COLUMN initial_attachments")
    op.execute("ALTER TABLE tasks DROP COLUMN name")
