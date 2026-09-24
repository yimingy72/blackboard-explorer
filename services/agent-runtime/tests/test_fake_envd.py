"""In-process envd behavior without shell execution or Docker."""

import asyncio
import io
import tarfile
from pathlib import Path

import httpx
import pytest
import zstandard
from bbx_runtime.testing.fake_envd import CommandOutput, FakeEnvd
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


@pytest.mark.asyncio
async def test_fake_envd_internal_api_and_archive(tmp_path: Path) -> None:
    fake = FakeEnvd(tmp_path, evidence_max_bytes=4, archive_max_bytes=1)
    transport = httpx.ASGITransport(app=fake.app)
    async with fake, httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/health")).status_code == 401
        client.headers["Authorization"] = f"Bearer {fake.token}"
        assert (await client.get("/health")).json() == {"status": "ok"}
        assert (await client.post("/users", json={"agent_id": "invalid;id"})).status_code == 400
        user = await client.post("/users", json={"agent_id": "agent-1"})
        assert user.json() == {
            "agent_id": "agent-1",
            "home": "/workspace/agents/agent-1",
        }
        assert (await client.post("/users", json={"agent_id": "agent-1"})).status_code == 200
        assert (tmp_path / "agents" / "agent-1").is_dir()

        path = "/workspace/agents/agent-1/proof.txt"
        fake.put_file(path, b"proof")
        assert (await client.get("/stat", params={"path": path})).json() == {
            "exists": True,
            "size": 5,
            "is_file": True,
            "is_dir": False,
        }
        assert (await client.get("/stat", params={"path": "/workspace/missing"})).json() == {
            "exists": False
        }
        assert (await client.get("/files", params={"path": path})).status_code == 413
        fake.evidence_max_bytes = 5
        response = await client.get("/files", params={"path": path})
        assert response.status_code == 200 and response.content == b"proof"
        head = await client.head("/files", params={"path": path})
        assert head.status_code == 200 and head.headers["content-length"] == "5"
        assert (
            await client.get("/files", params={"path": "/workspace/../etc/passwd"})
        ).status_code == 400
        assert (
            await client.get(
                "/files", params={"path": str(tmp_path / "agents" / "agent-1" / "proof.txt")}
            )
        ).status_code == 400
        (tmp_path / "agents" / "agent-1" / "external").symlink_to(tmp_path.parent)
        assert (
            await client.get("/files", params={"path": "/workspace/agents/agent-1/external"})
        ).status_code == 400

        fake.put_file("/workspace/shared/not-in-fallback.txt", b"shared")
        fake.put_file("/workspace/agents/agent-1/skip.o", b"object")
        fake.put_file("/workspace/agents/agent-1/node_modules/skip.txt", b"dependency")
        archive = await client.post("/archive")
        assert archive.status_code == 200
        assert archive.headers["x-archive-fallback"] == "agents-only"
        raw = zstandard.ZstdDecompressor().decompress(archive.content)
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            names = tar.getnames()
        assert "agents/agent-1/proof.txt" in names
        assert all(
            "not-in-fallback" not in name and "skip.o" not in name and "node_modules" not in name
            for name in names
        )


@pytest.mark.asyncio
async def test_fake_envd_mcp_scripted_outputs_and_truncation(tmp_path: Path) -> None:
    fake = FakeEnvd(tmp_path)
    fake.set_command(
        "show evidence",
        CommandOutput(stdout="done\n", files={"/workspace/agents/agent-1/evidence.txt": b"proof"}),
        agent_id="agent-1",
    )
    fake.set_command("large output", CommandOutput(stdout=b"x" * 80000))
    async with fake:
        transport = httpx.ASGITransport(app=fake.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.post("/mcp")).status_code == 401
            assert (
                await client.post(
                    "/users",
                    headers={"Authorization": f"Bearer {fake.token}"},
                    json={"agent_id": "agent-1"},
                )
            ).status_code == 200

        headers = {"Authorization": f"Bearer {fake.token}", "X-Agent-Id": "agent-1"}
        async with httpx.AsyncClient(transport=transport, headers=headers) as client:
            async with streamable_http_client("http://test/mcp", http_client=client) as (
                reader,
                writer,
                _,
            ):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    assert [tool.name for tool in tools.tools] == ["execute_command"]
                    result = await session.call_tool(
                        "execute_command", {"command": "show evidence"}
                    )
                    assert not result.isError
                    assert result.structuredContent == {
                        "exit_code": 0,
                        "stdout": "<command_output>done\n</command_output>",
                        "stderr": "<command_output></command_output>",
                        "truncated": False,
                        "full_output_path": None,
                    }
                    assert fake.calls[-1]["cwd"] == "/workspace/agents/agent-1"
                    large = await session.call_tool("execute_command", {"command": "large output"})
                    assert not large.isError
                    assert large.structuredContent is not None
                    assert large.structuredContent["truncated"] is True
                    output_path = large.structuredContent["full_output_path"]
                    assert (
                        tmp_path / "agents" / "agent-1" / ".outputs" / "1.txt"
                    ).read_bytes() == b"x" * 80000
                    assert output_path == "/workspace/agents/agent-1/.outputs/1.txt"
                    denied = await session.call_tool("execute_command", {"command": "not scripted"})
                    assert denied.isError

        unknown_headers = {"Authorization": f"Bearer {fake.token}", "X-Agent-Id": "agent-2"}
        async with httpx.AsyncClient(transport=transport, headers=unknown_headers) as client:
            async with streamable_http_client("http://test/mcp", http_client=client) as (
                reader,
                writer,
                _,
            ):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    unknown = await session.call_tool(
                        "execute_command", {"command": "show evidence"}
                    )
                    assert unknown.isError

    assert (tmp_path / "agents" / "agent-1" / "evidence.txt").read_bytes() == b"proof"


@pytest.mark.asyncio
async def test_fake_envd_tracks_overlapping_commands_and_rejects_shell_chaining(
    tmp_path: Path,
) -> None:
    fake = FakeEnvd(
        tmp_path,
        command_outputs={
            "first": {"stdout": "one", "delay": 0.02},
            "second": {"stdout": "two", "delay": 0.02},
        },
    )
    (tmp_path / "agents" / "agent-1").mkdir()
    results = await asyncio.gather(
        fake.run_command("agent-1", "first", None, 120, False),
        fake.run_command("agent-1", "second", None, 120, False),
    )
    assert [item["stdout"] for item in results] == [
        "<command_output>one</command_output>",
        "<command_output>two</command_output>",
    ]
    assert fake.commands == ["first", "second"]
    assert fake.max_concurrent_commands == 2
    fake.set_command("apt-get install x; id", CommandOutput())
    with pytest.raises(ValueError, match="privileged command"):
        await fake.run_command("agent-1", "apt-get install x; id", None, 120, True)
