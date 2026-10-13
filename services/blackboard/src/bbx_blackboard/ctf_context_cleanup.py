"""Explicit, dry-run-first cleanup of terminal CTF context markers."""

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from bbx_blackboard.ctf import CtfService
from bbx_blackboard.settings import Settings
from bbx_blackboard.store import schema as s


async def cleanup(
    engine: AsyncEngine, *, apply: bool = False, task_ids: Sequence[UUID] = ()
) -> list[dict]:
    """Select only terminal tasks; the service rechecks all write gates under lock."""
    query = select(s.tasks.c.id, s.tasks.c.ctf_control).where(
        s.tasks.c.mode == "ctf",
        s.tasks.c.status.in_(("finished", "failed", "stopped")),
        s.tasks.c.deleting.is_(False),
        s.tasks.c.ctf_control["phase"].astext == "closed",
    )
    if task_ids:
        query = query.where(s.tasks.c.id.in_(task_ids))
    async with engine.connect() as conn:
        candidates = (await conn.execute(query.order_by(s.tasks.c.id))).mappings().all()
    service = CtfService(engine)
    results = []
    for task in candidates:
        members = task["ctf_control"].get("context_blocked_members", {})
        if not members:
            continue
        if not apply:
            results.append({"task_id": str(task["id"]), "cleared_members": len(members)})
            continue
        try:
            result = await service.cleanup_terminal_context_blocks(task["id"])
            results.append({**result, "task_id": str(task["id"])})
        except HTTPException as error:
            results.append({"task_id": str(task["id"]), "blocked": True, "reason": error.detail})
    return results


async def run(args: argparse.Namespace) -> int:
    engine = None
    try:
        settings = Settings()  # pyright: ignore[reportCallIssue]
        engine = create_async_engine(settings.database_url, pool_pre_ping=True)
        results = await cleanup(engine, apply=args.apply, task_ids=args.task_id)
        print(json.dumps({"applied": args.apply, "tasks": results}, sort_keys=True))
        return int(any(result.get("blocked") for result in results))
    except Exception as error:
        # Database errors may contain connection details or private JSON parameters.
        print(f"CTF context cleanup failed ({type(error).__name__}).", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply audited terminal-only cleanup")
    parser.add_argument("--task-id", action="append", type=UUID, default=[])
    raise SystemExit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
