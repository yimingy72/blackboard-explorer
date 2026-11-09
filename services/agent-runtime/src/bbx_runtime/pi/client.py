"""Pi model calls; protocol parsing, streaming and cache usage stay in pi-ai."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from bbx_runtime.pi.bridge import PiBridge
from bbx_runtime.pi.data import (
    ChatResponse,
    ChatResponseUpdate,
    Content,
    Message,
    from_pi_message,
    pi_messages,
)
from bbx_runtime.pi.exceptions import (
    ChatClientContentFilterException,
)
from bbx_runtime.pi.stream import ResponseStream, queued_stream

if TYPE_CHECKING:
    from bbx_runtime.gateway_model import GatewayDispatch


FunctionInvocationConfiguration = dict[str, Any]


class BaseChatClient[T]:
    """A model-call boundary used by production Pi and deterministic test models."""

    def __init__(self, *, function_invocation_configuration=None, **_kwargs: Any) -> None:
        self.function_invocation_configuration = function_invocation_configuration or {}
        self.bridge = PiBridge()
        self.before_send: Any = None

    def get_response(
        self,
        messages: Sequence[Message] | None = None,
        *,
        options=None,
        stream: bool = False,
        **kwargs,
    ):
        return self._inner_get_response(
            messages=list(messages or []), options=dict(options or {}), stream=stream, **kwargs
        )

    def _inner_get_response(self, **kwargs) -> Any:
        raise NotImplementedError

    @staticmethod
    def _build_response_stream(source):
        return ResponseStream(source)

    async def close(self) -> None:
        await self.bridge.close()


API_BY_PROVIDER = {
    "deepseek": "openai-completions",
    "openai_chat": "openai-completions",
    "openai_compatible": "openai-completions",
    "foundry_local": "openai-completions",
    "openai_responses": "openai-responses",
    "azure_openai_chat": "openai-completions",
    "azure_openai_responses": "azure-openai-responses",
    "foundry": "openai-responses",
    "anthropic": "anthropic-messages",
    "anthropic_foundry": "anthropic-messages",
    "anthropic_vertex": "anthropic-messages",
    "anthropic_bedrock": "bedrock-converse-stream",
    "bedrock": "bedrock-converse-stream",
    "gemini": "google-generative-ai",
    "gemini_vertex": "google-vertex",
    "ollama": "openai-completions",
    "mistral": "mistral-conversations",
}

PI_PROVIDER_IDS = {
    "openai_chat": "openai",
    "openai_responses": "openai",
    "azure_openai_chat": "azure",
    "azure_openai_responses": "azure",
    "foundry": "openai",
    "anthropic_foundry": "anthropic",
    "anthropic_vertex": "anthropic",
    "anthropic_bedrock": "amazon-bedrock",
    "bedrock": "amazon-bedrock",
    "gemini": "google",
    "gemini_vertex": "google-vertex",
}


def usage_details(message: dict[str, Any], provider: str) -> dict[str, Any] | None:
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    required = ("input", "output", "cacheRead", "cacheWrite", "totalTokens")
    if any(
        type(usage.get(key)) is not int or not 0 <= usage[key] <= 1_000_000_000 for key in required
    ):
        return None
    raw_input = int(usage.get("input") or 0)
    hit = int(usage.get("cacheRead") or 0)
    write = int(usage.get("cacheWrite") or 0)
    output = int(usage.get("output") or 0)
    if not message.get("bbxUsageObserved"):
        return None
    total_input = raw_input + hit + write
    reasoning = usage.get("reasoning", 0)
    if (
        type(reasoning) is not int
        or not 0 <= reasoning <= output
        or usage["totalTokens"] != total_input + output
    ):
        return None
    separate = provider.startswith("anthropic") or provider == "bedrock"
    return {
        "input_token_count": raw_input if separate else total_input,
        "cache_read_input_token_count": hit,
        "cache_creation_input_token_count": write,
        "output_token_count": output,
        "total_token_count": total_input + output,
        "reasoning_output_token_count": reasoning,
        **(
            {"prompt_cache_hit_tokens": hit, "prompt_cache_miss_tokens": raw_input + write}
            if provider == "deepseek"
            else {}
        ),
    }


class PiChatClient(BaseChatClient):
    def __init__(
        self,
        model,
        *,
        credentials: Mapping[str, str],
        function_invocation_configuration: dict[str, Any],
        max_retries: int = 4,
        gateway: GatewayDispatch | None = None,
    ) -> None:
        super().__init__(function_invocation_configuration=function_invocation_configuration)
        if gateway is not None:
            if (
                credentials
                or model.provider != "openai_compatible"
                or model.model != gateway.ref.alias
                or model.base_url != gateway.origin + "/v1"
                or max_retries != 0
            ):
                raise ValueError("Invalid AgentGateway Pi model binding")
            self.bridge = PiBridge(use_env_proxy=False)
        self.gateway = gateway
        self.model = model
        self.credentials = dict(credentials)
        self.max_retries = max_retries
        from urllib.parse import urlsplit

        self.responses_replay_mode = model.provider_options.get("responses_replay_mode", "auto")
        if self.responses_replay_mode == "auto":
            self.responses_replay_mode = (
                "native"
                if (
                    model.provider == "azure_openai_responses"
                    or urlsplit(model.base_url).hostname == "api.openai.com"
                )
                else "tool_observations"
            )
        self.request_transport: Any = None
        self._auth_lock = asyncio.Lock()
        self._cloud_identity: Any = None
        self._cloud_url: str | None = None

    async def close(self) -> None:
        try:
            await super().close()
        finally:
            if self.model.provider == "foundry" and self._cloud_identity is not None:
                await self._cloud_identity.close()
                self._cloud_identity = None

    async def _cloud_credentials(self, model) -> dict[str, str]:
        credentials = dict(self.credentials)
        async with self._auth_lock:
            if self.model.provider == "anthropic_vertex":
                import json

                from google.auth.transport.requests import Request
                from google.oauth2.service_account import Credentials

                if self._cloud_identity is None:
                    self._cloud_identity = Credentials.from_service_account_info(
                        json.loads(credentials["service_account_json"]),
                        scopes=["https://www.googleapis.com/auth/cloud-platform"],
                    )
                if not self._cloud_identity.valid:
                    await asyncio.to_thread(self._cloud_identity.refresh, Request())
                credentials["access_token"] = self._cloud_identity.token
            elif self.model.provider == "foundry":
                from azure.ai.projects.aio import AIProjectClient
                from azure.identity.aio import ClientSecretCredential

                if self._cloud_identity is None:
                    self._cloud_identity = ClientSecretCredential(
                        tenant_id=credentials["tenant_id"],
                        client_id=credentials["client_id"],
                        client_secret=credentials["client_secret"],
                    )
                if self._cloud_url is None:
                    async with AIProjectClient(
                        endpoint=self.model.base_url, credential=self._cloud_identity
                    ) as project:
                        async with project.get_openai_client() as connection:
                            self._cloud_url = str(connection.base_url)
                model["baseUrl"] = self._cloud_url
                token = await self._cloud_identity.get_token("https://ai.azure.com/.default")
                credentials["api_key"] = token.token
        return credentials

    def model_definition(self) -> dict[str, Any]:
        from bbx_contracts.model_limits import effective_max_output_tokens

        api = API_BY_PROVIDER[self.model.provider]
        base_url = self.model.base_url
        if base_url == "provider-default":
            base_url = {
                "gemini": "https://generativelanguage.googleapis.com",
                "mistral": "https://api.mistral.ai/v1",
                "bedrock": "",
                "anthropic_bedrock": "",
                "gemini_vertex": "",
            }.get(self.model.provider, "")
        if self.model.provider == "ollama":
            base_url = base_url.rstrip("/")
            if not base_url.endswith("/v1"):
                base_url += "/v1"
        if self.model.provider == "azure_openai_chat":
            from urllib.parse import quote

            base_url = (
                base_url.rstrip("/") + "/openai/deployments/" + quote(self.model.model, safe="")
            )
        return {
            "id": self.model.model,
            "name": self.model.model,
            "api": api,
            "provider": PI_PROVIDER_IDS.get(self.model.provider, self.model.provider),
            "baseUrl": base_url,
            "input": ["text", "image"] if self.model.supports_vision is not False else ["text"],
            "reasoning": self.model.reasoning_effort not in {"none", "off", ""},
            "contextWindow": self.model.context_window or 1_000_000,
            "maxTokens": effective_max_output_tokens(self.model),
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "thinkingLevelMap": {
                level: level for level in ("minimal", "low", "medium", "high", "xhigh", "max")
            },
            **({"compat": {"supportsStore": False}} if api == "openai-completions" else {}),
        }

    async def _request(self, messages, options, queue=None) -> ChatResponse:
        from bbx_runtime.model_errors import (
            IncompleteModelStreamError,
            ModelRequestValidationError,
            ModelStreamError,
        )

        definitions = [item.definition() for item in options.get("tools", [])]
        options = dict(options)
        outgoing = list(messages)
        if self.model.provider in {"openai_responses", "azure_openai_responses"}:
            from urllib.parse import urlsplit

            from bbx_runtime.responses_history import project_tool_observations

            mode = self.responses_replay_mode
            if mode == "auto":
                mode = (
                    "native"
                    if (
                        self.model.provider == "azure_openai_responses"
                        or urlsplit(self.model.base_url).hostname == "api.openai.com"
                    )
                    else "tool_observations"
                )
            if mode == "tool_observations":
                outgoing = project_tool_observations(outgoing)
        model = self.model_definition()
        request_credentials = await self._cloud_credentials(model)
        max_tokens = options.get("max_tokens", model["maxTokens"])
        if self.gateway is not None:
            if type(max_tokens) is not int or max_tokens < 1:
                raise ValueError("AgentGateway output limit is invalid")
            max_tokens = min(max_tokens, model["maxTokens"])
        run_options: dict[str, Any] = {
            "maxTokens": max_tokens,
            "timeoutMs": min(
                120, self.function_invocation_configuration.get("max_duration_seconds", 120)
            )
            * 1000,
            "maxRetries": self.max_retries,
            "sessionId": options.get("session_id"),
            "cacheRetention": "short",
            "transport": "sse",
            "env": {}
            if self.gateway is not None
            else {
                key: value
                for key, value in os.environ.items()
                if key.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
            },
        }
        effort = options.get("reasoning_effort") or (options.get("reasoning") or {}).get("effort")
        if effort and effort not in {"none", "off"}:
            run_options["reasoning"] = effort
        if options.get("tool_choice") in ("auto", "none"):
            run_options["toolChoice"] = options["tool_choice"]
        if "parallel_tool_calls" in options and model["api"] in {
            "openai-completions",
            "openai-responses",
            "azure-openai-responses",
        }:
            run_options["samplingParams"] = {"parallel_tool_calls": options["parallel_tool_calls"]}
        native_options = self.model.provider_options
        secrets = request_credentials
        if self.model.provider in {"bedrock", "anthropic_bedrock"}:
            run_options["region"] = native_options["region"]
            run_options["env"] = {
                "AWS_REGION": native_options["region"],
                "AWS_ACCESS_KEY_ID": secrets["access_key_id"],
                "AWS_SECRET_ACCESS_KEY": secrets["secret_access_key"],
                **(
                    {"AWS_SESSION_TOKEN": secrets["session_token"]}
                    if secrets.get("session_token")
                    else {}
                ),
            }
        if self.model.provider == "gemini_vertex":
            run_options.update(
                project=native_options["project"], location=native_options["location"]
            )
        if self.model.provider == "azure_openai_responses":
            run_options["azureApiVersion"] = native_options["api_version"]
        run_options = {key: value for key, value in run_options.items() if value is not None}

        async def on_event(event):
            if queue is not None:
                content = (
                    Content.from_text(event["delta"])
                    if event["type"] == "text_delta"
                    else Content.from_text_reasoning(event["delta"])
                )
                await queue.put(ChatResponseUpdate(contents=[content]))

        attempt_id = uuid4()
        body_sha256 = None

        async def transport_callback(method, params):
            nonlocal body_sha256
            if method == "gateway_dispatch":
                if self.gateway is None or body_sha256 is not None:
                    raise ValueError("AgentGateway dispatch cannot be replayed")
                from bbx_runtime.clients.blackboard import RemoteError

                body_sha256 = params["body_sha256"]
                try:
                    return await self.gateway.claim(attempt_id, body_sha256)
                except RemoteError as error:
                    if error.status == 429 and error.code == "gateway_budget_exhausted":
                        body_sha256 = None
                    raise

            import base64

            import httpx2

            if method == "provider_ready":
                if self.before_send is not None:
                    await self.before_send()
                return None
            if method != "http" or self.request_transport is None:
                raise ValueError("No custom Pi transport is configured")
            request = httpx2.Request(
                params["method"],
                params["url"],
                headers=params["headers"],
                content=params["body"],
            )
            response = await self.request_transport.handle_async_request(request)
            try:
                data = await response.aread()
                return {
                    "status": response.status_code,
                    "headers": dict(response.headers),
                    "body": base64.b64encode(data).decode(),
                }
            finally:
                await response.aclose()

        result = None
        try:
            result = await self.bridge.call(
                "model",
                {
                    "model": model,
                    "context": {
                        "messages": pi_messages(
                            outgoing,
                            api=model["api"],
                            provider=model["provider"],
                            model=model["id"],
                        ),
                        "systemPrompt": options.get("instructions", ""),
                        "tools": definitions,
                    },
                    "options": run_options,
                    "api_key": secrets.get("api_key", "not-required"),
                    "adapter": self.model.provider,
                    "provider_options": native_options,
                    "credentials": secrets,
                    "custom_transport": self.request_transport is not None,
                    **({"gateway": self.gateway.url} if self.gateway is not None else {}),
                },
                on_event=on_event,
                callback=transport_callback,
            )
        finally:
            if self.gateway is not None and body_sha256 is not None:
                status = None
                if result is not None:
                    code = result.get("status")
                    reason = result.get("message", {}).get("stopReason")
                    if type(code) is int and (code >= 400 or reason in {"stop", "toolUse"}):
                        status = code
                try:
                    async with asyncio.timeout(3):
                        await self.gateway.result(attempt_id, body_sha256, status)
                except Exception:
                    # The durable claim can be reconciled independently; never resend.
                    pass
        assert result is not None
        from bbx_runtime.pi.observability import record_native_model

        record_native_model(result, model=self.model, messages=outgoing)
        message = result["message"]
        details = usage_details(message, self.model.provider)
        reason = message.get("stopReason")
        if reason not in {"stop", "toolUse"}:
            status = result.get("status")
            diagnostic = result.get("terminal") or {}
            if diagnostic.get("prepared") is False and status is None:
                raise ModelRequestValidationError("Pi rejected model input before dispatch")
            code = diagnostic.get("code")
            code = code if isinstance(code, str) else None
            raw_reason = str(message.get("rawStopReason") or "")
            if diagnostic.get("truncated_stream"):
                raise IncompleteModelStreamError("Pi rejected a stream without its terminal event")
            terminal = None
            if details is not None and (
                reason == "length"
                or diagnostic.get("event_type") in {"response.failed", "response.incomplete"}
            ):
                terminal = dict(details)
                terminal["input_token_count"] = (
                    details["total_token_count"] - details["output_token_count"]
                )
            if diagnostic.get("usage_details") is not None:
                terminal = diagnostic["usage_details"]
            error = ModelStreamError(
                "content_filter"
                if code in {"content_filter", "content_filter_error", "content_policy_violation"}
                or diagnostic.get("incomplete_reason") == "content_filter"
                or "content_filter" in raw_reason
                else "authentication"
                if status in {401, 403}
                or code in {"invalid_api_key", "authentication_error", "permission_denied"}
                else "invalid_request"
                if status == 400 or code in {"invalid_request", "invalid_request_error"}
                else "payment_required"
                if status == 402
                else "rate_limit"
                if status == 429 or code in {"rate_limit", "rate_limit_exceeded"}
                else "timeout"
                if status == 408
                else "conflict"
                if status == 409
                else "server_error"
                if status and status >= 500 or code == "server_error"
                else "invalid_response"
                if reason == "length" or diagnostic.get("event_type") == "response.incomplete"
                else "unknown"
                if diagnostic.get("event_type")
                else "timeout"
                if diagnostic.get("transport_timeout")
                else "connection"
                if diagnostic.get("transport_failure")
                else "unknown",
                finish_reason="length"
                if reason == "length" and model["api"] == "openai-completions"
                else None,
                provider_code=code,
                event_type=diagnostic.get("event_type"),
                incomplete_reason=diagnostic.get("incomplete_reason"),
                provider_response_id=diagnostic.get("response_id"),
                terminal_usage=terminal,
                http_status=status if status is not None and status >= 400 else None,
                request_id=result.get("request_id") or diagnostic.get("request_id"),
                retry_after=diagnostic.get("retry_after"),
            )
            if error.category == "content_filter":
                raise ChatClientContentFilterException("The model response was filtered") from error
            raise error
        response = ChatResponse(
            messages=[from_pi_message(message)],
            response_id=result.get("request_id"),
            model=self.model.model,
            usage_details=details,
            finish_reason="tool_calls" if reason == "toolUse" else "stop",
        )
        return response

    def _inner_get_response(self, *, messages, options, stream=False, **_kwargs):
        if not stream:
            return self._request(messages, options)

        async def produce(queue):
            response = await self._request(messages, options, queue)
            if response.usage_details is not None:
                await queue.put(
                    ChatResponseUpdate(contents=[Content.from_usage(response.usage_details)])
                )
            return response

        return queued_stream(produce)
