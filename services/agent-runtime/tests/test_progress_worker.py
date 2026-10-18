"""Scripted native MAF calls exercise progress persistence and dispatch fencing."""

import asyncio
import json
from copy import deepcopy
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import pytest
from agent_framework import AgentSession
from bbx_contracts.models import ModelConfig, Price
from bbx_runtime.clients.blackboard import BlackboardClient
from bbx_runtime.models import close_model_client, make_client
from bbx_runtime.progress.prompt import PROGRESS_INSTRUCTIONS, default_progress_prompt
from bbx_runtime.progress.worker import ProgressWorker, _instructions
from bbx_runtime.settings import Settings
from bbx_runtime.testing.scripted_client import (
    ScriptedChatClient,
    ScriptStep,
    ScriptToolCall,
    ScriptUsage,
)


def model() -> ModelConfig:
    return ModelConfig(
        provider="deepseek",
        model="test-model",
        base_url="https://model.invalid",
        reasoning_effort="none",
        context_window=100000,
        price=Price(
            currency="CNY",
            cache_hit_per_m=Decimal("1"),
            cache_miss_per_m=Decimal("2"),
            output_per_m=Decimal("3"),
            off_peak=False,
        ),
    )


def node(identifier: str, *, parent: str | None = "goal", version: int = 2) -> dict[str, Any]:
    return {
        "id": identifier,
        "parent_id": parent,
        "kind": "goal" if parent is None else "finding",
        "title": "目标" if parent is None else "编码样例触发校验失败",
        "summary": "同一意图下两批资料的认识保留。",
        "source_refs": [
            {
                "kind": "task" if parent is None else "fact",
                "id": "task" if parent is None else "F1",
                "version": version,
            }
        ],
    }


