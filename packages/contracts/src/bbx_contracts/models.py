"""Cross-service data shapes; domain rules belong to the blackboard service."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FactKind(StrEnum):
    OBSERVATION = "observation"
    INFERENCE = "inference"
    STRUCTURE = "structure"


class FactStatus(StrEnum):
    PROPOSED = "proposed"
    DISPUTED = "disputed"


class EvidenceType(StrEnum):
    HTTP = "http"
    FILE = "file"
    SCRIPT = "script"
    LOG = "log"
    CODE_REF = "code_ref"
    TEXT = "text"
    COMMAND_OUTPUT = "command_output"


class IntentStatus(StrEnum):
    OPEN = "open"
    CLAIMED = "claimed"
    CLOSED = "closed"


class IntentResult(StrEnum):
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"


class AcceptanceStatus(StrEnum):
    MET = "met"
    UNMET = "unmet"


class Verdict(StrEnum):
    MET = "met"
    UNMET = "unmet"


class TaskStatus(StrEnum):
    CREATED = "created"
    PROVISIONING = "provisioning"
    RUNNING = "running"
    CLOSING = "closing"
    FINISHED = "finished"
    FAILED = "failed"
    STOPPED = "stopped"


class AgentTaskType(StrEnum):
    EXPLORE = "explore"
    DERIVE = "derive"
    CLOSE = "close"


class CloseMode(StrEnum):
    JUDGE = "judge"
    FINAL = "final"


class AgentStatus(StrEnum):
    RUNNING = "running"
    CONCLUDING = "concluding"
    FINISHED = "finished"
    FAILED = "failed"


class EndReason(StrEnum):
    NORMAL = "normal"
    REFUSED = "refused"
    LIMIT = "limit"
    GRACE_TIMEOUT = "grace_timeout"
    HEARTBEAT = "heartbeat"
    RUNTIME_ERROR = "runtime_error"
    RUNTIME_RESTART = "runtime_restart"


class EventType(StrEnum):
    COST_RECONCILED = "cost.reconciled"
    TASK_CREATED = "task.created"
    TASK_PROVISIONING = "task.provisioning"
    TASK_RUNNING = "task.running"
    TASK_CLOSING = "task.closing"
    TASK_FINISHED = "task.finished"
    TASK_FAILED = "task.failed"
    TASK_STOPPED = "task.stopped"
    TASK_ARCHIVED = "task.archived"
    TASK_RESUMED = "task.resumed"
    TASK_CLEANUP_READY = "task.cleanup_ready"
    FACT_POSTED = "fact.posted"
    FACT_DISPUTED = "fact.disputed"
    FACT_UNDISPUTED = "fact.undisputed"
    INTENT_POSTED = "intent.posted"
    INTENT_CLAIMED = "intent.claimed"
    INTENT_RELEASED = "intent.released"
    INTENT_CLOSED = "intent.closed"
    AGENT_SPAWNED = "agent.spawned"
    AGENT_REACTIVATED = "agent.reactivated"
    AGENT_PROGRESS = "agent.progress"
    AGENT_FINISHED = "agent.finished"
    AGENT_CONCLUDE_REQUESTED = "agent.conclude_requested"
    DERIVE_RESULT = "derive.result"
    ACCEPTANCE_JUDGED = "acceptance.judged"
    ACCEPTANCE_REVERTED = "acceptance.reverted"
    TASK_REPORT = "task.report"
    BUDGET_UPDATED = "budget.updated"
    TOOL_CALL_RECORDED = "tool_call.recorded"
    AGENT_TRACE_RECORDED = "agent.trace.recorded"
    AGENT_MESSAGE_POSTED = "agent.message.posted"
    AGENT_MESSAGE_DELIVERED = "agent.message.delivered"
    AGENT_MESSAGE_REPLIED = "agent.message.replied"
    AGENT_MESSAGE_FAILED = "agent.message.failed"


class Evidence(ContractModel):
    type: EvidenceType = Field(description="证据类型")
    path: str | None = Field(default=None, min_length=1, description="执行环境内的原路径")
    uri: str | None = Field(default=None, min_length=1, description="持久化后的对象 key")
    summary: str = Field(min_length=1, description="证据摘要")
    call_id: str | None = Field(default=None, min_length=1, description="产生证据的工具调用")
    size: int | None = Field(default=None, ge=0, description="证据字节数")
    auto: bool = Field(default=False, description="是否由系统自动附加")


class Fact(ContractModel):
    id: str = Field(min_length=1, description="事实编号")
    kind: FactKind = Field(description="直接观察、推断或结构")
    statement: str = Field(min_length=1, description="可复核的事实陈述")
    evidence: list[Evidence] = Field(min_length=1, description="持久化证据")
    derived_from: list[str] = Field(default_factory=list, description="推断所依据的事实")
    disputes: list[str] = Field(default_factory=list, description="本事实反驳的事实")
    resolves: str | None = Field(default=None, min_length=1, description="本事实关闭的意图")
    result: IntentResult | None = Field(default=None, description="意图结论")
    satisfies: list[str] = Field(default_factory=list, description="声称满足的验收条件")
    author: str = Field(min_length=1, description="提交者")
    status: FactStatus | None = Field(default=None, description="读时计算的争议状态")
    provenance: Literal["tool_backed", "self_reported"] | None = Field(
        default=None, description="依据工具调用计算的证据来源"
    )
    relied_by: int | None = Field(default=None, ge=0, description="其他作者依赖此事实的次数")
    version: int | None = Field(default=None, ge=0, description="写入事件版本")

    @model_validator(mode="after")
    def check_local_consistency(self) -> Fact:
        if self.kind == FactKind.INFERENCE and not self.derived_from:
            raise ValueError("inference requires derived_from")
        if self.resolves is not None and self.result is None:
            raise ValueError("resolves requires result")
        if self.result is not None and self.resolves is None:
            raise ValueError("result requires resolves")
        return self


class IntentNote(ContractModel):
    by: str = Field(min_length=1, description="交接者")
    at: datetime = Field(description="交接时间")
    text: str = Field(min_length=1, description="交接说明")


class Intent(ContractModel):
    id: str = Field(min_length=1, description="意图编号")
    statement: str = Field(min_length=1, description="要调查的方向")
    based_on: list[str] = Field(min_length=1, description="至少一条依据事实")
    expected: str = Field(min_length=1, description="成立时预期观察")
    method: str = Field(min_length=1, description="可交接的调查方法")
    relates_to: list[str] = Field(min_length=1, description="相关验收条件")
    retry_of: str | None = Field(default=None, min_length=1, description="换方法重试的意图")
    status: IntentStatus = Field(default=IntentStatus.OPEN, description="认领状态")
    holder: str | None = Field(default=None, min_length=1, description="持有者")
    claimed_at: datetime | None = Field(default=None, description="认领时间")
    result: IntentResult | None = Field(default=None, description="关闭结论")
    closed_by: str | None = Field(default=None, min_length=1, description="关闭者")
    result_facts: list[str] = Field(default_factory=list, description="结论事实")
    notes: list[IntentNote] = Field(default_factory=list, description="交接说明")
    attempts: int = Field(default=0, ge=0, description="未关闭释放次数")
    author: str = Field(min_length=1, description="提出者")
    version: int | None = Field(default=None, ge=0, description="写入事件版本")


class AcceptanceItem(ContractModel):
    id: str = Field(min_length=1, description="验收条件编号")
    desc: str = Field(min_length=1, description="文字描述的验收条件")


class AcceptanceItemState(ContractModel):
    completion_basis: Literal["explicit", "inferred"] = "inferred"
    completion_reason: str | None = None
    status: AcceptanceStatus = Field(default=AcceptanceStatus.UNMET, description="当前满足状态")
    reason: str | None = Field(default=None, description="裁定理由")
    missing: str | None = Field(default=None, description="未满足时缺少什么")
    evidence_facts: list[str] = Field(default_factory=list, description="支撑事实")
    judged_version: int | None = Field(default=None, ge=0, description="裁定时的黑板版本")


class VerdictItem(ContractModel):
    completion_basis: Literal["explicit", "inferred"] = "inferred"
    completion_reason: str | None = None
    id: str = Field(min_length=1, description="验收条件编号")
    verdict: Verdict = Field(description="满足或未满足")
    reason: str = Field(min_length=1, description="裁定理由")
    missing: str | None = Field(default=None, description="未满足时的行动缺口")
    evidence_facts: list[str] = Field(default_factory=list, description="支撑事实")

    @model_validator(mode="after")
    def check_verdict(self) -> VerdictItem:
        if self.completion_basis == "explicit" and (
            self.verdict != Verdict.MET
            or not (self.completion_reason or "").strip()
            or not self.evidence_facts
        ):
            raise ValueError(
                "explicit completion requires met, completion_reason and evidence_facts"
            )
        if self.verdict == Verdict.UNMET and not self.missing:
            raise ValueError("unmet verdict requires missing")
        return self


class Usage(ContractModel):
    cache_hit_tokens: int = Field(default=0, ge=0, description="缓存命中输入 token")
    cache_miss_tokens: int = Field(default=0, ge=0, description="缓存未命中输入 token")
    output_tokens: int = Field(default=0, ge=0, description="输出 token")
    reasoning_tokens: int = Field(default=0, ge=0, description="推理 token")
    cost: Decimal = Field(default=Decimal(0), ge=0, description="金额成本")


class Budget(ContractModel):
    max_concurrent_agents: int = Field(default=5, ge=1, description="并发探索和推导上限")
    max_cost: Decimal = Field(gt=0, description="同价格表币种的金额上限")
    max_minutes: int = Field(gt=0, description="时间上限，分钟")


class Params(ContractModel):
    explore_max_steps: int = Field(default=60, ge=1, description="单次探索最多模型调用数")
    seed_max_steps: int = Field(default=20, ge=1, description="种子未认领意图的最大步数")
    context_threshold: int = Field(
        default=128000, ge=1, description="触发探索交接的上下文 token 数"
    )
    conclude_grace_calls: int = Field(default=3, ge=0, description="结束指令后的交接工具调用次数")
    grace_timeout: int = Field(default=5, ge=1, description="结束宽限时长，分钟")
    heartbeat_timeout: int = Field(default=30, ge=1, description="模型调用心跳超时，分钟")
    intent_max_attempts: int = Field(default=3, ge=1, description="意图未关闭时可被尝试的次数")
    max_consecutive_failures: int = Field(default=3, ge=1, description="连续运行错误上限")
    derive_empty_limit: int = Field(default=2, ge=1, description="连续空推导次数上限")
    derive_enabled: bool = Field(
        default=True, description="启用主动并行推导及额外空复核；必要完成复核始终保留"
    )
    close_reserve_ratio: Decimal = Field(
        default=Decimal("0.05"), ge=0, lt=1, description="总预算中为收尾预留的比例"
    )
    snapshot_max_lines: int = Field(default=150, ge=1, description="YAML 快照最大行数")
    delta_max_lines: int = Field(default=15, ge=1, description="增量推送最大行数")
    dispute_notify_depth: int = Field(default=2, ge=0, description="争议点名最大深度")


class TaskSpec(ContractModel):
    goal: str = Field(min_length=1, description="任务目标")
    domain_context: str | None = Field(default=None, description="领域提示")
    acceptance: list[AcceptanceItem] = Field(min_length=1, description="文字验收条件")
    budget: Budget = Field(description="金额、时长和并发预算")
    params: dict[str, int | float | Decimal | bool] = Field(
        default_factory=dict, description="Agent 配置参数覆盖"
    )
    agent_profile: str = Field(min_length=1, description="Agent 配置名称")
    egress_allowlist: list[str] = Field(
        default_factory=list, description="任务希望访问的域名（仅记录，不限制默认直连）"
    )

    @model_validator(mode="after")
    def check_params(self) -> TaskSpec:
        Params.model_validate({**Params().model_dump(), **self.params})
        return self


class AgentRun(ContractModel):
    task_id: UUID = Field(description="所属任务")
    id: str = Field(min_length=1, description="Agent 编号")
    task_type: AgentTaskType = Field(description="探索、推导或收尾")
    is_seed: bool = Field(default=False, description="是否种子探索")
    close_mode: CloseMode | None = Field(default=None, description="收尾的裁定或终结模式")
    judge_from_version: int | None = Field(default=None, ge=0, description="裁定开始的版本")
    derive_from_version: int | None = Field(
        default=None, ge=0, description="推导开始时的最大事实版本"
    )
    derive_parallel: bool = Field(default=False, description="登记时有探索 Agent 并行运行")
    derive_review: bool = Field(default=False, description="结束前必要推导复核")
    derive_round: int = Field(default=1, ge=1, description="推导轮次")
    round_start_version: int = Field(default=0, ge=0, description="本轮开始前的黑板版本")
    previous_receipt: dict[str, object] | None = Field(default=None, description="上一轮推导回执")
    finished_version: int | None = Field(default=None, ge=0)
    intent_id: str | None = Field(default=None, description="持有的意图")
    status: AgentStatus = Field(description="运行状态")
    end_reason: EndReason | None = Field(default=None, description="结束原因")
    steps: int = Field(default=0, ge=0, description="模型调用次数")
    context_tokens: int = Field(default=0, ge=0, description="最近输入 token")
    usage: Usage = Field(default_factory=Usage, description="累计用量")
    conclude_reason: str | None = Field(default=None, description="结束指令原因")
    conclude_requested_at: datetime | None = Field(default=None, description="结束指令时间")
    conclude_injected: bool = Field(default=False, description="是否已注入结束指令")
    grace_calls_left: int | None = Field(default=None, ge=0, description="剩余交接调用")
    last_seen_version: int = Field(default=0, ge=0, description="已见黑板版本")
    last_heartbeat_at: datetime | None = Field(default=None, description="最近心跳")
    receipt: dict[str, object] | None = Field(default=None, description="最终回执")
    started_at: datetime | None = Field(default=None, description="开始时间")
    finished_at: datetime | None = Field(default=None, description="结束时间")


class Event(ContractModel):
    version: int = Field(ge=1, description="全局递增版本")
    task_id: UUID = Field(description="所属任务")
    type: EventType = Field(description="事件类型")
    actor: str = Field(min_length=1, description="事件触发者")
    object_id: str | None = Field(default=None, description="关联对象编号")
    payload: dict[str, object] = Field(default_factory=dict, description="事件内容")
    addressed_to: list[str] | None = Field(default=None, description="点名目标，空值为广播")
    created_at: datetime = Field(description="创建时间")


class ExploreReceiptData(ContractModel):
    intent_result: Literal["confirmed", "rejected", "inconclusive", "none"] = Field(
        description="意图结果"
    )
    posted: list[str] = Field(default_factory=list, description="已提交对象编号")
    note: str = Field(description="调度日志说明")


class ExploreReceipt(ContractModel):
    accepted: Literal[True] = True
    data: ExploreReceiptData


class DeriveReceiptData(ContractModel):
    posted: list[str] = Field(default_factory=list, description="新提出的意图")
    excluded: list[str] = Field(default_factory=list, description="排除的方向与理由")


class DeriveReceipt(ContractModel):
    accepted: Literal[True] = True
    data: DeriveReceiptData


class RefusalReceipt(ContractModel):
    accepted: Literal[False] = False
    reason: str = Field(min_length=1, description="无法开始的原因")


class CloseReceiptData(ContractModel):
    note: str = Field(description="裁定或终结提交后的结束说明")


class CloseReceipt(ContractModel):
    accepted: Literal[True] = True
    data: CloseReceiptData


Receipt = Annotated[
    ExploreReceipt | DeriveReceipt | CloseReceipt | RefusalReceipt,
    Field(union_mode="left_to_right"),
]


class PostFactRequest(ContractModel):
    kind: FactKind
    statement: str = Field(min_length=1)
    evidence: list[Evidence] = Field(min_length=1)
    derived_from: list[str] = Field(default_factory=list)
    disputes: list[str] = Field(default_factory=list)
    resolves: str | None = None
    result: IntentResult | None = None
    satisfies: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_local_consistency(self) -> PostFactRequest:
        if self.kind == FactKind.INFERENCE and not self.derived_from:
            raise ValueError("inference requires derived_from")
        if (self.resolves is None) != (self.result is None):
            raise ValueError("resolves and result must be supplied together")
        return self


class PostIntentRequest(ContractModel):
    statement: str = Field(min_length=1)
    based_on: list[str] = Field(min_length=1)
    expected: str = Field(min_length=1)
    method: str = Field(min_length=1)
    relates_to: list[str] = Field(min_length=1)
    retry_of: str | None = None
    claim: bool = False


class ReleaseRequest(ContractModel):
    intent_id: str = Field(min_length=1)
    note: str = Field(min_length=1)


class SubmitCloseRequest(ContractModel):
    verdicts: list[VerdictItem] = Field(min_length=1)
    report: str | None = Field(default=None, min_length=1)


class Price(ContractModel):
    billing_mode: Literal["fixed", "deepseek_schedule"] = Field(
        default="fixed", description="固定费率或官方 DeepSeek 峰谷时段估算"
    )
    currency: str | None = Field(default=None, description="计价币种")
    cache_hit_per_m: Decimal | None = Field(
        default=None, ge=0, description="每百万缓存命中输入 token 单价"
    )
    cache_miss_per_m: Decimal | None = Field(
        default=None, ge=0, description="每百万缓存未命中输入 token 单价"
    )
    output_per_m: Decimal | None = Field(default=None, ge=0, description="每百万输出 token 单价")
    off_peak: bool | None = Field(default=None, description="是否启用错峰价格")

    def is_complete(self) -> bool:
        return all(value is not None for value in self.model_dump().values())


class ModelConfig(ContractModel):
    supports_vision: bool | None = Field(default=None, description="模型是否支持图片输入")
    context_window: int | None = Field(
        default=None, strict=True, ge=1, description="模型上下文容量，单位 token"
    )
    provider_options: dict[str, str] = Field(default_factory=dict)
    platform_id: str | None = Field(default=None, min_length=1)
    platform_version: int | None = Field(default=None, ge=1)
    provider: str = Field(min_length=1, description="模型供应商")
    model: str = Field(min_length=1, description="模型名称")
    base_url: str = Field(min_length=1, description="模型接口地址")
    reasoning_effort: str = Field(min_length=1, description="推理强度")
    price: Price = Field(description="模型价格表")

    @model_validator(mode="after")
    def complete_platform_reference(self) -> ModelConfig:
        if (self.platform_id is None) != (self.platform_version is None):
            raise ValueError("平台模型名称和版本必须同时填写")
        if self.price.billing_mode == "deepseek_schedule":
            from .billing import supports_deepseek_schedule

            if not supports_deepseek_schedule(self.base_url, self.model):
                raise ValueError("峰谷计费仅支持官方 DeepSeek 端点和已知模型")
        return self


class ModelSet(ContractModel):
    explore: ModelConfig
    derive: ModelConfig
    close: ModelConfig

    @model_validator(mode="after")
    def same_currency(self) -> ModelSet:
        currencies = {
            self.explore.price.currency,
            self.derive.price.currency,
            self.close.price.currency,
        }
        if len(currencies) != 1:
            raise ValueError("三个 worker 的模型价格必须使用相同币种，不能混合累计费用")
        return self


class PromptPaths(ContractModel):
    explore: str = Field(min_length=1, description="探索提示词模板路径")
    derive: str = Field(min_length=1, description="推导提示词模板路径")
    close: str = Field(min_length=1, description="收尾提示词模板路径")


class PromptTemplates(ContractModel):
    explore: str = Field(min_length=1, description="探索提示词模板正文快照")
    derive: str = Field(min_length=1, description="推导提示词模板正文快照")
    close: str = Field(min_length=1, description="收尾提示词模板正文快照")


class ExecResources(ContractModel):
    cpus: float = Field(gt=0, description="执行环境 CPU 限额")
    mem: str = Field(min_length=1, description="执行环境内存限额")
    pids: int = Field(gt=0, description="执行环境进程限额")


class McpBinding(ContractModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    version: int = Field(ge=1)
    allowed_tools: list[str] | None = None


class WorkerTools(ContractModel):
    builtin: list[str]
    mcp_servers: list[McpBinding] = Field(default_factory=list)


BUILTIN_TOOLS = {
    "explore": {
        "post_fact",
        "post_intent",
        "claim",
        "release",
        "get",
        "search",
        "read_evidence",
        "view_image",
        "execute_command",
    },
    "derive": {"post_intent", "get", "search", "read_evidence", "view_image"},
    "close": {"submit_close", "get", "search", "read_evidence", "view_image"},
}
REQUIRED_TOOLS = {
    "explore": {"post_fact", "release"},
    "derive": {"post_intent"},
    "close": {"submit_close", "get", "read_evidence"},
}


class AgentProfile(ContractModel):
    worker_tools: dict[Literal["explore", "derive", "close"], WorkerTools] = Field(
        default_factory=dict
    )

    models: ModelSet = Field(description="各任务类型模型与价格")
    params: Params = Field(description="默认调度参数")
    prompts: PromptPaths = Field(description="提示词模板文件路径")
    prompt_templates: PromptTemplates = Field(description="该版本固定的提示词模板正文")
    exec_image: str = Field(min_length=1, description="执行环境镜像")
    exec_resources: ExecResources = Field(description="执行环境资源限制")
    privileged_allowlist: list[str] = Field(default_factory=list, description="允许的提权命令前缀")

    @model_validator(mode="after")
    def validate_worker_tools(self) -> AgentProfile:
        for role, tools in self.worker_tools.items():
            selected = set(tools.builtin)
            if not selected <= BUILTIN_TOOLS[role] or not REQUIRED_TOOLS[role] <= selected:
                raise ValueError(f"{role} 工具配置超出角色权限或缺少必要工具")
            if len(selected) != len(tools.builtin):
                raise ValueError("内置工具不能重复")
            if role != "explore" and tools.mcp_servers:
                raise ValueError("外部 MCP 只能分配给 Explore；裁定与推导保留受限工具权限")
            if len({item.name for item in tools.mcp_servers}) != len(tools.mcp_servers):
                raise ValueError("同一 worker 不能重复挂载同名 MCP 服务")
        return self
