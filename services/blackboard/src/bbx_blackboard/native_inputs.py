"""Original Host-only initial-input claim, immutable bytes and completion proof."""

import hashlib
import hmac
from contextlib import aclosing
from uuid import UUID

from bbx_contracts.models import InitialAttachment
from bbx_contracts.runner_bootstrap import native_recovery_frozen
from bbx_contracts.runner_inputs import (
    INPUT_FILE_COUNT,
    INPUT_PROFILE,
    INPUT_TOTAL_LIMIT,
    NativeInputClaim,
    NativeInputClaimResult,
    NativeInputFile,
    NativeInputPlan,
    NativeInputReceipt,
    NativeInputScope,
    canonical_input_document,
)
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import update

from bbx_blackboard.store import schema as s

router = APIRouter(prefix="/api/tasks/{task_id}/sandbox-host/inputs", tags=["runner"])


def input_uri(tid, item):
    return f"inputs/{tid}/{item.id}/{item.filename}"


def freeze_native_inputs(tid, originals):
    files = []
    seen = set()
    for raw in originals:
        original = InitialAttachment.model_validate(raw)
        file = NativeInputFile.model_validate(original.model_dump(exclude={"uri"}))
        if original.uri != input_uri(tid, file) or file.id in seen:
            raise ValueError("Original initial attachment identity changed")
        seen.add(file.id)
        files.append(file.model_dump(mode="json"))
    files.sort(key=lambda item: item["id"])
    if len(files) > INPUT_FILE_COUNT or sum(file["size"] for file in files) > INPUT_TOTAL_LIMIT:
        raise ValueError("Native shared attachments exceed their total bound")
    return {
        "profile": INPUT_PROFILE,
        "files": files,
        "files_sha256": hashlib.sha256(
            canonical_input_document({"task_id": str(tid)}, files)
        ).hexdigest(),
    }


def input_manifest(record):
    return record["request"].get("shared_inputs")


def current_scope(record, manifest):
    observation = record["observation"]
    sandbox = observation.get("sandbox", {})
    host = record["sandbox_host"].get("context", {})
    scope = {
        "task_id": str(record["task_id"]),
        "operation_id": record["operation_id"],
        "resource_generation": record["data"].get("resource_generation", 1),
        "workspace_pvc_uid": observation.get("workspace_pvc", {}).get("uid"),
        "sandbox_id": sandbox.get("id"),
        "sandbox_pod_uid": sandbox.get("pod_uid"),
        "sandbox_container_id": sandbox.get("container_id"),
        "execution_context_id": host.get("execution_context_id"),
    }
    if (
        host.get("task_id") != scope["task_id"]
        or host.get("operation_id") != scope["operation_id"]
        or host.get("sandbox_id") != scope["sandbox_id"]
        or host.get("sandbox_pod_uid") != scope["sandbox_pod_uid"]
        or host.get("runner_pod_uid") != observation.get("runner", {}).get("pod_uid")
    ):
        raise HTTPException(409, "Original input Host identity changed")
    digest = hashlib.sha256(canonical_input_document(scope, manifest["files"])).hexdigest()
    try:
        return NativeInputScope.model_validate({**scope, "manifest_sha256": digest})
    except ValueError as error:
        raise HTTPException(409, "Original input workspace identity is unavailable") from error


def _retained_input_lineage(record, scope, generation):
    history = record["data"].get("recovery_history")
    if not isinstance(history, list) or len(history) != generation - 1:
        return False
    pvc = scope["workspace_pvc_uid"]
    for previous, incident in enumerate(history, start=1):
        resources = incident.get("resources", {})
        observed = incident.get("original_observation", {})
        proof = incident.get("replacement_proof", {})
        exits = incident.get("exit_proof", {})
        if (
            incident.get("operation_id") != record["operation_id"]
            or incident.get("resource_generation") != previous
            or resources.get("workspace_pvc_uid") != pvc
            or observed.get("workspace_pvc", {}).get("uid") != pvc
            or proof.get("workspace_pvc_uid") != pvc
            or proof.get("writer_released") is not True
            or not all(
                exits.get(key) is True
                for key in ("runner_terminated", "sandbox_terminated", "pvcs_retained")
            )
        ):
            return False
        if previous == 1 and (
            incident.get("execution_context_id") != scope["execution_context_id"]
            or any(
                resources.get(key) != scope[key]
                for key in ("sandbox_id", "sandbox_pod_uid", "sandbox_container_id")
            )
            or any(
                observed.get("sandbox", {}).get(field) != scope[key]
                for field, key in (
                    ("id", "sandbox_id"),
                    ("pod_uid", "sandbox_pod_uid"),
                    ("container_id", "sandbox_container_id"),
                )
            )
        ):
            return False
    return True