class ProgressEndpoint:
    def __init__(self, *, kind="refresh", pages=None):
        self.task_id, self.job_id = str(uuid4()), str(uuid4())
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.receipts: dict[str, dict[str, Any]] = {}
        self.saved = None
        self.revision = 0
        self.candidate = {"overview": "", "nodes": [node("goal", parent=None, version=1)]}
        self.completed = None
        self.failed = None
        self.loss_checkpoint = False
        self.loss_complete = False
        self.loss_settle = 0
        self.refuse_reserve = False
        self.invalid_candidates = 0
        self.pages = pages or [
            {
                "events": [
                    {"type": "task.created", "version": 1, "payload": {"goal": "调查编码失败"}},
                    {
                        "type": "fact.posted",
                        "version": 2,
                        "payload": {"id": "F1", "statement": "旧编码样例触发校验失败"},
                    },
                ],
                "after_version": 0,
                "next_version": 2,
                "through_version": 2,
                "has_more": False,
            }
        ]
        self.job = {
            "task_id": self.task_id,
            "job_id": self.job_id,
            "kind": kind,
            "run_number": 1,
            "claim_token": str(uuid4()),
            "token": "restricted-progress",
            "through_version": self.pages[-1]["through_version"],
            "source_version": 0,
            "base_revision": 0,
            "model": model().model_dump(mode="json"),
            "session": None,
            "message": {"content": "目前知道什么？"} if kind == "question" else None,
            "node_id": None,
            "tree": {**deepcopy(self.candidate), "revision": 0, "source_version": 0},
            "settings": {
                "max_output_tokens": 8192,
                "enabled_tools": [
                    "get",
                    "search",
                    "read_evidence",
                    "read_progress_sources",
                    "get_progress_tree",
                    "update_progress_tree",
                ],
            },
        }

    def response(self, data, status=200):
        return httpx.Response(status, json=data)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        self.requests.append((request.method, path, body))
        tool_path = any(part in path for part in ("/sources", "/objects/", "/search", "/evidence"))
        assert request.headers["authorization"] == (
            "Bearer restricted-progress" if tool_path else "Bearer trusted-runtime"
        )
        if path.endswith("/sources"):
            after = int(request.url.params["after_version"])
            return self.response(
                next(page for page in self.pages if page["after_version"] == after)
            )
        if "/objects/" in path:
            return self.response({"id": "F1", "version": 2, "statement": "旧编码样例触发校验失败"})
        if path.endswith("/credentials"):
            return self.response(
                {"credential_source": "stored", "credentials": {"api_key": "test-only"}}
            )
        if path.endswith("/session"):
            assert str(request.url.params["job_id"]) == self.job_id
            return self.response(self.saved) if self.saved else self.response({}, 404)
        assert body["claim_token"] == self.job["claim_token"]
        assert body["run_number"] == self.job["run_number"]
        if path.endswith("/checkpoint"):
            assert body["expected_revision"] == self.revision
            self.revision += 1
            self.saved = {
                "session": deepcopy(body["session"]),
                "revision": self.revision,
                "opening_instructions": body["opening_instructions"],
                "origin": "progress",
            }
            if self.loss_checkpoint:
                self.loss_checkpoint = False
                raise httpx.ReadError("checkpoint response lost")
            return self.response(self.saved)
        if path.endswith("/reserve"):
            if self.refuse_reserve:
                return self.response({"detail": "Progress budget exhausted"}, 402)
            receipt = {
                "status": "reserved",
                "price": model().price.model_dump(mode="json"),
                "pricing": {"rates": model().price.model_dump(mode="json")},
                "reserved": "0.05",
            }
            self.receipts.setdefault(body["request_id"], receipt)
            return self.response(self.receipts[body["request_id"]])
        if path.endswith("/settle"):
            receipt = self.receipts.get(body["request_id"])
            if receipt is None:
                return self.response({"detail": "missing reservation"}, 404)
            if self.loss_settle:
                self.loss_settle -= 1
                raise httpx.ReadError("settlement unavailable")
            if body["executed"]:
                assert self.saved is not None
                durable = self.saved["session"]["state"]["bbx_progress_receipts"][
                    body["request_id"]
                ]
                assert durable["usage"] == body["usage"]
                receipt.update(status="settled", usage=body["usage"])
            else:
                receipt["status"] = "settled" if body["executed"] is False else "unknown"
            return self.response(receipt)
        if path.endswith("/candidate"):
            if self.invalid_candidates:
                self.invalid_candidates -= 1
                return self.response({"detail": "Reference is not part of this task"}, 422)
            assert body["expected_revision"] == 0
            nodes = {
                n["id"]: n for n in self.candidate["nodes"] if n["id"] not in body["removed_ids"]
            }
            nodes.update({n["id"]: n for n in body["upserts"]})
            self.candidate["nodes"] = list(nodes.values())
            if body.get("overview") is not None:
                self.candidate["overview"] = body["overview"]
            return self.response({"saved": True, "published": False, "revision": 0})
        if path.endswith("/complete"):
            assert all(r["status"] == "settled" for r in self.receipts.values())
            self.completed = body
            if self.loss_complete:
                self.loss_complete = False
                raise httpx.ReadError("complete response lost")
            return self.response({"completed": True, "revision": 1})
        if path.endswith("/fail"):
            self.failed = body
            return self.response({"status": "failed"})
        raise AssertionError(f"Unexpected endpoint: {path}")


async def process(endpoint: ProgressEndpoint, script: ScriptedChatClient, monkeypatch):
    def client_factory(_model, **kwargs):
        assert kwargs["max_retries"] == 0
        return script

    monkeypatch.setattr("bbx_runtime.progress.worker.make_client", client_factory)
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint.handle)) as http:
        service = BlackboardClient("http://test", "trusted-runtime", http)
        await ProgressWorker(Settings.model_construct(), service).process(endpoint.job)


