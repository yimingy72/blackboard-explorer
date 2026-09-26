# M5-E2 · 事实提交与结论可靠性补充报告

日期：2026-09-26。基线：`285a5f2`；设计提交 `1ad89b1`，运行时修复 `5527dcc`，提示词提交 `3c17f81`。

**当前验证状态：通用框架改进完成，373普通/35集成检查通过；同一套通用提示词在数据核对与代码诊断两个真实DeepSeek闭环中均通过验收。旧E2批次4次已完成运行均由主代理完成复核；该批原计划3+3没有补齐，不能据此声称架构优势。**

后续进度（2026-09-26）：通用工具反馈修复已独立提取为`07600e7`；预算数值隐藏已在`cbe2e0d`实现。二者已合入并保留。本轮按用户进一步要求确立“通用问题求解引擎”的定位，由主代理直接收尾。最终Profile移除了按单个评测类别编排的专属检查项，证据要求只来自任务目标、领域资料和验收条件；它不同于旧E2实验配置。

## 完成内容

1. **参数校验反馈**：E1 single/run-001 的 9 次 `post_fact` 失败都缺少首条证据的 `type`。MAF 在工具体之前拒绝参数，原日志中间件把原因隐藏成异常类名。本次返回具体字段路径，例如“缺少必填字段 evidence[0].type”，保留原有校验，不猜默认值、不放宽证据路径和引用要求。失败调用仍记录到事件及对象存储。
2. **事实与结论表述**：修正“fact 是已确认事实”的误导；`proposed`只是当前未被争议，`tool_backed`只是工具调用可追溯。区分观察、推断与限制，按任务验收选择证据形式；同一结论的补充证据归并，前提、成因或结论实质不同则保留，均由Agent判断。
3. **继续由 Agent 判断**：default/single 三份提示词完全一致。没有增加代码语义去重，没有改调度器或角色权限。Explore 仍以 `satisfies` 声明达标，由 Close 裁定完成状态并生成报告。用户在本轮讨论后确认保留该机制。
4. **评分纠偏**：复核 E1 两组全部 Fact/报告中的业务主张，纠正“同券重复参数不在已知答案，所以是误报”的口径。真实重复抵扣属于独立发现；利用步骤不自动成为独立缺陷；肯定性无依据主张即使最终改成观察也仍保留。没有修改答案、评分算法或 P1–P6 召回分数。原始 review/score 保留，修订在 `eval/results/e1-reaudit-20260926/`。

## 已运行检查

- `uv sync --locked`：通过，未改锁文件。
- 最终`make check`：373 passed、39 deselected，ruff/pyright通过，未调用真实模型。
- 最终`make test-integration`：35 passed、377 deselected（114.58秒）；执行环境、代理、runtime、eval镜像构建通过。blackboard镜像也已重建，前端生产构建通过（保留既有大chunk提醒）。
- 新增真实 MAF + 假模型回归：缺少 type 时不写黑板；拿到字段反馈、补齐后仅成功写一次；失败日志已持久化；额外字段及非法枚举的输入值不会出现在简短错误反馈。
- `make eval-score EVAL_OUT=eval/results/e1-reaudit-20260926`：通过，全部 review_pending 为空；default/run-001 仍无兼容 replay，保留 provisional，不把未测写成成功。

## 历史口径修订

| E1 3+3 | 原始 precision | 统一复核 precision | Recall（未变） |
|---|---:|---:|---:|
| default | 0.7413 | 0.8500 | 1.0000 |
| single | 0.7667 | 0.9167 | 0.8611 |

这些差值只来自复核纠错，不能当成系统能力提升。逐项旧/新分类与证据见 `.data/checkpoints/m5/e1-reaudit-20260926/`；原始结果仍在 `eval/results/e1-20260925-01/`。对“未给出限领或保密规则”的肯定漏洞断言，本次按证据不支持计 FP，并未证明其业务行为在任何场景都不存在缺陷。

## 历史E2样本（非最终通用版本）

批次 `e2-20260926-01` 在首个任务约 285.7 秒时中止，原因是用户提出调整裁定职责；随后用户明确保留现行职责。该批没有完成样本，不用于效果比较，取消路径没有导出账本，不能宣称其费用为零。中断记录单独保存。

按澄清后的范围运行 `e2-20260926-02`，原计划两组各 3 次。目标 49 个非 Git 文件与 E1 的归档逐字节相同；模型、预算、调度、答案和评分代码不变。

- default 三次已完成，耗时分别约 382.8、324.2、400.8 秒。
- default/run-001 已完成逐项复核：已知 P1–P6 全部1分，列表外有效发现2项、业务FP0，接口覆盖14/14。仅为一个样本，不推断整体改善。
- default/run-002的复核草稿曾把普通超额退款误列为new；该字段已在中断前改回P4。主代理随后直接核对事实、报告和原始证据，确认P1–P6均1、new2、FP0，旧说明中new3已明确纠正。
- default/run-003经主代理复核：P1–P6均1、new1、FP1（缺少依据的限领规则断言）；最终报告降级不能消除早期肯定性错误主张。
- 原文主脚本隔离 replay：default/run-001、002 通过；003 因固定 `/workspace/shared/repo` 路径不兼容新环境而失败。未编辑脚本、未在失败后换脚本，不宣称全部问题均可复现。
- 当时收到子代理内容审核报错后停止了代理及live批次，保留中断记录；单次拦截不应被扩大解释为整个授权项目不能开发。用户随后要求主代理直接完成，已独立完成4次已结束运行的只读核对，没有改写请求反复试审核。
- single/run-001（235.5秒）复核结果：P1/P3/P4/P5/P6各1，P2为0（没有形成独立并发全额退款的机制主张，不能替模型从原代码补出发现）；new2、FP0、覆盖14/14。其所选原始scan.py重跑因未处理的查询参数绑定错误退出1，复现失败如实保留。
- single/run-002运行约21.7秒后中止，run-003未开始。此后最终提示词已经通用化，未将新配置运行混入旧实验补样。4个完成样本的review_pending均为空；批次总体仍不完整。

