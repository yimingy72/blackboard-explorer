# M5-E2 · 事实提交与结论可靠性补充报告

日期：2026-09-26。基线：`285a5f2`；设计提交 `1ad89b1`，运行时修复 `5527dcc`，提示词提交 `3c17f81`。

**当前状态：实现与普通/集成检查完成，评测复核因模型服务内容审核拦截停止；尚未合并 main。工作树保留在 `/Users/yym/bbx-wt/m5-e2`。**

## 完成内容

1. **参数校验反馈**：E1 single/run-001 的 9 次 `post_fact` 失败都缺少首条证据的 `type`。MAF 在工具体之前拒绝参数，原日志中间件把原因隐藏成异常类名。本次返回具体字段路径，例如“缺少必填字段 evidence[0].type”，保留原有校验，不猜默认值、不放宽证据路径和引用要求。失败调用仍记录到事件及对象存储。
2. **事实与结论表述**：修正“fact 是已确认事实”的误导；`proposed` 只是当前未被争议，`tool_backed` 只是工具调用可追溯。要求分别说明观察、结论和限制，业务缺陷须有规则或不变量依据；同根因变体在报告中归并，有独立依据的新问题仍保留。
3. **继续由 Agent 判断**：default/single 三份提示词完全一致。没有增加代码语义去重，没有改调度器或角色权限。Explore 仍以 `satisfies` 声明达标，由 Close 裁定完成状态并生成报告。用户在本轮讨论后确认保留该机制。
4. **评分纠偏**：复核 E1 两组全部 Fact/报告中的业务主张，纠正“同券重复参数不在已知答案，所以是误报”的口径。真实重复抵扣属于独立发现；利用步骤不自动成为独立缺陷；肯定性无依据主张即使最终改成观察也仍保留。没有修改答案、评分算法或 P1–P6 召回分数。原始 review/score 保留，修订在 `eval/results/e1-reaudit-20260926/`。

## 已运行检查

- `uv sync --locked`：通过，未改锁文件。
- `make check`：359 passed、39 deselected，ruff/pyright 通过，未调用真实模型。
- `make test-integration`：35 passed、363 deselected；执行环境、代理、runtime、eval 镜像构建通过。
- 新增真实 MAF + 假模型回归：缺少 type 时不写黑板；拿到字段反馈、补齐后仅成功写一次；失败日志已持久化；额外字段及非法枚举的输入值不会出现在简短错误反馈。
- `make eval-score EVAL_OUT=eval/results/e1-reaudit-20260926`：通过，全部 review_pending 为空；default/run-001 仍无兼容 replay，保留 provisional，不把未测写成成功。

## 历史口径修订

| E1 3+3 | 原始 precision | 统一复核 precision | Recall（未变） |
|---|---:|---:|---:|
| default | 0.7413 | 0.8500 | 1.0000 |
| single | 0.7667 | 0.9167 | 0.8611 |

这些差值只来自复核纠错，不能当成系统能力提升。逐项旧/新分类与证据见 `.data/checkpoints/m5/e1-reaudit-20260926/`；原始结果仍在 `eval/results/e1-20260925-01/`。对“未给出限领或保密规则”的肯定漏洞断言，本次按证据不支持计 FP，并未证明其业务行为在任何场景都不存在缺陷。

## 真实验证

批次 `e2-20260926-01` 在首个任务约 285.7 秒时中止，原因是用户提出调整裁定职责；随后用户明确保留现行职责。该批没有完成样本，不用于效果比较，取消路径没有导出账本，不能宣称其费用为零。中断记录单独保存。

按澄清后的范围运行 `e2-20260926-02`，原计划两组各 3 次。目标 49 个非 Git 文件与 E1 的归档逐字节相同；模型、预算、调度、答案和评分代码不变。

- default 三次已完成，耗时分别约 382.8、324.2、400.8 秒。
- default/run-001 已完成逐项复核：已知 P1–P6 全部1分，列表外有效发现2项、业务FP0，接口覆盖14/14。仅为一个样本，不推断整体改善。
- default/run-002 的复核草稿曾把普通超额退款误列为 new；根代理指出应归 P4，要求修正时子代理被模型服务以 possible cybersecurity risk 拦截。该草稿不能作为已验证评分使用。
- default/run-003 的复核也已停止，不将代理尚未完成的结论列为正式评分。
- 原文主脚本隔离 replay：default/run-001、002 通过；003 因固定 `/workspace/shared/repo` 路径不兼容新环境而失败。未编辑脚本、未在失败后换脚本，不宣称全部问题均可复现。
- 收到内容审核拦截后，按用户指令停止其他复核代理和剩余 live 批次，保留中断状态与原始产物，不改写措辞或更换代理继续被拦截的复核。停止时 single/run-001 已完成（约235.5秒，尚未复核），run-002 运行约21.7秒后中止，run-003 未开始。批次容器已清理；single 组未完成，不生成两组效果对比。

本轮因此不能得出提示词提升精确率、降低费用或多 Agent 优于 single 的结论。后续需要用户决定如何完成尚未验证的评测复核；已确认的工具反馈修复有独立回归测试支持。

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

- 真实 3+3 与全部复核未完成，不能把本任务标为全量验收通过。报告和未完成草稿保留，未合并 main、未删除工作树。
- 未做原调优分析暂称 E2 的“裁定节流”。本轮 E2 是可靠性修复批次；用户已确认当前裁定职责合理，不因高成本直接换架构。
- 参数失败反馈依赖已锁定 MAF 的参数异常链；用真实框架回归守护，其他内部异常仍不展示原始值。
- 提示词是行为指导，无法保证每次模型输出都正确。本轮同时修工具反馈与表述规则，3+3 同任务只能作探索性验证，不能证明因果或推广到其他任务。
- single 仍使用黑板与 Close，仅并发为1且关闭 derive；不是无黑板的普通单 Agent。

## 共享文件与提交划分

未改 contracts、Makefile、依赖、锁文件或 Compose 配置。

1. `Clarify fact confidence and finding review rules`：设计质量规则与本任务范围。
2. `Return actionable tool argument validation errors`：中间件和回归测试。
3. `Separate observations from confirmed findings in agent prompts`：两组提示词。
4. `Record reliability checks and corrected evaluation review`：复核说明、检查点和本报告。

后续仅在新证据支持时考虑调度改动；比较收益前应增加独立任务与重复次数。
