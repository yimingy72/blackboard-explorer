"""Index recipient sequence lookup and per-challenge record pagination."""

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ctf_message_sequence "
        "ON ctf_messages (task_id, recipient_id, recipient_sequence)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ctf_record_challenge_version "
        "ON ctf_records (task_id, (data->>'challenge_id'), version)"
    )


def downgrade() -> None:
    op.drop_index("ix_ctf_record_challenge_version", table_name="ctf_records")
    op.drop_index("ix_ctf_message_sequence", table_name="ctf_messages")
