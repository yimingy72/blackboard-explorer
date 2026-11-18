"""Freeze the protected legacy Task network and preserve each original run's receipt."""

from alembic import op
from bbx_blackboard.store.schema import legacy_execution_networks, legacy_execution_receipts

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    legacy_execution_networks.create(bind=bind, checkfirst=True)
    legacy_execution_receipts.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    legacy_execution_receipts.drop(bind=bind, checkfirst=True)
    legacy_execution_networks.drop(bind=bind, checkfirst=True)
