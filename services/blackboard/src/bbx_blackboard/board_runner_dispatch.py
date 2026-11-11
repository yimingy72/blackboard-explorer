"""Translate a fenced ordinary AgentRun into existing finite Runner commands."""

import json
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from bbx_contracts.runner import (
    EffectiveRunnerModel,
    InputPayload,
    McpTarget,
    RunnerBinding,
    RunnerMcpRoute,
    SessionSpec,
    runner_json_text,
    runner_payload_digest,
)
from bbx_contracts.runner_control import BoardAbortProof, BoardDispatchProof, BoardRunIdentity
from fastapi import HTTPException
from sqlalchemy import select

from bbx_blackboard.board_runner_scope import EXPLORATION_CONTRACT, require_board_run
from bbx_blackboard.pi_ledger import task_pi_model
from bbx_blackboard.store import schema as s

_SANDBOX_TOOLS = ("exec_command", "read_file", "write_file")
_COMMON = ("get", "search", "read_evidence")
_ROLE_TOOLS = {
    "explore": ("post_fact", "post_intent", "claim", "release"),
    "derive": ("post_intent",),
    "close": ("submit_close",),
}


def _id(tid, proof, name: str) -> str:
    started = proof.started_at.astimezone(UTC).isoformat()
    scope = f"{tid}:{proof.agent_id}:{proof.run_number}:{started}:{proof.derive_round}"
    return str(uuid5(NAMESPACE_URL, f"bbx:board-runner:{scope}:{name}"))


def _binding_id(tid, proof):
    if proof.derive_round is None:
        return _id(tid, proof, "binding")
    return str(uuid5(NAMESPACE_URL, f"bbx:board-derive:{tid}:{proof.run_number}:{proof.agent_id}"))


def _scope(proof):
    return BoardRunIdentity(
        run_number=proof.run_number, started_at=proof.started_at, derive_round=proof.derive_round
    ).model_dump(mode="json")


async def _next_derive_round(conn, tid, existing, proof):
    original = BoardRunIdentity.model_validate(existing["business"]["board_run"])
    if (
        original.derive_round is None
        or proof.derive_round != original.derive_round + 1
        or proof.run_number != original.run_number
        or existing["status"] != "prepared"
        or existing["sdk_session_id"] is None
        or existing["cursor"] is None
        or existing["permit_expires_at"] is None
        or existing["permit_expires_at"] <= datetime.now(UTC)
    ):
        raise HTTPException(409, "Original Derive journal cannot safely continue")
    await require_board_run(conn, tid, proof.agent_id, proof, action="new_binding")
    # The only previous round input must have its original confirmed terminal receipt.
    commands = s.runner_commands
    original_round = BoardAbortProof(
        agent_id=proof.agent_id, reason="readback", **original.model_dump()
    )
    terminal = await conn.scalar(
        select(commands.c.status).where(
            commands.c.task_id == tid,
            commands.c.binding_id == existing["id"],
            commands.c.request_id == _id(tid, original_round, "initial-request"),
            commands.c.owner_epoch == existing["owner_epoch"],
        )
    )
    if terminal != "completed":
        raise HTTPException(409, "Original Derive round outcome is unconfirmed")
    patch = {
        "business": {
            key: value
            for key, value in {**existing["business"], "board_run": _scope(proof)}.items()
            if key != "board_conclude_stamp"
        },
        "prompt_revision": existing["prompt_revision"] + 1,
        "instructions": proof.instructions,
    }
    table = s.runner_bindings
    await conn.execute(
        table.update().where(table.c.task_id == tid, table.c.id == existing["id"]).values(**patch)
    )
    return {**existing, **patch}


