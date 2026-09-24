from pathlib import Path

import bbx_blackboard.api as api
import httpx
import pytest
from bbx_blackboard.settings import Settings
from pydantic import SecretStr


@pytest.mark.asyncio
async def test_static_files_and_spa_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "index.html").write_text("<main>Blackboard</main>", encoding="utf-8")
    (tmp_path / "asset.js").write_text("window.bbx = true", encoding="utf-8")
    monkeypatch.setattr(api, "_web_dist", lambda: tmp_path)
    settings = Settings.model_construct(
        postgres_password=SecretStr("postgres-test"),
        minio_root_password=SecretStr("minio-test"),
        service_token=SecretStr("service-test"),
        agent_token_secret=SecretStr("jwt-test-secret-with-32-or-more-chars"),
        admin_users=SecretStr("alice:password-test"),
    )
    app = api.create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", trust_env=False
    ) as client:
        assert (await client.get("/asset.js")).text == "window.bbx = true"
        assert (await client.get("/tasks/some-task")).text == "<main>Blackboard</main>"
        assert (await client.get("/api/does-not-exist")).status_code == 404
        assert (await client.get("/missing.js")).status_code == 404
