# P4e：原生 Task 冻结交接与对账

## 完成内容

- 已签发原 Runner 凭据后，可信 Runtime 观察到原资源不明，或核对发现原 owner 过期、已撤权或无法匹配时，在 Task 锁下冻结。记录原控制器代次、Runner epoch、创建操作、原 Runner/Sandbox、PVC 和 Assignment 关联；撤销执行 owner 及已有租约，只有已证明未派出的调用记为未发送。已发送或结果不明的模型、工具、命令、输入和 Flag 保留原身份；不据资源重新显示就绪而解除冻结。原费用 key、累计、独立失败与预算暂停事实继续保留。
- 健康的控制器交接沿原 Runner epoch 与资源继续。新控制器可以通过现有 PostgreSQL 控制权接收已冻结任务，但仍只观察原资源、处理可信晚证及原有停止清理。冻结后不能重新发凭据、创建 Sandbox、激活或派工。通用 Runner owner 接口不能给原生任务创建、续租或撤换 owner；原生首个 owner 由已核对控制器代次的内部事务创建。跨 epoch 的原生绑定禁止 attach；无原生记录的既有合同保留。
- 用户通过 `GET /api/tasks/{task_id}/ctf/runtime/runner-bootstrap/recovery` 读取一致快照，包含原身份、最多 32 个原绑定及数据库确认游标，以及原模型尝试、传输、工具、命令、每条命令作用域、输入和任务级 Flag/费用状态计数。传输结束、已记录费用和命令作用域分别取证；计数不表示已确认执行退出。接口不返回 journal 正文、命令正文、调用内容或凭据，也不提供审查或处置。
- 迟到的可信证据仍归原记录，不能推进未经确认的 journal 游标或重放副作用。显式停止、删除和硬截止仍走原控制路径；只有原资源正证清理完成才显示 `stopped`，不以查询失败或缺少终止证明冒充已停止。

## 验证

- `.venv/bin/pytest services/blackboard/tests/test_native_runner_bootstrap_integration.py services/blackboard/tests/test_native_worker_dispatch_integration.py services/blackboard/tests/test_runner_control_integration.py -q`。其中 11 个原生 bootstrap/Worker 场景通过，另有 3 个旧 Runner 场景失败（总用时 44.10 秒）。真实 PostgreSQL 验证首次授权、健康交接、资源未知与 owner 过期冻结、冻结交接、原证据与其他 hold 保留、停止证据，以及相邻派工和原有 Runner 合同；网关和 Kubernetes 观察使用受控夹具。用独立 `git archive b220b74` 源目录和同一依赖重跑旧 Runner 文件，先核实模块实际从 `baseline-check/` 导入，3 个失败的用例、断言点与结果完全一致，属于既有失败；对照在 `.data/p4e-native-recovery-20261107/pg-baseline-comparison.json`。
- `.venv/bin/pytest services/agent-runtime/tests/test_native_runner_recovery.py services/agent-runtime/tests/test_native_runner_pause.py -q`。Runtime 定向三例通过（2.81 秒），验证恢复观察期间无挂载或新执行，停止后回到原清理路径。
- `make schemas openapi`、`pnpm types`、`pnpm exec tsc --noEmit` 已通过；新增两份共享 JSON Schema 并更新 OpenAPI 与 TypeScript。干净候选 `make check` 通过：2657 passed、477 deselected（201.27 秒）；ruff、pyright 与 Pi 检查通过。

## 偏差与待决

本片闭合的是冻结、控制权交接和原证据只读对账。控制器 generation 变化不等于创建新 Runner epoch；`not_frozen` 只表示没有冻结记录。游标来自数据库确认，未读取或验证 journal 字节，也未确认 PVC 的旧写入者退出。未发布新 Runner/Sandbox 重建、工作副本和跨 epoch 恢复执行，也未打开 helper。未调用真实模型、靶场或 Kubernetes；正式设计正本未改。

## 提交与下一步

`Freeze uncertain native Runner handoffs`：冻结与原证据投影、原生管理封口、Runtime 观察、必要验证和生成合同。由 GPT-6.1 Sol high 开发并独立复核；原 38 个 tracked 和 72 个 untracked 工作改动保留。

下一步先单独验证旧节点执行和卷写入者退出，再做新 epoch journal 工作副本的 CAS；helper 保持独立验证。
