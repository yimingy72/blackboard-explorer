# CTF-context-recovery · 缓存与运行报告复核

日期：2026-10-12。工作树 `/Users/yym/bbx-wt/ctf-cooperation`，分支 `feature/ctf-cooperation`。用户要求分析最新任务的低缓存命中率，审查 `bbx-cybench-smoke/deploy/batch/CYBENCH-RUN-REPORT.md` 后一并优化；子代理统一 GPT-6.1 Sol high，主会话审核、补齐预算/模板、完整验证并部署。

## 1. 真实数据与主要问题

检查任务 **`d11f03ea-e182-4a3a-8bf9-511e9995c490`，Cybench 全量 31 题**。截至 2026-10-12 12:31:57 UTC，任务结束，用量不再增长：465 次成功回复，输入命中 **2,102,825**、未命中 **7,113,978**，token 加权命中率 **22.82%**；已记录费用估算 24.720197 CNY。task、member、execution turn、MinIO model_output 的合计完全相同，没有遗漏 usage、待结 outbox 或未知预留。本次不能把低值归因于缺失整个 usage 或重复累计。

| 调用分组 | 调用数 | 输入 token 加权命中率 |
| --- | ---: | ---: |
| 全部成功回复 | 465 | 22.82% |
| 本次发送前裁剪起点向后移动 | 264 | **3.60%** |
| 本次发送前未移动裁剪起点 | 201 | **49.87%** |
| 尚未裁过历史 | 44 | 62.88% |
| 已裁过历史 | 421 | 20.06% |
| 使用工具结果预览 | 233 | 12.43% |