async def test_progress_native_tool_loop_checkpoints_before_settlement(monkeypatch):
    endpoint = ProgressEndpoint(kind="question")
    script = ScriptedChatClient(
        [
            ScriptStep(
                calls=(ScriptToolCall("get", {"object_id": "F1"}),), usage=ScriptUsage(2, 3, 4)
            ),
            ScriptStep(
                calls=(
                    ScriptToolCall(
                        "update_progress_tree",
                        {
                            "upserts": [node("encoding")],
                            "removed_ids": [],
                            "overview": "编码问题有复现依据，适用边界待补。",
                        },
                    ),
                ),
                usage=ScriptUsage(5, 6, 7),
            ),
            ScriptStep(
                text="旧编码样例触发失败 [F1](fact:F1)，整理截至 v2。", usage=ScriptUsage(8, 9, 10)
            ),
        ]
    )
    await process(endpoint, script, monkeypatch)
    assert endpoint.failed is None and endpoint.completed is not None
    assert len(endpoint.receipts) == 3
    assert all(receipt["status"] == "settled" for receipt in endpoint.receipts.values())
    assert sum(r["usage"]["output_tokens"] for r in endpoint.receipts.values()) == 21
    assert {n["id"] for n in endpoint.candidate["nodes"]} == {"goal", "encoding"}
    assert all(options["max_tokens"] == 8192 for options in script.received_options)
    assert {tool.name for tool in script.received_options[0]["tools"]} == {
        "get",
        "search",
        "read_evidence",
        "read_progress_sources",
        "get_progress_tree",
        "update_progress_tree",
    }
    assert endpoint.saved is not None
    assert "调查编码失败" in endpoint.saved["opening_instructions"]
    assert "bbx_progress_active_request" not in endpoint.saved["session"]["state"]
    assert "整理截至 v2" in endpoint.completed["content"]


async def test_progress_processes_every_page_and_keeps_previous_topics(monkeypatch):
    endpoint = ProgressEndpoint(
        pages=[
            {
                "events": [
                    {
                        "type": "fact.posted",
                        "version": 2,
                        "payload": {"id": "F1", "statement": "first discovery"},
                    }
                ],
                "after_version": 0,
                "next_version": 2,
                "through_version": 4,
                "has_more": True,
            },
            {
                "events": [
                    {
                        "type": "intent.closed",
                        "version": 4,
                        "payload": {"intent_id": "I1", "result": "rejected"},
                    }
                ],
                "after_version": 2,
                "next_version": 4,
                "through_version": 4,
                "has_more": False,
            },
        ]
    )
    script = ScriptedChatClient(
        [
            ScriptStep(
                calls=(
                    ScriptToolCall(
                        "update_progress_tree",
                        {
                            "upserts": [node("encoding")],
                            "removed_ids": [],
                            "overview": "first discovery",
                        },
                    ),
                )
            ),
            ScriptStep(text="第一批候选已保存。"),
            ScriptStep(
                calls=(
                    ScriptToolCall(
                        "update_progress_tree",
                        {
                            "upserts": [node("counterexample", version=4)],
                            "removed_ids": [],
                            "overview": "保留first discovery，补充rejected边界",
                        },
                    ),
                ),
                expect_contains="first discovery",
            ),
            ScriptStep(text="两批已整体归纳。"),
        ]
    )
    await process(endpoint, script, monkeypatch)
    assert endpoint.completed is not None and endpoint.failed is None
    assert {node["id"] for node in endpoint.candidate["nodes"]} == {
        "goal",
        "encoding",
        "counterexample",
    }
    assert len(script.received_messages) == 4


async def test_checkpoint_and_completion_response_loss_do_not_repeat_model(monkeypatch):
    endpoint = ProgressEndpoint()
    endpoint.loss_checkpoint = endpoint.loss_complete = True
    script = ScriptedChatClient([ScriptStep(text="No change.", usage=ScriptUsage(1, 2, 3))])
    await process(endpoint, script, monkeypatch)
    assert endpoint.failed is None and endpoint.completed is not None
    assert len(script.received_messages) == len(endpoint.receipts) == 1
    assert sum(path.endswith("/complete") for _, path, _ in endpoint.requests) == 2
    assert any(path.endswith("/session") for _, path, _ in endpoint.requests)


