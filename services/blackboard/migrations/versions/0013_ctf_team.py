"""Add isolated CTF team runtime state."""

from alembic import op
from bbx_blackboard.store import schema

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS mode text NOT NULL DEFAULT 'blackboard'")
    for field in ("ctf_options", "ctf_control", "ctf_conclusion"):
        op.execute(f"ALTER TABLE tasks ADD COLUMN IF NOT EXISTS {field} jsonb")
    for table in (
        schema.ctf_members,
        schema.ctf_turns,
        schema.ctf_messages,
        schema.ctf_challenges,
        schema.ctf_records,
    ):
        table.create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    for table in (
        schema.ctf_records,
        schema.ctf_challenges,
        schema.ctf_messages,
        schema.ctf_turns,
        schema.ctf_members,
    ):
        table.drop(op.get_bind())
    for field in ("ctf_conclusion", "ctf_control", "ctf_options", "mode"):
        op.execute(f"ALTER TABLE tasks DROP COLUMN {field}")
