"""Direct worker settings over retained internal profile snapshots."""

from __future__ import annotations

from typing import Any, Literal

from bbx_contracts.challenge_board import business_manifest
from bbx_contracts.model_limits import effective_max_input_tokens
from bbx_contracts.models import AgentProfile, ExecResources, ModelConfig, Params, WorkerTools
from bbx_contracts.runner_vpn import VPN_DYNAMIC_PROFILE
from fastapi import APIRouter, HTTPException, Request
from jinja2 import TemplateSyntaxError, meta
from jinja2.sandbox import SandboxedEnvironment
from pydantic import BaseModel, ConfigDict, Field

from bbx_blackboard.auth import require_user_or_service
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
    exec_resources: ExecResources


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


async def read_workers(profiles: ProfileStore, *, internal=False) -> dict[str, Any]:
    row = await profiles.get("default")
    profile = AgentProfile.model_validate({key: row[key] for key in AgentProfile.model_fields})
    return {"revision": row["version"], "profile": worker_document(profile, internal=internal)}


def worker_document(profile: AgentProfile, *, internal=False):
    exclude = set() if internal else {"models", "privileged_allowlist"}
    return profile.model_dump(mode="json", exclude=exclude)


def model_context_threshold(models: dict[str, Any]) -> int | None:
    limits = [
        limit
        for model in models.values()
        if (limit := effective_max_input_tokens(ModelConfig.model_validate(model))) is not None
    ]
    return max(1, min(limits) * 4 // 5) if limits else None


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
    current = await read_workers(profiles, internal=True)
    content = current["profile"]
    previous = AgentProfile.model_validate(content)
    content["prompt_templates"][role] = body.prompt
    content["worker_tools"][role] = body.tools.model_dump(mode="json")
    try:
        profile = AgentProfile.model_validate(content)
        from bbx_blackboard.mcp_catalog import validate_profile_tools

        await validate_profile_tools(request, profile, previous=previous)
    except ValueError:
        raise HTTPException(422, "工具配置超出角色权限或缺少必要工具，请检查勾选项") from None
    row = await profiles.create(
        "default", profile, identity.name, expected_version=body.expected_revision
    )
    return {"revision": row["version"], "profile": worker_document(profile)}


@router.put("/runtime")
async def update_runtime(request: Request, body: RuntimeUpdate) -> dict[str, Any]:
    identity = require_user_or_service(request)
    profiles = request.app.state.profile_store
    current = await read_workers(profiles, internal=True)
    content = {**current["profile"], **body.model_dump(mode="json", exclude={"expected_revision"})}
    profile = AgentProfile.model_validate(content)
    row = await profiles.create(
        "default", profile, identity.name, expected_version=body.expected_revision
    )
    return {"revision": row["version"], "profile": worker_document(profile)}


@router.get("/ctf/workers")
async def ctf_workers(request: Request) -> dict[str, Any]:
    from bbx_blackboard.profiles import ctf_profile

    require_user_or_service(request)
    publication = request.app.state.native_ctf_publication.publication
    if publication is None:
        return {"published": False}
    row = await request.app.state.profile_store.get("ctf", publication.ctf_profile_version)
    profile = ctf_profile(row)
    manifest = business_manifest(
        automatic=True,
        vpn=publication.vpn_profile is not None,
        task_network=publication.vpn_profile == VPN_DYNAMIC_PROFILE,
    )
    return {
        "published": True,
        "revision": row["version"],
        "prompt_templates": profile.prompt_templates.model_dump(mode="json"),
        "business_tools": manifest["roles"],
        "task_network": publication.vpn_profile == VPN_DYNAMIC_PROFILE,
    }
