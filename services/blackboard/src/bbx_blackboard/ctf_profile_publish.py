"""Publish matching CTF templates without rewriting task or Session snapshots.

Run inside the configured blackboard environment. The default is read-only;
applying a reviewed plan requires both expected profile versions.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from copy import deepcopy
from typing import Any

from bbx_contracts.ctf import CtfAgentProfile, CtfOptions, ctf_tool_capability_issues
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from bbx_blackboard.profiles import ProfileStore, _content, _digest, _row_content, ctf_profile
from bbx_blackboard.settings import Settings
from bbx_blackboard.store import schema as s

FAMILIES = ("ctf", "ctf-task-settings")
HANDOFF_MARKERS = {
    "lead": "context_blocked表示本次模型输入无法安全容纳，不等于题目不可解",
    "teammate": "一旦形成可复核的阶段成果或完成候选，先登记必要产物",
}


def publication_plan(
    rows: dict[str, dict[str, Any]],
    *,
    context_threshold: int | None = None,
    reserved_output_tokens: int | None = None,
) -> tuple[dict[str, CtfAgentProfile], dict[str, Any]]:
    """Validate every change before publishing; expose only structural digests and numbers."""
    profiles = {name: ctf_profile(rows[name]) for name in FAMILIES}
    for name, profile in profiles.items():
        if _digest(_content(profile)) != _digest(_row_content(rows[name])):
            raise ValueError(f"{name} cannot be republished without changing unspecified fields")
    templates = profiles["ctf"].prompt_templates.model_dump(mode="json")
    if not all(marker in templates[role] for role, marker in HANDOFF_MARKERS.items()):
        raise ValueError("Latest ctf templates lack the required evidence handoff guidance")
    target = profiles["ctf-task-settings"]
    window = target.model.context_window
    if context_threshold is not None:
        if (
            window is None
            or context_threshold < target.options.context_threshold
            or context_threshold > window
        ):
            raise ValueError(
                "Explicit context threshold must preserve or raise it within the model window"
            )
        target.options = CtfOptions.model_validate(
            {**target.options.model_dump(), "context_threshold": context_threshold}
        )
    if reserved_output_tokens is not None and (
        reserved_output_tokens < 1 or (window is not None and reserved_output_tokens >= window)
    ):
        raise ValueError("Output reserve must leave positive model input capacity")
    target.prompt_templates = profiles["ctf"].prompt_templates.model_copy(deep=True)
    expected = deepcopy(_row_content(rows["ctf-task-settings"]))
    expected["prompt_templates"] = templates
    if context_threshold is not None:
        expected["params"]["context_threshold"] = context_threshold
    if _digest(_content(target)) != _digest(expected):
        raise ValueError("Publication would modify unspecified task settings")
    template_digest = _digest(templates)
    if any(
        _digest(profile.prompt_templates.model_dump(mode="json")) != template_digest
        for profile in profiles.values()
    ):
        raise ValueError("Candidate templates do not match before publication")
    summary: dict[str, Any] = {"profiles": {}}
    for name, profile in profiles.items():
        before = _digest(_row_content(rows[name]))
        after = _digest(_content(profile))
        reserve = reserved_output_tokens if name == "ctf-task-settings" else None
        available = (
            max(0, profile.model.context_window - reserve)
            if profile.model.context_window is not None and reserve is not None
            else None
        )
        summary["profiles"][name] = {
            "missing_readback_tools": ctf_tool_capability_issues(profile),
            "before_version": rows[name]["version"],
            "version": rows[name]["version"] + int(before != after),
            "changed": before != after,
            "profile_digest_before": before,
            "profile_digest_after": after,
            "template_digest_before": _digest(rows[name]["prompt_templates"]),
            "template_digest_after": template_digest,
            "context_threshold": profile.options.context_threshold,
            "model_context_window": profile.model.context_window,
            "max_steps": profile.options.max_steps,
            "max_teammates": profile.options.max_teammates,
            "reserved_output_tokens": reserve,
            "available_input_tokens": available,
            "hard_input_limit_tokens": int(min(profile.options.context_threshold, available) * 0.8)
            if available is not None
            else None,
        }
    return profiles, summary


async def _latest(conn: AsyncConnection, name: str) -> dict[str, Any]:
    result = await conn.execute(
        select(s.agent_profiles)
        .where(s.agent_profiles.c.name == name)
        .order_by(s.agent_profiles.c.version.desc())
        .limit(1)
    )
    row = result.mappings().first()
    if row is None:
        raise ValueError(f"Missing profile family: {name}")
    return dict(row)


async def publish_ctf_profiles(
    store: ProfileStore,
    *,
    apply: bool = False,
    expected_versions: dict[str, int] | None = None,
    context_threshold: int | None = None,
    reserved_output_tokens: int | None = None,
) -> dict[str, Any]:
    if apply and (expected_versions is None or set(expected_versions) != set(FAMILIES)):
        raise ValueError("Apply requires both reviewed expected profile versions")
    manager = store.engine.begin() if apply else store.engine.connect()
    async with manager as conn:
        rows = {name: await _latest(conn, name) for name in FAMILIES}
        for name, version in (expected_versions or {}).items():
            if name not in rows or rows[name]["version"] != version:
                raise ValueError("Profile version changed; review a fresh dry-run")
        profiles, summary = publication_plan(
            rows,
            context_threshold=context_threshold,
            reserved_output_tokens=reserved_output_tokens,
        )
        if apply:
            if any(ctf_tool_capability_issues(profile) for profile in profiles.values()):
                raise ValueError("Publication requires the readback tools shown in the dry-run")
            # The shared transaction rolls back both writes on either CAS conflict.
            for name in FAMILIES:
                saved = await store.create_in_connection(
                    conn,
                    name,
                    profiles[name],
                    "ctf-profile-publish",
                    expected_version=rows[name]["version"],
                )
                summary["profiles"][name]["version"] = saved["version"]
        return {"applied": apply, **summary}


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply the reviewed publication")
    parser.add_argument("--expected-ctf-version", type=int)
    parser.add_argument("--expected-task-settings-version", type=int)
    parser.add_argument("--task-context-threshold", type=int, help="Explicit target threshold")
    parser.add_argument(
        "--reserved-output-tokens", type=int, help="Target runtime output reserve for sizing"
    )
    args = parser.parse_args(argv)
    expected = {
        name: version
        for name, version in zip(
            FAMILIES, (args.expected_ctf_version, args.expected_task_settings_version), strict=True
        )
        if version is not None
    }
    engine = None
    try:
        if args.apply and set(expected) != set(FAMILIES):
            parser.error(
                "--apply requires both --expected-ctf-version and --expected-task-settings-version"
            )
        settings = Settings()  # pyright: ignore[reportCallIssue]
        engine = create_async_engine(settings.database_url, pool_pre_ping=True)
        summary = await publish_ctf_profiles(
            ProfileStore(engine),
            apply=args.apply,
            expected_versions=expected,
            context_threshold=args.task_context_threshold,
            reserved_output_tokens=args.reserved_output_tokens,
        )
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as error:
        # Exceptions may include connection credentials or SQL profile values.
        print(
            f"CTF profile publication rejected ({type(error).__name__}); review a fresh dry-run.",
            file=sys.stderr,
        )
        return 1
    finally:
        if engine is not None:
            await engine.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
