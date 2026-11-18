"""Persist per-Task host capabilities and the command record projection."""

from alembic import op
from bbx_blackboard.store.schema import sandbox_command_records
from sqlalchemy import Column, inspect, text
from sqlalchemy.dialects.postgresql import JSONB

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "sandbox_host" not in {
        column["name"] for column in inspect(bind).get_columns("task_runner_resources")
    }:
        op.add_column(
            "task_runner_resources",
            Column("sandbox_host", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        )
    sandbox_command_records.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    sandbox_command_records.drop(bind=op.get_bind(), checkfirst=True)
    op.drop_column("task_runner_resources", "sandbox_host")
