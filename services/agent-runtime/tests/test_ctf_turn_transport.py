"""Control transport retries preserve settled results and never replay unknown writes."""

from collections import Counter
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from bbx_runtime.clients.blackboard import BlackboardClient, RemoteError
from bbx_runtime.ctf.client import CtfClient, CtfControlTransportExhausted
from bbx_runtime.ctf.runner import CtfRunner

MEMBER = {"id": "member-1"}
TURN = {"id": "turn-1", "agent_id": "member-1", "generation": 7, "status": "running"}


class HttpBoard:
    def __init__(self, *, read_failures=0, finish="ok", purpose="execution"):
        self.read_failures, self.finish = read_failures, finish
        self.calls = Counter()
        self.turn = {**TURN, "purpose": purpose}
        self.unknown = {"retain": {"state": "unknown", "cost": "1.5"}}
        self.finished_payloads = []

    def request(self, request):
        import json

        operation = request.url.path.rsplit("/", 1)[-1]
        self.calls[(request.method, operation)] += 1
        if operation == "runtime-state":
            if self.read_failures:
                self.read_failures -= 1
                raise httpx.RemoteProtocolError("synthetic control read failure", request=request)
            return httpx.Response(
                200,
                json={
                    "task": {"ctf_control": {"phase": "running"}},
                    "members": [{**MEMBER, "generation": 7, "current_turn_id": TURN["id"]}],
                    "turns": [self.turn] if self.turn["status"] == "running" else [],
                },
            )
        if operation == "state":
            return httpx.Response(
                200,
                json={
                    "task": {
                        "ctf_control": {
                            "phase": "running",
                            "budget_unknown_reservations": self.unknown,
                        }
                    },
                    "turns": [self.turn],
                    "messages": [],
                },
            )
        if operation == "finish_turn":
            payload = json.loads(request.content)
            self.finished_payloads.append(payload)
            if self.finish in {
                "ok",
                "committed_loss",
                "mismatch",
                "5xx_committed",
                "missing_proof",
            }:
                self.turn = {
                    **self.turn,
                    "status": "finished",
                    "end_reason": payload["end_reason"],
                    "final_answer": payload["answer"],
                    "usage": {"cost": "0.7"},
                }
                if self.finish == "mismatch":
                    self.turn["final_answer"] = "another saved result"
            if self.finish in {"committed_loss", "unknown", "mismatch"}:
                raise httpx.RemoteProtocolError(
                    "synthetic finish response failure", request=request
                )
            if self.finish == "missing_proof":
                return httpx.Response(200, json={})
            if self.finish == "5xx_committed":
                return httpx.Response(503, json={"detail": "synthetic uncertain response"})
            return httpx.Response(200, json=self.turn)
        raise AssertionError(operation)


class Envd:
    stops = 0

    async def stop_member(self, task_id, member):
        self.stops += 1
        return {"drained": True}


@pytest.fixture
async def settlement(monkeypatch):
    async def no_wait(_):
        return None

    monkeypatch.setattr("bbx_runtime.ctf.client.asyncio.sleep", no_wait)
    clock = [0.0]
    monkeypatch.setattr("bbx_runtime.ctf.runner.monotonic", lambda: clock[0])
    opened = []

    def create(**options):
        server = HttpBoard(**options)
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(server.request),
            base_url="http://synthetic",
            trust_env=False,
        )
        opened.append(http)
        service = CtfClient(BlackboardClient("http://synthetic", "synthetic-service", http))
        envd = Envd()
        runner = CtfRunner(service, cast(Any, SimpleNamespace()), envd=envd)
        return server, runner, envd, clock

    yield create
    for http in opened:
        await http.aclose()


async def test_settlement_single_get_drop_retries_without_losing_answer_or_reason(settlement):
    server, runner, envd, _ = settlement(read_failures=1)
    assert await runner.settle("task", MEMBER, TURN, "completed", "synthetic answer")
    assert server.calls[("GET", "runtime-state")] == 2
    assert envd.stops == 1
    assert server.finished_payloads[0]["answer"] == "synthetic answer"
    assert server.finished_payloads[0]["end_reason"] == "completed"
    assert not runner.pending_settlements
    assert server.unknown == {"retain": {"state": "unknown", "cost": "1.5"}}


