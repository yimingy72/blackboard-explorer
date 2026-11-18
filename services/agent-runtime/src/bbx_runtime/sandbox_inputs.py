"""Trusted preactivation attachment transfer to the original official Sandbox."""

import asyncio
import hashlib
import io
import json
from datetime import UTC, datetime, timedelta

import httpx
from bbx_contracts.runner_inputs import (
    INPUT_TOTAL_LIMIT,
    NativeInputClaimResult,
    NativeInputFile,
    NativeInputPlan,
    NativeInputReceipt,
    NativeInputScope,
    canonical_input_document,
)
from opensandbox import Sandbox
from opensandbox.models.execd import ExecutionHandlers, ExecutionInit, RunCommandOpts

CONTROL = "/opt/bbx/control"
SCRATCH = "/var/lib/bbx/control/inputs"


class TransferMismatch(ValueError):
    pass


async def root_input(sandbox: Sandbox, method, scope: NativeInputScope):
    if method not in {"prepare", "install", "status"}:
        raise ValueError("Invalid original attachment control")
    observed = None

    async def initialized(event: ExecutionInit):
        nonlocal observed
        if not event.id or observed not in {None, event.id}:
            raise ValueError("Original attachment control execution identity changed")
        observed = event.id

    deadline = 100 if method == "install" else 8
    async with asyncio.timeout(deadline + 1):
        result = await sandbox.commands.run(
            [
                "/usr/bin/env",
                "-i",
                "/usr/bin/python3",
                "-I",
                "-S",
                CONTROL + "/materialize-inputs.py",
                method,
                scope.manifest_sha256,
            ],
            opts=RunCommandOpts(working_directory=CONTROL, timeout=timedelta(seconds=deadline)),
            handlers=ExecutionHandlers(on_init=initialized),
        )
    text = "".join(item.text for item in result.logs.stdout)
    if (
        not observed
        or result.id != observed
        or result.complete is None
        or result.exit_code != 0
        or result.error is not None
        or len(text) > 4096
    ):
        raise ValueError("Original attachment control result is unconfirmed")
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("Invalid original attachment control result")
    return value


async def download(host, item: NativeInputFile):
    # The Host root filesystem is read-only. Hold only one bounded file in memory.
    result = io.BytesIO()
    digest = hashlib.sha256()
    try:
        async with host.http.stream(
            "GET",
            f"{host.config['blackboard_url'].rstrip('/')}/api/tasks/{host.config['task_id']}"
            f"/sandbox-host/inputs/{item.id}",
            headers={
                "x-bbx-sandbox-report": host.config["report_key"],
                "accept-encoding": "identity",
            },
        ) as response:
            response.raise_for_status()
            if response.headers.get("content-encoding", "identity") != "identity":
                raise TransferMismatch("Original attachment transport encoding changed")
            size = response.headers.get("content-length")
            if size is not None and size != str(item.size):
                raise TransferMismatch("Original attachment length changed")
            async for chunk in response.aiter_raw(chunk_size=64 * 1024):
                if result.tell() + len(chunk) > item.size:
                    raise TransferMismatch("Original attachment length exceeded the frozen size")
                digest.update(chunk)
                result.write(chunk)
        if result.tell() != item.size or digest.hexdigest() != item.sha256:
            raise TransferMismatch("Original attachment digest or length changed")
        result.seek(0)
        return result
    except BaseException:
        result.close()
        raise


def scope_document(host, plan):
    scope, files = plan.scope, plan.files
    if scope is None or not files or sum(item.size for item in files) > INPUT_TOTAL_LIMIT:
        raise ValueError("Original attachment manifest is absent or exceeds its bound")
    value = scope.model_dump(mode="json")
    if any(
        value[key] != host.config[key]
        for key in (
            "task_id",
            "operation_id",
            "sandbox_id",
            "sandbox_pod_uid",
            "execution_context_id",
        )
    ):
        raise ValueError("Original attachment Sandbox identity changed")
    document = canonical_input_document(value, [item.model_dump(mode="json") for item in files])
    if hashlib.sha256(document).hexdigest() != scope.manifest_sha256:
        raise ValueError("Original attachment manifest digest changed")
    return scope, files, document


async def reconcile_inputs(host, sandbox: Sandbox) -> bool:
    plan = NativeInputPlan.model_validate(await host.control("GET", "inputs"))
    if plan.state == "confirmed":
        # The retained workspace can contain user edits. Do not inspect or reapply it.
        return True
    if plan.state not in {"pending", "claimed"}:
        return False
    scope, files, document = scope_document(host, plan)
    if plan.state == "pending":
        claim = NativeInputClaimResult.model_validate(
            await host.control("POST", "inputs/claim", {"scope": scope.model_dump(mode="json")})
        )
        if claim.state != "claimed" or not claim.launch_allowed:
            return False
        if (await root_input(sandbox, "prepare", scope)).get("result") != "prepared":
            return False
        scratch = f"{SCRATCH}/{scope.manifest_sha256}"
        try:
            for item in files:
                with await download(host, item) as content:
                    await sandbox.files.write_file(
                        f"{scratch}/{item.id}.part", content, mode=600, owner="root", group="root"
                    )
            await sandbox.files.write_file(
                scratch + "/manifest.json", document, mode=600, owner="root", group="root"
            )
        except TransferMismatch:
            value = NativeInputReceipt(scope=scope, result="integrity_mismatch")
            await host.control("POST", "inputs/confirm", value.model_dump(mode="json"))
            return False
        current = NativeInputPlan.model_validate(await host.control("GET", "inputs"))
        if current.state != "claimed" or current.scope != scope:
            return False
        receipt = await root_input(sandbox, "install", scope)
    else:
        # The only recovery for an uncertain SDK upload/launch is exact local receipt readback.
        receipt = await root_input(sandbox, "status", scope)
    if receipt.get("result") == "unconfirmed" and "scope" not in receipt:
        return False
    verified = NativeInputReceipt.model_validate(receipt)
    if verified.scope != scope:
        raise ValueError("Original attachment terminal receipt changed")
    await host.control("POST", "inputs/confirm", verified.model_dump(mode="json"))
    current = NativeInputPlan.model_validate(await host.control("GET", "inputs"))
    return current.state == "confirmed"


async def initial_inputs_ready(host, sandbox: Sandbox):
    deadline = datetime.fromisoformat(host.config["hard_deadline"])
    while (remaining := (deadline - datetime.now(UTC)).total_seconds()) > 0:
        try:
            async with asyncio.timeout(min(remaining, 120)):
                if await reconcile_inputs(host, sandbox):
                    return True
        except (httpx.HTTPError, ValueError, TimeoutError, OSError):
            # An unresolved effect cannot be repeated; the next pass only reads its receipt.
            pass
        except Exception:
            # The pinned SDK uses its own exception types for transport loss.
            pass
        await asyncio.sleep(min(2, remaining))
    return False
