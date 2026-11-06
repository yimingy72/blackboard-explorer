# 原生业务 MCP 网关接线与任务路由生命周期

固定 agentgateway 1.7.0：
`cr.agentgateway.dev/agentgateway@sha256:60216aafb03d37f94e952ff3d51d2f630a65b10ecaebdc0a864882f92610c45b`。

## 共享部署基础

共享 agentgateway 保持单活。部署提供唯一的 `business-mcp` gateway、持久配置数据库、公钥与后台 Secret
挂载；动态 Task 路由由 Blackboard 管理。最小基础配置示例：

```json
{
  "config": {
    "adminAddr": "0.0.0.0:15000",
    "storage": {"mode": "hybrid"},
    "database": {"url": "sqlite:///var/lib/agentgateway/config.db?mode=rwc"}
  },
  "gateways": {"business-mcp": {"port": 3000}}
}
```

SQLite 路径必须挂持久卷；也可沿用已配置的原生持久数据库。可与既有模型 gateway 配置组合。
`business-mcp` 使用专属端口，不与另一个 gateway 共享端口；不要用旧 binds、MCP shorthand、静态 routes
或其他控制面向这个管理 gateway 添加路由，包含 catchall/prefix。本片受信基础配置不支持旧 binds 或顶层 MCP shorthand，
需要与模型共用进程时使用现有 llm 配置或明确的 named gateways。P3d 的静态 Task routes 需由受信部署先行
移除；本控制器发现它们会拒绝接管，不修改或删除文件中的内容。

Blackboard 配置：

- `NATIVE_GATEWAY_ACCOUNTING_SOURCE_ID`：稳定的逻辑部署和持久数据库命名空间，不能是 PodUID/进程 ID。
- `NATIVE_GATEWAY_ADMIN_URL` 与可选 `NATIVE_GATEWAY_ADMIN_TOKEN`：唯一原生管理入口。
- `NATIVE_MCP_GATEWAY_URL`：Runner 可达的 MCP gateway origin，例如 `http://agentgateway:3000`。
- `NATIVE_MCP_BACKEND_URL`：网关可达的私有 Blackboard origin，例如 `http://blackboard:8000`。
- `NATIVE_MCP_JWKS_FILE`、`NATIVE_MCP_BACKEND_TOKEN_FILE`：网关内的绝对挂载路径。
- `RUNNER_MCP_SIGNING_KEY`：Blackboard 独占的至少 2048 位 RSA PKCS8 PEM Secret。
- `RUNNER_MCP_BACKEND_TOKEN`：Blackboard 与网关独占的独立随机 Secret。

origin 不带 Task 路径、用户凭据、query 或 fragment；上述路径/来源在每个 Task 首次准备时冻结。
不要在 Task 活跃或退休未确认时切换源、改目标/管理入口或覆盖配置；漂移会拒绝写入及删除，保留原身份供恢复。
已确认退休的路由无需再次访问旧网关；删除 Task 仍须通过模型 key、未决调用、cleanup 等既有门禁。

由受信部署进程调用 `bbx_blackboard.gateway_auth.mcp_public_jwks(settings)` 导出 RSA 公钥，网关只读挂载。
不要把 service token、RSA 私钥或后台 Secret 下发给 Runner。后台 Secret 文件内容与 Blackboard 完全相同，
无尾随换行。原生 `backendAuth.passthrough` 从已验证 Claims 恢复原 Runner JWT，`credentials` 覆盖私有后台头；
host 同时核验后台 Secret、Runner capability 和实时领域事务 fence。管理 API 与私有后台端口只开放给可信服务网络，
本逻辑 source 的 `bbx-mcp-<source hash>-<Task UUID>` 路由命名空间只允许这个平台写入。

原生 API **没有 If-Match/CAS**，同名 PUT 会覆盖 API-owned 值。平台会先核完整原值及归属、拒绝异值和外国路由，
但这不代替独占管理命名空间的部署约束；不能让其他控制面在检查后抢写同名资源。

## 实际自动触发与恢复

受信管理调用现有 `POST /api/tasks/<Task UUID>/ctf/runtime/runner-gateway/bindings/<binding>/credentials` 时，
平台先锁 Task 核验当前 owner/binding，再持久化路由意图；原生 HTTP 全部在 Task 事务外执行。路由持久读回与
`/config_dump` 实际 listener/route/backend 引用链都确认后，才在新事务内再次核 owner/binding/Task 并签发。
回包增加 `mcp_gateway_url`，bootstrap 必须使用此可信返回值，不能自行拼接其他 Task 或后台地址。

固定 target `business`、`prefixMode: always`。管理端冻结 `canonical → business_<canonical>` 别名；
SDK、metadata、调用账本与真实领域仍使用 canonical 名称。精确 Task 路径在 native initialize 前核验 Task/角色。

app lifespan 启动受管的有界恢复循环：重启先小批读取持久意图，此后 ready/unknown 持久降频；Task 停止、closing、
完成、失败或请求删除会绕过首次等待，单调进入退休。已 ACK 的 ready route 在意外缺失后可沿原身份恢复；
发布 ACK 丢失只读回同一资源。未确认的 publishing/unknown 若远端也缺失，会保留未决，不能冒充未发送、ready 或 retired，
也不能无限重发。停/删与发布并发时，迟到 ACK 绑定原 operation，不能覆盖退休意图或再签发凭据。

状态可通过受信 `GET /api/tasks/<Task UUID>/ctf/runtime/runner-gateway/mcp-route` 查询，返回来源、资源 ID、
状态、退休意图、revision、下次核验时间和脱敏原因；不返回完整原生配置、Secret 或 capability。
原生资源与 active route 均缺失后才能确认 retired；purge 在确认前保留唯一持久归属。
存在 Task route 的 V1 还必须终态且有独立的 cleanup_ready 证据，不能仅凭 route retired 删除平台数据。

删除路由不会替代 Runner/Sandbox/远端 session 清理；固定版本同名重建甚至可恢复旧 session。
Task 退休后不重建原路由，V1 legacy resume 明确拒绝；后续 P4 恢复需独立合同。
保存结果与控制 API 仍走平台并保原权限，不因路由删除而丢失账本。

## 边界

本片真实自动入口是已存在的受信 credentials 注册；它不表示所有 Task 创建后立即发布，不开放 V1 start，
也没有补造 K8s bootstrap、Sandbox 控制器或完整 P4 编排。

离线单 Task 接缝仍可用 `python -m bbx_blackboard.native_mcp` 生成 P3d 静态配置；不要把那份带 routes 的输出
与启用本生命周期控制器的专属 gateway 混用。修改部署后先运行固定镜像 `--validate-only`，再加载基础配置。
