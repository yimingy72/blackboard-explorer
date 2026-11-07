"""Necessary security receipts for the trusted native resource adapter; no cluster or model."""

import base64
import copy
from uuid import uuid4

import pytest
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
    for audience in ["model", "mcp"]:
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
    assert actual["model"] == original["model"] and actual["mcp"] == original["mcp"]
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
        "audience": "mcp",
        "task_id": observation["task_id"],
        "binding_id": "draining",
        "expires_at": 4102444800,
        "token": "original-scoped-mcp",
    }
    old_records = {
        **renewed,
        "native-mcp-draining": old_mcp,
        "native-model-draining": {**old_mcp, "audience": "model", "expires_at": -1},
        "native-mcp-unrelated": {**old_mcp, "binding_id": "unrelated"},
    }
    secret["data"] = {
        **sent[-1][1]["data"],
        "credentials.json": base64.b64encode(json.dumps(old_records).encode()).decode(),
    }
    replacement = {
        **renewed,
        "native-mcp-replacement": {**old_mcp, "binding_id": "replacement", "token": "new-b"},
    }
    await adapter.publish_mount(
        observation, bootstrap, replacement, retain_binding_ids=["draining", "not-mounted"]
    )
    assert json_value(sent[-1][1]["data"]["credentials.json"]) == {
        **replacement,
        "native-mcp-draining": old_mcp,
    }
    with pytest.raises(ResourceUnknown, match="cannot be issued again"):
        await adapter.publish_mount(
            observation,
            bootstrap,
            {**renewed, "native-mcp-draining": {**old_mcp, "token": "new-draining-token"}},
            retain_binding_ids=["draining"],
        )
    old_records["native-mcp-draining"] = {**old_mcp, "binding_id": "wrong-binding"}
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
        sent.append((args, body))
        return "{}"

    async def unproven_watch(_pod, _namespace):
        return False

    monkeypatch.setattr(adapter, "_cluster", cluster)
    monkeypatch.setattr(adapter, "_get", get)
    monkeypatch.setattr(adapter, "_kubectl", kubectl)
    monkeypatch.setattr(adapter, "_terminated_watch", unproven_watch)
    result = await adapter.stop(observation, "synthetic-tenant")
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

        return json.dumps({"items": [sandbox]})

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

        return json.dumps({"items": [other]})

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
