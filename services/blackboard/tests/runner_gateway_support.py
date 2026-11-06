"""Synthetic registered Tasks and native SDK identities for the PostgreSQL suite."""

import hashlib
import json
from pathlib import Path
from uuid import uuid4

from bbx_blackboard.platform import ModelInput, PlatformStore
from bbx_blackboard.profiles import ProfileStore
from bbx_blackboard.store import schema as s
from bbx_blackboard.task_board import TaskBoardService
from bbx_contracts.challenge_board import BOARD_CONTRACT
from bbx_contracts.ctf import CtfAgentProfile, load_ctf_profile
from bbx_contracts.runner import PROTOCOL, runner_json_text
from sqlalchemy import select
from test_board_flags_integration import Objects, bind_owner, identity, install_fake_runner
from test_ctf_api import settings
from test_runner_control_integration import (
    SERVICE,
    acquire,
    confirm,
    confirmation,
    control,
    cursor,
    empty_cursor,
    mgmt,
    poll_query,
    register_owner,
)


def digest(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def cap(scope, audience="model"):
    return {
        "Authorization": "Bearer " + scope["credentials"][audience]["token"],
        "Content-Type": "application/json",
    }


def private(scope):
    return f"/api/tasks/{scope['tid']}/ctf/runtime/runner-gateway"


async def prepare(
    http,
    ctf,
    *,
    base_url="http://bounded-provider.invalid/v1",
    provider="openai_chat",
    billing="fixed",
    max_cost="1",
    concurrent=2,
    tools=None,
    native=True,
    credentials_status=200,
):
    if ctf.objects is None:
        ctf.objects = Objects()
    platform = PlatformStore(ctf.repo.engine, settings().agent_token_secret.get_secret_value())
    name = "bounded-" + uuid4().hex
    model_name = "deepseek-flash" if provider == "deepseek" else "bounded-fake"
    stored = await platform.save(
        "models",
        name,
        ModelInput.model_validate(
            {
                "label": "Bounded synthetic route",
                "provider": provider,
                "model": model_name,
                "base_url": base_url,
                "reasoning_effort": "none",
                "supports_vision": False,
                "context_window": 16384,
                "max_output_tokens": 128,
                "price": {
                    "billing_mode": billing,
                    "currency": "CNY",
                    "cache_hit_per_m": "0.5" if billing != "fixed" else "1",
                    "cache_miss_per_m": "0.5" if billing != "fixed" else "1",
                    "output_per_m": "0.5" if billing != "fixed" else "1",
                    "off_peak": billing != "fixed",
                },
                "api_key": "provider-fixture-key",
                "credential_source": "stored",
            }
        ),
        "test",
    )
    config = stored["config"]
    template = load_ctf_profile(Path(__file__).resolve().parents[3] / "profiles/ctf").model_dump(
        mode="json"
    )
    template["model"], template["role_run_options"] = config, {}
    profile_name = "gateway-" + uuid4().hex
    profile = await ProfileStore(ctf.repo.engine).create(
        profile_name, CtfAgentProfile.model_validate(template), "test"
    )
    tid = await ctf.create_task(
        {
            "mode": "ctf",
            "goal": "Synthetic gateway integration",
            "budget": {
                "max_cost": max_cost,
                "max_minutes": 60,
            },
            "ctf_options": {"max_teammates": 2},
            "agent_profile": profile_name,
        },
        profile_version=profile["version"],
        business_contract=BOARD_CONTRACT,
    )
    await install_fake_runner(ctf, tid)
    owner = await register_owner(http, tid)
    model = {
        "id": model_name,
        "api": "openai-completions",
        "input": ["text"],
        "reasoning": False,
        "thinking_level": "off",
        "context_window": 16384,
        "max_output_tokens": 128,
    }
    response = await http.post(
        mgmt(tid) + "/bindings",
        headers=SERVICE,
        json={
            "request_id": str(uuid4()),
            "expected_epoch": 1,
            "agent_id": "lead",
            "model": model,
            "model_credential_ref": "model-lead",
            "mcp_credential_ref": "mcp-lead",
            "allowed_tools": tools or [],
            "instructions": "Use the declared business tools. Never invent platform identities.",
            "profile_revision": profile["version"],
            "prompt_revision": 1,
        },
    )
    assert response.status_code == 200, response.text
    binding = response.json()["binding"]
    commands = await http.get(
        control(tid) + "/commands",
        headers={"Authorization": "Bearer " + owner["token"]},
        params=poll_query(owner),
    )
    command = commands.json()["commands"][0]
    response = await http.post(
        f"/api/tasks/{tid}/ctf/runtime/runner-gateway/bindings/{binding['binding_id']}/credentials",
        headers=SERVICE,
        json={"expected_epoch": 1},
    )
    assert response.status_code == credentials_status, response.text
    scope = {
        "tid": tid,
        "owner": owner,
        "binding": binding,
        "command": command,
        "credentials": response.json(),
        "model": model,
        "config": config,
        "profile_revision": profile["version"],
    }
    if native:
        await acquire(http, tid, owner, binding, command)
        first = cursor(empty_cursor())
        body = confirmation(owner, binding, empty_cursor(), first)
        body["sdk_session_id"] = "gateway-sdk-" + uuid4().hex
        await confirm(http, tid, owner, binding, body)
        scope.update(sdk=body["sdk_session_id"], journal=first)
    return scope


def attempt(scope, *, purpose="turn", marker="DO_NOT_COPY_FULL_MODEL_HISTORY"):
    wire = {
        "model": scope["config"]["model"],
        "messages": [{"role": "user", "content": marker}],
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_completion_tokens": scope["model"]["max_output_tokens"],
    }
    body_json = runner_json_text(wire)
    return {
        "protocol": PROTOCOL,
        "fence": scope["owner"]["fence"],
        "binding": scope["binding"],
        "sdk_session_id": scope["sdk"],
        "call_id": "call-" + uuid4().hex,
        "attempt_id": "attempt-" + uuid4().hex,
        "journal": scope["journal"],
        "purpose": purpose,
        "profile_revision": scope["profile_revision"],
        "prompt_revision": 1,
        "model_snapshot": scope["model"],
        "body_json": body_json,
        "body_digest": digest(body_json),
    }


async def admit(http, scope, body):
    return await http.post(
        f"/api/tasks/{scope['tid']}/runner-model/attempts/admit",
        headers=cap(scope),
        content=runner_json_text(body).encode(),
    )


async def dispatch(http, scope, body):
    return await http.post(
        private(scope) + f"/model/{body['attempt_id']}/dispatch",
        headers={**SERVICE, "X-BBX-Runner-Authorization": cap(scope)["Authorization"]},
        json={
            "protocol": PROTOCOL,
            "fence": scope["owner"]["fence"],
            "binding_id": scope["binding"]["binding_id"],
            "sdk_session_id": body["sdk_session_id"],
            "call_id": body["call_id"],
            "attempt_id": body["attempt_id"],
            "body_digest": body["body_digest"],
        },
    )


async def call_record(ctf, tid, key):
    async with ctf.repo.engine.connect() as conn:
        return dict(
            (
                await conn.execute(
                    select(s.runner_calls).where(
                        s.runner_calls.c.task_id == tid, s.runner_calls.c.id == key
                    )
                )
            )
            .mappings()
            .one()
        )


async def worker_scope(http, ctf, scope, *, tools=None, output_cap=64):
    tid, epoch = scope["tid"], scope["owner"]["fence"]["epoch"]
    async with ctf.repo.engine.connect() as conn:
        members = (
            (await conn.execute(select(s.ctf_members).where(s.ctf_members.c.task_id == tid)))
            .mappings()
            .all()
        )
    lead = next(item for item in members if item["role"] == "lead")
    worker = next(item for item in members if item["role"] == "teammate")
    leader = identity(tid, lead["id"], lead["generation"], lead["current_turn_id"])
    actor = identity(tid, worker["id"], worker["generation"], worker["current_turn_id"])
    challenge = await TaskBoardService(ctf).create_challenge(
        tid, leader, {"request_id": str(uuid4()), "title": "Current Worker Assignment"}
    )
    await bind_owner(ctf, tid, challenge, actor)
    model = {**scope["model"], "max_output_tokens": output_cap}
    response = await http.post(
        mgmt(tid) + "/bindings",
        headers=SERVICE,
        json={
            "request_id": str(uuid4()),
            "expected_epoch": epoch,
            "agent_id": worker["id"],
            "challenge_id": challenge["id"],
            "model": model,
            "model_credential_ref": "model-worker",
            "mcp_credential_ref": "mcp-worker",
            "allowed_tools": tools or [],
            "instructions": "Use only the current Assignment.",
            "profile_revision": scope["profile_revision"],
            "prompt_revision": 1,
        },
    )
    assert response.status_code == 200, response.text
    binding = response.json()["binding"]
    commands = (
        await http.get(
            control(tid) + "/commands",
            headers={"Authorization": "Bearer " + scope["owner"]["token"]},
            params=poll_query(scope["owner"]),
        )
    ).json()["commands"]
    command = next(
        item
        for item in commands
        if json.loads(item["payload_json"]).get("spec", {}).get("binding", {}).get("binding_id")
        == binding["binding_id"]
    )
    await acquire(http, tid, scope["owner"], binding, command)
    first = cursor(empty_cursor())
    body = confirmation(scope["owner"], binding, empty_cursor(), first)
    body["sdk_session_id"] = "gateway-worker-sdk-" + uuid4().hex
    await confirm(http, tid, scope["owner"], binding, body)
    response = await http.post(
        f"/api/tasks/{tid}/ctf/runtime/runner-gateway/bindings/{binding['binding_id']}/credentials",
        headers=SERVICE,
        json={"expected_epoch": epoch},
    )
    assert response.status_code == 200, response.text
    return {
        **scope,
        "binding": binding,
        "command": command,
        "credentials": response.json(),
        "sdk": body["sdk_session_id"],
        "journal": first,
        "model": model,
        "challenge": challenge,
    }


def invocation(scope, name, arguments, suffix="one"):
    args = runner_json_text(arguments)
    entry, call = "assistant-" + suffix, "tool-call-" + suffix
    iid = digest(
        runner_json_text(
            [str(scope["tid"]), scope["binding"]["binding_id"], scope["sdk"], entry, call, name]
        )
    )
    return {
        "protocol": PROTOCOL,
        "fence": scope["owner"]["fence"],
        "binding": scope["binding"],
        "sdk_session_id": scope["sdk"],
        "invocation_id": iid,
        "tool_call_id": call,
        "assistant_entry_id": entry,
        "tool_name": name,
        "arguments_json": args,
        "arguments_digest": digest(args),
        "journal": scope["journal"],
    }
