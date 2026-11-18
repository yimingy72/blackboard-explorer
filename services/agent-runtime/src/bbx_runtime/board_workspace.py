"""Archive registered Blackboard material and restore it to a verified new volume."""

import asyncio
import hashlib
import io
import json
import re
import tarfile
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

import zstandard
from bbx_contracts.runner_bootstrap import management_document_digest
from bbx_contracts.runner_control import BoardJournalProjection
from bbx_objects import ObjectStore

from bbx_runtime.execenv.archive import build_archive, initial_attachments
from bbx_runtime.runner_resources import ResourceUnknown, RunnerResources

ARCHIVE_LIMIT = 512 * 1024 * 1024
RESTORE_CODE = r"""
import hashlib, json, os, shutil, stat, sys, tarfile, tempfile
from pathlib import Path

workspace = Path("/workspace")
stub = workspace / ".tmp"
metadata = workspace / ".bbx"
marker = metadata / "restore-receipt.json"
arguments = json.loads(sys.argv[1])
expected = hashlib.sha256(sys.argv[1].encode("ascii")).hexdigest().encode("ascii")
skip = set(arguments["initial_paths"])
journals = set(arguments["journal_paths"])
journal_directories = {
    str(parent) for name in journals for parent in Path(name).parents
    if str(parent) == ".bbx/journals" or str(parent).startswith(".bbx/journals/")
}
input_directories = {
    str(parent) for name in skip for parent in Path(name).parents
    if str(parent) == "shared/inputs" or str(parent).startswith("shared/inputs/")
}

def drain():
    while sys.stdin.buffer.read(65536):
        pass

def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

if metadata.exists() or metadata.is_symlink():
    if metadata.is_symlink() or not metadata.is_dir():
        raise ValueError("Replacement workspace metadata changed")
    fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as proof:
        info = os.fstat(proof.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or proof.read(4097) != expected:
            raise ValueError("Replacement workspace receipt changed")
    drain()
    print("workspace-restored")
    sys.exit(0)

for path in workspace.iterdir():
    if path != stub or path.is_symlink() or not path.is_dir() or any(path.iterdir()):
        raise ValueError("Replacement workspace is not empty")
with tempfile.TemporaryDirectory(prefix=".bbx-restore-", dir=workspace) as directory:
    stage = Path(directory)
    seen, modes = set(), {}
    with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as archive:
        for index, item in enumerate(archive):
            if index >= 100000:
                raise ValueError("Workspace archive has too many entries")
            name = item.name.removeprefix("./").rstrip("/")
            if name in {"", "."} and item.isdir():
                continue
            parts = name.split("/")
            if not name or any(part in {"", ".", ".."} for part in parts) or "\\" in name:
                raise ValueError("Workspace archive contains an unsafe path")
            if name in seen or not (item.isfile() or item.isdir()):
                raise ValueError("Workspace archive contains a duplicate or unsupported entry")
            seen.add(name)
            if parts[0] == ".bbx":
                if not (
                    (name == ".bbx" and item.isdir())
                    or (name in {".bbx/task.json", ".bbx/manifest.json"} and item.isfile())
                    or (name in journals and item.isfile())
                    or (name in journal_directories and item.isdir())
                ):
                    raise ValueError("Workspace archive metadata changed")
                continue
            if name == "shared/inputs" or name.startswith("shared/inputs/"):
                if not (
                    (name in skip and item.isfile()) or
                    (name in input_directories and item.isdir())
                ):
                    raise ValueError("Original input archive paths changed")
                continue
            path = stage.joinpath(*parts)
            if item.isdir():
                path.mkdir(parents=True, exist_ok=True)
                # Historical portable archives emit shared as 01777. The original attachment
                # importer requires this trusted root to be owned and not group/world writable.
                modes[path] = 0o755 if name == "shared" else item.mode & 0o777
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(item) as source, path.open("xb") as output:
                    shutil.copyfileobj(source, output, 65536)
                    output.flush()
                    os.fsync(output.fileno())
                path.chmod(item.mode & 0o777)
    drain()
    if (stage / ".tmp").exists() and stub.exists():
        stub.rmdir()
    for path in sorted(stage.iterdir()):
        destination = workspace / path.name
        if destination.exists() or destination.is_symlink():
            raise ValueError("Replacement workspace changed")
        path.rename(destination)
    for path, mode in sorted(modes.items(), key=lambda item: -len(item[0].parts)):
        (workspace / path.relative_to(stage)).chmod(mode)
    metadata.mkdir(mode=0o700)
    with marker.open("xb") as proof:
        proof.write(expected)
        proof.flush()
        os.fsync(proof.fileno())
    marker.chmod(0o400)
    sync_dir(metadata)
    sync_dir(workspace)
print("workspace-restored")
"""


