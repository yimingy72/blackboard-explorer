"""Real in-memory envd registry reconciles incomplete control handshakes without tools."""

import asyncio
from copy import deepcopy
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from bbx_contracts.ctf import execution_agent_id
from bbx_envd.core import CtfCommandRegistry
from bbx_runtime.clients.blackboard import RemoteError
from bbx_runtime.ctf.execution import CtfExecutionAdapter, UnknownCommandOutcome

AGENT_ID = execution_agent_id("member-1")


class Envd:
    def __init__(self):
        # Control methods need no real marker, subprocess, command, or MCP server.
        self.registry = object.__new__(CtfCommandRegistry)
        self.registry.task_id = str(uuid4())
        self.registry.boot_id = str(uuid4())
        self.registry.unknown = False
        self.registry.registrations = {}
        self.registry.commands = {}
        self.calls = []
        self.fail_register = 0
        self.fail_after_commit = False

    async def ctf_status(self, **identity):
        return self.registry.status(identity.get("agent_id"))

    async def create_user(self, aid):
        return {"agent_id": aid}

    async def ctf_operation(self, operation, identity):
        self.calls.append((operation, identity.copy()))
        fail = operation == "register" and self.fail_register > 0
        if fail:
            self.fail_register -= 1
        if fail and not self.fail_after_commit:
            raise httpx.RemoteProtocolError("synthetic uncommitted registration")
        try:
            result = (
                self.registry.register(identity)
                if operation == "register"
                else await self.registry.stop(identity)
            )
        except ValueError as error:
            raise RemoteError(409, "synthetic registry rejection") from error
        if fail:
            raise httpx.RemoteProtocolError("synthetic lost registration response")
        return result


class Store:
    def __init__(self):
        self.member: dict[str, Any] = {
            "id": "member-1",
            "generation": 1,
            "current_turn_id": "turn-1",
            "execution": None,
        }
        self.calls = []
        self.fail_record = 0
        self.fail_after_commit = False

    async def runtime_state(self, _):
        return {"members": [deepcopy(self.member)]}

    async def runtime(self, _, operation, **body):
        self.calls.append((operation, body.copy()))
        if operation == "record_execution":
            fail = self.fail_record > 0
            if fail:
                self.fail_record -= 1
            if fail and not self.fail_after_commit:
                raise httpx.RemoteProtocolError("synthetic uncommitted binding")
            if (
                body["generation"] != self.member["generation"]
                or body["turn_id"] != self.member["current_turn_id"]
            ):
                raise RemoteError(409, "synthetic stale turn")
            old = self.member["execution"] or {}
            matching = all(old.get(key) == body[key] for key in ("boot_id", "generation"))
            if old and not matching and not old["drained"]:
                raise RemoteError(409, "synthetic previous generation not drained")
            self.member["execution"] = (
                old
                if matching
                else {
                    "boot_id": body["boot_id"],
                    "generation": body["generation"],
                    "drained": False,
                }
            )
            if fail:
                raise httpx.RemoteProtocolError("synthetic lost binding response")
            return self.member["execution"]
        assert operation == "record_execution_drained"
        old = self.member["execution"]
        proof = body["proof"]
        assert old is not None and proof["drained"] is True
        assert all(old[key] == proof[key] for key in ("boot_id", "generation"))
        self.member["execution"] = {**old, "drained": True}
        return {"drained": True}


def identity(env, generation=1, member="member-1"):
    return {
        "task_id": env.registry.task_id,
        "boot_id": env.registry.boot_id,
        "agent_id": AGENT_ID,
        "member_id": member,
        "generation": generation,
    }


def adapter(store, env):
    return CtfExecutionAdapter(cast(Any, store), cast(Any, env))


@pytest.mark.parametrize("committed", [False, True])
async def test_registration_response_loss_reconciles_actual_registry_without_commands(committed):
    store, env = Store(), Envd()
    env.fail_register = 1
    env.fail_after_commit = committed
    value = adapter(store, env)
    saved = await value.prepare(
        env.registry.task_id, store.member, {"id": "turn-1", "generation": 1}
    )
    assert saved == identity(env)
    assert len(env.calls) == (1 if committed else 2)
    assert store.member["execution"]["generation"] == 1
    assert not value.pending_registrations
    assert not env.registry.commands


@pytest.mark.parametrize("committed", [False, True])
async def test_binding_response_loss_reconciles_before_registering_another_generation(committed):
    store, env = Store(), Envd()
    store.fail_record = 1
    store.fail_after_commit = committed
    value = adapter(store, env)
    await value.prepare(env.registry.task_id, store.member, {"id": "turn-1", "generation": 1})
    assert len(store.calls) == (1 if committed else 2)
    assert len(env.calls) == 1
    assert not value.pending_registrations


