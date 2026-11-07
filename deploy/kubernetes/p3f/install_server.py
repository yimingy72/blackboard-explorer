"""Install the fixed official control chain in the already verified private cluster."""

import argparse
import base64
import hashlib
import json
import os
import secrets
import subprocess
from pathlib import Path

from common import CONFIG, HERE, KUBECTL, ROOT, STATE, SYSTEM, kubectl, owned_cluster


def apply(doc):
    return json.loads(kubectl("apply", "-f", "-", "-o", "json", data=doc))


def install(
    endpoint,
    resolver_file,
    ca_file,
    ingress_host,
    *,
    input_state=None,
    owner="p3f",
    server_image="bbx-p3f-server-g7:20261106",
    environment_revision="bbx-p3f-ubuntu-g1-20261106",
):
    owned_cluster()
    inputs = input_state or STATE
    repository, tag = server_image.rsplit(":", 1)
    namespaces = json.loads(kubectl("get", "namespace", "-o", "json"))["items"]
    existing = next((ns for ns in namespaces if ns["metadata"]["name"] == SYSTEM), None)
    if existing and existing["metadata"].get("labels", {}).get("bbx.dev/owner") != owner:
        raise RuntimeError("Foreign control namespace refused")
    namespace = apply(
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": SYSTEM, "labels": {"bbx.dev/owner": owner}},
        }
    )
    secret_raw = kubectl(
        "-n", SYSTEM, "get", "secret", "bbx-p3f-signing", "--ignore-not-found", "-o", "json"
    )
    if not secret_raw.strip():
        apply(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "namespace": SYSTEM,
                    "name": "bbx-p3f-signing",
                    "labels": {"bbx.dev/owner": owner},
                },
                "stringData": {
                    "active-key": "a",
                    "keys": "a=" + base64.b64encode(secrets.token_bytes(32)).decode(),
                },
            }
        )
    else:
        assert json.loads(secret_raw)["metadata"]["labels"].get("bbx.dev/owner") == owner
    apply(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "namespace": SYSTEM,
                "name": "bbx-p3f-platform-ca",
                "labels": {"bbx.dev/owner": owner},
            },
            "stringData": {"ca.pem": Path(ca_file).read_text()},
        }
    )
    template = json.loads((ROOT / "deploy/kubernetes/g1/batchsandbox-template.json").read_text())
    template["metadata"]["name"] = "bbx-p3f-template"
    env = template["spec"]["shardPatches"][0]["spec"]["containers"][0]["env"]
    env[0]["valueFrom"]["secretKeyRef"]["name"] = "bbx-execd"
    env[2]["value"] = environment_revision
    apply(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "namespace": SYSTEM,
                "name": "bbx-p3f-template",
                "labels": {"bbx.dev/owner": owner},
            },
            "data": {"template.json": json.dumps(template)},
        }
    )
    common = {"namespaceOverride": SYSTEM}
    controller = {
        **common,
        "controller": {
            "image": {
                "repository": "opensandbox/controller",
                "tag": "release-1.2.0@sha256:"
                "7701db7eacda4c3533a961e0451f6ce067535d8ce46f15765a04dcf0ffa6e2c6",
                "pullPolicy": "IfNotPresent",
            },
            "resources": {
                "requests": {"cpu": "20m", "memory": "64Mi"},
                "limits": {"cpu": "250m", "memory": "256Mi"},
            },
        },
    }
    ingress = {
        **common,
        "fullnameOverride": "bbx-p3f-ingress",
        "gateway": {
            "image": {
                "repository": "opensandbox/ingress",
                "tag": "release-1.2.0@sha256:"
                "c7fc7faa8e48793c6c9e74bb1178b46121abdaba315b51704163a8faa1f7cc83",
                "pullPolicy": "IfNotPresent",
            },
            "replicaCount": 1,
            "dataplaneNamespace": "",
            "gatewayRouteMode": "header",
            "secureAccess": {"existingSecret": "bbx-p3f-signing"},
            "resources": {
                "requests": {"cpu": "20m", "memory": "64Mi"},
                "limits": {"cpu": "250m", "memory": "256Mi"},
            },
        },
    }
    q = json.dumps
    toml = f"""[server]
host="0.0.0.0"
port=80
api_key=""
[log]
level="WARNING"
[runtime]
type="kubernetes"
execd_image="opensandbox/execd:release-1.2.0@sha256:c75836c46ef4302afccec182d5d7339eb2f905f51e3d73b7aa17bb76c832526c"
execd_run_as_init=true
[kubernetes]
namespace="bbx-p3f-unallocated"
workload_provider="batchsandbox"
batchsandbox_template_file="/etc/opensandbox/p3f-template.json"
batchsandbox_informer_enabled=false
image_pull_policy="IfNotPresent"
read_qps=20
read_burst=40
[kubernetes.execd_init_resources]
requests={{cpu="10m",memory="32Mi"}}
limits={{cpu="200m",memory="128Mi"}}
[tenants]
provider="http"
endpoint={q(endpoint)}
auth_header="x-bbx-sandbox-resolver"
auth_token="__BBX_RESOLVER__"
cache_max_entries=32
max_stale_seconds=0
timeout_seconds=3
"""
    server_digest = subprocess.check_output(
        ["docker", "image", "inspect", server_image, "--format", "{{.Id}}"],
        text=True,
    ).strip()
    patch_identity = json.loads((STATE / "build-source.json").read_text())["g7_identity"]
    content = {
        "toml": toml,
        "ca": Path(ca_file).read_text(),
        "resolver": Path(resolver_file).read_text(),
        "template": template,
        "server_digest": server_digest,
        "g7_identity": patch_identity,
    }
    server = {
        **common,
        "fullnameOverride": "bbx-p3f-server",
        "server": {
            "image": {
                "repository": repository,
                "tag": tag + "@" + server_digest,
                "pullPolicy": "Never",
            },
            "resources": {
                "requests": {"cpu": "100m", "memory": "256Mi"},
                "limits": {"cpu": "500m", "memory": "768Mi"},
            },
            "gateway": {
                "enabled": True,
                "host": ingress_host,
                "gatewayRouteMode": "header",
                "secureAccess": {"existingSecret": "bbx-p3f-signing"},
            },
            "env": [{"name": "SSL_CERT_FILE", "value": "/var/run/bbx-platform/ca.pem"}],
            "volumeMounts": [
                {
                    "name": "template",
                    "mountPath": "/etc/opensandbox/p3f-template.json",
                    "subPath": "template.json",
                    "readOnly": True,
                },
                {"name": "ca", "mountPath": "/var/run/bbx-platform", "readOnly": True},
            ],
            "volumes": [
                {"name": "template", "configMap": {"name": "bbx-p3f-template"}},
                {"name": "ca", "secret": {"secretName": "bbx-p3f-platform-ca"}},
            ],
            "podAnnotations": {
                "bbx.dev/content-sha256": hashlib.sha256(
                    json.dumps(content, sort_keys=True).encode()
                ).hexdigest()
            },
        },
        "configToml": toml,
    }
    env_vars = {
        **os.environ,
        "KUBECONFIG": str(CONFIG),
        "BBX_POSTRENDER_KUBECTL": str(KUBECTL),
        "BBX_POSTRENDER_KUBECONFIG": str(CONFIG),
        "HELM_CACHE_HOME": str(STATE / "helm-cache"),
        "HELM_CONFIG_HOME": str(STATE / "helm-config"),
        "HELM_DATA_HOME": str(STATE / "helm-data"),
        "BBX_P3F_RESOLVER_TOKEN_FILE": str(Path(resolver_file).resolve()),
    }
    for component, chart, values in [
        ("controller", "controller", controller),
        ("server", "server", server),
        ("ingress", "ingress-gateway", ingress),
    ]:
        values_path = STATE / (component + "-values.json")
        values_path.write_text(json.dumps(values))
        values_path.chmod(0o600)
        command = [
            str(inputs / "bin/helm"),
            "--kubeconfig",
            str(CONFIG),
            "upgrade",
            "--install",
            "bbx-p3f-" + component,
            str(inputs / "upstream/manifests/charts" / chart),
            "-n",
            SYSTEM,
            "-f",
            str(values_path),
        ]
        if component == "server":
            command += ["--post-renderer", str(HERE / "server_postrender.py")]
        subprocess.run(command, env=env_vars, capture_output=True, check=True)
    for deployment in ["opensandbox-controller-manager", "bbx-p3f-server", "bbx-p3f-ingress"]:
        kubectl("-n", SYSTEM, "rollout", "status", "deployment/" + deployment, "--timeout=180s")
    expected = json.loads((STATE / "build-source.json").read_text())["g7_identity"]
    actual = json.loads(
        kubectl(
            "-n", SYSTEM, "exec", "deployment/bbx-p3f-server", "--", "cat", "/etc/bbx-g7-patch.json"
        )
    )
    assert actual == expected
    pods = json.loads(kubectl("-n", SYSTEM, "get", "pods", "-o", "json"))["items"]
    (STATE / "server-evidence.json").write_text(
        json.dumps(
            {
                "namespace_uid": namespace["metadata"]["uid"],
                "patch_identity_matches": True,
                "pods": [
                    {
                        "name": p["metadata"]["name"],
                        "uid": p["metadata"]["uid"],
                        "images": [c["image"] for c in p["spec"]["containers"]],
                    }
                    for p in pods
                ],
            },
            indent=2,
        )
    )
    print("Owned official Server/Controller/Ingress are ready; fixed patch identity verified.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant-endpoint", required=True)
    parser.add_argument("--resolver-secret-file", required=True)
    parser.add_argument("--ca-file", required=True)
    parser.add_argument("--ingress-host", required=True)
    args = parser.parse_args()
    install(args.tenant_endpoint, args.resolver_secret_file, args.ca_file, args.ingress_host)