这些组有重叠，不能当作互斥原因相加，也不能据此计算修复后的命中率。它们与代码的前缀变化机制形成明确证据链：[DeepSeek 官方缓存说明](https://api-docs.deepseek.com/zh-cn/guides/kv_cache/)要求复用已经缓存的匹配前缀，删除旧头部或重写早期结果会损失这种复用。

具体缺陷：

1. **配置不生效**：任务保存 `max_steps=4000/context_threshold=400000`，运行时只取 profile 的 `60/128000`。模型配置为 `openai_responses`，经百智云调用 `deepseek-flash`，窗口 **1,000,000 token**，推理强度 max。运行前后四个关键模块的部署散列与检查源码一致，分析不是针对未部署版本。
2. **单位与模型视图错误**：128000 阈值被乘 0.8 后当 **102400 字符**使用，又比较整个 `Message.to_dict()`，含内部属性和未实际发送的记录。各成员第 6–8 次调用就开始裁历史，之后频繁更换前缀。Lead 初期曾有 80–93% 命中，第 8 次首次裁剪后降到约 1.8%；这种轨迹说明缓存确实可以工作。
3. **旧结果再次变化**：工具预览额度按当前结果数量重新计算，新增结果可能再次截短此前已发送的结果。一次长结果也可能改变多处早期内容。
4. **恢复链没有换掉不可发送的模型视图**：完整 Session 被保存，但继续沿用阻塞视图，resume 后会再次立即失败。Lead 原最终会话有 248 条消息，旧模型视图已排除前 241 条；其他成员排除 87–117 条。

逐成员命中率：Lead 25.57%、crypto 34.96%、rev 12.10%、forensics 28.01%、web 15.59%、pwn 26.57%、misc 19.21%。主因是上下文管理和配置保真度，增加成员数本身不能修复同一会话的前缀变化。

## 2. 对外部报告的审查

**确认**：配置被忽略、60 步结束后依赖派工、Lead/队友上下文封锁、旧续跑无法处理相同历史、只用了部分预算就收尾，均有状态或调用证据。外部报告记录匹配 6/31，但匹配结果不是黑板可信验证状态；本次未重新判分或读取官方答案。

**更正**：

- 没有 claim_turn 日志不能证明 coordinator hang：代码只对 `ready_member_ids` 认领，并非每次 tick 都向所有 idle 成员请求。还可能等待合法的恢复、停止、checkpoint、资源状态等操作。本轮增加等待阶段观测，没有据此盲取消控制协程。
- 60×11=660 是理论上限，不能当实际模型调用次数；本次有 465 次成功回复，其中阻塞回合可能零调用。
- “预算跑满能解 15–20 题”“一半以上缺口来自平台”没有足够对照数据，不能线性外推。修复可靠性不等于保证答案数。
- 后台自动给空闲成员选新题会改变 Lead 的负责边界，本轮只续接既有分工；范围核对与增援仍由 Lead 决策。
- 候选答案不能由代码自动升级 accepted 或题目完成。对 owner 的状态更新加强模板说明，保留工作完成与验证分离。

原报告、外部 grade、任务历史、原预算和模型配置保持不变。

## 3. 已实现的优化

### 3.1 配置与窗口

`resolve_ctf_options` 统一有效配置：保存的任务级值覆盖 profile；创建请求按实际提供字段合并，省略步数/人数继承配置，省略阈值且模型窗口已知时使用原始窗口。显式超窗返回 422，拒绝时不创建 profile、任务或 provision。有效值同时保存在固定 profile 与任务快照。

模型输入以 token 保守估计管理，计入固定系统提示、工具 schema、模型窗口和输出预留，再应用一次 20% 安全余量。这个估计不是供应商精确 tokenizer，实际 usage 仍是账本依据。DeepSeek 1M 配置保留原最大输出 393216；较小窗口的输出上限与请求/预算共同缩小，至少保留一半窗口给输入，不再预留不可能的“满窗口输入＋超过窗口输出”。

任务的 400000 阈值现在对应 320000 的输入估计预算，而非 102400 字符。没有改变用户选定的 max 思考强度、成员上限、价格或预算。

### 3.2 稳定前缀与 MAF 复用

继续复用 MAF 的 Agent、AgentSession、InMemoryHistoryProvider、MessageInjectionMiddleware、FunctionTool/工具循环、ResponseStream 与模型客户端。实际依赖为 core 1.19.0、openai 1.14.4，没有升级或新增依赖。

使用顶层公开 **`annotate_message_groups` / `GROUP_ANNOTATION_KEY`** 替换手写配对；非连续工具调用/结果、并行批次、reasoning 与 replay 分组不能拆开。浅拷贝保护保存的消息，旧 checkpoint 的 cutoff 在组内部时只向后归齐到完整已完成边界；没有安全边界时明确阻挡，不能重新引入旧调用或伪造结果。

长工具输出的首次模型预览决策保存到 Session；新增结果、重启或预算变化不重新缩短旧预览。原始输出、附件引用和完整历史保留。历史超限时一次批量裁到低水位，留出后续追加空间，避免每次调用重新移动前缀。保留稳定的目标与黑板恢复入口，不每次插入动态快照。

没有直接启用 MAF 默认工具结果压缩，因为它会随着历史组变旧改写旧输入；也没有启用需要额外模型调用的 SummarizationStrategy。原生 compaction 可用，但预算、持久 cutoff、稳定预览及 generation/receipt 仍是本平台业务约束。[MAF 官方文档](https://learn.microsoft.com/agent-framework/concepts/agents/conversations/compaction)。

### 3.3 有界恢复与投递状态

- `step_limit` 每成员每 run 最多自动续跑 **3 次**，`context_blocked` 最多 **1 次**；在结算同事务中去重，沿用同成员和既有分工，受预算、phase、停止、代次及成员操作保护。
- 新回合可以重建旧阻塞模型视图；超大新用户输入或派工仍明确阻挡，不能静默丢弃。完整 Session、已完成调用与结果未知工具不重执行。
- 新用户消息可请求 blocked 成员恢复，返回 `context_recovery_requested/context_blocked/deferred`；重复旧消息不会自动唤醒。终态复盘仍不能自动重开任务。
- 显式 resume 重置本轮恢复额度；不会仅清数据库封锁而让旧模型视图继续失败。旧 Lead 封锁先给一次恢复机会，失败后的 partial 明确说明恢复已尝试。
- 协调器记录等待阶段，超过 60 秒告警一次；不取消合法的长 drain/provision/checkpoint，不推定未知副作用已经结束。

### 3.4 提示词与范围核对

Lead 首次派工前核对目标、背景、附件和平台已知范围，登记暂不可用目标并核对给定题量；不把打开的附件当成全部任务。owner 完成可执行工作后明确更新题目状态，保留独立验证要求。两角色将可信身份、目录和实际工具名单放到稳定正文之后，减少不同成员在系统提示开头分叉。

没有写入 Cybench 题名、地址、答案、具体漏洞路线、固定单题时间或强制专家类别，也没有让后台替代 Lead 选题。

## 4. 实际验证

- `make check`：**1247 passed / 184 deselected**，19.04 秒；268 文件格式、Ruff、Pyright 通过，3 条既有 MCP sampling 弃用提示。
- 完整 `make test-integration`：**180 passed / 1251 deselected**，264.63 秒，使用随机端口 PostgreSQL/隔离执行环境/脚本化 MAF 客户端。运行时镜像采用已构建的本地源码镜像，不拉取新依赖，不调用真实模型或靶机。
- 重点回归覆盖配置 4000/400000、1M 默认窗口、超窗拒绝、稳定预览、原始历史保留、实际 Session 序列化重启、native 非连续配对、Responses 离线请求序列化、旧 cutoff 迁移、超大新输入阻挡、预算和停止、自动恢复次数/幂等/原分工、resume、显式消息投递和慢等待观测。
- 原始 7 个会话在内存中做离线恢复视图检查：原历史全部不变，模型调用 0。新视图通过当前任务 320000 token 估计 gate，Lead 历史部分约 77103、crypto 约 86873；此审计只计 opening prompt、未装配工具 schema，不能替代完整请求窗口验证。含工具 schema 的完整链路另由上述脚本化回归覆盖。
- OpenAPI 生成、`git diff --check` 通过。UI 没有变更，B 前端原构建保留，未重跑前端全量用例。

首轮集成 178 通过、2 失败：一个随机 PostgreSQL 迁移临时认证失败（未改代码，隔离重跑通过），一个测试仍硬断言小窗口应发 393216 输出，已更正为受 128000 窗口约束的 64000。定向 3 项通过，随后完整 180 项全绿。普通检查阶段的格式/import/TypedDict 测试错误已修正，最终完整检查通过，不隐去初轮失败。

复现：

```sh
cd /Users/yym/bbx-wt/ctf-cooperation
UV_CACHE_DIR=/private/tmp/bbx-uv-cache make check
TESTCONTAINERS_RYUK_DISABLED=true UV_CACHE_DIR=/private/tmp/bbx-uv-cache \
  make -o image-exec-env -o image-egress-proxy -o image-eval-env \
  -o image-agent-runtime test-integration
```

日志与脱敏元数据位于 `.data/cache-analysis-20261012/`：`call-metadata.json`、`summary.json`、`resume-view-replay.json`、`check.log`、`integration.log`、`integration-repair.log`、`integration-final.log`、`image-source-check.json` 及部署指纹。未保存原始会话副本到审计文件，只在内存中读取必要历史。

## 5. 本地发布

地址仍为 <http://127.0.0.1:58000/login>。

- 发布前 7 任务均终态、0 running turn、0 待处理复盘、0 待清理任务；没有重跑或续跑用户任务。
- 使用本地基础镜像、`--network=none` 只打包源码/合同/模板。现有容器停后更新应用源并启动；数据库、MinIO、数据卷、靶机和归档不重建。41 个 blackboard/合同/模板文件、68 个 runtime/合同/模板文件与检查源码逐一 SHA256 一致，应用服务 running、restartCount 0。
- 当前 future-recreate 镜像：runtime `fdac48fa60911662723527acb927f13f8b7fabebeef29ad1ef856b453857b95d`，blackboard `d12f7219963e7b75262a4daf35cfecb76aa70135248c062d69416bb13c286356`。当前容器 ID 保持，使用已核对的更新源码；没有用较旧前端覆盖 B。
- 两角色模板通过带 expected_revision 的原 API 发布，CTF 配置 revision **5 → 7**；仅 prompt_templates 变化，模型、价格、工具、资源及所有旧 profile 行保持。任务/轮次/Session/消息/记录/事件/配置的全表摘要（新模板版本除外）部署前后相同。
- schema 0014、登录 HTTP 200；B 前端 index SHA256 仍为 `030eea19b74cb4d35a812b84f643545f5df02be80cd8246aacf5547dbcd71cbf`。应用源码备份保存在同目录 `before-source/`，已有 PostgreSQL 恢复备份未动。
- 没有读写 `.env`、回显凭据、提交/合并/推送、修改 docs/design 或全局配置。认证只在既有服务进程配置内使用，部署没有读取用户凭据文件。

## 6. 边界与建议提交

本次尚未重跑收费的 31 题，不承诺修复后达到某个命中百分比、答案数或固定提速。历史已经变化的前缀不能补回供应商缓存，已有 checkpoint 沿用安全 cutoff，通过目标与黑板恢复；新模板用于新任务，旧任务 opening instructions 不重写。旧任务可由用户显式 resume，保存的 4000/400000 此后实际生效。

相对前序 batch-hardening 的“阻塞旧输入不自动重启”，本轮用户要求优化恢复后增加了**一次有界新回合**；不是循环绕过超大输入，原材料保留，预算/停止门禁继续生效。Token 估计、图片与厂商隐藏内容仍存在估算误差，容量必须按实际模型/网关能力配置；本轮没有调用网关确认容量或原始缓存字段。

建议在前序未提交修改之后分别整理：

1. `fix(ctf): honor task limits and model input capacity`：合同合并、创建入口、runtime 读取、输出/预留及配置测试。
2. `fix(ctf): preserve cache prefixes and recover durable context`：原生 MAF 分组、稳定预览、token 估计、批量裁剪、迁移/重启测试。
3. `fix(ctf): continue limited turns with bounded recovery`：事务续跑、消息可观测状态、resume、等待阶段与相关普通/集成检查。
4. `docs(ctf): clarify task scope and long-run recovery`：通用模板、方案、使用文档、任务与本报告。

当前分支含前序 cooperation/prompts-tools/batch-hardening 改动，后续按实际差异拆分，不能把全部 dirty diff 归本轮；此次未执行 Git 写操作。无需用户执行额外沙箱命令。