def require_inputs_confirmed(record):
    manifest = input_manifest(record)
    if manifest is None:
        return
    saved = record["data"].get("inputs_receipt") or {}
    scope = saved.get("scope", {})
    try:
        original = NativeInputScope.model_validate(scope)
    except ValueError as error:
        raise HTTPException(409, "Original shared attachments are not confirmed") from error
    if (
        saved.get("result") != "materialized"
        or original.resource_generation != 1
        or original.manifest_sha256
        != hashlib.sha256(canonical_input_document(scope, manifest["files"])).hexdigest()
        or saved.get("files_sha256") != manifest["files_sha256"]
        or scope.get("task_id") != str(record["task_id"])
        or scope.get("operation_id") != record["operation_id"]
        or not scope.get("workspace_pvc_uid")
        or scope["workspace_pvc_uid"] != record["observation"].get("workspace_pvc", {}).get("uid")
    ):
        raise HTTPException(409, "Original shared attachments are not confirmed")
    generation = record["data"].get("resource_generation", 1)
    if generation == 1:
        exact = scope == current_scope(record, manifest).model_dump(mode="json")
    else:
        exact = _retained_input_lineage(record, scope, generation)
    if not exact:
        raise HTTPException(409, "Original shared attachment receipt identity changed")


class NativeInputs:
    def __init__(self, bootstrap):
        self.bootstrap, self.repo = bootstrap, bootstrap.repo

    async def _original(self, conn, tid, key):
        await self.repo.lock(conn, tid)
        record, host = await self.bootstrap.gateway.sandbox.host(conn, tid, key)
        task = await self.bootstrap.control._task(conn, tid)
        await self.bootstrap._global_fence(
            conn,
            record["controller_generation"],
            record["controller_backend_pid"],
            record["controller_backend_start"],
        )
        if self.bootstrap._stopping(task, record) or (
            record["data"].get("recovery") or native_recovery_frozen(task)
        ):
            raise HTTPException(409, "Original input preparation is held or stopped")
        manifest = input_manifest(record)
        if manifest is not None and (
            not self.bootstrap._resources_ready(record["observation"])
            or record["data"].get("inputs_profile") != INPUT_PROFILE
            or host["context"].get("inputs_profile") != INPUT_PROFILE
        ):
            raise HTTPException(409, "Original attachment resources are unavailable")
        return record, task, manifest

    @staticmethod
    def _plan(record, manifest):
        if manifest is None:
            return NativeInputPlan(state="disabled")
        saved = record["data"].get("inputs_receipt") or {}
        if saved.get("result") == "materialized":
            require_inputs_confirmed(record)
            return NativeInputPlan(state="confirmed")
        if record["data"].get("resource_generation", 1) != 1 or record["data"].get(
            "credentials_issued"
        ):
            raise HTTPException(409, "Initial attachment import cannot reopen")
        scope = current_scope(record, manifest)
        existing = record["data"].get("inputs_claim")
        if existing is not None and existing != scope.model_dump(mode="json"):
            raise HTTPException(409, "Original attachment claim scope changed")
        return NativeInputPlan(
            state="blocked" if saved else "claimed" if existing else "pending",
            scope=scope,
            files=manifest["files"] if not saved else None,
        )

    async def plan(self, tid, key):
        async with self.repo.engine.begin() as conn:
            record, _, manifest = await self._original(conn, tid, key)
            return self._plan(record, manifest)

    async def claim(self, tid, key, body):
        async with self.repo.engine.begin() as conn:
            record, _, manifest = await self._original(conn, tid, key)
            plan = self._plan(record, manifest)
            if plan.state == "confirmed":
                if record["data"]["inputs_receipt"]["scope"] != body.scope.model_dump(mode="json"):
                    raise HTTPException(409, "Original attachment claim scope changed")
                return NativeInputClaimResult(launch_allowed=False, state="confirmed")
            if plan.scope is None or plan.scope != body.scope:
                raise HTTPException(409, "Original attachment claim scope changed")
            if plan.state != "pending":
                return NativeInputClaimResult(launch_allowed=False, state=plan.state)
            await self.bootstrap._set(
                conn,
                tid,
                data={**record["data"], "inputs_claim": body.scope.model_dump(mode="json")},
            )
            return NativeInputClaimResult(launch_allowed=True, state="claimed")

    async def content(self, tid, key, file_id, objects):
        async with self.repo.engine.begin() as conn:
            record, _, manifest = await self._original(conn, tid, key)
            plan = self._plan(record, manifest)
            if plan.state != "claimed" or plan.files is None:
                raise HTTPException(409, "Original attachment bytes are not claimed")
            file = next((item for item in plan.files if item.id == file_id), None)
            if file is None:
                raise HTTPException(404, "Original attachment is unavailable")

        async def stream():
            count, digest = 0, hashlib.sha256()
            async with aclosing(objects.stream(input_uri(tid, file))) as chunks:
                async for chunk in chunks:
                    count += len(chunk)
                    if count > file.size:
                        raise RuntimeError("Original input exceeds its frozen byte length")
                    digest.update(chunk)
                    yield chunk
            if count != file.size or not hmac.compare_digest(digest.hexdigest(), file.sha256):
                raise RuntimeError("Original input does not match its frozen bytes")

        return StreamingResponse(
            stream(),
            media_type="application/octet-stream",
            headers={"Content-Length": str(file.size), "Cache-Control": "no-store"},
        )

    async def confirm(self, tid, key, body):
        async with self.repo.engine.begin() as conn:
            record, task, manifest = await self._original(conn, tid, key)
            plan = self._plan(record, manifest)
            scope = body.scope.model_dump(mode="json")
            existing = record["data"].get("inputs_receipt")
            if (
                manifest is None
                or record["data"].get("inputs_claim") != scope
                or (plan.scope is not None and body.scope != plan.scope)
                or body.scope.resource_generation != record["data"].get("resource_generation", 1)
            ):
                raise HTTPException(409, "Original attachment completion scope changed")
            proof = {**body.model_dump(mode="json"), "files_sha256": manifest["files_sha256"]}
            if existing == proof:
                return NativeInputPlan(state=plan.state)
            if existing is not None and not (
                existing["result"] == "unconfirmed" and body.result == "materialized"
            ):
                raise HTTPException(409, "Original attachment completion already recorded")
            confirmed = body.result == "materialized"
            await self.bootstrap._set(
                conn,
                tid,
                data={**record["data"], "inputs_receipt": proof},
            )
            await conn.execute(
                update(s.tasks)
                .where(s.tasks.c.id == tid)
                .values(
                    ctf_control={
                        **task["ctf_control"],
                        "initial_attachment_state": "ready" if confirmed else "blocked",
                    }
                )
            )
            return NativeInputPlan(state="confirmed" if confirmed else "blocked")


