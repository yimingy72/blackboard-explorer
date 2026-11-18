"""Check the fixed template/chart contract and refusal before any foreign-node mutation."""

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

from common import HERE, REVISION, STATE, SYSTEM, TASK


class FixtureContract(unittest.TestCase):
    def test_fixed_template_and_official_chart_render(self):
        template = json.loads((HERE / "batchsandbox-template.json").read_text())
        pod = template["spec"]["shardPatches"][0]["spec"]
        main = pod["containers"][0]
        self.assertEqual(len(pod["containers"]), 1)
        self.assertEqual(
            main["command"], ["/opt/bbx/control/launcher", "--kubernetes-bootstrap-cgroup-v2"]
        )
        self.assertFalse(main["securityContext"]["privileged"])
        self.assertEqual(
            set(main["securityContext"]["capabilities"]["add"]),
            {
                "SYS_ADMIN",
                "NET_ADMIN",
                "NET_RAW",
                "SETUID",
                "SETGID",
                "SETPCAP",
                "KILL",
                "CHOWN",
                "DAC_OVERRIDE",
                "FOWNER",
            },
        )
        env = {item["name"]: item for item in main["env"]}
        for key in ("EXECD_ACCESS_TOKEN", "BBX_POD_UID"):
            self.assertEqual(env[key]["value"], "")
        self.assertEqual(env["BBX_POD_UID"]["valueFrom"]["fieldRef"]["fieldPath"], "metadata.uid")
        self.assertEqual(env["BBX_ENVIRONMENT_REVISION"]["value"], REVISION)
        values = (HERE / "values/server.yaml").read_text()
        config = tomllib.loads(
            "\n".join(
                line.removeprefix("  ")
                for line in values.split("configToml: |\n", 1)[1].splitlines()
            )
        )
        self.assertEqual(config["kubernetes"]["namespace"], TASK)
        self.assertTrue(config["runtime"]["execd_run_as_init"])
        self.assertEqual(config["tenants"]["provider"], "file")
        for name in ("server", "controller", "ingress"):
            chart = "ingress-gateway" if name == "ingress" else name
            rendered = subprocess.check_output(
                [
                    str(STATE / "bin/helm"),
                    "template",
                    "bbx-archv2-g1-k8s-" + name,
                    str(STATE / "upstream/manifests/charts" / chart),
                    "-n",
                    SYSTEM,
                    "-f",
                    str(HERE / "values" / (name + ".yaml")),
                    "--kube-version",
                    "1.34.0",
                ],
                text=True,
            )
            self.assertIn("namespace: " + SYSTEM, rendered)
            self.assertIn("release-1.2.0@sha256:", rendered)
        self.assertIn("existingSecret: bbx-archv2-g1-k8s-signing", values)

    def test_foreign_node_is_rejected_before_preparation_or_update(self):
        with tempfile.TemporaryDirectory(dir=STATE) as temporary:
            root = Path(temporary)
            log = root / "calls"
            stub = root / "docker"
            stub.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >> "
                + shlex.quote(str(log))
                + "\nprintf 'foreign-kind\\n'\n"
            )
            stub.chmod(0o700)
            result = subprocess.run(
                ["bash", str(HERE / "run.sh")],
                env={**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"]},
                capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
            calls = log.read_text().splitlines()
            self.assertEqual(len(calls), 2)
            self.assertTrue(
                all(call.startswith("inspect bbx-archv2-g1-k8s-control-plane") for call in calls)
            )

    def test_same_context_wrong_endpoint_rejected_before_any_write(self):
        with tempfile.TemporaryDirectory(dir=STATE) as temporary:
            root = Path(temporary)
            fixture = root / "deploy/kubernetes/g1"
            fixture.mkdir(parents=True)
            for name in ("run.sh", "common.py"):
                shutil.copyfile(HERE / name, fixture / name)
            python = root / "services/sandbox-execution/.venv/bin/python"
            python.parent.mkdir(parents=True)
            python.symlink_to(sys.executable)
            binaries = root / "stubs"
            binaries.mkdir()
            state = root / ".data/bbx-archv2-g1-k8s/bin"
            state.mkdir(parents=True)
            calls, writes = root / "calls", root / "writes"
            record = "printf '%s\\n' \"$*\" >> " + shlex.quote(str(calls)) + "\n"
            ports = json.dumps({"6443/tcp": [{"HostIp": "127.0.0.1", "HostPort": "36443"}]})
            labels = json.dumps({"io.x-k8s.kind.cluster": "bbx-archv2-g1-k8s"})
            reject = (
                "printf '%s\\n' unexpected-write >> " + shlex.quote(str(writes)) + "; exit 77\n"
            )
            scripts = {
                "docker": "#!/bin/sh\n" + record + 'case "$*" in\n'
                " *NetworkSettings.Ports*) printf '%s' " + shlex.quote(ports) + " ;;\n"
                " *'json .Config.Labels'*) printf '%s' " + shlex.quote(labels) + " ;;\n"
                " *Config.Labels*) printf 'bbx-archv2-g1-k8s\\n' ;;\n"
                " inspect*) exit 0 ;;\n *) " + reject + ";;\nesac\n",
                "kubectl": "#!/bin/sh\n" + record + 'case "$*" in\n'
                " *'config current-context'*) printf 'kind-bbx-archv2-g1-k8s\\n' ;;\n"
                " *'config view'*) printf 'https://127.0.0.1:36444' ;;\n"
                " *) " + reject + ";;\nesac\n",
                "python3": "#!/bin/sh\n" + record + "exit 0\n",
            }
            for name, text in scripts.items():
                path = binaries / name
                path.write_text(text)
                path.chmod(0o700)
            for name in ("kind", "helm"):
                path = state / name
                path.write_text("#!/bin/sh\n" + reject)
                path.chmod(0o700)
            result = subprocess.run(
                ["bash", str(fixture / "run.sh")],
                env={**os.environ, "PATH": str(binaries) + os.pathsep + os.environ["PATH"]},
                capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                b"Kubeconfig does not point at the owned node API endpoint", result.stderr
            )
            self.assertIn("config view --minify", calls.read_text())
            self.assertFalse(
                writes.exists(), "No node, Helm or kubectl writes may precede ownership"
            )


if __name__ == "__main__":
    unittest.main()