旧配置4个样本的recall/precision/所选脚本replay依次为：default001 1/1/1、default002 1/1/1、default003 1/0.875/0、single001 0.8333/1/0。仅作历史部分结果；样本不均衡且最终提示词已改变，不能据此声称提升精确率、降低费用或多Agent优于single。完整记录见`.data/checkpoints/m5/e2-20260926-02/`。

## 最终通用版本的跨领域闭环

核心定位已写入README和设计正本。`make e2e`新增`--task`选择注册任务，原先写死在运行器中的pytest准备要求移回对应任务文件；其他任务不接收这些领域要求。未新增依赖或通用框架中的任务专属分支。

| 任务 | 实际验证 | 账本估算USD |
|---|---|---:|
| attendance-reconciliation | 2/2验收met；5Fact、2Intent、4Agent。主代理读取归档：3行summary与9条issues逐项匹配独立参考；3份原始CSV逐字节未变 | 0.08072448 |
| flaky-order-test | 2/2验收met；7Fact、2Intent、3Agent。原始输出显示100/100与200/200稳定复现，加互斥的对照为0/300、0/200；原始orders.py逐字节未变 | 0.06399822 |

两任务使用同一固定版本的通用提示词，未根据结果调整提示词。资料核对任务的领域规则仅在任务输入/数据包中，参考答案没有打入目标包。macOS归档额外生成的`._`元数据已通过原生COPYFILE_DISABLE选项排除，检查得到目标包仅含4个预期文件。

工作区、事件、报告保存在主仓库`.data/e2e/bbx-e2e-86460215/`（数据）与`.data/e2e/bbx-e2e-a3da516e/`（代码）；精简验收结果在`.data/checkpoints/m5/framework-e2e-20260926/`。两套隔离容器均已清理。这是功能及跨领域回归，不是通用求解能力或性能优势的统计证明。

## 复现命令

```sh
uv sync --locked
make check
DOCKER_BUILD_ARGS='--add-host host.docker.internal:host-gateway --build-arg http_proxy=http://host.docker.internal:7897 --build-arg https_proxy=http://host.docker.internal:7897 --build-arg no_proxy=localhost,127.0.0.1,::1,host.docker.internal' make test-integration
make eval-run EVAL_ENV_FILE=/Users/yym/blackboard-explorer/.env EVAL_OUT=eval/results/new-experiment EVAL_N=3
make eval-score EVAL_OUT=eval/results/new-experiment
```

外网命令沿用 AGENTS.md 的代理与 UV_CACHE_DIR。真实模型只在显式 eval-run 中使用，由 uv 子进程加载既有 `.env`；本轮未读取、显示或修改密钥。复核 review.json、选择归档脚本后，再运行 `make eval-replay EVAL_OUT=... EVAL_RUN=...` 和 eval-score。所选脚本复现成功不代表全部问题都可独立复现。

## 偏差与待决

- 原E2的3+3没有补齐，已结束的4次复核已完成；中断数据继续保留。通用框架实现及两个独立闭环已经完成，二者的状态应分开记录。
- 未做原调优分析暂称 E2 的“裁定节流”。本轮 E2 是可靠性修复批次；用户已确认当前裁定职责合理，不因高成本直接换架构。
- 参数失败反馈依赖已锁定 MAF 的参数异常链；用真实框架回归守护，其他内部异常仍不展示原始值。
- 提示词是行为指导，无法保证每次模型输出都正确。本轮同时修工具反馈与表述规则，3+3 同任务只能作探索性验证，不能证明因果或推广到其他任务。
- single 仍使用黑板与 Close，仅并发为1且关闭 derive；不是无黑板的普通单 Agent。

## 共享文件与提交划分

未改contracts、Makefile、依赖、锁文件或Compose配置。共享的eval/targets/build.sh增加非代码任务打包并关闭macOS资源分叉元数据；e2e入口增加已注册任务选择，保留默认任务兼容。

1. `Clarify fact confidence and finding review rules`：设计质量规则与本任务范围。
2. `Return actionable tool argument validation errors`：中间件和回归测试。
3. `Separate observations from confirmed findings in agent prompts`：两组提示词。
4. `Record reliability checks and corrected evaluation review`：复核说明、检查点和本报告。

收尾追加：`Define task-independent evidence and conclusion rules`、`Generalize agent prompts across problem domains`、`Position the platform as a general problem-solving engine`、`Add domain-independent end-to-end task selection`，分别记录通用设计、提示词、产品定位与跨领域回归入口。

后续仅在新证据支持时考虑调度改动；比较收益前应增加独立任务与重复次数。
