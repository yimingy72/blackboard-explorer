"""Validate one built tools image in a disposable, offline container."""

import argparse
import hashlib
import io
import json
import tarfile
from pathlib import Path
from uuid import uuid4

import docker
from testcontainers.core.container import DockerContainer


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    client = docker.from_env()
    image = client.images.get(args.image)
    assert isinstance(image.id, str)
    assert image.attrs["Os"] == "linux"
    assert image.attrs["Architecture"] == "amd64"
    assert image.attrs["Config"]["User"] == "10000:10000"
    assert not image.attrs["Config"].get("Entrypoint")
    assert image.attrs["Config"]["Cmd"] == ["/bin/bash"]
    container = DockerContainer(
        image.id,
        name="bbx-ctf-image-smoke-" + uuid4().hex[:8],
        entrypoint="/usr/bin/python3",
        network_mode="none",
        nano_cpus=4_000_000_000,
        mem_limit="6g",
        pids_limit=512,
    )
    container.with_command("/opt/ctf-tools/smoke_tools.py")
    with container:
        wrapped = container.get_wrapped_container()
        pending = b""
        with (args.output / "smoke.jsonl").open("wb") as saved:
            for chunk in wrapped.logs(stream=True, follow=True):
                saved.write(chunk)
                saved.flush()
                lines = (pending + chunk).split(b"\n")
                pending = lines.pop()
                for line in lines:
                    if line.startswith(b'{"name":'):
                        record = json.loads(line)
                        print(record["name"], "PASS" if record["passed"] else "FAIL", flush=True)
        status = wrapped.wait(timeout=10)
        for name in ("manifest.json", "manifest.sha256", "slimming.json", "worker-tools.md"):
            stream, _ = wrapped.get_archive("/opt/ctf-tools/" + name)
            with tarfile.open(fileobj=io.BytesIO(b"".join(stream))) as archive:
                member = archive.extractfile(name)
                assert member is not None
                (args.output / name).write_bytes(member.read())
        raw = (args.output / "smoke.jsonl").read_text()
        reports = [
            json.loads(row)["final"] for row in raw.splitlines() if row.startswith('{"final":')
        ]
        assert len(reports) == 1, "Missing final smoke report"
        manifest_hash = hashlib.sha256((args.output / "manifest.json").read_bytes()).hexdigest()
        assert (args.output / "manifest.sha256").read_text().split()[0] == manifest_hash
        receipt = {
            "image": args.image,
            "image_id": image.id,
            "architecture": image.attrs["Architecture"],
            "inspect_size_bytes": image.attrs["Size"],
            "manifest_sha256": manifest_hash,
            "exit_code": status["StatusCode"],
            "smoke": reports[0],
        }
        (args.output / "validation.json").write_text(json.dumps(receipt, indent=2) + "\n")
        assert status["StatusCode"] == 0 and reports[0]["passed"], "Tool smoke checks failed"
        print("Validated", image.id, "manifest", manifest_hash, flush=True)


if __name__ == "__main__":
    main()
