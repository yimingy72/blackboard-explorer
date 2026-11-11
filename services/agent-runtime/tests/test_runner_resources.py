"""Necessary security receipts for the trusted native resource adapter; no cluster or model."""

import base64
import copy
import ipaddress
import json
from uuid import uuid4

import pytest
from bbx_contracts.runner_vpn import VPN_DYNAMIC_PROFILE, NativeTaskVpnProfile
from bbx_runtime.runner_resources import ResourceUnknown, RunnerResources


def configured():
    return RunnerResources(
        {
            "source_id": "synthetic-source",
            "kubeconfig": "/unused",
            "context": "unused",
            "cluster_uid": "synthetic-cluster",
            "runner_image": "runner@sha256:" + "a" * 64,
            "sandbox_image": "sandbox@sha256:" + "b" * 64,
            "environment_revision": "synthetic-1",
            "sandbox_server_url": "http://127.0.0.1:1",
            "storage_class": "unused",
            "system_namespace": "synthetic-control",
        }
    )


def identity(adapter):
    observation: dict = {
        "task_id": str(uuid4()),
        "operation_id": str(uuid4()),
        "namespace": "synthetic-task",
    }
    _, labels = adapter._scope(observation)
    observation.update(
        namespace_uid="ns-uid",
        source_id="synthetic-source",
        cluster_uid="synthetic-cluster",
        runner={
            "pod_name": "bbx-runner",
            "pod_uid": "runner-uid",
            "container_id": "containerd://old-runner",
        },
        sandbox={
            "id": "original-sandbox",
            "cr_uid": "cr-uid",
            "pod_name": "sandbox",
            "pod_uid": "sandbox-uid",
            "container_id": "containerd://old-sandbox",
        },
        runner_secret={"name": "bbx-runner", "uid": "secret-uid"},
        journal_pvc={"name": "bbx-journal", "uid": "journal-uid"},
        workspace_pvc={"name": "bbx-workspace", "uid": "workspace-uid"},
    )
    return observation, labels


def object_with(adapter, kind, name, labels, uid, **fields):
    obj = adapter._document(kind, name, labels, "synthetic-task", **fields)
    obj["metadata"].update(uid=uid, resourceVersion="original-rv")
    return obj


async def test_vpn_source_is_blocked_until_cluster_ipv4_boundaries_are_known(monkeypatch):
    adapter = configured()
    adapter.vpn = NativeTaskVpnProfile(
        profile=VPN_DYNAMIC_PROFILE,
        id="synthetic",
        label="Synthetic",
        egress_image="egress@sha256:" + "c" * 64,
        sandbox_image="sandbox@sha256:" + "d" * 64,
        tun_resource="devic.es/tun",
        source_origins=["https://vpn.example.test"],
        source_cidrs=["0.0.0.0/0"],
        peer_cidrs=["0.0.0.0/0"],
        target_cidrs=["0.0.0.0/0"],
        default_cidrs=["0.0.0.0/0"],
        deny_cidrs=["10.216.0.0/16", "172.30.0.2/32"],
    )
    monkeypatch.setattr(adapter, "vpn_enabled", lambda _: True)
    pod_cidrs = []

    async def kubectl(*args):
        if args[:2] == ("get", "namespace"):
            return json.dumps({"metadata": {"uid": "synthetic-cluster"}})
        if args[1] == "nodes":
            return json.dumps(
                {
                    "items": [
                        {
                            "spec": {"podCIDRs": pod_cidrs},
                            "status": {
                                "addresses": [{"type": "InternalIP", "address": "172.30.0.2"}]
                            },
                        }
                    ]
                }
            )
        assert args[1] == "servicecidrs"
        return json.dumps({"items": [{"spec": {"cidrs": ["10.217.0.0/16"]}}]})

    monkeypatch.setattr(adapter, "_kubectl", kubectl)
    with pytest.raises(ResourceUnknown, match="boundary is unavailable"):
        await adapter.verify_vpn_source_boundary({})
    pod_cidrs.append("10.216.0.0/24")
    with pytest.raises(ResourceUnknown, match="omits Kubernetes addresses"):
        await adapter.verify_vpn_source_boundary({})
    adapter.vpn = adapter.vpn.model_copy(
        update={"deny_cidrs": [*adapter.vpn.deny_cidrs, "10.217.0.0/16"]}
    )
    await adapter.verify_vpn_source_boundary({})


