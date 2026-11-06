#!/usr/bin/env python3
"""Only move the official chart's sensitive Server config from ConfigMap to Secret."""

import json
import os
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[3]
config = root / ".data/p3f-k8s-20261106/kubeconfig"
converted = subprocess.run(
    [
        str(root / ".data/p3f-k8s-20261106/bin/kubectl"),
        "--kubeconfig",
        str(config),
        "--context",
        "kind-bbx-p3f-k8s",
        "create",
        "--dry-run=client",
        "-f",
        "-",
        "-o",
        "json",
    ],
    input=sys.stdin.buffer.read(),
    capture_output=True,
)
if converted.returncode:
    raise SystemExit("Official Server chart conversion refused")
decoder = json.JSONDecoder()
text = converted.stdout.decode().strip()
items = []
while text:
    result, end = decoder.raw_decode(text)
    items.extend(result["items"] if result.get("kind") == "List" else [result])
    text = text[end:].lstrip()
configs = [
    doc for doc in items if doc["kind"] == "ConfigMap" and "config.toml" in doc.get("data", {})
]
assert len(configs) == 1
secret = configs[0]
text = secret.pop("data")["config.toml"]
assert text.count('"__BBX_RESOLVER__"') == 1
token = Path(os.environ["BBX_P3F_RESOLVER_TOKEN_FILE"]).read_text().strip()
assert token
secret["kind"] = "Secret"
secret["type"] = "Opaque"
secret["stringData"] = {"config.toml": text.replace('"__BBX_RESOLVER__"', json.dumps(token))}
changed = 0
for doc in items:
    if doc["kind"] != "Deployment":
        continue
    for volume in doc["spec"]["template"]["spec"].get("volumes", []):
        if volume.get("configMap", {}).get("name") == secret["metadata"]["name"]:
            volume.pop("configMap")
            volume["secret"] = {"secretName": secret["metadata"]["name"]}
            changed += 1
assert changed == 1
sys.stdout.write("\n---\n".join(json.dumps(item) for item in items))
