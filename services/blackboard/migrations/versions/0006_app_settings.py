"""Persist platform defaults independently of browser preferences."""

from alembic import op
from bbx_blackboard.store.schema import app_settings

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    app_settings.create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    app_settings.drop(op.get_bind(), checkfirst=True)
