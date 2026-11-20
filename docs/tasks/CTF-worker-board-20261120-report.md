# CTF Worker 池、题内黑板与直接执行报告

任务：`CTF-worker-board-20261120`。工作树：`/Users/yym/.codex/worktrees/platform-maintenance-release/platform-performance-audit`。分支：`feat/ctf-worker-board-20261120`。

## 完成范围

| 阶段 | 实现 |
| --- | --- |
| D0 | 与“多Agent CTF架构优化”同步并审定 Spec、开发计划与三份设计正本；设计提交 `6974f57`、`b65285d`、`4d72587`。后续删除 CTF 网络功能的指令覆盖原 VPN 保留要求。 |
| D1 | 同级 Worker 池；首次平台分配，之后通知自选；新题优先援助；持久化 `has_worked`；困难题初始两人、每题总计最多十人；每 Worker 并持三条 Intent。领取、援助、提交与版本检查在 Task 事务内完成。 |
| D2 | 稳定 Pi 会话跨题复用，动态 Assignment 绑定原模型请求；同题主责/援助平等提交待核验；Lead 驳回必须原子写原因 Fact；通过后按各 Worker 实际执行结束逐个释放。 |
| D3 | 持久事件通知、steer、同题事实与意图增量；空闲没有定时模型轮询；累计三十分钟计时；文件完整快照上传后原子登记 Artifact/Fact；新协作工具中删除子 helper。 |
| D4 | 工作台显示难度、主责/援助队伍、招募/解题/核验状态、Intent holder、事实和文件证据；保留成员执行日志和费用。删除 CTF VPN 上传、流量入口、网络状态和相关设置说明。 |
| D5 | CTF 使用 `ctf-direct-v1` 共享 Sandbox；跳过网络命名空间、抓包准备、网络许可/续租与网络释放等待。增加直接执行容器验收、并发/恢复/归档回归和本机组合镜像验收。 |

普通黑板模式继续使用其独立发布配置。CTF 与普通模式的发布样例已分离；CTF 使用 schema 4 执行验收。完整工具基底通过 `services/sandbox-execution/ctf-direct.Dockerfile` 派生，保留已有工具和附件能力。

## 验证

普通检查均使用假模型；实际模型验收单独执行，不属于 `make check`。

```sh
make check
make web-check
npm --prefix services/agent-runtime/task-runner test
uv run pytest services/blackboard/tests/test_worker_pool_integration.py services/blackboard/tests/test_worker_pool_lifecycle_integration.py services/blackboard/tests/test_peer_file_facts_integration.py services/blackboard/tests/test_native_ctf_publication_integration.py services/blackboard/tests/test_runner_control_integration.py -q
uv run pytest services/agent-runtime/tests/test_task_archive.py services/agent-runtime/tests/test_native_runner_recovery.py -q
uv run pytest services/sandbox-execution/tests/test_ctf_direct_integration.py -q
pnpm --dir web e2e native-ctf-create.spec.ts ctf-peer-board.spec.ts
pnpm --dir web e2e config-workbench.spec.ts -g 'CTF 只读'
uv run pytest services/blackboard/tests/test_peer_lead_evidence_integration.py -q
```

Docker 验收使用随机本机端口，结束后自动清理容器。直接执行测试需要先构建完整派生镜像；通过 `BBX_CTF_DIRECT_TEST_IMAGE` 选择已有镜像。Playwright 使用本机已有 Chromium 路径 `BBX_E2E_BROWSER`；无真实后端请求。

已完成检查：前端 165 项；Pi Runner 26 项通过、2 项原有可选测试跳过；PG 主定向组 22 项通过，额外恢复/原黑板/命令组 17 项通过；源码的创建及桌面/手机黑板 3 项 Playwright 通过，CTF 设置页回归 1 项通过；最终组合候选的上述 4 项全部通过，确认设置页没有 VPN、流量采集或 Helper 条目。Docker 实测确认 pwntools、本地命令、共享网络、二进制文件、后台进程收尾和另一 Worker 接续。

最终 `make check`：2331 passed、474 deselected，ruff/pyright/Pi 检查通过；最终 PG 复核 16 项通过。归档与控制器定向检查 37 项通过（原 35 项加两项原清理配置/释放名额回归）。最后一轮 Lead 证据与 Worker 生命周期 PG 回归：13 项通过（含 2 项新接缝测试）。真实 Pi 控制/就绪事件 PG 复核：4 项通过。