async def test_settlement_persistent_get_drop_is_backed_off_bounded_and_keeps_original(settlement):
    server, runner, envd, clock = settlement(read_failures=100)
    assert not await runner.settle("task", MEMBER, TURN, "step_limit", "synthetic partial")
    before = server.calls.copy()
    for _ in range(8):
        await runner.retry_settlements("task")
    assert server.calls == before
    clock[0] = runner.settlement_retries[("task", "member-1")].next_retry
    await runner.retry_settlements("task")
    clock[0] = runner.settlement_retries[("task", "member-1")].next_retry
    with pytest.raises(CtfControlTransportExhausted) as raised:
        await runner.retry_settlements("task")
    assert raised.value.task_id == "task"
    assert raised.value.member_id == MEMBER["id"]
    assert raised.value.turn_id == TURN["id"]
    assert raised.value.generation == 7
    assert raised.value.stage == "read_member"
    assert server.calls[("GET", "runtime-state")] == 9
    assert not server.finished_payloads
    assert envd.stops == 0
    assert runner.pending_settlements[("task", MEMBER["id"])][2:] == (
        "step_limit",
        "synthetic partial",
    )


@pytest.mark.parametrize("purpose", ["execution", "review"])
@pytest.mark.parametrize("finish", ["committed_loss", "5xx_committed", "missing_proof"])
async def test_committed_finish_response_loss_is_read_back_without_post_replay(
    settlement, purpose, finish
):
    server, runner, envd, _ = settlement(finish=finish, purpose=purpose)
    turn = {**TURN, "purpose": purpose}
    assert await runner.settle("task", MEMBER, turn, "completed", "synthetic complete")
    assert server.calls[("POST", "finish_turn")] == 1
    assert server.calls[("GET", "state")] == 1
    assert envd.stops == (1 if purpose == "execution" else 0)
    assert server.turn["final_answer"] == "synthetic complete"
    assert server.turn["usage"] == {"cost": "0.7"}
    assert not runner.pending_settlements
    assert server.unknown


async def test_unknown_finish_never_reposts_and_eventually_escalates_with_local_result(settlement):
    server, runner, _, clock = settlement(finish="unknown")
    assert not await runner.settle("task", MEMBER, TURN, "completed", "synthetic final")
    assert not await runner.settle("task", MEMBER, TURN, "failed", "replacement must not override")
    for attempt in range(2):
        clock[0] = runner.settlement_retries[("task", MEMBER["id"])].next_retry
        if attempt == 1:
            with pytest.raises(CtfControlTransportExhausted):
                await runner.retry_settlements("task")
        else:
            await runner.retry_settlements("task")
    assert server.calls[("POST", "finish_turn")] == 1
    assert server.calls[("GET", "state")] == 3
    assert runner.pending_settlements[("task", MEMBER["id"])][2:] == (
        "completed",
        "synthetic final",
    )
    assert server.turn["status"] == "running"
    assert server.unknown


async def test_finished_readback_mismatch_is_definite_failure_and_retains_pending(settlement):
    server, runner, _, _ = settlement(finish="mismatch")
    with pytest.raises(RuntimeError, match="does not match"):
        await runner.settle("task", MEMBER, TURN, "completed", "original synthetic final")
    assert runner.pending_settlements[("task", MEMBER["id"])][3] == "original synthetic final"
    assert server.calls[("POST", "finish_turn")] == 1


async def test_definite_get_409_is_not_retried_or_classified_as_transport(monkeypatch):
    reads = 0

    async def read(*args, **kwargs):
        nonlocal reads
        reads += 1
        raise RemoteError(409, "synthetic conflict")

    service = CtfClient(cast(Any, SimpleNamespace(_json=read, _task=lambda _: "/api/tasks/task")))
    with pytest.raises(RemoteError):
        await service.runtime_state("task")
    assert reads == 1


async def test_review_claim_response_loss_reclaims_same_owned_turn_with_fenced_token(monkeypatch):
    calls = []
    review = {**TURN, "purpose": "review", "runtime_instance": "same-runtime"}

    async def request(method, path, **body):
        calls.append(method)
        if method == "POST":
            if calls.count("POST") == 1:
                raise httpx.RemoteProtocolError("synthetic claim response loss")
            return {**review, "token": "synthetic-fenced-token"}
        return {
            "task": {"status": "finished"},
            "turns": [review],
            "members": [{**MEMBER, "generation": 7, "current_turn_id": TURN["id"]}],
        }

    service = CtfClient(
        cast(Any, SimpleNamespace(_json=request, _task=lambda _: "/api/tasks/task"))
    )
    claimed = await service.runtime(
        "task", "claim_review_turn", agent_id=MEMBER["id"], runtime_instance="same-runtime"
    )
    assert claimed == {**review, "token": "synthetic-fenced-token"}
    assert calls == ["POST", "GET", "POST"]


