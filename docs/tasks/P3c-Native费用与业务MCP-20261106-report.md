# P3c Native 费用与业务 MCP 接线报告

日期：2026-11-06。实际开发：GPT-6.1 Sol / max。

本批按已提交的 [费用阈值调整](P3c-Agentgateway费用阈值调整-20261106.md) 实施，替换尚未提交的 strict 模型草稿。V1 start/legacy claim 保护保持关闭，不代表已经可以发布真实跑题任务。

## 完成内容

1. 每个 Task 先在 PostgreSQL 加密持久化唯一原生 gateway key，再发布 `keyHash`、稳定名称、允许模型和无时间窗口的 USD Block 预算。所有角色与 summary 共用该 key。首次创建回包丢失后只读回原 hash/name/完整预算和原生 resource ID，不盲目再次创建；Runtime 重启不另发额度。
2. `native_admin.py` 集中访问原生资源、模型目录、预算 status 和过滤日志；Runtime 不维护第二个 admin 客户端。SDK 能力快照从 `/v1/models` 派生，角色的冻结 thinking/output 选择在该能力范围内校验，不把目录默认 off 当成唯一角色选择。
3. 模型调用在 `runner_calls` 中保留原 Task/binding/SDK/owner epoch、call/attempt、请求与原 body 摘要、journal 引用和一次 dispatch 证据，不保存整份模型 history。只有首次 CAS 私下返回 Task key；后续相同 attempt 不重新发送。保留独立 model/mcp JWT，服务权限不能从 Runner 凭据继承。
4. 删除本代理未提交草稿中的 model route contract、provider 配置解析、价格计算、预留和可信上游 capture；模型 provider/协议/SSE/usage/USD cost 由 agentgateway 1.7 执行。原普通任务的 CNY 引擎与实际调用路径未在本片盲删。
5. 新增原生费用事件表，以稳定 `accounting_source_id + native_log_id` 去重。该 source 是逻辑部署与持久数据库命名空间，不是 PodUID、进程启动 ID 或 Runtime epoch。按原 attempt 的七项可信属性查询完成日志，持久关联其原归属；完成游标不使用 startedAt 水位。金额由 PostgreSQL SUM 聚合，保持原生 USD，不二次 token 计价。
6. 预算 status 只用于阈值与高水位证据，不与逐请求费用相加。达到阈值后持久 hold，拒绝新模型、派工和输入；在途费用仍可归集并展示超额。原生计数重启回退不能降低平台已见高水位、已确认费用或 hold。FX 的来源、时点、汇率与阈值换算冻结，保留 USD 与换算 CNY。
7. 缺 usage、cost、catalog 证据或日志的请求进入待核，不能以原生 `cost=0` 或 SDK 自报 completed 补零。已见费用未知的记录降频核对；没有 transport 观察也会按冻结发送截止和有界等待进入 missing hold。后台只轮转活跃或到期未核的小批，不持续搬运全部历史；源失败和并发 Task 删除不会终止整个 consumer。
8. 区分模型传输结束与费用完成。SDK 终态观察先持久，再在事务外最多等三秒确认可信 transport，不等待费用消费。`response_ended` 后允许正常工具和下一模型；`disconnected` 保持未决。只有可信 Runtime 在实际发送前报告的 `not_sent` 可结束为明确未发送，不造零费用行，不重置 CAS；已有 native pending/known 记录或 prior sent transport 时拒绝降级。
9. hold 后仍允许原 owner 的有限 close/abort/shutdown 控制和控制续期，poll 不返回 prepare/input 或 Session 许可；模型与领域写仍核 Task、当前 Assignment/turn、owner 与许可。关闭证据不释放 Worker 席位，不冒称远端进程已清理。
10. 业务 MCP 使用官方 Python Server 作薄 host，与 REST 共用业务处理器，真实写事务内再次核 runtime fence。保留稳定 invocation、受保护 args/result、持久结果先于回复、unknown 不重派和旧 epoch completed 的有限读取。Worker 的题目列表额外限定当前 binding.challenge_id，公告仍在 Task 内共享；Task finish 后仍能取原保存结果。
11. 迁移 0024 增加调用关联、Task key 和原生费用三张表，不迁移旧模型账本。归档保留安全费用/FX投影，不导出 key 密文；删除要求先确认 native key 退休。在途/未知创建、归属漂移、不同 source 或未决调用均拒绝丢弃唯一密文；retire 的 source 核验先于任何 admin HTTP，纯历史费用读取不依赖当前 gateway。

## 验证