async def test_board_default_route_requires_the_original_resolver_and_cluster_deny(monkeypatch):
    adapter = RunnerResources(
        {
            **configured().config,
            "runner_profile": "node-sandbox-host-v1",
            "sandbox_host_image": "host@sha256:" + "c" * 64,
            "sandbox_network": {
                "namespace": "worker-a",
                "network_identity": "worker-a",
                "ipv4": "198.19.1.2",
            },
            "traffic_targets": [],
            "traffic_target_namespace": "synthetic-target",
            "traffic_sandbox_image": "sandbox@sha256:" + "d" * 64,
            "board_network": {
                "mode": "default",
                "dns_ipv4": "172.30.2.2",
                "allow_cidrs": ["0.0.0.0/0"],
                "deny_cidrs": ["10.216.0.0/16"],
            },
        }
    )
    observed, labels = identity(adapter)
    observed["runner"]["pod_ip"] = "10.216.0.2"
    observed["sandbox"]["pod_ip"] = "10.216.0.3"
    ref = observed["sandbox"]
    pod = {
        "metadata": {
            "labels": {"batch-sandbox.sandbox.opensandbox.io/name": ref["id"]},
            "ownerReferences": [{"uid": ref["cr_uid"]}],
        },
        "status": {"podIP": ref["pod_ip"]},
    }
    data = {
        "execution_contract": "exploration-board-v1",
        "native_network": "default",
        "traffic_profile": "binding-pcap-v1",
        "network_digest": "a" * 64,
        "dns_ipv4": "172.30.2.2",
        "route_policy": {
            "allow_cidrs": ["0.0.0.0/0"],
            "deny_cidrs": ["10.216.0.0/16"],
        },
    }
    operation = {**observed, "observation": observed, "data": data}

    async def runner(_):
        return observed["namespace"], labels, {}

    async def get(*_):
        return pod

    async def cluster_routes():
        return [ipaddress.IPv4Network("10.217.0.0/16")]

    async def installed(document, _, owner, digest):
        assert owner["uid"] == ref["pod_uid"]
        return document["spec"]

    monkeypatch.setattr(adapter, "_original_host_runner", runner)
    monkeypatch.setattr(adapter, "_cluster_routes", cluster_routes)
    monkeypatch.setattr(adapter, "_get", get)
    monkeypatch.setattr(adapter, "_pod_projection", lambda *_: ref)
    monkeypatch.setattr(adapter, "_sandbox_spec", lambda *_: None)
    monkeypatch.setattr(adapter, "_install_network_policy", installed)
    with pytest.raises(ResourceUnknown, match="exclude the control boundary"):
        await adapter.ensure_default_network_policy(operation)
    data["route_policy"]["deny_cidrs"].append("10.217.0.0/16")
    data["dns_ipv4"] = "172.30.2.3"
    with pytest.raises(ResourceUnknown, match="resolver changed"):
        await adapter.ensure_default_network_policy(operation)
    data["dns_ipv4"] = "172.30.2.2"
    spec = await adapter.ensure_default_network_policy(operation)
    assert spec["endpointSelector"]["matchLabels"] == {
        "batch-sandbox.sandbox.opensandbox.io/name": ref["id"]
    }
    assert spec["egress"][1] == {
        "toCIDR": ["172.30.2.2/32"],
        "toPorts": [{"ports": [{"port": "53", "protocol": "UDP"}]}],
    }
    assert not adapter.vpn_enabled(operation)
    with pytest.raises(ResourceUnknown, match="network deployment changed"):
        adapter.board_direct({"data": {"native_network": "default"}})


def test_board_socks_credentials_mount_only_in_protected_sidecar():
    digest = "c" * 64
    gateway = {
        "image": "legacy@sha256:" + digest,
        "uplink_network": "local-fixture",
        "upstream": {"ipv4": "172.30.3.2", "port": 1080},
        "allow_cidrs": ["10.70.0.0/24"],
        "deny_cidrs": ["10.216.0.0/16"],
        "probe": {"ipv4": "10.70.0.1", "port": 18888},
        "credential_file": "/unused/test-credentials.json",
        "credential_sha256": digest,
    }
    adapter = RunnerResources(
        {
            **configured().config,
            "runner_profile": "node-sandbox-host-v1",
            "sandbox_host_image": "host@sha256:" + digest,
            "sandbox_network": {
                "namespace": "worker-a",
                "network_identity": "worker-a",
                "ipv4": "198.19.1.2",
            },
            "traffic_targets": [],
            "traffic_target_namespace": "synthetic-target",
            "traffic_sandbox_image": "sandbox@sha256:" + "d" * 64,
            "board_network": {
                "mode": "socks5",
                "legacy_policy": {"mode": "socks5", "gateway": gateway},
            },
            "vpn": {
                "profile": VPN_DYNAMIC_PROFILE,
                "id": "synthetic",
                "label": "Synthetic",
                "egress_image": "egress@sha256:" + "e" * 64,
                "sandbox_image": "sandbox@sha256:" + "f" * 64,
                "tun_resource": "devic.es/tun",
                "peer_cidrs": ["172.30.3.2/32"],
                "target_cidrs": ["10.70.0.0/24"],
                "default_cidrs": ["10.70.0.0/24"],
                "deny_cidrs": ["10.216.0.0/16"],
            },
        }
    )
    _, labels = identity(adapter)
    pod = adapter._runner_document("synthetic-task", labels, with_vpn=True)
    runner, host, edge = pod["spec"]["containers"]
    startup = {"name": "BBX_PROTECTED_BOARD_START", "value": "1"}
    assert startup in runner["env"]
    assert startup not in host["env"] and startup not in edge.get("env", [])
    ctf = RunnerResources(
        {key: value for key, value in adapter.config.items() if key != "board_network"}
    )
    ctf_runner = ctf._runner_document("synthetic-task", labels, with_vpn=True)["spec"][
        "containers"
    ][0]
    assert startup not in ctf_runner["env"]
    assert all(
        "vpn" not in mount["name"] for value in (runner, host) for mount in value["volumeMounts"]
    )
    assert {mount["name"] for mount in edge["volumeMounts"]} == {"vpn-egress", "vpn-state"}
    assert "devic.es/tun" not in edge["resources"]["requests"]
    assert edge["securityContext"]["capabilities"] == {"add": ["NET_ADMIN"], "drop": ["ALL"]}
    raw_mount = {
        "type": "Opaque",
        "data": {key: "" for key in ("ca.pem", "config.json", "credentials.json")},
    }
    adapter._check_egress_secret(raw_mount)
    with pytest.raises(ResourceUnknown):
        configured()._check_egress_secret(raw_mount)
    with pytest.raises(ResourceUnknown):
        adapter._host_secret(raw_mount)


