# P3e 原生业务 MCP 路由生命周期报告

日期：2026-11-06；实际开发模型：GPT-6.1 Sol / high。基线 `fc222b6aee9080be2f327cf5216b71f1607667cd`。

## 完成内容

1. 现有受信 credentials 注册接口成为实际自动发布入口：先在 Task 锁内核验当前 owner/binding，持久化本 Task 的唯一原生路由意图；原生 HTTP 放事务外；确认后再锁 Task 重核 owner/binding/实时状态和验证 revision，才签发凭据并返回冻结的 `mcp_gateway_url`。没有把独立测试 helper 称为产品自动接线，也不表示所有 Task 创建后立即发布。
2. 新表 `native_task_mcp_routes` 与迁移 0025 保存稳定 source/admin/origin/挂载路径、source hash+Task 唯一资源名、冻结完整 route value、单调退休意图、发布状态、operation_id/revision、脱敏错误和持久下次核验时间。Task/配置漂移不重新分配身份；publishing 唯一 claim，旧读回或迟 ACK 不能覆盖新 operation/stop。
3. 复用唯一 NativeAdmin HTTP 边界，调用原生 `traffic.route` collection GET/PUT 与按 ID DELETE。同名 route PUT 是原生幂等 upsert，避免套用费用 key 的 UUID 创建合同。原生无 If-Match/CAS：检查前值与冻结归属，但仍要求该逻辑 source 的命名空间由平台独占管理。
4. 真实就绪同时读取 DB-owned 资源与 `/config_dump` 实际 listener/route/backend：核固定 gateway、唯一资源 key、Task exact path、business/prefix、引用链和冻结后台地址；最多两秒观察原生加载结果，不用固定 sleep 或 `runtime.synchronized` 伪装 generation ACK。退休必须持久资源与 active route 都缺失；完整 base/config dump 只在内存处理，不落日志或数据库。
5. 专属 `business-mcp` gateway 不接受外国 API-owned 路由或静态 Task 路由，包括不同名字的旧 P3d shadow、prefix/catchall。发现异值/影子拒绝写入及删除，不覆盖文件、别 Task 或别控制面的资源。配置/source 变化先于任何 HTTP 被拒。
6. app lifespan 管理独立的小批恢复循环，共享 NativeAdmin。重启先小批恢复持久意图；此后 ready/unknown 降频核验，首次 Task 停止绕过等待。已确认 ready 的原资源意外缺失可沿原身份恢复；未知发布且远端缺失始终保留 unknown，不假称未发送、不无限重发、不允许 purge。
7. stop/closing/terminal/deleting 的退休意图单调；发布途中停止不与未决创建抢先确认 retired。迟到发布成功后后台回收，不能再发 Task 凭据。已确认 retired 不因旧网关下线/source 配置变更而重新访问原部署。新增最小受信状态 GET 接口，展示必要归属、状态、原因和下次核验时间，避免未知状态只留后台沉默。
8. purge 在 route 退休前保留身份；存在 route 的 V1 在退休之后还必须 Task 终态且独立确认 `cleanup_ready`，不能把删除路由当成 Runner/Sandbox 清理证据。原调用账本、saved-result/control API 与域事务 fence 保留。V1 legacy resume 在同 Task 锁、幂等返回之前明确拒绝，防 stop→扫描前立刻 resume 逃过退休；普通/legacy 不改。
9. 部署文档与 `.env.example` 加四个空配置占位。共享 listener/JWKS/后台 Secret/持久数据库仍由部署提供；不新增 Operator、通用 registry 或新的认证框架。

## 验证

未读取 `.env`，未调用真实模型或靶场。固定镜像与源码沿用 P3d：
`sha256:60216aafb03d37f94e952ff3d51d2f630a65b10ecaebdc0a864882f92610c45b`、source `26f5aebe99a36a037fe54f8080a608aca5547e15`。

