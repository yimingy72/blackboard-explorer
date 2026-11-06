"""Persist immutable announcements, protected candidates and business submissions."""

from alembic import op
from bbx_blackboard.store.schema import board_announcements, flag_candidates, flag_submissions

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in (board_announcements, flag_candidates, flag_submissions):
        table.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    for table in (flag_submissions, flag_candidates, board_announcements):
        table.drop(bind=op.get_bind(), checkfirst=True)
