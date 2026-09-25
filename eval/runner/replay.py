"""Explicitly replay selected Python evidence in fresh, offline containers."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import shutil
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from uuid import uuid4

import docker
from bbx_blackboard.workspace import WorkspaceArchiveCache
from docker.types import LogConfig

DRIVER = """
import json, os, shutil, sqlite3, subprocess, tarfile, time, urllib.error, urllib.request
from pathlib import Path
root = Path('/workspace/shared/mini-shop')
root.mkdir(parents=True)
with tarfile.open('/input/target.tar.gz') as archive:
    archive.extractall(root, filter='data')
config = json.loads(Path('/input/config.json').read_text())
python = '/opt/envd/venv/bin/python'
script = Path('/workspace') / config['path']
script.parent.mkdir(parents=True, exist_ok=True)
shutil.copyfile('/input/script.py', script)
for path in config['files']:
    destination = Path('/workspace') / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path('/input/files') / path, destination)
os.chdir(root)
os.environ['PYTHONPATH'] = str(root)
os.environ['SHOP_DB'] = str(root / 'shop.sqlite3')
server = None
server_log = None
try:
    port = config['service_port']
    if port is not None:
        if config['seed']:
            seeded = subprocess.run(
                [python, str(root / 'scripts/seed.py')], capture_output=True,
                text=True, timeout=20,
            )
            if seeded.returncode:
                raise RuntimeError('Target seed failed: ' + seeded.stderr[-2000:])
            if config['seed_tokens']:
                with sqlite3.connect(os.environ['SHOP_DB']) as connection:
                    for name, token in config['seed_tokens'].items():
                        cursor = connection.execute(
                            'UPDATE users SET token=? WHERE name=?', (token, name)
                        )
                        if cursor.rowcount != 1:
                            raise RuntimeError('Target seed did not create expected user: ' + name)
        server_log = (Path('/workspace') / 'replay-server.log').open('w+')
        server = subprocess.Popen(
            [python, '-m', 'uvicorn', 'shop.app:app', '--host', '127.0.0.1',
             '--port', str(port)], stdout=server_log, stderr=subprocess.STDOUT,
        )
        ready_url = 'http://127.0.0.1:' + str(port) + '/products'
        deadline = time.monotonic() + 20
        ready = False
        while time.monotonic() < deadline:
            if server.poll() is not None:
                break
            try:
                with urllib.request.urlopen(ready_url, timeout=1) as response:
                    if response.status == 200:
                        ready = True
                        break
            except (urllib.error.URLError, TimeoutError):
                time.sleep(0.2)
        if not ready:
            server_log.flush()
            server_log.seek(0)
            raise RuntimeError('Target service readiness failed: '
                               + server_log.read()[-2000:])
    code = subprocess.call([python, str(script)])
finally:
    if server is not None:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
    if server_log is not None:
        server_log.close()
