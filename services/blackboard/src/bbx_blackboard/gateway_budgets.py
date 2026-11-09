"""Original lifetime AgentGateway budgets for ordinary and Progress execution."""

import hashlib
import secrets
from decimal import Decimal

from bbx_contracts.native_gateway import native_usd_amount
from fastapi import HTTPException
from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from bbx_blackboard.gateway_tasks import require_original_model
from bbx_blackboard.native_budget_increase import update_native_limit
from bbx_blackboard.store.schema import app_settings
from bbx_blackboard.store.schema import gateway_execution_budgets as budgets


class GatewayBudgets:
    def __init__(self, native, models):
        self.native = native
        self.models = models
        self.repo = native.repo
        self.admin = native.admin
        self.cipher = native.gateway.cipher

    @staticmethod
    async def find(conn, task_id, purpose):
        value = (
            (
                await conn.execute(
                    select(budgets).where(
                        budgets.c.task_id == task_id, budgets.c.purpose == purpose
                    )
                )
            )
            .mappings()
            .first()
        )
        return dict(value) if value is not None else None

    @staticmethod
    async def save(conn, task_id, purpose, **changes):
        await conn.execute(
            update(budgets)
            .where(budgets.c.task_id == task_id, budgets.c.purpose == purpose)
            .values(**changes)
        )

    async def prepare(self, conn, task_id, purpose, model, currency, cap):
        """Caller has already serialized and checked the original Task or job."""
        if currency != "USD":
            raise HTTPException(422, "Live AgentGateway budgets must use USD")
        if budget := await self.find(conn, task_id, purpose):
            if (
                budget["accounting_source_id"] != model.accounting_source_id
                or budget["config"]["model"] != model.model_dump(mode="json")
                or budget["config"]["budget_currency"] != currency
            ):
                raise HTTPException(409, "The original model budget cannot change identity")
            return budget
        if model.accounting_source_id != self.models.settings.native_gateway_accounting_source_id:
            raise HTTPException(409, "Gateway accounting source changed")
        try:
            limit = native_usd_amount(cap)
        except ValueError as error:
            raise HTTPException(422, "The budget cannot be represented in USD") from error
        key = "bbx-gateway-" + secrets.token_urlsafe(32)
        fingerprint = hashlib.sha256(key.encode()).hexdigest()
        name = f"bbx-{purpose}-{task_id}"
        budget = {
            "task_id": task_id,
            "purpose": purpose,
            "key_name": name,
            "key_fingerprint": fingerprint,
            "key_ciphertext": self.cipher.encrypt(key.encode()).decode(),
            "accounting_source_id": model.accounting_source_id,
            "native_resource_id": None,
            "publication_state": "prepared",
            "config": {
                "model": model.model_dump(mode="json"),
                "budget_currency": "USD",
                "threshold_usd": str(limit),
                "native_value": {
                    "keyHash": "sha256:" + fingerprint,
                    "metadata": {"name": name},
                    "allowedModels": [model.alias],
                    "budgets": [
                        {
                            "name": "task-usd",
                            "limit": {"unit": "USD", "amount": float(limit)},
                            "onBudgetExceeded": "Block",
                        }
                    ],
                },
            },
            "status_snapshot": None,
        }
        await conn.execute(insert(budgets).values(**budget))
        return budget

    async def publish(self, task_id, purpose, model):
        await require_original_model(self.models, model)
        async with self.repo.engine.connect() as conn:
            budget = await self.find(conn, task_id, purpose)
        if budget is None or budget["publication_state"] == "retired":
            raise HTTPException(409, "No original AgentGateway budget is available")
        if budget["config"].get("pending_increase"):
            raise HTTPException(409, "Read back the original budget update first")
        expected = budget["config"]["native_value"]
        if budget["publication_state"] == "ready":
            await self.native.verified_key(
                budget, expected, exact=bool(budget["config"].get("last_increase"))
            )
            return budget
        resources = await self.admin.resources()
        matches = self.native.key_candidates(resources, budget)
        if not matches:
            async with self.repo.engine.begin() as conn:
                await self.repo.lock(conn, task_id)
                original = await self.find(conn, task_id, purpose)
                if original != budget or budget["publication_state"] != "prepared":
                    raise HTTPException(409, "The original budget requires readback")
                await self.save(conn, task_id, purpose, publication_state="creating")
            # Losing the response leaves the original key discoverable by hash; do not create twice.
            await self.admin.create_key(expected)
            matches = self.native.key_candidates(await self.admin.resources(), budget)
        if len(matches) != 1 or not self.native._matches(matches[0]["value"], expected):
            raise HTTPException(409, "The original AgentGateway budget conflicts")
        ready = {**budget, "native_resource_id": matches[0]["id"], "publication_state": "ready"}
        await self.native.verified_key(ready, expected)
        if model.alias not in {entry["id"] for entry in await self.admin.models(self.key(ready))}:
            raise HTTPException(409, "The scoped key does not see its original model")
        await self.native.threshold_status(ready)
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, task_id)
            current = await self.find(conn, task_id, purpose)
            if (
                current is None
                or current["config"] != budget["config"]
                or current["native_resource_id"] not in {None, ready["native_resource_id"]}
                or current["publication_state"] not in {"prepared", "creating", "ready"}
            ):
                raise HTTPException(409, "The original budget changed during publication")
            await self.save(
                conn,
                task_id,
                purpose,
                publication_state="ready",
                native_resource_id=ready["native_resource_id"],
            )
        return ready

    def key(self, budget):
        return self.cipher.decrypt(budget["key_ciphertext"].encode()).decode()

    @staticmethod
    def floor(budget):
        return Decimal((budget["status_snapshot"] or {}).get("used_high_water_usd", "0"))

    async def status(self, budget):
        status, used, exceeded = await self.native.threshold_status(budget)
        if used < self.floor(budget):
            raise HTTPException(409, "AgentGateway cumulative usage regressed")
        return status, used, exceeded

    @staticmethod
    def request_key(task_id, purpose, request_id):
        return f"gateway-budget-request:v1:{task_id}:{purpose}:{request_id}"

    @classmethod
    async def request_receipt(cls, conn, task_id, purpose, request_id):
        return await conn.scalar(
            select(app_settings.c.value).where(
                app_settings.c.key == cls.request_key(task_id, purpose, request_id)
            )
        )

    @classmethod
    async def save_request(cls, conn, task_id, purpose, request_id, receipt):
        await conn.execute(
            pg_insert(app_settings)
            .values(key=cls.request_key(task_id, purpose, request_id), value=receipt)
            .on_conflict_do_update(index_elements=[app_settings.c.key], set_={"value": receipt})
        )

    async def increase(self, task_id, purpose, request_id, additional_usd, expected_usd):
        """Prepare, apply once and read back the exact original native resource."""
        async with self.repo.engine.connect() as conn:
            budget = await self.find(conn, task_id, purpose)
            original = await self.request_receipt(conn, task_id, purpose, request_id)
        if original is not None:
            if Decimal(original["additional_usd"]) != additional_usd:
                raise HTTPException(409, "The original budget request changed")
            return
        if budget is None or budget["publication_state"] != "ready":
            raise HTTPException(409, "The original AgentGateway budget is not ready")
        config = budget["config"]
        intent = config.get("pending_increase")
        if intent and (
            intent["request_id"] != str(request_id)
            or Decimal(intent["additional_usd"]) != additional_usd
        ):
            raise HTTPException(409, "Another budget update requires readback")
        if intent is None:
            current_usd = Decimal(config["threshold_usd"])
            target = expected_usd + additional_usd
            delta = target - current_usd
            if purpose == "ordinary" and delta <= 0 or purpose == "progress" and not delta:
                raise HTTPException(409, "The request does not change the original native budget")
            native_value = await self.native.verified_key(
                budget, config["native_value"], exact=bool(config.get("last_increase"))
            )
            try:
                usd = native_usd_amount(target)
            except ValueError as error:
                raise HTTPException(422, "The budget cannot be represented in USD") from error
            desired = update_native_limit(native_value, usd)
            _, used, _ = await self.status(budget)
            if used >= Decimal(desired["threshold_usd"]):
                raise HTTPException(422, "The new limit must exceed original cumulative usage")
            intent = {
                **desired,
                "request_id": str(request_id),
                "additional_usd": str(additional_usd),
                "used_floor": str(used),
            }
            async with self.repo.engine.begin() as conn:
                await self.repo.lock(conn, task_id)
                if await self.find(conn, task_id, purpose) != budget:
                    raise HTTPException(409, "The original budget changed")
                await self.save(
                    conn, task_id, purpose, config={**config, "pending_increase": intent}
                )
            try:
                await self.admin.update_key(budget["native_resource_id"], desired["native_value"])
            except HTTPException:
                pass
        desired = {key: intent[key] for key in ("threshold_usd", "native_value")}
        await self.native.verified_key(budget, desired["native_value"], exact=True)
        status, used, _ = await self.native.threshold_status({**budget, "config": desired})
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, task_id)
            current = await self.find(conn, task_id, purpose)
            if (
                current is None
                or current["native_resource_id"] != budget["native_resource_id"]
                or current["config"].get("pending_increase") != intent
                or used < max(Decimal(intent["used_floor"]), self.floor(current))
            ):
                raise HTTPException(409, "The original budget increase is unconfirmed")
            next_config = {**current["config"], **desired}
            del next_config["pending_increase"]
            receipt = {
                "request_id": str(request_id),
                "additional_usd": str(additional_usd),
                "threshold_usd": desired["threshold_usd"],
            }
            next_config["last_increase"] = receipt
            await self.save_request(conn, task_id, purpose, request_id, receipt)
            await self.save(
                conn,
                task_id,
                purpose,
                config=next_config,
                status_snapshot={"latest": status, "used_high_water_usd": str(used)},
            )

    @classmethod
    async def confirm_platform_limit(
        cls, conn, task_id, purpose, expected, request_id=None, payload=None
    ):
        budget = await cls.find(conn, task_id, purpose)
        if budget is None or budget["publication_state"] != "ready":
            raise HTTPException(409, "The original AgentGateway budget is not ready")
        config = budget["config"]
        if config.get("pending_increase"):
            raise HTTPException(409, "The original budget update requires readback")
        receipt = None
        if request_id is not None:
            receipt = await cls.request_receipt(conn, task_id, purpose, request_id)
            if receipt is None or receipt["request_id"] != str(request_id):
                raise HTTPException(409, "The native budget increase was not confirmed")
            if "platform_payload" in receipt:
                if receipt["platform_payload"] != payload:
                    raise HTTPException(409, "The original platform budget request changed")
                return False
        if Decimal(config["threshold_usd"]) != expected:
            raise HTTPException(409, "The original native and platform limits differ")
        if request_id is not None:
            if receipt is None or Decimal(receipt["threshold_usd"]) != expected:
                raise HTTPException(409, "The original budget threshold changed")
            acknowledged = {**receipt, "platform_payload": payload}
            await cls.save_request(conn, task_id, purpose, request_id, acknowledged)
            if (config.get("last_increase") or {}).get("request_id") == str(request_id):
                await cls.save(
                    conn, task_id, purpose, config={**config, "last_increase": acknowledged}
                )
        return True

    async def retire_task(self, task_id):
        """Delete only original managed key resources after their authorized use is closed."""
        from bbx_blackboard.store import schema as s
        from bbx_blackboard.store.repository import row

        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, task_id)
            scopes = (
                (await conn.execute(select(budgets).where(budgets.c.task_id == task_id)))
                .mappings()
                .all()
            )
            if not scopes:
                return
            task = await row(conn, s.tasks, task_id)
            if (
                not task["deleting"]
                or task["status"] not in {"created", "finished", "failed", "stopped"}
                or task["status"] != "created"
                and not task["cleanup_ready"]
            ):
                raise HTTPException(409, "The original task has not closed")
            unresolved = await conn.scalar(
                select(s.gateway_execution_attempts.c.id)
                .where(
                    s.gateway_execution_attempts.c.task_id == task_id,
                    s.gateway_execution_attempts.c.purpose.in_(("ordinary", "progress")),
                    s.gateway_execution_attempts.c.fee_state != "known",
                )
                .limit(1)
            )
            if unresolved:
                raise HTTPException(409, "Original AgentGateway calls still require readback")
            for scope in scopes:
                if (
                    scope["config"].get("pending_increase")
                    or scope["publication_state"] == "creating"
                ):
                    raise HTTPException(409, "Original AgentGateway key still requires readback")
                if scope["publication_state"] == "prepared":
                    await self.save(conn, task_id, scope["purpose"], publication_state="retired")
        for scope in scopes:
            if scope["publication_state"] not in {"ready", "retiring"}:
                continue
            original = self.native.key_candidates(await self.admin.resources(), scope)
            if original:
                await self.native.verified_key(
                    scope,
                    scope["config"]["native_value"],
                    exact=bool(scope["config"].get("last_increase")),
                )
                if scope["publication_state"] == "ready":
                    async with self.repo.engine.begin() as conn:
                        await self.repo.lock(conn, task_id)
                        if await self.find(conn, task_id, scope["purpose"]) != dict(scope):
                            raise HTTPException(409, "Original AgentGateway key changed")
                        await self.save(
                            conn, task_id, scope["purpose"], publication_state="retiring"
                        )
                    try:
                        await self.admin.delete_key(scope["native_resource_id"])
                    except HTTPException:
                        pass
                if self.native.key_candidates(await self.admin.resources(), scope):
                    raise HTTPException(
                        409, "Original AgentGateway key retirement requires readback"
                    )
            async with self.repo.engine.begin() as conn:
                await self.repo.lock(conn, task_id)
                await self.save(conn, task_id, scope["purpose"], publication_state="retired")
