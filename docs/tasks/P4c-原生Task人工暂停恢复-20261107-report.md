# P4c：原生 Task 人工暂停与恢复

## 完成内容

已接入同原 Task 的用户暂停与显式恢复，适用于已经运行的原生自动派工 Task。入口为 `/api/tasks/{task_id}/ctf/runtime/runner-bootstrap/pause`（POST 请求、GET 状态）和同前缀 `/resume`（POST，携带本轮 `pause_id`）。仅用户身份可调用；返回人工操作状态、操作 ID 与必要阻断原因，不返回 Runner 资源配置或密钥。人工操作状态不等于全平台运行状态；预算或故障暂停会在阻断原因中显示，停止中的 Task 单独报告 stopping。

人工暂停在 Task 锁内冻结原绑定、Assignment 和执行身份，拒绝新模型、工具、输入、派工与 Flag 外发。原 Runner 只接收关闭控制；原未发送调用和 Flag 准备记录保留原身份并取消。已经准入的执行可能迟到，仍写入原调用。仅本轮捕获、同原 epoch 和 Pod、已确认原 SDK 的 closing 会话，可在停止或其他 hold 前沿现 CAS 确认原 journal 尾；该通道不恢复执行许可。原 close、已确认 journal、原 Sandbox 作用域排空及原输入/执行/Flag/费用均确认后，才报 paused。已获取 prepare 许可但首条 SDK journal 未确认时保留等待，不能冒充从未启动。

恢复要求同一轮暂停、同原 Runner/Sandbox 身份、预算来源和稳定原生 key；可信读回需低于原阈值且不低于已确认累计或持久高水。原预算和累计不重置，成功读回更新已有高水快照；不加额度、不延硬截止、不清现有预算或故障 hold。旧未确认输入保持冻结；旧已应用输入、旧会话和原 journal 留史，旧未发送的排队输入延期保留，不自动投入新会话。

新 Lead 使用新 turn、binding、SDK 和独立短凭据引用。新会话收到明确的新继续输入，包含原目标、域上下文、完成要求及原授权附件元信息；默认指令来自系统，也可由用户明确提供新正文。整个新输入受现有大小与编码界限，超限拒绝并回滚；不截掉原任务意图。附件元信息不表示文件已送进 Sandbox 或恢复了内容访问。原命令结果仅以同角色、同原范围的有界只读摘录提示，不作指令或重播工具。新 Lead 确认准备后才放行新输入和续派；原已确认的未完成题按原优先级或已保存的新范围就绪进入队列，新 Worker Assignment 关联原历史，不改写旧记录。停止、删除和硬截止优先。

## 验证

- 干净候选真实 PostgreSQL：` .venv/bin/pytest services/blackboard/tests/test_native_worker_dispatch_integration.py -q -k manual_pause`，1 passed / 2 deselected，6.30 秒。经实际用户 API、Runner journal/close 和 Host 迟回验证人工暂停、旧未知阻断、原累计回退拒绝、原 key/阈值和新高水、原任务元信息、新 Lead 与同题新 Worker、仅新输入及停止优先。外部预算回读、旧 Sandbox 排空和 SDK 观察在该合同中为显式夹具。
- 同文件 `-k 'not manual_pause'`：2 passed / 1 deselected，8.54 秒；覆盖直接受共有关闭证明与 Assignment 优先级变动影响的原撤回/范围切换合同。
- 定向 Runtime `.venv/bin/pytest services/agent-runtime/tests/test_native_runner_pause.py -q`，1 passed，0.81 秒；真实 Pi `node --test --test-name-pattern="a resumed Lead" services/agent-runtime/task-runner/test/runner.test.mjs`，1 passed，原用例 0.70 秒。验证同原 fence 原 journal 尾关闭后保留、新 binding/SDK 和新凭据引用、旧输入及工具不重播。Pi 为本地 SDK 配受控服务夹具，未冒称本轮完整原生集群实测；P4a 的真实 Runner/Sandbox 链路仍是复用证据。
- 新增 3 个 JSON Schema，生成 OpenAPI 与 TypeScript，`pnpm exec tsc --noEmit` 通过。首次 `make check` 只停在一处格式，修正未改行为；最终干净候选 `make check` exit 0，2654 passed / 472 deselected，200.88 秒，ruff、pyright 和 Pi 检查均通过。

## 边界

本片是同原 epoch 的人工暂停和新会话继续。原 journal 尾、输入、远端执行、Flag 或费用不明，继续保守冻结；不重开旧绑定，不接管旧模型内存，不做跨 epoch 接管或预算增额。旧记录保留，命令界面仍只读，不新增审查或处置。本轮不调用真实模型或靶场，不重跑 Kubernetes 或旧矩阵。

本片按用户指定由 GPT-6.1 Sol high 开发并独立审核。正式设计正本不改，原 38 个 tracked 改动和 72 个 untracked 工作文件保持。

## 提交划分

`Pause and continue native Tasks under the original fence`：用户人工操作、旧执行确认、新会话和原预算读回、必要验证及生成合同。

下一步建议：先对原生预算 key 的阈值追加及累计保留做独立可确认接缝，再决定预算暂停恢复；跨 epoch 未知日志与 helper 分片验证。
