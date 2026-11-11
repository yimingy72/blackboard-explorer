"""Ordinary business tools over the original Blackboard transactions."""

import base64
import hashlib
import json
from codecs import getincrementaldecoder
from pathlib import PurePosixPath
from typing import Literal

from bbx_contracts.models import (
    ContractModel,
    EvidenceType,
    FactKind,
    IntentResult,
    PostIntentRequest,
    SubmitCloseRequest,
)
from fastapi import HTTPException
from pydantic import Field
from sqlalchemy import select

from bbx_blackboard.gateway_auth import validate_runtime_context
from bbx_blackboard.store import schema as s
from bbx_blackboard.store.repository import row


class ObjectArgs(ContractModel):
    object_id: str = Field(min_length=1)
    depth: int = Field(default=0, ge=0, le=1)


class SearchArgs(ContractModel):
    q: str | None = None
    k: int = Field(default=3, ge=1, le=100)
    type: Literal["fact", "intent", "all"] = "all"


class EvidenceArgs(ContractModel):
    uri: str = Field(min_length=1, max_length=2048)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=16000, ge=1, le=32000)


class ClaimArgs(ContractModel):
    intent_id: str = Field(min_length=1)


class ReleaseArgs(ClaimArgs):
    note: str = Field(min_length=1)


class RegisteredEvidence(ContractModel):
    type: EvidenceType
    path: str = Field(min_length=1, max_length=2048)
    summary: str = Field(min_length=1, max_length=4096)
    call_id: str | None = Field(default=None, min_length=1, max_length=160)


class BoardFactInput(ContractModel):
    kind: FactKind
    statement: str = Field(min_length=1)
    evidence: list[RegisteredEvidence] = Field(min_length=1, max_length=20)
    derived_from: list[str] = Field(default_factory=list)
    disputes: list[str] = Field(default_factory=list)
    resolves: str | None = None
    result: IntentResult | None = None
    satisfies: list[str] = Field(default_factory=list)


_TOOL_ARGS = {
    "get": ObjectArgs,
    "search": SearchArgs,
    "read_evidence": EvidenceArgs,
    "post_fact": BoardFactInput,
    "post_intent": PostIntentRequest,
    "claim": ClaimArgs,
    "release": ReleaseArgs,
    "submit_close": SubmitCloseRequest,
}


def schemas():
    return {name: model.model_json_schema() for name, model in _TOOL_ARGS.items()}


async def _recorded_file(application, tid, aid, context, path):
    pure = PurePosixPath(path)
    if not pure.is_absolute() or not pure.is_relative_to("/workspace") or ".." in pure.parts:
        raise HTTPException(422, "Evidence file must be in the original workspace")
    gateway = application.state.runner_gateway
    calls = s.runner_calls
    async with gateway.repo.engine.connect() as conn:
        binding = await validate_runtime_context(conn, tid, context)
        completed = (
            (
                await conn.execute(
                    select(calls.c.data)
                    .where(
                        calls.c.task_id == tid,
                        calls.c.binding_id == binding["id"],
                        calls.c.provider_key == "sandbox",
                        calls.c.state == "completed",
                        calls.c.data["tool_name"].astext == "read_file",
                    )
                    .order_by(calls.c.created_at.desc(), calls.c.id.desc())
                    .limit(64)
                )
            )
            .scalars()
            .all()
        )
    for item in completed:
        if not item.get("result_ciphertext"):
            continue
        result = json.loads(gateway.cipher.decrypt(item["result_ciphertext"].encode()))
        original = (result.get("structuredContent") or {}).get("result")
        if result.get("isError") or not isinstance(original, dict) or original.get("path") != path:
            continue
        if artifact := item.get("file_artifact"):
            if artifact.get("path") != path:
                raise HTTPException(409, "Original Sandbox file evidence path changed")
            return {key: artifact[key] for key in ("uri", "size", "sha256")}
        if original.get("truncated") is not False or original.get("offset") != 0:
            break
        raw = base64.b64decode(original["data_base64"], validate=True)
        if len(raw) != original.get("retained_bytes") or len(raw) > 65536:
            raise HTTPException(409, "Original Sandbox file receipt is invalid")
        checksum = hashlib.sha256(raw).hexdigest()
        uri = f"evidence/{tid}/{aid}/{checksum}.bin"
        await application.state.objects.put(uri, raw, content_type="application/octet-stream")
        return {"uri": uri, "size": len(raw), "sha256": checksum}
    raise HTTPException(422, "Read the complete file through the original Sandbox before citing it")


