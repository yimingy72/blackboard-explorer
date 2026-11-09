"""Forward the original authenticated AgentGateway UI; fence only its model writes."""

from __future__ import annotations

import asyncio
import json
import os
import re
import ssl
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from bbx_contracts.gateway_models import GatewayMaintenanceAck, GatewayMaintenanceFence
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

MUTATION = re.compile(
    rb"/api/config/resources/(llm\.model|llm\.provider|llm\.virtualModel|modelCatalog)"
    rb"(?:/[A-Za-z0-9][A-Za-z0-9_.-]{0,199})?"
)
READ_POST = {
    "/api/cel",
    "/api/logs/search",
    "/api/logs/get",
    "/api/logs/analytics/summary",
}
HEADERS = {"authorization", "accept", "content-type", "if-none-match", "origin"}
REPLY_HEADERS = {
    "content-type",
    "cache-control",
    "etag",
    "last-modified",
    "location",
    "www-authenticate",
}
LIMIT = 16 * 1024 * 1024


def origin(raw: str) -> str:
    url = urlsplit(raw)
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.path not in {"", "/"}
        or url.query
        or url.fragment
    ):
        raise ValueError("Invalid native administrator origin")
    return raw.rstrip("/")


class NativeRestart:
    """Read back a new process that loaded the same original native store."""

    def __init__(
        self, namespace: str, name: str, uid: str, pvc: str, pvc_uid: str, token_file: Path
    ):
        for part in (namespace, name, pvc):
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", part):
                raise ValueError("Invalid owned native object")
        UUID(uid)
        UUID(pvc_uid)
        self.url = (
            f"https://kubernetes.default.svc/apis/apps/v1/namespaces/{namespace}/deployments/{name}"
        )
        self.uid, self.pvc, self.token_file = uid, pvc, token_file
        self.pvc_uid = pvc_uid
        self.pvc_url = f"https://kubernetes.default.svc/api/v1/namespaces/{namespace}/persistentvolumeclaims/{pvc}"

    async def _volume(self, http, headers):
        response = await http.get(self.pvc_url, headers=headers)
        response.raise_for_status()
        volume = response.json()
        if volume["metadata"]["uid"] != self.pvc_uid or volume["status"]["phase"] != "Bound":
            raise ValueError("The original native volume changed")

    def _owned(self, value: dict) -> dict:
        metadata, spec = value["metadata"], value["spec"]
        template = spec["template"]
        claims = [
            item["persistentVolumeClaim"]["claimName"]
            for item in template["spec"].get("volumes", [])
            if "persistentVolumeClaim" in item
        ]
        if (
            metadata["uid"] != self.uid
            or spec.get("replicas", 1) != 1
            or spec["strategy"]["type"] != "Recreate"
            or claims != [self.pvc]
        ):
            raise ValueError("The original single native store is unavailable")
        return metadata

    async def __call__(self, http: httpx.AsyncClient, request_id: UUID) -> None:
        headers = {"Authorization": "Bearer " + self.token_file.read_text().strip()}
        await self._volume(http, headers)
        response = await http.get(self.url, headers=headers)
        response.raise_for_status()
        original = response.json()
        metadata = self._owned(original)
        annotation = "bbx.dev/admin-ui-reload"
        annotations = original["spec"]["template"].get("metadata", {}).get("annotations", {})
        desired = {**annotations, annotation: str(request_id)}
        patch = [
            {"op": "test", "path": "/metadata/uid", "value": self.uid},
            {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": metadata["resourceVersion"],
            },
            {"op": "add", "path": "/spec/template/metadata/annotations", "value": desired},
        ]
        response = await http.patch(
            self.url,
            json=patch,
            headers={**headers, "Content-Type": "application/json-patch+json"},
        )
        response.raise_for_status()
        generation = self._owned(response.json())["generation"]
        if generation <= metadata["generation"]:
            raise ValueError("No new native process was scheduled")
        async with asyncio.timeout(100):
            while True:
                response = await http.get(self.url, headers=headers)
                response.raise_for_status()
                deployed = response.json()
                current = self._owned(deployed)
                template = deployed["spec"]["template"]["metadata"]
                status = deployed.get("status", {})
                if current["generation"] != generation or template.get("annotations", {}).get(
                    annotation
                ) != str(request_id):
                    raise ValueError("The original native rollout was replaced")
                if status.get("observedGeneration") == generation and all(
                    status.get(name) == 1
                    for name in (
                        "replicas",
                        "updatedReplicas",
                        "readyReplicas",
                        "availableReplicas",
                    )
                ):
                    await self._volume(http, headers)
                    return
                await asyncio.sleep(0.5)


