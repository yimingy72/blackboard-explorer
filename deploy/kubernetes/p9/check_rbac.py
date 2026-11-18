"""Check P9 admission using disposable namespaces and server-side dry-runs."""

import argparse
import json
import subprocess
from uuid import uuid4


def verify(kubeconfig):
    kube = ["kubectl", "--kubeconfig", kubeconfig, "--context", "kind-bbx-local-native-k8s"]

    def account(home, name):
        return kube + [
            f"--as=system:serviceaccount:{home}:{name}",
            "--as-group=system:serviceaccounts",
            f"--as-group=system:serviceaccounts:{home}",
            "--as-group=system:authenticated",
        ]

    actor = account("bbx-platform", "bbx-runtime")
    server = account("bbx-local-native", "bbx-p9-sandbox-server")
    controller = account("bbx-local-native", "opensandbox-controller-manager")
    task, operation = str(uuid4()), str(uuid4())
    namespace, legacy = (f"bbx-task-{task}-r{run}" for run in (2, 3))
    report, created = [], []

    def doc(kind, name, *, api="v1", scope=None, **fields):
        return {
            "apiVersion": api,
            "kind": kind,
            "metadata": {
                "name": name,
                **({"namespace": scope} if scope else {}),
                "labels": {
                    "bbx.dev/managed-by": "runner-resources",
                    "bbx.dev/task-id": task,
                    "bbx.dev/operation-id": operation,
                    "bbx.dev/kubernetes-platform": "bbx-platform",
                },
                "annotations": {"bbx.dev/source-id": "bbx-local-native-1effbcb"},
            },
            **fields,
        }

    def create(resource, *, denied=None, real=False, who=actor):
        command = who + ["create", *([] if real else ["--dry-run=server"]), "-f", "-"]
        result = subprocess.run(
            command, input=json.dumps(resource), text=True, capture_output=True, timeout=15
        )
        if denied is None:
            assert result.returncode == 0, result.stderr
        else:
            assert result.returncode != 0 and denied in result.stderr, result.stderr
        report.append([resource["kind"], denied or "allowed"])

    def binding(
        scope,
        role="bbx-platform-task-resources",
        subject="bbx-runtime",
        *,
        name="bbx-runtime",
        home="bbx-platform",
    ):
        return doc(
            "RoleBinding",
            name,
            api="rbac.authorization.k8s.io/v1",
            scope=scope,
            roleRef={"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": role},
            subjects=[{"kind": "ServiceAccount", "name": subject, "namespace": home}],
        )

    def forbidden(scope, verb, resource, *, who=actor):
        result = subprocess.run(
            who + ["auth", "can-i", verb, resource, "-n", scope],
            text=True,
            capture_output=True,
            timeout=15,
        )
        assert result.returncode == 1 and result.stdout.strip() == "no", result.stderr
        report.append([scope, verb, resource, "denied"])

    def legacy_sandbox_read(scope, verb, expected):
        # Discovery cannot resolve this absent legacy API; the server still probes it.
        review = {
            "apiVersion": "authorization.k8s.io/v1",
            "kind": "SubjectAccessReview",
            "spec": {
                "user": "system:serviceaccount:bbx-local-native:bbx-p9-sandbox-server",
                "resourceAttributes": {
                    "namespace": scope,
                    "verb": verb,
                    "group": "sandbox.fast.io",
                    "resource": "sandboxes",
                },
            },
        }
        result = subprocess.run(
            kube + ["create", "-f", "-", "-o", "json"],
            input=json.dumps(review),
            text=True,
            check=True,
            capture_output=True,
            timeout=15,
        )
        assert json.loads(result.stdout)["status"]["allowed"] is expected
        report.append([scope, verb, "sandboxes.sandbox.fast.io", expected])

    try:
        create(doc("Namespace", "bbx-p9-forbidden"), denied="original Blackboard task namespaces")
        create(doc("Namespace", f"bbx-task-{task}"))
        create(doc("Namespace", namespace), real=True)
        created.append(namespace)
        legacy_fixture = doc("Namespace", legacy)
        del legacy_fixture["metadata"]["labels"]["bbx.dev/kubernetes-platform"]
        create(legacy_fixture, real=True, who=kube)
        created.append(legacy)
        for outside in ("bbx-platform", legacy):
            create(binding(outside), denied="only fixed P9 task roles")
        create(binding(namespace, subject="default"), denied="only fixed P9 task roles")
        create(binding(namespace, role="cluster-admin"), denied="not currently held")
        create(binding(namespace), real=True)
        create(
            binding(
                namespace,
                role="bbx-platform-sandbox-resources",
                name="bbx-p9-sandbox-server",
                subject="bbx-p9-sandbox-server",
                home="bbx-local-native",
            ),
            real=True,
        )
        for outside in ("bbx-platform", "kube-system", "bbx-local-native", legacy):
            forbidden(outside, "get", "secrets")
            forbidden(outside, "create", "pods/exec")
            forbidden(outside, "create", "pods/portforward")
            forbidden(outside, "create", "batchsandboxes.sandbox.opensandbox.io", who=server)
        legacy_sandbox_read(namespace, "list", True)
        legacy_sandbox_read(namespace, "create", False)
        legacy_sandbox_read(legacy, "list", False)
        for kind in (
            "pools.sandbox.opensandbox.io",
            "sandboxtemplates.sandbox.fast.io",
            "sandboxsnapshots.sandbox.opensandbox.io",
            "pods",
            "secrets",
        ):
            forbidden(namespace, "create", kind, who=server)
        batch = doc(
            "BatchSandbox",
            "fixture",
            api="sandbox.opensandbox.io/v1alpha1",
            scope=namespace,
            spec={"replicas": 1},
        )
        create(batch, who=server)
        create(
            {**batch, "spec": {"replicas": 1, "poolRef": "forbidden"}},
            denied="only individual sandboxes",
            who=server,
        )
        pod = {
            "automountServiceAccountToken": False,
            "containers": [
                {
                    "name": "fixture",
                    "image": "example.invalid/fixture",
                    "securityContext": {"allowPrivilegeEscalation": False},
                }
            ],
        }
        host_path = {"volumes": [{"name": "bad", "hostPath": {"path": "/tmp"}}]}
        create(doc("Pod", "fixture", scope=namespace, spec=pod))
        for override in ({"hostNetwork": True}, host_path, {"automountServiceAccountToken": True}):
            create(
                doc("Pod", "fixture", scope=namespace, spec={**pod, **override}),
                denied="original task isolation boundary",
            )
        solver = {
            "automountServiceAccountToken": False,
            "containers": [
                {
                    "name": "sandbox",
                    "command": ["/opt/bbx/control/launcher", "--kubernetes-bootstrap-cgroup-v2"],
                    "image": "bbx-p4m-vpn-sandbox@sha256:"
                    "05a46bc49dc8e0123063a304b4c531020f651b3b82c5b0a2aeea4f4ee8181ccb",
                    "securityContext": {"capabilities": {"drop": ["ALL"], "add": ["SYS_ADMIN"]}},
                }
            ],
        }
        delegated = doc("Pod", "sandbox-fixture", scope=namespace, spec=solver)
        delegated["metadata"]["ownerReferences"] = [
            {
                "apiVersion": "sandbox.opensandbox.io/v1alpha1",
                "kind": "BatchSandbox",
                "name": "fixture",
                "uid": operation,
            }
        ]
        create(delegated, who=controller)
        create(
            {**delegated, "spec": {**solver, **host_path}},
            denied="sandbox launcher and volume boundary",
            who=controller,
        )
        claim = {
            "storageClassName": "standard",
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": "128Mi"}},
        }
        create(doc("PersistentVolumeClaim", "bbx-journal", scope=namespace, spec=claim))
        create(
            doc(
                "PersistentVolumeClaim",
                "bbx-journal",
                scope=namespace,
                spec={**claim, "volumeName": "no-such-pv"},
            ),
            denied="original task storage claims",
        )
    finally:
        for name in created:
            subprocess.run(
                kube + ["delete", "namespace", name, "--ignore-not-found=true", "--wait=false"],
                check=True,
                capture_output=True,
                timeout=15,
            )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    print(json.dumps(verify(parser.parse_args().kubeconfig), indent=2))