async def test_real_native_model_completion_does_not_replay_after_settlement_read_drop(monkeypatch):
    from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep, ScriptToolCall
    from test_ctf_provider_boundary import DispatchService
    from test_ctf_runtime import TURN as scripted_turn

    clock = [0.0]
    monkeypatch.setattr("bbx_runtime.ctf.runner.monotonic", lambda: clock[0])

    class Service(DispatchService):
        lost_read = False

        def __init__(self):
            super().__init__()
            self.profile.options.context_threshold = 100000

        async def runtime_state(self, _):
            if self.saved and self.billed and not self.lost_read:
                self.lost_read = True
                raise httpx.RemoteProtocolError("synthetic settlement read drop")
            state: dict[str, Any] = await super().runtime_state(_)
            state["members"] = [
                {"id": "member-1", "generation": 1, "current_turn_id": scripted_turn["id"]}
            ]
            return state

        @property
        def billed(self):
            return [body for operation, body in self.calls if operation == "bill_usage"]

    class NativeEnvd:
        calls = []

        async def execute(self, task_id, member_id, command):
            self.calls.append(command)
            return "synthetic execution output"

        async def stop_member(self, task_id, member):
            return {"drained": True}

    service, envd = Service(), NativeEnvd()
    client = ScriptedChatClient(
        [
            ScriptStep(calls=(ScriptToolCall("execute_command", {"command": "synthetic"}),)),
            ScriptStep(text="synthetic completed answer"),
        ]
    )
    runner = CtfRunner(
        cast(Any, service),
        cast(Any, SimpleNamespace()),
        client_factory=lambda *_: client,
        envd=envd,
    )
    await runner.run(
        "task",
        {"id": "member-1", "display_name": "Alice", "role": "teammate"},
        {**scripted_turn, "token": "fake"},
    )
    assert len(client.received_options) == 2
    assert envd.calls == ["synthetic"]
    assert len(service.billed) == 2
    assert not service.finished
    assert runner.pending_settlements[("task", "member-1")][2:] == (
        "completed",
        "synthetic completed answer",
    )
    clock[0] = runner.settlement_retries[("task", "member-1")].next_retry
    await runner.retry_settlements("task")
    assert service.finished[0]["answer"] == "synthetic completed answer"
    assert service.finished[0]["end_reason"] == "completed"
    assert len(client.received_options) == 2 and envd.calls == ["synthetic"]


async def test_platform_observation_transport_preserves_streamed_answer_without_model_retry():
    from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep
    from test_ctf_provider_boundary import DispatchService
    from test_ctf_runtime import TURN as scripted_turn

    class Service(DispatchService):
        def __init__(self):
            super().__init__()
            self.profile.options.context_threshold = 100000

        async def runtime(self, task_id, operation, **payload):
            if operation == "record_observation" and payload["kind"] == "model_output":
                raise httpx.RemoteProtocolError("synthetic output observation response loss")
            return await super().runtime(task_id, operation, **payload)

    service = Service()
    client = ScriptedChatClient([ScriptStep(text="synthetic preserved stream answer")])
    runner = CtfRunner(
        cast(Any, service), cast(Any, SimpleNamespace()), client_factory=lambda *_: client
    )
    await runner.run(
        "task",
        {"id": "member-1", "display_name": "Alice", "role": "teammate"},
        {**scripted_turn, "token": "fake"},
    )
    assert len(client.received_options) == 1
    assert service.finished[0]["end_reason"] == "interrupted"
    assert service.finished[0]["answer"] == "synthetic preserved stream answer"
    assert service.saved is not None
    assert any(
        message["contents"][0].get("text") == "synthetic preserved stream answer"
        for message in service.saved["session"]["state"]["in_memory"]["messages"]
    )
    assert not any(
        body["kind"] == "model_error"
        for operation, body in service.calls
        if operation == "record_observation"
    )