async def _delete(resources, kind, path, namespace, name, uid):
    await resources._kubectl(
        "delete",
        "--raw",
        path,
        "-f",
        "-",
        body={
            "apiVersion": "v1",
            "kind": "DeleteOptions",
            "preconditions": {"uid": uid},
            **({"gracePeriodSeconds": 0} if kind == "pod" else {}),
        },
    )
    async with asyncio.timeout(25):
        while value := await resources._get(kind, name, namespace):
            if value["metadata"]["uid"] != uid:
                raise ResourceUnknown("The owned archive resource was replaced")
            await asyncio.sleep(0.2)


async def _workspace(resources, operation, namespace, labels, pvc_key="workspace_pvc"):
    observed = operation["observation"]
    await resources._cluster()
    resources._owned(
        await resources._get("namespace", namespace), labels, observed["namespace_uid"]
    )
    ref = observed[pvc_key]
    volume = resources._owned(
        await resources._get("persistentvolumeclaim", ref["name"], namespace), labels, ref["uid"]
    )
    if (
        volume["status"].get("phase") != "Bound"
        or volume["spec"].get("storageClassName") != resources.config["storage_class"]
        or volume["metadata"].get("deletionTimestamp")
    ):
        raise ResourceUnknown("The original workspace volume is not retained")
    return ref


async def _mounted_pods(resources, namespace, claim):
    pods = json.loads(await resources._kubectl("get", "pods", "-n", namespace, "-o", "json"))
    return [
        pod
        for pod in pods["items"]
        if any(
            volume.get("persistentVolumeClaim", {}).get("claimName") == claim
            for volume in pod["spec"].get("volumes", [])
        )
    ]


async def _wait_reader(resources, namespace, name, labels, uid, spec):
    async with asyncio.timeout(40):
        while True:
            pod = resources._owned(await resources._get("pod", name, namespace), labels, uid)
            current = pod["spec"]
            containers = current["containers"]
            if (
                current.get("automountServiceAccountToken") is not False
                or current.get("hostNetwork")
                or current.get("hostPID")
                or current.get("hostIPC")
                or current.get("initContainers")
                or current.get("ephemeralContainers")
                or current.get("volumes") != spec["volumes"]
                or len(containers) != 1
                or any(
                    containers[0].get(key) != value for key, value in spec["containers"][0].items()
                )
                or containers[0].get("env")
                or containers[0].get("envFrom")
                or pod["metadata"].get("deletionTimestamp")
            ):
                raise ResourceUnknown("The workspace reader is not the original isolated Pod")
            if pod["status"].get("phase") == "Running":
                statuses = pod["status"].get("containerStatuses", [])
                if len(statuses) == 1 and statuses[0].get("containerID"):
                    return statuses[0]["containerID"]
            if pod["status"].get("phase") in {"Failed", "Succeeded"}:
                raise ResourceUnknown("The original workspace reader exited before export")
            await asyncio.sleep(0.2)


