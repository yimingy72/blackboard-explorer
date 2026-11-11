"""The P8 prompt release preserves prior versions and unrelated user settings."""

from pathlib import Path

import pytest
from bbx_blackboard import p8_prompt_publish
from bbx_blackboard.profiles import ProfileStore
from bbx_blackboard.store import schema as s
from bbx_contracts.ctf import load_ctf_profile
from bbx_contracts.profile import load_profile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from test_ctf_integration import ctf_url as ctf_url

ROOT = Path(__file__).resolve().parents[3] / "profiles"
pytestmark = pytest.mark.integration


async def test_publish_is_cas_protected_and_preserves_custom_and_old_templates(
    ctf_url, monkeypatch
):
    engine = create_async_engine(ctf_url)
    try:
        store = ProfileStore(engine)
        default, _ = load_profile(ROOT / "default")
        default.prompt_templates.explore = "Old bundled Explore"
        default.prompt_templates.derive = "User edited Derive"
        single, _ = load_profile(ROOT / "single")
        single.prompt_templates.explore = "Old bundled Explore"
        ctf = load_ctf_profile(ROOT / "ctf")
        ctf.prompt_templates.lead = "Old bundled Lead {{ member_id }}"
        progress = {
            "revision": 0,
            "prompt": "Old bundled Progress",
            "enabled_tools": ["read_progress_sources", "get_progress_tree", "update_progress_tree"],
            "model_id": None,
        }
        monkeypatch.setitem(
            p8_prompt_publish._ORDINARY_P7,
            "explore",
            p8_prompt_publish._sha(default.prompt_templates.explore),
        )
        monkeypatch.setitem(
            p8_prompt_publish._CTF_P7, "lead", p8_prompt_publish._sha(ctf.prompt_templates.lead)
        )
        monkeypatch.setattr(
            p8_prompt_publish, "_PROGRESS_P7", p8_prompt_publish._sha(progress["prompt"])
        )
        previous = {
            name: await store.create(name, profile, "fixture")
            for name, profile in {"default": default, "single": single, "ctf": ctf}.items()
        }
        async with engine.begin() as conn:
            await conn.execute(s.app_settings.insert().values(key="progress", value=progress))
        planned = await p8_prompt_publish.publish(store, ROOT / "default", 1)
        assert not planned["applied"]
        assert planned["profiles"]["default"]["roles"]["derive"] == "preserved"
        with pytest.raises(ValueError, match="fresh dry-run"):
            await p8_prompt_publish.publish(store, ROOT / "default", 1, apply_plan="stale")
        assert all([await store.get(name) == row for name, row in previous.items()])
        saved = await p8_prompt_publish.publish(
            store, ROOT / "default", 1, apply_plan=planned["plan_sha256"]
        )
        assert saved["applied"]
        for name, row in previous.items():
            current = await store.get(name)
            assert current["version"] == 2
            assert await store.get(name, 1) == row
            assert (
                current["worker_tools"] == row["worker_tools"]
                and current["params"] == row["params"]
            )
        assert (await store.get("default"))["prompt_templates"]["derive"] == "User edited Derive"
        async with engine.connect() as conn:
            result = await conn.scalar(
                select(s.app_settings.c.value).where(s.app_settings.c.key == "progress")
            )
        assert result == {**progress, "revision": 1, "prompt": ""}
        repeated = await p8_prompt_publish.publish(store, ROOT / "default", 1)
        assert all(change["version"] == 2 for change in repeated["profiles"].values())
    finally:
        await engine.dispose()