async def test_uncertain_continuation_only_reads_after_response_loss(settlement):
    server, runner, _, clock = settlement()
    original = server.request
    notices = []

    def request(message):
        operation = message.url.path.rsplit("/", 1)[-1]
        if operation == "enqueue_continuation":
            server.calls[("POST", operation)] += 1
            notices.append(
                {"source_turn_id": TURN["id"], "notification_purpose": "context_continuation"}
            )
            raise httpx.RemoteProtocolError("synthetic continuation response loss", request=message)
        if operation == "state":
            server.calls[("GET", operation)] += 1
            return httpx.Response(
                200,
                json={
                    "task": {"ctf_control": {"phase": "running"}},
                    "turns": [server.turn],
                    "messages": notices,
                },
            )
        return original(message)

    runner.service.client._http._transport = httpx.MockTransport(request)
    assert not await runner.settle("task", MEMBER, TURN, "context_limit", "synthetic partial")
    clock[0] = runner.settlement_retries[("task", MEMBER["id"])].next_retry
    await runner.retry_settlements("task")
    assert not runner.pending_settlements
    assert server.calls[("POST", "finish_turn")] == 1
    assert server.calls[("POST", "enqueue_continuation")] == 1
    assert server.calls[("GET", "state")] == 1


@pytest.mark.parametrize("cleanup_type", ["transport", "runtime"])
async def test_client_cleanup_failure_keeps_completed_native_answer_and_settles_once(
    monkeypatch, caplog, cleanup_type
):
    from unittest.mock import AsyncMock

    import httpx2
    from bbx_runtime.testing.scripted_client import ScriptedChatClient, ScriptStep
    from pydantic import SecretStr
    from test_ctf_provider_boundary import DispatchService
    from test_ctf_runtime import TURN as scripted_turn

    service = DispatchService()
    service.profile.options.context_threshold = 100000
    client = ScriptedChatClient([ScriptStep(text="synthetic completed answer")])
    close_error = (
        httpx2.ReadError("private synthetic cleanup error")
        if cleanup_type == "transport"
        else RuntimeError("private synthetic cleanup error")
    )
    monkeypatch.setattr(
        "bbx_runtime.ctf.runner.resolve_model_credentials", AsyncMock(return_value={})
    )
    monkeypatch.setattr("bbx_runtime.ctf.runner.make_client", lambda *args, **kwargs: client)
    close = AsyncMock(side_effect=close_error)
    monkeypatch.setattr("bbx_runtime.ctf.runner.close_model_client", close)
    runner = CtfRunner(
        cast(Any, service),
        cast(Any, SimpleNamespace(deepseek_api_key=SecretStr("unused-scripted"))),
    )
    await runner.run(
        "task",
        {"id": "member-1", "display_name": "Alice", "role": "teammate"},
        {**scripted_turn, "token": "fake"},
    )
    assert len(client.received_options) == 1
    assert len(service.finished) == 1
    assert service.finished[0]["answer"] == "synthetic completed answer"
    assert service.finished[0]["end_reason"] == "completed"
    close.assert_awaited_once()
    assert "CTF model client cleanup failed" in caplog.text
    assert "private synthetic cleanup error" not in caplog.text


@pytest.mark.parametrize("primary_type", ["error", "cancelled"])
async def test_client_cleanup_error_does_not_mask_primary_exception_or_cancellation(
    monkeypatch, primary_type
):
    import asyncio
    from unittest.mock import AsyncMock

    from bbx_runtime.testing.scripted_client import ScriptedChatClient
    from pydantic import SecretStr
    from test_ctf_provider_boundary import DispatchService
    from test_ctf_runtime import TURN as scripted_turn

    original = (
        RuntimeError("synthetic primary failure")
        if primary_type == "error"
        else asyncio.CancelledError("synthetic original cancellation")
    )

    class FailingClient(ScriptedChatClient):
        calls = 0

        def _inner_get_response(self, **kwargs):
            self.calls += 1
            raise original

    service = DispatchService()
    service.profile.options.context_threshold = 100000
    client = FailingClient([])
    monkeypatch.setattr(
        "bbx_runtime.ctf.runner.resolve_model_credentials", AsyncMock(return_value={})
    )
    monkeypatch.setattr("bbx_runtime.ctf.runner.make_client", lambda *args, **kwargs: client)
    monkeypatch.setattr(
        "bbx_runtime.ctf.runner.close_model_client",
        AsyncMock(side_effect=RuntimeError("synthetic cleanup failure")),
    )
    runner = CtfRunner(
        cast(Any, service),
        cast(Any, SimpleNamespace(deepseek_api_key=SecretStr("unused-scripted"))),
    )
    with pytest.raises(type(original)) as raised:
        await runner.run(
            "task",
            {"id": "member-1", "display_name": "Alice", "role": "teammate"},
            {**scripted_turn, "token": "fake"},
        )
    assert raised.value is original
    assert client.calls == 1
    if primary_type == "cancelled":
        assert service.finished[0]["end_reason"] == "stopped"
    else:
        assert not service.finished


