"""Private Pi model listener; no provider or user-facing HTTP proxy."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, contextmanager

import uvicorn
from bbx_contracts.pi_models import PiCallPrepare, PiPlaygroundInvocation, playground_context
from bbx_contracts.runner import MAX_MODEL_BODY_BYTES, validate_runner_json_object
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from bbx_runtime.clients import BlackboardClient
from bbx_runtime.pi.bridge import PiBridge
from bbx_runtime.pi_sender import PiSender, context_sha256
from bbx_runtime.settings import NativeRunnerSettings, Settings


async def body_bytes(request: Request, maximum: int) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > maximum:
            raise HTTPException(413, "Model request exceeds its byte bound")
        body.extend(chunk)
    return bytes(body)


def json_object(raw: bytes) -> dict:
    try:
        text = raw.decode("utf-8")
        return validate_runner_json_object(text, hashlib.sha256(raw).hexdigest())
    except (ValueError, UnicodeError) as error:
        raise HTTPException(422, "Invalid model JSON") from error


class RunnerModelProxy:
    def __init__(
        self,
        settings: Settings | NativeRunnerSettings,
        service: BlackboardClient,
    ):
        if not settings.pi_runtime_sender_token:
            raise ValueError("No approved Runtime model sender is configured")
        self.settings, self.service = settings, service
        self.slots = asyncio.Semaphore(settings.runner_proxy_max_inflight)
        self.streams: set[asyncio.Task] = set()
        self.closing = False
        routes = [Route("/playground", self.playground, methods=["POST"])]
        if isinstance(settings, NativeRunnerSettings):
            routes.append(Route("/messages", self.pi_messages, methods=["POST"]))
        self.app = Starlette(routes=routes)

    @staticmethod
    def token(request: Request) -> str:
        values = request.headers.getlist("authorization")
        value = values[0] if len(values) == 1 else ""
        if not value.startswith("Bearer ") or not value[7:] or "origin" in request.headers:
            raise HTTPException(401, "Scoped model credential required")
        if request.url.query:
            raise HTTPException(422, "Model request query parameters are unsupported")
        return value

    async def playground(self, request: Request) -> JSONResponse:
        sender_token = self.settings.pi_runtime_sender_token
        if self.closing or sender_token is None:
            raise HTTPException(503, "Trusted Pi Runtime is unavailable")
        token = sender_token.get_secret_value()
        if not hmac.compare_digest(self.token(request).encode(), f"Bearer {token}".encode()):
            raise HTTPException(401, "Trusted model test authority required")
        try:
            body = PiPlaygroundInvocation.model_validate(
                json_object(await body_bytes(request, 64 * 1024))
            )
            context, options = playground_context(body.prompt)
        except (ValidationError, ValueError) as error:
            raise HTTPException(422, "Invalid scoped model test request") from error
        if self.slots.locked():
            raise HTTPException(429, "Trusted Pi Runtime is at capacity")
        handler = asyncio.current_task()
        assert handler is not None
        self.streams.add(handler)
        bridge = PiBridge(use_env_proxy=False)
        try:
            async with self.slots:
                sender = PiSender(
                    self.service, token, self.settings.pi_provider_allowed_origins, bridge
                )
                await sender.invoke(body.intent, context, options)
        except Exception:
            # The operator reads the original durable status from Blackboard.
            raise HTTPException(502, "The original model test result was not confirmed") from None
        finally:
            try:
                await bridge.close()
            finally:
                self.streams.discard(handler)
        return JSONResponse({"accepted": True}, headers={"Cache-Control": "no-store"})

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

    async def close(self) -> None:
        self.closing = True
        running = self.streams - {asyncio.current_task()}
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)


@asynccontextmanager
async def model_proxy_server(settings: Settings | NativeRunnerSettings, service: BlackboardClient):
    if settings.runner_proxy_port is None or not settings.pi_runtime_sender_token:
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