async def invoke(application, tid, principal, name, arguments):
    if (
        name not in _TOOL_ARGS
        or principal.mode != "blackboard"
        or principal.runtime_context is None
    ):
        raise HTTPException(403, "Original Blackboard tool context required")
    value = _TOOL_ARGS[name].model_validate(arguments)
    data = value.model_dump(mode="json")
    service = application.state.board_service
    context = principal.runtime_context
    aid = principal.agent_id
    kwargs = {"expected_derive_round": principal.derive_round, "runtime_context": context}
    if name == "post_fact":
        async with service.repo.engine.connect() as conn:
            await validate_runtime_context(conn, tid, context)
            task = await row(conn, s.tasks, tid)
        originals = {item["path"]: item for item in task["initial_attachments"]}
        safe = []
        for requested in data["evidence"]:
            item = originals.get(requested["path"])
            material = (
                {key: item[key] for key in ("uri", "size", "sha256")}
                if item is not None
                else await _recorded_file(application, tid, aid, context, requested["path"])
            )
            safe.append({**requested, **material})
        return await service.post_fact(tid, aid, {**data, "evidence": safe}, **kwargs)
    if name == "post_intent":
        return await service.post_intent(tid, aid, data, **kwargs)
    if name == "claim":
        return await service.claim(tid, aid, data["intent_id"], **kwargs)
    if name == "release":
        return await service.release(tid, aid, data["intent_id"], data["note"], **kwargs)
    if name == "submit_close":
        return await service.submit_close(tid, aid, data, **kwargs)
    async with service.repo.engine.connect() as conn:
        await validate_runtime_context(conn, tid, context)
    if name == "get":
        try:
            return await service.get_object(tid, data["object_id"], data["depth"])
        except KeyError as error:
            raise HTTPException(404, "Blackboard object not found") from error
    if name == "search":
        return await service.search(tid, data["q"], data["k"], data["type"])
    from bbx_blackboard.api import _key_task

    uri = data["uri"]
    if _key_task(uri) != tid:
        raise HTTPException(403, "Evidence does not belong to this Task")
    if uri.startswith("inputs/"):
        async with service.repo.engine.connect() as conn:
            task = await row(conn, s.tasks, tid)
        if not any(item["uri"] == uri for item in task["initial_attachments"]):
            raise HTTPException(404, "Original Task attachment not found")
    if not await application.state.objects.exists(uri):
        raise HTTPException(404, "Evidence not found")
    decoder = getincrementaldecoder("utf-8")(errors="replace")
    offset, limit = data["offset"], data["limit"]
    skipped, scanned = 0, 0
    content = ""
    async for chunk in application.state.objects.stream(uri):
        scanned += len(chunk)
        if scanned > 50 * 1024 * 1024:
            raise HTTPException(413, "Evidence exceeds the stored attachment limit")
        part = decoder.decode(chunk)
        start = max(0, offset - skipped)
        skipped += len(part)
        content += part[start : start + limit + 1 - len(content)]
        if len(content) > limit:
            break
    else:
        last = decoder.decode(b"", final=True)
        content += last[max(0, offset - skipped) :]
    return {
        "content": content[:limit],
        "next_offset": offset + limit if len(content) > limit else None,
    }
