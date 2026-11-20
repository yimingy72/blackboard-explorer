"""Keep first-work history and the original Intent claimant Assignment."""

from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE ctf_members ADD COLUMN IF NOT EXISTS has_worked boolean NOT NULL DEFAULT false"
    )
    op.execute("ALTER TABLE intents ADD COLUMN IF NOT EXISTS claim_assignment_ref jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE intents DROP COLUMN claim_assignment_ref")
    op.execute("ALTER TABLE ctf_members DROP COLUMN has_worked")
