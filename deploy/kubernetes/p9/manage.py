"""Read and redeploy the installed P9 site without applying bootstrap manifests."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

from data_transfer import postgres_snapshot

PLATFORM = "bbx-platform"
SYSTEM = "bbx-local-native"
WORKLOADS = {
    "blackboard": "deployment",
    "runtime": "deployment",
    "postgres": "statefulset",
    "minio": "statefulset",
}
START_ORDER = ("postgres", "minio", "blackboard", "runtime")
# Match the controller advisory lock and sequence in bbx_contracts.runner_bootstrap.
LOCK = 0x42425852554E
FENCE = ("sequence", "public.native_runtime_controller_fence")
DIGEST = re.compile(r"sha256:[a-f0-9]{64}$")
PG_STATE = f"""
BEGIN READ ONLY;
SELECT jsonb_build_object(
  'epoch', (SELECT last_value FROM native_runtime_controller_fence),
  'holders', (SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND granted
      AND classid::bigint={LOCK >> 32} AND objid::bigint={LOCK & 0xFFFFFFFF} AND objsubid=1),
  'pending', (
    EXISTS (SELECT FROM tasks WHERE deleting OR status NOT IN ('finished','failed','stopped')) OR
    EXISTS (SELECT FROM runner_owners WHERE current AND NOT revoked) OR
    EXISTS (SELECT FROM runner_calls WHERE state IN ('admitted','dispatched','unknown')) OR
    EXISTS (SELECT FROM pi_model_calls WHERE state IN ('prepared','dispatched','unknown')) OR
    EXISTS (SELECT FROM progress_jobs WHERE status IN ('queued','running')) OR
    EXISTS (SELECT FROM agent_messages WHERE status IN ('queued','processing')) OR
    EXISTS (SELECT FROM ctf_messages WHERE purpose='review' AND status IN ('queued','leased')) OR
    EXISTS (SELECT FROM ctf_turns WHERE status='running') OR
    EXISTS (SELECT FROM flag_submissions WHERE status IN ('prepared','dispatched','unknown')) OR
    EXISTS (SELECT FROM gateway_execution_attempts WHERE fee_state!='known') OR
    EXISTS (SELECT FROM task_runner_resources WHERE state!='stopped'
       OR NOT data @> '{{"exit_confirmed":true}}'::jsonb) OR
    EXISTS (SELECT FROM task_input_groups WHERE bound_task_id IS NULL
       AND (deleting OR expires_at <= now()))
  ));
COMMIT;
"""

# Executed inside the existing App, with its existing environment and no provider calls.
APP_READBACK = """
import hashlib,json,os,ssl,sys,urllib.request
from urllib.error import HTTPError
from urllib.parse import urlsplit
from xml.etree.ElementTree import tostring
from minio import Minio
from minio.error import S3Error

opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open('http://127.0.0.1:8000/login',timeout=5) as response:
    assert response.status==200
req=urllib.request.Request('http://127.0.0.1:8000/api/task-creation-capabilities',
    headers={'Authorization':'Bearer '+os.environ['SERVICE_TOKEN']})
with opener.open(req,timeout=5) as response:
    modes=json.load(response)
assert modes['blackboard']['available'] and modes['native_ctf']
if sys.argv[1]=='live':
    tls=ssl.create_default_context(cafile=os.environ['PI_MODEL_PEER_CA_FILE'])
    client=urllib.request.build_opener(urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=tls))
    try:
        client.open(urllib.request.Request(
            os.environ['PI_PLAYGROUND_RUNTIME_URL']+'/playground',data=b'{}',
            headers={'Content-Type':'application/json'}),timeout=5)
    except HTTPError as exc:
        assert exc.code==401
    else:
        raise RuntimeError('Runtime accepted an anonymous model request')
