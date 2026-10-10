"""SQLAlchemy Core schema shared by migrations and projections."""

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

metadata = MetaData()


def col(name, type_, **kw):
    return Column(name, type_, **kw)


def scoped(name, *columns):
    return Table(
        name,
        metadata,
        col("task_id", UUID(as_uuid=True), primary_key=True),
        col("id", Text, primary_key=True),
        *columns,
    )


tasks = Table(
    "tasks",
    metadata,
    col("id", UUID(as_uuid=True), primary_key=True),
    col("name", Text),
    col("goal", Text, nullable=False),
    col("initial_attachments", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    col("domain_context", Text),
    col("egress_allowlist", ARRAY(Text), nullable=False),
    col("acceptance", JSONB, nullable=False),
    col("acceptance_state", JSONB, nullable=False),
    col("budget", JSONB, nullable=False),
    col("params", JSONB, nullable=False),
    col("agent_profile", Text, nullable=False),
    col("agent_profile_version", Integer, nullable=False),
    col("status", Text, nullable=False),
    col("deleting", Boolean, nullable=False, server_default=text("false")),
    col("last_change_version", BigInteger, nullable=False, default=0),
    col("last_judgment_version", BigInteger, nullable=False, default=0),
    col("derive_empty_streak", Integer, nullable=False, default=0),
    col("failure_streak", Integer, nullable=False, default=0),
    col("failure_window_kind", Text),
    col("failure_window_started_at", DateTime(timezone=True)),
    col("seed_empty_count", Integer, nullable=False, default=0),
    col("fail_reason", Text),
    col("usage", JSONB, nullable=False),
    col("billing_mode", Text),
    col("report_uri", Text),
    col("workspace_uri", Text),
    col("resume_workspace_uri", Text),
    col("cleanup_ready", Boolean, nullable=False, server_default=text("false")),
    col("run_number", Integer, nullable=False, server_default=text("1")),
    col("run_started", Boolean, nullable=False, server_default=text("false")),
    col("active_seconds", Integer, nullable=False, server_default=text("0")),
    col("active_since", DateTime(timezone=True)),
    col("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    col("started_at", DateTime(timezone=True)),
    col("finished_at", DateTime(timezone=True)),
)

task_input_groups = Table(
    "task_input_groups",
    metadata,
    col("id", UUID(as_uuid=True), primary_key=True),
    col("owner", Text, nullable=False),
    col("expires_at", DateTime(timezone=True), nullable=False),
    col("files", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("bound_task_id", UUID(as_uuid=True), ForeignKey("tasks.id")),
    col("deleting", Boolean, nullable=False, server_default=text("false")),
    col("create_request_hash", Text),
)


task_runs = Table(
    "task_runs",
    metadata,
    Column("task_id", UUID(as_uuid=True), ForeignKey("tasks.id"), primary_key=True),
    col("run_number", Integer, primary_key=True),
    col("report_uri", Text),
    col("workspace_uri", Text),
    col("agent_profile", Text),
    col("agent_profile_version", Integer),
)

resume_requests = Table(
    "resume_requests",
    metadata,
    Column("task_id", UUID(as_uuid=True), ForeignKey("tasks.id"), primary_key=True),
    col("request_id", UUID(as_uuid=True), primary_key=True),
    col("additional_cost", Text, nullable=False),
    col("additional_minutes", Integer, nullable=False),
    col("refresh_tools", Boolean, nullable=False, server_default=text("false")),
    col("run_number", Integer, nullable=False),
)

task_counters = Table(
    "task_counters",
    metadata,
    Column("task_id", UUID(as_uuid=True), ForeignKey("tasks.id"), primary_key=True),
    col("kind", Text, primary_key=True),
    col("value", Integer, nullable=False),
)

events = Table(
    "events",
    metadata,
    col("version", BigInteger, primary_key=True, autoincrement=True),
    Column("task_id", UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=False),
    col("type", Text, nullable=False),
    col("actor", Text, nullable=False),
    col("object_id", Text),
    col("payload", JSONB, nullable=False),
    col("addressed_to", ARRAY(Text)),
    col("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)
Index("ix_events_task_version", events.c.task_id, events.c.version)

facts = scoped(
    "facts",
    col("kind", Text, nullable=False),
    col("statement", Text, nullable=False),
    col("evidence", JSONB, nullable=False),
    col("derived_from", ARRAY(Text), nullable=False),
    col("disputes", ARRAY(Text), nullable=False),
    col("resolves", Text),
    col("result", Text),
    col("satisfies", ARRAY(Text), nullable=False),
    col("author", Text, nullable=False),
    col("provenance", Text, nullable=False),
    col("version", BigInteger, nullable=False),
)

intents = scoped(
    "intents",
    col("statement", Text, nullable=False),
    col("based_on", ARRAY(Text), nullable=False),
    col("expected", Text, nullable=False),
    col("method", Text, nullable=False),
    col("relates_to", ARRAY(Text), nullable=False),
    col("retry_of", Text),
    col("status", Text, nullable=False),
    col("holder", Text),
    col("claimed_at", DateTime(timezone=True)),
    col("result", Text),
    col("closed_by", Text),
    col("result_facts", ARRAY(Text), nullable=False),
    col("notes", JSONB, nullable=False),
    col("attempts", Integer, nullable=False),
    col("author", Text, nullable=False),
    col("version", BigInteger, nullable=False),
)

agent_runs = scoped(
    "agent_runs",
    col("task_type", Text, nullable=False),
    col("is_seed", Boolean, nullable=False),
    col("close_mode", Text),
    col("judge_from_version", BigInteger),
    col("derive_from_version", BigInteger),
    col("derive_parallel", Boolean, nullable=False, server_default=text("false")),
    col("derive_review", Boolean, nullable=False, server_default=text("false")),
    col("derive_round", Integer, nullable=False, server_default=text("1")),
    col("round_start_version", BigInteger, nullable=False, server_default=text("0")),
    col("previous_receipt", JSONB),
    col("finished_version", BigInteger),
    col("intent_id", Text),
    col("status", Text, nullable=False),
    col("end_reason", Text),
    col("steps", Integer, nullable=False),
    col("context_tokens", Integer, nullable=False),
    col("usage", JSONB, nullable=False),
    col("conclude_reason", Text),
    col("conclude_requested_at", DateTime(timezone=True)),
    col("conclude_injected", Boolean, nullable=False),
    col("grace_calls_left", Integer),
    col("last_seen_version", BigInteger, nullable=False),
    col("last_heartbeat_at", DateTime(timezone=True), nullable=False),
    col("receipt", JSONB),
    col("started_at", DateTime(timezone=True), nullable=False),
    col("finished_at", DateTime(timezone=True)),
)

tool_calls = scoped(
    "tool_calls",
    col("agent_id", Text, nullable=False),
    col("tool", Text, nullable=False),
    col("args", JSONB, nullable=False),
    col("result_head", Text),
    col("result_uri", Text),
    col("created_at", DateTime(timezone=True), nullable=False),
)

agent_sessions = Table(
    "agent_sessions",
    metadata,
    Column("task_id", UUID(as_uuid=True), ForeignKey("tasks.id"), primary_key=True),
    col("agent_id", Text, primary_key=True),
    col("session", JSONB, nullable=False),
    col("opening_instructions", Text, nullable=False),
    col("origin", Text, nullable=False),
    col("revision", BigInteger, nullable=False),
    col("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

agent_messages = Table(
    "agent_messages",
    metadata,
    col("id", UUID(as_uuid=True), primary_key=True),
    Column("task_id", UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=False),
    col("agent_id", Text, nullable=False),
    col("role", Text, nullable=False),
    col("content", Text, nullable=False),
    col("status", Text, nullable=False),
    Column("reply_to", UUID(as_uuid=True), ForeignKey("agent_messages.id")),
    col("claim_token", UUID(as_uuid=True)),
    col("lease_until", DateTime(timezone=True)),
    col("usage", JSONB),
    col("error", Text),
    col("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    col("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)
Index(
    "ix_agent_messages_queue",
    agent_messages.c.task_id,
    agent_messages.c.agent_id,
    agent_messages.c.status,
    agent_messages.c.created_at,
)
Index("ux_agent_messages_reply_to", agent_messages.c.reply_to, unique=True)

agent_profiles = Table(
    "agent_profiles",
    metadata,
    col("name", Text, primary_key=True),
    col("version", Integer, primary_key=True),
    col("prompts", JSONB, nullable=False),
    col("prompt_templates", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    col("models", JSONB, nullable=False),
    col("worker_tools", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    col("params", JSONB, nullable=False),
    col("exec_image", Text, nullable=False),
    col("exec_resources", JSONB, nullable=False),
    col("privileged_allowlist", ARRAY(Text), nullable=False),
    col("created_by", Text),
    col("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)


platform_configs = Table(
    "platform_configs",
    metadata,
    col("kind", Text, primary_key=True),
    col("name", Text, primary_key=True),
    col("version", Integer, primary_key=True),
    col("label", Text, nullable=False),
    col("config", JSONB, nullable=False),
    col("credential_source", Text, nullable=False),
    col("secret_ciphertext", Text),
    col("enabled", Boolean, nullable=False, server_default=text("true")),
    col("created_by", Text, nullable=False),
    col("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)


app_settings = Table(
    "app_settings",
    metadata,
    col("key", Text, primary_key=True),
    col("value", JSONB, nullable=False),
)

# CTF tables retain authoritative runtime ownership and mailbox state during replay.
tasks.append_column(col("mode", Text, nullable=False, server_default=text("'blackboard'")))
for field in ("ctf_options", "ctf_control", "ctf_conclusion"):
    tasks.append_column(col(field, JSONB))

ctf_members = scoped(
    "ctf_members",
    col("role", Text, nullable=False),
    col("display_name", Text, nullable=False),
    col("normalized_name", Text, nullable=False),
    col("create_request_id", Text, nullable=False),
    col("lifecycle", Text, nullable=False),
    col("run_state", Text, nullable=False),
    col("generation", Integer, nullable=False),
    col("current_turn_id", Text),
    col("usage", JSONB, nullable=False),
    col("created_at", DateTime(timezone=True), nullable=False),
)
Index("ux_ctf_member_name", ctf_members.c.task_id, ctf_members.c.normalized_name, unique=True)
Index("ux_ctf_member_request", ctf_members.c.task_id, ctf_members.c.create_request_id, unique=True)
ctf_turns = scoped(
    "ctf_turns",
    col("agent_id", Text, nullable=False),
    col("generation", Integer, nullable=False),
    col("runtime_instance", Text, nullable=False),
    col("status", Text, nullable=False),
    col("purpose", Text, nullable=False),
    col("assignment_message_ids", JSONB, nullable=False),
    col("explicit_reply_ids", JSONB, nullable=False),
    col("checkpoint_revision", Integer, nullable=False),
    col("end_reason", Text),
    col("final_answer", Text),
    col("usage", JSONB, nullable=False),
    col("usage_requests", JSONB, nullable=False),
    col("started_at", DateTime(timezone=True), nullable=False),
    col("finished_at", DateTime(timezone=True)),
)
Index(
    "ux_ctf_active_turn",
    ctf_turns.c.task_id,
    ctf_turns.c.agent_id,
    unique=True,
    postgresql_where=ctf_turns.c.status == "running",
)
ctf_messages = scoped(
    "ctf_messages",
    col("sender_kind", Text, nullable=False),
    col("sender_id", Text, nullable=False),
    col("recipient_id", Text, nullable=False),
    col("recipient_sequence", BigInteger, nullable=False),
    col("kind", Text, nullable=False),
    col("body", Text, nullable=False),
    col("purpose", Text, nullable=False),
    col("reply_to", Text),
    col("status", Text, nullable=False),
    col("deferred", Boolean, nullable=False),
    col("claim_token", Text),
    col("claim_turn_id", Text),
    col("claim_generation", Integer),
    col("lease_until", DateTime(timezone=True)),
    col("delivered_turn_id", Text),
    col("session_revision", Integer),
    col("source_turn_id", Text),
    col("notification_purpose", Text),
    col("created_at", DateTime(timezone=True), nullable=False),
)
Index(
    "ux_ctf_result",
    ctf_messages.c.task_id,
    ctf_messages.c.source_turn_id,
    ctf_messages.c.notification_purpose,
    unique=True,
)
Index("ix_ctf_pending", ctf_messages.c.task_id, ctf_messages.c.recipient_id, ctf_messages.c.status)
ctf_challenges = scoped(
    "ctf_challenges", col("data", JSONB, nullable=False), col("revision", Integer, nullable=False)
)
ctf_records = scoped(
    "ctf_records", col("data", JSONB, nullable=False), col("version", BigInteger, nullable=False)
)

ctf_members.append_column(col("pending_operation", JSONB))
ctf_members.append_column(col("execution", JSONB))
ctf_members.append_column(
    col("operation_history", JSONB, nullable=False, server_default=text("'[]'::jsonb"))
)
ctf_members.append_column(col("removed_at", DateTime(timezone=True)))