class AdminUI:
    def __init__(self, upstream, public, peers, key, restart):
        self.upstream = origin(upstream)
        self.public = origin(public)
        self.peers = {name: origin(address) for name, address in peers.items()}
        if len(self.peers) < 2 or len(set(self.peers.values())) != len(self.peers) or len(key) < 32:
            raise ValueError("Every original consumer must be configured")
        self.key = key
        self.restart = restart
        self.lock = asyncio.Lock()

    async def fence(self, http, request_id, action):
        async def one(site, address):
            body = GatewayMaintenanceFence(request_id=request_id, site_id=site, action=action)
            response = await http.post(
                address + "/api/platform/gateway-models/maintenance/fence",
                json=body.model_dump(mode="json"),
                headers={"x-bbx-gateway-maintenance": self.key},
            )
            response.raise_for_status()
            ack = GatewayMaintenanceAck.model_validate(response.json())
            return (ack.request_id, ack.site_id, ack.state) == (
                request_id,
                site,
                "idle" if action == "prepare" else "released",
            )

        results = await asyncio.gather(
            *(one(site, address) for site, address in self.peers.items()), return_exceptions=True
        )
        return all(result is True for result in results)

    async def forward(self, http, method, path, headers, *, query="", body=None):
        headers = {**headers}
        if "origin" in headers:
            # Browser origin was checked at our public boundary. The native
            # same-origin check compares the private listener's own authority.
            headers["origin"] = self.upstream
        async with http.stream(
            method,
            self.upstream + path + ("?" + query if query else ""),
            headers=headers,
            content=body,
        ) as response:
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > LIMIT:
                    raise ValueError("Native response exceeded its bound")
            return httpx.Response(
                response.status_code,
                headers={
                    name: value for name, value in response.headers.items() if name in REPLY_HEADERS
                },
                content=bytes(data),
            )

    @staticmethod
    def reply(response: httpx.Response) -> Response:
        return Response(response.content, response.status_code, dict(response.headers))

    @staticmethod
    def persisted(method, path, native, readback):
        resources = {item["id"]: item for item in readback.json()["resources"]}
        if method == "DELETE":
            return path.rsplit("/", 1)[-1] not in resources
        written = native.json()["resources"]
        return bool(written) and all(
            item["id"] in resources
            and all(
                resources[item["id"]].get(k) == item[k] for k in ("revision", "value", "createdAt")
            )
            for item in written
        )

    @staticmethod
    def unavailable(request_id: UUID, *, idle=False) -> Response:
        message = "Model writes require both sites to be idle."
        if not idle:
            message = "The original native write requires maintenance readback."
        return JSONResponse(
            {"message": message},
            status_code=409 if idle else 503,
            headers={"x-bbx-maintenance-id": str(request_id), "cache-control": "no-store"},
        )

    async def mutate(self, request, path, kind, headers, body):
        http = request.app.state.http
        async with self.lock:
            # Authentication runs inside the original AgentGateway UI before
            # touching either admission fence.
            auth = await self.forward(http, "GET", "/api/runtime", headers)
            if auth.status_code != 200:
                return self.reply(auth)
            request_id = uuid4()
            if not await self.fence(http, request_id, "prepare"):
                released = await self.fence(http, request_id, "release")
                return self.unavailable(request_id, idle=released)
            read_path = "/api/config/resources/" + kind
            try:
                before = await self.forward(http, "GET", read_path, headers)
                if before.status_code != 200:
                    released = await self.fence(http, request_id, "release")
                    return self.unavailable(request_id, idle=released)
            except (httpx.HTTPError, ValueError):
                released = await self.fence(http, request_id, "release")
                return self.unavailable(request_id, idle=released)

            # Exactly one native effect; neither the body nor its credentials
            # leave this request or enter the durable maintenance record.
            try:
                native = await self.forward(http, request.method, path, headers, body=body)
                if native.is_success:
                    await self.restart(request.app.state.kube, request_id)
                    active = await self.forward(http, "GET", "/api/runtime", headers)
                    readback = await self.forward(http, "GET", read_path, headers)
                    if (
                        active.status_code != 200
                        or active.json()["configReload"]["synchronized"] is not True
                        or readback.status_code != 200
                        or not self.persisted(request.method, path, native, readback)
                    ):
                        return self.unavailable(request_id)
                elif native.status_code in {400, 401, 403, 404, 409, 422}:
                    after = await self.forward(http, "GET", read_path, headers)
                    if after.status_code != 200 or after.json() != before.json():
                        return self.unavailable(request_id)
                else:
                    return self.unavailable(request_id)
            except (httpx.HTTPError, KeyError, TypeError, ValueError, TimeoutError):
                return self.unavailable(request_id)
            if not await self.fence(http, request_id, "release"):
                return self.unavailable(request_id)
            return self.reply(native)

    async def handle(self, request: Request) -> Response:
        raw = request.scope.get("raw_path", b"")
        if not raw.startswith(b"/") or raw.startswith(b"//") or b"\\" in raw:
            return Response(status_code=404)
        path = raw.decode("ascii", errors="replace")
        headers = {name: value for name, value in request.headers.items() if name in HEADERS}
        match = MUTATION.fullmatch(raw)
        writing = request.method in {"PUT", "DELETE"} and match is not None
        if writing and request.method == "DELETE" and raw.count(b"/") != 5:
            return Response(status_code=405)
        reading = request.method in {"GET", "HEAD"} or (
            request.method == "POST" and path in READ_POST
        )
        if not (writing or reading):
            return Response(status_code=405)
        if request.method not in {"GET", "HEAD"} and request.headers.get("origin") != self.public:
            return Response(status_code=403)
        body = None
        if writing or request.method == "POST":
            data = bytearray()
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > LIMIT:
                    return Response(status_code=413)
            body = bytes(data)
        if writing:
            if request.url.query:
                return Response(status_code=403)
            if request.method == "PUT" and headers.get("content-type", "").split(";")[0] != (
                "application/json"
            ):
                return Response(status_code=415)
            assert match is not None
            return await self.mutate(request, path, match[1].decode(), headers, body)
        try:
            native = await self.forward(
                request.app.state.http,
                request.method,
                path,
                headers,
                query=request.url.query,
                body=body,
            )
        except (httpx.HTTPError, ValueError):
            return Response(status_code=503)
        return self.reply(native)


