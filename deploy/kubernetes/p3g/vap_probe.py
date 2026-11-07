"""Native single-server CEL seam; cleanup only UIDs returned by successful creates."""

import json
import subprocess
import time

from common import CONFIG, KUBECTL, STATE, owned_cluster


def call(*args, body=None, check=True):
    result = subprocess.run(
        [str(KUBECTL), "--kubeconfig", str(CONFIG), *args],
        input=json.dumps(body).encode() if body else None,
        capture_output=True,
    )
    if check and result.returncode:
        raise RuntimeError("Native CEL seam failed")
    return result


def main():
    owned_cluster()
    ns, other, name = "bbx-p3g-vap-check", "bbx-p3g-vap-check-other", "bbx-p3g-vap-check"
    label = {"bbx.dev/probe": "p3g-vap-check"}
    created = []
    evidence = {"cases": []}
    admission = "/apis/admissionregistration.k8s.io/v1/"
    paths = {
        "Namespace": "/api/v1/namespaces/",
        "ValidatingAdmissionPolicy": admission + "validatingadmissionpolicies/",
        "ValidatingAdmissionPolicyBinding": admission + "validatingadmissionpolicybindings/",
    }

    def create(doc, *, check=True):
        result = call("create", "-f", "-", "-o", "json", body=doc, check=check)
        if result.returncode:
            return None
        obj = json.loads(result.stdout)
        ref = {"path": paths[doc["kind"]] + obj["metadata"]["name"], "uid": obj["metadata"]["uid"]}
        created.append(ref)
        return obj

    def delete(ref):
        call(
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
        created.remove(ref)

    try:
        for namespace in [ns, other]:
            value = create(
                {
                    "apiVersion": "v1",
                    "kind": "Namespace",
                    "metadata": {"name": namespace, "labels": label},
                }
            )
            evidence["namespace_uid" if namespace == ns else "other_namespace_uid"] = value[
                "metadata"
            ]["uid"]
        uid = evidence["namespace_uid"]
        for expression in [
            "namespaceObject.metadata.uid != " + json.dumps(uid),
            "namespaceObject.metadata.UID != " + json.dumps(uid),
            "params.metadata.uid != " + json.dumps(uid),
        ]:
            param = expression.startswith("params")
            spec = {
                "failurePolicy": "Fail",
                "matchConstraints": {
                    "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": ns}},
                    "resourceRules": [
                        {
                            "apiGroups": [""],
                            "apiVersions": ["v1"],
                            "operations": ["CREATE"],
                            "resources": ["configmaps"],
                            "scope": "Namespaced",
                        }
                    ],
                },
                "validations": [{"expression": expression, "message": "BBX P3g exact UID fence"}],
            }
            if param:
                spec["paramKind"] = {"apiVersion": "v1", "kind": "Namespace"}
            doc = {
                "apiVersion": "admissionregistration.k8s.io/v1",
                "kind": "ValidatingAdmissionPolicy",
                "metadata": {"name": name, "labels": label},
                "spec": spec,
            }
            if create(doc, check=False) is None:
                evidence["cases"].append({"expression": expression, "create_refused": True})
                continue
            policy_ref = created[-1]
            binding = {
                "apiVersion": doc["apiVersion"],
                "kind": "ValidatingAdmissionPolicyBinding",
                "metadata": {"name": name, "labels": label},
                "spec": {
                    "policyName": name,
                    "validationActions": ["Deny"],
                    "matchResources": {"namespaceSelector": {"matchLabels": label}},
                },
            }
            if param:
                binding["spec"]["paramRef"] = {"name": ns, "parameterNotFoundAction": "Deny"}
            create(binding)
            binding_ref = created[-1]
            time.sleep(2)
            active = json.loads(call("get", "validatingadmissionpolicy", name, "-o", "json").stdout)
            assert active["metadata"]["uid"] == policy_ref["uid"]
            sentinel = {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": name, "namespace": ns},
                "data": {},
            }
            result = call("create", "--dry-run=server", "-f", "-", body=sentinel, check=False)
            evidence["cases"].append(
                {
                    "expression": expression,
                    "status": active.get("status"),
                    "denied": bool(result.returncode),
                    "denial": result.stderr.decode().strip(),
                }
            )
            if (
                result.returncode
                and "denied request: BBX P3g exact UID fence" in result.stderr.decode()
            ):
                alternate = {**sentinel, "metadata": {**sentinel["metadata"], "namespace": other}}
                evidence["cases"][-1]["non_target_allowed"] = (
                    call(
                        "create", "--dry-run=server", "-f", "-", body=alternate, check=False
                    ).returncode
                    == 0
                )
                doc["metadata"].update(
                    uid=policy_ref["uid"], resourceVersion=active["metadata"]["resourceVersion"]
                )
                doc["spec"]["validations"][0]["expression"] = expression.replace(
                    uid, "00000000-0000-0000-0000-000000000000"
                )
                call("replace", "-f", "-", body=doc)
                time.sleep(2)
                evidence["cases"][-1]["wrong_uid_allowed"] = (
                    call(
                        "create", "--dry-run=server", "-f", "-", body=sentinel, check=False
                    ).returncode
                    == 0
                )
            delete(binding_ref)
            delete(policy_ref)
    finally:
        for ref in list(reversed(created)):
            delete(ref)
        (STATE / "vap-cel-evidence.json").write_text(json.dumps(evidence, indent=2))
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()