- 真实 PostgreSQL + 固定 agentgateway 1.7.0 + 假 provider 的四组合并场景：`4 passed in 25.56s`。没有调用真实模型、靶场或当前部署。
- 同一真实 Node/Pi Runner 先输出只读工具，直连本批官方 BusinessMCP host，经过真实领域事务、保存结果并继续模型；费用 consumer 故意在整个工具回合后才执行，验证传输与费用分离。随后 Worker 用较小的 64 输出上限共用同一 Task key：原生费用累计 `0.003 × 3 = 0.009 USD`，跨过 `0.008 USD` 后持久 hold，保留超额与冻结 FX。
- 覆盖动态 key 创建 ACK 丢失恢复、同 attempt 并发一次发送、可信属性过滤、native ID 去重、缺 usage/SDK 假费用、无 transport 的日志缺口、status 回退/无效字段、后台存活、创建与退休、配置漂移、删除竞态、Worker 读范围及 Task finish 后的原结果读取。
- 最后 source-retire 与 prior sent/not_sent 负例只续跑受影响生命周期场景：`1 passed in 7.90s`；已过 PG 场景不重复运行。自有必要 ruff 检查通过，受影响 pyright 为 0 errors / 0 warnings。
- 首次组合候选 `make check` 的 lint/type/Pi 通过，pytest 为 `2623 passed / 2 failed / 461 deselected`，退出 2；两条失败来自新增 purge 入口对未初始化 gateway 的直接访问。已修为可选适配器分支，并把新 native key/Runner 调用清理查询限定于唯一能产生这些记录的冻结 V1 Task，普通/legacy 不走新增查询。真实数据库的 native key 未退休和原调用未核闸口保留。原两条删除回归不改断言，与已有 V1 生命周期场景一起 `3 passed in 6.63s`，首次失败日志保留。
- 修复后的独立 clean 候选最终 `make check` 退出 0：ruff format/lint 通过，pyright 0 errors / 0 warnings，Pi 5 项通过，pytest `2625 passed / 461 deselected in 174.54s`。候选 UV 和 Node 依赖按锁独立安装，Python import 指向导出候选；OpenAPI、JSON schema 与 TS 已生成，干净候选 `tsc -b` 通过。根门禁后的修改只有本报告实测结果，不再重复应用测试。
- 初始自有 fixture 曾将 public 配置重复转换、使用不存在的 CtfBudget 字段、给字符串参数传 SecretStr、使用不可达的 host 假服务，以及局部变量遮蔽 helper；这些失败已修正并保留短检查记录，未改原 automatic WIP 断言或产品语义。原 strict 草稿测试没有运行，旧模型用例已随草稿替换，不计为通过。
- 固定版本再次创建同 hash/name key 的必要接缝中，第二次未成功，读回仍为一条资源；这不替代本批平台的单次创建和未知状态对账合同，不声称 collection PUT 通用幂等。

可复现必要场景：

```sh
.venv/bin/pytest -q services/blackboard/tests/test_native_fees_integration.py services/blackboard/tests/test_runner_gateway_integration.py -m integration
```

P3 Runtime/Node 的 17 文件与 NativeAdmin 的 2 文件已有独立检查；通知片使用最终 `af1c596a…` 补丁。三方合成同一 clean HEAD 候选；首门禁发现接入回归后，仅因必要代码修复重跑最终门禁，不在包含旧 WIP 的原树假报全绿。工作树原有 automatic 行为失败保持隔离。

## 偏差与待决

- 本批真实验证的是 **Pi → Runtime → 原生模型 gateway → 假 provider**，以及 **Pi → 直连官方 BusinessMCP host → 真实领域事务**。此前 AGateway Virtual MCP/OpenAPI probe 是独立 toy 接缝证据；本批没有把它称为产品 alias/issuer 已统一，也未以该证据放开 V1 start。
- 原生 MCP 的受信 alias 映射、产品 issuer/target allowlist、Sandbox 路由与实际清理仍属下一切片。继续使用官方低层 pi-mcp 客户端与窄业务桥；不裸替换为已证会丢稳定 callId/meta、404 重发副作用的高层扩展，也不以 OpenAPI 转换器的 4xx 结果代替领域错误语义。
- 累计阈值允许在途费用与超额。约五秒的原生计数持久窗口、单活和未知日志仍按方案保留；不是绝对零损失预算，也未完成多副本公平配额或 G5 全部验证。部署 catalog revision 是受信部署版本标记，不是平台另验供应商账单。
- P4 仍需 Lead 修改题目信息/范围、派工/关闭及 scope_revision 失效的工具闭环、暂停/恢复语义、真实 Runner workload/资源清理。普通任务旧引擎需按方案逐个实际调用迁移后删除，本批只删除自己的未提交 strict 路线。

## 下一步与提交

本批采用单一可运行组合提交，按精确 candidate patch 暂存，保留 AGENTS footer、实际开发模型和原工作树 WIP。建议标题 `feat(runtime): integrate native gateway task cost thresholds`；完整 commit 号随交付消息提供。不推送或合并。下一片继续原生 MCP 的产品 alias/issuer 接线与方案已列 P4 边界。
