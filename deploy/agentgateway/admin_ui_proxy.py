"""Forward the native AgentGateway UI, its fenced model writes and local chat."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import ssl
from contextlib import asynccontextmanager
from html import escape
from http.cookiejar import CookieJar, DefaultCookiePolicy
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit
from uuid import UUID, uuid4

import httpx
from bbx_contracts.gateway_models import GatewayMaintenanceAck, GatewayMaintenanceFence
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

MUTATION = re.compile(
    r"/api/config/resources/(llm\.model|llm\.provider|llm\.virtualModel|modelCatalog|llm\.policy|llm\.apiKey)"
    r"(?:/([A-Za-z0-9_.!~*'()%-]{1,1024}))?"
)
USER_LLM_POLICIES = {"cors", "guardrails", "localRateLimit", "remoteRateLimit", "timeout"}
KEY_PATH = "/api/config/resources/llm.apiKey"
KEY_OWNER = "bbx_operator_managed"
KEY_ID = "agentgateway.dev/id"
COST_REFRESH = "/api/costs/refresh-base"
LOG_TAIL = "/api/logs/tail"
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
PLAYGROUND_PATH = b"/api/config/resources/playground/v1/chat/completions"
CHAT_PATH = "/v1/chat/completions"
PLAYGROUND_KEY_HEADER = "x-bbx-playground-key"
PLAYGROUND_MARKER = b'<meta name="bbx-native-playground" content="">'
DATA_ORIGIN_MARKER = b'<meta name="bbx-native-data-origin" content="">'
PLAYGROUND_TIMEOUT = httpx.Timeout(120, connect=5)


class LocalizedUI:
    """One verified build of the native UI; no filesystem lookup on a request."""

    def __init__(
        self, bundle: Path, *, playground: bool = False, public_data_origin: str | None = None
    ):
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
        index, media = self.assets["/ui/index.html"]
        if playground and index.count(PLAYGROUND_MARKER) != 1:
            raise ValueError("The localized native UI does not support the local playground")
        mode = b"enabled" if playground else b"disabled"
        marker = PLAYGROUND_MARKER.replace(b'content=""', b'content="' + mode + b'"')
        index = index.replace(PLAYGROUND_MARKER, marker)
        if public_data_origin is not None:
            if index.count(DATA_ORIGIN_MARKER) != 1:
                raise ValueError("The localized native UI does not support a public data origin")
            value = escape(origin(public_data_origin), quote=True).encode()
            marker = DATA_ORIGIN_MARKER.replace(b'content=""', b'content="' + value + b'"')
            index = index.replace(DATA_ORIGIN_MARKER, marker)
        self.index = self.assets["/ui/index.html"] = (index, media)
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


def resource_id(segment: str) -> str:
    decoded = unquote(segment, errors="strict")
    if (
        decoded in {".", ".."}
        or any(ord(char) < 32 or ord(char) == 127 or char == "\\" for char in decoded)
        or quote(decoded, safe="!~*'()") != segment
    ):
        raise ValueError("Invalid native resource ID")
    return decoded


def key_fingerprint(value: dict) -> str:
    if "key" in value and "keyHash" not in value:
        if isinstance(value["key"], str) and value["key"]:
            return hashlib.sha256(value["key"].encode()).hexdigest()
    if "key" not in value and re.fullmatch(r"sha256:[0-9a-fA-F]{64}", value.get("keyHash", "")):
        return value["keyHash"][7:].lower()
    raise ValueError("Invalid native key")


def operator_key(item: dict) -> bool:
    metadata = item["value"].get("metadata")
    return (
        isinstance(metadata, dict)
        and metadata.get(KEY_OWNER) is True
        and metadata.get(KEY_ID) == item["id"]
        and isinstance(metadata.get("name"), str)
        and not metadata["name"].strip().casefold().startswith(("bbx-", "bbx_", "bbx."))
    )


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
        self,
        upstream,
        public,
        peers,
        key,
        restart,
        *,
        localized: LocalizedUI | None = None,
        playground_data_url: str | None = None,
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
        self.playground_data = None
        if playground_data_url is not None:
            if localized is None:
                raise ValueError("The local playground requires a verified UI")
            self.playground_data = origin(playground_data_url)
            if urlsplit(self.playground_data).hostname != urlsplit(
                self.upstream
            ).hostname or self.playground_data in {self.upstream, self.public}:
                raise ValueError("The local playground requires a separate native data origin")

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

    async def forward(
        self,
        http,
        method,
        path,
        headers,
        *,
        query="",
        body=None,
        ui_security=False,
        timeout=httpx.USE_CLIENT_DEFAULT,
    ):
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
            timeout=timeout,
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
            if not isinstance(key, str) or not key or key in result or "value" not in item:
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
        deleted = resource_id(path.rsplit("/", 1)[-1]) if method == "DELETE" else None
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
        message = "Model, policy and price writes require both sites to be idle."
        if not idle:
            message = "The original native write requires maintenance readback."
        return JSONResponse(
            {"message": message},
            status_code=409 if idle else 503,
            headers={"x-bbx-maintenance-id": str(request_id), "cache-control": "no-store"},
        )

    async def catalog_refresh_receipt(self, http, headers, before, native):
        after = await self.forward(http, "GET", "/api/config/resources/modelCatalog", headers)
        if after.status_code != 200:
            raise ValueError("Native price catalog readback unavailable")
        previous = self.resources(before).get("default")
        entries = self.resources(after)
        if set(entries) != {"default"}:
            raise ValueError("Missing original price catalog")
        current = entries["default"]
        revision, value, created = self.resource_identity(current)
        receipt = native.json()
        providers = value["base"]["providers"]
        if (
            (
                previous is not None
                and (revision != previous["revision"] + 1 or created != previous["createdAt"])
            )
            or {k: v for k, v in value.items() if k != "base"}
            != {k: v for k, v in (previous["value"] if previous else {}).items() if k != "base"}
            or type(receipt.get("providers")) is not int
            or type(receipt.get("models")) is not int
            or len(providers) != receipt["providers"]
            or sum(len(provider["models"]) for provider in providers.values()) != receipt["models"]
        ):
            raise ValueError("Native price catalog refresh not confirmed")
        return after

    async def mutate(self, request, path, kind, headers, body):
        http = request.app.state.http
        refreshing = path == COST_REFRESH
        if refreshing:
            headers = {name: value for name, value in headers.items() if name != "if-none-match"}
        async with self.lock:
            # Authentication runs inside the original AgentGateway UI before
            # touching either admission fence.
            auth = await self.forward(http, "GET", "/api/runtime", headers)
            if auth.status_code != 200:
                return self.reply(auth)
            try:
                if auth.json()["ui"]["configStoreMode"] != "hybrid":
                    return self.managed()
            except (KeyError, TypeError, ValueError):
                return Response(status_code=503)
            if refreshing:
                try:
                    config = await self.forward(http, "GET", "/api/config", headers)
                    if config.status_code != 200:
                        return self.reply(config)
                    sources = config.json().get("config", {}).get("modelCatalog", [])
                    if any("file" in source for source in sources):
                        return self.managed()
                except (AttributeError, KeyError, TypeError, ValueError, httpx.HTTPError):
                    return Response(status_code=503)
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
                native = await self.forward(
                    http,
                    request.method,
                    path,
                    headers,
                    body=body,
                    timeout=60 if refreshing else httpx.USE_CLIENT_DEFAULT,
                )
            except (httpx.HTTPError, ValueError):
                return self.pending(request_id, "native_write", "unconfirmed_response")
            if native.is_success:
                receipt = native
                if refreshing:
                    try:
                        receipt = await self.catalog_refresh_receipt(http, headers, before, native)
                    except (AttributeError, KeyError, TypeError, ValueError, httpx.HTTPError):
                        return self.pending(request_id, "native_write", "unconfirmed_catalog")
                try:
                    await self.restart(request.app.state.kube, request_id)
                except (httpx.HTTPError, KeyError, TypeError, ValueError, TimeoutError):
                    return self.pending(request_id, "native_rollout", "unconfirmed_same_store")
                reason = await self.confirm(http, request.method, path, read_path, receipt, headers)
                if reason is not None:
                    return self.pending(request_id, "native_readback", reason)
            elif native.status_code in {400, 401, 403, 404, 409, 422} or (
                refreshing and native.status_code == 500
            ):
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

    @staticmethod
    def managed() -> Response:
        return JSONResponse(
            {"message": "This configuration is managed by the platform and is read-only here."},
            status_code=403,
            headers={"cache-control": "no-store"},
        )

    @staticmethod
    def new_operator_key(body, item_id, previous):
        envelope = json.loads(body)
        if item_id is None:
            if set(envelope) != {"resources"} or not isinstance(envelope["resources"], list):
                raise ValueError("Expected one native key")
            if len(envelope["resources"]) != 1:
                raise ValueError("Expected one native key")
            envelope = envelope["resources"][0]
        if not isinstance(envelope, dict) or set(envelope) != {"value"}:
            raise ValueError("Invalid native key body")
        value = envelope["value"]
        metadata = value.get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError("Invalid native key metadata")
        if previous is not None:
            for field, original in previous["value"]["metadata"].items():
                if (
                    field in metadata
                    and (field == KEY_OWNER or field.startswith("agentgateway.dev/"))
                    and field != "agentgateway.dev/keyHint"
                    and type(metadata[field]) is type(original)
                    and metadata[field] == original
                ):
                    del metadata[field]
        name = metadata.get("name")
        if (
            not isinstance(name, str)
            or not name.strip()
            or name.strip().casefold().startswith(("bbx-", "bbx_", "bbx."))
            or any(
                field.casefold().startswith(("bbx-", "bbx_", "bbx.", "bbx/"))
                or field.startswith("agentgateway.dev/")
                and field != "agentgateway.dev/keyHint"
                for field in metadata
            )
        ):
            raise ValueError("The key name or metadata is reserved")
        key_fingerprint(value)
        metadata[KEY_OWNER] = True
        return value

    async def mutate_key(self, request, path, item_id, headers, body):
        http = request.app.state.http
        headers = {name: value for name, value in headers.items() if name != "if-none-match"}
        async with self.lock:
            try:
                auth = await self.forward(http, "GET", "/api/runtime", headers)
                if auth.status_code != 200:
                    return self.reply(auth)
                info = auth.json()
                if info["ui"]["configStoreMode"] != "hybrid":
                    return self.managed()
                if info["configReload"]["synchronized"] is not True:
                    return Response(status_code=503)
                before = await self.forward(http, "GET", KEY_PATH, headers)
                if before.status_code != 200:
                    return self.reply(before)
                rows = self.resources(before)
                if item_id is not None and (item_id not in rows or not operator_key(rows[item_id])):
                    return self.managed()
            except (AttributeError, KeyError, TypeError, ValueError, httpx.HTTPError):
                return Response(status_code=503)
            if request.method == "PUT":
                try:
                    value = self.new_operator_key(body, item_id, rows.get(item_id))
                    fingerprint = key_fingerprint(value)
                except (AttributeError, KeyError, TypeError, ValueError):
                    return JSONResponse(
                        {"message": "Use one valid key with a non-reserved name and metadata."},
                        status_code=422,
                    )
                try:
                    base = await self.forward(http, "GET", "/api/config", headers)
                    if base.status_code != 200:
                        return self.reply(base)
                    policy = base.json()["llm"]["policies"]["apiKey"]
                    if policy["mode"] != "strict":
                        return self.managed()
                    file_keys = policy.get("keys", [])
                    # Native expands environment references only in the file configuration.
                    if any("$" in item.get("key", "") for item in file_keys):
                        return Response(status_code=503)
                    stored = [row["value"] for key, row in rows.items() if key != item_id]
                    for existing in [*file_keys, *stored]:
                        if key_fingerprint(existing) == fingerprint:
                            return JSONResponse(
                                {"message": "This virtual key credential is already in use."},
                                status_code=409,
                            )
                except (AttributeError, KeyError, TypeError, ValueError, httpx.HTTPError):
                    return Response(status_code=503)
                native_body = {"value": value}
                if item_id is None:
                    native_body = {"resources": [native_body]}
                body = json.dumps(native_body).encode()
            try:
                native = await self.forward(http, request.method, path, headers, body=body)
                if not native.is_success:
                    return self.reply(native)
                if request.method == "PUT":
                    written = self.resources(native)
                    if len(written) != 1:
                        raise ValueError("Unexpected original key response")
                    returned_id, item = next(iter(written.items()))
                    if (
                        (item_id is not None and returned_id != item_id)
                        or not operator_key(item)
                        or key_fingerprint(item["value"]) != fingerprint
                    ):
                        raise ValueError("Unexpected original key identity")
                reason = await self.confirm(http, request.method, path, KEY_PATH, native, headers)
                if reason is None:
                    response = self.reply(native)
                    response.headers["cache-control"] = "no-store"
                    return response
            except (AttributeError, KeyError, TypeError, ValueError, httpx.HTTPError):
                pass
            return JSONResponse(
                {"message": "Key change unconfirmed. Refresh the key list before retrying."},
                status_code=503,
                headers={"cache-control": "no-store"},
            )

    async def playground_chat(self, request: Request, headers: dict) -> Response:
        if self.playground_data is None or request.method != "POST":
            return Response(status_code=405)
        if request.headers.getlist("origin") != [self.public] or request.url.query:
            return Response(status_code=403)
        keys = request.headers.getlist(PLAYGROUND_KEY_HEADER)
        if len(keys) != 1 or not re.fullmatch(r"[!-~]{1,512}", keys[0]):
            return Response(status_code=401, headers={"cache-control": "no-store"})
        content_types = request.headers.getlist("content-type")
        if len(content_types) != 1 or content_types[0].split(";", 1)[0] != "application/json":
            return Response(status_code=415)
        body = await bounded_body(request.stream())
        if body is None:
            return Response(status_code=413)
        if self.lock.locked():
            return Response(status_code=409)
        try:
            # Model saves can restart the native process; keep chat and saves apart.
            async with self.lock, asyncio.timeout(120):
                headers.pop("if-none-match", None)
                lookup = await self.forward(request.app.state.http, "GET", KEY_PATH, headers)
                if lookup.status_code != 200:
                    return self.reply(lookup)
                fingerprint = hashlib.sha256(keys[0].encode("ascii")).hexdigest()
                matches = [
                    item
                    for item in self.resources(lookup).values()
                    if hmac.compare_digest(key_fingerprint(item["value"]), fingerprint)
                ]
                if len(matches) != 1 or not operator_key(matches[0]):
                    return Response(status_code=401, headers={"cache-control": "no-store"})
                return await self.forward_chat(request, keys[0], body)
        except (httpx.TimeoutException, TimeoutError):
            return Response(status_code=504)
        except (AttributeError, KeyError, TypeError, ValueError, httpx.HTTPError):
            return Response(status_code=502)

    async def forward_chat(self, request, key, body):
        async with request.app.state.playground_http.stream(
            "POST",
            self.playground_data + CHAT_PATH,
            headers={"authorization": "Bearer " + key, "content-type": "application/json"},
            content=body,
            timeout=PLAYGROUND_TIMEOUT,
            follow_redirects=False,
        ) as native:
            if native.is_redirect:
                return Response(status_code=502)
            result = await bounded_body(native.aiter_bytes())
            if result is None:
                return Response(status_code=502)
            return Response(
                result,
                native.status_code,
                media_type=(
                    "application/json"
                    if native.headers.get("content-type", "").split(";", 1)[0] == "application/json"
                    else "text/plain"
                ),
                headers={"cache-control": "no-store", "x-content-type-options": "nosniff"},
            )

    async def stream_logs(self, http, headers, body):
        if "origin" in headers:
            headers["origin"] = self.upstream
        try:
            native = await http.send(
                http.build_request(
                    "POST",
                    self.upstream + LOG_TAIL,
                    headers=headers,
                    content=body,
                    timeout=httpx.Timeout(30, connect=5),
                ),
                stream=True,
                follow_redirects=False,
            )
        except httpx.HTTPError:
            return Response(status_code=503)

        if native.status_code != 200 or not native.headers.get("content-type", "").startswith(
            "text/event-stream"
        ):
            try:
                data = await bounded_body(native.aiter_bytes())
                if data is None or native.status_code == 200:
                    return Response(status_code=503)
                return Response(
                    data,
                    native.status_code,
                    {k: v for k, v in native.headers.items() if k in REPLY_HEADERS},
                )
            except httpx.HTTPError:
                return Response(status_code=503)
            finally:
                await native.aclose()

        async def events():
            try:
                async for chunk in native.aiter_bytes():
                    yield chunk
            except httpx.HTTPError:
                return
            finally:
                await native.aclose()

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"cache-control": "no-store"},
            background=BackgroundTask(native.aclose),
        )

    async def handle(self, request: Request) -> Response:
        raw = request.scope.get("raw_path", b"")
        if not raw.startswith(b"/") or raw.startswith(b"//") or b"\\" in raw:
            return Response(status_code=404)
        path = raw.decode("ascii", errors="replace")
        headers = {name: value for name, value in request.headers.items() if name in HEADERS}
        if raw == PLAYGROUND_PATH:
            return await self.playground_chat(request, headers)
        if (
            self.localized is not None
            and request.method in {"GET", "HEAD"}
            and (path == "/ui" or path.startswith(("/ui/", "/ui%")))
        ):
            return await self.localized_reply(request, path, headers)
        match = MUTATION.fullmatch(path)
        writing = request.method in {"PUT", "DELETE"} and match is not None
        kind, item_id = None, None
        if writing:
            assert match is not None
            kind, segment = match.groups()
            try:
                item_id = resource_id(segment) if segment else None
            except ValueError:
                return Response(status_code=404)
            if request.method == "DELETE" and item_id is None:
                return Response(status_code=405)
            if kind == "llm.policy" and item_id not in USER_LLM_POLICIES:
                return self.managed()
            if kind == "llm.apiKey" and item_id is not None:
                try:
                    if str(UUID(item_id)) != item_id:
                        return self.managed()
                except ValueError:
                    return self.managed()
        refreshing = request.method == "POST" and path == COST_REFRESH
        tailing = request.method == "POST" and path == LOG_TAIL
        reading = request.method in {"GET", "HEAD"} or (
            request.method == "POST" and path in READ_POST
        )
        if not (writing or reading or refreshing or tailing):
            if path in {"/api/config", "/api/config/raw"} or path.startswith(
                "/api/config/resources/"
            ):
                return self.managed()
            return Response(status_code=405)
        if request.method not in {"GET", "HEAD"} and request.headers.getlist("origin") != [
            self.public
        ]:
            return Response(status_code=403)
        body = None
        if writing or request.method == "POST":
            body = await bounded_body(request.stream())
            if body is None:
                return Response(status_code=413)
        if writing or refreshing or tailing:
            if request.url.query:
                return Response(status_code=403)
            if (request.method == "PUT" or tailing) and headers.get("content-type", "").split(";")[
                0
            ] != "application/json":
                return Response(status_code=415)
            if refreshing:
                if body:
                    return Response(status_code=400)
                return await self.mutate(request, path, "modelCatalog", headers, body)
            if tailing:
                return await self.stream_logs(request.app.state.http, headers, body)
            if kind == "llm.apiKey":
                return await self.mutate_key(request, path, item_id, headers, body)
            return await self.mutate(request, path, kind, headers, body)
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


async def bounded_body(chunks):
    data = bytearray()
    async for chunk in chunks:
        if len(data) + len(chunk) > LIMIT:
            return None
        data.extend(chunk)
    return bytes(data)


def build() -> Starlette:
    service_account = Path("/var/run/secrets/kubernetes.io/serviceaccount")
    bundle = os.environ.get("BBX_NATIVE_LOCALIZED_UI_DIR")
    playground_data_url = os.environ.get("BBX_NATIVE_PLAYGROUND_DATA_URL")
    public_data_origin = os.environ.get("BBX_NATIVE_PUBLIC_LLM_ORIGIN")
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
        localized=LocalizedUI(
            Path(bundle),
            playground=playground_data_url is not None,
            public_data_origin=public_data_origin,
        )
        if bundle
        else None,
        playground_data_url=playground_data_url,
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
            httpx.AsyncClient(
                verify=ssl_ui,
                trust_env=False,
                cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=[])),
                limits=httpx.Limits(max_keepalive_connections=0),
            ) as playground_http,
        ):
            app.state.http, app.state.kube, app.state.playground_http = http, kube, playground_http
            yield

    return Starlette(
        routes=[Route("/{path:path}", proxy.handle, methods=["GET", "POST", "PUT", "DELETE"])],
        lifespan=lifespan,
    )
