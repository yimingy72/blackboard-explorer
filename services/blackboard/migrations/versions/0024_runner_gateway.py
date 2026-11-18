"""Persist Runner calls, native Task keys and deduplicated native fee evidence."""

from alembic import op
from bbx_blackboard.store.schema import native_request_costs, native_task_budgets, runner_calls

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    runner_calls.create(bind=op.get_bind(), checkfirst=True)
    native_task_budgets.create(bind=op.get_bind(), checkfirst=True)
    native_request_costs.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    native_request_costs.drop(bind=op.get_bind(), checkfirst=True)
    native_task_budgets.drop(bind=op.get_bind(), checkfirst=True)
    runner_calls.drop(bind=op.get_bind(), checkfirst=True)
