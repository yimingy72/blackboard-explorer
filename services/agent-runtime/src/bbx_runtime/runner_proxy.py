"""Live identity and one-shot opaque HTTP transport to one native gateway."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import quote, urlsplit

import httpx
import uvicorn
from bbx_contracts.native_gateway import (
    TRUSTED_ATTRIBUTE_HEADERS,
    NativeModelDispatchGrant,
    NativeModelDispatchRequest,
    NativeTransportObservation,
    NativeTransportReceipt,
)
from bbx_contracts.pi_models import PiCallPrepare
from bbx_contracts.runner import (
    MAX_MODEL_BODY_BYTES,
    MAX_MODEL_METADATA_BYTES,
    PROTOCOL,
    ModelAdmissionReceipt,
    ModelAttemptRequest,
    ModelOutcomeReceipt,
    ModelOutcomeSubmission,
    RunnerFence,
    runner_json_text,
    validate_runner_json_object,
)
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from bbx_runtime.clients import BlackboardClient
from bbx_runtime.pi.bridge import PiBridge
from bbx_runtime.pi_sender import PiSender, context_sha256
from bbx_runtime.settings import NativeRunnerSettings, Settings


async def body_bytes(request: Request, maximum: int) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > maximum:
            raise HTTPException(413, "Model gateway request exceeds its byte bound")
        body.extend(chunk)
    return bytes(body)


def json_object(raw: bytes) -> dict:
    try:
        text = raw.decode("utf-8")
        return validate_runner_json_object(text, hashlib.sha256(raw).hexdigest())
    except (ValueError, UnicodeError) as error:
        raise HTTPException(422, "Invalid model gateway JSON") from error


@dataclass
class Dispatch:
    request: NativeModelDispatchRequest
    grant: NativeModelDispatchGrant = field(repr=False)
    upstream: httpx.Response | None = field(default=None, repr=False)
    status: int | None = None
    phase: Literal["response_ended", "disconnected", "not_sent"] = "not_sent"
    finishing: asyncio.Task[None] | None = field(default=None, repr=False)
    released: bool = False


class ModelStreamResponse(StreamingResponse):
    def __init__(self, proxy: RunnerModelProxy, active: Dispatch):
        self.proxy, self.active = proxy, active
        assert active.upstream is not None
        headers = {
            key: value
            for key, value in active.upstream.headers.items()
            if key
            in {
                "content-type",
                "content-encoding",
                "content-length",
                "cache-control",
                "x-request-id",
            }
        }
        headers["cache-control"] = "no-store"
        super().__init__(
            proxy.stream(active), status_code=active.upstream.status_code, headers=headers
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.proxy.streams.add(task)
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                await self.proxy.finish(self.active)
            finally:
                self.proxy.streams.discard(task)


class RunnerModelProxy:
    def __init__(
        self,
        settings: Settings | NativeRunnerSettings,
        service: BlackboardClient,
        *,
        http: httpx.AsyncClient | None = None,
    ):
        self.settings, self.service = settings, service
        self.gateway = (settings.runner_gateway_url or "").rstrip("/")
        parsed = urlsplit(self.gateway)
        if self.gateway and (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("One fixed native gateway URL is required")
        if not self.gateway and not settings.pi_runtime_sender_token:
            raise ValueError("No approved Runtime model sender is configured")
        self.http = http or httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            timeout=20,
            transport=httpx.AsyncHTTPTransport(retries=0),
        )
        self.owned_http = http is None
        self.slots = asyncio.Semaphore(settings.runner_proxy_max_inflight)
        self.streams: set[asyncio.Task] = set()
        self.active: dict[tuple[str, str], Dispatch] = {}
        self.closing = False
        self.app = Starlette(
            routes=[
                Route("/messages", self.pi_messages, methods=["POST"]),
                Route("/attempts/admit", self.admit, methods=["POST"]),
                Route("/chat/completions", self.dispatch, methods=["POST"]),
                Route("/attempts/{attempt_id}/receipt", self.observation, methods=["POST"]),
            ]
        )

    @staticmethod
    def token(request: Request) -> str:
        values = request.headers.getlist("authorization")
        value = values[0] if len(values) == 1 else ""
        if not value.startswith("Bearer ") or not value[7:]:
            raise HTTPException(401, "Scoped model credential required")
        if request.url.query:
            raise HTTPException(422, "Model gateway query parameters are unsupported")
        return value

    @staticmethod
    def private(tid: str) -> str:
        return f"/tasks/{quote(tid, safe='')}/ctf/runtime/runner-gateway"

    async def board(
        self,
        path: str,
        *,
        token: str | None = None,
        raw: bytes,
        model_token: str | None = None,
        timeout: float = 20,
    ) -> dict:
        headers = {
            "Authorization": token or f"Bearer {self.service.token}",
            "Content-Type": "application/json",
        }
        if model_token:
            headers["X-BBX-Runner-Authorization"] = model_token
        response = await self.http.post(
            f"{self.service.base_url}/api{path}", headers=headers, content=raw, timeout=timeout
        )
        if not response.is_success:
            raise HTTPException(response.status_code, "Blackboard refused the model operation")
        return response.json()

    async def pi_messages(self, request: Request) -> StreamingResponse:
        """Project native SDK events onto the existing private Runtime listener."""
        sender_token = self.settings.pi_runtime_sender_token
        if self.closing or sender_token is None:
            raise HTTPException(503, "Trusted Pi Runtime is unavailable")
        token = self.token(request)
        try:
            payload = json_object(await body_bytes(request, MAX_MODEL_BODY_BYTES))
            if set(payload) != {"model", "context", "options", "bbx"}:
                raise ValueError("Unexpected Pi message fields")
            context, raw_options = payload["context"], payload["options"]
            if (
                not isinstance(context, dict)
                or set(context) - {"messages", "systemPrompt", "tools"}
                or not isinstance(context.get("messages"), list)
                or not isinstance(raw_options, dict)
            ):
                raise ValueError("Invalid original Pi context")
            maximum = raw_options.get("maxTokens")
            if maximum is not None and (type(maximum) is not int or maximum < 1):
                raise ValueError("Invalid approved output limit")
            options = {}
            if maximum is not None:
                options["maxTokens"] = maximum
            if raw_options.get("reasoning") in ("minimal", "low", "medium", "high", "xhigh", "max"):
                options["reasoning"] = raw_options["reasoning"]
            # Treat this request as a private session message, never a generic provider proxy.
            body = PiCallPrepare.model_validate(
                {
                    **payload["bbx"],
                    "context_sha256": context_sha256(context, options),
                }
            )
            if body.proof.kind != "native" or payload["model"] != body.model_id:
                raise ValueError("The frozen Runner Pi model did not match")
            proof = body.proof
            durable_scope = [
                "bbx-native-pi-v1",
                str(body.task_id),
                proof.binding.platform_session_id,
                proof.sdk_session_id,
                proof.journal.leaf_id or proof.journal.hash,
                proof.journal.seq,
                body.action,
            ]
            identity = json.dumps(durable_scope, ensure_ascii=False, separators=(",", ":")).encode()
            if hashlib.sha256(identity).hexdigest() != body.logical_call_id:
                raise ValueError("The original Runner call identity changed")
        except (TypeError, ValueError, ValidationError) as error:
            raise HTTPException(422, "Invalid scoped Pi model request") from error
        if self.slots.locked():
            raise HTTPException(429, "Trusted Pi Runtime is at capacity")

        def frame(event: dict) -> bytes:
            return (
                b"data: "
                + json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode()
                + b"\n\n"
            )

        async def stream() -> AsyncIterator[bytes]:
            handler = asyncio.current_task()
            assert handler is not None
            self.streams.add(handler)
            queue: asyncio.Queue[dict | None] = asyncio.Queue(32)
            bridge = PiBridge(use_env_proxy=False)

            async def produce():
                try:
                    sender = PiSender(
                        self.service,
                        sender_token.get_secret_value(),
                        self.settings.pi_provider_allowed_origins,
                        bridge,
                    )
                    result = await sender.invoke(
                        body,
                        context,
                        options,
                        runner_token=token,
                        wire_events=True,
                        on_event=queue.put,
                    )
                    message = result["message"]
                    if message["stopReason"] not in {"stop", "length", "toolUse"}:
                        raise ValueError("The original Pi model stream was incomplete")
                    event = {
                        "type": "done",
                        "reason": message["stopReason"],
                        "usage": message["usage"],
                        "responseId": result["accounting"].get("provider_request_id"),
                    }
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # This SDK error frame is not usage or fee evidence.
                    event = {
                        "type": "error",
                        "reason": "error",
                        "errorMessage": "The trusted model result was not confirmed",
                        "usage": {
                            "input": 0,
                            "output": 0,
                            "cacheRead": 0,
                            "cacheWrite": 0,
                            "totalTokens": 0,
                            "cost": {
                                "input": 0,
                                "output": 0,
                                "cacheRead": 0,
                                "cacheWrite": 0,
                                "total": 0,
                            },
                        },
                    }
                await queue.put(event)
                await queue.put(None)

            try:
                async with self.slots:
                    worker = asyncio.create_task(produce(), name="private-pi-model")
                    try:
                        while (event := await queue.get()) is not None:
                            yield frame(event)
                    finally:
                        if not worker.done():
                            worker.cancel()
                        await asyncio.gather(worker, return_exceptions=True)
                        await bridge.close()
            finally:
                self.streams.discard(handler)

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"}
        )

    async def admit(self, request: Request) -> JSONResponse:
        if not self.gateway:
            raise HTTPException(404, "Legacy model transport is unavailable")
        if self.closing:
            raise HTTPException(503, "Runtime model proxy is stopping")
        token = self.token(request)
        raw = await body_bytes(request, MAX_MODEL_METADATA_BYTES)
        try:
            attempt = ModelAttemptRequest.model_validate(json_object(raw))
            value = ModelAdmissionReceipt.model_validate(
                await self.board(
                    f"/tasks/{attempt.fence.task_id}/runner-model/attempts/admit",
                    token=token,
                    raw=raw,
                )
            )
            if (
                value.attempt_id != attempt.attempt_id
                or value.call_id != attempt.call_id
                or value.request_digest != hashlib.sha256(raw).hexdigest()
            ):
                raise HTTPException(502, "Model admission receipt did not match")
        except (ValidationError, ValueError) as error:
            raise HTTPException(422, "Invalid model admission") from error
        except (httpx.HTTPError, OSError) as error:
            raise HTTPException(503, "Model admission authority is unavailable") from error
        return JSONResponse(value.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    async def dispatch(self, request: Request) -> StreamingResponse:
        if not self.gateway:
            raise HTTPException(404, "Legacy model transport is unavailable")
        if self.closing:
            raise HTTPException(503, "Runtime transport is stopping")
        token = self.token(request)
        raw = await body_bytes(request, MAX_MODEL_BODY_BYTES)
        sha = hashlib.sha256(raw).hexdigest()
        try:
            fence = RunnerFence(
                task_id=request.headers["x-bbx-task-id"],
                runner_id=request.headers["x-bbx-runner-id"],
                pod_uid=request.headers["x-bbx-pod-uid"],
                epoch=int(request.headers["x-bbx-runtime-epoch"]),
            )
            call = NativeModelDispatchRequest(
                protocol=PROTOCOL,
                fence=fence,
                binding_id=request.headers["x-bbx-binding-id"],
                sdk_session_id=request.headers["x-bbx-sdk-session-id"],
                call_id=request.headers["x-bbx-call-id"],
                attempt_id=request.headers["x-bbx-attempt-id"],
                body_digest=sha,
            )
            if request.headers.get("x-bbx-body-digest") != sha:
                raise ValueError("Body digest did not match")
        except (KeyError, ValidationError, ValueError) as error:
            raise HTTPException(422, "Invalid model dispatch identity") from error
        if self.slots.locked():
            raise HTTPException(429, "Runtime model transport capacity is occupied")
        await self.slots.acquire()
        active, handed_off = None, False
        handler = asyncio.current_task()
        if handler is not None:
            self.streams.add(handler)
        try:
            grant = NativeModelDispatchGrant.model_validate(
                await self.board(
                    f"{self.private(fence.task_id)}/model/{call.attempt_id}/dispatch",
                    model_token=token,
                    raw=runner_json_text(call.model_dump(mode="json")).encode(),
                )
            )
            if not grant.send_allowed:
                raise HTTPException(409, "The attempt already crossed its dispatch boundary")
            if (
                grant.attempt_id != call.attempt_id
                or grant.body_digest != sha
                or not grant.dispatch_id
            ):
                raise HTTPException(502, "Private dispatch identity did not match")
            active = Dispatch(call, grant)
            self.active[(fence.task_id, call.attempt_id)] = active
            expected = {
                "bbx_task_id": fence.task_id,
                "bbx_binding_id": call.binding_id,
                "bbx_epoch": str(fence.epoch),
                "bbx_attempt_id": call.attempt_id,
                "bbx_call_id": call.call_id,
                "bbx_sdk_session_id": call.sdk_session_id,
            }
            attributes = grant.trusted_attributes
            if (
                grant.native_gateway_url is None
                or grant.native_gateway_url.rstrip("/") != self.gateway
                or not grant.task_gateway_key
                or not grant.accounting_source_id
                or grant.authorized_wait_until_ms is None
                or set(attributes) != set(TRUSTED_ATTRIBUTE_HEADERS)
                or not attributes.get("bbx_agent_id")
                or any(attributes.get(key) != value for key, value in expected.items())
            ):
                raise HTTPException(502, "Private native gateway grant did not match")
            headers = {
                "Authorization": f"Bearer {grant.task_gateway_key}",
                "Content-Type": "application/json",
                "Accept-Encoding": "identity",
            }
            headers.update(
                {header: attributes[name] for name, header in TRUSTED_ATTRIBUTE_HEADERS.items()}
            )
            remaining = grant.authorized_wait_until_ms / 1000 - time.time()
            if remaining <= 0:
                raise ValueError("The trusted transport deadline expired")
            outgoing = self.http.build_request(
                "POST",
                self.gateway + "/v1/chat/completions",
                content=raw,
                headers=headers,
                timeout=remaining,
            )
            active.phase = "disconnected"
            async with asyncio.timeout(remaining):
                active.upstream = await self.http.send(outgoing, stream=True)
            active.status = active.upstream.status_code
            response = ModelStreamResponse(self, active)
            handed_off = True
            return response
        except (ValidationError, ValueError, httpx.HTTPError, TimeoutError) as error:
            raise HTTPException(502, "Native model transport is unavailable") from error
        finally:
            if active is not None and not handed_off:
                await self.finish(active)
            elif active is None:
                self.slots.release()
            if handler is not None:
                self.streams.discard(handler)

    async def stream(self, active: Dispatch) -> AsyncIterator[bytes]:
        task = asyncio.current_task()
        assert task is not None
        self.streams.add(task)
        try:
            assert active.upstream is not None and active.grant.authorized_wait_until_ms is not None
            remaining = active.grant.authorized_wait_until_ms / 1000 - time.time()
            if remaining <= 0:
                raise TimeoutError("The trusted transport deadline expired")
            async with asyncio.timeout(remaining):
                async for chunk in active.upstream.aiter_raw():
                    yield chunk
            active.phase = "response_ended"
        finally:
            self.streams.discard(task)
            await self.finish(active)

    async def finish(self, active: Dispatch) -> None:
        if active.finishing is None:
            active.finishing = asyncio.create_task(
                self.finish_once(active), name="runner-model-finish"
            )
            active.finishing.add_done_callback(
                lambda task: None if task.cancelled() else task.exception()
            )
        await asyncio.shield(active.finishing)

    async def finish_once(self, active: Dispatch) -> None:
        try:
            upstream, active.upstream = active.upstream, None
            try:
                if upstream is not None:
                    await upstream.aclose()
            finally:
                if active.grant.dispatch_id:
                    body = NativeTransportObservation(
                        protocol=PROTOCOL,
                        attempt_id=active.request.attempt_id,
                        dispatch_id=active.grant.dispatch_id,
                        http_status=active.status,
                        phase=active.phase,
                    )
                    raw = runner_json_text(body.model_dump(mode="json")).encode()
                    try:
                        receipt = NativeTransportReceipt.model_validate(
                            await self.board(
                                f"{self.private(active.request.fence.task_id)}/model/{active.request.attempt_id}/transport",
                                raw=raw,
                                timeout=2,
                            )
                        )
                        if (
                            receipt.attempt_id != body.attempt_id
                            or receipt.dispatch_id != body.dispatch_id
                            or receipt.request_digest != hashlib.sha256(raw).hexdigest()
                        ):
                            raise ValueError("Transport observation receipt did not match")
                    except Exception:
                        pass  # Native logs/status remain the independent accounting authority.
        finally:
            if not active.released:
                active.released = True
                self.slots.release()
            self.active.pop((active.request.fence.task_id, active.request.attempt_id), None)

    async def observation(self, request: Request) -> JSONResponse:
        token = self.token(request)
        raw = await body_bytes(request, MAX_MODEL_METADATA_BYTES)
        try:
            body = ModelOutcomeSubmission.model_validate(json_object(raw))
            if body.attempt_id != request.path_params["attempt_id"]:
                raise ValueError("Model observation identity did not match")
            receipt = ModelOutcomeReceipt.model_validate(
                await self.board(
                    f"/tasks/{body.fence.task_id}/runner-model/attempts/{body.attempt_id}/observation",
                    token=token,
                    raw=raw,
                )
            )
            if (
                receipt.attempt_id != body.attempt_id
                or receipt.request_digest != hashlib.sha256(raw).hexdigest()
            ):
                raise HTTPException(502, "Model observation receipt did not match")
        except (ValidationError, ValueError) as error:
            raise HTTPException(422, "Invalid model observation") from error
        return JSONResponse(receipt.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    async def close(self) -> None:
        self.closing = True
        running = self.streams - {asyncio.current_task()}
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        await asyncio.gather(
            *(self.finish(active) for active in list(self.active.values())), return_exceptions=True
        )
        if self.owned_http:
            await self.http.aclose()


@asynccontextmanager
async def model_proxy_server(settings: Settings, service: BlackboardClient):
    if settings.runner_proxy_port is None:
        yield None
        return

    class RuntimeHttpServer(uvicorn.Server):
        @contextmanager
        def capture_signals(self):
            yield

    proxy = RunnerModelProxy(settings, service)
    server = RuntimeHttpServer(
        uvicorn.Config(
            proxy.app,
            host=settings.runner_proxy_host,
            port=settings.runner_proxy_port,
            access_log=False,
            lifespan="off",
            timeout_graceful_shutdown=3,
        )
    )
    running = asyncio.create_task(server.serve(), name="runner-model-proxy")
    try:
        yield running
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(running, 5)
        except TimeoutError:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        finally:
            await proxy.close()
