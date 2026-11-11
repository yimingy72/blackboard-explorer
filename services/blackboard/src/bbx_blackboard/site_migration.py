"""P7's one-time, empty-secondary PostgreSQL cutover. Never run on app startup."""

import argparse
import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from bbx_blackboard.store import schema as s

_CONFIG = (s.agent_profiles, s.platform_configs, s.app_settings)
_REVISION = "0032"
# This pre-upgrade inventory is independent of the current ORM and later migrations.
_TABLES = frozenset(
    """
    agent_messages agent_profiles agent_runs agent_sessions alembic_version app_settings
    board_announcements ctf_challenges ctf_members ctf_messages ctf_records ctf_turns events facts
    flag_candidates flag_submissions gateway_execution_attempts gateway_execution_budgets
    gateway_task_models intents legacy_execution_networks legacy_execution_receipts
    native_request_costs native_task_budgets native_task_mcp_routes native_vpn_assets
    pi_model_calls pi_task_models platform_configs progress_jobs resume_requests runner_bindings
    runner_calls runner_commands runner_owners runner_receipts sandbox_command_records task_counters
    task_input_groups task_progress task_runner_resources task_runs tasks tool_calls
""".split()
)


class CutoverBlocked(RuntimeError):
    """The exact cutpoint no longer matches the empty-secondary transfer contract."""


async def _require_cutpoint(conn: AsyncConnection, expected_system_id: str) -> None:
    actual = await conn.execute(
        text("SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname = 'public'")
    )
    if set(actual.scalars()) != _TABLES:
        raise CutoverBlocked("PostgreSQL table inventory differs from the approved cutpoint")
    version = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalars().all()
    system_id = (
        await conn.execute(text("SELECT system_identifier::text FROM pg_control_system()"))
    ).scalar_one()
    if version != [_REVISION] or system_id != expected_system_id:
        raise CutoverBlocked("Database identity or schema differs from the approved cutpoint")


async def _config_rows(conn: AsyncConnection) -> dict[str, list[dict[str, Any]]]:
    return {
        table.name: [
            dict(row)
            for row in (
                await conn.execute(select(table).order_by(*table.primary_key.columns))
            ).mappings()
        ]
        for table in _CONFIG
    }