raise SystemExit(code)
"""


class LocalArchive:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def stream(self, uri: str, chunk_size: int = 65536):
        with self.path.open("rb") as source:
            while chunk := source.read(chunk_size):
                yield chunk


def _workspace_path(path: object, *, python: bool) -> bool:
    if not isinstance(path, str):
        return False
    parts = PurePosixPath(path).parts
    return bool(
        len(parts) >= 2
        and parts[0] in {"agents", "shared"}
        and ".." not in parts
        and "\\" not in path
        and str(PurePosixPath(path)) == path
        and (not python or path.endswith(".py"))
    )


def _target_path(path: str) -> bool:
    return path == "shared/mini-shop" or path.startswith("shared/mini-shop/")


def _seed_tokens(value: object) -> bool:
    return bool(
        isinstance(value, dict)
        and value
        and set(value) <= {"alice", "bob"}
        and all(
            isinstance(token, str) and re.fullmatch(r"[0-9a-fA-F]{32}", token)
            for token in value.values()
        )
        and len(set(value.values())) == len(value)
    )


def validate_manifest(value: dict) -> list[dict]:
    scripts = value.get("scripts")
    if not isinstance(scripts, list) or not scripts:
        raise ValueError("replay.json must contain a non-empty scripts list")
    seen = set()
    for script in scripts:
        if not isinstance(script, dict):
            raise ValueError("Each script must be an object")
        path = script.get("path")
        if (
            not isinstance(path, str)
            or not _workspace_path(path, python=True)
            or _target_path(path)
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
        port = script.get("service_port")
        if port is not None and (type(port) is not int or not 1024 <= port <= 65535):
            raise ValueError("service_port must be an integer from 1024 to 65535")
        if "seed" in script and type(script["seed"]) is not bool:
            raise ValueError("seed must be a boolean")
        files = script.get("files", [])
        if not isinstance(files, list) or len(files) > 16:
            raise ValueError("files must be a list of at most 16 paths")
        if any(
            not _workspace_path(item, python=False) or _target_path(item) or item == path
            for item in files
        ) or len(set(files)) != len(files):
            raise ValueError(
                "files must be distinct normalized files outside the target and script"
            )
        tokens = script.get("seed_tokens")
        if tokens is not None and (
            port is None or script.get("seed", True) is not True or not _seed_tokens(tokens)
        ):
            raise ValueError("seed_tokens require a seeded service and 32-hex alice/bob tokens")
    return scripts


def execute(
    script: str,
    target: Path,
    image: str,
    timeout: int,
    *,
    script_path: str = "agents/replay.py",
    service_port: int | None = None,
    seed: bool = True,
    files: dict[str, str] | None = None,
    seed_tokens: dict[str, str] | None = None,
) -> dict:
    """Only explicit replay inputs are mounted; no host credentials are inherited."""
    attachments = files or {}
    if not _workspace_path(script_path, python=True) or _target_path(script_path):
        raise ValueError("Invalid script path")
    if service_port is not None and (
        type(service_port) is not int or not 1024 <= service_port <= 65535
    ):
        raise ValueError("Invalid service port")
    if len(attachments) > 16 or any(
        not _workspace_path(path, python=False) or _target_path(path) or path == script_path
        for path in attachments
    ):
        raise ValueError("Invalid attachment path")
    if (
        any(len(content.encode("utf-8")) > 200 * 1024 for content in attachments.values())
        or sum(len(content.encode("utf-8")) for content in attachments.values()) > 2 * 1024 * 1024
    ):
        raise ValueError("Attachments exceed the replay size limit")
    if seed_tokens is not None and (
        service_port is None or not seed or not _seed_tokens(seed_tokens)
    ):
        raise ValueError("Invalid seed tokens")
    client = docker.from_env(timeout=timeout + 10)
    container = None
    try:
        with tempfile.TemporaryDirectory(prefix="bbx-eval-replay-") as temporary:
            directory = Path(temporary)
            directory.chmod(0o755)
            (directory / "script.py").write_text(script)
            (directory / "config.json").write_text(
                json.dumps(
                    {
                        "path": script_path,
                        "service_port": service_port,
                        "seed": seed,
                        "files": list(attachments),
                        "seed_tokens": seed_tokens or {},
                    }
                )
            )
            for path, content in attachments.items():
                staged = directory / "files" / path
                staged.parent.mkdir(parents=True, exist_ok=True)
                staged.write_text(content)
            # Stage a fixed copy so the source cannot change during execution.
            shutil.copyfile(target, directory / "target.tar.gz")
            for item in directory.rglob("*"):
                item.chmod(0o555 if item.is_dir() else 0o444)
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
            tokens = spec.get("seed_tokens") or {}
            settings = {
                "path": spec["path"],
                "service_port": spec.get("service_port"),
                "seed": spec.get("seed", True) if spec.get("service_port") is not None else None,
                "files": spec.get("files", []),
                "seed_token_sha256": {
                    name: hashlib.sha256(token.encode()).hexdigest()
                    for name, token in tokens.items()
                },
            }
            script_hash = None
            attachment_hashes = {}
            try:
                content = await cache.file(
                    LocalArchive(directory / "workspace.tar.zst"), "workspace", spec["path"]
                )
                if content["binary"] or content["truncated"]:
                    raise ValueError("Selected script must be a complete UTF-8 file below 200 KiB")
                source = str(content["text"])
                if len(source.encode("utf-8")) != content["size"]:
                    raise ValueError("Selected script could not be copied without modification")
                script_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
                attachments = {}
                total_size = 0
                for path in settings["files"]:
                    file = await cache.file(
                        LocalArchive(directory / "workspace.tar.zst"), "workspace", path
                    )
                    size = file["size"]
                    if (
                        not isinstance(size, int)
                        or file["binary"]
                        or file["truncated"]
                        or size > 200 * 1024
                    ):
                        raise ValueError("Attachments must be complete UTF-8 files below 200 KiB")
                    text = str(file["text"])
                    data = text.encode("utf-8")
                    if len(data) != size:
                        raise ValueError("Attachment could not be copied without modification")
                    total_size += len(data)
                    if total_size > 2 * 1024 * 1024:
                        raise ValueError("Attachments exceed 2 MiB")
                    attachments[path] = text
                    attachment_hashes[path] = hashlib.sha256(data).hexdigest()
                result = await asyncio.to_thread(
                    execute,
                    source,
                    target,
                    image,
                    timeout,
                    script_path=spec["path"],
                    service_port=settings["service_port"],
                    seed=settings["seed"] if settings["seed"] is not None else True,
                    files=attachments,
                    seed_tokens=tokens or None,
                )
                for token in tokens.values():
                    result["output"] = result["output"].replace(token, "[REDACTED]")
                passed = (
                    result["exit_code"] == spec["expected_exit_code"]
                    and spec["expected_output"] in result["output"]
                )
                results.append(
                    {
                        "id": spec["id"],
                        "status": "passed" if passed else "failed",
                        **settings,
                        "script_sha256": script_hash,
                        "attachment_sha256": attachment_hashes,
                        **result,
                    }
                )
            except Exception as error:
                message = str(error)
                for token in tokens.values():
                    message = message.replace(token, "[REDACTED]")
                results.append(
                    {
                        "id": spec["id"],
                        "status": "error",
                        **settings,
                        "script_sha256": script_hash,
                        "attachment_sha256": attachment_hashes,
                        "error": message,
                    }
                )
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
