"""Forward the original authenticated AgentGateway UI; fence only its model writes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
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
READBACK_SECONDS = 20
READBACK_INTERVAL = 0.5
TRANSIENT_READ_STATUS = {502, 503, 504}
RETRY_READBACK = {
    "read_transport",
    "runtime_unavailable",
    "runtime_unsynchronized",
    "resource_unavailable",
    "resource_not_visible",
}
RESOURCE_FIELDS = ("revision", "value", "createdAt")
LOGGER = logging.getLogger(__name__)

UI_SECURITY_HEADERS = {
    "content-security-policy",
    "content-security-policy-report-only",
    "cross-origin-embedder-policy",
    "cross-origin-opener-policy",
    "cross-origin-resource-policy",
    "permissions-policy",
    "referrer-policy",
    "strict-transport-security",
    "x-content-type-options",
    "x-frame-options",
}
UI_PATH = re.compile(r"/ui(?:/[A-Za-z0-9][A-Za-z0-9_.-]*)*/?")
UPSTREAM_UI_COMMIT = "26f5aebe99a36a037fe54f8080a608aca5547e15"


class LocalizedUI:
    """One verified build of the native UI; no filesystem lookup on a request."""

    def __init__(self, bundle: Path):
        if bundle.is_symlink():
            raise ValueError("Invalid localized native UI bundle")
        bundle = bundle.resolve(strict=True)
        manifest_file = bundle / "assets-manifest.json"
        if (
            manifest_file.resolve(strict=True) != manifest_file
            or manifest_file.stat().st_size > LIMIT
        ):
            raise ValueError("Invalid localized native UI manifest")
        manifest = json.loads(manifest_file.read_text())
        root = bundle / "dist"
        if (
            manifest["upstream_commit"] != UPSTREAM_UI_COMMIT
            or root.resolve(strict=True) != root
            or not root.is_dir()
            or len(manifest["files"]) > 512
        ):
            raise ValueError("Invalid localized native UI bundle")
        self.assets: dict[str, tuple[bytes, str]] = {}
        total = 0
        for name, expected in manifest["files"].items():
            if not UI_PATH.fullmatch("/ui/" + name) or name.endswith("/"):
                raise ValueError("Invalid localized native UI asset path")
            path = root / name
            if (
                path.resolve(strict=True) != path
                or not path.is_file()
                or path.stat().st_size > LIMIT
            ):
                raise ValueError("Unexpected localized native UI file")
            data = path.read_bytes()
            total += len(data)
            if total > 64 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != expected:
                raise ValueError("Localized native UI asset integrity check failed")
            media = mimetypes.guess_type(name)[0] or "application/octet-stream"
            self.assets["/ui/" + name] = (data, media)
        self.index = self.assets["/ui/index.html"]
        self.pages = frozenset(manifest["spa_paths"])
        if "/ui" not in self.pages or not all(
            UI_PATH.fullmatch(page) and "." not in page and not page.endswith("/")
            for page in self.pages
        ):
            raise ValueError("Invalid localized native UI pages")

    def asset(self, path: str) -> tuple[bytes, str] | None:
        if path.rstrip("/") in self.pages:
            return self.index
        return self.assets.get(path)


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
    def __init__(
        self, upstream, public, peers, key, restart, *, localized: LocalizedUI | None = None
    ):
        self.upstream = origin(upstream)
        self.public = origin(public)
        self.peers = {name: origin(address) for name, address in peers.items()}
        if len(self.peers) < 2 or len(set(self.peers.values())) != len(self.peers) or len(key) < 32:
            raise ValueError("Every original consumer must be configured")
        self.key = key
        self.restart = restart
        self.lock = asyncio.Lock()
        self.localized = localized

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

    async def forward(self, http, method, path, headers, *, query="", body=None, ui_security=False):
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
                    name: value
                    for name, value in response.headers.items()
                    if name in REPLY_HEADERS or (ui_security and name in UI_SECURITY_HEADERS)
                },
                content=bytes(data),
            )

    @staticmethod
    def reply(response: httpx.Response) -> Response:
        return Response(response.content, response.status_code, dict(response.headers))

    @staticmethod
    def resources(response):
        items = response.json()["resources"]
        if not isinstance(items, list):
            raise ValueError("Invalid native resource collection")
        result = {}
        for item in items:
            key = item["id"]
            if (
                not isinstance(key, str)
                or not key
                or key in result
                or not isinstance(item["value"], dict)
            ):
                raise ValueError("Invalid native resource")
            result[key] = item
        return result

    @staticmethod
    def resource_identity(item):
        revision, value, created = (item[field] for field in RESOURCE_FIELDS)
        if type(revision) is not int or revision < 1 or not isinstance(created, str) or not created:
            raise ValueError("Missing stored resource identity")
        return revision, value, created

    @classmethod
    def write_receipt(cls, native):
        resources = cls.resources(native)
        if not resources:
            raise ValueError("No original native write receipt")
        return {key: cls.resource_identity(item) for key, item in resources.items()}

    async def readback(self, http, read_path, headers, expected, deleted):
        active = await self.forward(http, "GET", "/api/runtime", headers)
        if active.status_code in TRANSIENT_READ_STATUS:
            return "runtime_unavailable"
        if active.status_code != 200:
            return "runtime_rejected"
        synchronized = active.json()["configReload"]["synchronized"]
        if synchronized is False:
            return "runtime_unsynchronized"
        if synchronized is not True:
            return "invalid_runtime_receipt"
        current = await self.forward(http, "GET", read_path, headers)
        if current.status_code in TRANSIENT_READ_STATUS:
            return "resource_unavailable"
        if current.status_code != 200:
            return "resource_rejected"
        by_id = self.resources(current)
        if deleted is not None:
            return "resource_conflict" if deleted in by_id else None
        for key, receipt in expected.items():
            if key in by_id and self.resource_identity(by_id[key]) != receipt:
                return "resource_conflict"
        return None if expected.keys() <= by_id.keys() else "resource_not_visible"

    async def confirm(self, http, method, path, read_path, native, headers):
        deleted = path.rsplit("/", 1)[-1] if method == "DELETE" else None
        try:
            expected = {} if deleted is not None else self.write_receipt(native)
        except (KeyError, TypeError, ValueError):
            return "invalid_write_receipt"
        pending = "read_deadline"
        headers = {name: value for name, value in headers.items() if name != "if-none-match"}
        try:
            async with asyncio.timeout(READBACK_SECONDS):
                while True:
                    try:
                        pending = await self.readback(http, read_path, headers, expected, deleted)
                    except httpx.TransportError:
                        pending = "read_transport"
                    except (KeyError, TypeError, ValueError, httpx.HTTPError):
                        return "invalid_readback"
                    if pending not in RETRY_READBACK:
                        return pending
                    await asyncio.sleep(READBACK_INTERVAL)
        except TimeoutError:
            return pending

    @classmethod
    def pending(cls, request_id, phase, reason):
        LOGGER.warning(
            "Native model maintenance pending: request_id=%s phase=%s reason=%s",
            request_id,
            phase,
            reason,
        )
        return cls.unavailable(request_id)

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

            # Exactly one native effect. Only confirmation reads may be retried.
            try:
                native = await self.forward(http, request.method, path, headers, body=body)
            except (httpx.HTTPError, ValueError):
                return self.pending(request_id, "native_write", "unconfirmed_response")
            if native.is_success:
                try:
                    await self.restart(request.app.state.kube, request_id)
                except (httpx.HTTPError, KeyError, TypeError, ValueError, TimeoutError):
                    return self.pending(request_id, "native_rollout", "unconfirmed_same_store")
                reason = await self.confirm(http, request.method, path, read_path, native, headers)
                if reason is not None:
                    return self.pending(request_id, "native_readback", reason)
            elif native.status_code in {400, 401, 403, 404, 409, 422}:
                try:
                    after = await self.forward(http, "GET", read_path, headers)
                    if after.status_code != 200 or after.json() != before.json():
                        return self.pending(request_id, "native_write", "rejection_unconfirmed")
                except (httpx.HTTPError, ValueError):
                    return self.pending(request_id, "native_write", "rejection_unconfirmed")
            else:
                return self.pending(request_id, "native_write", "unexpected_status")
            if not await self.fence(http, request_id, "release"):
                return self.pending(request_id, "fence_release", "unconfirmed_consumers")
            return self.reply(native)

    async def localized_reply(self, request: Request, path: str, headers: dict) -> Response:
        assert self.localized is not None
        if not UI_PATH.fullmatch(path):
            return Response(status_code=404)
        try:
            auth = await self.forward(
                request.app.state.http,
                "GET",
                "/api/runtime",
                {k: v for k, v in headers.items() if k != "if-none-match"},
                ui_security=True,
            )
            if auth.status_code != 200:
                return self.reply(auth)
            if auth.json()["build"]["gitRevision"] != UPSTREAM_UI_COMMIT:
                return Response(status_code=503)
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            return Response(status_code=503)
        asset = self.localized.asset(path)
        if asset is None:
            return Response(status_code=404)
        data, media = asset
        security = {k: v for k, v in auth.headers.items() if k in UI_SECURITY_HEADERS}
        security.setdefault("x-content-type-options", "nosniff")
        return Response(
            b"" if request.method == "HEAD" else data,
            media_type=media,
            headers={**security, "cache-control": "no-store", "content-length": str(len(data))},
        )

    async def handle(self, request: Request) -> Response:
        raw = request.scope.get("raw_path", b"")
        if not raw.startswith(b"/") or raw.startswith(b"//") or b"\\" in raw:
            return Response(status_code=404)
        path = raw.decode("ascii", errors="replace")
        headers = {name: value for name, value in request.headers.items() if name in HEADERS}
        if (
            self.localized is not None
            and request.method in {"GET", "HEAD"}
            and (path == "/ui" or path.startswith(("/ui/", "/ui%")))
        ):
            return await self.localized_reply(request, path, headers)
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
    bundle = os.environ.get("BBX_NATIVE_LOCALIZED_UI_DIR")
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
        localized=LocalizedUI(Path(bundle)) if bundle else None,
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
