"""Independent task progress projection and serial leased jobs."""

from alembic import op
from bbx_blackboard.store import schema as s

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for table in (s.task_progress, s.progress_jobs):
        table.create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for table in (s.progress_jobs, s.task_progress):
        table.drop(bind, checkfirst=True)
