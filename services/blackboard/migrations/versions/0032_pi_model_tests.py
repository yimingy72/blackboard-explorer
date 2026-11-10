"""Add taskless model tests to the existing Pi ledger."""

from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE pi_model_calls ALTER COLUMN task_id DROP NOT NULL")
    op.execute(
        "ALTER TABLE pi_model_calls DROP CONSTRAINT IF EXISTS ck_pi_model_calls_playground_scope"
    )
    op.execute(
        "ALTER TABLE pi_model_calls ADD CONSTRAINT ck_pi_model_calls_playground_scope CHECK ("
        "(purpose = 'playground' AND task_id IS NULL AND request_user IS NOT NULL "
        "AND request_user <> '' AND agent_id = '') OR "
        "(purpose <> 'playground' AND task_id IS NOT NULL))"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_pi_model_calls_playground "
        "ON pi_model_calls (site_id, request_user, logical_call_id) WHERE purpose = 'playground'"
    )


def downgrade() -> None:
    # SET NOT NULL refuses to discard already-recorded model tests.
    op.execute("ALTER TABLE pi_model_calls ALTER COLUMN task_id SET NOT NULL")
    op.execute("DROP INDEX IF EXISTS ux_pi_model_calls_playground")
    op.execute("ALTER TABLE pi_model_calls DROP CONSTRAINT ck_pi_model_calls_playground_scope")
