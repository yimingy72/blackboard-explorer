# CTF Worker 池与题内黑板开发计划

任务编号：`CTF-worker-board-20261120`。正式合同：[Spec](CTF-worker-board-20261120-spec.md)。基线 `7831ca3`，开发工作树为 `/Users/yym/.codex/worktrees/platform-maintenance-release/platform-performance-audit`，保留其他工作树未提交内容。

## 实施顺序

| 阶段 | 完整交付 | 主要文件范围 |
| --- | --- | --- |
| D0 | 与指定会话审定 Spec/验收矩阵，更新三份正本，设计单独提交 | `docs/design/` 三份正本、本 Spec/计划 |
| D1 | 新版本字段/工具/角色合同，题目与同级成员、首次分配/自选/援助认领、10 人/3 Intent、版本与幂等 | `packages/contracts`；`services/blackboard` 的 challenge/task board、business tools、原生派工、事件/投影、相关测试 |
| D2 | 稳定 Worker 会话，空闲选择与动态 Assignment 调用归属，steer 投递、核验 Fact 原子写、执行自然结束后逐个释放 | `services/agent-runtime/task-runner`；Blackboard 的 runner control、准入/Sandbox、bootstrap、flag board 与恢复；假 Pi/Runner 测试 |
| D3 | 实时事件与通知合并、30 分钟累计定时/暂停恢复、文件快照证据、提示词、删除新任务 helper 入口 | Runtime 原生控制循环；Blackboard 原生资源/消息/证据；角色配置、发布脚本和相关契约 |
| D4 | 工作台完整展示与操作回读：难度、队伍、待领取/招募/核验、Intent holder/最多 3 条、事实/证据路径/作者、日志与费用 | `web/src/ctf`、生成契约、相关设置/创建界面与前端测试 |
| D5 | 合成串联、竞争/恢复/性能测量，修复回归，报告与本机候选部署验证 | 定向 PG/Runner/前端 E2E；任务报告、必要部署脚本 |

D1–D4 是交付切片，不是只做第一片便停止。契约可先提交，未完成能力不提前发布到正式新任务。优先复用现有锁、事件表、消息队列、runner journal、对象存储，不引入新调度服务或依赖。

## 必须覆盖的验收

| 编号 | 场景与判定 |
| --- | --- |
| A01 | 简单/中等且未开放援助：20 初始 Worker/10 新题为 10 主责、其余待命；30 新题为 20 主责、10 待领；困难题另按两人目标验收 |
| A02 | 首次直接分配援助也置 has_worked；完成后只通知自选，不再直接派题；恢复不重置首次标记 |
| A03 | 已工作空闲者收到全体可领摘要；有新题时不能先领援助；并发失败返回最新集合，当前回合重新选择 |
| A04 | 首次派工、主责认领、援助认领并发：主责唯一、每人一个题、每题最多 10 人，幂等重试不增人数 |
| A05 | 困难初始 1+1，主责无需等援助才求解，第二人到达后停止招募；再申请新增目标，重复请求不重复累计 |
| A06 | 每人同时 3 条 Intent，4 条拒绝且不污染状态；同 Intent 只能一人；援助能提出/认领/关闭自己的方向，普通模式语义不变 |
| A07 | 黑板可见性：未领/开放援助/自己的题可读；他人未开放题不可读；Lead 无 Worker 管理工具，Worker 无设置成员状态/退题工具 |
| A08 | 主责或援助提交均进入待核验、全员和 Lead 得到通知；不等长工具/ACK；多候选并发保留，待核验不加人 |
| A09 | 驳回缺原因 Fact 拒绝；有原因则 Fact 与状态原子提交；原团队收到 steer 并可读反馈；恢复既有招募缺口 |
| A10 | Lead 通过后各成员实际执行结束才逐个 idle；已结束者先复用；旧事件/旧核验/旧调用不改变新题归属 |
| A11 | 同一 Pi session 跨两题和空闲选择，日志/工具经验保留，正常换题不主动重连 MCP；断线重连保留归属、不重放；每条命令/证据仍归当时 Assignment |
| A12 | 30 分钟累计包含招援，排除待领/核验/暂停；驳回续算；无实际援助开至少一名额，有援助只提醒；重启不丢、不重复触发 |
| A13 | 消费者提交后崩溃、断线补读、重复/乱序事件与迟到输入：状态不回退、不双分配、不重复核验；待命没有模型定时轮询 |
| A14 | Fact/Intent 同题同步，未消费更新合并；读取快照与增量版本连续；不把每条事实都送成新模型回合 |
| A15 | 文件登记哈希/大小/内容一致，上传失败无 Fact，锁外 I/O 后状态改变要重核；同名修改不改变旧引用；执行时版本与事后快照清楚区分 |
| A16 | 删除 helper 新建工具/界面；旧记录可读和必要清理保留；普通 derive、原有权限/日志/费用/VPN/输入功能不回退 |
| A17 | 新旧 manifest 冻结、旧任务回读、暂停/恢复与 epoch 接续，不把故障或未知执行当 idle；有题有可用空闲人不会因通知流程永久卡住 |
| A18 | 前端显示主/援队伍、状态、难度、招募缺口、计时、Intent holder、Fact/反馈/证据；详情日志与费用实时更新 |

