# Provider 402 并发误杀与续跑报告

日期：2026-10-17。任务：`0ef17097-b431-49cf-b431-46d94c67f2e6`。

## 根因

复现模型连接时，百智云对当前 `baizhi-2` 配置返回原始响应：

```json
{"error":{"message":"成员余额不足","type":"permission_error"}}
```

HTTP 状态为 402，request id 为 `IHtATHzzzHNtlhYbvJRFebhjsUrbKIsL`。这不是黑板、网络或执行环境失败。

原任务在 09:55 左右出现同一 402 爆发：agent-54 至 agent-63 都记录 `payment_required`。原实现把余额、鉴权等共享供应商错误当作单个 Agent 的永久错误，既不暂停新派发，也不合并同一故障窗口内的并发结果。前三个 402 结束事件因此直接触发 `failure_streak=3`；调度器在首个 402 后仍继续创建 agent-60 至 agent-63，其中四个 Agent 都是 0 步失败。任务在 Close 已提交 A1 `met` 裁定后、复核与终结报告完成前被标为 failed。

## 修复

- 模型错误元数据增加明确的 `scope=provider|request`。连接、超时、限流、服务端、冲突、鉴权和余额不足属于 provider scope；无效请求、内容审核和未知本地异常保持 request scope。
- provider scope 的 HTTP 错误会按下发模型键暂停新 Agent 派发；120 秒冷却期内不继续扩并发。冷却后仍可由一次真实请求重新探测，持续故障仍受原有三次保护阈值约束。
- 同一任务 120 秒锚定窗口内，并发 Agent 的同一类 provider 错误只增加一次 `failure_streak`。窗口不会因后续并发失败向后滑动。
- provider 错误不增加 Intent attempts，也不增加种子空产计数。普通本地 `runtime_error`、无效回执和 request scope 错误继续逐次计数。
- `agent.finished` 事件固化 `provider_model_error` 判定，使 Postgres 实时投影与 replay 一致；旧事件兼容为空。

本次没有新增依赖、配置项或数据库迁移；复用了已有模型恢复门、错误分类、120 秒窗口和 Intent 保护。

## 验证

- 定向测试：模型错误、恢复、调度执行器共 73 项通过；领域测试 98 项通过。
- 新增 provider 窗口 PG 集成测试通过，覆盖并发合并、种子不误罚和 replay 一致性。
- `make check`：2155 项通过，354 项集成/live 排除。
- `make test-integration`：350 项通过，2159 项排除，548.24 秒；没有调用真实模型或用户靶场。

## 部署

- 代码提交 `0348faf` 已合并并推送到 `main`。
- 公网服务器先备份数据库到 root 私有目录，再传输并校验 `bbx-blackboard:0348faf`、`bbx-agent-runtime:0348faf`；Postgres、MinIO 和其他项目容器没有重建。
- 服务器 release 切换到 `0348faf`，黑板块和 runtime 健康；五个修改模块的容器内 SHA-256 与本地提交逐项一致。`/login` 返回 200。
- 部署前目标任务的黑板、会话、工作区和 `70 CNY / 180 分钟 / 20 Agent` 预算未改变。

## 续跑

- 原失败任务通过原任务 ID 续跑为第 4 轮，追加预算和时长均为 0，原预算与累计用量保留。
- 百智云余额仍不足，因此只为该任务新建固定配置版本，将 Worker 模型切换到实测可用的官方 `deepseek-default`；目标、验收、Fact、Intent、Session 和 run-3 归档不变。
- 新 run 已恢复正常模型调用，没有再次出现 402；Close 已依据 F66 的原始 Jenkins/docker.sock 证据提交 A1 `met`，完成依据为 `inferred`，等待 Derive 完成前复核。
- 旧 Derive Session 约 54 万 token，跨供应商复核在本地转换阶段失败。为该任务创建 v7 配置，将既有 `context_threshold` 调为 120000，触发平台已有的 Derive 分段：完整旧 Session 仍保留，下一轮模型输入从新黑板分段读取，不删除或覆盖历史。

## 边界

- 本修复不会自动为供应商充值，也不会自动切换任务模型；本次模型切换是用户要求续跑后的任务级恢复操作。
- 120 秒是既有恢复窗口，本轮没有开放新配置。共享 402 持续超过三个窗口仍会使任务失败，保留防止无限运行的边界。
- 历史任务的既有失败事件不回写；修复只影响之后的模型错误和续跑轮次。
