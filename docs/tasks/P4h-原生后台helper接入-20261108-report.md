# P4h：原生后台 helper 接入

## 完成内容

- 新原生 Task 可在初次可信 launch 独立启用后台 helper，支持只开启后台、不开前台。当前主 Worker 在同 Task 锁下与独立主工作共用席位；主工作优先，每父最多一个未清子，达 P4f 绑定边界时拒绝新增 helper。父工具的“已受理”只保存一次；子结果、终止和投递分开保留，原回执不改写。
- 同一 TaskRunner 监督实际的 Pi 插件子会话。父普通模型回合结束后，仍有效的原 Assignment 下子可以继续；原父本地冻结、scope 变化、换题、取消、暂停或停止会撤销新准入。后台子期限最多 180 秒，且不超过原 Task 硬截止；与父工具 60 秒等待分开。子持有独立 binding、SDK、journal 和短凭据，沿原 30 秒运行许可续期，费用计入同一个 Task key。不启用 detached daemon，不另加长期凭据或模型循环。
- 原父可查询或请求取消当前子，取消受理不等于已经退出。成功和取消都要核原子终态、日志和 Sandbox 作用域；未知不重启、不换位。已知结果只给仍有效的原父 binding 排一条确定性 follow_up，并用原 journal 应用回执确认投递。旧父或 hold 下只留历史；已排但未确认的输入及后台子未决状态继续阻断需要它们的暂停/跨 epoch 恢复。命令记录保持只读。

## 验证

- 干净候选 PostgreSQL：`.venv/bin/pytest services/blackboard/tests/test_native_helpers_integration.py -q`，2 passed（15.32 秒）。覆盖前台旧 wire、主工作优先、后台受理前拒绝 acquire、父未决解除和原受理结果不变、显式取消、唯一原父输入及应用回执、冻结后迟到结果只存历史和后台未决门槛。
- `npm --prefix services/agent-runtime/task-runner test` 完整 Node 15/15；最终未知退出保留原运行及同一取消回执重读的定向检查 2/2，分列计数。真实父 Pi 模型可先结束普通回合；快速子结果在已有 stream 后按原 follow_up 到达，取消、未知及回执丢失不重复启动。
- 真实本地 Kubernetes 按 [P4h 探针说明](../../deploy/kubernetes/p4h/README.md) 运行，exit 0，6/6。仅启用后台的同一个 Task `fe47ce58-6d49-4038-8bde-2b852350dabb` 先结束原父普通回合，子仍运行并收到实际续期 revision 2，随后跨过最初的许可到期点。子用独立 SDK 和确认日志在原 Sandbox 以非 root 执行真实命令，费用归原 Task key；子确认退出且作用域封存后归还席位，只向原父投递一条确认结果。假模型调用数 Lead 3、Worker 3、helper 2。原 Runner、工具宿主和 Sandbox 正证退出并保留两卷。脱敏证据：`.data/p4h-native-background-helper-20261108/k8s/joint/evidence.json`。
- `make schemas openapi`、`pnpm --dir web types`、`pnpm --dir web exec tsc --noEmit` 及 OpenAPI 快照核对通过。干净候选 `make check`：2659 passed、480 deselected（217.59 秒，exit 0）；ruff、pyright 和 Pi 检查通过。

## 偏差与待决

本片是同 Runner 受监督异步，不开放上游 detached daemon、跨进程续跑、后台 Shell、递归、20 席位压力矩阵或逐 Agent 物理网络归属。helper 只继承已获准的 `read_file` 和前台 `exec_command`，不开放业务写入、Flag 或命令处置。只对新 Task 冻结启用；没有调用真实模型或靶场，正式设计正本未改。物理 Runner 使用已冻结镜像；最终源码补了无正证关闭时保留原运行索引的失败分支，已用上述定向 Node 检查，未把它写成该镜像的物理实证。

## 提交与下一步

`Run supervised native background helpers`：后台受理、受控子会话、原父结果投递与生命周期、生成合同和必要实证。GPT-6.1 Sol high 开发并经独立 Agent 审核。原 38 个 tracked、72 个 untracked 工作改动保留；仅本地提交，未推送。提交核对：`.data/p4h-native-background-helper-20261108/commit-verification.json`。本轮独立集群、数据库、测试网络、自建镜像和临时凭据已清理，保留实验证据；回执：`.data/p4h-native-background-helper-20261108/k8s/cleanup-result.json`。

下一步独立验证 20 席位并发和逐 Agent 物理网络归属；若未来需要跨 Runner 存活，再按专门证据门槛评估 daemon。