async def test_retirement_accepts_only_events_about_positively_exited_recovery_resources(
    monkeypatch,
):
    adapter = configured()
    observation, labels = identity(adapter)
    namespace = object_with(adapter, "Namespace", observation["namespace"], labels, "ns-uid")
    operation = {
        **observation,
        "observation": observation,
        "retirement_id": str(uuid4()),
        "data": {
            "recovery_history": [
                {
                    "exit_proof": {
                        "runner_terminated": True,
                        "sandbox_terminated": True,
                        "pvcs_retained": True,
                    },
                    "exit_observation": {
                        "namespace_uid": "ns-uid",
                        "runner": {"pod_uid": "prior-runner"},
                        "sandbox": {"pod_uid": "prior-sandbox", "cr_uid": "prior-cr"},
                    },
                    "verifier": {
                        "pod_uid": "prior-verifier",
                        "init_container_id": "init",
                        "container_id": "main",
                        "terminated": True,
                    },
                }
            ]
        },
    }
    manifest = {
        "volumes": [
            {
                "name": key,
                "uid": key,
                "claim_uid": observation[key]["uid"],
                "claim_name": observation[key]["name"],
                "claim_namespace": observation["namespace"],
                "storage_class": "unused",
                "reclaim_policy": "Delete",
            }
            for key in ("journal_pvc", "workspace_pvc")
        ]
    }
    contents = []

    async def get(kind, *_):
        return namespace if kind == "namespace" else None

    async def inventory(_):
        return contents

    def event(uid, kind):
        return (
            "/events/" + uid,
            {
                "kind": "Event",
                "metadata": {"name": uid, "uid": "event-" + uid},
                "regarding": {"uid": uid, "kind": kind},
            },
        )

    monkeypatch.setattr(adapter, "_get", get)
    monkeypatch.setattr(adapter, "_retirement_inventory", inventory)
    contents[:] = [
        event("prior-runner", "Pod"),
        event("prior-cr", "BatchSandbox"),
        event("prior-verifier", "Pod"),
    ]
    planned = await adapter._retirement_plan(operation, manifest)
    assert len(planned["resources"]) == 3 and all(
        item["namespace_gc"] for item in planned["resources"]
    )
    contents[:] = [event("unrelated", "Pod")]
    with pytest.raises(ResourceUnknown, match="no original UID"):
        await adapter._retirement_plan(operation, manifest)
    contents[:] = [
        (
            "/pods/prior-runner",
            {
                "kind": "Pod",
                "metadata": {"name": "bbx-runner", "uid": "prior-runner"},
                "spec": {"containers": [{"name": "runner"}]},
                "status": {
                    "containerStatuses": [
                        {
                            "name": "runner",
                            "containerID": "containerd://prior",
                            "state": {"terminated": {"exitCode": 0}},
                        }
                    ]
                },
            },
        )
    ]
    with pytest.raises(ResourceUnknown, match="unresolved execution Pod"):
        await adapter._retirement_plan(operation, manifest)
    contents[:] = [
        (
            "/batchsandboxes/prior-cr",
            {
                "kind": "BatchSandbox",
                "metadata": {"name": "prior-cr", "uid": "prior-cr"},
            },
        )
    ]
    with pytest.raises(ResourceUnknown, match="foreign or unresolved"):
        await adapter._retirement_plan(operation, manifest)


def test_runner_native_defaults_do_not_allow_extra_processes_mounts_env_or_secret_material():
    expected = {
        "restartPolicy": "Never",
        "serviceAccountName": "bbx-runner",
        "automountServiceAccountToken": False,
        "securityContext": {"runAsUser": 1000},
        "volumes": [{"name": "journal", "persistentVolumeClaim": {"claimName": "bbx-journal"}}],
        "containers": [
            {
                "name": "runner",
                "image": "fixed",
                "imagePullPolicy": "IfNotPresent",
                "resources": {},
                "securityContext": {},
                "volumeMounts": [],
                "env": [
                    {
                        "name": "BBX_POD_UID",
                        "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}},
                    }
                ],
            }
        ],
    }
    base = {"metadata": {}, "spec": expected}
    accepted = copy.deepcopy(base)
    accepted["spec"]["containers"][0]["env"][0]["valueFrom"]["fieldRef"]["apiVersion"] = "v1"
    RunnerResources._runner_spec(accepted, expected)
    for mutate in [
        lambda p: p["spec"]["containers"].append({}),
        lambda p: p["spec"].update(hostNetwork=True),
        lambda p: p["spec"]["containers"][0].update(command=["evil"]),
        lambda p: p["spec"]["containers"][0]["env"].append(
            {"name": "SERVICE_TOKEN", "value": "synthetic"}
        ),
        lambda p: p["spec"]["volumes"].append({"name": "host", "hostPath": {"path": "/"}}),
    ]:
        changed = copy.deepcopy(base)
        mutate(changed)
        with pytest.raises(ResourceUnknown):
            RunnerResources._runner_spec(changed, expected)
    with pytest.raises(ResourceUnknown):
        RunnerResources._runner_secret({"type": "Opaque", "data": {"provider-key": "synthetic"}})


