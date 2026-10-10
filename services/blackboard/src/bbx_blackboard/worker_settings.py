"""Direct worker settings over retained internal profile snapshots."""

from __future__ import annotations

from typing import Any, Literal

from bbx_contracts.ctf import CtfPlatformToolBinding
from bbx_contracts.models import AgentProfile, ExecResources, Params, WorkerTools
from bbx_contracts.providers import reasoning_efforts
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


class CtfWorkerUpdate(WorkerUpdate):
    platform_tools: list[CtfPlatformToolBinding] | None = None


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


def model_context_threshold(models: dict[str, Any]) -> int | None:
    windows = [
        model["context_window"]
        for model in models.values()
        if model.get("context_window") is not None
    ]
    return max(1, min(windows) * 4 // 5) if windows else None


def override_reasoning_effort(models: dict[str, Any], effort: str) -> dict[str, Any]:
    for model in models.values():
        allowed = reasoning_efforts(model["provider"], model["model"])
        if not allowed:
            raise HTTPException(422, "所选模型的 Provider 不支持思考强度覆盖")
        if "max" in allowed:
            allowed.extend(["minimal", "medium", "xhigh", "off"])
        if effort not in ["none", *allowed]:
            raise HTTPException(422, "所选模型不支持此思考强度，请选择可用选项")
    return {role: {**model, "reasoning_effort": effort} for role, model in models.items()}


async def task_profile(
    profiles: ProfileStore,
    platform: PlatformStore,
    model_id: str | None,
    model_version: int | None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    workers = await read_workers(profiles)
    name = model_id or await platform.default_model_name()
    row = await platform.get("models", name, model_version)
    if not row["enabled"]:
        raise HTTPException(422, "所选模型已停用，请选择已启用的模型")
    model = platform.public(row)["config"]
    content = workers["profile"]
    content["models"] = {role: model for role in ("explore", "derive", "close")}
    if reasoning_effort is not None:
        content["models"] = override_reasoning_effort(content["models"], reasoning_effort)
    threshold = model_context_threshold(content["models"])
    if threshold is not None:
        content["params"]["context_threshold"] = threshold
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


@router.get("/ctf/workers")
async def ctf_workers(request: Request) -> dict[str, Any]:
    from bbx_blackboard.profiles import ctf_profile

    require_user_or_service(request)
    row = await request.app.state.profile_store.get("ctf")
    return {"revision": row["version"], "profile": ctf_profile(row).model_dump(mode="json")}


@router.get("/ctf/prompts/{role}")
async def ctf_prompt(request: Request, role: Literal["lead", "teammate"]) -> dict[str, Any]:
    from bbx_blackboard.profiles import ctf_profile

    require_user_or_service(request)
    row = await request.app.state.profile_store.get("ctf")
    return {"revision": row["version"], "prompt": getattr(ctf_profile(row).prompt_templates, role)}


@router.put("/ctf/workers/{role}")
async def update_ctf_worker(
    request: Request,
    role: Literal["lead", "teammate"],
    body: CtfWorkerUpdate,
) -> dict[str, Any]:
    from bbx_contracts.ctf import CtfAgentProfile

    from bbx_blackboard.profiles import ctf_profile

    identity = require_user_or_service(request)
    store = request.app.state.profile_store
    row = await store.get("ctf")
    content = ctf_profile(row).model_dump(mode="json")
    content["prompt_templates"][role] = body.prompt
    content["worker_tools"][role] = body.tools.model_dump(mode="json")
    if body.platform_tools is not None:
        content["platform_tools"] = [item.model_dump(mode="json") for item in body.platform_tools]
    try:
        profile = CtfAgentProfile.model_validate(content)
    except ValueError as error:
        raise HTTPException(422, "Invalid CTF prompt or role tools") from error
    for tools in profile.worker_tools.values():
        for binding in tools.mcp_servers:
            configured = await request.app.state.platform_store.get(
                "mcp-servers", binding.name, binding.version
            )
            if not configured["enabled"]:
                raise HTTPException(422, "Selected MCP server is disabled")
    saved = await store.create(
        "ctf", profile, identity.name, expected_version=body.expected_revision
    )
    return {"revision": saved["version"], "profile": ctf_profile(saved).model_dump(mode="json")}