@asynccontextmanager
async def _transfer_pod(resources, operation, *, journal=False):
    namespace, labels = resources._scope(operation)
    observed = operation["observation"]
    pvc_key = "journal_pvc" if journal else "workspace_pvc"
    ref = await _workspace(resources, operation, namespace, labels, pvc_key)
    component = "board-journal" if journal else "board-restore"
    name = "bbx-" + component
    permitted = None if journal else observed["sandbox"]["pod_uid"]
    pods = await _mounted_pods(resources, namespace, ref["name"])
    if not journal and not any(
        pod["metadata"]["uid"] == permitted and pod["status"].get("phase") == "Running"
        for pod in pods
    ):
        raise ResourceUnknown("The replacement Blackboard Sandbox is unavailable")
    if any(pod["metadata"]["name"] != name and pod["metadata"]["uid"] != permitted for pod in pods):
        raise ResourceUnknown("An original workspace writer is still present")
    uid_value = 1000 if journal else 10000
    mount: dict[str, str | bool] = {
        "name": "workspace",
        "mountPath": "/journal" if journal else "/workspace",
    }
    claim: dict[str, str | bool] = {"claimName": ref["name"]}
    if journal:
        mount["readOnly"] = claim["readOnly"] = True
        subdir = observed.get("journal_subdir")
        if subdir:
            if not re.fullmatch(r"epoch-[1-9][0-9]{0,8}", subdir):
                raise ResourceUnknown("The retained journal mount identity changed")
            mount["subPath"] = subdir
        elif resources.config.get("journal_writer_lock"):
            raise ResourceUnknown("The retained journal mount identity is unavailable")
    reader_labels = {**labels, "bbx.dev/component": component}
    policy = resources._document(
        "CiliumNetworkPolicy",
        name,
        reader_labels,
        namespace,
        spec={
            "endpointSelector": {"matchLabels": reader_labels},
            "ingressDeny": [{"fromEntities": ["all"]}],
            "egressDeny": [{"toEntities": ["all"]}],
        },
    )
    policy["apiVersion"] = "cilium.io/v2"
    deny = await resources._ensure(policy, reader_labels)
    spec = {
        "automountServiceAccountToken": False,
        "restartPolicy": "Never",
        "terminationGracePeriodSeconds": 0,
        "activeDeadlineSeconds": 200,
        "containers": [
            {
                "name": "reader",
                "image": resources.config["runner_image" if journal else "traffic_sandbox_image"],
                "imagePullPolicy": "IfNotPresent",
                "command": ["/usr/bin/sleep", "200"],
                "resources": {
                    "requests": {"cpu": "10m", "memory": "32Mi"},
                    "limits": {"cpu": "200m", "memory": "128Mi"},
                },
                "securityContext": {
                    "runAsUser": uid_value,
                    "runAsGroup": uid_value,
                    "runAsNonRoot": True,
                    "allowPrivilegeEscalation": False,
                    "readOnlyRootFilesystem": True,
                    "capabilities": {"drop": ["ALL"]},
                    "seccompProfile": {"type": "RuntimeDefault"},
                },
                "volumeMounts": [mount],
            }
        ],
        "volumes": [
            {
                "name": "workspace",
                "persistentVolumeClaim": claim,
            }
        ],
    }
    uid = None
    try:
        pod = await resources._ensure(
            resources._document("Pod", name, reader_labels, namespace, spec=spec), reader_labels
        )
        uid = pod["metadata"]["uid"]
        original = await _wait_reader(resources, namespace, name, reader_labels, uid, spec)
        yield namespace, name, ref
        if original != await _wait_reader(resources, namespace, name, reader_labels, uid, spec):
            raise ResourceUnknown("The original workspace reader process changed")
        await _workspace(resources, operation, namespace, labels, pvc_key)
    finally:
        if uid is not None:
            await _delete(
                resources,
                "pod",
                f"/api/v1/namespaces/{namespace}/pods/{name}",
                namespace,
                name,
                uid,
            )
        elif await resources._get("pod", name, namespace) is not None:
            raise ResourceUnknown("The workspace reader ownership is not confirmed")
        await _delete(
            resources,
            "ciliumnetworkpolicy",
            f"/apis/cilium.io/v2/namespaces/{namespace}/ciliumnetworkpolicies/{name}",
            namespace,
            name,
            deny["metadata"]["uid"],
        )


