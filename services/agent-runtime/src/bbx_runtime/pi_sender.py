"""The existing trusted Runtime's provider sender; the Runner never sees provider credentials."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

from bbx_contracts.pi_models import (
    PiCallDispatch,
    PiCallDispatchResult,
    PiCallPrepare,
    PiCallPrepared,
    PiCallSettle,
    PiModelRef,
    PiUsage,
)

from bbx_runtime.clients.blackboard import BlackboardClient, RemoteError
from bbx_runtime.pi.bridge import PiBridge


def context_sha256(context: Any, options: Any) -> str:
    value = json.dumps(
        [context, options], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(value.encode()).hexdigest()


def endpoint_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("An approved HTTPS model endpoint is required")
    return f"https://{parsed.netloc.lower()}"


async def provider_addresses(base_url: str, origins: tuple[str, ...]) -> list[str]:
    origin = endpoint_origin(base_url)
    if origins and origin not in origins:
        raise ValueError("The provider origin is not approved for this Runtime")
    target = urlsplit(origin)
    async with asyncio.timeout(5):
        results = await asyncio.get_running_loop().getaddrinfo(
            target.hostname, target.port or 443, type=socket.SOCK_STREAM
        )
    addresses = list(dict.fromkeys(str(item[4][0]) for item in results))
    translated = (ipaddress.IPv6Network("64:ff9b::/96"), ipaddress.IPv6Network("64:ff9b:1::/48"))
    if (
        not addresses
        or len(addresses) > 32
        or any(
            not ip.is_global or ip.is_multicast or any(ip in prefix for prefix in translated)
            for ip in map(ipaddress.ip_address, addresses)
        )
    ):
        raise ValueError("The provider address is outside the approved public network")
    return addresses


class PiSender:
    """One durable permission and one provider effect for a frozen execution identity."""

    def __init__(
        self, service: BlackboardClient, token: str, origins: tuple[str, ...], bridge: PiBridge
    ) -> None:
        if not token:
            raise ValueError("The trusted Pi model sender is unavailable")
        if any(endpoint_origin(origin) != origin for origin in origins):
            raise ValueError("Only exact approved provider origins may be configured")
        self.service = service
        self._token = token
        self._origins = origins
        self.bridge = bridge

    async def invoke(
        self,
        body: PiCallPrepare,
        context: dict[str, Any],
        options: dict[str, Any],
        *,
        runner_token: str | None = None,
        wire_events: bool = False,
        on_event: Callable[[dict], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        if body.context_sha256 != context_sha256(context, options):
            raise ValueError("The original Pi model context changed")
        prefix = "/runtime/pi-model-calls"
        original = PiCallPrepared.model_validate(
            await self.service.trusted_pi_model(
                prefix + "/prepare", body.model_dump(mode="json"), self._token, runner_token
            )
        )
        frozen = original.model
        if (
            original.call_id != body.call_id
            or original.attempt_id != body.attempt_id
            or frozen.id != body.model_id
            or frozen.model_revision != body.model_revision
            or frozen.api != "openai-completions"
        ):
            raise ValueError("The prepared Pi model identity did not match")
        call_path = f"{prefix}/{body.call_id}"
        claimed = None
        refused = False
        result = None

        async def dispatch(method: str, params: dict[str, Any]) -> Any:
            nonlocal claimed, refused
            if method == "provider_ready":
                return None
            if method != "pi_dispatch" or claimed is not None:
                raise ValueError("Unexpected trusted Pi dispatch callback")
            request = PiCallDispatch(attempt_id=body.attempt_id, **params)
            # A lost ACK cannot be treated as a clean pre-dispatch failure.
            claimed = request.request_sha256
            try:
                permit = PiCallDispatchResult.model_validate(
                    await self.service.trusted_pi_model(
                        call_path + "/dispatch",
                        request.model_dump(mode="json"),
                        self._token,
                        runner_token,
                    )
                )
            except RemoteError as error:
                if error.status == 429:
                    # The ledger declined the claim; no provider POST was authorized.
                    claimed = None
                elif 400 <= error.status < 500:
                    refused = True
                raise
            if not permit.send_allowed:
                refused = True
            return permit.model_dump(mode="json")

        try:
            rates = frozen.usd_rates
            addresses = await provider_addresses(frozen.base_url, self._origins)
            tools = context.get("tools", [])
            if not isinstance(tools, list) or any(not isinstance(tool, dict) for tool in tools):
                raise ValueError("The Pi tool declarations are invalid")
            names = [tool.get("name") for tool in tools]
            if any(not isinstance(name, str) for name in names) or len(names) != len(set(names)):
                raise ValueError("The Pi tool declarations are invalid")
            if tools and not frozen.tool_call:
                raise ValueError("The approved model does not support tools")
            allowed = getattr(original, "allowed_tools", None)
            if body.proof.kind == "native" and (allowed is None or not set(names) <= set(allowed)):
                raise ValueError("The original Runner tool declaration changed")
            maximum = min(
                options.get("maxTokens") or frozen.max_output_tokens,
                frozen.max_output_tokens,
                original.max_output_tokens or frozen.max_output_tokens,
            )
            send_options = {
                **options,
                "maxTokens": maximum,
                "maxRetries": 0,
                "timeoutMs": 120_000,
                "env": {},
            }
            if not frozen.reasoning:
                send_options.pop("reasoning", None)
            model = {
                "id": frozen.upstream_model,
                "name": frozen.label,
                "api": frozen.api,
                "provider": frozen.provider,
                "baseUrl": frozen.base_url,
                "input": list(frozen.input_modalities),
                "reasoning": frozen.reasoning,
                "contextWindow": frozen.context_window,
                "maxTokens": frozen.max_output_tokens,
                "cost": {
                    "input": float(rates.input) if rates is not None else 0,
                    "output": float(rates.output) if rates is not None else 0,
                    "cacheRead": float(rates.cache_read) if rates is not None else 0,
                    "cacheWrite": float(rates.cache_write) if rates is not None else 0,
                },
                "compat": {"supportsStore": False, "supportsUsageInStreaming": True},
            }
            async with asyncio.timeout(125):
                result = await self.bridge.call(
                    "model",
                    {
                        "model": model,
                        "context": context,
                        "options": send_options,
                        "api_key": original.api_key,
                        "trusted_model": {
                            "addresses": addresses,
                            "wire_events": wire_events,
                            "price_present": rates is not None,
                            "max_output_tokens": maximum,
                            "allowed_tools": allowed,
                        },
                    },
                    callback=dispatch,
                    on_event=on_event,
                )
        finally:
            if not refused:
                accounting = result.get("accounting", {}) if isinstance(result, dict) else {}
                usage = accounting.get("usage")
                completed_message = result.get("message") if isinstance(result, dict) else None
                status = result.get("status") if isinstance(result, dict) else None
                complete = (
                    claimed is not None
                    and isinstance(completed_message, dict)
                    and type(status) is int
                    and 200 <= status < 300
                    and (completed_message.get("stopReason") in {"stop", "length", "toolUse"})
                )
                settlement = PiCallSettle(
                    attempt_id=body.attempt_id,
                    request_sha256=claimed,
                    outcome="complete"
                    if complete
                    else "not_sent"
                    if claimed is None
                    else "unknown",
                    usage=PiUsage.model_validate(usage) if complete and usage is not None else None,
                    amount_usd=accounting.get("amount_usd") if complete else None,
                    provider_request_id=accounting.get("provider_request_id"),
                )

                # Finish the original ledger call despite a disappearing browser or Runner.
                async def settle():
                    async with asyncio.timeout(8):
                        ack = await self.service.trusted_pi_model(
                            call_path + "/settle", settlement.model_dump(mode="json"), self._token
                        )
                    if not isinstance(ack, dict) or (
                        ack.get("call_id") != str(body.call_id)
                        or ack.get("attempt_id") != str(body.attempt_id)
                        or ack.get("state") != settlement.outcome
                    ):
                        raise ValueError("The original Pi settlement receipt did not match")

                receipt = asyncio.create_task(settle(), name="pi-model-settlement")
                receipt.add_done_callback(
                    lambda task: None if task.cancelled() else task.exception()
                )
                await asyncio.shield(receipt)
        if refused or not isinstance(result, dict):
            raise ValueError("The original Pi model attempt is closed")
        return result


@dataclass(frozen=True)
class OrdinaryPiModel:
    sender: PiSender
    task_id: str
    reference: PiModelRef
    purpose: str
    proof: dict[str, Any] | Callable[[], dict[str, Any]] = field(repr=False)

    async def invoke(self, context, options, *, on_event, logical_anchor):
        proof = self.proof() if callable(self.proof) else self.proof
        context_digest = context_sha256(context, options)
        # The original task/run, prompt/turn, and persisted conversation anchor identify the
        # call. A fresh lease or a changed body must never mint a second dispatch for it.
        scope = {
            key: value
            for key, value in proof.items()
            if key in {"agent_id", "job_id", "message_id", "turn_id"}
        }
        logical_call_id = context_sha256([self.task_id, self.purpose, scope], logical_anchor)
        call_id = uuid5(NAMESPACE_URL, "bbx-pi-call-v1:" + logical_call_id)
        body = PiCallPrepare.model_validate(
            {
                "task_id": self.task_id,
                "call_id": str(call_id),
                "attempt_id": str(uuid5(call_id, "provider-attempt-1")),
                "logical_call_id": logical_call_id,
                "model_id": self.reference.model_id,
                "model_revision": self.reference.model_revision,
                "action": "turn",
                "proof": {"kind": self.purpose, **proof},
                "context_sha256": context_digest,
            }
        )
        return await self.sender.invoke(body, context, options, on_event=on_event)
