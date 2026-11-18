"""Persist native Task bootstrap ownership, tenant credentials and resource evidence."""

from alembic import op
from bbx_blackboard.store.schema import task_runner_resources
from bbx_contracts.runner_bootstrap import RUNTIME_FENCE_SEQUENCE
from sqlalchemy import Sequence

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Sequence(RUNTIME_FENCE_SEQUENCE).create(bind=op.get_bind(), checkfirst=True)
    task_runner_resources.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    task_runner_resources.drop(bind=op.get_bind(), checkfirst=True)
    Sequence(RUNTIME_FENCE_SEQUENCE).drop(bind=op.get_bind(), checkfirst=True)
