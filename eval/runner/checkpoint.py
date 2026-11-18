"""Isolated evaluation deployment; model execution uses an approved AgentGateway route."""

from __future__ import annotations

import asyncio
import secrets
import subprocess
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import docker
import yaml
from bbx_contracts.gateway_models import GatewayModelRef
from bbx_contracts.legacy_network import LEGACY_DOCKER_OWNER


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
    evaluation_model(source)
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
    result.pop("DEEPSEEK_API_KEY", None)
    return result


def redact(text: str, environment: dict[str, str]) -> str:
    for name, value in environment.items():
        sensitive = any(
            part in name.upper() for part in ("PASSWORD", "TOKEN", "KEY", "ADMIN_USERS")
        )
        if name.upper().endswith("PROXY") and value and urlsplit(value).username is not None:
            sensitive = True
        if value and sensitive:
            text = text.replace(value, "[REDACTED]")
    return text


def evaluation_model(source: dict[str, str]) -> GatewayModelRef:
    return GatewayModelRef.model_validate(
        {
            "alias": source.get("EVAL_GATEWAY_MODEL_ALIAS"),
            "catalog_revision": source.get("EVAL_GATEWAY_MODEL_REVISION"),
        }
    )


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

    def _remove_task_containers(self) -> None:
        if self.task_id is None:
            return
        client = docker.from_env()
        try:
            for container in client.containers.list(
                all=True,
                filters={
                    "label": [f"bbx.managed={LEGACY_DOCKER_OWNER}", f"bbx.task-id={self.task_id}"]
                },
            ):
                container.remove(force=True)
        finally:
            client.close()
