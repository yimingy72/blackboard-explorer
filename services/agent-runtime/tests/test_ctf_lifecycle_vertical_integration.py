"""CTF archive/resume/purge with real HTTP/PG/MAF and explicitly fake execution."""

import asyncio
import io
import tarfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
import zstandard
from bbx_blackboard.api import create_app
from bbx_blackboard.settings import Settings as BoardSettings
from bbx_objects import ObjectStore
from bbx_runtime.clients import BlackboardClient
from bbx_runtime.ctf.client import CtfClient
from bbx_runtime.ctf.coordinator import CtfCoordinator
from bbx_runtime.ctf.runner import CtfRunner
from bbx_runtime.execenv import ExecEnvHandle
from bbx_runtime.execenv.archive import build_archive
from bbx_runtime.execenv.manager import ArchiveResult
from bbx_runtime.scheduler.supervisor import TaskSupervisor
from bbx_runtime.settings import Settings
from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import create_async_engine
from test_ctf_vertical_integration import (
    ConcurrentScriptedClient,
    MemoryObjects,
    call,
    run_ready_turns,
)
from test_ctf_vertical_integration import ctf_vertical_database as ctf_vertical_database

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[3]
SCRIPT = "/workspace/shared/ctf/saved.py"


class LifecycleObjects(MemoryObjects):
    async def stream(self, uri):
        yield self.files[uri]

    async def list(self, prefix=""):
        return [uri for uri in self.files if uri.startswith(prefix)]

    async def remove(self, uri):
        self.files.pop(uri, None)


class SimulatedExecution:
    """Exercise runtime protocols; this adapter never starts an OS command."""

    def __init__(self, service):
        self.service = service
        self.envd = self
        self.boot_id = str(uuid4())
        self.files = {SCRIPT: b"print('preserved evidence')\n"}
        self.calls = []

    async def prepare(self, tid, member, turn):
        await self.service.runtime(
            tid,
            "record_execution",
            agent_id=member["id"],
            turn_id=turn["id"],
            generation=turn["generation"],
            boot_id=self.boot_id,
        )

    async def execute(self, tid, member_id, command):
        self.calls.append((tid, member_id, command))
        return {"exit_code": 0, "stdout": "simulated command only"}

    async def stat(self, path, **kwargs):
        return {"is_file": path in self.files, "size": len(self.files.get(path, b""))}

    @asynccontextmanager
    async def file_stream(self, path, **kwargs):
        yield httpx.Response(200, content=self.files[path])

    async def stop_member(self, tid, member):
        execution = member["execution"]
        proof = {
            "boot_id": execution["boot_id"],
            "generation": execution["generation"],
            "drained": True,
        }
        await self.service.runtime(
            tid, "record_execution_drained", agent_id=member["id"], proof=proof
        )
        return proof

    async def drain(self, tid):
        for member in (await self.service.state(tid))["members"]:
            execution = member.get("execution")
            if execution and not execution.get("drained"):
                await self.service.runtime(
                    tid,
                    "record_execution_drained",
                    agent_id=member["id"],
                    proof={
                        "boot_id": execution["boot_id"],
                        "generation": execution["generation"],
                        "drained": True,
                    },
                )
        return True


class SimulatedContainers:
    """Persist real archive bytes but simulate container destruction and restoration."""

    def __init__(self, objects, execution, directory):
        self.objects, self.execution, self.directory = objects, execution, directory
        self.handles = {}
        self.restores = []
        self.destroyed = []

    async def find(self, tid):
        return self.handles.get(str(tid))

    async def provision(self, tid, profile, restore_uri=None):
        tid = str(tid)
        self.restores.append(restore_uri)
        self.execution.boot_id = str(uuid4())
        if restore_uri:
            raw = zstandard.ZstdDecompressor().decompress(
                self.objects.files[restore_uri], max_output_size=2**24
            )
            with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
                script = archive.extractfile("shared/ctf/saved.py")
                assert script is not None
                self.execution.files = {SCRIPT: script.read()}
        handle = ExecEnvHandle(
            UUID(tid), str(uuid4()), "simulated-ctf", "http://unused.invalid", "unused"
        )
        self.handles[tid] = handle
        return handle

    async def wait_healthy(self, handle):
        return None

    async def ensure_initial_inputs(self, handle, attachments):
        return None

    async def archive_task(self, tid, data):
        output = self.directory / f"{tid}-{data['run_number']}.tar.zst"
        await build_archive(cast(ObjectStore, self.objects), UUID(str(tid)), data, output)
        uri = (
            f"workspace/{tid}.tar.zst"
            if data["run_number"] == 1
            else f"workspace/{tid}/run-{data['run_number']}.tar.zst"
        )
        await self.objects.put(uri, output.read_bytes())
        return ArchiveResult(uri, output.stat().st_size, "none")

    async def destroy(self, tid):
        self.destroyed.append(str(tid))
        self.handles.pop(str(tid), None)
        self.execution.files = {}


