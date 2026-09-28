"""Direct worker settings over retained internal profile snapshots."""

from __future__ import annotations

from typing import Any, Literal

from bbx_contracts.models import AgentProfile, ExecResources, Params, WorkerTools
from fastapi import APIRouter, HTTPException, Request
from jinja2 import TemplateSyntaxError, meta
from jinja2.sandbox import SandboxedEnvironment
from pydantic import BaseModel, ConfigDict, Field

from bbx_blackboard.auth import require_user_or_service
from bbx_blackboard.platform import PlatformStore
from bbx_blackboard.profiles import ProfileStore

Role = Literal["explore", "derive", "close"]
VARIABLES = {
    "goal",
    "domain_context",
    "acceptance_status",
    "agent_id",
    "seed_max_steps",
    "conclude_grace_calls",
    "current_intent",
    "yaml_snapshot",
    "facts_text",
    "intents_text",
    "previous_excluded",
    "satisfies_facts",
    "judgment_history",
    "mode",
    "budget_left",
}


class WorkerUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    prompt: str = Field(min_length=1)
    tools: WorkerTools


class RuntimeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    params: Params
    exec_image: str = Field(min_length=1)
    exec_resources: ExecResources
    privileged_allowlist: list[str] = Field(default_factory=list)


def validate_prompt(prompt: str) -> None:
    if not prompt.strip():
        raise HTTPException(422, "系统提示词不能为空")
    try:
        parsed = SandboxedEnvironment().parse(prompt)
    except TemplateSyntaxError as error:
        raise HTTPException(422, f"提示词模板第 {error.lineno} 行语法不正确") from None
    unknown = meta.find_undeclared_variables(parsed) - VARIABLES
    if unknown:
        raise HTTPException(422, "不支持的模板变量：" + ", ".join(sorted(unknown)))


async def read_workers(profiles: ProfileStore) -> dict[str, Any]:
    row = await profiles.get("default")
    profile = AgentProfile.model_validate({key: row[key] for key in AgentProfile.model_fields})
    return {"revision": row["version"], "profile": profile.model_dump(mode="json")}


async def task_profile(
    profiles: ProfileStore,
    platform: PlatformStore,
    model_id: str | None,
    model_version: int | None,
) -> dict[str, Any]:
    workers = await read_workers(profiles)
    name = model_id or await platform.default_model_name()
    row = await platform.get("models", name, model_version)
    if not row["enabled"]:
        raise HTTPException(422, "所选模型已停用，请选择已启用的模型")
    model = platform.public(row)["config"]
    content = workers["profile"]
    content["models"] = {role: model for role in ("explore", "derive", "close")}
    profile = AgentProfile.model_validate(content)
    return await profiles.create("task-settings", profile, "task")


router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("/workers")
async def workers(request: Request) -> dict[str, Any]:
    require_user_or_service(request)
    return await read_workers(request.app.state.profile_store)


@router.get("/prompts/{role}")
async def prompt(request: Request, role: Role, since: int | None = None) -> dict[str, Any]:
    require_user_or_service(request)
    row = await request.app.state.profile_store.get("default")
    return {
        "revision": row["version"],
        "prompt": None if since == row["version"] else row["prompt_templates"][role],
    }


@router.put("/workers/{role}")
async def update_worker(request: Request, role: Role, body: WorkerUpdate) -> dict[str, Any]:
    identity = require_user_or_service(request)
    validate_prompt(body.prompt)
    profiles = request.app.state.profile_store
    current = await read_workers(profiles)
    content = current["profile"]
    content["prompt_templates"][role] = body.prompt
    content["worker_tools"][role] = body.tools.model_dump(mode="json")
    try:
        profile = AgentProfile.model_validate(content)
        profile = await request.app.state.platform_store.normalize_profile(profile)
    except ValueError:
        raise HTTPException(422, "工具配置超出角色权限或缺少必要工具，请检查勾选项") from None
    row = await profiles.create(
        "default", profile, identity.name, expected_version=body.expected_revision
    )
    return {"revision": row["version"], "profile": profile.model_dump(mode="json")}


@router.put("/runtime")
async def update_runtime(request: Request, body: RuntimeUpdate) -> dict[str, Any]:
    identity = require_user_or_service(request)
    profiles = request.app.state.profile_store
    current = await read_workers(profiles)
    content = {**current["profile"], **body.model_dump(mode="json", exclude={"expected_revision"})}
    profile = AgentProfile.model_validate(content)
    row = await profiles.create(
        "default", profile, identity.name, expected_version=body.expected_revision
    )
    return {"revision": row["version"], "profile": profile.model_dump(mode="json")}