async def test_uncommitted_binding_drain_recovers_real_registration_with_current_turn():
    store, env = Store(), Envd()
    store.fail_record = 2
    value = adapter(store, env)
    with pytest.raises(httpx.RemoteProtocolError):
        await value.prepare(env.registry.task_id, store.member, {"id": "turn-1", "generation": 1})
    assert store.member["execution"] is None
    assert value.pending_registrations[(env.registry.task_id, "member-1")] == identity(env)
    assert await value.drain(env.registry.task_id) is True
    assert store.member["execution"] is not None
    assert store.member["execution"]["drained"] is True
    assert env.registry.status(AGENT_ID)["drained"] is True
    binding_writes = [body for operation, body in store.calls if operation == "record_execution"]
    assert all(body["turn_id"] == "turn-1" for body in binding_writes)
    assert not value.pending_registrations
    store.member.update(generation=3, current_turn_id="turn-3")
    await value.prepare(env.registry.task_id, store.member, {"id": "turn-3", "generation": 3})
    assert env.registry.status(AGENT_ID)["generation"] == 3
    assert not env.registry.commands


async def test_lost_local_handshake_is_found_by_global_drain_after_runtime_restart():
    store, env = Store(), Envd()
    env.registry.register(identity(env))
    value = adapter(store, env)
    assert not value.pending_registrations and store.member["execution"] is None
    assert await value.drain(env.registry.task_id) is True
    assert store.member["execution"] is not None
    assert store.member["execution"]["drained"] is True
    assert [operation for operation, _ in env.calls] == ["drain"]


async def test_already_stopped_registration_binds_and_drains_without_reopening():
    store, env = Store(), Envd()
    env.registry.register(identity(env))
    await env.registry.stop(identity(env))
    proof = await adapter(store, env).stop_member(env.registry.task_id, store.member)
    assert proof["generation"] == 1 and proof["drained"] is True
    assert store.member["execution"]["drained"] is True
    assert [operation for operation, _ in env.calls] == ["drain"]


async def test_new_generation_only_registers_after_real_previous_command_drain():
    store, env = Store(), Envd()
    env.registry.register(identity(env))
    old = env.registry.registrations[AGENT_ID]

    async def held_command() -> dict[str, object]:
        await asyncio.Event().wait()
        return {}

    held = asyncio.create_task(held_command())
    old.commands["synthetic-command"] = ("digest", held)
    env.registry.commands[(AGENT_ID, "synthetic-command")] = (1, "digest", held)
    store.member.update(
        generation=3,
        current_turn_id="turn-3",
        execution={"boot_id": env.registry.boot_id, "generation": 1, "drained": False},
    )
    try:
        await adapter(store, env).prepare(
            env.registry.task_id, store.member, {"id": "turn-3", "generation": 3}
        )
        assert held.cancelled() and old.stopping
        assert [(op, value["generation"]) for op, value in env.calls] == [
            ("drain", 1),
            ("register", 3),
        ]
        assert [operation for operation, _ in store.calls] == [
            "record_execution_drained",
            "record_execution",
        ]
        assert env.registry.commands[(AGENT_ID, "synthetic-command")][2] is held
    finally:
        held.cancel()
        await asyncio.gather(held, return_exceptions=True)


async def test_saved_old_drained_binding_is_repaired_to_matching_live_registration():
    store, env = Store(), Envd()
    env.registry.register(identity(env))
    store.member["execution"] = {"boot_id": env.registry.boot_id, "generation": 0, "drained": True}
    proof = await adapter(store, env).stop_member(env.registry.task_id, store.member)
    assert proof["generation"] == store.member["execution"]["generation"] == 1
    assert proof["drained"] is True


async def test_pending_registration_cannot_silently_move_to_a_different_boot():
    store, env = Store(), Envd()
    store.fail_record = 2
    value = adapter(store, env)
    with pytest.raises(httpx.RemoteProtocolError):
        await value.prepare(env.registry.task_id, store.member, {"id": "turn-1", "generation": 1})
    env.registry.boot_id = str(uuid4())
    with pytest.raises(UnknownCommandOutcome):
        await value.drain(env.registry.task_id)
    assert not any(operation == "drain" for operation, _ in env.calls)
    assert store.member["execution"] is None


@pytest.mark.parametrize("foreign", ["newer_generation", "another_member"])
async def test_drain_rejects_unknown_writer_instead_of_stopping_it(foreign):
    store, env = Store(), Envd()
    env.registry.register(
        identity(
            env,
            generation=3 if foreign == "newer_generation" else 1,
            member="member-2" if foreign == "another_member" else "member-1",
        )
    )
    with pytest.raises(UnknownCommandOutcome):
        await adapter(store, env).drain(env.registry.task_id)
    assert not env.calls and not store.calls