async def test_parallel_help_archive_resume_removed_history_and_isolated_purge(
    ctf_vertical_database, tmp_path
):
    settings = BoardSettings(
        postgres_password=SecretStr("isolated-test"),
        minio_root_password=SecretStr("unused"),
        service_token=SecretStr("ctf-lifecycle-test"),
        agent_token_secret=SecretStr("ctf-lifecycle-isolated-test-signing-key-32bytes"),
        admin_users=SecretStr("admin:test"),
        profiles_dir=ROOT / "profiles/default",
    )
    engine = create_async_engine(ctf_vertical_database)
    objects = LifecycleObjects()
    app = create_app(settings, engine=engine, objects=objects)
    await app.state.profile_store.ensure_bundled(settings.profiles_dir, app.state.platform_store)
    coordinator = None
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://board", trust_env=False
        ) as http:
            board = BlackboardClient("http://board", "ctf-lifecycle-test", http)
            ctf = CtfClient(board)
            spec = {
                "mode": "ctf",
                "goal": "Archive and explicitly continue simulated teamwork",
                "budget": {"max_cost": "100", "max_minutes": 20},
            }
            tid = (await board.create_task(spec))["id"]
            other = (await board.create_task(spec))["id"]
            sentinel = f"evidence/{other}/sentinel.txt"
            await objects.put(sentinel, b"keep other task")
            await board.start_task(tid)
            await ctf.runtime(tid, "start")
            lead = await ctf.runtime(tid, "claim_turn", agent_id="lead", runtime_instance="setup")

            async def lead_tool(name, **body) -> Any:
                return await ctf.tool(tid, lead["token"], name, **body)

            for name in ("Owner", "Helper"):
                await lead_tool("create_teammate", display_name=name, request_id=str(uuid4()))
            challenge = await lead_tool(
                "create_challenge", request_id=str(uuid4()), title="Difficult analysis"
            )
            cid = challenge["id"]
            challenge = await lead_tool(
                "update_challenge",
                challenge_id=cid,
                request_id=str(uuid4()),
                expected_revision=challenge["revision"],
                action="assign",
                owner_id="member-1",
            )
            challenge = await lead_tool(
                "update_challenge",
                challenge_id=cid,
                request_id=str(uuid4()),
                expected_revision=challenge["revision"],
                action="collaborators",
                collaborator_ids=["member-2"],
            )
            for mid in ("member-1", "member-2"):
                await lead_tool(
                    "send_message",
                    recipient_id=mid,
                    message_id=str(uuid4()),
                    kind="instruction",
                    body=f"Inspect shared challenge {cid}",
                )
            await ctf.runtime(
                tid,
                "finish_turn",
                agent_id="lead",
                turn_id=lead["id"],
                generation=lead["generation"],
                end_reason="completed",
                answer="Setup complete",
            )
            phase = "work"
            entered, ready, clients = set(), asyncio.Event(), []

            def factory(member, profile):
                mid = member["id"]
                if mid == "member-1" and mid not in entered:
                    steps = [
                        call(
                            "request_help",
                            challenge_id=cid,
                            expected_revision=challenge["revision"],
                            body="Need checksum insight",
                            attempted_routes="Manual inspection",
                            observations_and_basis="Checksum differs",
                            failure_conditions="Header omitted",
                            current_blocker="Input boundary unclear",
                            help_needed="Check header inclusion",
                            no_artifacts_reason="No script from manual inspection",
                        ),
                        ScriptStep(text="Owner needs help"),
                    ]
                elif mid == "member-2" and mid not in entered:
                    steps = [
                        call("execute_command", command="inspect simulated evidence"),
                        call("register_artifact", path=SCRIPT),
                        call(
                            "append_record",
                            challenge_id=cid,
                            body="Helper preserved a reproducible script",
                        ),
                        ScriptStep(text="Helper evidence saved"),
                    ]
                elif mid == "lead" and phase == "finish":
                    steps = [
                        call(
                            "finish_task",
                            summary="Partial simulated analysis preserved",
                            unresolved_items=["Checksum analysis"],
                            evidence_refs=[],
                            lead_claim=False,
                        ),
                        ScriptStep(text="Closing"),
                    ]
                else:
                    steps = [ScriptStep(text="Awaiting explicit next instruction")]
                client = (
                    ConcurrentScriptedClient(steps, mid, entered, ready)
                    if mid in {"member-1", "member-2"} and mid not in entered
                    else ScriptedChatClient(steps)
                )
                clients.append((mid, client))
                return client

            runtime_settings = Settings.model_construct(
                deepseek_api_key=SecretStr("unused-scripted")
            )
            execution = SimulatedExecution(ctf)
            runner = CtfRunner(
                ctf, runtime_settings, client_factory=factory, envd=execution, objects=objects
            )
            coordinator = CtfCoordinator(board, runtime_settings, runner=runner, envd=execution)
            manager = SimulatedContainers(objects, execution, tmp_path)
            await manager.provision(tid, None)
            supervisor = TaskSupervisor(
                runtime_settings, board, cast(Any, manager), cast(Any, runner)
            )
            supervisor.ctf = coordinator
            for _ in range(3):
                await run_ready_turns(coordinator, tid)
            assert ready.is_set() and len(execution.calls) == 1
            state = await ctf.state(tid)
            assert any(record["kind"] == "help_request" for record in state["records"])
            assert any(item["path"] == SCRIPT for item in state["artifacts"])
            helper_session = await board.get_agent_session(tid, "member-2")
            owner_session = await board.get_agent_session(tid, "member-1")
            assert (
                await http.post("/api/login", json={"username": "admin", "password": "test"})
            ).status_code == 200
            removal = await http.post(
                f"/api/tasks/{tid}/ctf/members/member-2/remove", json={"request_id": str(uuid4())}
            )
            assert removal.status_code == 202, removal.text
            await run_ready_turns(coordinator, tid)
            assert (
                next(m for m in (await ctf.state(tid))["members"] if m["id"] == "member-2")[
                    "lifecycle"
                ]
                == "removed"
            )
            phase = "finish"
            await http.post(
                f"/api/tasks/{tid}/agents/lead/messages",
                json={"id": str(uuid4()), "content": "Finish partial work"},
            )
            await run_ready_turns(coordinator, tid)
            deferred_id = str(uuid4())
            late = await http.post(
                f"/api/tasks/{tid}/agents/member-1/messages",
                json={"id": deferred_id, "content": "Continue only after Lead confirms"},
            )
            assert late.status_code == 200 and late.json()["deferred"]
            await coordinator.tick(tid)
            await supervisor._cleanup(tid)
            archived = (await ctf.state(tid))["task"]
            archive_uri = archived["workspace_uri"]
            assert archived["cleanup_ready"] and archive_uri in objects.files
            assert tid not in manager.handles
            phase = "resume"
            resume = await http.post(
                f"/api/tasks/{tid}/resume",
                json={"request_id": str(uuid4()), "additional_cost": "10", "additional_minutes": 5},
            )
            assert resume.status_code == 200, resume.text
            envd_client = await supervisor._ctf_envd(tid)
            await envd_client.close()
            assert manager.restores[-1] == archive_uri
            assert execution.files[SCRIPT] == b"print('preserved evidence')\n"
            await ctf.runtime(tid, "start")
            resumed = await ctf.state(tid)
            assert (
                next(m for m in resumed["members"] if m["id"] == "member-2")["lifecycle"]
                == "removed"
            )
            assert next(m for m in resumed["messages"] if m["id"] == deferred_id)["deferred"]
            owner_runs = sum(mid == "member-1" for mid, _ in clients)
            await run_ready_turns(coordinator, tid)
            assert sum(mid == "member-1" for mid, _ in clients) == owner_runs
            lead = await ctf.runtime(tid, "claim_turn", agent_id="lead", runtime_instance="confirm")
            if lead is None:
                await http.post(
                    f"/api/tasks/{tid}/agents/lead/messages",
                    json={"id": str(uuid4()), "content": "Confirm preserved owner instruction"},
                )
                lead = await ctf.runtime(
                    tid, "claim_turn", agent_id="lead", runtime_instance="confirm"
                )
            await lead_tool("confirm_messages", message_ids=[deferred_id])
            await ctf.runtime(
                tid,
                "finish_turn",
                agent_id="lead",
                turn_id=lead["id"],
                generation=lead["generation"],
                end_reason="completed",
                answer="Confirmed",
            )
            await run_ready_turns(coordinator, tid)
            owner_after = await board.get_agent_session(tid, "member-1")
            assert owner_after["session"]["session_id"] == owner_session["session"]["session_id"]
            assert owner_after["revision"] > owner_session["revision"]
            assert (await board.get_agent_session(tid, "member-2"))["session"][
                "session_id"
            ] == helper_session["session"]["session_id"]
            assert (await http.post(f"/api/tasks/{tid}/stop")).status_code == 200
            await coordinator.tick(tid)
            await supervisor._cleanup(tid)
            assert (await http.delete(f"/api/tasks/{tid}")).status_code == 202
            await supervisor._purge_deleted(tid)
            assert (await http.get(f"/api/tasks/{tid}")).status_code == 404
            assert not any(tid in key for key in objects.files)
            assert objects.files[sentinel] == b"keep other task"
            assert (await http.get(f"/api/tasks/{other}")).status_code == 200
    finally:
        if coordinator:
            await coordinator.close()
        await app.state.workspace_cache.close()
        await engine.dispose()