def _digest(config: dict[str, list[dict[str, Any]]]) -> str:
    encoded = json.dumps(config, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


async def configuration_digest(conn: AsyncConnection) -> str:
    """Compare full immutable configuration at cutover without exposing config or secrets."""
    return _digest(await _config_rows(conn))


async def import_secondary_profile(
    primary: AsyncConnection,
    secondary: AsyncConnection,
    *,
    primary_system_id: str,
    secondary_system_id: str,
    secondary_config_sha256: str,
    check_only: bool = False,
) -> bool:
    """Within explicit stopped-site transactions, copy only the exact original ctf@1."""
    if not primary.in_transaction() or not secondary.in_transaction():
        raise CutoverBlocked("Both cutover connections must be inside transactions")
    if primary_system_id == secondary_system_id:
        raise CutoverBlocked("Source and target must be different PostgreSQL clusters")
    await _require_cutpoint(primary, primary_system_id)
    await _require_cutpoint(secondary, secondary_system_id)

    # The caller must keep old application writers off and verify external resources and backups.
    tables = ", ".join(f'public."{name}"' for name in sorted(_TABLES))
    await secondary.execute(text(f"LOCK TABLE {tables} IN SHARE MODE NOWAIT"))
    for name in sorted(_TABLES - {"alembic_version", *(table.name for table in _CONFIG)}):
        if (await secondary.execute(text(f'SELECT 1 FROM public."{name}" LIMIT 1'))).first():
            raise CutoverBlocked(f"Secondary has business data in {name}")
    config = await _config_rows(secondary)
    if _digest(config) != secondary_config_sha256:
        raise CutoverBlocked("Secondary configuration differs from the approved cutpoint")
    profiles = {(row["name"], row["version"]): row for row in config[s.agent_profiles.name]}
    if set(profiles) != {("default", 1), ("single", 1), ("ctf", 1)} or any(
        not row["deleted"] or row["secret_ciphertext"] is not None
        for row in config[s.platform_configs.name]
    ):
        raise CutoverBlocked(
            "Secondary has profiles or credential material outside the cutpoint contract"
        )

    await primary.execute(text(f"LOCK TABLE {tables} IN SHARE ROW EXCLUSIVE MODE NOWAIT"))
    active = (
        (s.tasks, s.tasks.c.deleting | ~s.tasks.c.status.in_(("finished", "failed", "stopped"))),
        (s.runner_owners, s.runner_owners.c.current & ~s.runner_owners.c.revoked),
        (s.runner_calls, s.runner_calls.c.state.in_(("admitted", "dispatched", "unknown"))),
        (s.pi_model_calls, s.pi_model_calls.c.state.in_(("prepared", "dispatched", "unknown"))),
        (s.progress_jobs, s.progress_jobs.c.status.in_(("queued", "running"))),
        (s.agent_messages, s.agent_messages.c.status.in_(("queued", "processing"))),
        (
            s.ctf_messages,
            (s.ctf_messages.c.purpose == "review")
            & s.ctf_messages.c.status.in_(("queued", "leased")),
        ),
        (s.ctf_turns, s.ctf_turns.c.status == "running"),
        (s.gateway_execution_attempts, s.gateway_execution_attempts.c.fee_state != "known"),
        (
            s.task_runner_resources,
            (s.task_runner_resources.c.state != "stopped")
            | ~s.task_runner_resources.c.data.contains({"exit_confirmed": True}),
        ),
    )
    for source, condition in active:
        if (await primary.execute(select(1).select_from(source).where(condition).limit(1))).first():
            raise CutoverBlocked("Primary still has work that requires the original writer")
    table = s.agent_profiles
    original = profiles["ctf", 1]
    matches = (
        (
            await primary.execute(
                select(table).where(table.c.name == "ctf").order_by(table.c.version)
            )
        )
        .mappings()
        .all()
    )
    if matches:
        if len(matches) == 1 and dict(matches[0]) == original:
            return False
        raise CutoverBlocked("Primary CTF profile conflicts with the exact original row")
    if not check_only:
        await primary.execute(table.insert().values(**original))
    return True


async def run_cutpoint(path: Path, *, apply: bool) -> str:
    manifest = json.loads(path.read_text())
    keys = {"primary_system_id", "secondary_system_id", "secondary_config_sha256"}
    original = {key: manifest[key] for key in keys}
    if any(not isinstance(value, str) for value in original.values()) or not (
        re.fullmatch(r"[0-9]{10,24}", original["primary_system_id"])
        and re.fullmatch(r"[0-9]{10,24}", original["secondary_system_id"])
        and re.fullmatch(r"[a-f0-9]{64}", original["secondary_config_sha256"])
    ):
        raise CutoverBlocked("Invalid cutpoint manifest")
    primary = create_async_engine(os.environ["P7_PRIMARY_DATABASE_URL"])
    try:
        secondary = create_async_engine(os.environ["P7_SECONDARY_DATABASE_URL"])
        try:
            async with primary.begin() as target, secondary.begin() as source:
                changed = await import_secondary_profile(
                    target, source, **original, check_only=not apply
                )
        finally:
            await secondary.dispose()
    finally:
        await primary.dispose()
    return ("imported" if apply else "would_import") if changed else "already_exact"


def main():
    parser = argparse.ArgumentParser(description="Copy only the exact P7 ctf@1 profile")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        disposition = asyncio.run(run_cutpoint(args.manifest, apply=args.apply))
    except CutoverBlocked as error:
        parser.exit(1, f"Cutpoint blocked: {error}\n")
    except Exception:
        parser.exit(1, "Cutpoint blocked: could not verify the original database or manifest.\n")
    print(json.dumps({"disposition": disposition, "profile": "ctf@1"}))


if __name__ == "__main__":
    main()
