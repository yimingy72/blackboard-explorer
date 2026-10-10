"""Real API, runtime, and exec-env CTF smoke runs with a scripted model."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import docker
import httpx
import pytest
from bbx_blackboard.api import create_app
from bbx_blackboard.settings import Settings as BoardSettings
from bbx_contracts.ctf import load_ctf_profile
from bbx_runtime.clients import BlackboardClient, EnvdClient
from bbx_runtime.ctf.client import CtfClient
from bbx_runtime.ctf.coordinator import CtfCoordinator
from bbx_runtime.ctf.runner import CtfRunner
from bbx_runtime.execenv.manager import ExecEnvManager
from bbx_runtime.scheduler.supervisor import TaskSupervisor
from bbx_runtime.settings import Settings
from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep
from docker.errors import ImageNotFound
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import create_async_engine
from test_ctf_vertical_integration import ConcurrentScriptedClient, MemoryObjects, call

pytest_plugins = ("test_ctf_vertical_integration",)
pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[3]


async def wait_for_state(ctf, task_id: str, predicate, timeout: float = 45) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        state = await ctf.state(task_id)
        if predicate(state):
            return state
        if asyncio.get_running_loop().time() >= deadline:
            turns = [
                (turn["agent_id"], turn["status"], turn["end_reason"]) for turn in state["turns"]
            ]
            members = [
                (member["id"], member["current_turn_id"], member["lifecycle"])
                for member in state["members"]
            ]
            messages = [
                (
                    message["recipient_id"],
                    message["status"],
                    message["purpose"],
                    message["deferred"],
                    message["notification_purpose"],
                    message["body"][:40],
                )
                for message in state["messages"]
            ]
            raise AssertionError(
                f"Timed out waiting for CTF state: {state['task']['status']} "
                f"{state['task']['ctf_control']['phase']} "
                f"turns={turns} members={members} messages={messages} "
                f"conclusion={state['task'].get('ctf_conclusion')}"
            )
        await asyncio.sleep(0.25)


@pytest.mark.asyncio
@pytest.mark.parametrize("run_index", range(3))
async def test_real_ctf_execution_chain_repeats_without_target(ctf_vertical_database, run_index):
    """Run the ordinary team lifecycle three times using only a scripted model."""
    docker_client = docker.from_env()
    try:
        docker_client.images.get("bbx-exec-env:latest")
    except ImageNotFound:
        pytest.skip("bbx-exec-env:latest is not available")

    network = await asyncio.to_thread(
        docker_client.networks.create,
        f"bbx-ctf-real-{run_index}-{uuid4().hex[:8]}",
        driver="bridge",
        internal=False,
    )
    engine = create_async_engine(ctf_vertical_database)
    board_settings = BoardSettings(
        postgres_password=SecretStr("isolated-test"),
        minio_root_password=SecretStr("unused-test"),
        service_token=SecretStr("ctf-real-service"),
        agent_token_secret=SecretStr("ctf-real-isolated-signing-key-32"),
        admin_users=SecretStr("admin:test"),
        profiles_dir=ROOT / "profiles/default",
    )
    objects = MemoryObjects()
    app = create_app(board_settings, engine=engine, objects=objects)
    await app.state.profile_store.ensure_bundled(
        board_settings.profiles_dir, app.state.platform_store
    )
    manager = None
    coordinator = None
    coordinator_task = None
    task_id = None
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://board", trust_env=False
        ) as http:
            board = BlackboardClient("http://board", "ctf-real-service", http)
            task_id = (
                await board.create_task(
                    {
                        "mode": "ctf",
                        "goal": f"Synthetic ordinary execution chain {run_index}",
                        "budget": {"max_cost": "100", "max_minutes": 20},
                        "ctf_options": {"max_teammates": 2},
                    }
                )
            )["id"]
            first_start = await board.start_task(task_id)
            second_start = await board.start_task(task_id)
            assert first_start["status"] == second_start["status"] == "provisioning"
            assert (
                await http.post("/api/login", json={"username": "admin", "password": "test"})
            ).status_code == 200

            runtime_settings = Settings.model_construct(
                deepseek_api_key=SecretStr("unused-scripted"),
                service_token=SecretStr("ctf-real-service"),
                envd_token_secret=SecretStr("ctf-real-envd-secret-32"),
                minio_root_password=SecretStr("unused-test"),
                exec_network=network.name,
                exec_access_mode="relay",
                exec_egress_mode="direct",
            )
            lead_initialized = False
            entered: set[str] = set()
            both_running = asyncio.Event()
            first_work: set[str] = set()
            clients = []

            def factory(member, _profile):
                nonlocal lead_initialized
                member_id = member["id"]
                if member_id == "lead" and not lead_initialized:
                    lead_initialized = True
                    steps = [
                        call("create_teammate", display_name="Alice"),
                        call("create_teammate", display_name="Bob"),
                        call(
                            "send_message",
                            recipient_id="member-1",
                            body="Write alpha",
                            instruction=True,
                        ),
                        call(
                            "send_message",
                            recipient_id="member-2",
                            body="Read shared alpha",
                            instruction=True,
                        ),
                        ScriptStep(text="Waiting for the team"),
                    ]
                elif member_id == "member-1" and member_id not in first_work:
                    first_work.add(member_id)
                    steps = [
                        call(
                            "execute_command",
                            command=f"printf 'alpha-{run_index}\\n' > /workspace/shared/alpha.txt",
                        ),
                        ScriptStep(text="Alpha file written"),
                    ]
                elif member_id == "member-2" and member_id not in first_work:
                    first_work.add(member_id)
                    steps = [
                        call(
                            "execute_command",
                            command=(
                                "while [ ! -s /workspace/shared/alpha.txt ]; do sleep 0.1; done; "
                                "cat /workspace/shared/alpha.txt > /workspace/shared/beta.txt"
                            ),
                        ),
                        call("register_artifact", path="/workspace/shared/beta.txt"),
                        ScriptStep(text="Beta file registered"),
                    ]
                elif member_id == "lead":
                    steps = [ScriptStep(text="Waiting for the team")]
                else:
                    steps = [ScriptStep(text="No further work")]
                if member_id == "lead":
                    # Mailbox input can arrive after this client is constructed.
                    # Select closing from actual model messages, not a fixture phase.
                    finish_index = len(steps)
                    steps.extend(
                        [
                            call(
                                "finish_task",
                                summary="Synthetic execution completed",
                                unresolved_items=[],
                                evidence_refs=[],
                                lead_claim=False,
                            ),
                            ScriptStep(text="Closing synthetic run"),
                        ]
                    )
                    client = ScriptedChatClient(
                        steps, jump_on={"Finish the synthetic run": finish_index}
                    )
                elif member_id in {"member-1", "member-2"} and member_id not in entered:
                    client = ConcurrentScriptedClient(steps, member_id, entered, both_running)
                else:
                    client = ScriptedChatClient(steps)
                clients.append((member_id, client))
                return client

            manager = ExecEnvManager(
                runtime_settings, docker_client=docker_client, objects=cast(Any, objects)
            )
            ctf_profile = load_ctf_profile(ROOT / "profiles/ctf")
            runner = CtfRunner(
                CtfClient(board),
                runtime_settings,
                client_factory=factory,
                objects=objects,
            )
            supervisor = TaskSupervisor(runtime_settings, board, manager, cast(Any, runner))
            coordinator = CtfCoordinator(
                board,
                runtime_settings,
                runner=runner,
                envd_factory=supervisor._ctf_envd,
                envd_replacement=supervisor._replace_ctf_envd,
            )
            supervisor.ctf = coordinator
            coordinator_task = asyncio.create_task(coordinator.run(task_id))

            running = await wait_for_state(
                coordinator.service,
                task_id,
                lambda state: state["task"]["ctf_control"]["phase"] == "running",
            )
            assert running["task"]["status"] == "running"
            state = await wait_for_state(
                coordinator.service,
                task_id,
                lambda current: (
                    len(
                        [
                            m
                            for m in current["messages"]
                            if m["notification_purpose"] == "assignment_result"
                        ]
                    )
                    == 2
                ),
            )
            assert both_running.is_set()
            assert {m["display_name"] for m in state["members"] if m["role"] != "lead"} == {
                "Alice",
                "Bob",
            }

            handle = await manager.find(task_id)
            assert handle is not None
            same_handle = await manager.provision(task_id, ctf_profile)
            assert same_handle.container_id == handle.container_id

            async with EnvdClient(handle.base_url, handle.token) as envd:
                assert (await envd.health())["status"] == "ok"
                beta = await envd.read_file("/workspace/shared/beta.txt")
                assert beta == f"alpha-{run_index}\n".encode()

            followup = await http.post(
                f"/api/tasks/{task_id}/agents/lead/messages",
                json={"id": str(uuid4()), "content": "Finish the synthetic run"},
            )
            assert followup.status_code == 200, followup.text
            final = await wait_for_state(
                coordinator.service,
                task_id,
                lambda current: current["task"]["ctf_control"]["phase"] == "closed",
                timeout=30,
            )
            await asyncio.wait_for(coordinator_task, 5)
            assert final["task"]["ctf_control"]["phase"] == "closed"
            assert final["task"]["status"] == "finished"
            assert (
                len(
                    [
                        m
                        for m in final["messages"]
                        if m["notification_purpose"] == "assignment_result"
                    ]
                )
                == 2
            )

            await manager.destroy(task_id)
            await manager.destroy(task_id)
            assert await manager.find(task_id) is None
    finally:
        if coordinator_task is not None and not coordinator_task.done():
            coordinator_task.cancel()
            await asyncio.gather(coordinator_task, return_exceptions=True)
        if coordinator is not None:
            await coordinator.close()
        if manager is not None and task_id is not None:
            await manager.destroy(task_id)
        await app.state.workspace_cache.close()
        await engine.dispose()
        await asyncio.to_thread(network.remove)
        docker_client.close()
