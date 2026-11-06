"""One task-local test container with three real assignment namespaces."""

import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from testcontainers.core.container import DockerContainer

from bbx_sandbox_execution.execution import AssignmentBinding

TOKEN = "bbx-archv2-fixture-token-no-real-secret"
IMAGE = os.environ.get("BBX_EXECUTION_TEST_IMAGE", "bbx-archv2-sandbox-seam-test:20261106")


@pytest.fixture(scope="module")
def container() -> Iterator[DockerContainer]:
    yield from running_container(False)


@pytest.fixture(scope="module")
def accounting_container() -> Iterator[DockerContainer]:
    yield from running_container(True)


def running_container(accounting: bool, pod_uid: str | None = None) -> Iterator[DockerContainer]:
    pod_uid = pod_uid or "bbx-g1-pod-" + uuid4().hex
    instance = DockerContainer(
        IMAGE,
        name="bbx-archv2-execution-" + uuid4().hex[:10],
        cap_add=["SYS_ADMIN", "NET_ADMIN"],
        security_opt=["apparmor=unconfined"],
        sysctls={"net.ipv4.ip_forward": "1", "net.ipv6.conf.all.forwarding": "1"},
        mem_limit="1g",
        nano_cpus=1_000_000_000,
        pids_limit=256,
        cgroupns="private",
        labels={"bbx.archv2.pod-uid": pod_uid} if accounting else {},
    ).with_env("BBX_TEST_TOKEN", TOKEN)
    if accounting:
        instance.with_env("BBX_TEST_ACCOUNTING", "cgroup-v2")
        instance.with_env("BBX_POD_UID", pod_uid)
        instance.with_env("BBX_ENVIRONMENT_REVISION", "environment-1")
    # Docker supports a random port restricted to host loopback; the helper's annotation does not.
    instance.ports["44772/tcp"] = ("127.0.0.1", None)  # pyright: ignore[reportArgumentType]
    try:
        instance.start()
        host = instance.get_wrapped_container().attrs["HostConfig"]
        assert host["CgroupnsMode"] == "private" and not host["Privileged"]
        assert {value.removeprefix("CAP_") for value in host["CapAdd"]} == {
            "SYS_ADMIN",
            "NET_ADMIN",
        }
        endpoint = "http://127.0.0.1:" + str(instance.get_exposed_port(44772))
        with httpx.Client(trust_env=False) as client:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    if client.get(endpoint + "/ready", timeout=1).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.1)
            else:
                pytest.fail(instance.get_logs()[0].decode() + instance.get_logs()[1].decode())
        yield instance
    finally:
        instance.stop()


def binding(container: DockerContainer, index: int = 1) -> AssignmentBinding:
    identifier = container.get_wrapped_container().id
    assert identifier is not None
    return AssignmentBinding(
        task_id="task-p1",
        agent_id=("worker-a", "worker-b", "helper-a")[index - 1],
        role="helper" if index == 3 else "worker",
        assignment_id=f"assignment-{index}",
        generation=1,
        epoch=1,
        sandbox_id="sandbox-p1",
        pod_uid=container.get_wrapped_container()
        .attrs["Config"]["Labels"]
        .get("bbx.archv2.pod-uid", identifier[:32]),
        environment_revision="environment-1",
        network_identity=f"network-{index}-generation-1",
        namespace=("worker-a", "worker-b", "helper-a")[index - 1],
        ipv4=f"198.19.{index}.2",
        challenge_id="challenge-p1",
        parent_agent_id="worker-a" if index == 3 else None,
    )


def admin(container: DockerContainer, *args: str) -> str:
    result = container.get_wrapped_container().exec_run(list(args))
    assert isinstance(result.output, bytes)
    assert result.exit_code == 0, result.output.decode()
    return result.output.decode()


def write_grants(container: DockerContainer, path: Path) -> None:
    encoded = json.dumps(path.read_text())
    admin(
        container,
        "/usr/bin/python3",
        "-I",
        "-c",
        "from pathlib import Path;p=Path('/var/lib/bbx/control/proxy/grants.json');"
        f"p.write_text({encoded});p.chmod(0o600)",
    )