def _service(request):
    from bbx_blackboard.sandbox_tools import _report_key

    bootstrap = request.app.state.native_runner_bootstrap
    if bootstrap is None:
        raise HTTPException(503, "Native Runner bootstrap is unavailable")
    return bootstrap.inputs, _report_key(request)


@router.get("", response_model=NativeInputPlan, response_model_exclude_none=True)
async def input_plan(request: Request, task_id: UUID):
    service, key = _service(request)
    return await service.plan(task_id, key)


@router.post("/claim", response_model=NativeInputClaimResult)
async def input_claim(request: Request, task_id: UUID):
    from bbx_blackboard.sandbox_tools import _host_body

    service, _ = _service(request)
    body, key = await _host_body(request, NativeInputClaim, 4096)
    return await service.claim(task_id, key, body)


@router.get("/{file_id}", response_class=StreamingResponse)
async def input_content(request: Request, task_id: UUID, file_id: UUID):
    service, key = _service(request)
    if request.app.state.objects is None:
        raise HTTPException(503, "Original attachment storage is unavailable")
    return await service.content(task_id, key, file_id, request.app.state.objects)


@router.post("/confirm", response_model=NativeInputPlan, response_model_exclude_none=True)
async def input_confirm(request: Request, task_id: UUID):
    from bbx_blackboard.sandbox_tools import _host_body

    service, _ = _service(request)
    body, key = await _host_body(request, NativeInputReceipt, 4096)
    return await service.confirm(task_id, key, body)
