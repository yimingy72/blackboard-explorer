"""CTF task-board story across real HTTP/Postgres and declared-artifact archives."""

import hashlib
import io
import json
import tarfile
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
import zstandard
from bbx_blackboard.api import create_app
from bbx_blackboard.settings import Settings
from bbx_objects import ObjectStore
from bbx_runtime.clients import BlackboardClient
from bbx_runtime.clients.blackboard import RemoteError
from bbx_runtime.ctf.client import CtfClient
from bbx_runtime.execenv.archive import build_archive
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import create_async_engine
from test_ctf_vertical_integration import MemoryObjects
from test_ctf_vertical_integration import ctf_vertical_database as ctf_vertical_database

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[3]


class StreamingObjects(MemoryObjects):
    async def stream(self, uri):
        yield self.files[uri]


async def test_owner_help_existing_collaborator_and_registered_script_recovery(
    ctf_vertical_database, tmp_path
):
    settings = Settings(
        postgres_password=SecretStr("isolated-test"),
        minio_root_password=SecretStr("unused-test"),
        service_token=SecretStr("ctf-board-test-service"),
        agent_token_secret=SecretStr("ctf-board-isolated-test-signing-key"),
        admin_users=SecretStr("admin:test"),
        profiles_dir=ROOT / "profiles/default",
    )
    engine = create_async_engine(ctf_vertical_database)
    objects = StreamingObjects()
    app = create_app(settings, engine=engine, objects=objects)
    await app.state.profile_store.ensure_bundled(settings.profiles_dir, app.state.platform_store)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://board", trust_env=False
        ) as http:
            board = BlackboardClient("http://board", "ctf-board-test-service", http)
            client = CtfClient(board)
            specification = {
                "mode": "ctf",
                "goal": "Solve one ordinary and one difficult challenge",
                "budget": {"max_cost": "100", "max_minutes": 20},
            }
            tid = (await board.create_task(specification))["id"]
            other_tid = (await board.create_task(specification))["id"]
            await board.start_task(tid)
            await client.runtime(tid, "start")
            lead = await client.runtime(tid, "claim_turn", agent_id="lead", runtime_instance="test")

            async def tool(turn, name, **body):
                return await client.tool(tid, turn["token"], name, **body)

            for name in ("Owner", "Existing helper"):
                await tool(lead, "create_teammate", display_name=name, request_id=str(uuid4()))
            turns = {}
            for mid in ("member-1", "member-2"):
                await tool(
                    lead,
                    "send_message",
                    recipient_id=mid,
                    body="Inspect assigned work",
                    message_id=str(uuid4()),
                    kind="instruction",
                )
                turns[mid] = await client.runtime(
                    tid, "claim_turn", agent_id=mid, runtime_instance="test"
                )
            owner, helper = turns["member-1"], turns["member-2"]
            ordinary = await tool(
                lead, "create_challenge", request_id=str(uuid4()), title="Ordinary"
            )
            claimed = await tool(
                owner,
                "update_challenge",
                challenge_id=ordinary["id"],
                request_id=str(uuid4()),
                expected_revision=ordinary["revision"],
                action="claim",
            )
            with pytest.raises(RemoteError) as stale:
                await tool(
                    helper,
                    "update_challenge",
                    challenge_id=ordinary["id"],
                    request_id=str(uuid4()),
                    expected_revision=ordinary["revision"],
                    action="claim",
                )
            assert stale.value.status == 409
            complete = await tool(
                owner,
                "update_challenge",
                challenge_id=ordinary["id"],
                request_id=str(uuid4()),
                expected_revision=claimed["revision"],
                action="set_status",
                work_status="completed",
            )
            assert complete["work_status"] == "completed" and complete["owner_id"] == "member-1"
            hard = await tool(lead, "create_challenge", request_id=str(uuid4()), title="Difficult")
            hard = await tool(
                lead,
                "update_challenge",
                challenge_id=hard["id"],
                request_id=str(uuid4()),
                expected_revision=hard["revision"],
                action="assign",
                owner_id="member-1",
            )

            async def register(body):
                key = str(uuid4())
                uri = f"evidence/{tid}/{key}/replay.py"
                await objects.put(uri, body)
                return await client.runtime(
                    tid,
                    "register_artifact",
                    agent_id="member-1",
                    turn_id=owner["id"],
                    generation=owner["generation"],
                    request_id=key,
                    path="/workspace/shared/ctf/replay.py",
                    uri=uri,
                    sha256=hashlib.sha256(body).hexdigest(),
                    size=len(body),
                    filename="replay.py",
                )

            first = await register(b"print('failed route')\n")
            help_fields = {
                "attempted_routes": "Ran replay.py with the baseline input",
                "observations_and_basis": "The stored output rejects the checksum",
                "failure_conditions": "Fails whenever the checksum is nonzero",
                "current_blocker": "Cannot explain the checksum transform",
                "help_needed": "Independently inspect the checksum and compare observations",
            }
            help_request_id = str(uuid4())
            request = {
                "challenge_id": hard["id"],
                "request_id": help_request_id,
                "expected_revision": hard["revision"],
                "body": "Need help; registered script /workspace/shared/ctf/replay.py",
                "artifact_ids": [first["id"]],
                **help_fields,
            }
            help_record = await tool(owner, "request_help", **request)
            assert await tool(owner, "request_help", **request) == help_record
            assert {key: help_record[key] for key in help_fields} == help_fields
            assert help_record["artifact_refs"][0]["uri"] == first["uri"]
            blocked = await tool(lead, "get_challenge", challenge_id=hard["id"])
            assert blocked["work_status"] == "blocked" and blocked["owner_id"] == "member-1"
            shared = await tool(
                lead,
                "update_challenge",
                challenge_id=hard["id"],
                request_id=str(uuid4()),
                expected_revision=blocked["revision"],
                action="collaborators",
                collaborator_ids=["member-2"],
            )
            await tool(
                lead,
                "send_message",
                recipient_id="member-2",
                body=f"Read {help_record['id']} for {hard['id']}; discuss with member-1",
                message_id=str(uuid4()),
                kind="instruction",
            )
            # The helper first reads the shared evidence, then contacts the existing owner.
            records = await tool(helper, "list_records", challenge_id=hard["id"])
            assert help_record in records["records"]
            peer_message = await tool(
                helper,
                "send_message",
                recipient_id="member-1",
                body="I read the failed checksum route; I will copy the script before editing",
                message_id=str(uuid4()),
                kind="message",
            )
            assert peer_message["recipient_id"] == "member-1"
            supplement = await tool(
                helper,
                "append_record",
                challenge_id=hard["id"],
                request_id=str(uuid4()),
                body="The checksum includes the header; reproduce with the registered script",
                artifact_ids=[first["id"]],
            )
            assert supplement["author_id"] == "member-2"
            with pytest.raises(RemoteError):
                await tool(
                    helper,
                    "update_challenge",
                    challenge_id=hard["id"],
                    request_id=str(uuid4()),
                    expected_revision=shared["revision"],
                    action="set_status",
                    work_status="completed",
                )
            latest = await register(b"print('include header')\n")
            await tool(
                owner,
                "append_record",
                challenge_id=hard["id"],
                request_id=str(uuid4()),
                body="Corrected script; /workspace/shared/scratch.py is not registered",
                artifact_ids=[latest["id"]],
            )
            with pytest.raises(RemoteError) as foreign:
                await client.runtime(
                    tid,
                    "register_artifact",
                    agent_id="member-1",
                    turn_id=owner["id"],
                    generation=owner["generation"],
                    request_id=str(uuid4()),
                    path=first["path"],
                    uri=f"evidence/{other_tid}/{uuid4()}/replay.py",
                    sha256=first["sha256"],
                    size=first["size"],
                    filename="replay.py",
                )
            assert foreign.value.status in {409, 422}
            with pytest.raises(RemoteError) as crossed:
                await client.tool(
                    other_tid, owner["token"], "get_challenge", challenge_id=hard["id"]
                )
            assert crossed.value.status == 403
            state = await client.state(tid)
            assert len(state["members"]) == 3
            final_hard = await tool(owner, "get_challenge", challenge_id=hard["id"])
            assert final_hard["owner_id"] == "member-1"
            assert final_hard["collaborator_ids"] == ["member-2"]
            help_messages = [
                item
                for item in state["messages"]
                if item["recipient_id"] == "lead" and help_record["id"] in item["body"]
            ]
            assert len(help_messages) == 1
            snapshot = {"format": "bbx.task-archive.v1", "task_id": tid, "state": state}
            output = tmp_path / "ctf-board.tar.zst"
            await build_archive(cast(ObjectStore, objects), UUID(tid), snapshot, output)
            raw = zstandard.ZstdDecompressor().decompress(
                output.read_bytes(), max_output_size=2**24
            )
            with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
                script = archive.extractfile("shared/ctf/replay.py")
                assert script is not None and script.read() == b"print('include header')\n"
                assert "shared/scratch.py" not in archive.getnames()
                exported = archive.extractfile(".bbx/task.json")
                assert exported is not None
                saved = json.load(exported)
                old_record = next(
                    r for r in saved["state"]["records"] if r["id"] == help_record["id"]
                )
                assert old_record["artifact_refs"][0]["sha256"] == first["sha256"]
                assert old_record["artifact_refs"][0]["uri"] == first["uri"]
    finally:
        await app.state.workspace_cache.close()
        await engine.dispose()
