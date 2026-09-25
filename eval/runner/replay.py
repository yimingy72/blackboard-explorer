"""Explicitly replay selected Python evidence in fresh, offline containers."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from uuid import uuid4

import docker
from bbx_blackboard.workspace import WorkspaceArchiveCache
from docker.types import LogConfig

DRIVER = """
import os, subprocess, tarfile
from pathlib import Path
root = Path('/workspace/shared/mini-shop')
root.mkdir(parents=True)
with tarfile.open('/input/target.tar.gz') as archive:
    archive.extractall(root, filter='data')
os.chdir(root)
os.environ['PYTHONPATH'] = str(root)
os.environ['SHOP_DB'] = str(root / 'shop.sqlite3')
raise SystemExit(subprocess.call(['/opt/envd/venv/bin/python', '/input/script.py']))
"""


class LocalArchive:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def stream(self, uri: str, chunk_size: int = 65536):
        with self.path.open("rb") as source:
            while chunk := source.read(chunk_size):
                yield chunk


def validate_manifest(value: dict) -> list[dict]:
    scripts = value.get("scripts")
    if not isinstance(scripts, list) or not scripts:
        raise ValueError("replay.json must contain a non-empty scripts list")
    seen = set()
    for script in scripts:
        if not isinstance(script, dict):
            raise ValueError("Each script must be an object")
        path = script.get("path", "")
        if not isinstance(path, str):
            raise ValueError("Script path must be a string")
        parts = PurePosixPath(path).parts
        if (
            not isinstance(path, str)
            or not parts
            or parts[0] not in {"agents", "shared"}
            or ".." in parts
            or "\\" in path
            or str(PurePosixPath(path)) != path
            or not path.endswith(".py")
        ):
            raise ValueError("Select a normalized Python file under agents/ or shared/")
        identity = script.get("id")
        if not isinstance(identity, str) or not identity or identity in seen:
            raise ValueError("Script ids must be non-empty and unique")
        seen.add(identity)
        if type(script.get("expected_exit_code")) is not int:
            raise ValueError("expected_exit_code must be explicit")
        if not isinstance(script.get("expected_output"), str) or not script["expected_output"]:
            raise ValueError("expected_output must state the observable result to match")
    return scripts


def execute(script: str, target: Path, image: str, timeout: int) -> dict:
    """Only the two explicit input files are mounted; no host credentials are inherited."""
    client = docker.from_env(timeout=timeout + 10)
    container = None
    try:
        with tempfile.TemporaryDirectory(prefix="bbx-eval-replay-") as temporary:
            directory = Path(temporary)
            directory.chmod(0o755)
            (directory / "script.py").write_text(script)
            # Stage a fixed copy so the source cannot change during execution.
            shutil.copyfile(target, directory / "target.tar.gz")
            for item in directory.iterdir():
                item.chmod(0o444)
            container = client.containers.create(
                image,
                ["-c", DRIVER],
                entrypoint=["/opt/envd/venv/bin/python"],
                name=f"bbx-m5-replay-{uuid4().hex[:12]}",
                network_mode="none",
                user="1000:1000",
                read_only=True,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                mem_limit="1g",
                nano_cpus=2_000_000_000,
                pids_limit=128,
                tmpfs={"/workspace": "rw,size=512m,mode=1777", "/tmp": "rw,size=64m,mode=1777"},
                volumes={str(directory): {"bind": "/input", "mode": "ro"}},
                environment={"PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"},
                log_config=LogConfig(type="json-file", config={"max-size": "1m", "max-file": "1"}),
            )
            container.start()
            result = container.wait(timeout=timeout)
            output = container.logs(tail=1000).decode("utf-8", errors="replace")[-200_000:]
            return {"exit_code": result["StatusCode"], "output": output}
    finally:
        try:
            if container is not None:
                container.remove(force=True)
        finally:
            client.close()


async def replay(directory: Path, target: Path, image: str, timeout: int) -> dict:
    scripts = validate_manifest(json.loads((directory / "replay.json").read_text()))
    if not target.is_file() or not tarfile.is_tarfile(target):
        raise ValueError("A saved evaluation target tar archive is required")
    if target.stat().st_size > 128 * 1024 * 1024:
        raise ValueError("Evaluation target archive exceeds 128 MiB")
    cache = WorkspaceArchiveCache()
    results = []
    try:
        for spec in scripts:
            try:
                content = await cache.file(
                    LocalArchive(directory / "workspace.tar.zst"), "workspace", spec["path"]
                )
                if content["binary"] or content["truncated"]:
                    raise ValueError("Selected script must be a complete UTF-8 file below 200 KiB")
                result = await asyncio.to_thread(
                    execute, str(content["text"]), target, image, timeout
                )
                passed = (
                    result["exit_code"] == spec["expected_exit_code"]
                    and spec["expected_output"] in result["output"]
                )
                results.append(
                    {"id": spec["id"], "status": "passed" if passed else "failed", **result}
                )
            except Exception as error:
                results.append({"id": spec["id"], "status": "error", "error": str(error)})
    finally:
        await cache.close()
    with target.open("rb") as source:
        target_hash = hashlib.file_digest(source, "sha256").hexdigest()
    document = {
        "scripts": results,
        "reproducibility": sum(row["status"] == "passed" for row in results) / len(results),
        "image": image,
        "target_sha256": target_hash,
        "scope": "selected standalone Python scripts, each in a fresh offline container",
    }
    (directory / "replay-results.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2)
    )
    return document


def main() -> None:
    parser = argparse.ArgumentParser(description="重跑显式选择的证据脚本（每项新建无外网容器）")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--target", type=Path, required=True, help="本次评估保存的 target.tar.gz")
    parser.add_argument("--image", default="bbx-eval-env:latest")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error("timeout must be positive")
    result = asyncio.run(replay(args.directory, args.target, args.image, args.timeout))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if any(row["status"] != "passed" for row in result["scripts"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
