# 通用问题求解引擎

本项目通过共享黑板上的多个 Agent 处理有明确目标和完成条件、但求解路径需要探索的任务。资料核对、数据分析、假设验证、方案研究和软件诊断都是可用场景；领域知识和验收条件由任务提供，通用 Profile 不预置某个评测题的答案或专属清单。

创建任务时填写目标 `goal`、领域资料 `domain_context` 与文字验收条件。Explore 调查并提交 Fact/Intent，derive 根据黑板上的缺口提出方向，Close 核对证据、裁定完成状态并生成报告。Agent 根据黑板信息自行判断语义重复；调度器管理费用、时间和并发。操作步骤见 [使用与部署](docs/使用与部署.md)。

系统由三个运行组件构成：`blackboard` 提供 API、浏览器工作台、事件流和持久化；`agent-runtime` 调用模型、同步黑板并运行确定性调度器；每个任务的 `exec-env` Ubuntu 容器承接 Explore 的命令操作。PostgreSQL 保存事件和折叠状态，MinIO 保存证据、工具记录、报告及归档。浏览器通过黑板服务创建任务、查看实时变化和下载产物，不直接连接执行容器。

## 探索如何运转

黑板是 Agent 唯一的共享记忆和协调通道。Agent 不直接对话，也没有某个持有全部私有结论的“主 Agent”。黑板服务保存带全局版本的只增事件，形成当前任务、事实、意图、Agent 和验收状态的视图；证据、工具调用全文、最终报告和工作区归档存入对象存储。调度器是纯代码：它读取当前状态，决定创建、结束或重新派发 Agent；它不替 Agent 判断事实真假或目标是否完成。

一次任务沿着这条循环推进：

```text
人给出 Goal + 验收条件 + 预算
  → 空黑板启动一个种子 Explore
  → Explore 调查，提交有证据的 Fact 和待验证的 Intent
  → 调度器按 Intent 派发更多 Explore，并行调查、复核和交接
  → 有达成声明或探索暂时停下时，Close 裁定每项验收条件
  → 仍有缺口且无待查 Intent 时，derive 从事实和裁定反馈提出新 Intent
  → 全部验收通过或达到终止条件后，Close 生成终结报告
```

### 起点：Goal 和执行环境

任务有一个目标、可选的领域背景、至少一项带编号的文字验收条件，以及金额、时长、并发上限。目标回答“要解决什么”；验收条件回答“拿什么证据判断完成”；领域背景提供资源地址、业务规则和操作边界。选择的 Profile 版本在创建任务时固定，包含模型、提示词、调度参数和执行环境配置。领域专属要求应写在任务中，而不是塞进通用 Profile。调度器记录用量并强制预算，但不把剩余金额或时间数值塞给 Agent。