async def test_mount_native_uid_resourceversion_and_immutable_bootstrap(monkeypatch):
    adapter = configured()
    observation, labels = identity(adapter)
    pod = adapter._runner_document("synthetic-task", labels)
    pod["metadata"].update(uid="runner-uid", resourceVersion="original-rv")
    pod["status"] = {
        "containerStatuses": [
            {"name": "runner", "containerID": "containerd://old-runner", "state": {"running": {}}}
        ]
    }
    secret = object_with(
        adapter, "Secret", "bbx-runner", labels, "secret-uid", type="Opaque", data={}
    )
    sent = []

    async def cluster():
        pass

    async def get(kind, _name, _namespace):
        return pod if kind == "pod" else secret

    async def kubectl(*args, body=None):
        sent.append((args, copy.deepcopy(body)))
        return "{}"

    monkeypatch.setattr(adapter, "_cluster", cluster)
    monkeypatch.setattr(adapter, "_get", get)
    monkeypatch.setattr(adapter, "_kubectl", kubectl)
    bootstrap = {
        "fence": {"pod_uid": "runner-uid", "task_id": observation["task_id"]},
        "control_credential_ref": "control",
    }
    credential = {
        "control": {
            "audience": "runner-control",
            "task_id": observation["task_id"],
            "token": "synthetic",
        }
    }
    await adapter.publish_mount(observation, bootstrap, credential)
    assert sent[0][0] == ("replace", "-f", "-")
    assert sent[0][1]["metadata"]["uid"] == "secret-uid"
    assert sent[0][1]["metadata"]["resourceVersion"] == "original-rv"
    assert set(sent[0][1]["data"]) == {"runner-bootstrap.json", "credentials.json", "ca.pem"}
    assert json_value(sent[0][1]["data"]["runner-bootstrap.json"]) == bootstrap
    secret["data"] = sent[0][1]["data"]
    with pytest.raises(ResourceUnknown):
        await adapter.publish_mount(
            observation, {**bootstrap, "journal_root": "changed"}, credential
        )
    assert len(sent) == 1

    import json

    original = json_value(secret["data"]["credentials.json"])
    for audience in ["model", "business-mcp"]:
        original[audience] = {
            "audience": audience,
            "task_id": observation["task_id"],
            "expires_at": -1,
            "token": "old-expired-" + audience,
        }
    secret["data"]["credentials.json"] = base64.b64encode(json.dumps(original).encode()).decode()
    before = copy.deepcopy(secret["data"])
    renewed = {"control": {**credential["control"], "token": "new-synthetic-control"}}
    await adapter.publish_mount(observation, bootstrap, renewed, control_only=True)
    actual = json_value(sent[-1][1]["data"]["credentials.json"])
    assert actual["control"] == renewed["control"]
    assert (
        actual["model"] == original["model"] and actual["business-mcp"] == original["business-mcp"]
    )
    assert sent[-1][1]["data"]["runner-bootstrap.json"] == before["runner-bootstrap.json"]
    assert sent[-1][1]["data"]["ca.pem"] == before["ca.pem"]
    with pytest.raises(ResourceUnknown):
        await adapter.publish_mount(
            observation, bootstrap, {**renewed, "model": original["model"]}, control_only=True
        )
    secret["data"] = {"ca.pem": before["ca.pem"]}
    await adapter.publish_mount(observation, bootstrap, renewed, control_only=True)
    assert json_value(sent[-1][1]["data"]["credentials.json"]) == renewed
    assert json_value(sent[-1][1]["data"]["runner-bootstrap.json"]) == bootstrap
    assert sent[-1][1]["data"]["ca.pem"] == before["ca.pem"]

    old_mcp = {
        "audience": "business-mcp",
        "task_id": observation["task_id"],
        "binding_id": "draining",
        "expires_at": 4102444800,
        "token": "original-scoped-mcp",
    }
    old_sandbox = {**old_mcp, "audience": "sandbox-mcp", "token": "original-scoped-sandbox"}
    old_business = {**old_mcp, "audience": "business-mcp", "token": "original-scoped-business"}
    old_records = {
        **renewed,
        "native-mcp-draining-sandbox": old_sandbox,
        "native-mcp-draining-business": old_business,
        "native-model-draining": {**old_mcp, "audience": "model", "expires_at": -1},
        "native-mcp-unrelated-business": {**old_mcp, "binding_id": "unrelated"},
    }
    secret["data"] = {
        **sent[-1][1]["data"],
        "credentials.json": base64.b64encode(json.dumps(old_records).encode()).decode(),
    }
    replacement = {
        **renewed,
        "native-mcp-replacement-business": {
            **old_mcp,
            "binding_id": "replacement",
            "token": "new-b",
        },
    }
    await adapter.publish_mount(
        observation, bootstrap, replacement, retain_binding_ids=["draining", "not-mounted"]
    )
    assert json_value(sent[-1][1]["data"]["credentials.json"]) == {
        **replacement,
        "native-mcp-draining-sandbox": old_sandbox,
        "native-mcp-draining-business": old_business,
    }
    with pytest.raises(ResourceUnknown, match="cannot be issued again"):
        await adapter.publish_mount(
            observation,
            bootstrap,
            {
                **renewed,
                "native-mcp-draining-business": {**old_business, "token": "new-draining-token"},
            },
            retain_binding_ids=["draining"],
        )
    old_records["native-mcp-draining-business"] = {**old_business, "binding_id": "wrong-binding"}
    secret["data"]["credentials.json"] = base64.b64encode(json.dumps(old_records).encode()).decode()
    with pytest.raises(ResourceUnknown, match="credential identity changed"):
        await adapter.publish_mount(
            observation, bootstrap, renewed, retain_binding_ids=["draining"]
        )


