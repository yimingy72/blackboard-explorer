"""Persist Task route intent while the native gateway owns routing and sessions."""

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import uuid4

from cryptography.fernet import InvalidToken
from fastapi import HTTPException
from sqlalchemy import insert, or_, select, update
from sqlalchemy.exc import SQLAlchemyError

from bbx_blackboard.native_admin import ROUTES_PATH
from bbx_blackboard.native_mcp import business_mcp_route
from bbx_blackboard.runner_control import row
from bbx_blackboard.store import schema as s


class NativeMCPRoutes:
    def __init__(self, gateway):
        self.gateway, self.repo, self.settings = gateway, gateway.repo, gateway.settings

    @property
    def admin(self):
        return self.gateway.native.admin

    def configuration(self):
        config = {
            "source": self.settings.native_gateway_accounting_source_id,
            "admin_url": self.settings.native_gateway_admin_url.rstrip("/"),
            "gateway_url": self.settings.native_mcp_gateway_url.rstrip("/"),
            "backend_url": self.settings.native_mcp_backend_url.rstrip("/"),
            "jwks_file": self.settings.native_mcp_jwks_file,
            "backend_token_file": self.settings.native_mcp_backend_token_file,
        }
        if not all(config.values()):
            raise HTTPException(503, "mcp_deployment_unavailable")
        for name in ("admin_url", "gateway_url", "backend_url"):
            try:
                parsed = urlsplit(config[name])
                port = parsed.port
            except ValueError as error:
                raise HTTPException(503, "mcp_deployment_unavailable") from error
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.path
                or port == 0
            ):
                raise HTTPException(503, "mcp_deployment_unavailable")
        if any(not config[name].startswith("/") for name in ("jwks_file", "backend_token_file")):
            raise HTTPException(503, "mcp_deployment_unavailable")
        return config

    @staticmethod
    def stopping(task):
        return (
            task["deleting"]
            or task["ctf_control"].get("native_stop_requested")
            or task["status"] in {"finished", "failed", "stopped", "closing"}
            or task["ctf_control"].get("phase") in {"closing", "finished", "failed", "stopped"}
        )

    async def _get(self, conn, tid):
        found = (
            (
                await conn.execute(
                    select(s.native_task_mcp_routes).where(
                        s.native_task_mcp_routes.c.task_id == tid
                    )
                )
            )
            .mappings()
            .first()
        )
        return dict(found) if found else None

    async def _set(self, conn, tid, **values):
        current = await self._get(conn, tid)
        assert current is not None
        changed = {name: value for name, value in values.items() if current[name] != value}
        if not changed:
            return current["revision"]
        fenced = bool(
            set(changed)
            & {"desired", "publication_state", "operation_id", "config", "value", "resource_id"}
        )
        revision = current["revision"] + int(fenced)
        await conn.execute(
            update(s.native_task_mcp_routes)
            .where(s.native_task_mcp_routes.c.task_id == tid)
            .values(revision=revision, **changed)
        )
        return revision

    @staticmethod
    def _next_check():
        return datetime.now(UTC) + timedelta(seconds=30)

    def _same_deployment(self, record):
        if (
            record["config"] != self.configuration()
            or record["config"]["admin_url"] != self.admin.admin_url
        ):
            raise HTTPException(409, "mcp_deployment_changed")

    def _value(self, record):
        value = record["value"]
        if "ciphertext" not in value:
            return value
        try:
            return json.loads(self.gateway.cipher.decrypt(value["ciphertext"].encode()))
        except (InvalidToken, ValueError) as error:
            raise HTTPException(503, "mcp_route_config_unavailable") from error

    async def prepare(self, conn, tid):
        """Called under the real Task lock after its owner and binding have been checked."""
        original = await self._get(conn, tid)
        if original:
            self._same_deployment(original)
            if original["desired"] == "active":
                return
            if (original["desired"], original["publication_state"]) != ("recovery", "retired"):
                raise HTTPException(409, "mcp_route_retirement_is_final")
        config = self.configuration()
        task = await row(conn, s.tasks, tid)
        if original and (
            self.stopping(task) or not task["ctf_control"].get("native_recovery_rebuilding")
        ):
            raise HTTPException(409, "mcp_route_retirement_is_final")
        resource = (
            (
                await conn.execute(
                    select(s.task_runner_resources).where(s.task_runner_resources.c.task_id == tid)
                )
            )
            .mappings()
            .first()
        )
        sandbox = None
        if resource and (resource["config"] or {}).get("runner_profile") == "node-sandbox-host-v1":
            host = resource["sandbox_host"]
            if not host.get("ready"):
                raise HTTPException(503, "mcp_sandbox_host_not_ready")
            sandbox = {
                "host_url": host["context"]["host_url"],
                "inbound_key": self.gateway.cipher.decrypt(
                    host["inbound_key_ciphertext"].encode()
                ).decode(),
            }
        value = business_mcp_route(
            tid,
            config["backend_url"],
            config["gateway_url"],
            config["jwks_file"],
            config["backend_token_file"],
            sandbox=sandbox,
            helper_foreground=bool(
                resource
                and (
                    resource["request"].get("helper_foreground")
                    or resource["request"].get("helper_background")
                )
            ),
            business_roles=task["ctf_control"].get("business_tools", {}).get("roles", {}),
        )
        source_hash = hashlib.sha256(config["source"].encode()).hexdigest()[:16]
        value["name"] = f"bbx-mcp-{source_hash}-{tid}"
        generation = resource["data"].get("resource_generation", 1) if resource else 1
        if generation > 1:
            value["name"] += f"-{generation}"
        values = {
            "resource_id": value["name"],
            "value": {
                "ciphertext": self.gateway.cipher.encrypt(json.dumps(value).encode()).decode()
            }
            if sandbox
            else value,
            "desired": "active",
            "publication_state": "pending",
            "next_check_at": datetime.now(UTC),
        }
        if original:
            await self._set(conn, tid, **values, operation_id=None, last_error=None)
        else:
            await conn.execute(
                insert(s.native_task_mcp_routes).values(
                    **values,
                    task_id=tid,
                    accounting_source_id=config["source"],
                    config=config,
                    revision=1,
                    created_at=datetime.now(UTC),
                )
            )

    async def retire_for_recovery(self, tid, action, recovery, budget, used, exceeded):
        bootstrap = recovery.bootstrap
        async with self.repo.engine.begin() as conn:
            await bootstrap._checked(conn, tid, action)
            route = await self._get(conn, tid)
            if not route or (route["desired"], route["publication_state"]) not in {
                ("active", "ready"),
                ("recovery", "recovery_retiring"),
                ("recovery", "retired"),
            }:
                return
        present = await self._preflight(route)
        claim = False
        async with self.repo.engine.begin() as conn:
            resource, task = await bootstrap._checked(conn, tid, action)
            incident = recovery._context(resource, task, action.proof["incident_id"])
            if recovery._independent_hold(task, incident) or not await recovery.matching_budget(
                conn, tid, budget, used, exceeded
            ):
                return
            current = await self._get(conn, tid)
            if not current or current["revision"] != route["revision"]:
                return
            if route["desired"] == "active":
                if not present:
                    return
                await self._set(
                    conn,
                    tid,
                    desired="recovery",
                    publication_state="recovery_retiring",
                    last_error=None,
                )
                claim = True
            elif not present and route["publication_state"] == "recovery_retiring":
                await self._set(conn, tid, publication_state="retired", last_error=None)
        if claim:
            # Only the caller that claimed retirement may send. Later callers verify absence.
            await self.admin.delete_route(route["resource_id"])
            await self.retire_for_recovery(tid, action, recovery, budget, used, exceeded)

    async def require_ready(self, conn, tid, verified_revision):
        record = await self._get(conn, tid)
        if record is None:
            raise HTTPException(503, "mcp_route_not_prepared")
        self._same_deployment(record)
        if (
            record["desired"] != "active"
            or record["publication_state"] != "ready"
            or verified_revision is None
            or record["revision"] != verified_revision
        ):
            raise HTTPException(503, "mcp_route_not_ready")
        return record["config"]["gateway_url"] + f"/api/tasks/{tid}/mcp"

    @staticmethod
    def _uses_gateway(value):
        gateways = value.get("gateways", [])
        if isinstance(gateways, str):
            gateways = [gateways]
        if not isinstance(gateways, list):
            raise HTTPException(409, "mcp_route_ownership_conflict")
        return any(
            isinstance(name, str) and name.split("/")[0] == "business-mcp" for name in gateways
        )

    async def _preflight(self, record):
        self._same_deployment(record)
        base = await self.admin.request("GET", "/api/config")
        if not isinstance(base, dict):
            raise HTTPException(503, "mcp_deployment_unavailable")
        gateways = base.get("gateways", {})
        own = gateways.get("business-mcp") if isinstance(gateways, dict) else None
        if not isinstance(own, dict) or not own.get("port") or own.get("listeners"):
            raise HTTPException(409, "mcp_static_route_conflict")
        if (
            base.get("binds")
            or base.get("mcp")
            or any(
                name != "business-mcp" and value.get("port") == own["port"]
                for name, value in gateways.items()
            )
        ):
            raise HTTPException(409, "mcp_static_route_conflict")
        # The dedicated gateway accepts only our DB-owned routes, including catchalls/prefixes.
        if any(self._uses_gateway(value) for value in base.get("routes", [])):
            raise HTTPException(409, "mcp_static_route_conflict")
        resources = await self.admin.resources(ROUTES_PATH)
        resource_ids = {record["resource_id"]} | {
            resource["id"] for resource in resources if self._uses_gateway(resource["value"])
        }
        async with self.repo.engine.connect() as conn:
            owned = {
                item["resource_id"]: self._value(item)
                for item in (
                    await conn.execute(
                        select(s.native_task_mcp_routes).where(
                            s.native_task_mcp_routes.c.accounting_source_id
                            == record["accounting_source_id"],
                            s.native_task_mcp_routes.c.config == record["config"],
                            s.native_task_mcp_routes.c.resource_id.in_(resource_ids),
                        )
                    )
                ).mappings()
            }
        for resource in resources:
            if (
                self._uses_gateway(resource["value"])
                and owned.get(resource["id"]) != resource["value"]
            ):
                raise HTTPException(409, "mcp_route_ownership_conflict")
        matches = [resource for resource in resources if resource["id"] == record["resource_id"]]
        if len(matches) > 1 or (matches and matches[0]["value"] != self._value(record)):
            raise HTTPException(409, "mcp_route_ownership_conflict")
        # DB resource reads describe desired config; only this live store proves reload.
        try:
            async with asyncio.timeout(2):
                while True:
                    dump = await self.admin.request("GET", "/config_dump")
                    if self._active(dump, record, owned) is bool(matches):
                        break
                    await asyncio.sleep(0.05)
        except TimeoutError as error:
            raise HTTPException(503, "mcp_dataplane_pending") from error
        return bool(matches)

    @staticmethod
    def _active(dump, record, owned):
        if not isinstance(dump, dict) or not isinstance(dump.get("binds"), list):
            raise HTTPException(503, "mcp_dataplane_unavailable")
        selected = None
        for bind in dump["binds"]:
            for listener_key, listener in bind["listeners"].items():
                for key, active in listener.get("routes", {}).items():
                    name = active.get("name")
                    if listener_key == "gateway/business-mcp":
                        if name not in owned or active["matches"] != owned[name]["matches"]:
                            raise HTTPException(409, "mcp_active_route_conflict")
                    if name == record["resource_id"]:
                        if (
                            selected is not None
                            or listener_key != "gateway/business-mcp"
                            or key != f"gateway/business-mcp/default/{name}"
                        ):
                            raise HTTPException(409, "mcp_active_route_conflict")
                        selected = active
        if selected is None:
            return False
        expected_ref = "/" + selected["key"] + "/backend0"
        if (
            len(selected["backends"]) != 1
            or selected["backends"][0].get("weight") != 1
            or selected["backends"][0].get("backend") != expected_ref
        ):
            raise HTTPException(409, "mcp_active_route_conflict")
        backends = {
            value.get("namespace", "") + "/" + value["name"]: (kind, value)
            for wrapped in dump.get("backends", [])
            for kind, value in wrapped.get("backend", {}).items()
            if kind in {"mcp", "host"}
        }
        mcp_backend = backends.get(expected_ref)
        if mcp_backend is None:
            return None
        if mcp_backend[0] != "mcp":
            raise HTTPException(409, "mcp_active_route_conflict")
        target = mcp_backend[1]["target"]
        targets = target["targets"]
        expected_targets = owned[record["resource_id"]]["backends"][0]["mcp"]["targets"]
        if target["prefixMode"] != "always" or [t["name"] for t in targets] != [
            t["name"] for t in expected_targets
        ]:
            raise HTTPException(409, "mcp_active_route_conflict")
        for item, expected in zip(targets, expected_targets, strict=True):
            upstream = item["mcp"]
            expected_url = urlsplit(expected["mcp"]["host"])
            if upstream["path"] != expected_url.path or item.get("condition") != expected.get(
                "condition"
            ):
                raise HTTPException(409, "mcp_active_route_conflict")
            host_backend = backends.get(upstream["backend"]["backend"])
            if host_backend is None:
                return None
            expected_host = expected_url.hostname
            if expected_host and ":" in expected_host:
                expected_host = "[" + expected_host + "]"
            expected_port = expected_url.port or (443 if expected_url.scheme == "https" else 80)
            if (
                host_backend[0] != "host"
                or host_backend[1]["target"] != f"{expected_host}:{expected_port}"
            ):
                raise HTTPException(409, "mcp_active_route_conflict")
        return True

    async def _remember_error(self, tid, code, *, uncertain=False):
        async with self.repo.engine.begin() as conn:
            try:
                await self.repo.lock(conn, tid)
            except KeyError:
                return
            record = await self._get(conn, tid)
            if record is not None:
                state = record["publication_state"]
                if uncertain and state == "publishing":
                    state = "unknown"
                await self._set(
                    conn,
                    tid,
                    last_error=code,
                    publication_state=state,
                    next_check_at=self._next_check(),
                )

    async def reconcile(self, tid, *, read_only=False):
        try:
            return await self._reconcile(tid, read_only=read_only)
        except HTTPException as error:
            code = (
                error.detail
                if isinstance(error.detail, str) and error.detail.startswith("mcp_")
                else "mcp_management_unavailable"
            )
            await self._remember_error(tid, code)
            raise
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            await self._remember_error(tid, "mcp_management_unavailable")
            raise HTTPException(503, "mcp_management_unavailable") from error

    async def _reconcile(self, tid, *, read_only):
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            record = await self._get(conn, tid)
            if record is None:
                return
            task = await row(conn, s.tasks, tid)
            if self.stopping(task) and record["desired"] != "retired":
                await self._set(conn, tid, desired="retired")
                record = await self._get(conn, tid)
                assert record is not None
            if record["desired"] == "recovery":
                return
        present = await self._preflight(record)
        operation_id = None
        async with self.repo.engine.begin() as conn:
            await self.repo.lock(conn, tid)
            current = await self._get(conn, tid)
            task = await row(conn, s.tasks, tid)
            assert current is not None
            self._same_deployment(current)
            if current["revision"] != record["revision"]:
                return
            if self.stopping(task):
                await self._set(conn, tid, desired="retired")
                current["desired"] = "retired"
            state = current["publication_state"]
            if current["desired"] == "retired":
                if not present:
                    if state in {"publishing", "unknown"}:
                        await self._set(
                            conn,
                            tid,
                            publication_state="unknown",
                            last_error="mcp_original_publication_unconfirmed",
                            next_check_at=self._next_check(),
                        )
                        return
                    await self._set(conn, tid, publication_state="retired", last_error=None)
                    return
                if read_only:
                    await self._set(
                        conn,
                        tid,
                        last_error="mcp_retirement_pending",
                        next_check_at=datetime.now(UTC),
                    )
                    return
                action = "delete"
                await self._set(conn, tid, publication_state="retiring", last_error=None)
            elif present:
                if state in {"retiring", "retired"}:
                    raise HTTPException(409, "mcp_route_retirement_is_final")
                return await self._set(
                    conn,
                    tid,
                    publication_state="ready",
                    last_error=None,
                    next_check_at=self._next_check(),
                )
            elif state in {"publishing", "unknown"}:
                await self._set(
                    conn,
                    tid,
                    publication_state="unknown",
                    last_error="mcp_original_publication_unconfirmed",
                    next_check_at=self._next_check(),
                )
                return
            elif read_only:
                await self._set(
                    conn,
                    tid,
                    last_error="mcp_publication_readback_pending",
                    next_check_at=self._next_check(),
                )
                return
            else:
                action = "put"
                # One durable claim; concurrent callers can only read this operation back.
                operation_id = uuid4().hex
                await self._set(
                    conn,
                    tid,
                    publication_state="publishing",
                    operation_id=operation_id,
                    last_error=None,
                )
        try:
            if action == "put":
                await self.admin.put_route(self._value(record))
                async with self.repo.engine.begin() as conn:
                    await self.repo.lock(conn, tid)
                    current = await self._get(conn, tid)
                    assert current is not None
                    if current["operation_id"] == operation_id and current["publication_state"] in {
                        "publishing",
                        "unknown",
                    }:
                        await self._set(conn, tid, publication_state="published")
            else:
                await self.admin.delete_route(record["resource_id"])
        except BaseException:
            await asyncio.shield(
                self._remember_error(tid, "mcp_route_write_unconfirmed", uncertain=action == "put")
            )
            raise
        return await self.reconcile(tid, read_only=True)

    async def retire(self, tid):
        async with self.repo.engine.begin() as conn:
            if not (await conn.execute(select(s.tasks.c.id).where(s.tasks.c.id == tid))).first():
                return
            await self.repo.lock(conn, tid)
            record = await self._get(conn, tid)
            if record is None:
                return
            if record["desired"] == "retired" and record["publication_state"] == "retired":
                return
            if not self.stopping(await row(conn, s.tasks, tid)):
                raise HTTPException(409, "mcp_route_retirement_requires_task_stop")
            await self._set(conn, tid, desired="retired")
        await self.reconcile(tid)
        async with self.repo.engine.connect() as conn:
            record = await self._get(conn, tid)
            if record is not None and record["publication_state"] != "retired":
                raise HTTPException(409, "mcp_route_retirement_unconfirmed")

    async def view(self, tid):
        async with self.repo.engine.connect() as conn:
            record = await self._get(conn, tid)
        if record is None:
            return {"task_id": str(tid), "publication_state": "not_prepared"}
        return {
            "task_id": str(tid),
            "accounting_source_id": record["accounting_source_id"],
            "resource_id": record["resource_id"],
            "desired": record["desired"],
            "revision": record["revision"],
            "publication_state": record["publication_state"],
            "last_error": record["last_error"],
            "next_check_at": record["next_check_at"],
            "mcp_gateway_url": record["config"]["gateway_url"] + f"/api/tasks/{tid}/mcp",
        }

    async def run(self):
        if not self.settings.native_mcp_gateway_url:
            return
        after = None
        recovering = True
        while True:
            try:
                async with self.repo.engine.connect() as conn:
                    query = (
                        select(s.native_task_mcp_routes.c.task_id)
                        .join(s.tasks, s.tasks.c.id == s.native_task_mcp_routes.c.task_id)
                        .where(s.native_task_mcp_routes.c.publication_state != "retired")
                    )
                    if not recovering:
                        query = query.where(
                            or_(
                                s.native_task_mcp_routes.c.next_check_at <= datetime.now(UTC),
                                (s.native_task_mcp_routes.c.desired == "active")
                                & or_(
                                    s.tasks.c.deleting.is_(True),
                                    s.tasks.c.status.in_(
                                        ["finished", "failed", "stopped", "closing"]
                                    ),
                                    s.tasks.c.ctf_control["phase"].astext.in_(
                                        ["closing", "finished", "failed", "stopped"]
                                    ),
                                ),
                            )
                        )
                    if after is not None:
                        query = query.where(s.native_task_mcp_routes.c.task_id > after)
                    tids = (
                        (
                            await conn.execute(
                                query.order_by(s.native_task_mcp_routes.c.task_id).limit(20)
                            )
                        )
                        .scalars()
                        .all()
                    )
                after = tids[-1] if tids else None
                if not tids:
                    recovering = False
                for tid in tids:
                    try:
                        await self.reconcile(tid)
                    except HTTPException:
                        pass
                    except (KeyError, ValueError, TypeError):
                        await self._remember_error(tid, "mcp_management_unavailable")
            except SQLAlchemyError:
                logging.getLogger(__name__).warning("Native MCP route database is unavailable")
            await asyncio.sleep(1)