u=urlsplit(os.environ['MINIO_ENDPOINT'])
client=Minio(u.netloc,access_key=os.environ['MINIO_ROOT_USER'],
             secret_key=os.environ['MINIO_ROOT_PASSWORD'],secure=u.scheme=='https')
absent={'NoSuchBucketPolicy','ObjectLockConfigurationNotFoundError',
        'NoSuchLifecycleConfiguration','ReplicationConfigurationNotFoundError',
        'ServerSideEncryptionConfigurationNotFoundError','NoSuchTagSet'}
def encode(value):
    if hasattr(value,'toxml'):
        return tostring(value.toxml(None)).decode()
    return value if isinstance(value,(str,bool,int,list,dict,type(None))) else str(value)
def digest(value):
    data=json.dumps(value,sort_keys=True,separators=(',',':')).encode()
    return hashlib.sha256(data).hexdigest()
summary=[]
for bucket in sorted(client.list_buckets(),key=lambda b:b.name):
    name=bucket.name
    options=[]
    for method in ('get_bucket_policy','get_bucket_encryption','get_bucket_lifecycle',
                   'get_bucket_replication','get_object_lock_config','get_bucket_tags'):
        try:
            options.append(encode(getattr(client,method)(name)))
        except S3Error as exc:
            if exc.code not in absent:
                raise
            options.append(None)
    rows=sorted([(x.object_name,x.version_id,x.etag,x.size,x.is_latest,x.is_delete_marker)
                 for x in client.list_objects(name,recursive=True,include_version=True)],key=repr)
    summary.append((name,client.get_bucket_versioning(name).status,options,rows))
print(json.dumps({'buckets':len(summary),'versions_and_markers':sum(len(x[3]) for x in summary),
                  'sha256':digest(summary)}))