每个任务有一个 Ubuntu 执行容器；同一任务的 Explore 共用 `/workspace`，但各自使用独立 Linux 用户。只有 Explore 能通过 `execute_command` 在其中运行命令、读取目标、发请求或生成证据文件。Agent runtime 持有模型客户端和黑板工具，执行容器不接收模型密钥或服务密钥。容器默认直连公网，代理隔离模式需要显式启用，详见[网络模式](docs/使用与部署.md#网络模式与排障)。

### 黑板：Fact、Intent 和关系

| 对象 | 内容和作用 | 关键规则 |
|---|---|---|
| Fact | 有证据的陈述，可为直接观察 `observation`、由已有事实推导的 `inference`，或目标相关结构 `structure` | 提交时必须引用证据文件并持久化；推断要给 `derived_from`。`proposed` 表示当前未被争议，不等于已获独立确认。 |
| Intent | 从已有 Fact 出发的待查方向，写明成立时应看到什么 `expected`、怎么查 `method`、关联哪项验收条件 `relates_to` | 同时只能被一个 Explore 认领；关闭时必须由持有者提交带 `resolves` 和 `result` 的 Fact，结果为 `confirmed`、`rejected` 或 `inconclusive`。 |
| 争议与达成声明 | Fact 可以用 `disputes` 反驳另一 Fact，用 `satisfies` 声称支持某验收条件 | 争议也须有证据；`satisfies` 只请求 Close 裁定，绝不直接把条件改为 `met`。 |

所有对象和状态变化留在事件日志中，不因后来的结论而删除。事实引用真实工具调用的 `call_id` 时，系统附上原命令及输出；`tool_backed` 表示可追溯，仍不保证 Agent 的解释正确。系统校验证据、身份和引用关系，但**不做语义查重**：Agent 读快照与增量，用 `get`、关键词 `search` 和 `read_evidence` 自行判断是否已有相同发现；`search` 不是向量相似度搜索。不同前提、成因或结论即使措辞相近，也应分别保留；错误主张通过有证据的争议事实纠正。争议也可以被后来的事实反驳，因此 `proposed`/`disputed` 按整条争议链重新计算。支撑某项 `met` 裁定的 Fact 一旦变为 `disputed`，该项会自动退回 `unmet`。结果为 `inconclusive` 的 Intent 如要换方法重查，应新建带 `retry_of` 的 Intent。

### 三种工作任务

| 任务 | 何时运行 | 能做什么 |
|---|---|---|
| Explore | 黑板为空时作为种子启动；以后每条开放 Intent 可触发一个，受并发与预算限制 | 调查、运行命令、提交 Fact/Intent、认领或释放 Intent、读取黑板；可用 `satisfies` 声明已找到满足条件的证据。种子负责从目标出发产出首批事实和方向，可自己认领新 Intent。 |
| derive | 探索静止且已裁定时启动；探索仍在进行时，如有新 Fact、空闲槽位且没有待分派 Intent，也可启动一个并行 derive | 结合全部事实、意图、争议和 Close 指出的缺口提出新 Intent；不能执行命令或提交 Fact。提出不了方向则明确返回空。 |
| Close | 新的达成声明出现，或探索静止且黑板有变化时以 `judge` 模式运行；任务进入 `closing` 后以 `final` 模式运行 | `judge` 逐项给出 `met`/`unmet`、理由、支撑事实或缺口，可与 Explore 并行且不占探索并发槽位。`final` 最后裁定未满足项并写报告。 |

这三种任务使用同一种 Agent 构造，只是提示词、工具和启动上下文不同。Close 的 `met` 必须引用至少一条当前未被争议的 Fact；`unmet` 必须写明缺什么。Explore 有权判断并提出达成声明，但**验收状态只由 Close 裁定改变**（或因支撑事实被争议而回退）。终结 Close 的主要职责是最后核对和形成报告，不要求到最后才第一次判断是否达标。

### 调度、同步与交接

调度器先处理失败和上限，再处理验收与预算，然后决定是否裁定、派发或推导。探索费用阈值是任务 `max_cost × (1 − close_reserve_ratio)`，其余金额留给收尾；达到任务时长上限也会结束探索。新 Intent 优先派给关联仍未满足验收项的方向，其次按创建顺序；每条 Intent 最多一个持有者。种子启动时不带当前 Intent，其他 Explore 启动时得到目标、领域背景、当前验收状态、所认领 Intent 与依据事实，以及压缩的黑板快照。需要细节时按 ID、关键词或证据 URI 读取。Explore 的每次模型调用前接收黑板增量：与自己有关的争议、conclude 和裁定反馈给出全文，其他变化给摘要；过长则给计数并提示按需读取。derive 和 Close 是短任务，启动时读取完整所需快照。黑板数据是证据资料，不是新的操作指令。

Explore 应及时发布有证据的中间发现和独立方向；每约 5 轮没有本人发布时，系统提醒检查共享机会。并行 derive 对同一事实版本只触发一次，空产不推进静止收尾计数；派发阶段在黑板事务内重新核验。Explore 在意图解决后可原子认领下一条，也可释放意图并留下 `notes`。达到步数或上下文上限、任务进入收尾时，系统发出 `conclude`；宽限期仅允许有限次 `release`、`post_fact`、`post_intent`，用于保存发现与交接。结束回执不是黑板写入，只有已提交的事实和意图会留给后来者。未关闭的 Intent 被释放后回到开放状态，达到重试上限则系统以 `inconclusive` 关闭，避免无限重派。运行错误连击或种子反复无法起步会使任务失败；runtime 重启会按黑板状态恢复派发。

默认调度参数（可由任务/Profile 的有效配置覆盖）包括：Explore 最多 60 次模型调用；种子在未认领意图前最多 20 次；上下文阈值 128,000 token；收到 conclude 后最多 3 次交接工具调用、宽限 5 分钟；单条 Intent 未关闭的尝试上限 3 次；连续运行错误上限 3 次；derive 连续空产上限 2 次；收尾金额预留比例 5%。任务自己的金额、时长和并发上限在创建时指定。具体参数定义见[概念设计 §9](docs/design/黑板式探索架构设计.md#9-参数)。

### 裁定与结束

出现新的 `satisfies` 声明后，调度器可在 Explore 继续工作时启动一次 `judge`；探索全部停下且黑板自上次裁定后有变化时也会裁定。若还有未满足项，裁定理由和 `missing` 会反馈给探索者；没有可查意图时再交给 derive。derive 连续无法提出方向、探索预算用尽或人工停止时，进入终结收尾；全部验收项 `met` 时进入成功收尾。收尾先通知仍在运行的 Agent 交接，之后由 `final` Close 写报告，任务变为 `finished`。即使以未满足项结束，报告也应列出缺口。模型或运行环境连续失败等异常可使任务直接 `failed`，此时可能没有终结报告。任务结束后工作区归档，执行容器销毁。

默认 Profile 允许并发探索与 derive。评测用的 `single` Profile 关闭 derive；只有在创建任务时同时把并发上限设为 1，才是项目当前的单 Explore 基线。它**仍使用黑板、Fact/Intent、Close 裁定和报告**，不是“没有黑板”的传统单 Agent。历史评测结果见 [HANDOFF](docs/HANDOFF.md)，不能把旧网络或旧 Profile 的样本当作当前配置的同版本对比。

例如，任务目标是“核对两份月度汇总是否一致”，验收条件 A1 要列出每个不一致项及原始行依据，A2 要解释差异来源。种子 Explore 读取两份数据，将文件位置和字段含义写成 F1，再根据初步差异提出 I1“核对日期口径”、I2“核对重复记录”，每条意图都带预期结果和操作方法。调度器把 I1、I2 分给不同 Explore。检查 I1 的 Agent 提交带证据的 F2（`resolves=I1`、`result=confirmed`），并用 `satisfies=[A1]` 声称找齐差异；Close 可能裁定 A1 仍 `unmet`，指出缺少原始行对照。这个缺口会同步给继续探索的 Agent，后者补交 F3。若所有方向暂时关闭，derive 可依据裁定缺口提出 I3“逐行对照源文件”。只有 Close 对 A1、A2 都裁定 `met`，任务才进入成功收尾；若预算先耗尽，终结报告要保留仍未解决的差异。

## 用户干预与持久会话

点击 Agent 后，可在右侧输入消息。运行中的消息进入持久队列，在下一次模型调用前送达；界面显示等待、处理中、已送达或失败。Session 保存实际模型历史、工具结果、黑板注入和初始指令，服务重启后可恢复。

Agent 或任务结束后仍能继续问答，读取已有黑板和证据；该模式不执行命令、不写 Fact/Intent、不改变验收结果。复盘费用单独记录。旧任务未保存完整 Session 时会明确标记基于已有记录初始化，不伪称恢复缺失历史。需要重新执行探索时应明确创建新的执行任务。

会话不因任务结束或归档而清除。任务列表中的删除操作经确认后清理该任务及其所有 Agent 会话、消息、证据、报告和归档；运行中的任务不能直接删除。删除清理失败会保留状态并重试。

## 人民币与 Agent 配置

内置 default/single 最新版本以人民币 CNY 计价，创建任务的金额上限单位为元，列表、工作台、Agent 与复盘费用均显示币种。采用2026-09-28核对的 [DeepSeek 官方人民币高峰价](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)估算：每百万 token 缓存命中0.04元、未命中2元、输出8元；沿用固定价策略，不自动切换峰谷，因此不等同于账单实扣。历史任务继续使用其固定版本的原币种（例如 US$），不会改写或按现价换算。新 Profile 的三个 worker 必须同币种。

Agent 配置默认按 Explore、derive、Close 展示完整系统提示词，可逐个编辑；种子包含在 Explore 模板内，judge/final 包含在 Close 模板内。目标、黑板等变量在实际运行时注入。高级 YAML 可编辑完整模型、价格与参数，与提示词页签共用草稿；发布产生新版本，不改变已创建任务。浏览器如固定了旧美元版本，请在创建任务时选择最新人民币版本。

## 本地启动

在仓库根目录，将 `.env.example` 复制为 `.env`，由你填好本地服务凭据、`ADMIN_USERS` 和模型密钥。不要把 `.env` 提交到 Git。

```sh
UV_CACHE_DIR=/private/tmp/bbx-uv-cache uv sync --locked
make web-install web-build
make image-exec-env image-agent-runtime image-blackboard
docker compose --project-name blackboard-explorer up -d --wait
```

打开 <http://127.0.0.1:58000/login>，使用自己配置的管理员账号登录。需要使用仓库内的评测目标时，先运行 `make eval-targets`，启动命令再叠加 `-f docker-compose.yml -f docker-compose.dev.yml`；具体命令、网络模式切换和排障见 [使用与部署](docs/使用与部署.md)。`make up` 只启动基础 Compose 服务，不会构建镜像或启用内置评测目标。

执行 Ubuntu 容器默认直接出网（`EXEC_EGRESS_MODE=direct`、`EXEC_NETWORK_INTERNAL=false`）。宿主机下载依赖所用代理、运行时访问模型的 `RUNTIME_HTTP_PROXY`，以及可选的执行容器白名单代理，是三个不同配置；参见[网络说明](docs/使用与部署.md#网络模式与排障)。

## 验证与进一步阅读

`make check` 使用假模型，不调用 DeepSeek；`make test-integration` 使用 Docker，也不调用 DeepSeek。`make e2e`、`make eval-run` 和从工作台启动真实任务会调用模型并产生费用。任务选择与评分见 [评测任务](eval/tasks/README.md) 和 [评测运行器](eval/runner/README.md)。

设计正本见 [概念设计](docs/design/黑板式探索架构设计.md)及 [实现架构](docs/design/黑板系统实现架构.md)，当前进度见 [HANDOFF](docs/HANDOFF.md)。运行时实现见 [Agent runtime](services/agent-runtime/README.md)，前端见 [Web](web/README.md)。