def json_value(value):
    import json

    return json.loads(base64.b64decode(value))


async def test_stop_uses_uid_precondition_and_keeps_unproven_exit_unknown(monkeypatch):
    adapter = configured()
    observation, labels = identity(adapter)
    sent = []
    objects = {"namespace": object_with(adapter, "Namespace", "synthetic-task", labels, "ns-uid")}
    for key in ("journal_pvc", "workspace_pvc"):
        objects[observation[key]["name"]] = object_with(
            adapter,
            "PersistentVolumeClaim",
            observation[key]["name"],
            labels,
            observation[key]["uid"],
        )
    for key in ("runner", "sandbox"):
        objects[observation[key]["pod_name"]] = object_with(
            adapter,
            "Pod",
            observation[key]["pod_name"],
            labels,
            observation[key]["pod_uid"],
            spec={"containers": [{"name": key}]},
            status={
                "phase": "Running",
                "containerStatuses": [
                    {
                        "name": key,
                        "containerID": observation[key]["container_id"],
                        "state": {"running": {}},
                    }
                ],
            },
        )

    async def cluster():
        pass

    async def get(kind, name, _namespace=None):
        return (
            None
            if kind == "batchsandbox"
            else objects["namespace" if kind == "namespace" else name]
        )

    async def kubectl(*args, body=None):
        if args[:2] == ("get", "--raw"):
            path, _, query = args[2].partition("?")
            assert path == "/api/v1/namespaces/synthetic-task/pods"
            name = query.removeprefix("fieldSelector=metadata.name%3D")
            return json.dumps(
                {
                    "kind": "PodList",
                    "metadata": {"resourceVersion": "fresh-list-rv"},
                    "items": [objects[name]],
                }
            )
        sent.append((args, body))
        return "{}"

    watched = []

    async def unproven_watch(pod, _namespace):
        assert pod["watch_revision"] == "fresh-list-rv"
        assert pod["resource_version"] == "original-rv"
        watched.append(pod["pod_uid"])
        return False

    monkeypatch.setattr(adapter, "_cluster", cluster)
    monkeypatch.setattr(adapter, "_get", get)
    monkeypatch.setattr(adapter, "_kubectl", kubectl)
    monkeypatch.setattr(adapter, "_terminated_watch", unproven_watch)
    result = await adapter.stop(observation, "synthetic-tenant")
    assert sorted(watched) == ["runner-uid", "sandbox-uid"]
    assert sent[0][0][:2] == ("delete", "--raw")
    assert sent[0][1]["preconditions"] == {"uid": "runner-uid"}
    assert result == {
        "runner_terminated": False,
        "sandbox_terminated": False,
        "pvcs_retained": True,
    }


def test_unapproved_debug_container_cannot_turn_main_exit_into_pod_cleanup():
    pod: dict = {
        "metadata": {"name": "sandbox", "uid": "original"},
        "spec": {"containers": [{"name": "sandbox"}]},
        "status": {
            "containerStatuses": [
                {
                    "name": "sandbox",
                    "containerID": "original-cid",
                    "state": {"terminated": {"exitCode": 0}},
                }
            ]
        },
    }
    assert RunnerResources._pod_projection(pod)["terminated"] is True
    pod["spec"]["ephemeralContainers"] = [{"name": "unknown-debug"}]
    pod["status"]["ephemeralContainerStatuses"] = [
        {"name": "unknown-debug", "state": {"running": {}}}
    ]
    with pytest.raises(ResourceUnknown):
        RunnerResources._pod_projection(pod)


async def test_watch_rejects_debug_scope_added_after_original_pod_was_read(monkeypatch):
    adapter = configured()
    pod = {
        "metadata": {"name": "sandbox", "uid": "original", "resourceVersion": "before-debug"},
        "spec": {"containers": [{"name": "sandbox"}]},
        "status": {
            "containerStatuses": [
                {"name": "sandbox", "containerID": "original-cid", "state": {"running": {}}}
            ]
        },
    }

    import json

    event = copy.deepcopy(pod)
    event["spec"]["ephemeralContainers"] = [{"name": "debug"}]
    event["status"]["containerStatuses"][0]["state"] = {"terminated": {"exitCode": 0}}

    class Process:
        returncode = None

        def __init__(self):
            self.stdout = self

        async def readline(self):
            return (json.dumps({"type": "MODIFIED", "object": event}) + "\n").encode()

        def kill(self):
            self.returncode = -9

        async def communicate(self):
            return b"", b""

    async def create(*args, **_kwargs):
        assert any("watch=true" in arg and "resourceVersion=" in arg for arg in args)
        assert "--resource-version" not in args
        return Process()

    monkeypatch.setattr("bbx_runtime.runner_resources.asyncio.create_subprocess_exec", create)
    reference = {**adapter._pod_projection(pod), "resource_version": "before-debug"}
    assert await adapter._terminated_watch(reference, "synthetic-task") is False


