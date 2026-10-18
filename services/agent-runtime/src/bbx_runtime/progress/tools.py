"""The complete allowlist available to the presentation Agent."""

import html
import json
from typing import Any

from agent_framework import tool
from bbx_contracts.progress import ProgressNode
from pydantic import ValidationError

from bbx_runtime.clients.blackboard import RemoteError
from bbx_runtime.progress.client import ProgressClient
from bbx_runtime.progress.session import ProgressSessionClient
from bbx_runtime.session import SessionCheckpoint


def data_block(value: Any) -> str:
    return (
        "<progress_source_data>\n"
        + html.escape(json.dumps(value, ensure_ascii=False, default=str), quote=False)
        + "\n</progress_source_data>"
    )


def progress_tools(
    service: ProgressClient, checkpoint: SessionCheckpoint, tree: dict[str, Any], enabled: list[str]
) -> list[Any]:
    assert service.job is not None
    job = service.job
    invalid_patches = 0

    @tool(approval_mode="never_require")
    async def read_progress_sources(after_version: int = 0, limit: int = 30) -> str:
        """分页读取本任务截止版本内的领域事件；next_version 用作下一页，limit 最大100。"""
        if after_version < 0 or not 1 <= limit <= 100:
            return "after_version 必须非负，limit 须为1–100。"
        result = await service.sources(after_version, limit)
        adapter = checkpoint.service
        assert isinstance(adapter, ProgressSessionClient)
        adapter.source_version = max(adapter.source_version, int(result["next_version"]))
        return data_block(result)

    @tool(name="get", approval_mode="never_require")
    async def get_object(object_id: str) -> str:
        """读取截止来源版本内的 Fact 或 Intent 全文与状态，不读取未来对象。"""
        try:
            return data_block(await service.object(object_id))
        except RemoteError as error:
            if error.status not in {404, 422}:
                raise
            return error.message

    @tool(approval_mode="never_require")
    async def search(q: str, after_version: int = 0, limit: int = 20) -> str:
        """搜索截止版本内事实和意图；limit 最大50，可按next_version继续检索。"""
        if not q.strip() or len(q) > 200 or after_version < 0 or not 1 <= limit <= 50:
            return "搜索词须1–200字，after_version 非负，limit 须为1–50。"
        return data_block(await service.search(q, after_version, limit))

    @tool(approval_mode="never_require")
    async def read_evidence(
        uri: str, version: int, sha256: str, offset: int = 0, limit: int = 16000
    ) -> str:
        """读取登记版本与内容哈希都匹配的本任务证据；offset/limit按字节，limit最大64000。"""
        if version < 0 or offset < 0 or not 1 <= limit <= 64000:
            return "version/offset 须非负，limit 须为1–64000。"
        try:
            return data_block(await service.evidence(uri, version, sha256, offset, limit))
        except RemoteError as error:
            if error.status not in {404, 409, 413, 422}:
                raise
            return "该历史证据内容不可校验或超出分页限制：" + error.message

    @tool(approval_mode="never_require")
    async def get_progress_tree() -> str:
        """读取当前候选成果树（包含本轮已保存的小patch）及发布版revision。"""
        return data_block(tree)

    @tool(approval_mode="never_require")
    async def update_progress_tree(
        upserts: list[ProgressNode], removed_ids: list[str], overview: str | None = None
    ) -> str:
        """保存增量候选：新增或替换节点、移除节点，可更新全局overview；成功完成后才原子发布。

        重用稳定ID；保留未修改节点，不要每次重发整树。upserts/removed_ids不用时传空列表。
        """
        nonlocal invalid_patches
        if invalid_patches >= 2:
            return "候选修正次数已用完，本轮不会发布。"
        if len(upserts) > 300 or len(removed_ids) > 300 or (overview and len(overview) > 6000):
            invalid_patches += 1
            checkpoint.session.state["bbx_progress_candidate_failures"] = invalid_patches
            checkpoint.session.state["bbx_progress_candidate_error"] = (
                "Progress patch exceeds limits"
            )
            return "Patch超过限制；合并归纳节点，overview最多6000字。"
        try:
            upserts = [ProgressNode.model_validate(node) for node in upserts]
        except ValidationError:
            invalid_patches += 1
            checkpoint.session.state["bbx_progress_candidate_failures"] = invalid_patches
            checkpoint.session.state["bbx_progress_candidate_error"] = "Invalid progress node"
            return "节点字段不符合合同，请按工具schema修正；最多一次修正。"
        payload = {
            "expected_revision": job["base_revision"],
            "upserts": [node.model_dump(mode="json") for node in upserts],
            "removed_ids": removed_ids,
            "overview": overview,
        }
        try:
            await service.operation("candidate", **payload)
        except RemoteError as error:
            if error.status != 422:
                vars(checkpoint)["progress_control_error"] = error
                raise
            invalid_patches += 1
            checkpoint.session.state["bbx_progress_candidate_failures"] = invalid_patches
            checkpoint.session.state["bbx_progress_candidate_error"] = error.message
            return "候选未保存，请修正图结构或来源；最多进行一次修正：" + error.message
        except Exception as error:
            vars(checkpoint)["progress_control_error"] = error
            raise
        nodes = {node["id"]: node for node in tree["nodes"] if node["id"] not in removed_ids}
        nodes.update({node.id: node.model_dump(mode="json") for node in upserts})
        tree["nodes"] = list(nodes.values())
        if overview is not None:
            tree["overview"] = overview
        checkpoint.session.state["bbx_progress_candidate"] = tree
        checkpoint.session.state.pop("bbx_progress_candidate_error", None)
        checkpoint.session.state.pop("bbx_progress_candidate_failures", None)
        return "候选已保存，尚未发布。"

    tools = [
        read_progress_sources,
        get_object,
        search,
        read_evidence,
        get_progress_tree,
        update_progress_tree,
    ]
    return [item for item in tools if item.name in enabled]
