"""The explicit paid entry point cleans its own deployment on failure."""

import httpx
import pytest

from eval.runner import batch


async def test_batch_cleanup_runs_when_first_profile_fails(tmp_path, monkeypatch):
    target = tmp_path / "eval/targets/dist/mini-shop.tar.gz"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"fixture")
    monkeypatch.setattr(batch, "ROOT", tmp_path)
    monkeypatch.setattr(batch, "changed_interfaces", lambda path: ["GET /orders"])
    calls = []

    class Checkpoint:
        def __init__(self, *args):
            self.environment = {"SERVICE_TOKEN": "test-only"}
            self.project = "test-project"

        async def docker(self, *args):
            calls.append(args)
            return "127.0.0.1:12345" if args[0] == "port" else ""

    async def failing_run(**kwargs):
        assert kwargs["exec_image"] == "bbx-eval-env:latest"
        raise RuntimeError("fixture failure")

    real_client = httpx.AsyncClient
    monkeypatch.setattr(batch, "ComposeCheckpoint", Checkpoint)
    monkeypatch.setattr(batch, "run_batch", failing_run)
    monkeypatch.setattr(
        batch.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200))
        ),
    )
    with pytest.raises(RuntimeError, match="fixture failure"):
        await batch.run(tmp_path / "results", 5, 10)
    assert ("stop", "agent-runtime") in calls
    assert calls[-1] == ("down", "-v", "--remove-orphans")
    assert (tmp_path / "results/target.tar.gz").read_bytes() == b"fixture"
