"""The evaluation runner uses only a scripted HTTP boundary."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from eval.runner import run as runner
from eval.runner.run import run_batch

TASK = "mini-shop-review"
TOKEN = "service-test-secret-value"


class ScriptedBoard:
    def __init__(self, status: str = "finished") -> None:
        self.status = status
        self.published: list[dict[str, Any]] = []
        self.created: list[dict[str, Any]] = []
        self.stopped: list[str] = []
        self.state_calls: dict[str, int] = {}
        self.output_dir: Path | None = None
        self.start_metadata: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        path = request.url.path
        if path == "/api/profiles/default/versions" and request.method == "POST":
            body = json.loads(request.content)
            self.published.append(body)
            return httpx.Response(200, json={"name": "default", "version": 7, "profile": body})
        if path == "/api/profiles/single/versions" and request.method == "POST":
            body = json.loads(request.content)
            self.published.append(body)
            return httpx.Response(200, json={"name": "single", "version": 3, "profile": body})
        if path == "/api/tasks" and request.method == "POST":
            body = json.loads(request.content)
            self.created.append(body)
            number = len(self.created)
            return httpx.Response(200, json={"id": f"00000000-0000-4000-8000-{number:012d}"})
        if path.endswith("/start"):
            if self.output_dir is not None:
                metadata = self.output_dir / f"run-{len(self.created):03d}" / "run.json"
                self.start_metadata.append(json.loads(metadata.read_text()))
            return httpx.Response(200, json={"status": "provisioning", "events": []})
        if path.endswith("/stop"):
            self.stopped.append(path.split("/")[3])
            return httpx.Response(200, json={"status": "closing", "events": []})
        if path.endswith("/state"):
            task_id = path.split("/")[3]
            calls = self.state_calls.get(task_id, 0) + 1
            self.state_calls[task_id] = calls
            if self.stopped:
                status = "closing" if calls == 2 else "stopped"
            elif self.status == "running":
                status = "running"
            else:
                status = "running" if calls == 1 else self.status
            final = status in {"finished", "failed", "stopped"}
            return httpx.Response(
                200,
                json={
                    "task": {
                        "status": status,
                        "report_uri": f"reports/{task_id}.md" if status == "finished" else None,
                        "workspace_uri": f"workspace/{task_id}.tar.zst" if final else None,
                        "usage": {"cost": "0.12"},
                    },
                    "facts": {"F1": {"evidence": [{"uri": f"evidence/{task_id}/text.txt"}]}},
                    "intents": {},
                    "agents": {},
                    "acceptance": {},
                },
            )
        if path.endswith("/events"):
            task_id = path.split("/")[3]
            return httpx.Response(
                200,
                json=[
                    {
                        "type": "tool_call.recorded",
                        "payload": {"result_uri": f"toolcalls/{task_id}/call.txt"},
                    }
                ],
            )
        if path.endswith("/report"):
            return httpx.Response(200, text="# Evaluation report\nEvidence is reproducible.\n")
        if path.endswith("/workspace"):
            return httpx.Response(200, content=b"tar-zstd-fixture")
        if path == "/api/evidence":
            return httpx.Response(200, content=f"evidence:{request.url.params['uri']}".encode())
        raise AssertionError(f"Unexpected request: {request.method} {path}")


@pytest.mark.asyncio
async def test_fixed_profile_and_complete_exports(tmp_path: Path) -> None:
    server = ScriptedBoard()
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as http:
        out = tmp_path / "batch"
        server.output_dir = out
        results = await run_batch(
            task=TASK,
            profile="default",
            n=2,
            out=out,
            base_url="http://board",
            token=TOKEN,
            exec_image="bbx-eval-env:latest",
            http_client=http,
            poll_interval=0.001,
        )
        assert [result["outcome"] for result in results] == ["success", "success"]
        assert len(server.published) == 1
        assert server.published[0]["exec_image"] == "bbx-eval-env:latest"
        assert [item["profile_version"] for item in server.created] == [7, 7]
        assert [item["outcome"] for item in server.start_metadata] == ["running", "running"]
        assert [item["task_id"] for item in server.start_metadata] == [
            f"00000000-0000-4000-8000-{number:012d}" for number in (1, 2)
        ]
        for number in (1, 2):
            run = out / f"run-{number:03d}"
            metadata = json.loads((run / "run.json").read_text())
            assert metadata["profile_name"] == "default"
            assert metadata["profile_version"] == 7
            assert metadata["profile"]["exec_image"] == "bbx-eval-env:latest"
            assert metadata["status"] == "finished"
            assert metadata["elapsed_seconds"] >= 0
            assert metadata["export_seconds"] >= 0
            assert (run / "state.json").is_file()
            assert (run / "events.json").is_file()
            assert "Evaluation report" in (run / "report.md").read_text()
            assert (run / "workspace.tar.zst").read_bytes() == b"tar-zstd-fixture"
            mapping = json.loads((run / "evidence/index.json").read_text())
            assert len(mapping) == 2
            assert all(
                (run / path).read_bytes().startswith(b"evidence:") for path in mapping.values()
            )
        with pytest.raises(FileExistsError):
            await run_batch(
                task=TASK,
                profile="default",
                n=1,
                out=out,
                base_url="http://board",
                token=TOKEN,
                http_client=http,
            )


@pytest.mark.asyncio
async def test_export_time_is_excluded_from_elapsed_seconds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = runner._download

    async def slow_download(*args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(0.05)
        await original(*args, **kwargs)

    monkeypatch.setattr(runner, "_download", slow_download)
    server = ScriptedBoard()
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as http:
        result = (
            await run_batch(
                task=TASK,
                profile="default",
                n=1,
                out=tmp_path / "slow-export",
                base_url="http://board",
                token=TOKEN,
                http_client=http,
                poll_interval=0.001,
            )
        )[0]
    assert result["outcome"] == "success"
    assert result["export_seconds"] > 0.1
    assert result["elapsed_seconds"] < result["export_seconds"]


@pytest.mark.asyncio
async def test_timeout_stops_only_its_task_and_records_terminal_state(tmp_path: Path) -> None:
    server = ScriptedBoard(status="running")
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as http:
        result = (
            await run_batch(
                task=TASK,
                profile="default",
                n=1,
                out=tmp_path / "timeout",
                base_url="http://board",
                token=TOKEN,
                http_client=http,
                timeout=0.005,
                settle_timeout=0.03,
                poll_interval=0.001,
            )
        )[0]
    assert result["outcome"] == "timeout"
    assert result["status"] == "stopped"
    assert server.stopped == [result["task_id"]]
    assert (tmp_path / "timeout/run-001/workspace.tar.zst").is_file()


@pytest.mark.asyncio
async def test_failed_task_is_not_success(tmp_path: Path) -> None:
    server = ScriptedBoard(status="failed")
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as http:
        result = (
            await run_batch(
                task=TASK,
                profile="default",
                n=1,
                out=tmp_path / "failed",
                base_url="http://board",
                token=TOKEN,
                http_client=http,
                poll_interval=0.001,
            )
        )[0]
    assert result["status"] == result["outcome"] == "failed"
    assert (tmp_path / "failed/run-001/report.md").read_text() == ""


@pytest.mark.asyncio
async def test_single_profile_uses_one_agent_and_disables_derive(tmp_path: Path) -> None:
    server = ScriptedBoard()
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as http:
        result = (
            await run_batch(
                task=TASK,
                profile="single",
                n=1,
                out=tmp_path / "single",
                base_url="http://board",
                token=TOKEN,
                http_client=http,
                poll_interval=0.001,
            )
        )[0]
    assert result["outcome"] == "success"
    assert server.created[0]["budget"]["max_concurrent_agents"] == 1
    assert server.created[0]["profile_version"] == 3
    assert server.published[0]["params"]["derive_enabled"] is False


@pytest.mark.asyncio
async def test_cancel_stops_task_and_leaves_failure_metadata(tmp_path: Path) -> None:
    server = ScriptedBoard(status="running")
    waiting = asyncio.Event()

    async def block(_: float) -> None:
        waiting.set()
        await asyncio.Event().wait()

    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as http:
        task = asyncio.create_task(
            run_batch(
                task=TASK,
                profile="default",
                n=1,
                out=tmp_path / "cancelled",
                base_url="http://board",
                token=TOKEN,
                http_client=http,
                sleep=block,
            )
        )
        await waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(server.stopped) == 1
    result = json.loads((tmp_path / "cancelled/run-001/run.json").read_text())
    assert result["outcome"] == "interrupted"
    assert (tmp_path / "cancelled/run-001/state.json").is_file()
    assert (tmp_path / "cancelled/run-001/report.md").is_file()


def test_cli_reports_failure_without_printing_token(monkeypatch, capsys, tmp_path: Path) -> None:
    async def failed_batch(**_: Any) -> list[dict[str, Any]]:
        return [{"task_id": "one", "outcome": "failed", "elapsed_seconds": 0.1}]

    monkeypatch.setattr(runner, "run_batch", failed_batch)
    monkeypatch.setenv("SERVICE_TOKEN", TOKEN)
    code = runner.main(
        [
            "--task",
            TASK,
            "--profile",
            "default",
            "--n",
            "1",
            "--out",
            str(tmp_path / "x"),
        ]
    )
    assert code == 1
    assert TOKEN not in capsys.readouterr().out
