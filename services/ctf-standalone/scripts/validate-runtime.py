"""Validate a complete CTF image with offline fixtures mounted outside the image."""

import argparse
import hashlib
import io
import json
import tarfile
import time
from pathlib import Path
from uuid import uuid4

import docker
from testcontainers.core.container import DockerContainer


def read_file(container, path: str) -> bytes:
    stream, _ = container.get_archive(path)
    with tarfile.open(fileobj=io.BytesIO(b"".join(stream))) as archive:
        member = next(item for item in archive if item.isfile())
        source = archive.extractfile(member)
        assert source is not None
        return source.read()


def run_case(image, source: Path, output: Path, name: str, command: str, artifacts=None):
    started = time.monotonic()
    container = DockerContainer(
        image,
        name=f"bbx-ctf-runtime-{name}-{uuid4().hex[:8]}",
        entrypoint="/bin/bash",
        network_mode="none",
        nano_cpus=4_000_000_000,
        mem_limit="6g",
        pids_limit=512,
        working_dir="/opt/ctf-runner",
    )
    container.with_volume_mapping(str(source / "test"), "/opt/ctf-runner/test", "ro")
    container.with_volume_mapping(str(source / "benchmark"), "/opt/ctf-runner/benchmark", "ro")
    container.with_command(["-c", command])
    with container:
        wrapped = container.get_wrapped_container()
        with (output / f"{name}.log").open("wb") as saved:
            for chunk in wrapped.logs(stream=True, follow=True):
                saved.write(chunk)
                saved.flush()
        status = wrapped.wait(timeout=10)
        result = {
            "case": name,
            "exit_code": status["StatusCode"],
            "duration_seconds": round(time.monotonic() - started, 3),
        }
        if result["exit_code"]:
            print((output / f"{name}.log").read_text(), flush=True)
            raise RuntimeError(f"{name} failed with exit {result['exit_code']}")
        for filename, path in (artifacts or {}).items():
            (output / filename).write_bytes(read_file(wrapped, path))
        print(json.dumps(result), flush=True)
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("output", type=Path)
    parser.add_argument("--counts", default="1,8,16,20,32,64")
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--history-rounds", type=int, default=16)
    args = parser.parse_args()
    counts = [int(value) for value in args.counts.split(",")]
    if any(value < 1 for value in counts) or min(args.rounds, args.history_rounds) < 1:
        parser.error("Counts and rounds must be positive integers")
    source = Path(__file__).resolve().parents[1]
    args.output.mkdir(parents=True, exist_ok=True)
    client = docker.from_env()
    image = client.images.get(args.image)
    assert image.attrs["Os"] == "linux" and image.attrs["Architecture"] == "amd64"
    assert image.attrs["Config"]["User"] == "10000:10000"
    assert image.attrs["Config"]["Entrypoint"] == [
        "/usr/local/bin/node",
        "/opt/ctf-runner/src/main.mjs",
    ]
    cases = [
        run_case(
            image.id,
            source,
            args.output,
            "entrypoint-help",
            "node /opt/ctf-runner/src/main.mjs --help",
        ),
        run_case(
            image.id,
            source,
            args.output,
            "node-tests",
            "node --test --test-concurrency=1 test/*.test.mjs",
        ),
    ]
    cases.append(
        run_case(
            image.id,
            source,
            args.output,
            "tools",
            "python3 /opt/ctf-tools/smoke_tools.py > /workspace/ctf-run/tools.jsonl",
            {
                "tools.jsonl": "/workspace/ctf-run/tools.jsonl",
                "tool-manifest.json": "/opt/ctf-tools/manifest.json",
            },
        )
    )
    benchmarks = []
    for count in counts:
        for phase, filename, rounds in (
            ("capacity", "session-capacity.mjs", args.history_rounds),
            ("combined", "run.mjs", args.rounds),
        ):
            name = f"{phase}-{count}"
            artifact = f"{name}.json"
            cases.append(
                run_case(
                    image.id,
                    source,
                    args.output,
                    name,
                    f"node benchmark/{filename} --count {count} --rounds {rounds} "
                    f"--root /workspace/ctf-run/benchmark > /workspace/ctf-run/result.json",
                    {artifact: "/workspace/ctf-run/result.json"},
                )
            )
            benchmark = json.loads((args.output / artifact).read_text())
            assert benchmark.get("failures", 0) == 0
            assert benchmark["real_model_calls"] == 0
            assert benchmark["measurement"]["after"]["cpu"]["quota_cpus"] == 4
            assert benchmark["measurement"]["after"]["memory"]["limit_bytes"] == 6 * 1024**3
            benchmarks.append(benchmark)
    reports = [
        json.loads(line)["final"]
        for line in (args.output / "tools.jsonl").read_text().splitlines()
        if line.startswith('{"final":')
    ]
    assert len(reports) == 1 and reports[0]["passed"]
    result = {
        "image": args.image,
        "image_id": image.id,
        "architecture": image.attrs["Architecture"],
        "inspect_size_bytes": image.attrs["Size"],
        "uid": 10000,
        "cpu_limit": 4,
        "memory_limit_bytes": 6 * 1024**3,
        "pid_limit": 512,
        "network_mode": "none",
        "tool_manifest_sha256": hashlib.sha256(
            (args.output / "tool-manifest.json").read_bytes()
        ).hexdigest(),
        "tool_checks": len(reports[0]["results"]),
        "cases": cases,
        "benchmarks": benchmarks,
        "real_model_calls": 0,
        "scope": "Offline fixture workflows and synthetic fixed work per Worker; "
        "actual competition solving is unmeasured",
    }
    (args.output / "validation.json").write_text(json.dumps(result, indent=2) + "\n")
    print("Validated", image.id, flush=True)


if __name__ == "__main__":
    main()
