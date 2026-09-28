"""Track task runs, active duration and idempotent continuation requests."""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS run_number integer NOT NULL DEFAULT 1")
    op.execute(
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS run_started boolean NOT NULL DEFAULT false"
    )
    op.execute(
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS active_seconds integer NOT NULL DEFAULT 0"
    )
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS active_since timestamptz")
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS resume_workspace_uri text")
    op.execute(
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS cleanup_ready boolean NOT NULL DEFAULT false"
    )
    op.execute(
        "UPDATE tasks SET active_seconds = CASE WHEN status IN ('running', 'closing') "
        "THEN 0 ELSE GREATEST(0, EXTRACT(EPOCH FROM (finished_at - started_at))::integer) END, "
        "active_since = CASE WHEN status IN ('running', 'closing') THEN started_at END "
        "WHERE started_at IS NOT NULL"
    )
    op.execute("UPDATE tasks SET run_started = started_at IS NOT NULL")
    op.execute(
        "CREATE TABLE IF NOT EXISTS task_runs (task_id uuid NOT NULL REFERENCES tasks(id), "
        "run_number integer NOT NULL, report_uri text, workspace_uri text, "
        "agent_profile text, agent_profile_version integer, "
        "PRIMARY KEY (task_id, run_number))"
    )
    op.execute(
        "INSERT INTO task_runs(task_id, run_number, report_uri, workspace_uri, "
        "agent_profile, agent_profile_version) "
        "SELECT id, 1, report_uri, workspace_uri, agent_profile, agent_profile_version "
        "FROM tasks WHERE report_uri IS NOT NULL OR workspace_uri IS NOT NULL "
        "ON CONFLICT (task_id, run_number) DO NOTHING"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS resume_requests (task_id uuid NOT NULL REFERENCES tasks(id), "
        "request_id uuid NOT NULL, additional_cost text NOT NULL, "
        "additional_minutes integer NOT NULL, refresh_tools boolean NOT NULL DEFAULT false, "
        "run_number integer NOT NULL, "
        "PRIMARY KEY (task_id, request_id))"
    )


def downgrade() -> None:
    op.execute("DROP TABLE resume_requests")
    op.execute("DROP TABLE task_runs")
    op.execute("ALTER TABLE tasks DROP COLUMN cleanup_ready")
    op.execute("ALTER TABLE tasks DROP COLUMN resume_workspace_uri")
    op.execute("ALTER TABLE tasks DROP COLUMN active_since")
    op.execute("ALTER TABLE tasks DROP COLUMN active_seconds")
    op.execute("ALTER TABLE tasks DROP COLUMN run_number")
    op.execute("ALTER TABLE tasks DROP COLUMN run_started")