async def test_transport_unknown_keeps_reservation_and_blocks_replay(monkeypatch):
    endpoint = ProgressEndpoint()

    class Interrupted(ScriptedChatClient):
        def _inner_get_response(self, **kwargs):
            self.received_messages.append(list(kwargs["messages"]))
            raise httpx.ReadTimeout("provider response unknown")

    script = Interrupted([])
    await process(endpoint, script, monkeypatch)
    assert endpoint.completed is None and endpoint.failed is not None
    assert len(endpoint.receipts) == 1
    assert next(iter(endpoint.receipts.values()))["status"] == "unknown"
    assert endpoint.failed["retryable"] is False
    endpoint.job["session"] = endpoint.saved
    await process(endpoint, script, monkeypatch)
    assert len(script.received_messages) == 1


async def test_saved_model_response_recovers_settlement_without_provider_replay(monkeypatch):
    endpoint = ProgressEndpoint()
    endpoint.loss_settle = 3
    script = ScriptedChatClient(
        [ScriptStep(text="Saved complete answer", usage=ScriptUsage(1, 2, 3))]
    )
    await process(endpoint, script, monkeypatch)
    assert endpoint.completed is None and endpoint.failed is not None
    assert len(script.received_messages) == 1
    endpoint.job["session"] = endpoint.saved
    endpoint.failed = None
    retry = ScriptedChatClient([])
    await process(endpoint, retry, monkeypatch)
    assert endpoint.completed is not None and endpoint.failed is None
    assert endpoint.completed["content"] == "Saved complete answer"
    assert retry.received_messages == []
    assert len(endpoint.receipts) == 1


async def test_budget_refusal_sends_no_provider_request(monkeypatch):
    endpoint = ProgressEndpoint()
    endpoint.refuse_reserve = True
    script = ScriptedChatClient([ScriptStep(text="Must not send")])
    await process(endpoint, script, monkeypatch)
    assert endpoint.receipts == {} and script.received_messages == []
    assert endpoint.failed is not None and endpoint.completed is None
    assert endpoint.saved is not None
    assert "bbx_progress_active_request" not in endpoint.saved["session"]["state"]


async def test_invalid_candidate_correction_is_bounded_and_calls_still_billed(monkeypatch):
    endpoint = ProgressEndpoint()
    endpoint.invalid_candidates = 3
    patch = ScriptToolCall(
        "update_progress_tree", {"upserts": [node("invalid")], "removed_ids": []}
    )
    script = ScriptedChatClient([ScriptStep(calls=(patch,), usage=ScriptUsage(1, 2, 3))] * 3)
    await process(endpoint, script, monkeypatch)
    assert endpoint.completed is None and endpoint.failed is not None
    assert len(script.received_messages) == 2
    assert len(endpoint.receipts) == 2
    assert all(receipt["status"] == "settled" for receipt in endpoint.receipts.values())


async def test_saved_completion_republishes_candidate_without_model(monkeypatch):
    endpoint = ProgressEndpoint()
    script = ScriptedChatClient(
        [
            ScriptStep(
                calls=(
                    ScriptToolCall(
                        "update_progress_tree",
                        {
                            "upserts": [node("encoding")],
                            "removed_ids": [],
                            "overview": "编码问题已归纳。",
                        },
                    ),
                )
            ),
            ScriptStep(text="Saved answer"),
        ]
    )
    await process(endpoint, script, monkeypatch)
    assert endpoint.completed is not None
    endpoint.job["session"] = endpoint.saved
    endpoint.candidate = {"overview": "", "nodes": [node("goal", parent=None, version=1)]}
    retry = ScriptedChatClient([])
    await process(endpoint, retry, monkeypatch)
    assert {n["id"] for n in endpoint.candidate["nodes"]} == {"goal", "encoding"}
    assert retry.received_messages == []


async def test_compaction_preserves_session_and_supplies_whole_candidate(monkeypatch):
    from agent_framework import Content, Message

    endpoint = ProgressEndpoint(kind="question")
    endpoint.job["model"]["context_window"] = 24000
    native = AgentSession()
    native.state["in_memory"] = {
        "messages": [
            Message(role="user", contents=[Content.from_text("old material " * 12000)]),
            Message(role="assistant", contents=[Content.from_text("旧方向已经归纳。")]),
        ]
    }
    endpoint.job["tree"]["nodes"].append(node("stable-old-topic"))
    endpoint.job["session"] = {
        "session": native.to_dict(),
        "revision": 0,
        "opening_instructions": "",
        "origin": "progress",
    }
    script = ScriptedChatClient(
        [ScriptStep(text="旧方向和新来源都保留。", expect_contains="stable-old-topic")]
    )
    await process(endpoint, script, monkeypatch)
    assert endpoint.completed is not None and endpoint.failed is None
    assert endpoint.saved is not None
    assert "old material " * 12000 in json.dumps(endpoint.saved["session"])
    assert "old material " * 12000 not in str(script.received_messages[0])


