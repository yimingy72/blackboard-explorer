# P4d：原生 Task 预算追加与恢复

## 完成内容

- 新发生、原因明确的 `budget_exceeded` 持久冻结派工和执行许可。若当下能确认原执行身份，沿 P4c 同事务记录原 Runner/Sandbox、原绑定、原 Worker 任务和原预算身份，并关闭原会话。原 SDK 只可确认本轮捕获会话的 journal 尾，不能取得新执行许可。原输入、工具、Flag、费用和执行证据须分别确认；已知费用不能证明模型响应已结束，已封闭命令作用域也不能替代未知命令结果。旧无来源的 hold、无法捕获原身份或后续独立故障继续冻结。
- 用户用 `GET /api/tasks/{task_id}/ctf/runtime/runner-bootstrap/pause` 获取本轮 `pause_id`。排空后，只有用户可向同前缀的 `POST /budget-increase` 提交 `request_id`、`pause_id` 和 `additional_cny`。按原冻结汇率、向下取 9 位 USD 小数计算新总阈值，拒绝不可表示的金额或不能覆盖原累计的额度。原 key 名称、UUID、指纹、来源、无窗口 Block 策略、已确认累计和硬截止保留。持久配置与运行中阈值均读回确认，实际累计不得低于写前观察、持久高水或已确认明细。
- 每轮只受理一笔已确认的追加。先持久登记固定目标，再允许一次原 UUID 的 PUT；同一请求回执丢失后只读回，不重发。读到旧值不证明写入不会迟到。追加确认后任务仍暂停；用户明确调用同前缀 `POST /resume`，再次验证原执行和原生预算后，才清本轮纯预算 hold 及其专属伴随标记，用新 Lead 绑定、短凭据和新继续输入恢复。停止、删除和硬截止优先。若停止先到，保留追加事实，已完成清理的删除流程可核对并删除原 UUID 的精确旧值或已登记的新值。

## 验证

- [固定 Agentgateway 1.7.0 探针](../../deploy/agentgateway/p4d/README.md)：`.venv/bin/python deploy/agentgateway/p4d/probe.py`。单活、hybrid SQLite、假模型；原阈值 0.005 USD、原累计 0.006 USD，下一调用被拦；增至 0.008 USD 且回包被丢弃后，仅一次 PUT，原累计和 key 身份保留；同 key 再扣 0.003 后累计 0.009 USD，下一调用再次被拦。已刷库后重启仍为 0.009 USD。证据 `.data/p4d-native-budget-probe/bbx-p4d-budget-6d0076e2.json`。
- 干净候选运行 `.venv/bin/pytest services/blackboard/tests/test_native_worker_dispatch_integration.py services/blackboard/tests/test_native_fees_integration.py -q`，9 passed（37.72 秒）。覆盖真实 PostgreSQL 下的纯预算触发、原 journal/close、未知传输、回执丢失、原累计回退、追加与继续分离、独立 hold、停止优先、已登记目标的原 UUID 回收，以及既有真实网关费用和 P4c 场景。新预算 API 场景的网关管理和旧执行观察为受控夹具，原生原地增额由上面的独立真实探针验证。
- `make schemas openapi`、`pnpm types` 和 `pnpm exec tsc --noEmit` 已通过，生成两个共享 JSON Schema、OpenAPI 和 TypeScript 合同。提交前干净候选 `make check` 通过：2655 passed、475 deselected（202.37 秒），ruff、pyright 与 Pi 检查通过。

## 偏差与待决

保守保留不明意图：进程若在写意图落库后、请求发出前中断，也可能一直等读回；旧阈值不足以安全解开等待。同一轮不自动提交另一笔，也不创建替代 key。已刷库后的重启证据不覆盖 P3c 已实测的未落盘 counter 回退窗口。当前使用固定版本单活合同；未声称验证网关多副本、网关自身 PostgreSQL 存储或完整 Kubernetes 恢复。命令记录仍为只读，不新增审查或处置。

## 提交与下一步

`Extend and resume native Task budgets in place`：用户增额与明确恢复、预算归集和原执行证据保护、生成合同、必要验证与固定版本复跑探针。本片由 GPT-6.1 Sol high 开发并独立复核。正式设计正本未改。原 38 个 tracked 和 72 个 untracked 工作改动保留。

下一步将跨 epoch 的旧日志与副作用对账、helper 执行分片分开验证；继续沿原 key 与累计，单独补各自的证据闸门。
