"""Trusted native resource I/O; Blackboard owns dispatch, identities and cleanup decisions."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import secrets
import ssl
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from uuid import UUID

import httpx
from opensandbox import Sandbox, SandboxManager
from opensandbox.adapters.factory import AdapterFactory
from opensandbox.config import ConnectionConfig
from opensandbox.models.sandboxes import PVC, SandboxFilter, SandboxImageSpec, Volume
from opensandbox.transport import RetryPolicy

CLEANUP_GRACE_SECONDS = 120


class ResourceUnknown(RuntimeError):
    """No replay or cleanup claim can be inferred from this local observation."""


class RunnerResources:
    def __init__(self, config: dict):
        self.config = dict(config)
        for key in (
            "source_id",
            "kubeconfig",
            "context",
            "cluster_uid",
            "runner_image",
            "sandbox_image",
            "environment_revision",
            "sandbox_server_url",
            "storage_class",
            "system_namespace",
        ):
            if not self.config.get(key):
                raise ValueError("The trusted resource deployment is incomplete")
        for key in ("runner_image", "sandbox_image"):
            if not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", self.config[key]):
                raise ValueError("Resource images must use trusted immutable digests")
        url = urlsplit(self.config["sandbox_server_url"])
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
        ):
            raise ValueError("A fixed native Sandbox Server origin is required")
        self.server = url
        self._name(self.config["system_namespace"])

    async def _kubectl(self, *args: str, body: dict | None = None) -> str:
        process = await asyncio.create_subprocess_exec(
            "kubectl",
            "--kubeconfig",
            self.config["kubeconfig"],
            "--context",
            self.config["context"],
            *args,
            stdin=asyncio.subprocess.PIPE if body is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, _ = await asyncio.wait_for(
                process.communicate(None if body is None else json.dumps(body).encode()), timeout=25
            )
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        if process.returncode or len(out) > 16 * 1024 * 1024:
            raise ResourceUnknown("The native resource operation is unresolved")
        return out.decode()

    async def _cluster(self):
        value = json.loads(await self._kubectl("get", "namespace", "kube-system", "-o", "json"))
        if value["metadata"]["uid"] != self.config["cluster_uid"]:
            raise ResourceUnknown("The configured Kubernetes cluster identity changed")

    @staticmethod
    def _name(value: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", value
        ):
            raise ResourceUnknown("Invalid trusted resource name")
        return value

    def _scope(self, operation):
        proof = operation.get("observation") or operation
        for key in ("source_id", "cluster_uid"):
            if key in proof and proof[key] != self.config[key]:
                raise ResourceUnknown("The original native resource source changed")
        tid, oid = str(UUID(operation["task_id"])), str(UUID(operation["operation_id"]))
        namespace = self._name(operation["namespace"])
        return namespace, {
            "bbx.dev/managed-by": "runner-resources",
            "bbx.dev/task-id": tid,
            "bbx.dev/operation-id": oid,
        }

    async def _get(self, kind, name, namespace=None):
        args = ["get", kind, self._name(name), "--ignore-not-found", "-o", "json"]
        if namespace:
            args += ["-n", self._name(namespace)]
        raw = await self._kubectl(*args)
        return json.loads(raw) if raw.strip() else None

    def _owned(self, value, labels, uid=None):
        meta = value.get("metadata", {}) if value else {}
        if (
            not value
            or any(meta.get("labels", {}).get(k) != v for k, v in labels.items())
            or meta.get("annotations", {}).get("bbx.dev/source-id") != self.config["source_id"]
            or (uid is not None and meta.get("uid") != uid)
        ):
            raise ResourceUnknown("The native resource ownership or UID changed")
        return value

    async def _ensure(self, doc, labels, uid=None):
        meta = doc["metadata"]
        existing = await self._get(doc["kind"], meta["name"], meta.get("namespace"))
        if existing is None:
            if uid is not None:
                raise ResourceUnknown("An originally bound resource is missing")
            await self._kubectl("create", "-f", "-", body=doc)
            existing = await self._get(doc["kind"], meta["name"], meta.get("namespace"))
        existing = self._owned(existing, labels, uid)
        if doc["kind"] in {"ResourceQuota", "CiliumNetworkPolicy"} and existing.get(
            "spec"
        ) != doc.get("spec"):
            raise ResourceUnknown("The Task quota or network policy changed")
        if doc["kind"] == "ServiceAccount" and (
            existing.get("automountServiceAccountToken") is not False or existing.get("secrets")
        ):
            raise ResourceUnknown("The Runner service account changed")
        return existing

    def _document(self, kind, name, labels, namespace=None, **fields):
        return {
            "apiVersion": "v1",
            "kind": kind,
            "metadata": {
                "name": name,
                **({"namespace": namespace} if namespace else {}),
                "labels": labels,
                "annotations": {"bbx.dev/source-id": self.config["source_id"]},
            },
            **fields,
        }

    def _connection(self, tenant_key, execd_token=None):
        if not isinstance(tenant_key, str) or not tenant_key.strip():
            raise ResourceUnknown("The original Task tenant key is unavailable")
        verify = ssl.create_default_context(cafile=self.config.get("ca_file"))
        return ConnectionConfig(
            domain=self.server.netloc,
            protocol=self.server.scheme,
            api_key=tenant_key,
            headers={"X-EXECD-ACCESS-TOKEN": execd_token} if execd_token else {},
            disable_metrics=True,
            request_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(max_retries=0, wake_retry_enabled=False),
            transport=httpx.AsyncHTTPTransport(verify=verify, retries=0),
        )

    @staticmethod
    async def _close_sdk(value, connection):
        await value.close()
        if connection.transport is not None:
            await connection.transport.aclose()

    async def _sandbox_id(self, tenant_key, metadata):
        connection = self._connection(tenant_key)
        manager = await SandboxManager.create(connection_config=connection)
        try:
            found = await manager.list_sandbox_infos(
                SandboxFilter(metadata=metadata, page_size=2, page=1)
            )
            if len(found.sandbox_infos) > 1 or found.pagination.has_next_page:
                raise ResourceUnknown("The original Sandbox create is ambiguous")
            return found.sandbox_infos[0].id if found.sandbox_infos else None
        except ResourceUnknown:
            raise
        except Exception:
            raise ResourceUnknown("The native Sandbox metadata lookup is unresolved") from None
        finally:
            await self._close_sdk(manager, connection)

    async def _wait_pod(self, namespace, cr_uid, old_uid=None):
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            pods = json.loads(await self._kubectl("get", "pods", "-n", namespace, "-o", "json"))[
                "items"
            ]
            found = [
                p
                for p in pods
                if any(r.get("uid") == cr_uid for r in p["metadata"].get("ownerReferences", []))
            ]
            if len(found) > 1:
                raise ResourceUnknown("The native Sandbox has multiple Pods")
            if found:
                pod = found[0]
                if old_uid and pod["metadata"]["uid"] != old_uid:
                    raise ResourceUnknown("The original Sandbox Pod was replaced")
                if any(
                    c["type"] == "Ready" and c["status"] == "True"
                    for c in pod.get("status", {}).get("conditions", [])
                ):
                    return pod
            await asyncio.sleep(0.25)
        raise ResourceUnknown("The native Sandbox Pod readiness is unresolved")

    async def ensure_task_resources(
        self,
        operation,
        tenant_key,
        *,
        admit_sandbox_create: Callable[[], Awaitable[bool]],
        checkpoint: Callable[[dict], Awaitable[None]],
    ):
        namespace, labels = self._scope(operation)
        await self._cluster()
        observed = operation.get("observation") or {}
        if observed and (
            observed.get("task_id") != labels["bbx.dev/task-id"]
            or observed.get("operation_id") != labels["bbx.dev/operation-id"]
            or observed.get("namespace") != namespace
        ):
            raise ResourceUnknown("The original resource operation changed")
        deadline = datetime.fromisoformat(operation["hard_deadline"].replace("Z", "+00:00"))
        if deadline.tzinfo is None:
            raise ResourceUnknown("The Task deadline requires an explicit timezone")
        remaining = (deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise ResourceUnknown("The Task resource deadline has expired")
        ns = await self._ensure(
            self._document("Namespace", namespace, labels), labels, observed.get("namespace_uid")
        )
        await self._ensure(
            self._document(
                "ServiceAccount",
                "bbx-runner",
                labels,
                namespace,
                automountServiceAccountToken=False,
            ),
            labels,
        )
        await self._ensure(
            self._document(
                "ResourceQuota",
                "bbx-task",
                labels,
                namespace,
                spec={
                    "hard": {
                        "pods": "2",
                        "persistentvolumeclaims": "2",
                        "requests.storage": "192Mi",
                        "requests.cpu": "1",
                        "requests.memory": "2Gi",
                        "limits.cpu": "2",
                        "limits.memory": "2Gi",
                    }
                },
            ),
            labels,
        )
        policy = self._document(
            "CiliumNetworkPolicy",
            "bbx-task",
            labels,
            namespace,
            spec={
                "endpointSelector": {},
                "ingress": [
                    {
                        "fromEndpoints": [
                            {"matchLabels": {"k8s:io.kubernetes.pod.namespace": n}}
                            for n in [
                                namespace,
                                self.config["system_namespace"],
                            ]
                        ]
                    }
                ],
                "egress": [
                    {
                        "toEndpoints": [
                            {"matchLabels": {"k8s:io.kubernetes.pod.namespace": namespace}}
                        ]
                    },
                    {
                        "toEndpoints": [
                            {
                                "matchLabels": {
                                    "k8s:io.kubernetes.pod.namespace": "kube-system",
                                    "k8s:k8s-app": "kube-dns",
                                }
                            }
                        ],
                        "toPorts": [
                            {
                                "ports": [
                                    {"port": "53", "protocol": "UDP"},
                                    {"port": "53", "protocol": "TCP"},
                                ]
                            }
                        ],
                    },
                ],
                "egressDeny": [{"toEntities": ["host", "remote-node", "kube-apiserver"]}],
            },
        )
        policy["apiVersion"] = "cilium.io/v2"
        await self._ensure(policy, labels)
        allowed = self.config.get("runner_control_egress", [])
        runner_policy = self._document(
            "CiliumNetworkPolicy",
            "bbx-runner-control",
            labels,
            namespace,
            spec={
                "endpointSelector": {"matchLabels": {"bbx.dev/component": "runner"}},
                "egress": [
                    {
                        "toCIDR": entry["cidrs"],
                        "toPorts": [
                            {
                                "ports": [
                                    {"port": str(port), "protocol": "TCP"}
                                    for port in entry["ports"]
                                ]
                            }
                        ],
                    }
                    for entry in allowed
                ],
            },
        )
        runner_policy["apiVersion"] = "cilium.io/v2"
        await self._ensure(runner_policy, labels)
        refs = {}
        for name, size in [("journal", "128Mi"), ("workspace", "64Mi")]:
            claim = await self._ensure(
                self._document(
                    "PersistentVolumeClaim",
                    "bbx-" + name,
                    labels,
                    namespace,
                    spec={
                        "accessModes": ["ReadWriteOnce"],
                        "storageClassName": self.config["storage_class"],
                        "resources": {"requests": {"storage": size}},
                    },
                ),
                labels,
                observed.get(name + "_pvc", {}).get("uid"),
            )
            if (
                claim["spec"].get("storageClassName") != self.config["storage_class"]
                or claim["metadata"].get("ownerReferences")
                or claim["spec"].get("accessModes") != ["ReadWriteOnce"]
                or claim["spec"].get("volumeMode", "Filesystem") != "Filesystem"
                or claim["spec"].get("resources", {}).get("requests", {}).get("storage") != size
                or any(claim["spec"].get(k) for k in ("dataSource", "dataSourceRef", "selector"))
            ):
                raise ResourceUnknown("The retained PVC contract changed")
            refs[name + "_pvc"] = {
                "name": claim["metadata"]["name"],
                "uid": claim["metadata"]["uid"],
            }
        secret = await self._ensure(
            self._document(
                "Secret",
                "bbx-execd",
                labels,
                namespace,
                type="Opaque",
                stringData={"token": secrets.token_urlsafe(32)},
            ),
            labels,
            observed.get("execd_secret", {}).get("uid"),
        )
        token = base64.b64decode(secret["data"]["token"]).decode()
        if not token or secret.get("type") != "Opaque" or set(secret.get("data", {})) != {"token"}:
            raise ResourceUnknown("The original execd identity is absent")
        metadata = {
            "bbx-task-id": labels["bbx.dev/task-id"],
            "bbx-create-op": labels["bbx.dev/operation-id"],
        }
        sid = await self._sandbox_id(tenant_key, metadata)
        if observed.get("sandbox") and sid != observed["sandbox"]["id"]:
            raise ResourceUnknown("The original Sandbox is absent or changed")
        connection = self._connection(tenant_key, token)
        sandbox = None
        partial = {
            "source_id": self.config["source_id"],
            "cluster_uid": self.config["cluster_uid"],
            "namespace": namespace,
            "namespace_uid": ns["metadata"]["uid"],
            **refs,
            "task_id": labels["bbx.dev/task-id"],
            "operation_id": labels["bbx.dev/operation-id"],
            "execd_secret": {"name": "bbx-execd", "uid": secret["metadata"]["uid"]},
        }
        try:
            if not sid:
                if not await admit_sandbox_create():
                    raise ResourceUnknown("The original Sandbox create cannot be replayed")
                remaining = (deadline - datetime.now(UTC)).total_seconds()
                if remaining <= 0:
                    raise ResourceUnknown("The Task deadline expired before native creation")
                created = (
                    await AdapterFactory(connection)
                    .create_sandbox_service()
                    .create_sandbox(
                        spec=SandboxImageSpec(image=self.config["sandbox_image"]),
                        env={},
                        extensions={},
                        network_policy=None,
                        timeout=timedelta(seconds=max(1, int(remaining)) + CLEANUP_GRACE_SECONDS),
                        secure_access=True,
                        metadata=metadata,
                        entrypoint=["/usr/bin/tail", "-f", "/dev/null"],
                        resource={"cpu": "500m", "memory": "768Mi"},
                        resource_requests={"cpu": "50m", "memory": "128Mi"},
                        volumes=[
                            Volume(
                                name="bbx-workspace",
                                mountPath="/workspace",
                                pvc=PVC(
                                    claimName="bbx-workspace",
                                    createIfNotExists=False,
                                    deleteOnSandboxTermination=False,
                                ),
                            )
                        ],
                    )
                )
                sid = created.id
            await checkpoint({**partial, "sandbox": {"id": sid}})
            sandbox = await Sandbox.connect(
                sid, connection_config=connection, connect_timeout=timedelta(seconds=150)
            )
        except ResourceUnknown:
            raise
        except Exception:
            raise ResourceUnknown("The native Sandbox create or readiness is unresolved") from None
        finally:
            if sandbox is not None:
                await sandbox.close()
            if connection.transport is not None:
                await connection.transport.aclose()
        cr = await self._get("batchsandbox", sid, namespace)
        if not cr or any(cr["metadata"].get("labels", {}).get(k) != v for k, v in metadata.items()):
            raise ResourceUnknown("The native Sandbox identity did not match")
        if (
            observed.get("sandbox", {}).get("cr_uid")
            and cr["metadata"]["uid"] != observed["sandbox"]["cr_uid"]
        ):
            raise ResourceUnknown("The original Sandbox CR was replaced")
        pod = await self._wait_pod(
            namespace, cr["metadata"]["uid"], observed.get("sandbox", {}).get("pod_uid")
        )
        environment_revision = self._sandbox_spec(pod)
        mount = await self._ensure(
            self._document(
                "Secret",
                "bbx-runner",
                labels,
                namespace,
                type="Opaque",
                stringData={
                    "ca.pem": Path(self.config["ca_file"]).read_text()
                    if self.config.get("ca_file")
                    else ""
                },
            ),
            labels,
            observed.get("runner_secret", {}).get("uid"),
        )
        self._runner_secret(mount)
        expected_ca = Path(self.config["ca_file"]).read_text() if self.config.get("ca_file") else ""
        if base64.b64decode(mount.get("data", {}).get("ca.pem", "")).decode() != expected_ca:
            raise ResourceUnknown("The Runner startup trust anchor changed")
        runner_doc = self._runner_document(namespace, labels)
        fingerprint = hashlib.sha256(
            json.dumps(runner_doc["spec"], sort_keys=True).encode()
        ).hexdigest()
        runner_doc["metadata"]["annotations"]["bbx.dev/spec-sha256"] = fingerprint
        runner = await self._ensure(
            runner_doc,
            {**labels, "bbx.dev/component": "runner"},
            observed.get("runner", {}).get("pod_uid"),
        )
        if (
            runner["metadata"]["annotations"].get("bbx.dev/spec-sha256") != fingerprint
            or runner["spec"]["containers"][0]["image"] != self.config["runner_image"]
        ):
            raise ResourceUnknown("The Runner Pod template changed")
        self._runner_spec(runner, runner_doc["spec"])
        runner_uid = runner["metadata"]["uid"]
        until = time.monotonic() + 120
        while not self._pod_projection(runner).get("container_id"):
            if time.monotonic() >= until:
                raise ResourceUnknown("The original Runner container has not started")
            await asyncio.sleep(0.25)
            runner = self._owned(
                await self._get("pod", "bbx-runner", namespace),
                {**labels, "bbx.dev/component": "runner"},
                runner_uid,
            )
            self._runner_spec(runner, runner_doc["spec"])
        return {
            "source_id": self.config["source_id"],
            "cluster_uid": self.config["cluster_uid"],
            "namespace": namespace,
            "namespace_uid": ns["metadata"]["uid"],
            **refs,
            "task_id": labels["bbx.dev/task-id"],
            "operation_id": labels["bbx.dev/operation-id"],
            "execd_secret": {"name": "bbx-execd", "uid": secret["metadata"]["uid"]},
            "runner_secret": {"name": "bbx-runner", "uid": mount["metadata"]["uid"]},
            "sandbox": {
                "id": sid,
                "cr_uid": cr["metadata"]["uid"],
                "pod_name": pod["metadata"]["name"],
                "pod_uid": pod["metadata"]["uid"],
                "environment_revision": environment_revision,
                "container_id": self._pod_projection(pod).get("container_id"),
            },
            "runner": self._pod_projection(runner),
        }

    def _sandbox_spec(self, pod):
        self._pod_projection(pod)
        main = next(c for c in pod["spec"]["containers"] if c["name"] == "sandbox")
        env = {entry["name"]: entry for entry in main.get("env", [])}
        if (
            main["image"] != self.config["sandbox_image"]
            or len(env) != len(main.get("env", []))
            or env.get("BBX_ENVIRONMENT_REVISION", {}).get("value")
            != self.config["environment_revision"]
            or env.get("BBX_POD_UID", {}).get("valueFrom", {}).get("fieldRef", {}).get("fieldPath")
            != "metadata.uid"
            or env.get("BBX_POD_UID", {})
            .get("valueFrom", {})
            .get("fieldRef", {})
            .get("apiVersion", "v1")
            != "v1"
            or env.get("EXECD_ACCESS_TOKEN", {})
            .get("valueFrom", {})
            .get("secretKeyRef", {})
            .get("name")
            != "bbx-execd"
            or env.get("EXECD_ACCESS_TOKEN", {})
            .get("valueFrom", {})
            .get("secretKeyRef", {})
            .get("key")
            != "token"
            or any(
                volume.get("persistentVolumeClaim", {}).get("claimName") == "bbx-journal"
                for volume in pod["spec"].get("volumes", [])
            )
        ):
            raise ResourceUnknown("The native Sandbox deployment identity changed")
        return env["BBX_ENVIRONMENT_REVISION"]["value"]

    def _runner_document(self, namespace, labels):
        return self._document(
            "Pod",
            "bbx-runner",
            {**labels, "bbx.dev/component": "runner"},
            namespace,
            spec={
                "restartPolicy": "Never",
                "serviceAccountName": "bbx-runner",
                "automountServiceAccountToken": False,
                "terminationGracePeriodSeconds": 15,
                "securityContext": {
                    "runAsUser": 1000,
                    "runAsGroup": 1000,
                    "fsGroup": 1000,
                    "runAsNonRoot": True,
                },
                "containers": [
                    {
                        "name": "runner",
                        "image": self.config["runner_image"],
                        "imagePullPolicy": "IfNotPresent",
                        "env": [
                            {
                                "name": "BBX_POD_UID",
                                "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}},
                            },
                            {"name": "NODE_EXTRA_CA_CERTS", "value": "/var/run/bbx/ca.pem"},
                        ],
                        "resources": {
                            "requests": {"cpu": "50m", "memory": "128Mi"},
                            "limits": {"cpu": "500m", "memory": "768Mi"},
                        },
                        "securityContext": {
                            "allowPrivilegeEscalation": False,
                            "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"]},
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "volumeMounts": [
                            {"name": "identity", "mountPath": "/var/run/bbx", "readOnly": True},
                            {"name": "journal", "mountPath": "/journal"},
                            {"name": "start", "mountPath": "/run/bbx-start"},
                        ],
                    }
                ],
                "volumes": [
                    {
                        "name": "identity",
                        "secret": {"secretName": "bbx-runner", "defaultMode": 0o440},
                    },
                    {"name": "journal", "persistentVolumeClaim": {"claimName": "bbx-journal"}},
                    {"name": "start", "emptyDir": {"medium": "Memory", "sizeLimit": "1Mi"}},
                ],
            },
        )

    @staticmethod
    def _runner_secret(secret):
        if secret.get("type") != "Opaque" or not set(secret.get("data", {})) <= {
            "runner-bootstrap.json",
            "credentials.json",
            "ca.pem",
        }:
            raise ResourceUnknown("The Runner Secret contains undeclared material")

    @staticmethod
    def _runner_spec(pod, expected):
        spec = pod["spec"]
        if (
            pod["metadata"].get("ownerReferences")
            or len(spec.get("containers", [])) != 1
            or any(
                spec.get(k)
                for k in (
                    "initContainers",
                    "ephemeralContainers",
                    "hostNetwork",
                    "hostPID",
                    "hostIPC",
                    "shareProcessNamespace",
                    "hostAliases",
                )
            )
            or any(
                spec.get(k) != expected[k]
                for k in (
                    "restartPolicy",
                    "serviceAccountName",
                    "automountServiceAccountToken",
                    "securityContext",
                    "volumes",
                )
            )
        ):
            raise ResourceUnknown("The original Runner Pod isolation or mounts changed")
        actual = spec["containers"][0]
        desired = expected["containers"][0]
        env = json.loads(json.dumps(actual.get("env", [])))
        for entry in env:
            if "fieldRef" in entry.get("valueFrom", {}):
                if entry["valueFrom"]["fieldRef"].pop("apiVersion", "v1") != "v1":
                    raise ResourceUnknown("The Runner Downward API changed")
        if (
            env != desired["env"]
            or any(
                actual.get(k) != desired[k]
                for k in (
                    "name",
                    "image",
                    "imagePullPolicy",
                    "resources",
                    "securityContext",
                    "volumeMounts",
                )
            )
            or any(
                actual.get(k)
                for k in (
                    "command",
                    "args",
                    "envFrom",
                    "ports",
                    "workingDir",
                    "lifecycle",
                    "startupProbe",
                    "readinessProbe",
                    "livenessProbe",
                    "stdin",
                    "tty",
                )
            )
        ):
            raise ResourceUnknown("The original Runner container contract changed")

    @staticmethod
    def _pod_projection(pod):
        spec = pod.get("spec", {})
        containers = spec.get("containers", [])
        states = pod.get("status", {}).get("containerStatuses", [])
        if (
            len(containers) != 1
            or spec.get("ephemeralContainers")
            or pod.get("status", {}).get("ephemeralContainerStatuses")
            or len(states) > 1
            or any(s.get("name") != containers[0]["name"] for s in states)
            or any(c.get("restartPolicy") == "Always" for c in spec.get("initContainers", []))
            or any(
                "running" in s.get("state", {}) or "waiting" in s.get("state", {})
                for s in pod.get("status", {}).get("initContainerStatuses", [])
            )
        ):
            raise ResourceUnknown("The native Pod has an unapproved container scope")
        ended = bool(states) and all("terminated" in s.get("state", {}) for s in states)
        return {
            "pod_name": pod["metadata"]["name"],
            "pod_uid": pod["metadata"]["uid"],
            "phase": pod.get("status", {}).get("phase", "Pending"),
            "terminated": ended,
            "container_name": containers[0]["name"],
            **(
                {"resource_version": pod["metadata"]["resourceVersion"]}
                if pod["metadata"].get("resourceVersion")
                else {}
            ),
            **(
                {"container_id": states[0]["containerID"]}
                if len(states) == 1 and states[0].get("containerID")
                else {}
            ),
            **(
                {"exit_code": states[0]["state"]["terminated"]["exitCode"]}
                if ended and len(states) == 1
                else {}
            ),
        }

    @staticmethod
    def _scoped_credentials(value, task_id):
        if not isinstance(value, dict) or any(
            not isinstance(item, dict)
            or item.get("audience") not in {"runner-control", "model", "mcp"}
            or item.get("task_id") != task_id
            for item in value.values()
        ):
            raise ResourceUnknown("Only Task-scoped Runner credentials can be mounted")
        return value

    async def publish_mount(self, observation, bootstrap, credentials, *, control_only=False):
        namespace, labels = self._scope(observation)
        await self._cluster()
        pod = self._owned(
            await self._get("pod", observation["runner"]["pod_name"], namespace),
            {**labels, "bbx.dev/component": "runner"},
            observation["runner"]["pod_uid"],
        )
        if (
            self._pod_projection(pod)["terminated"]
            or self._pod_projection(pod).get("container_id")
            != observation["runner"].get("container_id")
            or bootstrap["fence"]["pod_uid"] != pod["metadata"]["uid"]
            or bootstrap["fence"]["task_id"] != labels["bbx.dev/task-id"]
        ):
            raise ResourceUnknown("The Runner mount is not bound to the original live Pod")
        self._runner_spec(pod, self._runner_document(namespace, labels)["spec"])
        secret = self._owned(
            await self._get("secret", "bbx-runner", namespace),
            labels,
            observation["runner_secret"]["uid"],
        )
        self._runner_secret(secret)
        self._scoped_credentials(credentials, labels["bbx.dev/task-id"])
        config_json = json.dumps(bootstrap, ensure_ascii=False, separators=(",", ":"))
        prior = secret.get("data", {}).get("runner-bootstrap.json")
        if prior and json.loads(base64.b64decode(prior)) != bootstrap:
            raise ResourceUnknown("The original Pod bootstrap cannot be changed")
        if control_only:
            reference = bootstrap.get("control_credential_ref")
            if (
                set(credentials) != {reference}
                or credentials[reference].get("audience") != "runner-control"
                or "ca.pem" not in secret["data"]
            ):
                raise ResourceUnknown("Control renewal cannot publish new execution credentials")
            try:
                existing = self._scoped_credentials(
                    json.loads(base64.b64decode(secret["data"]["credentials.json"]))
                    if "credentials.json" in secret["data"]
                    else {},
                    labels["bbx.dev/task-id"],
                )
            except (KeyError, ValueError, UnicodeError):
                raise ResourceUnknown("The original credential mount is unavailable") from None
            existing[reference] = credentials[reference]
            if not prior:
                secret["data"]["runner-bootstrap.json"] = base64.b64encode(
                    config_json.encode()
                ).decode()
            secret["data"]["credentials.json"] = base64.b64encode(
                json.dumps(existing, ensure_ascii=False, separators=(",", ":")).encode()
            ).decode()
            await self._kubectl("replace", "-f", "-", body=secret)
            return
        ca = Path(self.config["ca_file"]).read_text() if self.config.get("ca_file") else ""
        secret["data"] = {
            key: base64.b64encode(value.encode()).decode()
            for key, value in {
                "runner-bootstrap.json": base64.b64decode(prior).decode() if prior else config_json,
                "credentials.json": json.dumps(
                    credentials, ensure_ascii=False, separators=(",", ":")
                ),
                "ca.pem": ca,
            }.items()
        }
        await self._kubectl("replace", "-f", "-", body=secret)

    async def recover(self, operation, tenant_key):
        """Discover only the original operation; never create or replay resource mutations."""
        namespace, labels = self._scope(operation)
        await self._cluster()
        prior = operation.get("observation") or {}
        claimed = operation.get("sandbox_create_claimed", True)
        result = {
            **prior,
            "source_id": self.config["source_id"],
            "cluster_uid": self.config["cluster_uid"],
            "task_id": labels["bbx.dev/task-id"],
            "operation_id": labels["bbx.dev/operation-id"],
            "namespace": namespace,
            "not_started": False,
            "pvcs_retained": True,
        }
        ns = await self._get("namespace", namespace)
        had_execution = bool(
            prior.get("sandbox", {}).get("id") or prior.get("runner", {}).get("pod_uid")
        )
        if not ns:
            if prior.get("namespace_uid") or claimed is not False or had_execution:
                raise ResourceUnknown("The original namespace or create result is unresolved")
            return {**result, "not_started": True}
        self._owned(ns, labels, prior.get("namespace_uid"))
        result["namespace_uid"] = ns["metadata"]["uid"]
        for key in ("journal_pvc", "workspace_pvc", "execd_secret", "runner_secret"):
            name = {
                "journal_pvc": "bbx-journal",
                "workspace_pvc": "bbx-workspace",
                "execd_secret": "bbx-execd",
                "runner_secret": "bbx-runner",
            }[key]
            value = await self._get("pvc" if key.endswith("_pvc") else "secret", name, namespace)
            if not value:
                if prior.get(key, {}).get("uid"):
                    result["pvcs_retained"] = False
                continue
            self._owned(value, labels, prior.get(key, {}).get("uid"))
            if key.endswith("_pvc"):
                size = "128Mi" if key == "journal_pvc" else "64Mi"
                if (
                    value["spec"].get("storageClassName") != self.config["storage_class"]
                    or value["spec"].get("accessModes") != ["ReadWriteOnce"]
                    or value["spec"].get("volumeMode", "Filesystem") != "Filesystem"
                    or value["spec"].get("resources", {}).get("requests", {}).get("storage") != size
                    or value["metadata"].get("ownerReferences")
                    or any(
                        value["spec"].get(k) for k in ("dataSource", "dataSourceRef", "selector")
                    )
                ):
                    raise ResourceUnknown("The retained PVC contract changed")
            elif key == "runner_secret":
                self._runner_secret(value)
            elif (
                value.get("type") != "Opaque"
                or set(value.get("data", {})) != {"token"}
                or not base64.b64decode(value["data"]["token"]).strip()
            ):
                raise ResourceUnknown("The original execd Secret changed")
            result[key] = {"name": name, "uid": value["metadata"]["uid"]}
        metadata = {
            "bbx-task-id": labels["bbx.dev/task-id"],
            "bbx-create-op": labels["bbx.dev/operation-id"],
        }
        sid = prior.get("sandbox", {}).get("id") or await self._sandbox_id(tenant_key, metadata)
        pods = json.loads(await self._kubectl("get", "pods", "-n", namespace, "-o", "json"))[
            "items"
        ]
        if sid:
            result["sandbox"] = {**prior.get("sandbox", {}), "id": sid}
            cr = await self._get("batchsandbox", sid, namespace)
            if cr:
                if any(
                    cr["metadata"].get("labels", {}).get(k) != v for k, v in metadata.items()
                ) or (
                    prior.get("sandbox", {}).get("cr_uid")
                    and cr["metadata"]["uid"] != prior["sandbox"]["cr_uid"]
                ):
                    raise ResourceUnknown("The original Sandbox CR identity changed")
                result["sandbox"]["cr_uid"] = cr["metadata"]["uid"]
                children = [
                    pod
                    for pod in pods
                    if any(
                        owner.get("uid") == cr["metadata"]["uid"]
                        for owner in pod["metadata"].get("ownerReferences", [])
                    )
                ]
                if len(children) > 1:
                    raise ResourceUnknown("The original Sandbox Pod is ambiguous")
                if children:
                    pod = children[0]
                    revision = self._sandbox_spec(pod)
                    projection = self._pod_projection(pod)
                    for key in ("pod_uid", "container_id"):
                        if prior.get("sandbox", {}).get(key) and prior["sandbox"][
                            key
                        ] != projection.get(key):
                            raise ResourceUnknown("The original Sandbox process identity changed")
                    result["sandbox"].update(**projection, environment_revision=revision)
        runner = await self._get("pod", "bbx-runner", namespace)
        if runner:
            self._owned(
                runner,
                {**labels, "bbx.dev/component": "runner"},
                prior.get("runner", {}).get("pod_uid"),
            )
            self._runner_spec(runner, self._runner_document(namespace, labels)["spec"])
            projection = self._pod_projection(runner)
            if prior.get("runner", {}).get("container_id") and prior["runner"][
                "container_id"
            ] != projection.get("container_id"):
                raise ResourceUnknown("The original Runner container identity changed")
            result["runner"] = projection
        known_pods = {result.get(key, {}).get("pod_uid") for key in ("runner", "sandbox")}
        if any(pod["metadata"]["uid"] not in known_pods for pod in pods):
            raise ResourceUnknown("An unapproved execution Pod exists in the Task namespace")
        any_execution = bool(sid or runner or pods or had_execution)
        if claimed is False and not any_execution:
            result["not_started"] = True
        return result

    async def observe(self, observation):
        namespace, labels = self._scope(observation)
        await self._cluster()
        self._owned(await self._get("namespace", namespace), labels, observation["namespace_uid"])
        result = dict(observation)
        for key in ("journal_pvc", "workspace_pvc"):
            self._owned(
                await self._get("pvc", observation[key]["name"], namespace),
                labels,
                observation[key]["uid"],
            )
        for key in ("runner", "sandbox"):
            ref = observation.get(key, {})
            if not ref.get("pod_name") or not ref.get("pod_uid"):
                result[key] = {**ref, "terminated": False, "observation": "unknown"}
                continue
            pod = await self._get("pod", ref["pod_name"], namespace)
            if (
                not pod
                or pod["metadata"]["uid"] != observation[key]["pod_uid"]
                or self._pod_projection(pod).get("container_id")
                != observation[key].get("container_id")
            ):
                result[key] = {**observation[key], "terminated": False, "observation": "unknown"}
            else:
                result[key] = {**observation[key], **self._pod_projection(pod)}
        return result

    async def _terminated_watch(self, reference, namespace):
        container_id = reference.get("container_id")
        if (
            not container_id
            or not reference.get("resource_version")
            or not reference.get("container_name")
        ):
            return False
        meta = {
            "name": self._name(reference["pod_name"]),
            "uid": reference["pod_uid"],
            "resourceVersion": reference["resource_version"],
        }
        query = urlencode(
            {
                "watch": "true",
                "resourceVersion": meta["resourceVersion"],
                "fieldSelector": "metadata.name=" + meta["name"],
                "timeoutSeconds": "25",
            }
        )
        uri = f"/api/v1/namespaces/{namespace}/pods?{query}"
        process = await asyncio.create_subprocess_exec(
            "kubectl",
            "--kubeconfig",
            self.config["kubeconfig"],
            "--context",
            self.config["context"],
            "get",
            "--raw",
            uri,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=1024 * 1024,
        )
        failure = "watch_eof"
        confirmed = False
        try:
            async with asyncio.timeout(30):
                assert process.stdout is not None
                while line := await process.stdout.readline():
                    event = json.loads(line)
                    if event.get("type") == "BOOKMARK":
                        continue
                    if event.get("type") == "ERROR":
                        failure = (
                            "watch_expired"
                            if event.get("object", {}).get("code") == 410
                            else "watch_error"
                        )
                        break
                    current = event.get("object", {})
                    if current.get("metadata", {}).get("uid") != meta["uid"]:
                        failure = "watch_uid_changed"
                        break
                    states = current.get("status", {}).get("containerStatuses", [])
                    if not states:
                        continue
                    if (
                        len(states) != 1
                        or states[0].get("name") != reference["container_name"]
                        or states[0].get("containerID") not in {None, container_id}
                    ):
                        failure = "watch_container_changed"
                        break
                    if "terminated" not in states[0].get("state", {}):
                        continue
                    projection = self._pod_projection(current)
                    if projection["terminated"] and projection.get("container_id") == container_id:
                        confirmed = True
                        break
        except TimeoutError:
            failure = "watch_timeout"
        except (ValueError, ResourceUnknown):
            failure = "watch_invalid_scope"
        finally:
            if process.returncode is None:
                process.kill()
            await process.communicate()
        if not confirmed:
            logging.getLogger(__name__).warning("Native Pod exit remains unresolved (%s)", failure)
        return confirmed

    async def stop(self, observation, tenant_key):
        namespace, labels = self._scope(observation)
        await self._cluster()
        namespace_object = self._owned(
            await self._get("namespace", namespace), labels, observation["namespace_uid"]
        )
        result = {"runner_terminated": False, "sandbox_terminated": False, "pvcs_retained": False}
        watches = {}
        try:
            runner_delete = False
            for key in ("runner", "sandbox"):
                ref = observation.get(key, {})
                if not ref.get("pod_name") or not ref.get("pod_uid") or not ref.get("container_id"):
                    continue
                pod = await self._get("pod", ref["pod_name"], namespace)
                if pod:
                    if pod["metadata"]["uid"] != ref["pod_uid"]:
                        raise ResourceUnknown("The original Pod identity changed")
                    projection = self._pod_projection(pod)
                    if projection.get("container_id") != ref["container_id"]:
                        raise ResourceUnknown("The original container identity changed")
                    if projection["terminated"]:
                        result[key + "_terminated"] = True
                        continue
                    watch_reference = {**ref, **projection}
                    runner_delete = runner_delete or key == "runner"
                else:
                    # A prior cursor selects history; only an actual exit event proves termination.
                    watch_reference = {
                        **ref,
                        "container_name": ref.get("container_name", key),
                        "resource_version": ref.get("resource_version")
                        or namespace_object["metadata"]["resourceVersion"],
                    }
                watches[key] = asyncio.create_task(
                    self._terminated_watch(watch_reference, namespace)
                )
            await asyncio.sleep(0)
            if runner_delete:
                await self._kubectl(
                    "delete",
                    "--raw",
                    f"/api/v1/namespaces/{namespace}/pods/{observation['runner']['pod_name']}",
                    "-f",
                    "-",
                    body={
                        "apiVersion": "v1",
                        "kind": "DeleteOptions",
                        "preconditions": {"uid": observation["runner"]["pod_uid"]},
                        "gracePeriodSeconds": 15,
                    },
                )
            sandbox_ref = observation.get("sandbox", {})
            cr = (
                await self._get("batchsandbox", sandbox_ref["id"], namespace)
                if sandbox_ref.get("id")
                else None
            )
            if cr and cr["metadata"]["uid"] != sandbox_ref.get("cr_uid"):
                raise ResourceUnknown("The original logical Sandbox CR identity changed")
            if cr:
                connection = self._connection(tenant_key)
                manager = await SandboxManager.create(connection_config=connection)
                try:
                    try:
                        await manager.kill_sandbox(observation["sandbox"]["id"])
                    except Exception:
                        # The original exit watch and native CR readback decide uncertain kill ACKs.
                        pass
                finally:
                    await self._close_sdk(manager, connection)
            for key, watch in watches.items():
                result[key + "_terminated"] = await watch
            if sandbox_ref.get("id") and result["sandbox_terminated"]:
                if not sandbox_ref.get("cr_uid"):
                    raise ResourceUnknown("The original logical Sandbox UID was not bound")
                until = time.monotonic() + 30
                while True:
                    cr = await self._get("batchsandbox", sandbox_ref["id"], namespace)
                    if cr and cr["metadata"]["uid"] != sandbox_ref.get("cr_uid"):
                        raise ResourceUnknown("The original logical Sandbox was replaced")
                    pods = json.loads(
                        await self._kubectl("get", "pods", "-n", namespace, "-o", "json")
                    )["items"]
                    for pod in pods:
                        if any(
                            owner.get("uid") == sandbox_ref.get("cr_uid")
                            for owner in pod["metadata"].get("ownerReferences", [])
                        ):
                            projection = self._pod_projection(pod)
                            if (
                                projection["pod_uid"] != sandbox_ref.get("pod_uid")
                                or projection.get("container_id") != sandbox_ref.get("container_id")
                                or not projection["terminated"]
                            ):
                                raise ResourceUnknown(
                                    "The logical Sandbox still has another execution"
                                )
                    if cr is None:
                        break
                    if time.monotonic() >= until:
                        raise ResourceUnknown("The logical Sandbox deletion has not converged")
                    await asyncio.sleep(0.2)
            for key in ("journal_pvc", "workspace_pvc"):
                if not observation.get(key):
                    continue
                self._owned(
                    await self._get("pvc", observation[key]["name"], namespace),
                    labels,
                    observation[key]["uid"],
                )
            result["pvcs_retained"] = True
            return result
        finally:
            for watch in watches.values():
                if not watch.done():
                    watch.cancel()
            await asyncio.gather(*watches.values(), return_exceptions=True)