async def test_recover_sid_only_is_read_only_and_stop_reaches_known_sandbox(monkeypatch):
    adapter = configured()
    observation, labels = identity(adapter)
    observation.pop("runner")
    observation.pop("runner_secret")
    observation["sandbox"] = {"id": "original-sandbox"}
    namespace = object_with(adapter, "Namespace", "synthetic-task", labels, "ns-uid")
    claims = {
        ref["name"]: object_with(
            adapter,
            "PersistentVolumeClaim",
            ref["name"],
            labels,
            ref["uid"],
            spec={
                "storageClassName": "unused",
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": "128Mi" if key == "journal_pvc" else "64Mi"}},
            },
        )
        for key, ref in observation.items()
        if key in {"journal_pvc", "workspace_pvc"}
    }
    cr = {
        "metadata": {
            "uid": "cr-uid",
            "labels": {
                "bbx-task-id": observation["task_id"],
                "bbx-create-op": observation["operation_id"],
            },
        }
    }
    sandbox = object_with(
        adapter,
        "Pod",
        "sandbox",
        labels,
        "sandbox-uid",
        spec={
            "containers": [
                {
                    "name": "sandbox",
                    "image": adapter.config["sandbox_image"],
                    "env": [
                        {
                            "name": "BBX_ENVIRONMENT_REVISION",
                            "value": adapter.config["environment_revision"],
                        },
                        {
                            "name": "BBX_POD_UID",
                            "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}},
                        },
                        {
                            "name": "EXECD_ACCESS_TOKEN",
                            "valueFrom": {"secretKeyRef": {"name": "bbx-execd", "key": "token"}},
                        },
                    ],
                }
            ]
        },
        status={
            "phase": "Running",
            "containerStatuses": [
                {"name": "sandbox", "containerID": "original-cid", "state": {"running": {}}}
            ],
        },
    )
    sandbox["metadata"]["ownerReferences"] = [{"uid": "cr-uid"}]
    effects = []
    deleted = False

    async def cluster():
        pass

    async def get(kind, name, _namespace=None):
        if kind == "namespace":
            return namespace
        if kind == "pvc":
            return claims[name]
        if kind == "batchsandbox":
            return None if deleted else cr
        if kind == "pod" and name == "sandbox":
            return sandbox
        return None

    async def kubectl(*args, body=None):
        assert args[0] == "get" and body is None
        import json

        return json.dumps(
            {"kind": "PodList", "metadata": {"resourceVersion": "fresh-rv"}, "items": [sandbox]}
        )

    async def watch(_pod, _namespace):
        sandbox["status"]["containerStatuses"][0]["state"] = {"terminated": {"exitCode": 0}}
        return True

    class Manager:
        async def kill_sandbox(self, sid):
            nonlocal deleted
            effects.append(sid)
            deleted = True

        async def close(self):
            pass

    async def manager_create(*, connection_config):
        return Manager()

    monkeypatch.setattr(adapter, "_cluster", cluster)
    monkeypatch.setattr(adapter, "_get", get)
    monkeypatch.setattr(adapter, "_kubectl", kubectl)
    monkeypatch.setattr(adapter, "_terminated_watch", watch)
    monkeypatch.setattr("bbx_runtime.runner_resources.SandboxManager.create", manager_create)
    operation = {
        "task_id": observation["task_id"],
        "operation_id": observation["operation_id"],
        "namespace": "synthetic-task",
        "observation": observation,
        "sandbox_create_claimed": True,
    }
    recovered = await adapter.recover(operation, "synthetic-tenant")
    assert recovered["sandbox"]["cr_uid"] == "cr-uid"
    assert recovered["sandbox"]["pod_uid"] == "sandbox-uid" and "runner" not in recovered
    assert recovered["not_started"] is False
    result = await adapter.stop(recovered, "synthetic-tenant")
    assert effects == ["original-sandbox"]
    assert result == {"runner_terminated": False, "sandbox_terminated": True, "pvcs_retained": True}


@pytest.mark.parametrize("claimed,expected", [(False, True), (True, False)])
async def test_recover_absence_is_not_a_never_dispatched_proof_without_pg_flag(
    monkeypatch, claimed, expected
):
    adapter = configured()
    observation, labels = identity(adapter)
    namespace = object_with(adapter, "Namespace", "synthetic-task", labels, "ns-uid")

    async def cluster():
        pass

    async def get(kind, _name, _namespace=None):
        return namespace if kind == "namespace" else None

    async def lookup(_tenant, _metadata):
        return None

    async def kubectl(*args, body=None):
        assert args[0] == "get" and body is None
        return '{"items":[]}'

    async def forbidden_create(*_args, **_kwargs):
        raise AssertionError("Read-only recovery attempted a create")

    monkeypatch.setattr(adapter, "_cluster", cluster)
    monkeypatch.setattr(adapter, "_get", get)
    monkeypatch.setattr(adapter, "_sandbox_id", lookup)
    monkeypatch.setattr(adapter, "_kubectl", kubectl)
    monkeypatch.setattr(adapter, "_ensure", forbidden_create)
    operation = {
        "task_id": observation["task_id"],
        "operation_id": observation["operation_id"],
        "namespace": "synthetic-task",
        "observation": {},
        "sandbox_create_claimed": claimed,
    }
    result = await adapter.recover(operation, "synthetic-tenant")
    assert result["not_started"] is expected and result["pvcs_retained"] is True


