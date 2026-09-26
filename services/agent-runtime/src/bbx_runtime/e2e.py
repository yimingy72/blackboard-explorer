"""Explicit paid toy-task checkpoint on a disposable Compose project."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import docker
import httpx
import yaml

from bbx_runtime.clients import BlackboardClient

ROOT = Path(__file__).resolve().parents[4]
TERMINAL = {"finished", "failed", "stopped"}


def task_spec(root: Path, name: str) -> dict:
    tasks = {path.parent.name: path for path in (root / "eval/tasks").glob("*/task.yaml")}
    if name not in tasks:
        raise ValueError(f"Unknown task: {name}")
    return yaml.safe_load(tasks[name].read_text())


def container_proxy(value: str) -> str:
    if not value:
        return ""
    parsed = urlsplit(value)
    if parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        host = "host.docker.internal"
        if parsed.port is not None:
            host += f":{parsed.port}"
        if "@" in parsed.netloc:
            host = parsed.netloc.rsplit("@", 1)[0] + "@" + host
        return urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))
    return value


def compose_document(root: Path) -> dict:
    config = yaml.safe_load((root / "docker-compose.yml").read_text())
    dev = yaml.safe_load((root / "docker-compose.dev.yml").read_text())
    config["services"].update(dev["services"])
    for name in ("postgres", "minio"):
        config["services"][name].pop("ports", None)
    config["services"]["blackboard"]["ports"] = [
        {"target": 8000, "published": "0", "host_ip": "127.0.0.1", "protocol": "tcp"}
    ]
    config["services"]["eval-targets"]["volumes"] = [f"{root / 'eval/targets/dist'}:/targets:ro"]
    return config


def checkpoint_environment(source: dict[str, str], project: str) -> dict[str, str]:
    key = source.get("DEEPSEEK_API_KEY", "")
    if not key or key == "replace-me":
        raise ValueError("DEEPSEEK_API_KEY must be set for the explicit e2e command")
    result = {
        **source,
        "POSTGRES_USER": "blackboard",
        "POSTGRES_PASSWORD": secrets.token_urlsafe(24),
        "POSTGRES_DB": "blackboard",
        "MINIO_ROOT_USER": "blackboard",
        "MINIO_ROOT_PASSWORD": secrets.token_urlsafe(24),
        "MINIO_BUCKET": "blackboard",
        "SERVICE_TOKEN": secrets.token_urlsafe(32),
        "AGENT_TOKEN_SECRET": secrets.token_urlsafe(32),
        "ENVD_TOKEN_SECRET": secrets.token_urlsafe(32),
        "ADMIN_USERS": "e2e:local-e2e-only",
        "EXEC_NETWORK": f"{project}_exec",
        "MAX_RUNNING_TASKS": "1",
        "EGRESS_ALLOWLIST": "eval-targets,mirrors.aliyun.com,pypi.org,files.pythonhosted.org",
    }
    for kind in ("HTTP", "HTTPS"):
        value = (
            source.get(f"RUNTIME_{kind}_PROXY")
            or source.get(f"{kind.lower()}_proxy")
            or source.get(f"{kind}_PROXY", "")
        )
        result[f"RUNTIME_{kind}_PROXY"] = container_proxy(value)
    return result


def redact(text: str, environment: dict[str, str]) -> str:
    for name, value in environment.items():
        sensitive = any(
            part in name.upper() for part in ("PASSWORD", "TOKEN", "API_KEY", "ADMIN_USERS")
        )
        if name.upper().endswith("PROXY") and value and urlsplit(value).username is not None:
            sensitive = True
        if value and sensitive:
            text = text.replace(value, "[REDACTED]")
    return text


class ComposeCheckpoint:
    def __init__(self, root: Path, environment: dict[str, str]) -> None:
        self.root = root
        self.project = f"bbx-e2e-{uuid4().hex[:8]}"
        self.environment = checkpoint_environment(environment, self.project)
        self.directory = root / ".data/e2e" / self.project
        self.directory.mkdir(parents=True)
        self.config = self.directory / "compose.yml"
        self.config.write_text(yaml.safe_dump(compose_document(root), allow_unicode=True))
        self.command = [
            "docker",
            "compose",
            "--env-file",
            "/dev/null",
            "--project-name",
            self.project,
            "-f",
            str(self.config),
        ]
        self.task_id: str | None = None

    async def docker(self, *arguments: str) -> str:
        result = await asyncio.to_thread(
            subprocess.run,
            [*self.command, *arguments],
            env=self.environment,
            cwd=self.root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=180,
        )
        output = redact(result.stdout, self.environment)
        if result.returncode:
            raise RuntimeError(f"Compose {' '.join(arguments)} failed: {output[-2000:]}")
        return output

    async def run(self, *, keep: bool, timeout: int, task_name: str = "flaky-order-test") -> dict:
        spec = task_spec(self.root, task_name)
        complete = False
        try:
            await self.docker("up", "-d")
            address = (await self.docker("port", "blackboard", "8000")).strip().splitlines()[-1]
            base_url = f"http://{address}"
            print(f"工作台：{base_url}（隔离测试账号 e2e / local-e2e-only）", flush=True)
            async with BlackboardClient(base_url, self.environment["SERVICE_TOKEN"]) as board:
                async with asyncio.timeout(120):
                    while True:
                        try:
                            await board.list_tasks()
                            break
                        except (httpx.HTTPError, RuntimeError):
                            await asyncio.sleep(1)
                spec["agent_profile"] = "default"
                tid = (await board.create_task(spec))["id"]
                self.task_id = tid
                await board.start_task(tid)
                metadata = {
                    "project": self.project,
                    "task": task_name,
                    "task_id": tid,
                    "url": f"{base_url}/tasks/{tid}",
                }
                (self.directory / "run.json").write_text(json.dumps(metadata, indent=2))
                print(f"任务：{tid}\n进度：{metadata['url']}", flush=True)
                previous = None
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    state = await board.state(tid)
                    task = state["task"]
                    signature = (
                        task["status"],
                        len(state["facts"]),
                        len(state["intents"]),
                        len(state["agents"]),
                        bool(task.get("workspace_uri")),
                    )
                    if signature != previous:
                        print(
                            f"{signature[0]}：事实 {signature[1]} / 意图 {signature[2]} / "
                            f"Agent {signature[3]} / 归档 {signature[4]}",
                            flush=True,
                        )
                        previous = signature
                    if task["status"] in TERMINAL and task.get("workspace_uri"):
                        complete = True
                        break
                    if task["status"] in {"failed", "stopped"} and not state["agents"]:
                        break
                    await asyncio.sleep(1)
                else:
                    raise TimeoutError("End-to-end checkpoint exceeded its wall-clock limit")
                (self.directory / "state.json").write_text(
                    redact(json.dumps(state, ensure_ascii=False, indent=2), self.environment)
                )
                (self.directory / "events.json").write_text(
                    redact(
                        json.dumps(await board.events(tid), ensure_ascii=False, indent=2),
                        self.environment,
                    )
                )
                if task.get("report_uri"):
                    async with httpx.AsyncClient(trust_env=False, timeout=30) as http:
                        response = await http.get(
                            f"{base_url}/api/tasks/{tid}/report",
                            headers={
                                "Authorization": f"Bearer {self.environment['SERVICE_TOKEN']}"
                            },
                        )
                        response.raise_for_status()
                        report = response.text
                    (self.directory / "report.md").write_text(redact(report, self.environment))
                    print(redact(report, self.environment), flush=True)
                if task.get("workspace_uri"):
                    async with httpx.AsyncClient(trust_env=False, timeout=120) as http:
                        async with http.stream(
                            "GET",
                            f"{base_url}/api/tasks/{tid}/workspace",
                            headers={
                                "Authorization": f"Bearer {self.environment['SERVICE_TOKEN']}"
                            },
                        ) as response:
                            response.raise_for_status()
                            with (self.directory / "workspace.tar.zst").open("wb") as archive:
                                async for chunk in response.aiter_bytes():
                                    archive.write(chunk)
                print(
                    f"账本用量与估算费用：{json.dumps(task['usage'], ensure_ascii=False)}",
                    flush=True,
                )
                print(f"检查产物：{self.directory}", flush=True)
                if (
                    task["status"] != "finished"
                    or not task.get("report_uri")
                    or not task.get("workspace_uri")
                ):
                    raise RuntimeError("Task did not finish with both report and workspace archive")
                return {**metadata, "usage": task["usage"], "directory": str(self.directory)}
        finally:
            try:
                logs = await self.docker("logs", "--no-color", "--tail", "200")
                (self.directory / "compose.log").write_text(logs)
            finally:
                if keep and complete:
                    print(
                        f"保留隔离工作台；清理命令：docker compose -p {self.project} "
                        f"-f {self.config} down -v --remove-orphans",
                        flush=True,
                    )
                else:
                    await self._cleanup()

    async def _cleanup(self) -> None:
        try:
            await self.docker("stop", "agent-runtime")
        finally:
            try:
                await asyncio.to_thread(self._remove_task_containers)
            finally:
                await self.docker("down", "-v", "--remove-orphans")

    def _remove_task_containers(self) -> None:
        if self.task_id is None:
            return
        client = docker.from_env()
        try:
            for container in client.containers.list(
                all=True,
                filters={"label": ["bbx.managed=agent-runtime", f"bbx.task-id={self.task_id}"]},
            ):
                container.remove(force=True)
        finally:
            client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="运行真实 DeepSeek 任务闭环（会产生费用）")
    parser.add_argument("--task", default="flaky-order-test", help="eval/tasks 下的任务目录名")
    parser.add_argument("--keep", action="store_true", help="结束后保留隔离工作台供复盘")
    parser.add_argument(
        "--timeout", type=int, default=1800, help="任务等待上限，秒；Compose 命令另限 180 秒"
    )
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("timeout must be positive")
    checkpoint = ComposeCheckpoint(ROOT, dict(os.environ))
    try:
        asyncio.run(checkpoint.run(keep=args.keep, timeout=args.timeout, task_name=args.task))
    except Exception as error:
        print(
            redact(f"检查失败：{type(error).__name__}: {error}", checkpoint.environment), flush=True
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