async def _binding(conn, tid, control, owner, proof: BoardDispatchProof):
    table = s.runner_bindings
    key = _binding_id(tid, proof)
    existing = (
        (await conn.execute(select(table).where(table.c.task_id == tid, table.c.id == key)))
        .mappings()
        .first()
    )
    if existing is not None:
        if existing["owner_epoch"] != owner["epoch"]:
            raise HTTPException(409, "Original Blackboard binding requires owner recovery")
        if existing["business"]["board_run"] != _scope(proof):
            return await _next_derive_round(conn, tid, dict(existing), proof)
        return dict(existing)
    task, agent = await require_board_run(conn, tid, proof.agent_id, proof, action="new_binding")
    frozen = await task_pi_model(conn, tid)
    if frozen is None:
        raise HTTPException(409, "Blackboard Task has no frozen Pi model")
    model = frozen[1]
    if not model.has_text_limits() or not model.tool_call or task["budget"]["currency"] != "USD":
        raise HTTPException(409, "Frozen Blackboard Pi model is incomplete")
    role = agent["task_type"]
    profile = (
        await conn.execute(
            select(s.agent_profiles.c.worker_tools).where(
                s.agent_profiles.c.name == task["agent_profile"],
                s.agent_profiles.c.version == task["agent_profile_version"],
            )
        )
    ).scalar_one_or_none()
    if profile is None:
        raise HTTPException(409, "Blackboard Task has no frozen worker profile")
    enabled = profile.get(role, {}).get("builtin")
    tools = [name for name in (*_COMMON, *_ROLE_TOOLS[role]) if enabled is None or name in enabled]
    if role == "explore":
        grants = await conn.scalar(
            select(s.task_runner_resources.c.data["native_mcp"]["explore"]).where(
                s.task_runner_resources.c.task_id == tid
            )
        )
        tools.extend(sorted(grants or {}))
    sandbox = role == "explore" and (enabled is None or "execute_command" in enabled)
    refs: dict[McpTarget, str] = {"business": f"native-mcp-{key}-business"}
    instructions = proof.instructions
    if sandbox:
        tools.extend(_SANDBOX_TOOLS)
        refs["sandbox"] = f"native-mcp-{key}-sandbox"
        instructions += (
            "\nThe workspace command tool is named exec_command, formerly execute_command."
        )
    binding = RunnerBinding(
        business_contract=EXPLORATION_CONTRACT,
        binding_id=key,
        platform_session_id=_id(tid, proof, "platform-session"),
        agent_id=proof.agent_id,
        generation=proof.derive_round or 1,
        journal_generation=_id(tid, proof, "journal"),
        assignment_ref=None,
    )
    spec = SessionSpec(
        binding=binding,
        model=EffectiveRunnerModel.model_validate(
            {
                "id": model.id,
                "model_revision": model.model_revision,
                "api": "openai-completions",
                "input": model.input_modalities,
                "reasoning": model.reasoning,
                "thinking_level": "off",
                "context_window": model.context_window,
                "max_output_tokens": model.max_output_tokens,
            }
        ),
        model_credential_ref=f"native-model-{key}",
        mcp_target_credential_refs=refs,
        allowed_tools=tools,
        mcp_tool_routes={
            name: RunnerMcpRoute(
                target="sandbox" if name in _SANDBOX_TOOLS else "business", wire_name=name
            )
            for name in tools
        },
        instructions=instructions,
        profile_revision=task["agent_profile_version"],
        prompt_revision=1,
    )
    payload = runner_json_text({"kind": "prepare", "spec": spec.model_dump(mode="json")})
    value = {
        "task_id": tid,
        "id": key,
        "owner_epoch": owner["epoch"],
        "registration_id": _id(tid, proof, "registration"),
        "registration_digest": runner_payload_digest(payload),
        "binding": binding.model_dump(mode="json"),
        "business": {"board_run": _scope(proof)},
        "prepare_request_id": _id(tid, proof, "prepare"),
        "payload_json": payload,
        "payload_digest": runner_payload_digest(payload),
        "status": "registered",
        "sdk_session_id": None,
        "cursor": None,
        "permit_expires_at": None,
        "permit_revision": 0,
        "prompt_revision": 1,
        "instructions": instructions,
    }
    await conn.execute(table.insert().values(**value))
    await control._enqueue(
        conn, tid, owner, value["prepare_request_id"], payload, value["payload_digest"]
    )
    return value


