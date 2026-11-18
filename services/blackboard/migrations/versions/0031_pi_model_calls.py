"""Add site-local Pi call evidence without rewriting historical fee rows."""

from alembic import op
from bbx_blackboard.store.schema import pi_model_calls, pi_task_models

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE agent_messages ADD COLUMN IF NOT EXISTS request_user text")
    op.execute("ALTER TABLE ctf_messages ADD COLUMN IF NOT EXISTS request_user text")
    for table in (pi_task_models, pi_model_calls):
        table.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    for table in (pi_model_calls, pi_task_models):
        table.drop(bind=op.get_bind(), checkfirst=True)
    op.drop_column("ctf_messages", "request_user")
    op.drop_column("agent_messages", "request_user")
