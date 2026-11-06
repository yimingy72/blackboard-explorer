# P3d 原生业务 MCP 静态接线

固定 agentgateway 1.7.0：
`cr.agentgateway.dev/agentgateway@sha256:60216aafb03d37f94e952ff3d51d2f630a65b10ecaebdc0a864882f92610c45b`。

Blackboard 配置 `RUNNER_MCP_SIGNING_KEY`（至少 2048 位 RSA PKCS8 PEM，Secret）和
`RUNNER_MCP_BACKEND_TOKEN`（独立随机 Secret）。两者缺失时拒绝相应 MCP 接口；模型 capability 仍沿用独立的 HS256。
签发器固定 issuer `bbx-runner-gateway-v1`、audience `mcp`，只从已核验的 binding/member 取角色。
不要把服务 token、RSA 私钥或 backend token 下发给 Runner。

用受信部署进程调用 `bbx_blackboard.gateway_auth.mcp_public_jwks(settings)` 导出 RSA **公钥**到
`/etc/bbx-mcp/jwks.json`；不要公开任何对称签名 key。Gateway 单独只读挂载公钥与
`/etc/bbx-mcp/backend-token`（内容与 Blackboard 后端 Secret 完全相同，无尾随换行）。
AGW 的 MCP 鉴权会剥除入口 Authorization；原生 backendAuth `passthrough` 从已验证 Claims 恢复原 Runner JWT，
`credentials` 覆盖专用后台头，SDK 头仍由原生转发。
Blackboard 拒绝缺失、错误或重复后台头；它仍再次验证 Runner 的短 scope 和领域事务 fence。
后台 Secret 只提供给 Blackboard 与可信网关；后台 HTTP 端口应仅在可信服务网络暴露。

在仓库根目录生成共享单进程的静态路由（可多次指定 `--task-id`）：

```sh
.venv/bin/python -m bbx_blackboard.native_mcp \
  --task-id 11111111-1111-1111-1111-111111111111 \
  --task-id 22222222-2222-2222-2222-222222222222 \
  --backend-origin http://blackboard:8000 \
  --gateway-origin https://mcp.example.invalid \
  --jwks-file /etc/bbx-mcp/jwks.json \
  --backend-token-file /etc/bbx-mcp/backend-token > business-mcp.json
```

仅把输出合并到该共享网关的受信配置；每条 Task route 名称与内联 MCP backend 独立。
每条精确路径 `/api/tasks/<Task UUID>/mcp` 在任何 backend initialize 前核验已验签的 Task/role；
未知 Task 路径不产生 target 请求。单 target 原生不允许配置 `condition`，因此使用 route authorization 的
`require` 与 MCP 角色工具授权。固定 target 名称 `business`、`prefixMode: always`；管理端冻结
`canonical → business_<canonical>` 映射。Runner SDK、账本、metadata 与真实领域工具保留 canonical 名称。

Runner 配置 `mcp_gateway_url` 为该 Task 的完整网关路径，准备命令由管理端产生可信别名；不要把它设为
Blackboard 的 `/business-mcp/` 私有后台地址。通过固定镜像的 `--validate-only` 后再加载配置。
当前入口生成可部署静态配置；Task 创建/停止时自动发布或回收 route 的生命周期控制仍属于后续切片，
本片不宣称已启用 V1 start、完整多 Agent 编排或 Sandbox。