@pytest.mark.parametrize("confirmed", [False, True])
async def test_old_review_usage_requires_confirmed_original_receipt_before_clearing(confirmed):
    from unittest.mock import AsyncMock

    from bbx_runtime.ctf.middleware import flush_usage_outbox

    entry = {
        "request_id": "old-review-receipt",
        "reservation_id": "old-review:1",
        "turn_id": "old-review",
        "agent_id": "member-1",
        "generation": 2,
        "usage": {"cost": "0.3"},
    }
    service = SimpleNamespace(
        runtime=AsyncMock(return_value={"request_ids": [entry["request_id"]] if confirmed else []})
    )
    checkpoint = SimpleNamespace(
        task_id="task",
        agent_id="member-1",
        save=AsyncMock(),
        session=SimpleNamespace(state={"ctf_review_usage_outbox": [entry]}),
    )
    current = {**TURN, "purpose": "review"}
    if confirmed:
        await flush_usage_outbox(cast(Any, service), cast(Any, checkpoint), current)
        assert checkpoint.session.state["ctf_review_usage_outbox"] == []
        checkpoint.save.assert_awaited_once()
    else:
        with pytest.raises(RuntimeError, match="absent from the confirmed Session"):
            await flush_usage_outbox(cast(Any, service), cast(Any, checkpoint), current)
        assert checkpoint.session.state["ctf_review_usage_outbox"] == [entry]
        checkpoint.save.assert_not_awaited()
    service.runtime.assert_awaited_once_with("task", "reconcile_usage_outbox", agent_id="member-1")


def test_control_transport_classification_excludes_model_and_local_protocol_faults():
    import httpx2
    from bbx_runtime.clients.blackboard import is_control_transport_error

    assert is_control_transport_error(httpx.RemoteProtocolError("synthetic disconnect"))
    assert not is_control_transport_error(httpx.LocalProtocolError("synthetic invalid request"))
    assert not is_control_transport_error(httpx.UnsupportedProtocol("synthetic invalid scheme"))
    assert not is_control_transport_error(httpx2.RemoteProtocolError("synthetic model disconnect"))
    assert not is_control_transport_error(RemoteError(409, "synthetic conflict"))
    assert is_control_transport_error(RemoteError(503, "synthetic service failure"))


@pytest.mark.parametrize("error_type", [httpx.LocalProtocolError, httpx.UnsupportedProtocol])
async def test_local_protocol_fault_is_not_retried_by_control_get_or_settlement(
    error_type, monkeypatch
):
    error = error_type("synthetic invalid request")
    calls = []

    async def read(*args, **kwargs):
        calls.append(args)
        raise error

    service = CtfClient(cast(Any, SimpleNamespace(_json=read, _task=lambda _: "/api/tasks/task")))
    with pytest.raises(error_type) as raised:
        await service.runtime_state("task")
    assert raised.value is error and len(calls) == 1
    runner = CtfRunner(service, cast(Any, SimpleNamespace()), envd=Envd())
    with pytest.raises(error_type):
        await runner.settle("task", MEMBER, TURN, "completed", "synthetic final")
    assert len(calls) == 2
    assert runner.settlement_retries[("task", MEMBER["id"])].attempts == 0
    assert runner.pending_settlements[("task", MEMBER["id"])][3] == "synthetic final"


