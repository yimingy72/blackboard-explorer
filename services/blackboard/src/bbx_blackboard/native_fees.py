"""Native gateway Task keys, once-only sends and native USD fee evidence."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal, DecimalException, InvalidOperation
from typing import TYPE_CHECKING
from uuid import uuid4

import httpx
from bbx_contracts.native_gateway import (
    NativeBudgetPrepare,
    NativeModelDispatchRequest,
    NativeTransportObservation,
)
from bbx_contracts.runner import (
    PROTOCOL,
    EffectiveRunnerModel,
    ModelAttemptRequest,
    ModelOutcomeSubmission,
    runner_json_text,
    runner_payload_digest,
)
from fastapi import HTTPException
from sqlalchemy import Numeric, cast, exists, func, insert, or_, select, update
from sqlalchemy.exc import SQLAlchemyError

from bbx_blackboard.native_admin import NativeAdmin
from bbx_blackboard.runner_auth import RunnerIdentity
from bbx_blackboard.runner_control import fence, row
from bbx_blackboard.store import schema as s

if TYPE_CHECKING:
    from bbx_blackboard.native_pause import NativePause


def threshold_hold_id(task):
    control = task["ctf_control"]
    hold = control.get("native_budget_hold") or {}
    hold_id = hold.get("id")
    if (
        hold_id
        and hold.get("reason") == "budget_exceeded"
        and not hold.get("recovery_blocker")
        and control.get("failure_hold") == budget_failure_hold(hold_id)
    ):
        return hold_id
    return None


def budget_failure_hold(hold_id):
    return {"task_wide": True, "source": "native_gateway", "budget_hold_id": hold_id}


class NativeFees:
    def __init__(self, gateway):
        self.gateway = gateway
        self.repo, self.control, self.settings = gateway.repo, gateway.control, gateway.settings
        self.admin = NativeAdmin(self.settings)
        self.clock = lambda: datetime.now(UTC)
        self.pause_control: NativePause | None = None

    def _configured(self):
        if not all(
            (
                self.settings.native_gateway_accounting_source_id,
                self.settings.native_gateway_admin_url,
                self.settings.native_gateway_data_url,
                self.settings.native_gateway_catalog_revision,
            )
        ):
            raise HTTPException(503, "Native gateway deployment is not configured")

    async def _budget(self, conn, tid, *, current_source=True):
        found = (
            (
                await conn.execute(
                    select(s.native_task_budgets).where(s.native_task_budgets.c.task_id == tid)
                )
            )
            .mappings()
            .first()
        )
        if found is None:
            raise HTTPException(409, "Task native budget has not been prepared")
        if (
            current_source
            and found["accounting_source_id"] != self.settings.native_gateway_accounting_source_id
        ):
            raise HTTPException(409, "Native accounting source changed")
        return found

    def _public(self, budget):
        config = budget["config"]
        return {
            "task_id": str(budget["task_id"]),
            "accounting_source_id": budget["accounting_source_id"],
            "key_name": budget["key_name"],
            "budget_name": "task-usd",
            "key_fingerprint": budget["key_fingerprint"],
            "native_resource_id": budget["native_resource_id"],
            "threshold_usd": config["threshold_usd"],
            "exchange_rate": config["exchange_rate"],
            "selected_models": config["selected_models"],
            "publication_state": budget["publication_state"],
        }

    @staticmethod
    def _sdk_model(model):
        context, output = model.get("context_window"), model.get("max_tokens")
        modalities = model.get("architecture", {}).get("input_modalities")
        caps = model.get("capabilities", {})
        if isinstance(caps, list):
            caps = {item: True for item in caps}
        if (
            type(context) is not int
            or context < 1024
            or type(output) is not int
            or output < 1
            or not isinstance(modalities, list)
            or "text" not in modalities
            or not isinstance(caps, dict)
        ):
            raise HTTPException(409, "Native catalog lacks required SDK metadata")
        return EffectiveRunnerModel(
            id=model["id"],
            api="openai-completions",
            input=[item for item in ("text", "image") if item in modalities],
            reasoning=caps.get("reasoning") is True,
            thinking_level="off",
            context_window=context,
            max_output_tokens=output,
        ).model_dump(mode="json")

    async def prepare(self, tid, raw):
        from bbx_blackboard.runner_gateway import decode

        self._configured()
        body, _ = decode(NativeBudgetPrepare, raw, 65536)
        if body.exchange_rate.observed_at.tzinfo is None:
            raise HTTPException(422, "Exchange rate timestamp requires a timezone")
        if body.accounting_source_id != self.settings.native_gateway_accounting_source_id or len(
            set(body.selected_models)
        ) != len(body.selected_models):
            raise HTTPException(409, "Native source or model selection is invalid")
        frozen = body.model_dump(mode="json", exclude={"expected_epoch"})
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            task = await self.control._task(conn, tid, live=True)
            owner = await self.control._current_owner(conn, tid)
            if owner["epoch"] != body.expected_epoch:
                raise HTTPException(409, "Runner owner changed")
            original = (
                (
                    await conn.execute(
                        select(s.native_task_budgets).where(s.native_task_budgets.c.task_id == tid)
                    )
                )
                .mappings()
                .first()
            )
            if original:
                if original["config"]["preparation"] != frozen:
                    raise HTTPException(409, "Frozen Task budget cannot be replaced")
                return self._public(original)
            usd = (
                Decimal(str(task["budget"]["max_cost"])) / body.exchange_rate.rate_cny_per_usd
            ).quantize(Decimal("0.000000001"), rounding=ROUND_DOWN)
            if usd <= 0:
                raise HTTPException(422, "Converted USD threshold must be positive")
            key = "bbx-task-" + secrets.token_urlsafe(32)
            sha = hashlib.sha256(key.encode()).hexdigest()
            name = "bbx-task-" + str(tid)
            value = {
                "keyHash": "sha256:" + sha,
                "metadata": {"name": name},
                "allowedModels": body.selected_models,
                "budgets": [
                    {
                        "name": "task-usd",
                        "limit": {"unit": "USD", "amount": float(usd)},
                        "onBudgetExceeded": "Block",
                    }
                ],
            }
            budget = dict(
                task_id=tid,
                accounting_source_id=body.accounting_source_id,
                key_name=name,
                key_fingerprint=sha,
                key_ciphertext=self.gateway.cipher.encrypt(key.encode()).decode(),
                native_resource_id=None,
                publication_state="prepared",
                status_snapshot=None,
                hold_reason=None,
                created_at=self.clock(),
                config={
                    "preparation": frozen,
                    "threshold_usd": str(usd),
                    "threshold_cny": str(task["budget"]["max_cost"]),
                    "exchange_rate": body.exchange_rate.model_dump(mode="json"),
                    "selected_models": body.selected_models,
                    "catalog_revision": self.settings.native_gateway_catalog_revision,
                    "native_value": value,
                    "models": {},
                },
            )
            await conn.execute(insert(s.native_task_budgets).values(**budget))
            return self._public(budget)

    @staticmethod
    def _matches(value, expected):
        return (
            value.get("keyHash") == expected["keyHash"]
            and value.get("metadata", {}).get("name") == expected["metadata"]["name"]
            and value.get("allowedModels") == expected["allowedModels"]
            and value.get("budgets") == expected["budgets"]
        )

    @staticmethod
    def key_candidates(resources, budget):
        def name_of(item):
            metadata = item["value"].get("metadata")
            return metadata.get("name") if isinstance(metadata, dict) else None

        return [
            item
            for item in resources
            if item["id"] == budget["native_resource_id"]
            or item["value"].get("keyHash") == budget["config"]["native_value"]["keyHash"]
            or name_of(item) == budget["key_name"]
        ]

    async def verified_key(self, budget, expected, *, exact=False):
        items = self.key_candidates(await self.admin.resources(), budget)
        if (
            len(items) != 1
            or items[0]["id"] != budget["native_resource_id"]
            or expected.get("keyHash") != "sha256:" + budget["key_fingerprint"]
            or expected.get("metadata", {}).get("name") != budget["key_name"]
        ):
            raise HTTPException(503, "Original native key configuration is unconfirmed")
        value = items[0]["value"]
        compare = value
        if not exact and isinstance(value.get("metadata"), dict):
            metadata = dict(value["metadata"])
            if (
                metadata.pop("agentgateway.dev/id", budget["native_resource_id"])
                != budget["native_resource_id"]
                or type(metadata.pop("agentgateway.dev/createdAt", 0)) is not int
                or not isinstance(metadata.pop("agentgateway.dev/owner", ""), str)
            ):
                raise HTTPException(503, "Original native key metadata is unconfirmed")
            compare = {**value, "metadata": metadata}
        if compare != expected:
            raise HTTPException(503, "Original native key configuration is unconfirmed")
        return value

    async def threshold_status(self, budget):
        status = await self.admin.budget(budget["key_name"])
        try:
            used, exceeded = self._status_used(status, budget)
        except (ValueError, KeyError, TypeError, ArithmeticError) as error:
            raise HTTPException(503, "Original native budget readback is unconfirmed") from error
        return status, used, exceeded

    async def usage_floor(self, conn, tid, budget):
        prior_used = Decimal((budget["status_snapshot"] or {}).get("used_high_water_usd", "0"))
        return max(prior_used, await self._total(conn, tid))

    async def publish(self, tid):
        """A lost creation ACK is resolved by readback, never a second create."""
        self._configured()
        async with self.repo.engine.connect() as conn:
            budget = await self._budget(conn, tid)
        if budget["config"].get("pending_increase"):
            raise HTTPException(409, "Original native budget increase requires readback")
        resources = await self.admin.resources()
        expected = budget["config"]["native_value"]
        same = self.key_candidates(resources, budget)
        if same and (len(same) != 1 or not self._matches(same[0]["value"], expected)):
            await self.hold(tid, "native_key_conflict", config=budget["config"])
            raise HTTPException(409, "Native Task key publication conflicts")
        if not same:
            async with self.repo.engine.begin() as conn:
                await self.repo.lock(conn, tid)
                await self.control._task(conn, tid, live=True)
                current = await self._budget(conn, tid)
                if current["publication_state"] != "prepared":
                    raise HTTPException(409, "Native key creation is unresolved; readback required")
                await conn.execute(
                    update(s.native_task_budgets)
                    .where(s.native_task_budgets.c.task_id == tid)
                    .values(publication_state="creating")
                )
            try:
                await self.admin.create_key(expected)
                resources = await self.admin.resources()
                same = [item for item in resources if self._matches(item["value"], expected)]
                if len(same) != 1:
                    raise HTTPException(503, "Native key creation requires readback")
            except Exception:
                async with self.repo.engine.begin() as conn:
                    await self.repo.lock(conn, tid)
                    await conn.execute(
                        update(s.native_task_budgets)
                        .where(
                            s.native_task_budgets.c.task_id == tid,
                            s.native_task_budgets.c.publication_state == "creating",
                        )
                        .values(publication_state="creating_unknown")
                    )
                raise
        # A ready key can be recovered even after owner/Task stop; it grants no send.
        key = self.gateway.cipher.decrypt(budget["key_ciphertext"].encode()).decode()
        catalog = await self.admin.models(key)
        selected = {
            item["id"]: item
            for item in catalog
            if item.get("id") in budget["config"]["selected_models"]
        }
        if set(selected) != set(budget["config"]["selected_models"]):
            raise HTTPException(409, "Selected native model is not published")
        models = {alias: self._sdk_model(item) for alias, item in selected.items()}
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            current = await self._budget(conn, tid)
            config = current["config"]
            if config.get("pending_increase") or config["native_value"] != expected:
                raise HTTPException(409, "Original native key configuration changed")
            if current["publication_state"] == "retired":
                raise HTTPException(409, "Native Task key has been retired")
            if current["native_resource_id"] not in {None, same[0]["id"]}:
                raise HTTPException(409, "Native key resource identity changed")
            if config["models"] and config["models"] != models:
                raise HTTPException(409, "Frozen native SDK catalog changed")
            updated = {
                **current,
                "publication_state": "ready",
                "native_resource_id": same[0]["id"],
                "config": {**config, "models": models},
            }
            await conn.execute(
                update(s.native_task_budgets)
                .where(s.native_task_budgets.c.task_id == tid)
                .values(
                    publication_state="ready",
                    native_resource_id=same[0]["id"],
                    config=updated["config"],
                )
            )
            return self._public(updated)

    async def selection(self, tid, alias):
        async with self.repo.engine.connect() as conn:
            budget = await self._budget(conn, tid)
        model = budget["config"]["models"].get(alias)
        if budget["publication_state"] != "ready" or model is None:
            raise HTTPException(409, "Native Task model is not ready")
        return {
            "protocol": PROTOCOL,
            "accounting_source_id": budget["accounting_source_id"],
            "model_alias": alias,
            "sdk_model": model,
            "catalog_revision": runner_payload_digest(
                runner_json_text({"source": budget["config"]["catalog_revision"], "model": model})
            ),
        }

    async def admit_model(self, tid, identity, raw):
        from bbx_blackboard.runner_gateway import decode, exact_json

        self._configured()
        body, sha = decode(ModelAttemptRequest, raw)
        if identity.binding_id != body.binding.binding_id or identity.fence != body.fence:
            raise HTTPException(403, "Model capability scope changed")
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            _, owner, binding = await self.gateway._scope(
                conn,
                tid,
                identity,
                sdk=body.sdk_session_id,
                journal=body.journal.model_dump(mode="json"),
            )
            budget = await self._budget(conn, tid)
            if budget["publication_state"] != "ready" or budget["hold_reason"]:
                raise HTTPException(409, "Native Task budget is not ready")
            original = await self.gateway._call(conn, tid, "model:" + body.attempt_id)
            if original:
                if original["data"]["request_digest"] != sha:
                    raise HTTPException(409, "Model attempt bytes conflict")
                return self._admission(body, sha)
            spec = json.loads(binding["payload_json"])["spec"]
            expected = budget["config"]["models"].get(body.model_snapshot.id)
            wire = exact_json(body.body_json)
            snapshot = body.model_snapshot.model_dump(mode="json")
            if (
                body.binding.model_dump(mode="json") != binding["binding"]
                or snapshot != spec["model"]
                or expected is None
                or not set(snapshot["input"]) <= set(expected["input"])
                or snapshot["context_window"] > expected["context_window"]
                or snapshot["max_output_tokens"] > expected["max_output_tokens"]
                or (snapshot["reasoning"] and not expected["reasoning"])
                or body.profile_revision != spec["profile_revision"]
                or body.prompt_revision != binding["prompt_revision"]
                or not isinstance(wire, dict)
                or wire.get("model") != snapshot["id"]
                or wire.get("stream") is not True
            ):
                raise HTTPException(403, "Frozen native model selection changed")
            output = wire.get("max_completion_tokens", wire.get("max_tokens"))
            if (
                type(output) is not int
                or not 0 < output <= snapshot["max_output_tokens"]
                or ("max_completion_tokens" in wire and "max_tokens" in wire)
                or wire.get("n", 1) != 1
            ):
                raise HTTPException(403, "Model request exceeds the frozen role output settings")
            for tool in wire.get("tools", []):
                if (
                    not isinstance(tool, dict)
                    or tool.get("function", {}).get("name") not in spec["allowed_tools"]
                ):
                    raise HTTPException(403, "Model tool declaration exceeds the binding")
            if await self.gateway._count(
                conn,
                s.runner_calls.c.task_id == tid,
                s.runner_calls.c.binding_id == binding["id"],
                self.gateway._unresolved(),
            ):
                raise HTTPException(409, "Binding has an unresolved call")
            data = {
                "call_id": body.call_id,
                "attempt_id": body.attempt_id,
                "body_digest": body.body_digest,
                "request_digest": sha,
                "fence": fence(owner),
                "sdk_session_id": body.sdk_session_id,
                "binding": binding["binding"],
                "business": binding["business"],
                "journal": body.journal.model_dump(mode="json"),
                "purpose": body.purpose,
                "model_alias": snapshot["id"],
                "accounting_source_id": budget["accounting_source_id"],
                "native_resource_id": budget["native_resource_id"],
                "catalog_revision": budget["config"]["catalog_revision"],
                "prompt_revision": body.prompt_revision,
                "fee_state": "awaiting_log",
            }
            await conn.execute(
                insert(s.runner_calls).values(
                    task_id=tid,
                    id="model:" + body.attempt_id,
                    kind="model",
                    binding_id=binding["id"],
                    owner_epoch=owner["epoch"],
                    state="admitted",
                    concurrency_active=True,
                    provider_key="native",
                    data=data,
                    created_at=self.clock(),
                )
            )
            return self._admission(body, sha)

    @staticmethod
    def _admission(body, sha):
        return {
            "protocol": PROTOCOL,
            "confirmed": True,
            "call_id": body.call_id,
            "attempt_id": body.attempt_id,
            "request_digest": sha,
        }

    async def _call(self, conn, tid, attempt_id, identity, *, live=True):
        found = await self.gateway._call(conn, tid, "model:" + attempt_id)
        if found is None:
            raise HTTPException(404, "Original model attempt is unavailable")
        data = found["data"]
        if found["binding_id"] != identity.binding_id or data["fence"] != identity.fence.model_dump(
            mode="json"
        ):
            raise HTTPException(403, "Model attempt belongs to another capability")
        if live:
            await self.gateway._scope(
                conn, tid, identity, sdk=data["sdk_session_id"], journal=data["journal"]
            )
        else:
            await self.control._identity(
                conn, tid, RunnerIdentity(identity.fence, identity.token_version)
            )
        return found

    @staticmethod
    def _attributes(tid, call):
        data = call["data"]
        return {
            "bbx_task_id": str(tid),
            "bbx_binding_id": call["binding_id"],
            "bbx_epoch": str(call["owner_epoch"]),
            "bbx_agent_id": data["binding"]["agent_id"],
            "bbx_attempt_id": data["attempt_id"],
            "bbx_call_id": data["call_id"],
            "bbx_sdk_session_id": data["sdk_session_id"],
        }

    async def dispatch_model(self, tid, attempt_id, identity, raw):
        from bbx_blackboard.runner_gateway import decode

        body, _ = decode(NativeModelDispatchRequest, raw, 65536)
        if (
            body.attempt_id != attempt_id
            or body.fence != identity.fence
            or body.binding_id != identity.binding_id
        ):
            raise HTTPException(403, "Dispatch capability scope changed")
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            call = await self._call(conn, tid, attempt_id, identity)
            data = call["data"]
            if any(
                getattr(body, name) != data[name]
                for name in ("sdk_session_id", "call_id", "attempt_id", "body_digest")
            ):
                raise HTTPException(409, "Actual request differs from admission")
            public = {
                "protocol": PROTOCOL,
                "confirmed": True,
                "attempt_id": attempt_id,
                "body_digest": data["body_digest"],
                "state": call["state"],
            }
            if call["state"] != "admitted":
                return {**public, "send_allowed": False}
            budget = await self._budget(conn, tid)
            if budget["hold_reason"] or budget["publication_state"] != "ready":
                raise HTTPException(409, "Native Task admission is held")
            binding = await row(conn, s.runner_bindings, tid, call["binding_id"])
            if binding["prompt_revision"] != data["prompt_revision"]:
                raise HTTPException(409, "Published prompt changed")
            owner = await self.control._identity(
                conn, tid, RunnerIdentity(identity.fence, identity.token_version), live=True
            )
            dispatch_id = str(uuid4())
            deadline = min(
                owner["hard_deadline"],
                owner["expires_at"],
                binding["permit_expires_at"],
                datetime.fromtimestamp(identity.expires_at, UTC),
            )
            await conn.execute(
                update(s.runner_calls)
                .where(s.runner_calls.c.task_id == tid, s.runner_calls.c.id == call["id"])
                .values(
                    state="dispatched",
                    data={
                        **data,
                        "dispatch_id": dispatch_id,
                        "dispatched_at": self.clock().isoformat(),
                        "authorized_until": deadline.isoformat(),
                    },
                )
            )
            return {
                **public,
                "state": "dispatched",
                "send_allowed": True,
                "dispatch_id": dispatch_id,
                "native_gateway_url": self.settings.native_gateway_data_url.rstrip("/"),
                "task_gateway_key": self.gateway.cipher.decrypt(
                    budget["key_ciphertext"].encode()
                ).decode(),
                "accounting_source_id": budget["accounting_source_id"],
                "trusted_attributes": self._attributes(tid, call),
                "authorized_wait_until_ms": int(deadline.timestamp() * 1000),
            }

    async def transport(self, tid, attempt_id, raw):
        from bbx_blackboard.runner_gateway import decode

        body, sha = decode(NativeTransportObservation, raw, 65536)
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            call = await self.gateway._call(conn, tid, "model:" + attempt_id)
            if (
                call is None
                or body.attempt_id != attempt_id
                or body.dispatch_id != call["data"].get("dispatch_id")
            ):
                raise HTTPException(403, "Transport observation has no original dispatch")
            data = call["data"]
            prior = data.get("transport", {}).get("phase")
            native_evidence = (
                await conn.execute(
                    select(
                        exists().where(
                            s.native_request_costs.c.task_id == tid,
                            s.native_request_costs.c.attempt_id == attempt_id,
                        )
                    )
                )
            ).scalar_one()
            if (
                body.phase == "not_sent"
                and (
                    prior not in {None, "not_sent"}
                    or call["state"] == "completed"
                    or native_evidence
                )
            ) or (prior == "not_sent" and body.phase != "not_sent"):
                raise HTTPException(
                    409, "Transport observation conflicts with original send evidence"
                )
            not_sent = body.phase == "not_sent"
            # Transport and SDK observations cannot create native fee evidence or unlock a resend.
            await conn.execute(
                update(s.runner_calls)
                .where(s.runner_calls.c.task_id == tid, s.runner_calls.c.id == call["id"])
                .values(
                    state="not_sent" if not_sent else call["state"],
                    concurrency_active=call["concurrency_active"] and body.phase == "disconnected",
                    data={
                        **data,
                        "fee_state": "not_sent" if not_sent else data["fee_state"],
                        "transport": body.model_dump(mode="json"),
                        "transport_observed_at": data.get(
                            "transport_observed_at", self.clock().isoformat()
                        ),
                    },
                )
            )
            return {
                "protocol": PROTOCOL,
                "confirmed": True,
                "attempt_id": attempt_id,
                "dispatch_id": body.dispatch_id,
                "request_digest": sha,
            }

    async def observe_model(self, tid, attempt_id, identity, raw):
        from bbx_blackboard.runner_gateway import decode

        body, sha = decode(ModelOutcomeSubmission, raw, 131072)
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            call = await self._call(conn, tid, attempt_id, identity, live=False)
            data = call["data"]
            if (
                body.fence != identity.fence
                or body.binding_id != call["binding_id"]
                or body.sdk_session_id != data["sdk_session_id"]
                or body.call_id != data["call_id"]
                or body.attempt_id != attempt_id
            ):
                raise HTTPException(403, "SDK observation scope changed")
            receipt = {
                "protocol": PROTOCOL,
                "confirmed": True,
                "attempt_id": attempt_id,
                "request_digest": sha,
            }
            receipt = await self.control._receipt(
                conn,
                tid,
                call["owner_epoch"],
                "sdk_model_observation",
                attempt_id,
                raw.decode("utf-8"),
                sha,
                receipt,
                call["binding_id"],
            )
        if body.model_outcome == "completed":
            try:
                async with asyncio.timeout(3):
                    while True:
                        async with self.repo.engine.connect() as conn:
                            current = await self._call(conn, tid, attempt_id, identity, live=False)
                        if not current["concurrency_active"]:
                            break
                        await asyncio.sleep(0.01)
            except TimeoutError as error:
                raise HTTPException(
                    503, "Trusted model transport completion is unconfirmed"
                ) from error
        return receipt

    async def _hold(self, conn, tid, reason):
        from bbx_blackboard.runner_gateway import cancel_admitted

        task = await row(conn, s.tasks, tid)
        budget = await self._budget(conn, tid, current_source=False)
        control = task["ctf_control"]
        if existing := control.get("native_budget_hold"):
            if (
                existing.get("id")
                and reason != "budget_exceeded"
                and not existing.get("recovery_blocker")
            ):
                await conn.execute(
                    update(s.tasks)
                    .where(s.tasks.c.id == tid)
                    .values(
                        ctf_control={
                            **control,
                            "native_budget_hold": {**existing, "recovery_blocker": reason},
                        }
                    )
                )
            return
        reason = budget["hold_reason"] or reason
        hold = {"reason": reason, "source_id": budget["accounting_source_id"]}
        if (
            reason == "budget_exceeded"
            and not budget["hold_reason"]
            and not control.get("failure_hold")
        ):
            hold["id"] = str(uuid4())
        await conn.execute(
            update(s.native_task_budgets)
            .where(s.native_task_budgets.c.task_id == tid)
            .values(hold_reason=reason)
        )
        await conn.execute(
            update(s.tasks)
            .where(s.tasks.c.id == tid)
            .values(
                ctf_control={
                    **control,
                    "native_budget_hold": hold,
                    "failure_hold": control.get("failure_hold")
                    or (
                        budget_failure_hold(hold["id"])
                        if "id" in hold
                        else {"task_wide": True, "source": "native_gateway"}
                    ),
                }
            )
        )
        await cancel_admitted(conn, tid)
        if self.pause_control is not None and "id" in hold:
            try:
                async with conn.begin_nested():
                    await self.pause_control.start_budget(conn, tid, hold["id"], budget)
            except (HTTPException, KeyError):
                # An unconfirmed original Runner stays held without any recovery snapshot.
                pass
        await self.gateway.ctf._emit(
            conn,
            tid,
            "coordination.reminded",
            "system",
            {
                "kind": "native_budget_hold",
                "status": "admission_held",
                "reason": reason,
                "accounting_source_id": budget["accounting_source_id"],
                "currency": "USD",
            },
        )

    async def hold(self, tid, reason, *, config=None):
        async with self.repo.engine.begin() as conn:
            try:
                await self.repo.lock(conn, tid)
            except KeyError:
                return
            if (
                config is not None
                and (await self._budget(conn, tid, current_source=False))["config"] != config
            ):
                return
            await self._hold(conn, tid, reason)

    async def _total(self, conn, tid):
        return (
            await conn.execute(
                select(
                    func.coalesce(func.sum(cast(s.native_request_costs.c.cost_usd, Numeric)), 0)
                ).where(
                    s.native_request_costs.c.task_id == tid,
                    s.native_request_costs.c.state == "known",
                )
            )
        ).scalar_one()

    def _due_calls(self):
        return (
            (s.runner_calls.c.kind == "model")
            & s.runner_calls.c.state.in_(["dispatched", "unknown"])
            & or_(
                s.runner_calls.c.data["next_reconcile_at"].astext.is_(None),
                s.runner_calls.c.data["next_reconcile_at"].astext <= self.clock().isoformat(),
            )
        )

    @staticmethod
    def _status_used(status, budget):
        if not isinstance(status.get("budgets"), list) or any(
            not isinstance(item, dict) for item in status["budgets"]
        ):
            raise ValueError("Native budget status has an unsupported shape")
        matches = [
            item
            for item in status["budgets"]
            if item.get("apiKeyName") == budget["key_name"] and item.get("name") == "task-usd"
        ]
        if len(matches) != 1:
            raise ValueError("Native budget status is missing or ambiguous")
        value = matches[0]
        limit, usage, window = value["limit"], value["usage"], value["window"]
        if any(not isinstance(item, dict) for item in (limit, usage, window)):
            raise ValueError("Native budget status has an unsupported shape")
        used = Decimal(str(usage["used"]))
        if (
            limit["unit"] != "USD"
            or Decimal(str(limit["amount"])) != Decimal(budget["config"]["threshold_usd"])
            or not used.is_finite()
            or used < 0
            or type(usage["exceeded"]) is not bool
            or value["onBudgetExceeded"] != "Block"
            or window["durationMs"] is not None
            or window["expired"] is not False
        ):
            raise ValueError("Native budget status does not match the frozen contract")
        return used, usage["exceeded"]

    async def consume(self, tid):
        """Use bounded native completion pages for original attempts, outside DB transactions."""
        async with self.repo.engine.connect() as conn:
            budget = await self._budget(conn, tid)
            calls = (
                (
                    await conn.execute(
                        select(s.runner_calls)
                        .where(s.runner_calls.c.task_id == tid, self._due_calls())
                        .order_by(s.runner_calls.c.created_at, s.runner_calls.c.id)
                        .limit(100)
                    )
                )
                .mappings()
                .all()
            )
        if not budget["config"].get("pending_increase"):
            try:
                status = await self.admin.budget(budget["key_name"])
                used, exceeded = self._status_used(status, budget)
            except (
                HTTPException,
                httpx.HTTPError,
                ValueError,
                KeyError,
                TypeError,
                DecimalException,
            ):
                await self.hold(tid, "native_accounting_unavailable", config=budget["config"])
            else:
                async with self.repo.engine.begin() as conn:
                    await self.repo.lock(conn, tid)
                    current = await self._budget(conn, tid)
                    if current["config"] == budget["config"]:
                        prior = Decimal(
                            (current["status_snapshot"] or {}).get("used_high_water_usd", "0")
                        )
                        high_water = max(prior, used)
                        await conn.execute(
                            update(s.native_task_budgets)
                            .where(s.native_task_budgets.c.task_id == tid)
                            .values(
                                status_snapshot={
                                    "latest": status,
                                    "used_high_water_usd": str(high_water),
                                }
                            )
                        )
                        if exceeded or high_water >= Decimal(current["config"]["threshold_usd"]):
                            await self._hold(conn, tid, "budget_exceeded")
        for call in calls:
            cursor, records = None, []
            for _ in range(10):
                page = await self.admin.logs(self._attributes(tid, call), cursor=cursor)
                records.extend(page["logs"])
                next_cursor = page.get("nextCursor")
                if not next_cursor or next_cursor == cursor:
                    break
                cursor = next_cursor
            else:
                raise ValueError("Native attempt log exceeds its reconciliation page bound")
            if not records:
                data = call["data"]
                end = datetime.fromisoformat(
                    data.get("transport_observed_at", data["authorized_until"])
                )
                missing = self.clock() >= end + timedelta(seconds=5)
                async with self.repo.engine.begin() as conn:
                    await self.repo.lock(conn, tid)
                    current_call = await self.gateway._call(conn, tid, call["id"])
                    if current_call["state"] in {"not_sent", "completed"}:
                        continue
                    await conn.execute(
                        update(s.runner_calls)
                        .where(s.runner_calls.c.task_id == tid, s.runner_calls.c.id == call["id"])
                        .values(
                            data={
                                **current_call["data"],
                                "fee_state": "missing" if missing else "awaiting_log",
                                "next_reconcile_at": (
                                    self.clock() + timedelta(seconds=30 if missing else 1)
                                ).isoformat(),
                            }
                        )
                    )
                    if missing:
                        await self._hold(conn, tid, "native_log_missing")
                continue
            await self._consume_records(tid, budget, call, records)

    async def _consume_records(self, tid, budget, call, records):
        expected = self._attributes(tid, call)
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            current = await self.gateway._call(conn, tid, call["id"])
            if current["state"] == "not_sent":
                raise HTTPException(409, "Native log conflicts with a proven unsent attempt")
            known = True
            for record in records:
                attrs = record.get("attributes", {})
                if not isinstance(attrs, dict) or not isinstance(record.get("id"), str):
                    raise ValueError("Native log has an unsupported shape")
                if any(str(attrs.get(key)) != value for key, value in expected.items()):
                    raise HTTPException(
                        409, "Native log attribution does not match its original attempt"
                    )
                if attrs.get("agw.api_key.name") != budget["key_name"]:
                    raise HTTPException(409, "Native log belongs to another Task key")
                usage = record.get("usage") or {}
                if not isinstance(usage, dict):
                    raise ValueError("Native log usage has an unsupported shape")
                try:
                    amount = Decimal(str(record["cost"]))
                    complete = (
                        amount.is_finite()
                        and amount >= 0
                        and all(
                            type(usage.get(field)) is int and usage[field] >= 0
                            for field in ("inputTokens", "outputTokens", "totalTokens")
                        )
                    )
                except (KeyError, InvalidOperation):
                    complete, amount = False, None
                known &= complete
                evidence = {
                    key: record.get(key)
                    for key in (
                        "id",
                        "startedAt",
                        "completedAt",
                        "httpStatus",
                        "error",
                        "genAi",
                        "usage",
                        "cost",
                    )
                }
                evidence.update(
                    attributes=expected, catalog_revision=call["data"]["catalog_revision"]
                )
                original = (
                    (
                        await conn.execute(
                            select(s.native_request_costs).where(
                                s.native_request_costs.c.accounting_source_id
                                == budget["accounting_source_id"],
                                s.native_request_costs.c.native_log_id == record["id"],
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                if original:
                    if (
                        original["task_id"] != tid
                        or original["attempt_id"] != call["data"]["attempt_id"]
                    ):
                        raise HTTPException(409, "Native log identity is already assigned")
                    if original["state"] == "known":
                        if not complete or Decimal(original["cost_usd"]) != amount:
                            raise HTTPException(409, "Confirmed native fee cannot regress")
                        continue
                    await conn.execute(
                        update(s.native_request_costs)
                        .where(
                            s.native_request_costs.c.accounting_source_id
                            == budget["accounting_source_id"],
                            s.native_request_costs.c.native_log_id == record["id"],
                        )
                        .values(
                            state="known" if complete else "pending",
                            cost_usd=str(amount) if complete else None,
                            evidence=evidence,
                        )
                    )
                else:
                    await conn.execute(
                        insert(s.native_request_costs).values(
                            accounting_source_id=budget["accounting_source_id"],
                            native_log_id=record["id"],
                            task_id=tid,
                            attempt_id=call["data"]["attempt_id"],
                            state="known" if complete else "pending",
                            cost_usd=str(amount) if complete else None,
                            evidence=evidence,
                            created_at=self.clock(),
                        )
                    )
            data = {
                **current["data"],
                "fee_state": "known" if known else "unknown",
                "next_reconcile_at": (self.clock() + timedelta(seconds=60)).isoformat(),
            }
            await conn.execute(
                update(s.runner_calls)
                .where(s.runner_calls.c.task_id == tid, s.runner_calls.c.id == call["id"])
                .values(
                    data=data, state="completed" if known else "unknown", concurrency_active=False
                )
            )
            if not known:
                await self._hold(conn, tid, "native_fee_pending")
            current_budget = await self._budget(conn, tid)
            if await self._total(conn, tid) >= Decimal(current_budget["config"]["threshold_usd"]):
                await self._hold(conn, tid, "budget_exceeded")

    async def view(self, tid):
        async with self.repo.engine.connect() as conn:
            budget = await self._budget(conn, tid, current_source=False)
            total = await self._total(conn, tid)
            pending = await self.gateway._count(
                conn,
                s.runner_calls.c.task_id == tid,
                s.runner_calls.c.kind == "model",
                s.runner_calls.c.state.in_(["dispatched", "unknown"]),
            )
            config = budget["config"]
            return {
                "accounting_source_id": budget["accounting_source_id"],
                "currency": "USD",
                "confirmed_usd": str(total),
                "converted_cny": str(total * Decimal(config["exchange_rate"]["rate_cny_per_usd"])),
                "threshold_usd": config["threshold_usd"],
                "overage_usd": str(max(Decimal(0), total - Decimal(config["threshold_usd"]))),
                "pending_attempts": pending,
                "hold_reason": budget["hold_reason"],
                "exchange_rate": config["exchange_rate"],
                "catalog_revision": config["catalog_revision"],
                "native_used_high_water_usd": (budget["status_snapshot"] or {}).get(
                    "used_high_water_usd"
                ),
            }

    async def retire(self, tid, *, explicit_delete=False):
        """Remove the native credential before dropping its only encrypted local copy."""
        async with self.repo.engine.begin() as conn:
            if not (await conn.execute(select(s.tasks.c.id).where(s.tasks.c.id == tid))).first():
                return
            await self.repo.lock(conn, tid)
            task = await row(conn, s.tasks, tid)
            found = (
                (
                    await conn.execute(
                        select(s.native_task_budgets).where(s.native_task_budgets.c.task_id == tid)
                    )
                )
                .mappings()
                .first()
            )
            if found is None or found["publication_state"] == "retired":
                return
            deletion_policy = False
            if explicit_delete:
                resources = (
                    (
                        await conn.execute(
                            select(s.task_runner_resources).where(
                                s.task_runner_resources.c.task_id == tid
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                deletion_policy = bool(
                    resources is not None
                    and resources["state"] == "stopped"
                    and resources["data"].get("exit_confirmed") is True
                    and resources["data"].get("retirement", {}).get("policy") == "user_delete"
                )
            if (
                not task["deleting"]
                or (not task["cleanup_ready"] and not deletion_policy)
                or task["status"] not in {"stopped", "failed", "finished"}
            ):
                raise HTTPException(409, "Native key retirement requires completed Task cleanup")
            if await self.gateway._count(
                conn,
                s.runner_calls.c.task_id == tid,
                s.runner_calls.c.state.in_(["dispatched", "unknown"]),
            ):
                raise HTTPException(409, "Original calls still require reconciliation")
            if found["publication_state"] in {"creating", "creating_unknown"}:
                raise HTTPException(409, "Native key creation still requires readback")
            if found["publication_state"] == "prepared":
                await conn.execute(
                    update(s.native_task_budgets)
                    .where(s.native_task_budgets.c.task_id == tid)
                    .values(publication_state="retired")
                )
                return
            budget = dict(found)
        if budget["accounting_source_id"] != self.settings.native_gateway_accounting_source_id:
            raise HTTPException(409, "Native accounting source changed")
        resources = await self.admin.resources()
        config = budget["config"]
        expected = config["native_value"]
        pending = config.get("pending_increase")
        owned = self.key_candidates(resources, budget)
        if len(owned) > 1:
            raise HTTPException(409, "Duplicate native key requires reconciliation")
        if owned:
            value = owned[0]["value"]
            if pending:
                matches = value in (pending["previous_native_value"], pending["native_value"])
            elif config.get("last_increase_hold_id"):
                matches = value == expected
            else:
                matches = self._matches(value, expected)
            if owned[0]["id"] != budget["native_resource_id"] or not matches:
                raise HTTPException(409, "Native key identity or configuration changed")
            await self.admin.delete_key(owned[0]["id"])
            if self.key_candidates(await self.admin.resources(), budget):
                raise HTTPException(503, "Native key retirement requires readback")
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            current = await self._budget(conn, tid)
            task = await row(conn, s.tasks, tid)
            if (
                not task["deleting"]
                or (not task["cleanup_ready"] and not deletion_policy)
                or current["native_resource_id"] != budget["native_resource_id"]
            ):
                raise HTTPException(409, "Native key retirement authority changed")
            await conn.execute(
                update(s.native_task_budgets)
                .where(s.native_task_budgets.c.task_id == tid)
                .values(publication_state="retired")
            )

    async def run(self):
        if not self.settings.native_gateway_accounting_source_id:
            return
        after = None
        while True:
            try:
                async with self.repo.engine.connect() as conn:
                    due = exists(
                        select(s.runner_calls.c.id).where(
                            s.runner_calls.c.task_id == s.native_task_budgets.c.task_id,
                            self._due_calls(),
                        )
                    )
                    query = (
                        select(s.native_task_budgets.c.task_id)
                        .join(s.tasks, s.tasks.c.id == s.native_task_budgets.c.task_id)
                        .where(
                            s.native_task_budgets.c.publication_state == "ready",
                            or_(
                                due,
                                (
                                    s.tasks.c.status.not_in(["stopped", "failed", "finished"])
                                    & s.native_task_budgets.c.hold_reason.is_(None)
                                    & s.tasks.c.deleting.is_(False)
                                ),
                            ),
                        )
                    )
                    if after is not None:
                        query = query.where(s.native_task_budgets.c.task_id > after)
                    tids = (
                        (
                            await conn.execute(
                                query.order_by(s.native_task_budgets.c.task_id).limit(100)
                            )
                        )
                        .scalars()
                        .all()
                    )
                after = tids[-1] if tids else None
                for tid in tids:
                    try:
                        await self.consume(tid)
                    except (
                        HTTPException,
                        httpx.HTTPError,
                        ValueError,
                        KeyError,
                        TypeError,
                        DecimalException,
                    ):
                        await self.hold(tid, "native_accounting_unavailable")
            except SQLAlchemyError:
                logging.getLogger(__name__).warning("Native accounting database is unavailable")
            await asyncio.sleep(1)
