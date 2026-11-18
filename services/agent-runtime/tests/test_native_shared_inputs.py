import hashlib
import io
from types import SimpleNamespace
from typing import cast

import pytest
from bbx_contracts.runner_inputs import NativeInputPlan, canonical_input_document
from bbx_runtime import sandbox_inputs
from bbx_runtime.runner_resources import ResourceUnknown, RunnerResources
from opensandbox import Sandbox


@pytest.mark.parametrize(
    "update",
    [
        {},
        {"readOnly": True},
        {"subPath": "nested"},
        {"subPathExpr": "nested"},
        {"mountPropagation": "Bidirectional"},
    ],
)
def test_original_workspace_mount_requires_exact_writable_pvc(update):
    main = {"volumeMounts": [{"name": "original", "mountPath": "/workspace", **update}]}
    pod = {
        "spec": {
            "volumes": [
                {"name": "original", "persistentVolumeClaim": {"claimName": "bbx-workspace"}}
            ]
        }
    }
    if not update:
        RunnerResources._workspace_input_mount(pod, main)
        main["volumeMounts"].append({"name": "unexpected", "mountPath": "/workspace/shared"})
    with pytest.raises(ResourceUnknown):
        RunnerResources._workspace_input_mount(pod, main)


def plan(state):
    scope = {
        "task_id": "00000000-0000-4000-8000-000000000001",
        "operation_id": "00000000-0000-4000-8000-000000000002",
        "resource_generation": 1,
        "workspace_pvc_uid": "00000000-0000-4000-8000-000000000004",
        "sandbox_id": "00000000-0000-4000-8000-000000000005",
        "sandbox_pod_uid": "00000000-0000-4000-8000-000000000006",
        "sandbox_container_id": "containerd://" + "b" * 64,
        "execution_context_id": "a" * 64,
    }
    fid = "00000000-0000-4000-8000-000000000003"
    files = [
        {
            "id": fid,
            "filename": "x.bin",
            "path": f"/workspace/shared/inputs/{fid}/x.bin",
            "size": 1,
            "sha256": hashlib.sha256(b"x").hexdigest(),
        }
    ]
    scope["manifest_sha256"] = hashlib.sha256(canonical_input_document(scope, files)).hexdigest()
    return NativeInputPlan.model_validate({"state": state, "scope": scope, "files": files})


@pytest.mark.asyncio
async def test_claim_without_original_receipt_is_read_only(monkeypatch):
    result = plan("claimed")
    assert result.scope is not None
    methods = []

    async def control(method, endpoint, data=None):
        assert (method, endpoint, data) == ("GET", "inputs", None)
        return result.model_dump(mode="json")

    async def root(sandbox, method, scope):
        methods.append(method)
        return {"result": "unconfirmed"}

    monkeypatch.setattr(sandbox_inputs, "root_input", root)
    host = SimpleNamespace(config=result.scope.model_dump(mode="json"), control=control)
    assert not await sandbox_inputs.reconcile_inputs(host, cast(Sandbox, None))
    assert methods == ["status"]


@pytest.mark.asyncio
async def test_stop_recheck_after_private_staging_never_launches_writer(monkeypatch):
    result = plan("pending")
    assert result.scope is not None
    methods, writes = [], []
    views = iter([result.model_dump(mode="json"), {"state": "blocked"}])

    async def control(method, endpoint, data=None):
        if method == "GET":
            assert endpoint == "inputs"
            return next(views)
        assert (method, endpoint) == ("POST", "inputs/claim")
        return {"state": "claimed", "launch_allowed": True}

    async def root(sandbox, method, scope):
        methods.append(method)
        return {"result": "prepared"}

    async def file(host, item):
        return io.BytesIO(b"x")

    async def write(path, content, *, mode, owner, group):
        assert (mode, owner, group) == (600, "root", "root")
        writes.append(path)

    monkeypatch.setattr(sandbox_inputs, "root_input", root)
    monkeypatch.setattr(sandbox_inputs, "download", file)
    host = SimpleNamespace(config=result.scope.model_dump(mode="json"), control=control)
    sandbox = SimpleNamespace(files=SimpleNamespace(write_file=write))
    assert not await sandbox_inputs.reconcile_inputs(host, cast(Sandbox, sandbox))
    assert methods == ["prepare"]
    assert len(writes) == 2
    assert all(path.startswith(sandbox_inputs.SCRATCH + "/") for path in writes)


@pytest.mark.asyncio
async def test_confirmed_same_workspace_does_not_inspect_the_files():
    async def control(method, endpoint, data=None):
        assert (method, endpoint, data) == ("GET", "inputs", None)
        return {"state": "confirmed"}

    assert await sandbox_inputs.reconcile_inputs(
        SimpleNamespace(control=control), cast(Sandbox, None)
    )
