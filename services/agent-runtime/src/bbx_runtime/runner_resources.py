"""Trusted native resource I/O; Blackboard owns dispatch, identities and cleanup decisions."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
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
from bbx_sandbox_execution.connect import read_execution_context
from opensandbox import Sandbox, SandboxManager
from opensandbox.adapters.factory import AdapterFactory
from opensandbox.config import ConnectionConfig
from opensandbox.constants import DEFAULT_EXECD_PORT
from opensandbox.models.sandboxes import PVC, SandboxFilter, SandboxImageSpec, Volume
from opensandbox.transport import RetryPolicy

CLEANUP_GRACE_SECONDS = 120
SANDBOX_HOST_PROFILE = "node-sandbox-host-v1"
SANDBOX_HOST_SECRET = "bbx-sandbox-host"
SANDBOX_HOST_MOUNT = "/var/run/bbx-sandbox-host"
SANDBOX_HOST_PORT = 8766


class ResourceUnknown(RuntimeError):
    """No replay or cleanup claim can be inferred from this local observation."""


class RunnerResources:
    def __init__(self, config: dict):
        self.config = dict(config)
        profile = config.get("runner_profile")
        if profile not in {None, SANDBOX_HOST_PROFILE}:
            raise ValueError("Unknown trusted Runner resource profile")
        self.sandbox_host = profile == SANDBOX_HOST_PROFILE
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
        images = ["runner_image", "sandbox_image"]
        if self.sandbox_host:
            images.append("sandbox_host_image")
            network = self.config.get("sandbox_network")
            if not isinstance(network, dict) or set(network) != {
                "namespace",
                "network_identity",
                "ipv4",
            }:
                raise ValueError("A frozen Sandbox Assignment network is required")
            self._name(network["namespace"])
            self._name(network["network_identity"])
            ipaddress.IPv4Address(network["ipv4"])
            if self.config.get("sandbox_host_execd_origin"):
                self._execd_origin(self.config["sandbox_host_execd_origin"])
        for key in images:
            if not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", self.config.get(key, "")):
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

    async def _kubectl(
        self, *args: str, body: dict | None = None, expected_denial: tuple[str, str] | None = None
    ) -> str:
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
            out, err = await asyncio.wait_for(
                process.communicate(None if body is None else json.dumps(body).encode()), timeout=25
            )
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        if expected_denial is not None:
            policy, message = expected_denial
            if (
                process.returncode
                and f"ValidatingAdmissionPolicy '{policy}'".encode() in err
                and f"with binding '{policy}' denied request: {message}".encode() in err
                and b"resulted in error" not in err
            ):
                return "denied-by-original-retirement-policy"
            raise ResourceUnknown("The original retirement admission fence is not confirmed")
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

    @staticmethod
    def _execd_origin(value):
        if not isinstance(value, str) or not value or any(c.isspace() for c in value):
            raise ResourceUnknown("A fixed native execd ingress authority is required")
        parsed = urlsplit("http://" + value)
        if (
            not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ResourceUnknown("The native execd ingress authority is invalid")
        return parsed.netloc

    async def _get(self, kind, name, namespace=None):
        if not isinstance(name, str) or len(name) > 253:
            raise ResourceUnknown("Invalid trusted object name")
        for label in name.split("."):
            self._name(label)
        args = ["get", kind, name, "--ignore-not-found", "-o", "json"]
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
        if self.sandbox_host:
            for target, port in (
                ({"k8s:bbx.dev/component": "native-mcp-gateway"}, "3000"),
                (
                    {
                        "k8s:app.kubernetes.io/component": "ingress-gateway",
                        "k8s:app.kubernetes.io/part-of": "opensandbox",
                    },
                    "28888",
                ),
            ):
                runner_policy["spec"]["egress"].append(
                    {
                        "toEndpoints": [
                            {
                                "matchLabels": {
                                    "k8s:io.kubernetes.pod.namespace": self.config[
                                        "system_namespace"
                                    ],
                                    **target,
                                }
                            }
                        ],
                        "toPorts": [{"ports": [{"port": port, "protocol": "TCP"}]}],
                    }
                )
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
        if self.sandbox_host:
            host_mount = await self._ensure(
                self._document(
                    "Secret",
                    SANDBOX_HOST_SECRET,
                    labels,
                    namespace,
                    type="Opaque",
                    stringData={"ca.pem": expected_ca},
                ),
                labels,
                observed.get("sandbox_host_secret", {}).get("uid"),
            )
            self._host_secret(host_mount)
            if (
                base64.b64decode(host_mount.get("data", {}).get("ca.pem", "")).decode()
                != expected_ca
            ):
                raise ResourceUnknown("The Sandbox host startup trust anchor changed")
            refs["sandbox_host_secret"] = {
                "name": SANDBOX_HOST_SECRET,
                "uid": host_mount["metadata"]["uid"],
            }
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
        while True:
            projection = self._pod_projection(runner, with_host=self.sandbox_host)
            if projection["terminated"] or projection.get("observation") == "unknown":
                raise ResourceUnknown("An original Runner container exited before readiness")
            if projection.get("container_id") and (
                not self.sandbox_host
                or (projection.get("host_container_id") and projection.get("pod_ip"))
            ):
                break
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
            "runner": projection,
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
        document = self._document(
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

        if self.sandbox_host:
            spec = document["spec"]
            spec["shareProcessNamespace"] = False
            spec["containers"].append(
                {
                    "name": "sandbox-host",
                    "image": self.config["sandbox_host_image"],
                    "imagePullPolicy": "IfNotPresent",
                    "command": ["python", "-m", "bbx_runtime.sandbox_mcp"],
                    "args": ["--config", SANDBOX_HOST_MOUNT + "/host.json"],
                    "env": [
                        {
                            "name": "BBX_POD_UID",
                            "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}},
                        },
                        {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
                    ],
                    "ports": [{"containerPort": SANDBOX_HOST_PORT, "protocol": "TCP"}],
                    "resources": {
                        "requests": {"cpu": "25m", "memory": "64Mi"},
                        "limits": {"cpu": "200m", "memory": "192Mi"},
                    },
                    "securityContext": {
                        **spec["containers"][0]["securityContext"],
                        "runAsUser": 1001,
                        "runAsGroup": 1001,
                    },
                    "volumeMounts": [
                        {"name": "sandbox-host", "mountPath": SANDBOX_HOST_MOUNT, "readOnly": True}
                    ],
                }
            )
            spec["volumes"].append(
                {
                    "name": "sandbox-host",
                    "secret": {"secretName": SANDBOX_HOST_SECRET, "defaultMode": 0o440},
                }
            )
        return document

    @staticmethod
    def _runner_secret(secret):
        if secret.get("type") != "Opaque" or not set(secret.get("data", {})) <= {
            "runner-bootstrap.json",
            "credentials.json",
            "ca.pem",
        }:
            raise ResourceUnknown("The Runner Secret contains undeclared material")

    @staticmethod
    def _host_secret(secret):
        data = secret.get("data", {})
        if (
            secret.get("type") != "Opaque"
            or "ca.pem" not in data
            or not set(data) <= {"ca.pem", "host.json"}
        ):
            raise ResourceUnknown("The Sandbox host Secret contains undeclared material")

    @staticmethod
    def _runner_spec(pod, expected):
        spec = pod["spec"]
        if (
            pod["metadata"].get("ownerReferences")
            or len(spec.get("containers", [])) != len(expected["containers"])
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
        for actual, desired in zip(spec["containers"], expected["containers"], strict=True):
            env = json.loads(json.dumps(actual.get("env", [])))
            for entry in env:
                if "fieldRef" in entry.get("valueFrom", {}):
                    if entry["valueFrom"]["fieldRef"].pop("apiVersion", "v1") != "v1":
                        raise ResourceUnknown("The Runner Downward API changed")
            if env != desired["env"] or any(
                actual.get(k) != desired.get(k)
                for k in (
                    "name",
                    "image",
                    "imagePullPolicy",
                    "resources",
                    "securityContext",
                    "volumeMounts",
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
            ):
                raise ResourceUnknown("The original Runner container contract changed")

    @staticmethod
    def _pod_projection(pod, *, with_host=False):
        spec = pod.get("spec", {})
        containers = spec.get("containers", [])
        names = [c["name"] for c in containers]
        states = pod.get("status", {}).get("containerStatuses", [])
        by_name = {state.get("name"): state for state in states}
        if (
            (names != ["runner", "sandbox-host"] if with_host else len(names) != 1)
            or spec.get("ephemeralContainers")
            or pod.get("status", {}).get("ephemeralContainerStatuses")
            or len(by_name) != len(states)
            or not set(by_name) <= set(names)
            or any(c.get("restartPolicy") == "Always" for c in spec.get("initContainers", []))
            or any(
                "running" in st.get("state", {}) or "waiting" in st.get("state", {})
                for st in pod.get("status", {}).get("initContainerStatuses", [])
            )
        ):
            raise ResourceUnknown("The native Pod has an unapproved container scope")
        primary = by_name.get(names[0], {})
        host = by_name.get("sandbox-host", {}) if with_host else {}
        primary_ended = "terminated" in primary.get("state", {})
        host_ended = "terminated" in host.get("state", {})
        ended = primary_ended and (not with_host or host_ended)
        return {
            "pod_name": pod["metadata"]["name"],
            "pod_uid": pod["metadata"]["uid"],
            "phase": pod.get("status", {}).get("phase", "Pending"),
            "terminated": ended,
            "container_name": names[0],
            **(
                {"resource_version": pod["metadata"]["resourceVersion"]}
                if pod["metadata"].get("resourceVersion")
                else {}
            ),
            **({"container_id": primary["containerID"]} if primary.get("containerID") else {}),
            **({"exit_code": primary["state"]["terminated"]["exitCode"]} if primary_ended else {}),
            **({"host_container_id": host["containerID"]} if host.get("containerID") else {}),
            **({"host_terminated": host_ended} if with_host else {}),
            **(
                {"pod_ip": pod["status"]["podIP"]}
                if with_host and pod.get("status", {}).get("podIP")
                else {}
            ),
            **({"observation": "unknown"} if with_host and primary_ended != host_ended else {}),
        }

    @staticmethod
    def _same_process(projection, reference):
        return all(
            projection.get(key) == reference.get(key)
            for key in ("pod_uid", "container_id", "host_container_id")
        )

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

    async def _original_host_runner(self, operation):
        if not self.sandbox_host:
            raise ResourceUnknown("The original Runner has no Sandbox host profile")
        observed = operation["observation"]
        namespace, labels = self._scope(operation)
        await self._cluster()
        self._owned(await self._get("namespace", namespace), labels, observed["namespace_uid"])
        pod = self._owned(
            await self._get("pod", "bbx-runner", namespace),
            {**labels, "bbx.dev/component": "runner"},
            observed["runner"]["pod_uid"],
        )
        self._runner_spec(pod, self._runner_document(namespace, labels)["spec"])
        actual = self._pod_projection(pod, with_host=True)
        if (
            pod["metadata"].get("deletionTimestamp")
            or not self._same_process(actual, observed["runner"])
            or not actual.get("host_container_id")
            or not actual.get("pod_ip")
            or any(
                "running" not in state.get("state", {})
                for state in pod["status"]["containerStatuses"]
            )
            or actual.get("pod_ip") != observed["runner"].get("pod_ip")
            or actual["terminated"]
            or actual.get("observation") == "unknown"
        ):
            raise ResourceUnknown("The original Sandbox host process is not live")
        return namespace, labels, pod

    async def sandbox_host_context(self, operation, tenant_key):
        namespace, labels, runner = await self._original_host_runner(operation)
        observed = operation["observation"]
        original = observed["sandbox"]
        pod = await self._get("pod", original["pod_name"], namespace)
        if (
            not pod
            or pod["metadata"].get("deletionTimestamp")
            or not any(
                owner.get("uid") == original["cr_uid"]
                for owner in pod["metadata"].get("ownerReferences", [])
            )
            or not self._same_process(self._pod_projection(pod), original)
            or self._pod_projection(pod)["terminated"]
            or self._sandbox_spec(pod) != original["environment_revision"]
        ):
            raise ResourceUnknown("The original Sandbox Pod identity changed")
        secret = self._owned(
            await self._get("secret", "bbx-execd", namespace),
            labels,
            observed["execd_secret"]["uid"],
        )
        if secret.get("type") != "Opaque" or set(secret.get("data", {})) != {"token"}:
            raise ResourceUnknown("The original execd identity is unavailable")
        token = base64.b64decode(secret["data"]["token"], validate=True).decode()
        if not token.strip():
            raise ResourceUnknown("The original execd identity is unavailable")
        connection = self._connection(tenant_key, token)
        sandbox = None
        try:
            sandbox = await Sandbox.connect(
                original["id"], connection_config=connection, connect_timeout=timedelta(seconds=30)
            )
            endpoint = await sandbox.get_endpoint(DEFAULT_EXECD_PORT)
            context = await read_execution_context(sandbox)
        except Exception:
            raise ResourceUnknown(
                "The original native execd endpoint or context is unresolved"
            ) from None
        finally:
            if sandbox is not None:
                await sandbox.close()
            if connection.transport is not None:
                await connection.transport.aclose()
        headers = endpoint.headers
        if (
            set(headers) != {"OpenSandbox-Ingress-To", "OpenSandbox-Secure-Access"}
            or headers["OpenSandbox-Ingress-To"] != f"{original['id']}-{DEFAULT_EXECD_PORT}"
            or not headers["OpenSandbox-Secure-Access"].strip()
            or context["pod_uid"] != original["pod_uid"]
            or context["environment_revision"] != original["environment_revision"]
        ):
            raise ResourceUnknown("The original native execd route or execution identity changed")
        origin = self._execd_origin(endpoint.endpoint)
        # Runtime and its sidecar may reach the same ingress through different addresses.
        if self.config.get("sandbox_host_execd_origin"):
            origin = self._execd_origin(self.config["sandbox_host_execd_origin"])
        address = str(ipaddress.IPv4Address(runner["status"]["podIP"]))
        return {
            "sandbox_id": original["id"],
            "sandbox_pod_uid": original["pod_uid"],
            "environment_revision": original["environment_revision"],
            "execution_context_id": context["execution_context_id"],
            "execd_endpoint": origin,
            "execd_headers": headers,
            "execd_token": token,
            "host_url": f"http://{address}:{SANDBOX_HOST_PORT}",
        }

    async def publish_sandbox_host(self, operation, configuration):
        namespace, labels, _ = await self._original_host_runner(operation)
        observed = operation["observation"]
        expected = {
            "version": 1,
            "task_id": operation["task_id"],
            "operation_id": operation["operation_id"],
            "runner_pod_uid": observed["runner"]["pod_uid"],
            "sandbox_id": observed["sandbox"]["id"],
            "sandbox_pod_uid": observed["sandbox"]["pod_uid"],
            "environment_revision": observed["sandbox"]["environment_revision"],
            "network": self.config["sandbox_network"],
            "ca_file": SANDBOX_HOST_MOUNT + "/ca.pem",
        }
        fields = set(expected) | {
            "execution_context_id",
            "blackboard_url",
            "inbound_key",
            "report_key",
            "execd_endpoint",
            "execd_headers",
            "execd_token",
            "hard_deadline",
        }
        if (
            set(configuration) != fields
            or any(configuration[k] != value for k, value in expected.items())
            or configuration["inbound_key"] == configuration["report_key"]
            or not re.fullmatch(r"[a-f0-9]{64}", configuration["execution_context_id"])
        ):
            raise ResourceUnknown("The Sandbox host mount does not match the original Task")
        secret = self._owned(
            await self._get("secret", SANDBOX_HOST_SECRET, namespace),
            labels,
            observed["sandbox_host_secret"]["uid"],
        )
        self._host_secret(secret)
        existing = secret["data"].get("host.json")
        if existing:
            if json.loads(base64.b64decode(existing)) != configuration:
                raise ResourceUnknown("The original Sandbox host startup material cannot change")
            return
        secret["data"]["host.json"] = base64.b64encode(
            json.dumps(configuration, ensure_ascii=False, separators=(",", ":")).encode()
        ).decode()
        await self._kubectl("replace", "-f", "-", body=secret)

    async def wait_sandbox_host_ready(self, operation, configuration):
        namespace, _, pod = await self._original_host_runner(operation)
        process = await asyncio.create_subprocess_exec(
            "kubectl",
            "--kubeconfig",
            self.config["kubeconfig"],
            "--context",
            self.config["context"],
            "-n",
            namespace,
            "port-forward",
            "pod/" + pod["metadata"]["name"],
            f":{SANDBOX_HOST_PORT}",
            "--address",
            "127.0.0.1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            async with asyncio.timeout(120):
                assert process.stdout is not None
                port = None
                while port is None:
                    line = await process.stdout.readline()
                    if not line:
                        raise ResourceUnknown("The original Sandbox host readiness forward failed")
                    match = re.fullmatch(rb"Forwarding from 127\.0\.0\.1:(\d+) -> 8766\s*", line)
                    if match:
                        port = int(match[1])
                async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
                    while process.returncode is None:
                        try:
                            response = await client.get(
                                f"http://127.0.0.1:{port}/ready",
                                headers={"x-bbx-sandbox-inbound": configuration["inbound_key"]},
                            )
                        except httpx.RequestError:
                            await asyncio.sleep(0.2)
                            continue
                        if response.status_code == 200:
                            value = response.json()
                            if (
                                not isinstance(value, dict)
                                or value.get("ready") is not True
                                or any(
                                    value.get(k) != configuration[k]
                                    for k in (
                                        "task_id",
                                        "operation_id",
                                        "runner_pod_uid",
                                        "sandbox_id",
                                        "sandbox_pod_uid",
                                        "execution_context_id",
                                    )
                                )
                            ):
                                raise ResourceUnknown(
                                    "Sandbox host readiness belongs to a different identity"
                                )
                            await self._original_host_runner(operation)
                            return
                        if response.status_code < 500:
                            raise ResourceUnknown(
                                "The original Sandbox host readiness authority was refused"
                            )
                        await asyncio.sleep(0.2)
        except (TimeoutError, ValueError):
            raise ResourceUnknown("The original Sandbox host readiness is unresolved") from None
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except TimeoutError:
                    process.kill()
                    await process.wait()
        raise ResourceUnknown("The original Sandbox host readiness is unresolved")

    async def publish_mount(
        self, observation, bootstrap, credentials, *, control_only=False, retain_binding_ids=()
    ):
        namespace, labels = self._scope(observation)
        await self._cluster()
        pod = self._owned(
            await self._get("pod", observation["runner"]["pod_name"], namespace),
            {**labels, "bbx.dev/component": "runner"},
            observation["runner"]["pod_uid"],
        )
        projection = self._pod_projection(pod, with_host=self.sandbox_host)
        if (
            projection["terminated"]
            or projection.get("observation") == "unknown"
            or not self._same_process(projection, observation["runner"])
            or (self.sandbox_host and not projection.get("host_container_id"))
            or bootstrap["fence"]["pod_uid"] != pod["metadata"]["uid"]
            or bootstrap["fence"]["task_id"] != labels["bbx.dev/task-id"]
        ):
            raise ResourceUnknown("The Runner mount is not bound to the original live Pod")
        self._runner_spec(pod, self._runner_document(namespace, labels)["spec"])
        if self.sandbox_host:
            host = self._owned(
                await self._get("secret", SANDBOX_HOST_SECRET, namespace),
                labels,
                observation["sandbox_host_secret"]["uid"],
            )
            self._host_secret(host)
            if "host.json" not in host["data"]:
                raise ResourceUnknown("The original Sandbox host has no frozen startup material")
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
        if retain_binding_ids:
            if not isinstance(retain_binding_ids, (list, tuple)) or any(
                not isinstance(bid, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", bid)
                for bid in retain_binding_ids
            ):
                raise ResourceUnknown("Invalid retained Runner binding identities")
            try:
                existing = self._scoped_credentials(
                    json.loads(base64.b64decode(secret["data"]["credentials.json"], validate=True))
                    if "credentials.json" in secret.get("data", {})
                    else {},
                    labels["bbx.dev/task-id"],
                )
            except (KeyError, ValueError, UnicodeError):
                raise ResourceUnknown("The original credential mount is unavailable") from None
            credentials = dict(credentials)
            for bid in retain_binding_ids:
                for audience in ("model", "mcp"):
                    ref = f"native-{audience}-{bid}"
                    if ref in credentials:
                        raise ResourceUnknown("A draining credential cannot be issued again")
                    previous = existing.get(ref)
                    if previous is None:
                        continue
                    if (
                        previous.get("binding_id") != bid
                        or previous.get("audience") != audience
                        or type(previous.get("expires_at")) is not int
                        or not isinstance(previous.get("token"), str)
                        or not previous["token"]
                    ):
                        raise ResourceUnknown("The original draining credential identity changed")
                    if previous["expires_at"] > time.time():
                        credentials[ref] = previous
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
        retained = ("journal_pvc", "workspace_pvc", "execd_secret", "runner_secret")
        if self.sandbox_host:
            retained += ("sandbox_host_secret",)
        for key in retained:
            name = {
                "journal_pvc": "bbx-journal",
                "workspace_pvc": "bbx-workspace",
                "execd_secret": "bbx-execd",
                "runner_secret": "bbx-runner",
                "sandbox_host_secret": SANDBOX_HOST_SECRET,
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
            elif key == "sandbox_host_secret":
                self._host_secret(value)
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
            projection = self._pod_projection(runner, with_host=self.sandbox_host)
            if any(
                prior.get("runner", {}).get(key) and prior["runner"][key] != projection.get(key)
                for key in ("container_id", "host_container_id", "pod_ip")
            ):
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
            projection = (
                self._pod_projection(pod, with_host=self.sandbox_host and key == "runner")
                if pod
                else {}
            )
            if not pod or not self._same_process(projection, ref):
                result[key] = {**ref, "terminated": False, "observation": "unknown"}
            else:
                if key == "runner":
                    self._runner_spec(pod, self._runner_document(namespace, labels)["spec"])
                result[key] = {**ref, **projection}
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
                    projection = self._pod_projection(
                        current, with_host=bool(reference.get("host_container_id"))
                    )
                    if projection["container_name"] != reference["container_name"] or any(
                        projection.get(k) not in {None, reference.get(k)}
                        for k in ("container_id", "host_container_id")
                    ):
                        failure = "watch_container_changed"
                        break
                    if projection["terminated"] and self._same_process(projection, reference):
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
                if key == "runner" and self.sandbox_host and not ref.get("host_container_id"):
                    continue
                pod = await self._get("pod", ref["pod_name"], namespace)
                if pod:
                    if pod["metadata"]["uid"] != ref["pod_uid"]:
                        raise ResourceUnknown("The original Pod identity changed")
                    projection = self._pod_projection(
                        pod, with_host=self.sandbox_host and key == "runner"
                    )
                    if not self._same_process(projection, ref):
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

    async def _retirement_topology(self):
        frozen = self.config.get("retirement_single_apiserver") or {}
        pods = json.loads(
            await self._kubectl(
                "get", "pods", "-n", "kube-system", "-l", "component=kube-apiserver", "-o", "json"
            )
        )["items"]
        if len(pods) != 1 or pods[0]["metadata"]["uid"] != frozen.get("pod_uid"):
            raise ResourceUnknown("Automatic retirement requires the frozen single API server")
        pod = pods[0]
        if not any(
            c.get("type") == "Ready" and c.get("status") == "True"
            for c in pod.get("status", {}).get("conditions", [])
        ):
            raise ResourceUnknown("The single API server is not ready")
        node = await self._get("node", pod["spec"]["nodeName"])
        endpoints = await self._get("endpoints", "kubernetes", "default")
        addresses = [
            a for s in (endpoints or {}).get("subsets", []) for a in s.get("addresses", [])
        ]
        if not node or node["metadata"]["uid"] != frozen.get("node_uid") or len(addresses) != 1:
            raise ResourceUnknown("The original single API server topology changed")
        return dict(frozen)

    async def _retirement_inventory(self, namespace):
        groups = json.loads(await self._kubectl("get", "--raw", "/apis"))["groups"]
        versions = [
            "/api/" + version
            for version in json.loads(await self._kubectl("get", "--raw", "/api"))["versions"]
        ]
        versions += [
            "/apis/" + version["groupVersion"] for group in groups for version in group["versions"]
        ]
        found = []
        seen = {}
        for version in versions:
            discovery = json.loads(await self._kubectl("get", "--raw", version))
            for resource in discovery["resources"]:
                if not resource.get("namespaced") or "/" in resource["name"]:
                    continue
                if "list" not in resource.get("verbs", []) and "get" not in resource.get(
                    "verbs", []
                ):
                    if (version, resource["name"]) in {
                        ("/api/v1", "bindings"),
                        ("/apis/authorization.k8s.io/v1", "localsubjectaccessreviews"),
                    }:
                        continue
                    raise ResourceUnknown("An unknown namespaced API cannot be inventoried")
                if "list" not in resource.get("verbs", []):
                    raise ResourceUnknown("A namespaced resource cannot be inventoried")
                path = version + "/namespaces/" + namespace + "/" + resource["name"]
                listing = json.loads(await self._kubectl("get", "--raw", path))
                if listing.get("metadata", {}).get("continue"):
                    raise ResourceUnknown("The retirement inventory is incomplete")
                for value in listing["items"]:
                    value.setdefault("kind", resource["kind"])
                    meta = value["metadata"]
                    identity = (value["kind"], meta["name"], meta.get("namespace"))
                    if not meta.get("uid"):
                        raise ResourceUnknown("A namespaced resource has no native UID")
                    if meta["uid"] in seen:
                        if seen[meta["uid"]] != identity:
                            raise ResourceUnknown(
                                "A native UID has conflicting resource identities"
                            )
                        continue
                    seen[meta["uid"]] = identity
                    found.append((path + "/" + value["metadata"]["name"], value))
        return found

    def _retirement_fence(self, operation, labels, namespace_uid):
        namespace = operation["namespace"]
        name = "bbx-retire-" + str(UUID(operation["retirement_id"]))
        message = "BBX retirement " + str(UUID(operation["retirement_id"]))
        policy = self._document("ValidatingAdmissionPolicy", name, labels)
        policy["apiVersion"] = "admissionregistration.k8s.io/v1"
        policy["spec"] = {
            "failurePolicy": "Fail",
            "paramKind": {"apiVersion": "v1", "kind": "Namespace"},
            "matchConstraints": {
                "matchPolicy": "Equivalent",
                "objectSelector": {},
                "resourceRules": [
                    {
                        "apiGroups": ["*"],
                        "apiVersions": ["*"],
                        "operations": ["CREATE"],
                        "resources": ["*"],
                        "scope": "Namespaced",
                    }
                ],
                "namespaceSelector": {
                    "matchLabels": {
                        "kubernetes.io/metadata.name": namespace,
                        "bbx.dev/managed-by": "runner-resources",
                        "bbx.dev/retiring": "true",
                    }
                },
            },
            "validations": [
                {
                    "expression": "params.metadata.uid != " + json.dumps(namespace_uid),
                    "message": message,
                }
            ],
        }
        binding = self._document("ValidatingAdmissionPolicyBinding", name, labels)
        binding["apiVersion"] = policy["apiVersion"]
        binding["spec"] = {
            "policyName": name,
            "validationActions": ["Deny"],
            "paramRef": {"name": namespace, "parameterNotFoundAction": "Deny"},
        }
        return policy, binding, message

    async def _retirement_plan(self, operation, manifest):
        namespace, labels = self._scope(operation)
        observed = operation["observation"]
        ns = await self._get("namespace", namespace)
        if ns is None:
            if not manifest.get("namespace_delete_started"):
                raise ResourceUnknown("The original Namespace disappeared before retirement")
            return manifest
        self._owned(ns, labels, observed["namespace_uid"])
        native_ca = await self._get("configmap", "kube-root-ca.crt", "kube-system")
        inventory = await self._retirement_inventory(namespace)
        allowed = {
            ("ServiceAccount", "bbx-runner"),
            ("ResourceQuota", "bbx-task"),
            ("CiliumNetworkPolicy", "bbx-task"),
            ("CiliumNetworkPolicy", "bbx-runner-control"),
        }
        original = {observed["namespace_uid"]}
        for value in observed.values():
            if isinstance(value, dict):
                original.update(value[k] for k in ("uid", "pod_uid", "cr_uid") if value.get(k))
        saved = {r["path"]: r for r in manifest.get("resources", [])}
        original.update(r["uid"] for r in saved.values())
        resources = list(saved.values())
        native = []
        for path, value in inventory:
            meta, kind = value["metadata"], value["kind"]
            name, uid = meta["name"], meta["uid"]
            if path in saved:
                if saved[path]["uid"] != uid:
                    raise ResourceUnknown("A retired resource name was rebound to another UID")
            if kind in {"Event", "CiliumEndpoint", "Endpoints", "EndpointSlice"}:
                native.append((path, value))
                continue
            if kind == "ServiceAccount" and name == "default":
                if (
                    value.get("secrets")
                    or value.get("imagePullSecrets")
                    or meta.get("ownerReferences")
                ):
                    raise ResourceUnknown("The default service account has foreign attachments")
                resources.append(
                    {"path": path, "uid": uid, "kind": kind, "name": name, "namespace_gc": True}
                )
                original.add(uid)
                continue
            if kind == "ConfigMap" and name == "kube-root-ca.crt":
                if value.get("data") != (native_ca or {}).get("data") or value.get("binaryData"):
                    raise ResourceUnknown("The native root CA ConfigMap changed")
                resources.append(
                    {"path": path, "uid": uid, "kind": kind, "name": name, "namespace_gc": True}
                )
                original.add(uid)
                continue
            expected = None
            for key in (
                "journal_pvc",
                "workspace_pvc",
                "runner_secret",
                "execd_secret",
                "sandbox_host_secret",
            ):
                ref = observed.get(key, {})
                if name == ref.get("name") and kind == (
                    "PersistentVolumeClaim" if key.endswith("pvc") else "Secret"
                ):
                    expected = ref["uid"]
            if kind == "Pod":
                ref = next(
                    (
                        observed[k]
                        for k in ("runner", "sandbox")
                        if observed.get(k, {}).get("pod_name") == name
                    ),
                    {},
                )
                expected = ref.get("pod_uid")
                projection = self._pod_projection(
                    value, with_host=self.sandbox_host and name == "bbx-runner"
                )
                if (
                    not expected
                    or not projection["terminated"]
                    or not self._same_process(projection, ref)
                ):
                    raise ResourceUnknown("Retirement still has an unresolved execution Pod")
            if kind == "BatchSandbox" or (expected is None and (kind, name) not in allowed):
                raise ResourceUnknown(
                    "The Task namespace contains a foreign or unresolved resource"
                )
            self._owned(value, labels, expected)
            if kind == "Secret":
                if name == "bbx-runner":
                    self._runner_secret(value)
                elif name == SANDBOX_HOST_SECRET and self.sandbox_host:
                    self._host_secret(value)
                elif value.get("type") != "Opaque" or set(value.get("data", {})) != {"token"}:
                    raise ResourceUnknown("The original execd Secret changed")
            resources.append({"path": path, "uid": uid, "kind": kind, "name": name})
            original.add(uid)
        for path, value in native:
            meta = value["metadata"]
            related = value.get("regarding", value.get("involvedObject", {}))
            owners = meta.get("ownerReferences", [])
            if not (
                (value["kind"] == "Event" and related.get("uid") in original)
                or (owners and all(o.get("uid") in original for o in owners))
            ):
                raise ResourceUnknown("A native metadata object has no original UID reference")
            resources.append(
                {
                    "path": path,
                    "uid": meta["uid"],
                    "kind": value["kind"],
                    "name": meta["name"],
                    "namespace_gc": True,
                }
            )
        volumes = manifest.get("volumes", [])
        if not volumes:
            for key in ("journal_pvc", "workspace_pvc"):
                ref = observed.get(key)
                if not ref:
                    raise ResourceUnknown("Retirement lacks an original retained PVC identity")
                claim = self._owned(
                    await self._get("pvc", ref["name"], namespace), labels, ref["uid"]
                )
                if claim["spec"].get("storageClassName") != self.config["storage_class"]:
                    raise ResourceUnknown("The original PVC storage class changed")
                if not claim["spec"].get("volumeName"):
                    raise ResourceUnknown(
                        "An unbound retained PVC has no frozen provisioning history"
                    )
                pv = await self._get("pv", claim["spec"].get("volumeName", ""))
                if (
                    not pv
                    or pv["spec"].get("persistentVolumeReclaimPolicy") != "Delete"
                    or pv["spec"].get("claimRef", {}).get("uid") != ref["uid"]
                    or pv["spec"]["claimRef"].get("namespace") != namespace
                    or pv["spec"]["claimRef"].get("name") != ref["name"]
                    or pv["spec"].get("storageClassName") != self.config["storage_class"]
                ):
                    raise ResourceUnknown("The retained PV binding or Delete policy is not proven")
                volumes.append(
                    {
                        "name": pv["metadata"]["name"],
                        "uid": pv["metadata"]["uid"],
                        "claim_uid": ref["uid"],
                        "claim_name": ref["name"],
                        "claim_namespace": namespace,
                        "storage_class": self.config["storage_class"],
                        "reclaim_policy": "Delete",
                    }
                )
        else:
            for ref in volumes:
                claims = [observed.get(k, {}) for k in ("journal_pvc", "workspace_pvc")]
                if (
                    not any(
                        c.get("uid") == ref.get("claim_uid")
                        and c.get("name") == ref.get("claim_name")
                        for c in claims
                    )
                    or ref.get("claim_namespace") != namespace
                    or ref.get("storage_class") != self.config["storage_class"]
                    or ref.get("reclaim_policy") != "Delete"
                ):
                    raise ResourceUnknown("The frozen PV claim projection changed")
                if ref.get("unbound"):
                    raise ResourceUnknown("Unbound retained PVC retirement is not supported")
                pv = await self._get("pv", ref["name"])
                if pv and (
                    pv["metadata"]["uid"] != ref["uid"]
                    or pv["spec"].get("persistentVolumeReclaimPolicy") != "Delete"
                    or pv["spec"].get("claimRef", {}).get("uid") != ref["claim_uid"]
                    or pv["spec"]["claimRef"].get("namespace") != namespace
                    or pv["spec"]["claimRef"].get("name") != ref["claim_name"]
                    or pv["spec"].get("storageClassName") != ref["storage_class"]
                ):
                    raise ResourceUnknown("The original PV retirement identity changed")
        if len(volumes) != 2 or {v["claim_uid"] for v in volumes} != {
            observed[k]["uid"] for k in ("journal_pvc", "workspace_pvc")
        }:
            raise ResourceUnknown("The retirement manifest does not cover both original claims")
        return {
            **manifest,
            "task_id": operation["task_id"],
            "operation_id": operation["operation_id"],
            "retirement_id": operation["retirement_id"],
            "namespace": {"name": namespace, "uid": observed["namespace_uid"]},
            "namespace_uid": observed["namespace_uid"],
            "source_id": self.config["source_id"],
            "cluster_uid": self.config["cluster_uid"],
            "resources": list({ref["path"]: ref for ref in resources}.values()),
            "volumes": volumes,
        }

    async def retire(self, operation, *, checkpoint, admit_retire):
        """Retire only the explicitly authorized original retained resources, never infer exit."""
        namespace, labels = self._scope(operation)
        await self._cluster()
        topology = await self._retirement_topology()
        manifest = json.loads(json.dumps(operation.get("retirement") or {}))
        if manifest.get("authority") != topology:
            raise ResourceUnknown("The original retirement topology authority changed")
        for key in ("task_id", "operation_id", "retirement_id"):
            if key in manifest and manifest[key] != operation[key]:
                raise ResourceUnknown("The original retirement operation changed")
        if manifest.get("namespace") not in (
            None,
            {"name": namespace, "uid": operation["observation"]["namespace_uid"]},
        ):
            raise ResourceUnknown("The original retirement Namespace changed")
        if any(
            k in manifest and manifest[k] != self.config[k] for k in ("source_id", "cluster_uid")
        ):
            raise ResourceUnknown("The persisted retirement resource source changed")
        manifest = await self._retirement_plan(operation, manifest)
        policy, binding, message = self._retirement_fence(
            operation, labels, manifest["namespace_uid"]
        )
        expected_fence = {"policy": policy, "binding": binding}
        if manifest.get("fence") and manifest["fence"] != expected_fence:
            raise ResourceUnknown("The original retirement admission configuration changed")
        manifest.update(fence=expected_fence, topology=topology)
        await checkpoint(manifest)

        async def admit():
            if not await admit_retire():
                raise ResourceUnknown("The original retirement authority was revoked")

        async def delete(ref):
            current = json.loads(await self._kubectl("get", "--raw", ref["path"]))
            if current["metadata"]["uid"] != ref["uid"]:
                raise ResourceUnknown("Retirement cannot delete a replacement UID")
            await admit()
            await self._kubectl(
                "delete",
                "--raw",
                ref["path"],
                "-f",
                "-",
                body={
                    "apiVersion": "v1",
                    "kind": "DeleteOptions",
                    "preconditions": {"uid": ref["uid"]},
                },
            )

        ns = await self._get("namespace", namespace)
        if ns is not None:
            for key, document in (("policy", policy), ("binding", binding)):
                resource = (
                    "validatingadmissionpolicies"
                    if key == "policy"
                    else "validatingadmissionpolicybindings"
                )
                existing = await self._get(resource, document["metadata"]["name"])
                ref = manifest.get(key)
                if existing is None:
                    if ref:
                        raise ResourceUnknown("The original retirement fence disappeared")
                    await admit()
                    await self._kubectl("create", "-f", "-", body=document)
                    existing = await self._get(resource, document["metadata"]["name"])
                existing = self._owned(existing, labels, ref.get("uid") if ref else None)
                if existing["spec"] != document["spec"]:
                    raise ResourceUnknown("The original retirement policy changed")
                manifest[key] = {
                    "path": "/apis/admissionregistration.k8s.io/v1/"
                    + resource
                    + "/"
                    + document["metadata"]["name"],
                    "uid": existing["metadata"]["uid"],
                }
                await checkpoint(manifest)
            until = time.monotonic() + 25
            while True:
                active = await self._get("validatingadmissionpolicies", policy["metadata"]["name"])
                self._owned(active, labels, manifest["policy"]["uid"])
                if active and active.get("status", {}).get("observedGeneration") == active[
                    "metadata"
                ].get("generation"):
                    if active["status"].get("typeChecking", {}).get("expressionWarnings"):
                        raise ResourceUnknown("The original retirement policy has CEL warnings")
                    break
                if time.monotonic() >= until:
                    raise ResourceUnknown("The original retirement policy is not type checked")
                await asyncio.sleep(0.1)
            self._owned(ns, labels, manifest["namespace_uid"])
            if ns["metadata"].get("labels", {}).get("bbx.dev/retiring") != "true":
                await admit()
                ns["metadata"].setdefault("labels", {})["bbx.dev/retiring"] = "true"
                await self._kubectl("replace", "-f", "-", body=ns)
            sentinel = self._document(
                "ConfigMap", "bbx-retirement-sentinel", labels, namespace, data={}
            )
            await self._kubectl(
                "create",
                "--dry-run=server",
                "-f",
                "-",
                body=sentinel,
                expected_denial=(policy["metadata"]["name"], message),
            )
            manifest = await self._retirement_plan(operation, manifest)
            await checkpoint(manifest)
            for ref in manifest["resources"]:
                if ref.get("namespace_gc"):
                    continue
                current = await self._get(ref["kind"], ref["name"], namespace)
                if current is not None:
                    await delete(ref)
            for ref in manifest["volumes"]:
                until = time.monotonic() + 25
                while (pv := await self._get("pv", ref["name"])) is not None:
                    if pv["metadata"]["uid"] != ref["uid"]:
                        raise ResourceUnknown("The original PV name was rebound")
                    if time.monotonic() >= until:
                        raise ResourceUnknown("The original provisioner has not reclaimed the PV")
                    await asyncio.sleep(0.2)
            # Reinventory after every original object deletion; CREATE is fenced above.
            manifest = await self._retirement_plan(operation, manifest)
            remaining = await self._retirement_inventory(namespace)
            by_path = {r["path"]: r for r in manifest["resources"]}
            if any(not by_path.get(path, {}).get("namespace_gc") for path, _ in remaining):
                raise ResourceUnknown("Original namespaced deletions have not converged")
            manifest["namespace_delete_started"] = True
            await checkpoint(manifest)
            await delete(
                {"path": "/api/v1/namespaces/" + namespace, "uid": manifest["namespace_uid"]}
            )
        until = time.monotonic() + 25
        while (ns := await self._get("namespace", namespace)) is not None:
            if ns["metadata"]["uid"] != manifest["namespace_uid"]:
                raise ResourceUnknown("The retired Namespace name was rebound")
            if time.monotonic() >= until:
                raise ResourceUnknown("The original Namespace deletion is unresolved")
            await asyncio.sleep(0.2)
        for ref in manifest["volumes"]:
            if await self._get("pv", ref["name"]) is not None:
                raise ResourceUnknown("The original retained PV reclamation is unresolved")
        pvs = json.loads(await self._kubectl("get", "pv", "-o", "json"))["items"]
        claim_uids = {ref["claim_uid"] for ref in manifest["volumes"]}
        if any(p["spec"].get("claimRef", {}).get("uid") in claim_uids for p in pvs):
            raise ResourceUnknown("An original claim still has a provisioned PV")
        if await self._retirement_inventory(namespace):
            raise ResourceUnknown("The original Namespace still has orphaned resources")
        manifest["namespace_absent"] = True
        for ref in manifest["resources"] + manifest["volumes"]:
            ref["absent"] = True
        for key, kind in (
            ("binding", "validatingadmissionpolicybindings"),
            ("policy", "validatingadmissionpolicies"),
        ):
            ref = manifest.get(key)
            if not ref:
                continue
            current = await self._get(kind, policy["metadata"]["name"])
            if current is not None:
                await delete(ref)
            if await self._get(kind, policy["metadata"]["name"]) is not None:
                raise ResourceUnknown("The original admission fence cleanup is unresolved")
            ref["absent"] = True
        manifest["fence_absent"] = True
        manifest["retired"] = True
        await checkpoint(manifest)
        return {
            "retired": True,
            "namespace_uid": manifest["namespace_uid"],
            "pvc_uids": [
                operation["observation"][key]["uid"] for key in ("journal_pvc", "workspace_pvc")
            ],
            "pv_uids": [ref["uid"] for ref in manifest["volumes"] if ref["uid"]],
            "storage_erasure_verified": False,
        }
