# P3d 原生业务 MCP 接线报告

日期：2026-11-06；开发模型：GPT-6.1 Sol / high。候选基线 `b9a83cf3160869e68c7c50e237112c130f1b2ad3`。

## 完成内容

- MCP capability 改为配置式 RSA/RS256、固定 issuer/audience 与公钥指纹 kid；缺少 RSA 配置明确不可用，不保 MCP HS256 回退。模型 capability 仍保持原 HS256。签发与验签共用最多两份 key material 缓存，避免每个请求反复解析 RSA 私钥；公开 JWKS helper 只输出 RSA 公钥。
- role 从当前可信 binding 对应 member 读取。每次网关 scope 与真实领域事务再次复核 role，保留 Task、owner epoch、binding、SDK journal 和当前 Assignment 验证。
- 官方 BusinessMCP host 要求独立后台 Secret 和原 Runner MCP capability。原生 backendAuth passthrough 从已验证 Claims 恢复原 Runner Authorization，再用 credentials 覆盖唯一后台头；Runner 不获得后台 Secret 或 service token。host 拒绝缺失、错误与重复后台头。
- 一个共享 agentgateway 进程按 Task 精确路径挂多个原生 Virtual MCP route。每条 route 先原生鉴权，再强制 Task/role，最后角色工具白名单；固定业务 target `business`。管理端冻结 canonical→`business_<canonical>` 别名；Runner 仅改变 wire 名称，metadata、调用身份、账本和领域仍保持原 canonical 名称。
- 提供 `python -m bbx_blackboard.native_mcp` 静态配置生成入口与 `deploy/agentgateway/README.md`。这不是任务生命周期发布控制器；自动发布/回收仍属后续切片。
- 保留现有持久调用账本、保存结果先于回复、unknown 不重派与历史 completed 有限读取。更新旧 P3c 的业务 MCP fixture，使其同样走原生网关，不给 Runner 添加后台凭据。

## 验证

全程只使用合成 RSA/Secret、假模型 provider、自有容器和真实 PostgreSQL，没有调用真实模型或靶场。

- 固定镜像 `cr.agentgateway.dev/agentgateway@sha256:60216aafb03d37f94e952ff3d51d2f630a65b10ecaebdc0a864882f92610c45b`，配置 validate-only 通过；字段与执行顺序按 source `26f5aebe99a36a037fe54f8080a608aca5547e15` 交叉审核。
- RSA 边界测试 `1 passed in 1.32s`：有效 MCP/模型分别验证，拒绝 MCP HS256、错误 issuer/audience/role/kid，缺 key 返回 unavailable，JWKS 只有公钥字段。
- 真实 Pi→Runtime/原生模型 gateway/假 provider 与 Pi→原生 Virtual MCP→官方 BusinessMCP→真实领域事务：`1 passed in 15.40s`。同进程两 Task 路由；Pi 真正建题一次，host 保存结果后故意丢回复为 503，Runner 读取原保存结果并继续模型；领域效果和业务派发都只有一次。
- 同一接缝验证错 Task initialize 返回 403 且后台 MCP POST（含 initialize）计数 0，外 target 调用后台 MCP POST 计数 0，客户端伪造后台头被覆盖为唯一受信值，直连 host 401；真实 Worker 只发现两个获准工具，调用隐藏 Lead 建题工具被拒绝且后台 MCP POST 计数 0，跨题读取准入 403。
- 原持久账本/旧 owner/domain fence/Task finish 后原结果读取的受影响 PostgreSQL 场景已通过一次（组合输出 `1 passed`；另一新增检查因测试解析器错误失败，不把失败组合称为全绿）。没有重跑原生预算/费用探针。
- 自有文件 ruff 通过；受影响 pyright 0 errors/0 warnings。Node 5 项自有检查通过。最终 clean HEAD 组合候选 `make check` 退出 0：ruff format/lint 通过、pyright 0 errors/0 warnings、Pi 5 项通过、pytest `2626 passed / 462 deselected in 191.99s`。候选 UV、Node、Web 依赖按锁独立安装，Python import 指向候选目录；contracts JSON schemas、OpenAPI 与 TS 已重生成，`tsc -b` 退出 0。共享 UV 缓存坏件导致依赖准备初次失败，改为本轮独立缓存后成功；不把环境准备停点当产品测试失败。门禁之后只更新报告实测，不重复应用测试。

可复现必要入口：

```sh
.venv/bin/pytest -q services/blackboard/tests/test_gateway_auth.py
.venv/bin/pytest -q services/blackboard/tests/test_native_mcp_integration.py services/blackboard/tests/test_runner_gateway_integration.py -m integration
```

本次必要接缝的初始失败保留于 `.data/p3d-native-mcp-20261106`：合成 fixture 的预算预期值不匹配；容器缺少 `host.docker.internal` 解析（仅本轮容器添加 host-gateway，不改全局配置）；MCP 原生鉴权剥除入口 Authorization（修为 verified-token passthrough）；人工 Worker 探针把 SSE 响应当 JSON（只修测试解析器）。未放松任何 Task、角色或业务 fence。

## 偏差与待决

- 原生固定版本禁止单 target 使用 condition，因此采用每 Task 精确 route 的原生 authorization require，先于 backend initialize；没有伪造第二 target。独审已核 JWT claims 写入先于 route authorization；真实错 Task 负例也证明后台 MCP POST（含 initialize）为 0；不外推 GET 或全网络流量。
- 本片没有改设计正本，没有开放 V1 start。多 Task 静态共享网关链已接通，生命周期自动路由发布、P4 派工/停止/作用域更新与 Sandbox 接线尚未交付。
- 本片只读确认本地 Kubernetes `docker-desktop`、v1.36.1 Ready；实际接缝使用隔离 Docker fixture，没有部署 Kubernetes workload，未把环境现状称为已锁版本证明。
- 现有 WIP 保留原样，按起始 baseline 提取自有精确补丁；禁止 whole-file stage。最终组合只从 clean HEAD 导出候选，不以混合工作树测试替代发布门禁。

## 下一步与提交建议

建议与已独审 Runner alias 片组合为一个可运行提交：`feat(runtime): route business MCP through native virtual gateway`。clean 候选 make check、生成契约与 TypeScript 检查均已通过；不 push、不合并 main。下一片实现受信任务生命周期发布/回收，然后按既定 P4 范围继续。
