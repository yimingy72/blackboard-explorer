# Provider-failure-coalescing · 模型瞬时故障合并报告

日期：2026-09-29。问题样本：任务 `ae3919e0-ffca-47d0-954f-3dccb3a5c5f6`。

## 诊断结论

该任务运行 15 分 35 秒，失败时估算费用仅 ¥1.51921980 / ¥15，远未达到 60 分钟或预算限制。四个 Explore Agent 在 13 秒内结束，持久化错误均为 `connection`；每个请求已经执行最多 5 次 SDK 尝试，耗时分别约 35.3、39.3、41.3、44.5 秒，没有 HTTP 状态、供应商码或请求 ID。第三个结束时旧 failure streak 达到 3，任务立即 `failed`，第四个随后结束。

失败后代理 TCP、经代理访问 DeepSeek HTTPS、账户余额接口均恢复正常且余额可用，因此这是短时共享连接链路故障。上轮的 NUL Session 修复、模型错误分类和 trace 均正常工作；本次暴露的是任务级计数仍按 Agent 数放大同一故障。

## 完成内容

1. 模型错误回执增加安全的 `error` 元数据；connection、timeout、rate_limit、conflict、server_error 标记为 transient，其余模型错误及本地 runtime_error 不标记。
2. 同任务 120 秒锚定窗口内的 transient 模型错误只增加一次 failure streak。窗口不会被后续失败向后延长；持续故障跨三个窗口仍以“模型服务连续不可用”终止。
3. transient 模型错误不增加 Intent attempts，不增加种子空产计数。永久错误、旧格式回执和其他 runtime_error 保持逐次计数。
4. 正常 Agent 结束清空 streak 和窗口；续跑、replay 同样清空。任务终态后在途 Agent 的后续正常或失败交接只更新 Agent，不再覆盖触发终止时的任务失败计数。
5. 新增 schema 0011，保存 failure_window_kind 与 failure_window_started_at；agent.finished 事件固化 failure_increment、窗口起点和 transient 判定，保证重启 replay 与原运行一致。
6. closing 阶段的 final Close 重试改为“非 runtime 失败数 + 已合并的任务 failure streak”，同窗 transient 错误不会绕过合并规则，永久失败仍保持三次上限。

## 实际验证

- 领域与 Runner 定向测试：121 项通过。
- PostgreSQL store 集成：33 项通过，覆盖旧事件兼容、窗口投影、种子门控、正常重置、终态冻结、续跑和 replay。
- 真实调度器/黑板脚本化并发场景：4 个并行 transient connection 错误全部结束后，任务仍为 running、failure streak=1、所有 Intent attempts=0，并记录 4 条 model_error trace。
- `make check`：ruff、pyright 与 580 项普通测试通过；不调用真实模型或目标。
- `make test-integration`：65 项通过，包含上述并发场景以及既有完整闭环、完成复核、归档恢复、Session 和调度恢复。

## 偏差与边界

- 窗口固定 120 秒。若实际故障持续，约在第 1、121、241 秒形成三个独立窗口并终止，不会无限创建 Agent。
- 任一正常 Agent 完成会清空窗口，这表示任务仍可取得模型响应；它不改变单个 Intent 的语义结果。
- 本轮不修改 SDK 重试次数、默认并发、提示词、预算或领域策略，也不自动续跑失败任务。

## 本地部署

- 已部署 blackboard/runtime 与 schema0011；两容器健康、重启次数均为 0。部署前备份位于 `.data/backups/pre-provider-failure-coalescing.dump`。
- 生产烟测直接通过领域/数据库接口提交 4 个并行 transient connection 结束事件：任务保持非终态、failure streak=1、窗口种类为 model_transient。临时任务及对象随后完整清理。
- 失败任务 `ae3919e0-ffca-47d0-954f-3dccb3a5c5f6` 的 15 条 Fact、14 条 Intent、17 个 Agent、Session 和归档保持不变；没有自动续跑或模型调用。

## 提交划分

设计合同；Runner 结构化 transient 回执；领域规则与数据库迁移/投影；集成场景、报告和部署记录。部署前备份数据库，升级 schema0011 后进行无模型 HTTP/DB 烟测。
