"""Retirement must preserve foreign objects, frozen identities and retained storage."""

import copy
from uuid import uuid4

import pytest
from bbx_runtime.runner_resources import ResourceUnknown
from test_runner_resources import configured, identity, object_with


def fixture(monkeypatch):
    adapter = configured()
    observation, labels = identity(adapter)
    operation = {**observation, "observation": observation, "retirement_id": str(uuid4())}
    objects = {
        ("namespace", observation["namespace"]): object_with(
            adapter, "Namespace", observation["namespace"], labels, "ns-uid"
        )
    }
    resources = []
    for short in ("journal", "workspace"):
        name, uid = "bbx-" + short, short + "-uid"
        claim = object_with(
            adapter,
            "PersistentVolumeClaim",
            name,
            labels,
            uid,
            spec={"storageClassName": "unused", "volumeName": "pv-" + short},
        )
        objects["pvc", name] = claim
        objects["pv", "pv-" + short] = {
            "metadata": {"name": "pv-" + short, "uid": "pv-" + uid},
            "spec": {
                "storageClassName": "unused",
                "persistentVolumeReclaimPolicy": "Delete",
                "claimRef": {"namespace": observation["namespace"], "name": name, "uid": uid},
            },
        }
        resources.append(
            ("/api/v1/namespaces/synthetic-task/persistentvolumeclaims/" + name, claim)
        )

    async def get(kind, name, namespace=None):
        return copy.deepcopy(objects.get((kind, name)))

    async def inventory(namespace):
        return copy.deepcopy(resources)

    monkeypatch.setattr(adapter, "_get", get)
    monkeypatch.setattr(adapter, "_retirement_inventory", inventory)
    return adapter, operation, objects, resources


async def test_retirement_preflight_refuses_foreign_or_retained_pv_before_mutation(monkeypatch):
    adapter, operation, objects, resources = fixture(monkeypatch)
    plan = await adapter._retirement_plan(operation, {})
    assert len(plan["volumes"]) == 2
    assert all(
        v["claim_namespace"] == "synthetic-task" and v["reclaim_policy"] == "Delete"
        for v in plan["volumes"]
    )
    objects["pv", "pv-workspace"]["spec"]["persistentVolumeReclaimPolicy"] = "Retain"
    with pytest.raises(ResourceUnknown, match="Delete policy"):
        await adapter._retirement_plan(operation, {})
    objects["pv", "pv-workspace"]["spec"]["persistentVolumeReclaimPolicy"] = "Delete"
    foreign = {"kind": "Secret", "metadata": {"name": "foreign", "uid": "foreign-uid"}}
    resources.append(("/api/v1/namespaces/synthetic-task/secrets/foreign", foreign))
    with pytest.raises(ResourceUnknown, match="foreign"):
        await adapter._retirement_plan(operation, {})


async def test_retirement_never_rebinds_saved_resource_or_pv_uid(monkeypatch):
    adapter, operation, objects, resources = fixture(monkeypatch)
    plan = await adapter._retirement_plan(operation, {})
    resources[0][1]["metadata"]["uid"] = "replacement-uid"
    with pytest.raises(ResourceUnknown, match="another UID"):
        await adapter._retirement_plan(operation, plan)
    resources[0][1]["metadata"]["uid"] = "journal-uid"
    objects["pv", "pv-journal"]["spec"]["claimRef"]["uid"] = "other-pvc-uid"
    with pytest.raises(ResourceUnknown, match="PV retirement identity"):
        await adapter._retirement_plan(operation, plan)


async def test_retirement_requires_explicit_single_server_authority(monkeypatch):
    adapter, operation, _, _ = fixture(monkeypatch)

    async def kubectl(*args, **kwargs):
        return '{"items": []}'

    async def cluster():
        pass

    monkeypatch.setattr(adapter, "_kubectl", kubectl)
    monkeypatch.setattr(adapter, "_cluster", cluster)
    with pytest.raises(ResourceUnknown, match="single API server"):
        await adapter.retire(operation, checkpoint=None, admit_retire=None)


def test_retirement_fence_is_namespaced_create_only_and_uses_original_namespace_parameter():
    adapter = configured()
    observed, labels = identity(adapter)
    operation = {**observed, "retirement_id": str(uuid4())}
    policy, binding, _ = adapter._retirement_fence(operation, labels, "original-ns-uid")
    spec = policy["spec"]
    assert spec["paramKind"] == {"apiVersion": "v1", "kind": "Namespace"}
    assert spec["validations"][0]["expression"] == 'params.metadata.uid != "original-ns-uid"'
    assert spec["matchConstraints"]["resourceRules"][0]["operations"] == ["CREATE"]
    assert spec["matchConstraints"]["resourceRules"][0]["scope"] == "Namespaced"
    assert binding["spec"]["paramRef"] == {
        "name": "synthetic-task",
        "parameterNotFoundAction": "Deny",
    }