async def test_empty_bootstrap_does_not_call_model(monkeypatch):
    endpoint = ProgressEndpoint(
        pages=[
            {
                "events": [{"type": "task.created", "version": 1, "payload": {"goal": "等待资料"}}],
                "after_version": 0,
                "next_version": 1,
                "through_version": 1,
                "has_more": False,
            }
        ]
    )
    await process(endpoint, ScriptedChatClient([]), monkeypatch)
    assert endpoint.completed is not None and not endpoint.receipts


async def test_worker_bounds_parallel_jobs_without_solver_slots():
    worker = ProgressWorker(Settings.model_construct(), None)  # type: ignore[arg-type]

    class Queue:
        async def claim(self):
            return {"task_id": str(uuid4())}

    worker.control = Queue()  # type: ignore[assignment]
    ready = asyncio.Event()
    started = []

    async def task(job):
        started.append(job)
        if len(started) == 2:
            ready.set()
        await asyncio.Event().wait()

    worker.process = task
    work = asyncio.create_task(worker.run())
    try:
        await asyncio.wait_for(ready.wait(), 2)
        assert len(started) == 2
    finally:
        await worker.stop()
        await work


def test_prompt_requires_synthesis_and_evidence_boundaries():
    for fragment in (
        "禁止一条 Fact 建一个节点",
        "只总结最后一批",
        "overview",
        "significance",
        "key_steps",
        "稳定",
        "反例",
        "inconclusive",
        "Fact.proposed",
    ):
        assert fragment in default_progress_prompt() + PROGRESS_INSTRUCTIONS


def test_prompt_setting_changes_next_job_prefix_and_preserves_original_goal():
    endpoint = ProgressEndpoint()
    endpoint.job["settings"]["system_prompt"] = "old configured prompt"
    original = _instructions(endpoint.job, endpoint.pages[0])
    endpoint.job["session"] = {"opening_instructions": original}
    endpoint.job["settings"]["system_prompt"] = "next configured prompt"
    updated = _instructions(endpoint.job, {"events": []})
    assert "old configured prompt" not in updated
    assert "next configured prompt" in updated
    assert "调查编码失败" in updated
    assert PROGRESS_INSTRUCTIONS in updated


@pytest.mark.parametrize(
    "provider",
    ["deepseek", "openai_responses", "anthropic", "bedrock", "gemini", "mistral", "ollama"],
)
async def test_progress_clients_disable_sdk_retries(provider):
    config = model().model_copy(
        update={"provider": provider, "provider_options": {"region": "us-east-1"}}
    )
    client = make_client(
        config,
        credentials={"api_key": "test-only", "access_key_id": "test", "secret_access_key": "test"},
        explore_max_steps=2,
        conclude_grace_calls=0,
        max_duration_seconds=30,
        max_retries=0,
    )
    try:
        if provider in {"deepseek", "openai_responses"}:
            assert client.client.max_retries == 0  # type: ignore[attr-defined]
        elif provider == "anthropic":
            assert client.anthropic_client.max_retries == 0  # type: ignore[attr-defined]
        elif provider == "bedrock":
            assert client._bedrock_client.meta.config.retries["total_max_attempts"] == 1  # type: ignore[attr-defined]
        elif provider == "gemini":
            assert client._genai_client._api_client._http_options.retry_options.attempts == 1  # type: ignore[attr-defined]
        elif provider == "mistral":
            assert client.client.sdk_configuration.retry_config is None  # type: ignore[attr-defined]
    finally:
        await close_model_client(client)