def inspect_archive(source, tid, audit):
    metadata = {}
    journal_files = {}
    journal_dirs = set()
    with zstandard.ZstdDecompressor(max_window_size=64 * 1024 * 1024).stream_reader(
        source, closefd=False
    ) as raw:

        class Bounded(io.RawIOBase):
            size = 0

            def read(self, size=-1):
                block = raw.read(size)
                self.size += len(block)
                if self.size > ARCHIVE_LIMIT:
                    raise ResourceUnknown("Original workspace archive exceeds its restore bound")
                return block

        bounded = Bounded()
        with tarfile.open(fileobj=bounded, mode="r|") as archive:
            seen = set()
            for index, member in enumerate(archive):
                if (
                    index >= 100000
                    or member.name in seen
                    or not (member.isfile() or member.isdir())
                ):
                    raise ResourceUnknown(
                        "Original registered archive contains an unexpected entry"
                    )
                seen.add(member.name)
                if member.name in {".bbx/task.json", ".bbx/manifest.json"}:
                    if not member.isfile() or member.size > 128 * 1024 * 1024:
                        raise ResourceUnknown("Original archive metadata is not bounded")
                    content = archive.extractfile(member)
                    assert content is not None
                    metadata[member.name] = json.load(content)
                elif member.name == ".bbx/journals" or member.name.startswith(".bbx/journals/"):
                    if member.isdir():
                        journal_dirs.add(member.name)
                    else:
                        if not 0 < member.size <= 64 * 1024 * 1024:
                            raise ResourceUnknown("Original archive journal is outside its bound")
                        content = archive.extractfile(member)
                        assert content is not None
                        digest = hashlib.sha256()
                        size = 0
                        while chunk := content.read(65536):
                            size += len(chunk)
                            digest.update(chunk)
                        journal_files[member.name] = (size, digest.hexdigest())
        while bounded.read(65536):
            pass
    snapshot = metadata.get(".bbx/task.json", {})
    manifest = metadata.get(".bbx/manifest.json", {})
    if (
        snapshot.get("format") != "bbx.task-archive.v1"
        or snapshot.get("task_id") != str(tid)
        or snapshot.get("run_number") != audit["run_number"]
        or snapshot.get("runtime_audit", {}).get("native_workspace") != audit
        or manifest.get("task_id") != str(tid)
        or manifest.get("run_number") != audit["run_number"]
    ):
        raise ResourceUnknown("Original workspace archive scope changed")
    expected_journals = {}
    journal_proofs = []
    expected_dirs = set()
    for item in manifest.get("board_journals", []):
        aid = item["agent_id"]
        if not isinstance(aid, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", aid):
            raise ResourceUnknown("Original archive journal Agent identity changed")
        proof = BoardJournalProjection.model_validate(
            {k: value for k, value in item.items() if k not in {"path", "agent_id"}}
        )
        directory = f".bbx/journals/{aid}/run-{proof.run_number}"
        name = f"{directory}/{proof.binding_id}-{proof.cursor.hash}.jsonl"
        if (
            item["path"] != name
            or proof.run_number != audit["run_number"]
            or name in expected_journals
            or proof.transcript_uri
            != f"traces/{tid}/{aid}/{proof.binding_id}/{proof.cursor.hash}.jsonl"
        ):
            raise ResourceUnknown("Original archive journal reference changed")
        expected_journals[name] = (proof.size, proof.sha256)
        journal_proofs.append({"agent_id": aid, "projection": proof.model_dump(mode="json")})
        expected_dirs.update({".bbx/journals", f".bbx/journals/{aid}", directory})
    if expected_journals != journal_files or not journal_dirs <= expected_dirs:
        raise ResourceUnknown("Original archive journal bytes changed")
    inputs = [
        item["path"].removeprefix("/workspace/")
        for item in initial_attachments(UUID(str(tid)), manifest.get("initial_attachments", []))
    ]
    journal_proofs.sort(key=lambda item: item["projection"]["binding_id"])
    return (
        inputs,
        sorted(expected_journals),
        management_document_digest({"journals": journal_proofs}),
    )


async def archive_board_workspace(
    resources: RunnerResources, operation: dict, data: dict, objects: ObjectStore
):
    if operation["state"] != "stopped" or not operation["data"].get("exit_confirmed"):
        raise ResourceUnknown("The original Blackboard workspace writer has not exited")
    tid = UUID(operation["task_id"])
    run_number = data["run_number"]
    if type(run_number) is not int or run_number < 1:
        raise ResourceUnknown("Original workspace run number is unavailable")
    uri = (
        f"workspace/{tid}.tar.zst"
        if run_number == 1
        else f"workspace/{tid}/run-{run_number}.tar.zst"
    )
    namespace, labels = resources._scope(operation)
    ref = await _workspace(resources, operation, namespace, labels)
    if await _mounted_pods(resources, namespace, ref["name"]):
        raise ResourceUnknown("An original workspace writer is still present")
    audit = {
        "operation_id": operation["operation_id"],
        "resource_generation": operation["data"].get("resource_generation", 1),
        "workspace_pvc_uid": ref["uid"],
        "run_number": run_number,
    }
    with tempfile.TemporaryDirectory(prefix="bbx-board-archive-") as directory:
        path = Path(directory) / "archive.tar.zst"
        existed = await objects.exists(uri)
        if existed:
            with path.open("wb") as output:
                async for block in objects.stream(uri):
                    if output.tell() + len(block) > ARCHIVE_LIMIT:
                        raise ResourceUnknown("Original workspace archive exceeds its size bound")
                    output.write(block)
        else:
            snapshot = {
                **data,
                "runtime_audit": {**data.get("runtime_audit", {}), "native_workspace": audit},
            }
            await build_archive(objects, tid, snapshot, path)
        size = path.stat().st_size
        if not 0 < size <= ARCHIVE_LIMIT:
            raise ResourceUnknown("Original workspace archive exceeds its size bound")
        with path.open("rb") as output:
            digest = hashlib.file_digest(output, "sha256").hexdigest()
            output.seek(0)
            _, _, journals_sha256 = inspect_archive(output, tid, audit)
            output.seek(0)
            if not existed:
                await objects.put(uri, output, length=size, content_type="application/zstd")
        saved = hashlib.sha256()
        observed_size = 0
        async for block in objects.stream(uri):
            observed_size += len(block)
            if observed_size > size:
                raise ResourceUnknown("The original workspace archive object changed")
            saved.update(block)
        if observed_size != size or saved.hexdigest() != digest:
            raise ResourceUnknown("The original workspace archive object did not match")
    await _workspace(resources, operation, namespace, labels)
    return uri, size, {**audit, "sha256": digest, "journals_sha256": journals_sha256}


async def _restore_stream(resources, namespace, name, source, receipt):
    process = await asyncio.create_subprocess_exec(
        "kubectl",
        "--kubeconfig",
        resources.config["kubeconfig"],
        "--context",
        resources.config["context"],
        "-n",
        namespace,
        "exec",
        "-i",
        name,
        "-c",
        "reader",
        "--",
        "/usr/bin/python3",
        "-I",
        "-S",
        "-c",
        RESTORE_CODE,
        receipt,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    assert process.stdin is not None and process.stdout is not None
    try:
        async with asyncio.timeout(100):
            raw = 0
            with zstandard.ZstdDecompressor(max_window_size=64 * 1024 * 1024).stream_reader(
                source, closefd=False
            ) as reader:
                while block := reader.read(65536):
                    raw += len(block)
                    if raw > ARCHIVE_LIMIT:
                        raise ResourceUnknown(
                            "Original workspace archive exceeds the restore limit"
                        )
                    process.stdin.write(block)
                    await process.stdin.drain()
            process.stdin.close()
            result = await process.stdout.read(4096)
            if await process.wait() or result != b"workspace-restored\n":
                raise ResourceUnknown("Original workspace restore is not confirmed")
    except (BrokenPipeError, ConnectionResetError, zstandard.ZstdError):
        raise ResourceUnknown("Original workspace restore bytes were not confirmed") from None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def restore_board_workspace(
    resources: RunnerResources, operation: dict, objects: ObjectStore
):
    data = operation["data"]
    if data.get("credentials_issued") or data.get("sandbox_host_ready"):
        raise ResourceUnknown("Replacement workspace already has an execution writer")
    source = data["board_workspace_source"]
    tid = str(UUID(operation["task_id"]))
    run = source["run_number"]
    expected = f"workspace/{tid}.tar.zst" if run == 1 else f"workspace/{tid}/run-{run}.tar.zst"
    size = source["size"]
    if source["uri"] != expected or type(size) is not int or not 0 < size <= ARCHIVE_LIMIT:
        raise ResourceUnknown("Original workspace archive identity is unavailable")
    proof = {
        "uri": expected,
        "run_number": run,
        "sha256": source["sha256"],
        "sandbox_pod_uid": operation["observation"]["sandbox"]["pod_uid"],
    }
    with tempfile.TemporaryFile() as compressed:
        saved = hashlib.sha256()
        async for block in objects.stream(expected):
            if compressed.tell() + len(block) > size:
                raise ResourceUnknown("Original workspace archive length changed")
            saved.update(block)
            compressed.write(block)
        if compressed.tell() != size or saved.hexdigest() != source["sha256"]:
            raise ResourceUnknown("Original workspace archive hash changed")
        compressed.seek(0)
        initial_paths, journal_paths, _ = inspect_archive(
            compressed,
            tid,
            {
                key: source[key]
                for key in (
                    "operation_id",
                    "resource_generation",
                    "workspace_pvc_uid",
                    "run_number",
                )
            },
        )
        compressed.seek(0)
        async with _transfer_pod(resources, operation) as (namespace, name, ref):
            receipt = json.dumps(
                {
                    **proof,
                    "operation_id": operation["operation_id"],
                    "workspace_pvc_uid": ref["uid"],
                    "initial_paths": initial_paths,
                    "journal_paths": journal_paths,
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            await _restore_stream(resources, namespace, name, compressed, receipt)
    return proof


async def read_retained_board_journal(resources, operation, aid, receipt, output):
    if (
        operation["state"] != "stopped"
        or not operation["data"].get("exit_confirmed")
        or receipt["fence"]["task_id"] != operation["task_id"]
        or receipt["fence"]["pod_uid"] != operation["observation"]["runner"]["pod_uid"]
    ):
        raise ResourceUnknown("The original journal writer exit or source is not confirmed")
    async with _transfer_pod(resources, operation, journal=True) as (namespace, name, _):
        return await resources._copy_board_journal(namespace, name, "reader", aid, receipt, output)
