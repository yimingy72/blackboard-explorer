"""Persist Runner ownership, finite commands and native journal receipts."""

from alembic import op
from bbx_blackboard.store.schema import (
    runner_bindings,
    runner_commands,
    runner_owners,
    runner_receipts,
)

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in (runner_owners, runner_bindings, runner_commands, runner_receipts):
        table.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    for table in (runner_receipts, runner_commands, runner_bindings, runner_owners):
        table.drop(bind=op.get_bind(), checkfirst=True)
