# P3c Agentgateway 费用阈值调整报告

2026-11-06 · GPT-6.1 Sol / max · 预算 probe 事实已补，原生 MCP 仍为候选门槛；文档待最终独审，未暂存/提交。

## 1. 完成内容

用户侧聊授权为“如果评估下来可行请发给主会话修改方案”；root/runner_protocol_review 交叉审核后，按授权先改七份文档，无需再向用户确认。对[整体方案](平台架构重构开发方案-20261105.md)的模型/预算/P3/G5/迁移段及四份设计正本当前合同作必要覆盖，不重写下文 legacy/普通领域历史。

新 V1 默认预算变为累计达阈值后暂停/终止，允许在途/超额；agentgateway 1.7 standalone 唯一负责上游 provider/协议/凭据、usage、USD cost 目录/APIKey DB per-key 执行。每 Task 稳定共享无 window key，Runner 仍沿 binding/epoch 短 token，Runtime 仅 fence/原请求一次派发/字节薄转发。平台可信原生日志去重归集、status 补触发，不用 Prom、假 webhook 或快照＋明细双计。

原生复用矩阵已收敛：暂推荐官方低层 pi-mcp＋窄桥，高层因 callId/meta/404 重发等未采用；business_mcp 是官方 Server 薄 host，不称自研整层。Virtual/OpenAPI 仍是待实证候选，不全量开放内部 API，不造复杂协议；错误/结果/fence 和命令/流量/eBPF 保留。

本轮追加只落必要范围：poll 后续复用现 PG 通知、先订阅后读/提交生效/租约与 listener 回退，不增总线；DTO/小元数据派生而非 registry；native 模型/路由一处编辑其余派生；K8s 1.34 原生职责及 ownerRef/Job/卷/Lease 边界分清。原模块替代矩阵明确普通/CTF/progress/chatworker/EnvdClient 等阻塞调用，逐闭环同步删旧代码/失效配置依赖测试，不只裁 P3c、不统一拖 P6、不因历史可读无限保旧引擎；本轮不盲删代码或误当 G1/P1 probe 是产品控制器。

USD/source/catalog 原值保留，CNY 需显式配置并冻结汇率来源/时点一次换算，不 1:1、不猜实时汇率；legacy CNY 保持。缺行/usage/cost/catalog/消费缺口为待核；已知零需完整证据，已确认费用/暂停不回退。约 5 秒写库窗口、单活及异常/多副本边界不扩成零超支保证。

旧未提交严格预留、自建 provider/SSE/通用计价仍冻结，未裁剪或新增预算代码。既有 P3a/P3b/G1 与 MCP 实际写事务 fence、可信 host 结果、unknown 不重派、历史 completed 读取保留；当前 Worker 列题进一步按 runtime binding 当前题限域，公告共享。MCP 小补丁独立记录，不随本次文档批暂存。

## 2. 实证与未验证边界

已读 `.data/p3c-agentgateway-20261106/report.md` 与 `summary.json`：官方固定 1.7.0 镜像 digest `sha256:60216aafb03d37f94e952ff3d51d2f630a65b10ecaebdc0a864882f92610c45b`，git `26f5aebe99a36a037fe54f8080a608aca5547e15`。同 Task 两角色各 0.003 USD、累计 0.006 跨 0.005 完成；下一 budget_exceeded 429 且 fake provider 零调用，另一 Task 仍 200；provider 收到自身 fake key，薄门面覆盖伪造归属头，native id 由网关生成。

无 usage/断流/普通 429 的行最终可由容器内 SQL/native API 找到，cost 可为 0、error=NULL 而 token 全 NULL，均待核；缺 catalog 则 usage 齐、cost=NULL、不扣原生 USD，不能当已知零。首轮宿主机 SQLite 未见最新行的停点保留，根因未证，不改称永久缺行或保证每请求必有费用行。

本轮实测 status/search/get API、completed_at/id opaque 分页与晚完成，说明 started_at 水位会漏；分页本身不证明所有 attempt 完成，自定义属性过滤未测。SIGKILL 前 API 0.006/DB 0.003，重启 API 0.003；等待 5.2 秒持久后重启均 0.006，原日志 id 可 get。平台已确认明细/暂停不能随 native counter 回退，不能把两视图相加。该 metadata 配置 has_payload=0，未保存 prompt/completion；不是所有部署默认配置保证。隔离容器/DB/网络与测试凭据已清，保留脱敏证据。

新模型链路的持久一次派发/native 费用消费者、完整缺口恢复、K8s 单活升级/多副本、动态 key 创建恢复、FX/旧 CNY 隔离与原生 MCP/OpenAPI 对比未完成；P3b 已有持久控制/fence 不因此被否定。只读限制与候选门槛见[任务](P3c-Agentgateway费用阈值调整-20261106.md)，native 局部 probe 不代表完整 G5/产品发布通过。

## 3. 检查与范围

修改前五份既有文档干净，原样 SHA-256 基线位于 `.data/p3c-costdocs-20261106/baseline-manifest.json`（HEAD 83836a0）。最终七文档本地链接 38 处均存在、围栏平衡、`git diff --check` 通过；官方链接已按 standalone 1.7 文档核对，原生 MCP 只读限制与尚未实证的门槛分别记录。只做本批文档/合同覆盖检查，不为它运行应用全量、真实模型、靶场或部署。

产品旧严格快照仍在 `.data/p3c-gateway-20261106/frozen-strict-20261106/`，本侧 21 文件 own patch SHA-256 为 `d0c5320cebc7b964a04e989f21db6a7d4032a8c4c527095f568f971f6ecd93f7`。它保留审计，不能当新预算交付。未读取 `.env`，未推送或合并，未改运行中 Task。

## 4. 后续与提交

先补 probe 事实、最终独审，再仅提交七份文档，建议标题 `docs: adopt agentgateway task cost thresholds`。产品范围差异审之后另做必要实现/组合 candidate；P4 补暂停/恢复、Lead 题目更新/派工/关闭及 scope_revision 工具闭环。报告不写自身提交号，结果由最终消息给出。
