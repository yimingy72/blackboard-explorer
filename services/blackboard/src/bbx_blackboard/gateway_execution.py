"""Once-only model dispatch for trusted Runtime purposes outside native Runner bindings."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from bbx_contracts.gateway_models import (
    GATEWAY_EXECUTION_ATTRIBUTE_HEADERS,
    GatewayExecutionGrant,
    GatewayExecutionResult,
    GatewayModelSnapshot,
    OrdinaryGatewayAttempt,
    ProgressGatewayAttempt,
    ReviewGatewayAttempt,
)
from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import Numeric, cast, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from bbx_blackboard.auth import require_service
from bbx_blackboard.gateway_budgets import GatewayBudgets
from bbx_blackboard.gateway_cost_evidence import gateway_cost_evidence
from bbx_blackboard.gateway_maintenance import admission
from bbx_blackboard.gateway_tasks import (
    ctf_review_allowed,
    require_original_model,
    task_gateway_model,
)
from bbx_blackboard.store import schema as s
from bbx_blackboard.store.repository import row

router = APIRouter(prefix="/api/tasks", tags=["agentgateway-runtime"])
TERMINAL = {"finished", "failed", "stopped"}
ACTIVE_AGENT = {"running", "concluding"}


def denied(message="The original model execution is not authorized"):
    return HTTPException(409, message)


class GatewayExecution:
    def __init__(self, native, models):
        self.budgets = GatewayBudgets(native, models)
        self.repo = native.repo
        self.models = models
        self.settings = models.settings

    async def ordinary_ready(self, task_id):
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, task_id)
            task = await row(conn, s.tasks, task_id)
            frozen = await task_gateway_model(conn, task_id)
            if frozen is None or task["mode"] != "blackboard" or task["deleting"]:
                raise denied("This historical Task has no AgentGateway execution reference")
            model, currency = frozen
            await self.budgets.prepare(
                conn, task_id, "ordinary", model, currency, Decimal(str(task["budget"]["max_cost"]))
            )
        return await self.budgets.publish(task_id, "ordinary", model)

    async def _ordinary_scope(self, conn, task_id, body):
        task = await row(conn, s.tasks, task_id)
        agent = await row(conn, s.agent_runs, task_id, body.agent_id)
        role = agent["task_type"]
        if (
            task["mode"] != "blackboard"
            or task["deleting"]
            or task["run_number"] != body.run_number
            or task["status"] not in {"running", "closing"}
            or task["status"] == "closing"
            and role != "close"
            or agent["status"] not in ACTIVE_AGENT
            or body.expected_started_at != agent["started_at"]
            or role == "derive"
            and body.expected_derive_round != agent["derive_round"]
            or role != "derive"
            and body.expected_derive_round is not None
        ):
            raise denied()
        return task, agent

    async def _ordinary(self, conn, task_id, body):
        task, _ = await self._ordinary_scope(conn, task_id, body)
        frozen = await task_gateway_model(conn, task_id)
        if frozen is None:
            raise denied()
        return (
            frozen[0],
            frozen[1],
            Decimal(str(task["budget"]["max_cost"])),
            body.agent_id,
            body.agent_id,
        )

    async def _progress_scope(self, conn, task_id, body, *, pi=False):
        task = await row(conn, s.tasks, task_id)
        job = (
            (
                await conn.execute(
                    select(s.progress_jobs).where(
                        s.progress_jobs.c.task_id == task_id,
                        s.progress_jobs.c.id == body.job_id,
                    )
                )
            )
            .mappings()
            .first()
        )
        current = (
            await conn.execute(
                select(s.task_progress.c.data).where(s.task_progress.c.task_id == task_id)
            )
        ).scalar_one_or_none()
        if (
            task["deleting"]
            or not current
            or not current["settings"]["enabled"]
            or job is None
            or job["status"] != "running"
            or task["run_number"] != body.run_number
            or job["run_number"] != body.run_number
        ):
            raise denied("The original Progress job is not active")
        data = job["data"]
        if (
            data.get("claim_token") != str(body.claim_token)
            or datetime.fromisoformat(data["lease_until"]) <= datetime.now(UTC)
            or data.get("budget_currency") != "USD"
            or not (
                data.get("pi_model")
                if pi
                else data.get("gateway_model")
                and data["receipts"].get(str(body.reservation_id), {}).get("status") == "reserved"
            )
        ):
            raise denied("The original Progress reservation is unavailable")
        return task, dict(job), current

    async def _progress(self, conn, task_id, body):
        _, job, current = await self._progress_scope(conn, task_id, body)
        return (
            GatewayModelSnapshot.model_validate(job["data"]["gateway_model"]),
            "USD",
            Decimal(current["settings"]["max_cost"]),
            "__progress__",
            str(body.job_id),
        )

    async def _review(self, conn, task_id, body):
        task = await row(conn, s.tasks, task_id)
        if task["deleting"] or task["run_number"] != body.run_number:
            raise denied("Review requires the original Task run")
        if task["mode"] == "blackboard" and body.message_id is not None:
            agent = await row(conn, s.agent_runs, task_id, body.agent_id)
            message = (
                (
                    await conn.execute(
                        select(s.agent_messages).where(
                            s.agent_messages.c.task_id == task_id,
                            s.agent_messages.c.id == body.message_id,
                            s.agent_messages.c.agent_id == body.agent_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if (
                agent["status"] not in {"finished", "failed"}
                or message is None
                or message["role"] != "user"
                or message["purpose"] != "review"
                or message["request_run_number"] != body.run_number
                or message["request_started_at"] != agent["started_at"]
                or message["request_derive_round"] != agent["derive_round"]
                or message["status"] != "processing"
                or message["claim_token"] != body.claim_token
                or message["lease_until"] is None
                or message["lease_until"] <= datetime.now(UTC)
            ):
                raise denied("The original review message claim changed")
            return str(body.message_id), message["review_model"]
        if task["mode"] == "ctf" and body.turn_id is not None:
            member = await row(conn, s.ctf_members, task_id, body.agent_id)
            turn = await row(conn, s.ctf_turns, task_id, str(body.turn_id))
            if (
                not ctf_review_allowed(task, member)
                or task["status"] in TERMINAL
                and not task["cleanup_ready"]
                or member["current_turn_id"] != str(body.turn_id)
                or member["generation"] != body.generation
                or turn["generation"] != body.generation
                or turn["agent_id"] != body.agent_id
                or turn["purpose"] != "review"
                or turn["status"] != "running"
            ):
                raise denied("The original review turn changed")
            selected = await conn.scalar(
                select(s.ctf_messages.c.id)
                .where(
                    s.ctf_messages.c.task_id == task_id,
                    s.ctf_messages.c.id.in_(turn["assignment_message_ids"]),
                    s.ctf_messages.c.claim_turn_id == str(body.turn_id),
                    s.ctf_messages.c.claim_generation == body.generation,
                    s.ctf_messages.c.status.in_(("leased", "delivered")),
                    s.ctf_messages.c.sender_kind == "user",
                    s.ctf_messages.c.purpose == "review",
                    s.ctf_messages.c.request_run_number == body.run_number,
                    s.ctf_messages.c.request_agent_generation == turn["review_origin_generation"],
                )
                .limit(1)
            )
            if selected is None:
                raise denied("The original terminal review message is not claimed")
            return str(body.turn_id), turn["review_model"]
        raise denied("An original terminal review claim is required")

    async def _insert_attempt(
        self, conn, task_id, body, purpose, model, key_name, agent_id, conversation
    ):
        await admission(conn)
        if not self.settings.gateway_site_id:
            raise HTTPException(503, "AgentGateway execution site is not configured")
        exists = await conn.scalar(
            select(s.gateway_execution_attempts.c.id).where(
                s.gateway_execution_attempts.c.task_id == task_id,
                s.gateway_execution_attempts.c.id == body.attempt_id,
            )
        )
        if exists is not None:
            raise denied("The original attempt already crossed its send boundary")
        if purpose == "progress":
            reserved = await conn.scalar(
                select(s.gateway_execution_attempts.c.id)
                .where(
                    s.gateway_execution_attempts.c.task_id == task_id,
                    s.gateway_execution_attempts.c.purpose == purpose,
                    s.gateway_execution_attempts.c.attributes["bbx_reservation_id"].astext
                    == str(body.reservation_id),
                )
                .limit(1)
            )
            if reserved is not None:
                raise denied("The original Progress reservation was already dispatched")
        attrs = {
            "bbx_site_id": self.settings.gateway_site_id,
            "bbx_task_id": str(task_id),
            "bbx_agent_id": agent_id,
            "bbx_attempt_id": str(body.attempt_id),
            "bbx_purpose": purpose,
            "bbx_conversation_id": conversation,
            "bbx_run_number": str(body.run_number),
            "bbx_model_alias": model.alias,
        }
        if purpose == "progress":
            attrs["bbx_reservation_id"] = str(body.reservation_id)
        await conn.execute(
            insert(s.gateway_execution_attempts).values(
                task_id=task_id,
                id=body.attempt_id,
                purpose=purpose,
                accounting_source_id=model.accounting_source_id,
                key_name=key_name,
                body_sha256=body.body_sha256,
                attributes=attrs,
                phase="dispatched",
            )
        )
        return {GATEWAY_EXECUTION_ATTRIBUTE_HEADERS[key]: value for key, value in attrs.items()}

    async def claim(self, task_id, purpose, body):
        check = self._ordinary if purpose == "ordinary" else self._progress
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, task_id)
            original = await check(conn, task_id, body)
            model, currency, cap, agent_id, conversation = original
            if (model.alias, model.catalog_revision) != (body.alias, body.catalog_revision):
                raise denied("The original model reference changed")
            await self.budgets.prepare(conn, task_id, purpose, model, currency, cap)
        budget = await self.budgets.publish(task_id, purpose, model)
        status, used, exceeded = await self.budgets.status(budget)
        blocked = exceeded or used >= Decimal(budget["config"]["threshold_usd"])
        headers = None
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, task_id)
            if await check(conn, task_id, body) != original:
                raise denied("The original execution changed")
            current = await self.budgets.find(conn, task_id, purpose)
            if (
                current is None
                or current["native_resource_id"] != budget["native_resource_id"]
                or current["config"] != budget["config"]
                or Decimal(current["config"]["threshold_usd"]) != cap
                or used < self.budgets.floor(current)
            ):
                raise denied("The original model budget changed")
            await self.budgets.save(
                conn,
                task_id,
                purpose,
                status_snapshot={"latest": status, "used_high_water_usd": str(used)},
            )
            if not blocked:
                headers = await self._insert_attempt(
                    conn,
                    task_id,
                    body,
                    purpose,
                    model,
                    budget["key_name"],
                    agent_id,
                    conversation,
                )
        if blocked:
            raise HTTPException(
                429,
                {
                    "code": "gateway_budget_exhausted",
                    "message": "The original AgentGateway budget is exhausted",
                },
            )
        assert headers is not None
        return GatewayExecutionGrant(
            gateway_url=self.settings.gateway_runtime_data_url
            or self.settings.native_gateway_data_url,
            key=self.budgets.key(budget),
            headers=headers,
            attempt_id=body.attempt_id,
            body_sha256=body.body_sha256,
        )

    async def review_claim(self, task_id, body):
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, task_id)
            original = await self._review(conn, task_id, body)
        conversation, frozen = original
        if frozen is None:
            raise denied("The original review model was not frozen")
        model = GatewayModelSnapshot.model_validate(frozen)
        if (model.alias, model.catalog_revision) != (body.alias, body.catalog_revision):
            raise denied("The original review model changed")
        if not model.tool_call:
            raise denied("The selected review route does not support tools")
        key, name = await self.review_key()
        await require_original_model(self.models, model)
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, task_id)
            if await self._review(conn, task_id, body) != original:
                raise denied("The original review claim changed")
            headers = await self._insert_attempt(
                conn,
                task_id,
                body,
                "review",
                model,
                name,
                body.agent_id,
                conversation,
            )
        return GatewayExecutionGrant(
            gateway_url=self.settings.gateway_runtime_data_url
            or self.settings.native_gateway_data_url,
            key=key,
            headers=headers,
            attempt_id=body.attempt_id,
            body_sha256=body.body_sha256,
        )

    async def review_key(self):
        from bbx_blackboard.gateway_review_key import read_review_key

        return await read_review_key(self.settings, self.repo.engine, self.budgets.admin)

    async def result(self, task_id, attempt_id, body):
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, task_id)
            original = (
                (
                    await conn.execute(
                        select(s.gateway_execution_attempts).where(
                            s.gateway_execution_attempts.c.task_id == task_id,
                            s.gateway_execution_attempts.c.id == attempt_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if original is None or original["body_sha256"] != body.body_sha256:
                raise denied("The original attempt does not match")
            if original["phase"] != "dispatched":
                if (original["phase"], original["status_code"]) != (body.outcome, body.status_code):
                    raise denied("The original transport result changed")
                return
            await conn.execute(
                update(s.gateway_execution_attempts)
                .where(
                    s.gateway_execution_attempts.c.task_id == task_id,
                    s.gateway_execution_attempts.c.id == attempt_id,
                )
                .values(phase=body.outcome, status_code=body.status_code)
            )

    async def consume_costs(self):
        attempt = s.gateway_execution_attempts
        async with self.repo.engine.connect() as conn:
            due = (
                (
                    await conn.execute(
                        select(attempt)
                        .where(
                            attempt.c.accounting_source_id
                            == self.settings.native_gateway_accounting_source_id,
                            attempt.c.fee_state != "known",
                            attempt.c.next_check_at <= datetime.now(UTC),
                        )
                        .order_by(attempt.c.next_check_at)
                        .limit(12)
                    )
                )
                .mappings()
                .all()
            )
        for original in due:
            evidence = None
            amount = None
            try:
                page = await self.budgets.admin.logs(original["attributes"], limit=2)
                if len(page["logs"]) == 1 and page.get("nextCursor") is None:
                    evidence, amount = gateway_cost_evidence(
                        page["logs"][0],
                        original["attributes"],
                        original["key_name"],
                    )
            except (HTTPException, ValueError, ArithmeticError):
                pass
            key = (attempt.c.task_id == original["task_id"], attempt.c.id == original["id"])
            async with self.repo.engine.begin() as conn:
                row_now = (
                    (await conn.execute(select(attempt).where(*key).with_for_update()))
                    .mappings()
                    .first()
                )
                if row_now is None or row_now["fee_state"] == "known":
                    continue
                if evidence is None or amount is None:
                    await conn.execute(
                        update(attempt)
                        .where(*key)
                        .values(
                            fee_state="unknown",
                            next_check_at=datetime.now(UTC) + timedelta(seconds=15),
                        )
                    )
                    continue
                evidence = {
                    key: value for key, value in evidence.items() if key not in {"error", "genAi"}
                }
                cost = s.native_request_costs
                log_key = (
                    cost.c.accounting_source_id == original["accounting_source_id"],
                    cost.c.native_log_id == evidence["id"],
                )
                await conn.execute(
                    pg_insert(cost)
                    .values(
                        accounting_source_id=original["accounting_source_id"],
                        native_log_id=evidence["id"],
                        task_id=original["task_id"],
                        attempt_id=str(original["id"]),
                        state="known",
                        cost_usd=str(amount),
                        evidence=evidence,
                        created_at=datetime.now(UTC),
                    )
                    .on_conflict_do_nothing(
                        index_elements=[cost.c.accounting_source_id, cost.c.native_log_id]
                    )
                )
                stored = (await conn.execute(select(cost).where(*log_key))).mappings().one()
                if (
                    stored["task_id"] != original["task_id"]
                    or stored["attempt_id"] != str(original["id"])
                    or stored["state"] != "known"
                    or Decimal(stored["cost_usd"]) != amount
                ):
                    await conn.execute(
                        update(attempt)
                        .where(*key)
                        .values(
                            fee_state="unknown",
                            next_check_at=datetime.now(UTC) + timedelta(seconds=60),
                        )
                    )
                    continue
                await conn.execute(
                    update(attempt)
                    .where(*key)
                    .values(fee_state="known", native_log_id=evidence["id"])
                )


def _service(request, response):
    require_service(request)
    response.headers["Cache-Control"] = "no-store"
    return request.app.state.gateway_execution


@router.post("/{task_id}/gateway-model/ordinary/claim")
async def claim_ordinary(
    request: Request,
    response: Response,
    task_id: UUID,
    body: OrdinaryGatewayAttempt,
) -> GatewayExecutionGrant:
    return await _service(request, response).claim(task_id, "ordinary", body)


@router.post("/{task_id}/gateway-model/progress/claim")
async def claim_progress(
    request: Request,
    response: Response,
    task_id: UUID,
    body: ProgressGatewayAttempt,
) -> GatewayExecutionGrant:
    return await _service(request, response).claim(task_id, "progress", body)


@router.post("/{task_id}/gateway-model/review/claim")
async def claim_review(
    request: Request,
    response: Response,
    task_id: UUID,
    body: ReviewGatewayAttempt,
) -> GatewayExecutionGrant:
    return await _service(request, response).review_claim(task_id, body)


@router.post("/{task_id}/gateway-model/attempts/{attempt_id}/result")
async def attempt_result(
    request: Request,
    response: Response,
    task_id: UUID,
    attempt_id: UUID,
    body: GatewayExecutionResult,
) -> dict[str, bool]:
    await _service(request, response).result(task_id, attempt_id, body)
    return {"saved": True}


async def review_cost_ledger(engine, site_id, task_id=None):
    attempt, cost = s.gateway_execution_attempts, s.native_request_costs
    query = attempt.outerjoin(
        cost,
        (cost.c.accounting_source_id == attempt.c.accounting_source_id)
        & (cost.c.native_log_id == attempt.c.native_log_id),
    )
    filters = [
        attempt.c.purpose == "review",
        attempt.c.attributes["bbx_site_id"].astext == site_id,
    ]
    if task_id is not None:
        filters.append(attempt.c.task_id == task_id)
    async with engine.connect() as conn:
        if (
            task_id is not None
            and await conn.scalar(select(s.tasks.c.id).where(s.tasks.c.id == task_id)) is None
        ):
            raise HTTPException(404, "Task not found")
        amount, pending, count = (
            await conn.execute(
                select(
                    func.sum(cast(cost.c.cost_usd, Numeric)),
                    func.count().filter(cost.c.cost_usd.is_(None)),
                    func.count(),
                )
                .select_from(query)
                .where(*filters)
            )
        ).one()
        rows = (
            (
                await conn.execute(
                    select(
                        attempt.c.id, attempt.c.attributes, attempt.c.created_at, cost.c.cost_usd
                    )
                    .select_from(query)
                    .where(*filters)
                    .order_by(attempt.c.created_at.desc(), attempt.c.id.desc())
                    .limit(100)
                )
            )
            .mappings()
            .all()
        )
    return {
        "site_id": site_id,
        "confirmed_usd": str(amount) if amount is not None else None,
        "pending": pending,
        "more": count > len(rows),
        "calls": [
            {
                "attempt_id": str(item["id"]),
                "site_id": site_id,
                "task_id": item["attributes"]["bbx_task_id"],
                "agent_id": item["attributes"]["bbx_agent_id"],
                "conversation_id": item["attributes"]["bbx_conversation_id"],
                "model_alias": item["attributes"]["bbx_model_alias"],
                "at": item["created_at"].isoformat(),
                "cost_usd": item["cost_usd"],
            }
            for item in rows
        ],
    }


@router.get("/{task_id}/gateway-model/review-costs")
async def task_review_costs(request: Request, response: Response, task_id: UUID) -> dict:
    from bbx_blackboard.auth import require_task_reader

    if require_task_reader(request, task_id).kind != "user":
        raise HTTPException(403, "User token required")
    response.headers["Cache-Control"] = "no-store"
    return await review_cost_ledger(
        request.app.state.engine,
        request.app.state.settings.gateway_site_id,
        task_id,
    )
