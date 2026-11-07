"""SQLAlchemy Core schema shared by migrations and projections."""

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    func,
    literal_column,
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
Index(
    "ix_events_task_closing_version",
    events.c.task_id,
    events.c.version,
    postgresql_where=events.c.type.in_(("task.closing", "task.stopped")),
)
Index(
    "ix_events_ctf_observation",
    events.c.task_id,
    events.c.object_id,
    postgresql_where=events.c.type == "agent.trace.recorded",
)

facts = scoped(
    "facts",
    col("challenge_id", Text),
    col("assignment_ref", JSONB(none_as_null=True)),
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
    col("challenge_id", Text),
    col("assignment_ref", JSONB(none_as_null=True)),
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

Index("ix_fact_challenge", facts.c.task_id, facts.c.challenge_id, facts.c.version)
Index("ix_intent_challenge", intents.c.task_id, intents.c.challenge_id, intents.c.version)

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
    col("deleted", Boolean, nullable=False, server_default=text("false")),
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
    col("challenge_id", Text),
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
Index(
    "ix_ctf_message_sequence",
    ctf_messages.c.task_id,
    ctf_messages.c.recipient_id,
    ctf_messages.c.recipient_sequence,
)
ctf_challenges = scoped(
    "ctf_challenges", col("data", JSONB, nullable=False), col("revision", Integer, nullable=False)
)
ctf_records = scoped(
    "ctf_records", col("data", JSONB, nullable=False), col("version", BigInteger, nullable=False)
)

board_announcements = scoped(
    "board_announcements",
    col("revision", Integer, nullable=False),
    col("data", JSONB, nullable=False),
    col("version", BigInteger, nullable=False),
)
Index(
    "ux_board_announcement_revision",
    board_announcements.c.task_id,
    board_announcements.c.revision,
    unique=True,
)

flag_candidates = scoped(
    "flag_candidates",
    col("challenge_id", Text, nullable=False),
    col("verification_round", Text, nullable=False),
    col("value_digest", Text, nullable=False),
    col("value_ciphertext", Text, nullable=False),
    col("data", JSONB, nullable=False),
    col("version", BigInteger, nullable=False),
)
Index(
    "ux_flag_candidate_value",
    flag_candidates.c.task_id,
    flag_candidates.c.challenge_id,
    flag_candidates.c.verification_round,
    flag_candidates.c.value_digest,
    unique=True,
)

flag_submissions = scoped(
    "flag_submissions",
    col("challenge_id", Text, nullable=False),
    col("candidate_id", Text, nullable=False),
    col("verification_round", Text, nullable=False),
    col("status", Text, nullable=False),
    col("platform_call_id", Text),
    col("data", JSONB, nullable=False),
    col("version", BigInteger, nullable=False),
)
Index(
    "ux_flag_submission_candidate",
    flag_submissions.c.task_id,
    flag_submissions.c.candidate_id,
    unique=True,
)
Index(
    "ux_flag_submission_call",
    flag_submissions.c.task_id,
    flag_submissions.c.platform_call_id,
    unique=True,
    postgresql_where=flag_submissions.c.platform_call_id.is_not(None),
)
Index(
    "ix_ctf_record_challenge_version",
    ctf_records.c.task_id,
    ctf_records.c.data[literal_column("'challenge_id'")].astext,
    ctf_records.c.version,
)

ctf_members.append_column(col("pending_operation", JSONB))
ctf_members.append_column(col("execution", JSONB))
ctf_members.append_column(
    col("operation_history", JSONB, nullable=False, server_default=text("'[]'::jsonb"))
)
ctf_members.append_column(col("removed_at", DateTime(timezone=True)))

# Independent presentation work never participates in solver ownership or budgets.
task_progress = Table(
    "task_progress",
    metadata,
    Column(
        "task_id", UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    ),
    col("data", JSONB, nullable=False),
)
progress_jobs = Table(
    "progress_jobs",
    metadata,
    col("id", UUID(as_uuid=True), primary_key=True),
    Column(
        "task_id", UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    ),
    col("kind", Text, nullable=False),
    col("status", Text, nullable=False),
    col("run_number", Integer, nullable=False),
    col("data", JSONB, nullable=False),
    col("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)
Index("ix_progress_jobs_queue", progress_jobs.c.status, progress_jobs.c.created_at)
Index(
    "ux_progress_job_active",
    progress_jobs.c.task_id,
    unique=True,
    postgresql_where=progress_jobs.c.status == "running",
)
Index(
    "ux_progress_job_refresh",
    progress_jobs.c.task_id,
    unique=True,
    postgresql_where=(progress_jobs.c.status == "queued") & (progress_jobs.c.kind == "refresh"),
)

# Runner epochs are historical authority; only one row may own a task now.
runner_owners = Table(
    "runner_owners",
    metadata,
    col("task_id", UUID(as_uuid=True), primary_key=True),
    col("epoch", BigInteger, primary_key=True),
    col("current", Boolean, nullable=False),
    col("runner_id", Text, nullable=False),
    col("pod_uid", Text, nullable=False),
    col("token_version", Text, nullable=False),
    col("expires_at", DateTime(timezone=True), nullable=False),
    col("hard_deadline", DateTime(timezone=True), nullable=False),
    col("revoked", Boolean, nullable=False),
    col("registration_id", Text, nullable=False),
    col("registration_digest", Text, nullable=False),
    col("next_seq", BigInteger, nullable=False),
)
Index(
    "ux_runner_current_owner",
    runner_owners.c.task_id,
    unique=True,
    postgresql_where=runner_owners.c.current.is_(True),
)
Index(
    "ux_runner_registration", runner_owners.c.task_id, runner_owners.c.registration_id, unique=True
)

runner_bindings = scoped(
    "runner_bindings",
    col("owner_epoch", BigInteger, nullable=False),
    col("registration_id", Text, nullable=False),
    col("registration_digest", Text, nullable=False),
    col("binding", JSONB, nullable=False),
    col("business", JSONB, nullable=False),
    col("prepare_request_id", Text, nullable=False),
    col("payload_json", Text, nullable=False),
    col("payload_digest", Text, nullable=False),
    col("status", Text, nullable=False),
    col("sdk_session_id", Text),
    col("cursor", JSONB),
    col("permit_expires_at", DateTime(timezone=True)),
    col("permit_revision", BigInteger, nullable=False),
    col("prompt_revision", BigInteger, nullable=False),
    col("instructions", Text, nullable=False),
)
Index(
    "ux_runner_binding_registration",
    runner_bindings.c.task_id,
    runner_bindings.c.registration_id,
    unique=True,
)

runner_commands = scoped(
    "runner_commands",
    col("owner_epoch", BigInteger, nullable=False),
    col("request_id", Text, nullable=False),
    col("command_seq", BigInteger, nullable=False),
    col("payload_json", Text, nullable=False),
    col("payload_digest", Text, nullable=False),
    col("kind", Text, nullable=False),
    col("binding_id", Text),
    col("input_id", Text),
    col("message_proof", JSONB),
    col("native_proof", JSONB),
    col("status", Text, nullable=False),
    col("origin_epoch", BigInteger, nullable=False),
)
Index(
    "ux_runner_command_request",
    runner_commands.c.task_id,
    runner_commands.c.owner_epoch,
    runner_commands.c.request_id,
    unique=True,
)
Index(
    "ux_runner_command_seq",
    runner_commands.c.task_id,
    runner_commands.c.owner_epoch,
    runner_commands.c.command_seq,
    unique=True,
)

runner_receipts = scoped(
    "runner_receipts",
    col("owner_epoch", BigInteger, nullable=False),
    col("kind", Text, nullable=False),
    col("request_id", Text, nullable=False),
    col("binding_id", Text),
    col("body_text", Text, nullable=False),
    col("body_digest", Text, nullable=False),
    col("response", JSONB, nullable=False),
    col("late", Boolean, nullable=False),
)
Index(
    "ix_runner_receipt_request",
    runner_receipts.c.task_id,
    runner_receipts.c.owner_epoch,
    runner_receipts.c.kind,
    runner_receipts.c.request_id,
)

# Call identity and one-shot dispatch evidence; native USD fees have their own source IDs.
runner_calls = scoped(
    "runner_calls",
    col("kind", Text, nullable=False),
    col("binding_id", Text, nullable=False),
    col("owner_epoch", BigInteger, nullable=False),
    col("state", Text, nullable=False),
    col("concurrency_active", Boolean, nullable=False),
    col("provider_key", Text),
    col("data", JSONB, nullable=False),
    col("created_at", DateTime(timezone=True), nullable=False),
)
Index(
    "ix_runner_calls_active",
    runner_calls.c.kind,
    runner_calls.c.concurrency_active,
    runner_calls.c.provider_key,
)
Index(
    "ix_runner_calls_binding",
    runner_calls.c.task_id,
    runner_calls.c.binding_id,
    runner_calls.c.state,
)

native_task_budgets = Table(
    "native_task_budgets",
    metadata,
    col("task_id", UUID(as_uuid=True), primary_key=True),
    col("accounting_source_id", Text, nullable=False),
    col("key_name", Text, nullable=False),
    col("key_fingerprint", Text, nullable=False),
    col("key_ciphertext", Text, nullable=False),
    col("native_resource_id", Text),
    col("publication_state", Text, nullable=False),
    col("config", JSONB, nullable=False),
    col("status_snapshot", JSONB),
    col("hold_reason", Text),
    col("created_at", DateTime(timezone=True), nullable=False),
)
Index(
    "ux_native_task_key",
    native_task_budgets.c.accounting_source_id,
    native_task_budgets.c.key_fingerprint,
    unique=True,
)

task_runner_resources = Table(
    "task_runner_resources",
    metadata,
    col("task_id", UUID(as_uuid=True), primary_key=True),
    col("operation_id", Text, nullable=False, unique=True),
    col("request_id", Text, nullable=False),
    col("request_digest", Text, nullable=False),
    col("request", JSONB, nullable=False),
    col("config", JSONB),
    col("controller_id", Text),
    col("controller_backend_pid", Integer),
    col("controller_backend_start", DateTime(timezone=True)),
    col("controller_generation", BigInteger, nullable=False),
    col("revision", BigInteger, nullable=False),
    col("namespace", Text, nullable=False),
    col("state", Text, nullable=False),
    col("desired", Text, nullable=False),
    col("hard_deadline", DateTime(timezone=True), nullable=False),
    col("sandbox_create_claimed", Boolean, nullable=False),
    col("tenant_key_hash", Text, nullable=False, unique=True),
    col("tenant_key_ciphertext", Text, nullable=False),
    col("observation", JSONB, nullable=False),
    col("sandbox_host", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    col("data", JSONB, nullable=False),
    col("last_error", Text),
    col("next_check_at", DateTime(timezone=True), nullable=False),
    col("created_at", DateTime(timezone=True), nullable=False),
)

sandbox_command_records = scoped(
    "sandbox_command_records",
    Column("sequence", BigInteger, Identity(), nullable=False),
    col("binding_id", Text, nullable=False),
    col("agent_id", Text, nullable=False),
    col("role", Text, nullable=False),
    col("challenge_id", UUID(as_uuid=True)),
    col("assignment_id", Text, nullable=False),
    col("generation", BigInteger, nullable=False),
    col("epoch", BigInteger, nullable=False),
    col("execution_id", Text),
    col("state", Text, nullable=False),
    col("exit_code", Integer),
    col("created_at", DateTime(timezone=True), nullable=False),
    col("started_at", DateTime(timezone=True)),
    col("finished_at", DateTime(timezone=True)),
    col("data", JSONB, nullable=False),
)
Index(
    "ix_sandbox_commands_page",
    sandbox_command_records.c.task_id,
    sandbox_command_records.c.sequence,
)

native_task_mcp_routes = Table(
    "native_task_mcp_routes",
    metadata,
    col("task_id", UUID(as_uuid=True), primary_key=True),
    col("accounting_source_id", Text, nullable=False),
    col("resource_id", Text, nullable=False),
    col("config", JSONB, nullable=False),
    col("value", JSONB, nullable=False),
    col("desired", Text, nullable=False),
    col("publication_state", Text, nullable=False),
    col("revision", Integer, nullable=False),
    col("operation_id", Text),
    col("next_check_at", DateTime(timezone=True), nullable=False),
    col("last_error", Text),
    col("created_at", DateTime(timezone=True), nullable=False),
)
Index(
    "ux_native_mcp_route",
    native_task_mcp_routes.c.accounting_source_id,
    native_task_mcp_routes.c.resource_id,
    unique=True,
)

native_request_costs = Table(
    "native_request_costs",
    metadata,
    col("accounting_source_id", Text, primary_key=True),
    col("native_log_id", Text, primary_key=True),
    col("task_id", UUID(as_uuid=True), nullable=False),
    col("attempt_id", Text, nullable=False),
    col("state", Text, nullable=False),
    col("cost_usd", Text),
    col("evidence", JSONB, nullable=False),
    col("created_at", DateTime(timezone=True), nullable=False),
)
Index("ix_native_cost_task", native_request_costs.c.task_id, native_request_costs.c.attempt_id)
