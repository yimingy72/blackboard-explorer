"""Platform Sandbox tools and bounded command audit views."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from .models import ContractModel
from .runner import ToolInvocation

SANDBOX_TOOLS = (
    "exec_command",
    "execution_status",
    "execution_output",
    "interrupt_execution",
    "read_file",
    "write_file",
)
HELPER_SANDBOX_TOOLS = ("read_file", "exec_command")
SANDBOX_CONTROLS = frozenset(SANDBOX_TOOLS[1:4])
MAX_COMMAND_OUTPUT_BYTES = 65536


def tool_alias(name):
    return ("sandbox_" if name in SANDBOX_TOOLS else "business_") + name


def sandbox_schemas():
    def object_schema(properties, required):
        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }

    command_id = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    path = {"type": "string", "minLength": 1, "maxLength": 4096}
    return {
        "exec_command": object_schema(
            {
                "command": {"type": "string", "minLength": 1, "maxLength": 65536},
                "cwd": {"type": "string", "default": "/workspace", "maxLength": 4096},
                "background": {"type": "boolean", "default": False},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300},
            },
            ["command"],
        ),
        "execution_status": object_schema({"command_id": command_id}, ["command_id"]),
        "execution_output": object_schema(
            {"command_id": command_id, "cursor": {"type": "integer", "minimum": 0}},
            ["command_id"],
        ),
        "interrupt_execution": object_schema({"command_id": command_id}, ["command_id"]),
        "read_file": object_schema(
            {
                "path": path,
                "offset": {"type": "integer", "const": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 65536},
            },
            ["path"],
        ),
        "write_file": object_schema(
            {
                "path": path,
                "content": {"type": "string", "maxLength": 65536},
                "mode": {"type": "integer", "default": 644},
            },
            ["path", "content"],
        ),
    }


class SandboxCommandSummary(ContractModel):
    id: str
    sequence: int
    task_id: UUID
    agent_id: str
    role: Literal["lead", "worker", "helper", "explore"]
    parent_agent_id: str | None = None
    challenge_id: UUID | None
    assignment_id: str
    generation: int
    epoch: int
    command: str
    cwd: str
    background: bool
    state: Literal["dispatched", "running", "completed", "failed", "unknown"]
    execution_id: str | None
    exit_code: int | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    output_truncated: bool


class SandboxCommandDetail(SandboxCommandSummary):
    stdout: str
    stderr: str
    stdout_bytes: int = Field(ge=0)
    stderr_bytes: int = Field(ge=0)


class SandboxCommandPage(ContractModel):
    items: list[SandboxCommandSummary]
    next_before: int | None


class SandboxToolClaim(ContractModel):
    invocation: ToolInvocation
    name: str
    arguments: dict
    network_grant_revision: int | None = Field(default=None, ge=1, strict=True)


class SandboxHostEvent(ContractModel):
    event: str
    payload: dict


class SandboxHostResult(ContractModel):
    result: dict
