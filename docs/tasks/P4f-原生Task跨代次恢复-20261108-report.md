# P4f：原生 Task 跨代次恢复

## 完成内容

- 用户读取 `GET /api/tasks/{task_id}/ctf/runtime/runner-bootstrap/recovery` 获取冻结 incident，再对同一路径 `POST` 提交 `request_id`、`incident_id` 和可选 `instruction`。该请求只授权恢复当前 incident；重试同一请求返回原受理回执。停止、删除、原硬截止及独立预算、故障或人工暂停继续优先。旧执行凭据、租约及重放通道保持关闭。
- 在支持恢复写锁的单节点部署上，可信 Runtime 核对原 Runner、Sandbox host 与 Sandbox 的 Pod UID 和容器 ID，等原进程正证退出。保留两个 PVC，并核对原本地 PV 与 Node UID、同一个文件写锁和卷消费者。单次查询缺失、强删、资源替换和写锁不明都不构成退出或释放证据。日志核验 Pod 也只领取一次创建；原 Pod UID 和两个容器 ID 正证终止及副本回执落库后才移除。已领取而缺正证时，停止仍保持待确认。
- 原日志按数据库确认的 SDK ID、绑定、游标和哈希核验，文件末尾必须与最后确认点完全一致。核验只读原日志，把已确认字节复制到新执行代次的独立历史目录；新 Pi SDK 使用新绑定与空白活动会话，不加载旧输入、命令或平台控制条目。未知日志尾、模型运输或费用、工具、命令、输入与 Flag 仍阻止恢复。原 Sandbox 正证退出可以独立证明原执行作用域已无进程，但不会改写原命令的作用域记录，也不能替代命令结果。
- 代次切换在 Task 锁下核对 incident、原退出与历史工作副本证明，再以独立创建 ID 获取一个新的 Sandbox，并使用新 Runner/host 身份和新 owner epoch。原任务、Namespace、工作卷、预算 key、已确认累计及截止时间保留。旧 MCP 路由只领取一次撤销，确认原配置和运行路由均消失后才切新路由；丢回执只读回。新会话确认 journal 后再核原预算累计，之后才发新的继续输入及续派；原记录仍可只读查询。删除回收只额外接受引用已正证退出的旧资源 UID 的 Kubernetes Event；实际旧执行对象或外部 UID 仍阻断。

## 验证

- 干净候选 PostgreSQL：`.venv/bin/pytest services/blackboard/tests/test_native_recovery_integration.py -q`，1 passed（8.40 秒）。覆盖用户意图和幂等、原副作用阻断、旧作用域身份不匹配、验证器单次创建与正证、预算累计回退、旧路由丢回执读回、新 owner/SDK/input、停止待确认与最终停止。加上相邻 bootstrap、Worker、MCP 路由的较早候选定向共 14 例通过（43.27 秒），未重跑既有失败矩阵。
- 回收元数据定向：`.venv/bin/pytest services/agent-runtime/tests/test_runner_resources.py -q -k retirement_accepts_only_events`。只接受正证退出的旧 Runner/Sandbox 和核验器 UID 的 Event；拒绝外部 Event 与仍出现在清单里的旧 Pod/CR；Runtime 定向回归共 15 例通过（2.85 秒）。
- `node --test services/agent-runtime/task-runner/test/recovery-journal.test.mjs`，1 passed。校验原字节与工作副本一致，拒绝未知尾、错误 SDK 身份及不可信路径。
- `make schemas openapi`、`pnpm --dir web types`、`pnpm --dir web exec tsc --noEmit` 和 OpenAPI 快照核对均通过。干净候选 `make check` 通过：2658 passed、478 deselected（180.40 秒）；ruff、pyright 和 Pi 检查通过。
- 真实本地 Kubernetes：按 [P4f 探针说明](../../deploy/kubernetes/p4f/README.md) 启动独立 kind 集群，使用固定 Agentgateway、官方 OpenSandbox 和脚本化假模型，执行 `deploy/kubernetes/p4f/probe.py --candidate <clean-candidate>`，exit 0。原活跃写入者拒绝竞争锁；同 Task 从 epoch 1 到 2，有新的 Runner、SDK 和 Sandbox，原 journal 核验副本及工作卷保留。新 Sandbox 通过真实工具路由执行 `exec_command`，读回旧工作卷标记；两代各只有一次命令和一次答案模型调用。原 key、阈值和硬截止不变，累计从 0.0012 到 0.0024 USD；新代次正常停止并保留 PVC。脱敏证据：`.data/p4f-native-recovery-20261108/k8s/joint/evidence.json`。之后只补了删除回收的旧 Event 元数据收口，按上面的定向用例和最终 `make check` 核对，未重跑 Kubernetes 链路。

## 偏差与待决

首版只接受启动时冻结启用写锁、原 Node 与 local-path PV 都可核验的单节点执行；确认日志必须正好到文件末尾，原 epoch 全部绑定（含历史绑定）目前最多 32 个，且不得存在独立 hold 或未决账。此前未启用写锁的原生 Task 仍能冻结、对账、停止，不能据现有证据接管写入。没有将已确认历史字节当作新 SDK 会话状态，也没有恢复旧进程内存。对未确认副作用、未知日志尾、分区节点或其他存储不推断结果。命令记录仍为只读，不增加审查或处置。未涉及真实模型或靶场、helper、多副本和正式设计正本。

## 提交与下一步

`Recover frozen native Tasks in a new execution epoch`：恢复意图与原证据门槛、原生路由交接、新 Runner/Sandbox 及 Pi 日志接缝、生成合同、必要测试与报告。GPT-6.1 Sol high 开发并由独立 Agent 复核。原 38 个 tracked 和 72 个 untracked 工作改动保留。

下一步按独立接缝验证 helper 继承、借位与取消，再进入 P4 席位并发验证。跨节点存储恢复须先得到独立的旧写入者退出与卷隔离证据。