async def test_inventory_covers_nonpreferred_versions_and_rejects_unknown_unlistable_api(
    monkeypatch,
):
    import json

    adapter = configured()
    docs = {
        "/api": {"versions": ["v1"]},
        "/apis": {
            "groups": [
                {
                    "preferredVersion": {"groupVersion": "example.dev/v1"},
                    "versions": [
                        {"groupVersion": "example.dev/v1"},
                        {"groupVersion": "example.dev/v1alpha2"},
                    ],
                }
            ]
        },
        "/api/v1": {"resources": []},
        "/apis/example.dev/v1": {"resources": []},
        "/apis/example.dev/v1alpha2": {
            "resources": [
                {
                    "name": "hiddenroutes",
                    "kind": "HiddenRoute",
                    "namespaced": True,
                    "verbs": ["get", "list"],
                }
            ]
        },
        "/apis/example.dev/v1alpha2/namespaces/synthetic-task/hiddenroutes": {
            "metadata": {},
            "items": [
                {
                    "metadata": {
                        "name": "foreign",
                        "namespace": "synthetic-task",
                        "uid": "foreign-uid",
                    }
                }
            ],
        },
    }

    async def kubectl(*args, **kwargs):
        return json.dumps(docs[args[-1]])

    monkeypatch.setattr(adapter, "_kubectl", kubectl)
    found = await adapter._retirement_inventory("synthetic-task")
    assert found[0][1]["kind"] == "HiddenRoute"
    assert "v1alpha2" in found[0][0]
    docs["/apis/example.dev/v1"]["resources"] = [
        {"name": "unknown", "kind": "Unknown", "namespaced": True, "verbs": ["create", "delete"]}
    ]
    with pytest.raises(ResourceUnknown, match="unknown namespaced API"):
        await adapter._retirement_inventory("synthetic-task")


async def test_unbound_claim_refuses_current_state_as_historical_provisioning_proof(monkeypatch):
    adapter, operation, objects, _ = fixture(monkeypatch)
    claim = objects["pvc", "bbx-journal"]
    claim["spec"].pop("volumeName")
    claim["status"] = {"phase": "Pending"}
    operation["sandbox_create_claimed"] = False
    operation["data"] = {"exit_confirmed": True, "exit_reason": "not_dispatched"}
    operation["observation"].pop("sandbox")
    operation["observation"].pop("runner")
    with pytest.raises(ResourceUnknown, match="frozen provisioning history"):
        await adapter._retirement_plan(operation, {})


def test_vap_probe_collision_never_deletes_uncreated_policy(monkeypatch, tmp_path):
    import json
    import runpy
    import subprocess
    import sys
    from pathlib import Path
    from types import SimpleNamespace

    checked = []
    deleted = []
    monkeypatch.setitem(
        sys.modules,
        "common",
        SimpleNamespace(
            CONFIG=tmp_path / "kubeconfig",
            KUBECTL=tmp_path / "kubectl",
            STATE=tmp_path,
            owned_cluster=lambda: checked.append(True),
        ),
    )
    scope = runpy.run_path(
        str(Path(__file__).resolve().parents[3] / "deploy/kubernetes/p3g/vap_probe.py")
    )

    def call(*args, body=None, check=True):
        assert checked
        if args[0] == "create":
            if body["kind"] == "ValidatingAdmissionPolicy":
                assert check is False
                return subprocess.CompletedProcess(args, 1, b"", b"already exists")
            assert body["kind"] == "Namespace"
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps(
                    {
                        "metadata": {
                            "name": body["metadata"]["name"],
                            "uid": "original-" + body["metadata"]["name"],
                        }
                    }
                ).encode(),
                b"",
            )
        assert args[0:2] == ("delete", "--raw")
        deleted.append((args[2], body["preconditions"]["uid"]))
        return subprocess.CompletedProcess(args, 0, b"{}", b"")

    scope["main"].__globals__["call"] = call
    scope["main"]()
    assert len(deleted) == 2
    assert all(
        path.startswith("/api/v1/namespaces/") and uid.startswith("original-")
        for path, uid in deleted
    )