- 独立原生小接缝确认：同名 PUT 保同 ID，single GET 不存在、missing single PUT 409、file-owned 冲突 409、DELETE missing 404；删除 A 不误伤 B。active `/config_dump` 实际形状另用 0.84 秒管理 HTTP 读写确认，未重复 Pi/PG/费用探针。
- 固定源码交叉审核：DB commit+notify 并不等待数据面；`synchronized` 仅表示 lastError 为空。`sync_local` 在同一 write lock 中安装 backend→route→policy，active dump 与数据面读同一 read lock，inline JWT/MCP 授权不会以半成品暴露；不另造 policy 解释器。
- 真实 AGW+官方 BusinessMCP+PG 第一组最终 `1 passed in 10.32s`：credentials 自动发布并返回可用 URL；三 Task 共用一个 gateway；五个并发签发不互相作废；已确认缺失原资源恢复；ACK 丢失后更换 HTTP owner/manager，真实 run loop 从 PG 恢复且没有再次 PUT；停止自动退休，另一 Task 仍可调用；V1 resume 拒绝且状态不变；已退休离线可处理，退休尚未 cleanup 禁止 purge，模拟可信清理确认后才允许删除并清掉 route 行。
- 第二组最终 `1 passed in 10.35s`：未知+缺失阻重发和 purge；source 漂移在管理 HTTP 前拒绝；外国 catchall 不删；旧静态同 Task shadow 拒绝；发布未结束时 stop，迟 PUT 一次、后台 DELETE 一次、无迟到凭据，另一 Task 仍可用；状态口只允许 service。
- 原 PG 账本/事务 fence/历史 completed 只读与 Task finish 保存结果回归 `1 passed in 5.03s`。没有重跑 P3d Pi 主链或原生预算/费用 probe。
- 自有 ruff 与受影响 pyright 已通过。最终 clean HEAD 组合候选 `make check` 退出 0：ruff format/lint、pyright 0 errors/0 warnings、Pi 5 项通过，pytest `2626 passed / 464 deselected in 210.72s`。候选 UV/Node/Web 依赖按锁独立安装，Python import 指向候选源码；生成 schemas/OpenAPI/TS 后 `tsc -b` 退出 0。门禁后只补报告与部署文案，不重复应用测试。

必要复现：

```sh
.venv/bin/pytest -q services/blackboard/tests/test_native_mcp_routes_integration.py -m integration
.venv/bin/pytest -q services/blackboard/tests/test_runner_gateway_integration.py -m integration
```

初始必要检查停点保留在 `.data/p3e-native-mcp-20261106`：真实授权 route 的 backend 带 inlinePolicies，初版全字典比较过严，改为固定引用投影并复用原生原子加载；测试 catchall 参数使用了错误的 prefix 字段，修为 pathPrefix；测试直接截断写配置造成临时 503，修为原子替换并实际观察 native base 后再严格断言影子 409；并发测试过早只等待 desired retired，修为等待完整 unknown 观察。未通过放宽业务权限、状态或错误断言绕过停点。

## 偏差与待决

- 原生 API 无资源 CAS，专属网关/命名空间独占管理是部署前提；平台的前置完整读回不冒称可原子阻止外部控制面随后抢写。native route 只保平台需要的薄业务/持久意图。
- 远端缺失但原 PUT 仍未确认时，安全策略是显式 unknown 并阻 purge，不能自动宣布已经清理；状态口给出原因供恢复原配置/运维核验。没有补造通用强制清除或再派发接口。
- 原生删除 route 不发送后台 MCP DELETE，且同名恢复可令旧 session 再可用。产品始终保 host 实时 Task/binding/epoch/SDK/Assignment fence；退休后不重建原 route，不释放进程/席位或冒称 Sandbox 已清理。
- 本片的自动触发点是受信 credentials 注册与既有 app lifespan 恢复循环。K8s bootstrap、V1 start、P4 恢复/派工和 Sandbox 生命周期仍待后续接线；本片实际部署验证为隔离 Docker，并未创建 K8s workload。
- 38 项原 tracked WIP 和 72 份旧 untracked 文件有起始字节 baseline。混合 api.py 的本片改动单独提取；不整文件暂存，不纳入旧 automatic 行为，不改设计正本。

## 下一步与提交建议

建议单个可运行提交：`feat(runtime): reconcile native MCP routes with task lifecycle`。最终 clean 候选 OpenAPI/TS、make check 与 TypeScript 已通过，随后精确暂存、本地提交；不 push、不合并 main。下一片接可信 K8s Runner bootstrap，再按既定 P4 合同推进恢复与多 Agent 控制。