## 验证与记录

- 基线及每次提交前：`make check`，无真实模型；输出保存到 `.data/ctf-worker-board-*`。
- 领域规则/权限/并发/事件用现有 pytest 和 testcontainers PostgreSQL，随机主机端口；不使用 `make up`。
- Runner/SDK 控制用本仓库 Node 测试和脚本化假模型，核实实际 journal、steer 与自然结束语义。
- A10–A11 至少一组使用真实 Pi 1.5.0 生命周期、MCP transport 和脚本化 provider/工具：覆盖同响应 claim+执行、旧调用延迟到新题、旧 steer 排队时释放、claim 提交后响应丢失、重复激活无第二 SDK run。
- A03–A04 覆盖新题发布与援助认领竞争；A08–A10 覆盖同轮追加候选与过期驳回/通过竞争；A12–A13 覆盖旧 deadline 不消耗有效触发、事务提交顺序与恢复；A17 覆盖集合不变但未领工作的结束事件重提醒。
- 前端运行 `make web-check` 和受影响的 Playwright 合成流程。全量 E2E 的已有过时用例须明确处理或单列，不能宣称未跑的测试通过。
- 性能报告分别列事件通知/准备/模型发送时间和模型调用数；冷启动与暖会话分开。无真实比赛测试则不宣称解题成功率或 p95 提升。
- 本机候选保留当前完整镜像功能，合成验证后切换；真实模型不属于普通检查，不用运行中的用户任务做实验。

## 提交划分

1. `docs: specify CTF worker pool and challenge collaboration`：三份正本、Spec/计划；检查基线通过。
2. `feat: add CTF worker claims and peer assistance`：D1 及必要契约/服务测试。
3. `feat: retain native worker sessions across challenge work`：D2 与归属/核验/恢复测试。
4. `feat: deliver event-driven challenge updates and evidence`：D3 与计时/文件/通知测试。
5. `feat: expose collaborative challenge workbench`：D4、串联修复、D5 报告。

实际切片以可编译、检查通过为准，关联变更不能为了划分而留下可发布的断链。提交正文末尾使用 `Implemented by Codex (gpt-6-sol) for task CTF-worker-board-20261120.`。不合并 main，不推送远端。

## 协作审核记录

2026-11-20 双方此前审核已确认业务闭合：10 人包含主责、3 条 Intent 是并持上限、新题组内自选、援助目标人数、30 分钟按实际成员双分支；旧 50 人和旧 helper 均不继续。依据用户最新开发授权形成此开发基线，先实施这些已确定的契约和事务。

已向[多Agent CTF架构优化](https://chatgpt.com/c/6affc0e7-5164-83ee-8d97-04df11d9c226)同步 Spec/计划全文。终审确认业务无新冲突、长期会话可实施；要求补三处执行合同，现已写入 Spec 第 5/7/9 节及 A03–A17：模型响应到工具的来源快照；自然结束、旧排队通知和 claim 丢回执/激活互等；长期 MCP 的稳定身份与动态调用范围。另明确新题发布也持 Task 锁、核验绑定候选与状态版本、旧 deadline 不消耗触发、提交顺序安全水位、未领工作结束后的重提醒。A01 限定普通难度，A10–A11 加真实 SDK+假 provider 接缝验证。没有需要用户重新决定的业务点。

基线 `make check`：ruff/pyright/Pi 检查通过，pytest 2316 passed、455 deselected（186.87 秒）。Spec/计划本地链接和 `git diff --check` 通过；没有调用真实模型。
