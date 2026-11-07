"""One retained Task's user-authorized resource retirement, not a general GC loop."""

from bbx_contracts.runner_bootstrap import retirement_manifest_digest
from fastapi import HTTPException
from sqlalchemy import select

from bbx_blackboard.conversations import require_settled_progress
from bbx_blackboard.runner_gateway import cancel_admitted
from bbx_blackboard.store import schema as s


class NativeResourceRetirement:
    def __init__(self, bootstrap):
        self.bootstrap = bootstrap
        self.gateway, self.repo = bootstrap.gateway, bootstrap.repo

    async def _ready(self, conn, tid, record, task, *, native_retired):
        retirement = record["data"].get("retirement", {})
        if (
            not task["deleting"]
            or task["status"] not in {"stopped", "failed", "finished"}
            or record["state"] != "stopped"
            or record["data"].get("exit_confirmed") is not True
            or retirement.get("policy") != "user_delete"
            or not retirement.get("operation_id")
        ):
            raise HTTPException(
                409, "Resource retirement requires terminal execution and explicit deletion"
            )
        authority = retirement.get("authority", {})
        if set(authority) != {"pod_uid", "node_uid"} or not all(authority.values()):
            raise HTTPException(
                409, "Resource retirement requires a frozen single-apiserver authority"
            )
        await cancel_admitted(conn, tid)
        if (
            await conn.execute(
                select(s.runner_calls.c.id)
                .where(
                    s.runner_calls.c.task_id == tid,
                    s.runner_calls.c.state.in_(["dispatched", "unknown"]),
                )
                .limit(1)
            )
        ).first():
            raise HTTPException(409, "Original Runner calls still require reconciliation")
        if (
            await conn.execute(
                select(s.native_request_costs.c.native_log_id)
                .where(
                    s.native_request_costs.c.task_id == tid,
                    s.native_request_costs.c.state != "known",
                )
                .limit(1)
            )
        ).first():
            raise HTTPException(409, "Original native fees still require reconciliation")
        if (
            await conn.execute(
                select(s.ctf_turns.c.id)
                .where(s.ctf_turns.c.task_id == tid, s.ctf_turns.c.status == "running")
                .limit(1)
            )
        ).first() or task["ctf_control"].get("unresolved_checkpoints"):
            raise HTTPException(409, "Original execution closure is not confirmed")
        await require_settled_progress(conn, tid)
        if native_retired:
            budget = (
                await conn.execute(
                    select(s.native_task_budgets.c.publication_state).where(
                        s.native_task_budgets.c.task_id == tid
                    )
                )
            ).scalar_one_or_none()
            route = (
                await conn.execute(
                    select(s.native_task_mcp_routes.c.publication_state).where(
                        s.native_task_mcp_routes.c.task_id == tid
                    )
                )
            ).scalar_one_or_none()
            if budget not in {None, "retired"} or route not in {None, "retired"}:
                raise HTTPException(
                    409, "Original native credentials must retire before retained volumes"
                )
        return retirement

    @staticmethod
    def _bindings(manifest):
        result = {}
        for item in manifest.get("resources", []):
            if not isinstance(item, dict) or not item.get("path") or not item.get("uid"):
                raise HTTPException(422, "Retirement resource identity is incomplete")
            if set(item) - {"path", "uid", "kind", "name", "namespace_gc", "absent"}:
                raise HTTPException(
                    422, "Retirement resources require a narrow identity projection"
                )
            key = "resource:" + item["path"]
            if key in result:
                raise HTTPException(422, "Duplicate retirement resource pointer")
            result[key] = item["uid"]
        for volume in manifest.get("volumes", []):
            if not isinstance(volume, dict) or not volume.get("claim_uid"):
                raise HTTPException(422, "Retirement PV identity is incomplete")
            unbound = volume.get("unbound") is True
            if unbound or not volume.get("name") or not volume.get("uid"):
                raise HTTPException(422, "Retirement PV binding is ambiguous")
            key = "claim:" + volume["claim_uid"]
            if key in result:
                raise HTTPException(422, "Duplicate retirement claim pointer")
            result[key] = (volume.get("name"), volume.get("uid"), unbound)
        pv_uids = [volume["uid"] for volume in manifest.get("volumes", [])]
        if len(pv_uids) != len(set(pv_uids)):
            raise HTTPException(422, "A retirement PV cannot belong to multiple claims")
        for key in ("policy", "binding"):
            item = manifest.get(key, {})
            if item.get("uid"):
                result[key + ":" + item["path"]] = item["uid"]
        return result

    def _manifest(self, record, retirement, incoming):
        if not isinstance(incoming, dict):
            raise HTTPException(422, "A retirement manifest is required")
        expected = {
            "task_id": str(record["task_id"]),
            "operation_id": record["operation_id"],
            "retirement_id": retirement["operation_id"],
            "source_id": record["config"]["source_id"],
            "cluster_uid": record["config"]["cluster_uid"],
            "authority": retirement["authority"],
            "namespace": {
                "name": record["namespace"],
                "uid": record["observation"].get("namespace_uid"),
            },
        }
        if any(incoming.get(key) != value for key, value in expected.items()):
            raise HTTPException(409, "Frozen retirement resource source changed")
        if not expected["namespace"]["uid"]:
            raise HTTPException(409, "Original namespace identity is unknown")
        before, after = self._bindings(retirement.get("manifest", {})), self._bindings(incoming)
        if any(after.get(key) != value for key, value in before.items()):
            raise HTTPException(409, "Original retirement UID cannot be replaced or dropped")
        prefix = "/namespaces/" + record["namespace"] + "/"
        for item in incoming.get("resources", []):
            if prefix not in item["path"]:
                raise HTTPException(409, "Retirement resource exceeds its original namespace")
        for key, resource in (
            ("journal_pvc", "persistentvolumeclaims"),
            ("workspace_pvc", "persistentvolumeclaims"),
            ("execd_secret", "secrets"),
            ("runner_secret", "secrets"),
        ):
            ref = record["observation"].get(key)
            if (
                ref
                and after.get("resource:/api/v1" + prefix + resource + "/" + ref["name"])
                != ref["uid"]
            ):
                raise HTTPException(409, "Original retained resource is missing from its manifest")
        prior_manifest = retirement.get("manifest", {})
        for key in ("fence", "topology"):
            if key in prior_manifest and incoming.get(key) != prior_manifest[key]:
                raise HTTPException(409, "Frozen retirement fence specification changed")
        for key in ("policy", "binding"):
            prior = prior_manifest.get(key, {})
            if prior.get("path") and incoming.get(key, {}).get("path") != prior["path"]:
                raise HTTPException(409, "Original retirement policy pointer changed")
        claims = {
            ref["uid"]: ref["name"]
            for key in ("journal_pvc", "workspace_pvc")
            if (ref := record["observation"].get(key))
        }
        if len(claims) != 2:
            raise HTTPException(409, "Both original retained PVC identities must be known")
        for volume in incoming.get("volumes", []):
            if (
                volume.get("claim_uid") not in claims
                or volume.get("claim_name") != claims[volume["claim_uid"]]
                or volume.get("claim_namespace") != record["namespace"]
                or volume.get("storage_class") != record["config"]["storage_class"]
                or volume.get("reclaim_policy") != "Delete"
            ):
                raise HTTPException(409, "Retirement PV claim or policy changed")
        if {volume["claim_uid"] for volume in incoming.get("volumes", [])} != set(claims):
            raise HTTPException(409, "Every original retained PVC requires an explicit PV binding")
        return incoming

    @staticmethod
    def _complete(manifest):
        return (
            manifest.get("retired") is True
            and manifest.get("namespace_delete_started") is True
            and manifest.get("namespace_absent") is True
            and manifest.get("fence_absent") is True
            and all(manifest.get(key, {}).get("uid") for key in ("policy", "binding"))
            and all(item.get("absent") is True for item in manifest.get("resources", []))
            and all(item.get("absent") is True for item in manifest.get("volumes", []))
        )

    async def act(self, tid, body):
        async with self.repo.engine.begin() as conn:
            record, task = await self.bootstrap._checked(conn, tid, body)
            retirement = await self._ready(
                conn, tid, record, task, native_retired=body.action != "retire_prepare"
            )
            if retirement.get("state") == "retired":
                return self.bootstrap._public(record)
        if body.action == "retire_prepare":
            await self.gateway.native.retire(tid, explicit_delete=True)
            await self.gateway.routes.retire(tid)
            async with self.repo.engine.begin() as conn:
                record, task = await self.bootstrap._checked(conn, tid, body)
                retirement = await self._ready(conn, tid, record, task, native_retired=True)
                await self.bootstrap._set(
                    conn,
                    tid,
                    data={**record["data"], "retirement": {**retirement, "state": "retiring"}},
                )
                return self.bootstrap._public(await self.bootstrap._get(conn, tid))
        async with self.repo.engine.begin() as conn:
            record, task = await self.bootstrap._checked(conn, tid, body)
            retirement = await self._ready(conn, tid, record, task, native_retired=True)
            if body.action == "retire_plan":
                manifest = self._manifest(record, retirement, body.proof.get("manifest"))
                await self.bootstrap._set(
                    conn,
                    tid,
                    data={
                        **record["data"],
                        "retirement": {**retirement, "manifest": manifest, "state": "retiring"},
                    },
                )
            elif body.action in {"retire_admit", "retire_complete"}:
                manifest = retirement.get("manifest", {})
                digest = retirement_manifest_digest(manifest)
                if not manifest or body.proof.get("manifest_digest") != digest:
                    raise HTTPException(409, "Retirement manifest confirmation changed")
                if body.action == "retire_admit":
                    return {"admitted": True, "manifest_digest": digest}
                self._manifest(record, retirement, manifest)
                if body.proof.get("retired") is not True or not self._complete(manifest):
                    raise HTTPException(409, "Original native resources are not confirmed retired")
                await self.bootstrap._set(
                    conn,
                    tid,
                    data={
                        **record["data"],
                        "retirement": {
                            **retirement,
                            "state": "retired",
                            "manifest_digest": digest,
                            "last_error": None,
                        },
                    },
                )
            else:
                raise HTTPException(422, "Unsupported resource retirement action")
            return self.bootstrap._public(await self.bootstrap._get(conn, tid))