@pytest.mark.parametrize("replacement", ["cr", "pod"])
async def test_original_exit_does_not_confirm_a_replaced_logical_sandbox(monkeypatch, replacement):
    adapter = configured()
    observation, labels = identity(adapter)
    observation.pop("runner")
    namespace = object_with(adapter, "Namespace", "synthetic-task", labels, "ns-uid")
    pod = object_with(
        adapter,
        "Pod",
        "sandbox",
        labels,
        "sandbox-uid",
        spec={"containers": [{"name": "sandbox"}]},
        status={
            "containerStatuses": [
                {
                    "name": "sandbox",
                    "containerID": "containerd://old-sandbox",
                    "state": {"terminated": {"exitCode": 0}},
                }
            ]
        },
    )
    pod["metadata"]["ownerReferences"] = [{"uid": "cr-uid"}]
    other = copy.deepcopy(pod)
    other["metadata"]["uid"] = "replacement-pod"

    async def cluster():
        pass

    async def get(kind, name, _namespace=None):
        if kind == "namespace":
            return namespace
        if kind == "pod":
            return pod
        if kind == "batchsandbox":
            return {"metadata": {"uid": "replacement-cr"}} if replacement == "cr" else None
        raise AssertionError("Unexpected resource operation")

    async def kubectl(*args, body=None):
        assert args[0] == "get" and body is None
        import json

        return json.dumps(
            {
                "kind": "PodList",
                "metadata": {"resourceVersion": "fresh-rv"},
                "items": [pod if "--raw" in args else other],
            }
        )

    monkeypatch.setattr(adapter, "_cluster", cluster)
    monkeypatch.setattr(adapter, "_get", get)
    monkeypatch.setattr(adapter, "_kubectl", kubectl)
    with pytest.raises(ResourceUnknown, match="identity changed|another execution"):
        await adapter.stop(observation, "synthetic-tenant")


def test_sandbox_host_profile_keeps_mounts_separate_and_requires_both_original_exits():
    from bbx_runtime.runner_resources import SANDBOX_HOST_PROFILE

    old = configured()
    adapter = RunnerResources(
        {
            **old.config,
            "runner_profile": SANDBOX_HOST_PROFILE,
            "sandbox_host_image": "host@sha256:" + "c" * 64,
            "sandbox_network": {
                "namespace": "worker-a",
                "network_identity": "worker-a",
                "ipv4": "198.19.1.2",
            },
        }
    )
    _, labels = identity(adapter)
    pod = adapter._runner_document("synthetic-task", labels)
    pod["metadata"].update(uid="original-runner", resourceVersion="123")
    desired = copy.deepcopy(pod["spec"])
    node, host = pod["spec"]["containers"]
    assert pod["spec"]["shareProcessNamespace"] is False
    assert {v["name"] for v in node["volumeMounts"]} == {"identity", "journal", "start"}
    assert {v["name"] for v in host["volumeMounts"]} == {"sandbox-host"}
    assert host["securityContext"]["runAsUser"] == 1001
    adapter._runner_spec(pod, desired)
    pod["status"] = {
        "podIP": "10.216.0.4",
        "containerStatuses": [
            {"name": "runner", "containerID": "containerd://node", "state": {"running": {}}},
            {"name": "sandbox-host", "containerID": "containerd://host", "state": {"running": {}}},
        ],
    }
    original = adapter._pod_projection(pod, with_host=True)
    assert (
        original["host_container_id"] == "containerd://host" and original["pod_ip"] == "10.216.0.4"
    )
    node_state, host_state = pod["status"]["containerStatuses"]
    node_state["state"] = {"terminated": {"exitCode": 143}}
    half = adapter._pod_projection(pod, with_host=True)
    assert half["terminated"] is False and half["observation"] == "unknown"
    host_state["state"] = {"terminated": {"exitCode": 143}}
    both = adapter._pod_projection(pod, with_host=True)
    assert both["terminated"] and adapter._same_process(both, original)
    host_state["containerID"] = "containerd://replacement-host"
    assert not adapter._same_process(adapter._pod_projection(pod, with_host=True), original)
    with pytest.raises(ResourceUnknown):
        old._pod_projection(pod)
    pod["spec"]["ephemeralContainers"] = [{"name": "debug"}]
    with pytest.raises(ResourceUnknown):
        adapter._pod_projection(pod, with_host=True)


async def test_runner_egress_excludes_retired_gateway(monkeypatch):
    from bbx_runtime.runner_resources import SANDBOX_HOST_PROFILE

    adapter = RunnerResources(
        {
            **configured().config,
            "runner_profile": SANDBOX_HOST_PROFILE,
            "sandbox_host_image": "host@sha256:" + "c" * 64,
            "sandbox_network": {
                "namespace": "worker-a",
                "network_identity": "worker-a",
                "ipv4": "198.19.1.2",
            },
        }
    )
    observation, _ = identity(adapter)
    operation = {
        "task_id": observation["task_id"],
        "operation_id": observation["operation_id"],
        "namespace": observation["namespace"],
        "hard_deadline": "2099-01-01T00:00:00Z",
        "config": {"mcp_transport": "direct-v2"},
        "data": {},
    }
    targets = []

    class PolicyObserved(Exception):
        pass

    async def ensure(doc, *_):
        if doc["metadata"]["name"] == "bbx-runner-control":
            targets.extend(rule["toPorts"][0]["ports"][0]["port"] for rule in doc["spec"]["egress"])
            raise PolicyObserved()
        return {"metadata": {"uid": "test-uid"}}

    async def unused(*_args, **_kwargs):
        pass

    async def unexpected_dispatch():
        raise AssertionError("No Sandbox creation is needed for this policy proof")

    monkeypatch.setattr(adapter, "_ensure", ensure)
    monkeypatch.setattr(adapter, "_cluster", unused)
    with pytest.raises(PolicyObserved):
        await adapter.ensure_task_resources(
            operation,
            "synthetic-tenant",
            admit_sandbox_create=unexpected_dispatch,
            checkpoint=unused,
        )
    assert targets == ["28888"]