def build() -> Starlette:
    service_account = Path("/var/run/secrets/kubernetes.io/serviceaccount")
    proxy = AdminUI(
        os.environ["BBX_NATIVE_ADMIN_UI_URL"],
        os.environ["BBX_NATIVE_ADMIN_PUBLIC_ORIGIN"],
        json.loads(os.environ["GATEWAY_MAINTENANCE_PEERS"]),
        os.environ["GATEWAY_MAINTENANCE_KEY"],
        NativeRestart(
            os.environ["BBX_NATIVE_NAMESPACE"],
            os.environ["BBX_NATIVE_DEPLOYMENT"],
            os.environ["BBX_NATIVE_DEPLOYMENT_UID"],
            os.environ["BBX_NATIVE_PVC"],
            os.environ["BBX_NATIVE_PVC_UID"],
            service_account / "token",
        ),
    )

    @asynccontextmanager
    async def lifespan(app):
        ssl_ui = ssl.create_default_context(cafile=os.environ["BBX_NATIVE_UI_CA_FILE"])
        ssl_kube = ssl.create_default_context(cafile=str(service_account / "ca.crt"))
        async with (
            httpx.AsyncClient(
                verify=ssl_ui,
                trust_env=False,
                follow_redirects=False,
                timeout=7,
                limits=httpx.Limits(max_keepalive_connections=0),
            ) as http,
            httpx.AsyncClient(
                verify=ssl_kube, trust_env=False, follow_redirects=False, timeout=5
            ) as kube,
        ):
            app.state.http, app.state.kube = http, kube
            yield

    return Starlette(
        routes=[Route("/{path:path}", proxy.handle, methods=["GET", "POST", "PUT", "DELETE"])],
        lifespan=lifespan,
    )
