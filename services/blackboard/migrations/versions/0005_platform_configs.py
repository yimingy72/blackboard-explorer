"""Version platform model/MCP connections and worker tool bindings."""

from alembic import op
from bbx_blackboard.store.schema import platform_configs

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    platform_configs.create(op.get_bind(), checkfirst=True)
    op.execute(
        "ALTER TABLE agent_profiles ADD COLUMN IF NOT EXISTS worker_tools jsonb "
        "NOT NULL DEFAULT '{}'::jsonb"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE agent_profiles DROP COLUMN IF EXISTS worker_tools")
    platform_configs.drop(op.get_bind(), checkfirst=True)