实际本地模型验收任务 `a519d455-7cdb-41c3-8546-d34a4cceadee`：两题均 `completed`，总任务自然结束；第一题实际走过驳回、原因 Fact、原团队继续和再次通过；第二题由已工作过的 member-2 自主认领，同一 binding 和 Pi session 贯穿两题；最终所有成员 idle，五条实际命令均完成，无 UNKNOWN 模型调用。最终通过的一次费用 USD 0.95669；含此前诊断模型调用合计 USD 1.371976。任务归档保存成功，`cleanup_ready=true`、资源 `retired`；退役后仍可回读三个成员的原生日志，抽查两份文件下载的大小与 SHA-256 均一致。

单次样本：首题发布至平台分配约 0.90 秒；第二题发布至 Worker 自选领取约 6.45 秒，至其首条命令约 12.80 秒；冷任务创建至首条 Worker 命令约 79.73 秒，包含容器、SDK 和 Lead 模型建题。容器创建观测约 5 秒。实际 Pod 仅有 Sandbox、Runner 与 Sandbox host，没有 VPN 容器、抓包进程或网络 scope。上述不是并发压力测试或受控前后性能对比。

## 修复的运行问题

1. 原首轮实测的主 Worker 卡在旧网络准备链。本次按用户新指令移除该链，没有继续增加网络重试机制。
2. 文件快照的 `evidence/<task>/member-N/<hash>.bin` 与旧归档文件名规则不兼容，造成已停止任务归档报错并带动 Runtime 重启。已支持不可变哈希快照和 Worker 路径，同时隔离单任务数据错误，保留原错误数据用于处理。
3. 发布切换后，已停止任务必须用原冻结环境完成归档清理；不能占用当前解题名额反复等待新配置匹配。仅对已确认退出的任务提供原清理配置；运行中任务不切换归属或执行身份。
4. OpenSandbox 以加载脚本的方式执行初始化，直接 `exit 0` 会结束主进程。改为条件跳过网络初始化，并使用同样加载方式验证完整工具、文件与命令收尾。
5. Lead 的 Runtime 命令证据此前未纳入黑板事实引用，已按原命令与当前执行身份接通；无效文件 Fact 参数返回持久工具错误，模型可纠正参数继续，不冻结会话。提示词明确可直接引用既有 Fact 核验，并在等待同伴时结束回合，避免 sleep 和重复环境探测。
6. 本机旧 Pod 镜像配置未包含新 direct Sandbox 摘要，首次 direct 部署验收在创建 Pod 前被拒绝，未调用模型。已补充执行 Pod 与归档 reader 的实际镜像摘要，并在部署文档写明同步要求。

## 偏差与待决

没有新增需要用户决定的业务规则。部署为本机 CTF publication 22、普通模式 publication 16。两次在模型启动前失败的合成记录已停止，保留原始资源状态用于诊断，不强行清空 UNKNOWN；它们不计入成功验收。

保留历史任务记录、旧 helper 回读和普通模式依赖的公共代码；新 CTF 不发布或启动这些已删除的能力。

本次不以简单算术验收推断真实 CTF 比赛成功率或 p95。真实题目难度、模型延迟和工具耗时仍需比赛样本测量。

## 提交与共享文件

设计已单独提交。实现涉及合同、服务、Runner、事件、UI 和执行层，作为一个完整实现提交，避免中间提交暴露不完整的调用合同：

`feat: implement peer CTF workers and direct runtime execution`

共享文件改动为 `packages/contracts` 中协作类型、模型请求来源、角色工具、生成 JSON Schema 与前端 OpenAPI 类型；未修改依赖、锁文件、宿主机全局设置或 `.env`。本机只更新本平台镜像、发布配置及对应镜像登记。不合并 `main`，不推送远端。

## 后续建议

冷启动实测说明耗时仍包含 SDK 准备及模型轮次，不能仅用 Sandbox 的约 5 秒创建时间代表开始解题时间。实测后已补上 Lead 准备完成的持久就绪事件，立即唤醒控制器，避免等待周期续期才激活；真实 Pi/PG 回归确认事件与准备状态一同持久化，但未为这一单独优化另做付费耗时测量。后续重点减少不必要的重复模型读取。

在当前版本完成一批真实比赛题，分别记录冷启动、平台事件到下一次模型发送、模型延迟、工具时间、暖会话换题时间；再据瓶颈决定下一轮优化。
