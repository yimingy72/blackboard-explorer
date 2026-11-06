"""Persist Task ownership and publication intent for native Virtual MCP routes."""

from alembic import op
from bbx_blackboard.store.schema import native_task_mcp_routes

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    native_task_mcp_routes.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    native_task_mcp_routes.drop(bind=op.get_bind(), checkfirst=True)
