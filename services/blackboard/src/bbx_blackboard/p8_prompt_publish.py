"""Publish the P8 bundled prompts without replacing user edits or frozen Tasks."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from bbx_contracts.ctf import load_ctf_profile
from bbx_contracts.models import AgentProfile
from bbx_contracts.profile import load_profile
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import create_async_engine

from bbx_blackboard.ctf_profile_publish import _latest
from bbx_blackboard.native_ctf_publication import load_publication
from bbx_blackboard.profiles import ProfileStore, _content, _digest, _row_content, ctf_profile
from bbx_blackboard.settings import Settings
from bbx_blackboard.store import schema as s

# Exact bundled P7 prompt bodies, including final newlines.
_ORDINARY_P7 = {
    "explore": "f2e9d30f0e48664c0abf719a595afbadf4f519fe8876c3e43c68c3770c97ea40",
    "derive": "09bdcbba12175232974bc51a44135af2644934496f1f26216df0e9dc89efe635",
    "close": "d2116606e01c505bdd7a4e7659d0b572a15c79450295dda134b68c99a7705865",
}
_CTF_P7 = {
    "lead": "ed5a99282a99c75a3151465bb0d08ffc69cf66f8693465f3a4eeb8965fe8b09e",
    "teammate": "d2da7c285e9ba0587a263c1a64fbb5f0f4551257464fcfd75a9a0685cdca3219",
}
_PROGRESS_P7 = "c7078410d0d064f91ff11f8127e2967edffb8e24a811bef38922437c0a8fb813"


def _sha(prompt: str) -> str:
    return hashlib.sha256(prompt.encode()).hexdigest()


def _upgrade(row, bundled, baseline):
    content = _row_content(row)
    current = content["prompt_templates"]
    source = bundled.prompt_templates.model_dump()
    change = {role: _sha(current[role]) == before for role, before in baseline.items()}
    templates = {role: source[role] if change[role] else text for role, text in current.items()}
    content["prompt_templates"] = templates
    candidate = (
        ctf_profile(content) if "lead" in templates else AgentProfile.model_validate(content)
    )
    if _digest(_content(candidate)) != _digest(content):
        raise ValueError("Prompt publication would normalize unrelated settings")
    return candidate, {
        role: "updated"
        if templates[role] != current[role]
        else "already_current"
        if current[role] == source[role]
        else "preserved"
        for role in current
    }


async def publish(
    store: ProfileStore,
    directory: Path,
    ctf_version: int,
    *,
    apply_plan: str | None = None,
) -> dict[str, Any]:
    default, _ = load_profile(directory)
    single, _ = load_profile(directory.parent / "single")
    bundled = {
        "default": default,
        "single": single,
        "ctf": load_ctf_profile(directory.parent / "ctf"),
    }
    async with store.engine.begin() as conn:
        result: dict[str, Any] = {"profiles": {}, "published_ctf_version": ctf_version}
        changes = {}
        for name, replacement in bundled.items():
            latest = await _latest(conn, name)
            if name == "ctf" and latest["version"] != ctf_version:
                query = select(s.agent_profiles).where(
                    s.agent_profiles.c.name == name, s.agent_profiles.c.version == ctf_version
                )
                row = dict((await conn.execute(query)).mappings().one())
            else:
                row = latest
            candidate, roles = _upgrade(
                row, replacement, _CTF_P7 if name == "ctf" else _ORDINARY_P7
            )
            before, after = _digest(_row_content(latest)), _digest(_content(candidate))
            if name == "ctf" and latest["version"] != ctf_version and before != after:
                raise ValueError("CTF has another unpublished version; review it before switching")
            changes[name] = (candidate, latest["version"])
            result["profiles"][name] = {
                "before_version": latest["version"],
                "version": latest["version"] + int(before != after),
                "before_sha256": before,
                "after_sha256": after,
                "roles": roles,
            }

        progress = s.app_settings
        query = select(progress.c.value).where(progress.c.key == "progress")
        value = (
            await conn.execute(query.with_for_update() if apply_plan else query)
        ).scalar_one_or_none()
        reset_progress = value is not None and _sha(value["prompt"]) == _PROGRESS_P7
        result["progress"] = {
            "before_revision": value["revision"] if value is not None else None,
            "settings_sha256": _digest(value) if value is not None else None,
            "selection": "bundled"
            if not value or reset_progress or not value["prompt"]
            else "custom",
            "bundled_sha256": _sha(
                (directory / "prompts" / "progress.md.j2").read_text(encoding="utf-8")
            ),
        }
        plan_sha256 = _digest(result)
        if apply_plan is not None:
            if plan_sha256 != apply_plan:
                raise ValueError("Prompt publication changed; review a fresh dry-run")
            for name, (candidate, expected) in changes.items():
                saved = await store.create_in_connection(
                    conn, name, candidate, "p8-prompt-publish", expected_version=expected
                )
                assert saved["version"] == result["profiles"][name]["version"]
            if reset_progress and value is not None:
                await conn.execute(
                    update(progress)
                    .where(progress.c.key == "progress")
                    .values(value={**value, "prompt": "", "revision": value["revision"] + 1})
                )
        return {"applied": apply_plan is not None, "plan_sha256": plan_sha256, **result}


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply-plan", help="SHA-256 returned by the reviewed dry-run")
    args = parser.parse_args()
    engine = None
    try:
        settings = Settings()  # pyright: ignore[reportCallIssue]
        publication = load_publication(settings).publication
        if publication is None:
            raise ValueError("Verified native CTF publication is required")
        engine = create_async_engine(settings.database_url, pool_pre_ping=True)
        result = await publish(
            ProfileStore(engine),
            settings.profiles_dir,
            publication.ctf_profile_version,
            apply_plan=args.apply_plan,
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as error:
        # Do not print SQL, connection arguments or profile text.
        print(
            f"Prompt publication rejected ({type(error).__name__}); review a fresh dry-run.",
            file=sys.stderr,
        )
        return 1
    finally:
        if engine is not None:
            await engine.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
