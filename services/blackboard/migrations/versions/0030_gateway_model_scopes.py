"""Freeze AgentGateway references without rewriting historical Task models."""

from alembic import op
from bbx_blackboard.store.schema import (
    gateway_execution_attempts,
    gateway_execution_budgets,
    gateway_task_models,
)

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE agent_messages ADD COLUMN IF NOT EXISTS purpose text")
    op.execute("ALTER TABLE agent_messages ADD COLUMN IF NOT EXISTS request_started_at timestamptz")
    op.execute("ALTER TABLE agent_messages ADD COLUMN IF NOT EXISTS request_derive_round integer")
    op.execute("ALTER TABLE ctf_messages ADD COLUMN IF NOT EXISTS request_agent_generation integer")
    for table in ("agent_messages", "ctf_messages"):
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS request_run_number integer")
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS review_model jsonb")
    op.execute("ALTER TABLE ctf_turns ADD COLUMN IF NOT EXISTS review_model jsonb")
    op.execute("ALTER TABLE ctf_turns ADD COLUMN IF NOT EXISTS review_origin_generation integer")
    bind = op.get_bind()
    for table in (gateway_task_models, gateway_execution_budgets, gateway_execution_attempts):
        table.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    op.drop_column("ctf_turns", "review_origin_generation")
    op.drop_column("ctf_turns", "review_model")
    op.drop_column("ctf_messages", "request_agent_generation")
    op.drop_column("agent_messages", "request_derive_round")
    op.drop_column("agent_messages", "request_started_at")
    for table in ("ctf_messages", "agent_messages"):
        op.drop_column(table, "review_model")
        op.drop_column(table, "request_run_number")
    op.drop_column("agent_messages", "purpose")
    bind = op.get_bind()
    for table in (gateway_execution_attempts, gateway_execution_budgets, gateway_task_models):
        table.drop(bind=bind, checkfirst=True)