async def board_dispatch(conn, tid, control, proof: BoardDispatchProof):
    # The controller fence and Task lock were checked before entering this operation.
    owner = await control._current_owner(conn, tid)
    binding = await _binding(conn, tid, control, owner, proof)
    input_id, request_id = _id(tid, proof, "initial-input"), _id(tid, proof, "initial-request")
    previous = await conn.scalar(
        select(s.runner_commands.c.status).where(
            s.runner_commands.c.task_id == tid,
            s.runner_commands.c.owner_epoch == owner["epoch"],
            s.runner_commands.c.request_id == request_id,
        )
    )
    if previous is None:
        payload = runner_json_text(
            InputPayload(
                kind="input", binding_id=binding["id"], input_id=input_id, content=proof.input
            ).model_dump(mode="json")
        )
        value = await control._enqueue(
            conn, tid, owner, request_id, payload, runner_payload_digest(payload), board_start=True
        )
        previous = value["status"]
    if previous in {"accepted", "applied"} and binding["status"] == "prepared":
        _, agent = await require_board_run(
            conn, tid, proof.agent_id, proof, action="active_binding"
        )
        messages = s.agent_messages
        ready = (
            await conn.execute(
                select(messages.c.id, messages.c.content)
                .where(
                    messages.c.task_id == tid,
                    messages.c.agent_id == proof.agent_id,
                    messages.c.role == "user",
                    messages.c.purpose == "active",
                    messages.c.status == "queued",
                    messages.c.request_run_number == proof.run_number,
                    messages.c.request_started_at == proof.started_at,
                    messages.c.request_derive_round == agent["derive_round"],
                )
                .order_by(messages.c.created_at, messages.c.id)
                .limit(20)
            )
        ).mappings()
        for message in ready:
            payload = runner_json_text(
                InputPayload(
                    kind="input",
                    binding_id=binding["id"],
                    input_id=str(message["id"]),
                    content=message["content"],
                    mode="steer",
                ).model_dump(mode="json")
            )
            await control._enqueue(
                conn,
                tid,
                owner,
                _id(tid, proof, "user-input:" + str(message["id"])),
                payload,
                runner_payload_digest(payload),
            )
    result = {
        "binding_id": binding["id"],
        "prepare_request_id": binding["prepare_request_id"],
        "input_request_id": request_id,
        "input_id": input_id,
        "input_status": previous,
    }
    if previous == "completed":
        terminal = await conn.scalar(
            select(s.runner_receipts.c.body_text)
            .where(
                s.runner_receipts.c.task_id == tid,
                s.runner_receipts.c.binding_id == binding["id"],
                s.runner_receipts.c.request_id == request_id,
                s.runner_receipts.c.owner_epoch == owner["epoch"],
                s.runner_receipts.c.kind == "command",
                s.runner_receipts.c.response["status"].astext == "completed",
            )
            .limit(1)
        )
        if terminal is None:
            raise HTTPException(409, "Confirmed Blackboard terminal receipt is unavailable")
        result["terminal"] = json.loads(terminal)
    return result


async def board_abort(conn, tid, control, proof: BoardAbortProof):
    owner = await control._current_owner(conn, tid)
    binding_id = _binding_id(tid, proof)
    binding = await control._binding(conn, tid, binding_id, owner)
    if binding["binding"]["business_contract"] != EXPLORATION_CONTRACT:
        raise HTTPException(403, "Original Blackboard binding required")
    if binding["status"] == "closed":
        receipts, commands = s.runner_receipts, s.runner_commands
        original = await conn.scalar(
            select(receipts.c.body_text)
            .join(
                commands,
                (commands.c.task_id == receipts.c.task_id)
                & (commands.c.owner_epoch == receipts.c.owner_epoch)
                & (commands.c.request_id == receipts.c.request_id),
            )
            .where(
                receipts.c.task_id == tid,
                receipts.c.binding_id == binding_id,
                receipts.c.owner_epoch == owner["epoch"],
                receipts.c.kind == "command",
                receipts.c.response["status"].astext == "closed",
                commands.c.kind == "close",
                commands.c.status == "closed",
            )
            .limit(1)
        )
        if original is not None:
            terminal = json.loads(original)
            return {
                "binding_id": binding_id,
                "request_id": terminal["request_id"],
                "status": "aborted",
                "terminal": terminal,
            }
    payload = runner_json_text({"kind": "abort", "binding_id": binding_id})
    command = await control._enqueue(
        conn, tid, owner, _id(tid, proof, "abort"), payload, runner_payload_digest(payload)
    )
    result = {
        "binding_id": binding_id,
        "request_id": command["request_id"],
        "status": command["status"],
    }
    if command["status"] == "aborted":
        terminal = await conn.scalar(
            select(s.runner_receipts.c.body_text)
            .where(
                s.runner_receipts.c.task_id == tid,
                s.runner_receipts.c.binding_id == binding_id,
                s.runner_receipts.c.request_id == command["request_id"],
                s.runner_receipts.c.owner_epoch == owner["epoch"],
                s.runner_receipts.c.kind == "command",
                s.runner_receipts.c.response["status"].astext == "aborted",
            )
            .limit(1)
        )
        if terminal is not None:
            result["terminal"] = json.loads(terminal)
    return result
