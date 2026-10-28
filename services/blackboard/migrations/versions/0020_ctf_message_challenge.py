"""Associate CTF mailbox inputs with their challenge without changing old messages."""

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE ctf_messages ADD COLUMN IF NOT EXISTS challenge_id text")


def downgrade() -> None:
    op.execute("ALTER TABLE ctf_messages DROP COLUMN challenge_id")