@pytest.mark.parametrize("outcome", ["ready", "refused", "wrong_identity", "replaced"])
async def test_sandbox_readiness_reconnects_only_to_the_original_host(monkeypatch, outcome):
    import httpx

    adapter = configured()
    operation = {"observation": identity(adapter)[0]}
    config = {
        "task_id": "task",
        "operation_id": "operation",
        "runner_pod_uid": "runner-uid",
        "sandbox_id": "sandbox",
        "sandbox_pod_uid": "sandbox-uid",
        "execution_context_id": "context",
        "inbound_key": "synthetic-readiness-key",
    }
    validated = []
    forwards = []
    requests = []

    async def verify(actual):
        assert actual is operation
        validated.append(actual["observation"]["runner"]["pod_uid"])
        if outcome == "replaced" and len(validated) == 2:
            raise ResourceUnknown("The original Sandbox host process is not live")
        return "synthetic", {}, {"metadata": {"name": "bbx-runner"}}

    class Forward:
        def __init__(self, port):
            self.port, self.returncode, self.stdout = port, None, self

        async def readline(self):
            return f"Forwarding from 127.0.0.1:{self.port} -> 8766\n".encode()

        def kill(self):
            self.returncode = -9

        async def wait(self):
            assert self.returncode is not None
            return self.returncode

    async def spawn(*args, **_kwargs):
        assert args[args.index("port-forward") + 1 :] == (
            "pod/bbx-runner",
            ":8766",
            "--address",
            "127.0.0.1",
        )
        process = Forward(24000 + len(forwards))
        forwards.append(process)
        return process

    async def request(probe):
        assert probe.method == "GET" and probe.url.path == "/ready"
        assert probe.headers["x-bbx-sandbox-inbound"] == config["inbound_key"]
        requests.append(probe.url.port)
        if outcome in {"ready", "replaced"} and len(requests) == 1:
            forwards[-1].returncode = 1
            raise httpx.ConnectError("Synthetic cold listener")
        if outcome == "refused":
            return httpx.Response(403)
        ready = {k: v for k, v in config.items() if k != "inbound_key"}
        if outcome == "wrong_identity":
            ready["sandbox_pod_uid"] = "foreign"
        return httpx.Response(200, json={**ready, "ready": True})

    client = httpx.AsyncClient
    monkeypatch.setattr(adapter, "_original_host_runner", verify)
    monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr(
        "bbx_runtime.runner_resources.httpx.AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(request), **kwargs),
    )
    if outcome == "ready":
        await adapter.wait_sandbox_host_ready(operation, config)
        assert requests == [24000, 24001]
        assert validated == ["runner-uid"] * 3
    else:
        with pytest.raises(ResourceUnknown):
            await adapter.wait_sandbox_host_ready(operation, config)
        assert requests == [24000]
        assert len(forwards) == 1
        assert len(validated) == (2 if outcome == "replaced" else 1)
    assert all(p.returncode is not None for p in forwards)


def test_explicit_board_resume_initializes_a_new_generation_without_borrowing_old_writer():
    from bbx_contracts.runner_bootstrap import explicit_board_journal_restart

    adapter = configured()
    adapter.journal_writer_lock = True
    adapter.board_network = {"mode": "default"}
    previous, _ = identity(adapter)
    previous["resource_generation"] = 1
    source = {
        "run_number": 1,
        "operation_id": previous["operation_id"],
        "workspace_pvc_uid": previous["workspace_pvc"]["uid"],
    }
    new, _ = identity(adapter)
    new["task_id"] = previous["task_id"]
    new["namespace"] = f"bbx-task-{previous['task_id']}-r2"
    new.update(journal_pvc={"uid": "new-journal"}, workspace_pvc={"uid": "new-workspace"})
    operation = {
        **new,
        "observation": new,
        "data": {
            "execution_contract": "exploration-board-v1",
            "resource_generation": 2,
            "board_workspace_source": source,
            "board_history": [
                {
                    "operation_id": previous["operation_id"],
                    "namespace": previous["namespace"],
                    "archive": source,
                    "observation": previous,
                }
            ],
        },
    }
    assert explicit_board_journal_restart(operation)
    namespace, labels = adapter._scope(operation)
    fresh = adapter._runner_document(namespace, labels, 2, fresh_journal=True)["spec"]
    assert fresh["initContainers"][0]["command"][-1] == "epoch-2"
    assert fresh["containers"][0]["command"][-1] == "run"
    assert fresh["containers"][0]["volumeMounts"][1]["subPath"] == "epoch-2"

    new["journal_pvc"] = previous["journal_pvc"]
    assert not explicit_board_journal_restart(operation)
    original = adapter._runner_document(namespace, labels, 2, "1:99")["spec"]
    assert "initContainers" not in original and original["containers"][0]["command"][-1] == "1:99"
    with pytest.raises(ResourceUnknown, match="original lock is absent"):
        adapter._runner_document(namespace, labels, 2)