@pytest.mark.parametrize("wrong_identity", [False, True])
@pytest.mark.parametrize(
    "background",
    [
        "stopped",
        "resumed",
        "resumed_settled",
        "resumed_wrong_settled_id",
        "resumed_wrong_settled_generation",
        "unproven_active",
    ],
)
async def test_authoritative_stop_retires_only_exact_old_settlement(wrong_identity, background):

    class Service:
        stopped = False
        finishes = 0
        unknown = {"retained": {"cost": "0.3", "state": "unknown"}}

        async def runtime_state(self, task_id):
            operation_history = []
            if self.stopped and background.startswith("resumed"):
                stop = {"kind": "stop", "target_generation": 7}
                if background != "resumed":
                    stop.update(
                        settled_turn_id="another-turn"
                        if background == "resumed_wrong_settled_id"
                        else TURN["id"],
                        settled_turn_generation=6
                        if background == "resumed_wrong_settled_generation"
                        else TURN["generation"],
                    )
                if background == "resumed_settled":
                    # The previous binding drained before this newer turn registered.
                    stop["target_generation"] = 6
                operation_history = [stop, {"kind": "resume"}]
            return {
                "members": [
                    {
                        "id": MEMBER["id"],
                        "generation": 8 if self.stopped else 7,
                        "current_turn_id": None if self.stopped else TURN["id"],
                        "lifecycle": "stopped"
                        if self.stopped and background == "stopped"
                        else "active",
                        "operation_history": operation_history,
                        "pending_operation": None if self.stopped else {"kind": "stop"},
                    }
                ]
            }

        async def state(self, task_id):
            latest: dict[str, Any] = await self.runtime_state(task_id)
            latest["task"] = {
                "ctf_control": {"phase": "running", "budget_unknown_reservations": self.unknown}
            }
            latest["turns"] = [
                {
                    **TURN,
                    "agent_id": "another-member" if wrong_identity else MEMBER["id"],
                    "purpose": "execution",
                    "status": "finished",
                    "end_reason": "stopped",
                    "final_answer": "",
                },
                {
                    "id": "healthy-turn",
                    "agent_id": "healthy-peer",
                    "status": "running",
                    "generation": 4,
                },
            ]
            return latest

        async def runtime(self, task_id, operation, **payload):
            self.finishes += 1
            raise AssertionError("An authoritative stopped turn must not be finished again")

    service = Service()
    runner = CtfRunner(cast(Any, service), cast(Any, SimpleNamespace()), envd=Envd())
    assert not await runner.settle("task", MEMBER, TURN, "completed", "synthetic original partial")
    key = ("task", MEMBER["id"])
    checkpoint = (object(), MEMBER, TURN, "synthetic checkpoint remains")
    runner.pending_checkpoints[key] = checkpoint
    service.stopped = True  # The stop committed even if its coordinator ACK was lost.
    if wrong_identity or background in {
        "unproven_active",
        "resumed_wrong_settled_id",
        "resumed_wrong_settled_generation",
    }:
        with pytest.raises(RuntimeError):
            await runner.retry_settlements("task")
        assert runner.pending_settlements[key][3] == "synthetic original partial"
    else:
        await runner.retry_settlements("task")
        assert key not in runner.pending_settlements and key not in runner.settlement_retries
    assert runner.pending_checkpoints[key] is checkpoint
    assert service.finishes == 0 and service.unknown
    assert (await service.state("task"))["turns"][1]["status"] == "running"


async def test_cancelled_partial_stream_failed_checkpoint_retries_as_interrupted_only(monkeypatch):
    import asyncio

    from agent_framework import ChatResponseUpdate, Content
    from bbx_runtime.testing.scripted_client import ScriptedChatClient
    from test_ctf_provider_boundary import DispatchService
    from test_ctf_runtime import TURN as scripted_turn

    class Service(DispatchService):
        failing = False

        def __init__(self):
            super().__init__()
            self.profile.options.context_threshold = 100000

        async def runtime(self, task_id, operation, **payload):
            if operation == "checkpoint" and self.failing:
                raise httpx.RemoteProtocolError("synthetic cancellation snapshot drop")
            return await super().runtime(task_id, operation, **payload)

    service = Service()

    class PartialClient(ScriptedChatClient):
        calls = 0

        def _inner_get_response(self, **kwargs):
            self.calls += 1

            async def updates():
                yield ChatResponseUpdate(
                    role="assistant", contents=[Content.from_text("synthetic partial answer")]
                )
                service.failing = True
                raise asyncio.CancelledError("synthetic stop")

            return self._build_response_stream(updates())

    client = PartialClient([])
    runner = CtfRunner(
        cast(Any, service), cast(Any, SimpleNamespace()), client_factory=lambda *_: client
    )
    with pytest.raises(httpx.RemoteProtocolError):
        await runner.run(
            "task",
            {"id": MEMBER["id"], "display_name": "Alice", "role": "teammate"},
            {**scripted_turn, "token": "fake"},
        )
    pending = runner.pending_checkpoints[("task", MEMBER["id"])]
    assert pending[0].pending_end_reason == "interrupted"
    assert pending[3] == "synthetic partial answer"
    service.failing = False
    await runner.retry_checkpoints("task")
    assert service.finished[0]["end_reason"] == "interrupted"
    assert service.finished[0]["answer"] == "synthetic partial answer"
    assert client.calls == 1