"""


class MaintenanceError(RuntimeError):
    pass


def require(value, message):
    if not value:
        raise MaintenanceError(message)


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def pinned(image):
    return isinstance(image, str) and "@" in image and DIGEST.fullmatch(image.rsplit("@", 1)[1])


class Site:
    def __init__(self, kubeconfig, context, namespace):
        require(Path(kubeconfig).is_file(), "The explicit kubeconfig file does not exist")
        self.base = ["kubectl", "--kubeconfig", str(kubeconfig), "--context", context]
        self.namespace = namespace

    def run(self, *args, namespace=None, stdin=None, timeout=30):
        result = subprocess.run(
            [*self.base, "-n", namespace or self.namespace, *args],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        require(result.returncode == 0, f"Kubernetes {args[0]} failed")
        return result.stdout.strip()

    def get(self, kind, name, *, namespace=None):
        return json.loads(self.run("get", kind, name, "-o", "json", namespace=namespace))

    def metadata(self, kind, name, *, namespace=None):
        text = self.run(
            "get",
            kind,
            name,
            "-o",
            "jsonpath={.metadata.uid}:{.metadata.resourceVersion}",
            namespace=namespace,
        )
        require(len(text.split(":")) == 2 and all(text.split(":")), "Incomplete object identity")
        return text

    def protected(self):
        configmaps = (
            "platform-config",
            "runtime-kubeconfig",
            "platform-control-proxy",
            "platform-control-ca",
            "platform-publications",
        )
        secrets = (
            "postgres-auth",
            "minio-auth",
            "blackboard-auth",
            "runtime-auth",
            "blackboard-control-tls",
            "runtime-control-tls",
        )
        result = {
            f"{self.namespace}/{kind}/{name}": self.metadata(kind, name)
            for kind, names in (("configmap", configmaps), ("secret", secrets))
            for name in names
        }
        for kind, name in (
            ("configmap", "bbx-p9-sandbox-template"),
            ("configmap", "bbx-p9-platform-control-ca"),
            ("secret", "bbx-p9-sandbox-server-config"),
            ("secret", "bbx-p3f-signing"),
        ):
            result[f"{SYSTEM}/{kind}/{name}"] = self.metadata(kind, name, namespace=SYSTEM)
        for namespace, names in (
            (self.namespace, ("blackboard", "runtime", "postgres", "minio")),
            (SYSTEM, ("bbx-p9-sandbox-server",)),
        ):
            for name in names:
                result[f"{namespace}/ciliumnetworkpolicy/{name}"] = self.metadata(
                    "ciliumnetworkpolicy", name, namespace=namespace
                )
        services = ("blackboard", "blackboard-control", "runtime-control", "postgres", "minio")
        for name in services:
            svc = self.get("service", name)
            require(svc["spec"]["type"] == "ClusterIP", f"{name} must be an internal Service")
            require(not svc["spec"].get("externalIPs"), f"{name} has an external address")
            result[f"{self.namespace}/service/{name}"] = (
                svc["metadata"]["uid"],
                digest(svc["spec"]),
            )
        return result

    def volumes(self):
        result = {}
        for name in ("postgres", "minio"):
            claim = self.get("pvc", f"data-{name}-0")
            spec = claim["spec"]
            require(
                claim["status"]["phase"] == "Bound"
                and spec["storageClassName"] == "bbx-platform-retain",
                f"{name} needs its original bound platform claim",
            )
            pv = self.get("pv", spec["volumeName"])
            require(
                pv["spec"]["persistentVolumeReclaimPolicy"] == "Retain"
                and pv["spec"]["claimRef"]["uid"] == claim["metadata"]["uid"],
                f"{name} PV identity or retention policy changed",
            )
            result[name] = [claim["metadata"]["uid"], pv["metadata"]["uid"], spec["volumeName"]]
        return result

    def workload(self, name, *, namespace=None):
        return self.get(WORKLOADS.get(name, "deployment"), name, namespace=namespace)

    def pods(self, name, *, namespace=None):
        label = f"app.kubernetes.io/name={name}"
        if namespace == SYSTEM:
            label = f"app.kubernetes.io/instance={name}"
        return json.loads(self.run("get", "pods", "-l", label, "-o", "json", namespace=namespace))[
            "items"
        ]

    def owned_pod(self, name, workload, *, namespace=None):
        items = self.pods(name, namespace=namespace)
        require(len(items) == 1, f"{name} must have exactly one Pod")
        pod = items[0]
        info = pod["metadata"]
        refs = [ref for ref in info.get("ownerReferences", []) if ref.get("controller")]
        require(len(refs) == 1, f"{name} Pod ownership is not established")
        owner = refs[0]
        if WORKLOADS.get(name, "deployment") == "deployment":
            require(owner["kind"] == "ReplicaSet", f"{name} Pod is not Deployment-owned")
            replica = self.get("replicaset", owner["name"], namespace=namespace)["metadata"]
            require(replica["uid"] == owner["uid"], f"{name} ReplicaSet identity changed")
            refs = [ref for ref in replica.get("ownerReferences", []) if ref.get("controller")]
            require(len(refs) == 1, f"{name} ReplicaSet ownership is not established")
            owner = refs[0]
        require(
            owner["uid"] == workload["metadata"]["uid"] and not info.get("deletionTimestamp"),
            f"{name} Pod is not the original workload's",
        )
        return pod

    @staticmethod
    def identity(workload):
        spec = {key: value for key, value in workload["spec"].items() if key != "replicas"}
        return {"uid": workload["metadata"]["uid"], "spec_sha256": digest(spec)}

    def live_workload(self, name, *, namespace=None):
        workload = self.workload(name, namespace=namespace)
        require(workload["spec"]["replicas"] == 1, f"{name} is not at its normal replica count")
        pod = self.owned_pod(name, workload, namespace=namespace)
        images = {c["name"]: c["image"] for c in workload["spec"]["template"]["spec"]["containers"]}
        statuses = {c["name"]: c for c in pod["status"].get("containerStatuses", [])}
        require(
            images.keys() == statuses.keys()
            and all(
                pinned(image)
                and statuses[key]["ready"]
                and statuses[key]["imageID"].rsplit("@", 1)[-1] == image.rsplit("@", 1)[-1]
                for key, image in images.items()
            ),
            f"{name} Pods are not ready with the reviewed digest",
        )
        return {**self.identity(workload), "pod_uid": pod["metadata"]["uid"], "images": images}

    def pg_command(self):
        return (
            *self.base,
            "-n",
            self.namespace,
            "exec",
            "-i",
            "postgres-0",
            "--",
            "sh",
            "-ec",
            'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" "$@"',
            "p10",
        )

    def database(self):
        state = subprocess.run(
            [*self.pg_command(), "-XqAt", "-v", "ON_ERROR_STOP=1", "-f", "-"],
            input=PG_STATE,
            capture_output=True,
            text=True,
            timeout=30,
        )
        require(state.returncode == 0, "PostgreSQL activity or lock check failed")
        return json.loads(state.stdout)

    def pg_data(self):
        rows = postgres_snapshot(self.pg_command())
        fence = rows.pop(FENCE)["last"]
        return {
            "system_id": rows[("identity", "")]["system_identifier"],
            "sha256": digest(sorted(rows.items())),
            "fence": fence,
        }

    def app_data(self):
        return json.loads(
            self.run(
                "exec",
                "deployment/blackboard",
                "-c",
                "app",
                "--",
                "python",
                "-c",
                APP_READBACK,
                "live",
                timeout=120,
            )
        )

    def snapshot(self):
        return {
            "cluster_uid": self.get("namespace", "kube-system")["metadata"]["uid"],
            "workloads": {name: self.live_workload(name) for name in WORKLOADS},
            "sandbox": self.live_workload("bbx-p9-sandbox-server", namespace=SYSTEM),
            "protected": self.protected(),
            "volumes": self.volumes(),
            "postgres": self.pg_data(),
            "minio": self.app_data(),
            "activity": self.database(),
        }

    def set_replicas(self, name, before, after, reviewed):
        actual = self.workload(name)
        require(
            self.identity(actual) == {k: reviewed[k] for k in ("uid", "spec_sha256")}
            and actual["spec"]["replicas"] == before,
            f"{name} changed outside this maintenance",
        )
        patch = [
            {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": actual["metadata"]["resourceVersion"],
            },
            {"op": "test", "path": "/spec/replicas", "value": before},
            {"op": "replace", "path": "/spec/replicas", "value": after},
        ]
        self.run("patch", WORKLOADS[name], name, "--type=json", "-p", json.dumps(patch))

    def wait_stopped(self, name):
        deadline = time.monotonic() + 180
        while self.pods(name):
            require(time.monotonic() < deadline, f"{name} Pod did not stop gracefully")
            time.sleep(1)

    def wait_ready(self, name):
        self.run("rollout", "status", f"{WORKLOADS[name]}/{name}", "--timeout=180s", timeout=195)
        self.live_workload(name)


def ensure_browser_closed():
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", 15800))
        except OSError:
            raise MaintenanceError(
                "Close the existing browser port-forward on 15800 first"
            ) from None


def quiet(activity, holders, epoch):
    require(not activity["pending"], "Tasks, uploads, or calls are still pending or unresolved")
    require(
        activity["holders"] == holders and activity["epoch"] == epoch,
        "Runtime holder count or epoch differs from the reviewed state",
    )


def protection(site, original):
    require(
        site.protected() == original["protected"],
        "A protected Secret, ConfigMap or Service changed",
    )
    require(site.volumes() == original["volumes"], "A retained PVC or PV changed")


def redeploy(site, expected, *, restart_stores=False):
    original = site.snapshot()
    require(digest(original) == expected, "Site changed since check; review a new fingerprint")
    epoch = original["activity"]["epoch"]
    quiet(original["activity"], 1, epoch)
    require(original["postgres"]["fence"] == epoch, "PostgreSQL fence changed during check")
    ensure_browser_closed()
    changed = set()

    def stop(name):
        site.set_replicas(name, 1, 0, original["workloads"][name])
        changed.add(name)
        site.wait_stopped(name)

    def start(name):
        site.set_replicas(name, 0, 1, original["workloads"][name])
        site.wait_ready(name)
        changed.remove(name)

    try:
        stop("blackboard")
        quiet(site.database(), 1, epoch)
        stop("runtime")
        quiet(site.database(), 0, epoch)
        protection(site, original)
        require(site.pg_data() == original["postgres"], "Database changed before the quiet restart")
        if restart_stores:
            for name in ("minio", "postgres"):
                stop(name)
            for name in ("postgres", "minio"):
                start(name)
            quiet(site.database(), 0, epoch)
            protection(site, original)
            require(
                site.pg_data() == original["postgres"], "Database differs after storage restart"
            )
        start("blackboard")
        start("runtime")
        after = site.snapshot()
        quiet(after["activity"], 1, epoch + 1)
        require(
            after["postgres"] == {**original["postgres"], "fence": epoch + 1}
            and all(
                after[key] == original[key]
                for key in ("cluster_uid", "protected", "volumes", "sandbox", "minio")
            ),
            "Data, storage, or a protected platform object differs after restart",
        )
        for name, before in original["workloads"].items():
            actual = after["workloads"][name]
            restarted = restart_stores or name in {"blackboard", "runtime"}
            require(
                {k: v for k, v in actual.items() if k != "pod_uid"}
                == {k: v for k, v in before.items() if k != "pod_uid"}
                and (actual["pod_uid"] != before["pod_uid"]) == restarted,
                f"{name} replica, template, image, or Pod identity differs from the plan",
            )
        return {"redeployed": True, "before": original, "after": after}
    except Exception as error:
        try:
            protection(site, original)
            for name in START_ORDER:
                if name in changed:
                    actual = site.workload(name)
                    if actual["spec"]["replicas"] == 0:
                        site.wait_stopped(name)
                        if name == "runtime":
                            require(
                                site.database()["holders"] == 0,
                                "The previous Runtime lock was not released",
                            )
                        start(name)
                    else:
                        require(
                            site.identity(actual)
                            == {k: original["workloads"][name][k] for k in ("uid", "spec_sha256")},
                            "Workload changed during recovery",
                        )
                        site.wait_ready(name)
        except Exception:
            raise MaintenanceError(
                "Restart failed; recovery could not establish the original workload state. "
                "Keep browser access closed and inspect the current cluster."
            ) from None
        if isinstance(error, MaintenanceError):
            raise
        raise MaintenanceError(
            "Restart verification failed. Inspect the current cluster before reopening access."
        ) from None


def main():
    if not __debug__:
        raise SystemExit("Use Python without -O for the database checks")
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--kubeconfig", type=Path, required=True)
    cli.add_argument("--context", required=True)
    cli.add_argument("--namespace", default=PLATFORM, choices=[PLATFORM])
    commands = cli.add_subparsers(dest="command", required=True)
    commands.add_parser("check")
    restart = commands.add_parser("redeploy")
    restart.add_argument("--expect", required=True, help="Reviewed fingerprint printed by check")
    restart.add_argument("--restart-stores", action="store_true")
    args = cli.parse_args()
    try:
        site = Site(args.kubeconfig, args.context, args.namespace)
        if args.command == "check":
            state = site.snapshot()
            result = {"fingerprint": digest(state), "site": state}
        else:
            result = redeploy(site, args.expect, restart_stores=args.restart_stores)
    except MaintenanceError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (
        OSError,
        ValueError,
        KeyError,
        AssertionError,
        RuntimeError,
        subprocess.SubprocessError,
    ):
        print("Platform inspection failed; no credential or server output shown.", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
