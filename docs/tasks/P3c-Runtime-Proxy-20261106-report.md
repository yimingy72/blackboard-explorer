# P3c：Native gateway 模型薄入口阶段报告

2026-11-06 · GPT-6.1 Sol / max。正式方案基线 `0efc586cd8e83937ae52f66cc79df607805c75d1`；用户授权累计费用阈值方案覆盖旧未提交 strict provider/SSE/预留草稿。旧完整 r2 快照保留于 `.data` 审计，本文描述最终 native 薄入口本侧，不把此前 strict 验证当新方案交付。

## 1. 完成内容

| 项目 | 最终实现 |
|---|---|
| Runtime 职责 | 同进程三个模型入口；固定 `runner_gateway_url`，只向 `/v1/chat/completions` 发原文。保 live 身份/已确认 journal/原请求摘要/一次 CAS，消费已 ready 的同 Task 私有 key grant；没有独立 admin/key 创建与重试层 |
| 同 Task 归属 | 仅 Runtime→gateway 使用 Task key；七项可信 Task/binding/epoch/Agent/attempt/call/SDK 属性从后台 grant 注入，覆盖 Caller 头，不带用户选择的 x-request-id。key 不进入 Node/命令/journal/日志 |
| 请求与响应 | SDK 最终 body_json/digest 保确切 UTF-8；限制8MiB原文/17MiB元数据。响应字节、HTTP状态、压缩编码原样转发，不解 SSE、不仿造错误SSE或等待计费ACK |
| 代码删除 | 删除 resolve_model_credentials/provider URL准备、provider usage/SSE parser、价格计算/严格route声明文件、硬context证明、financial capture/nonce 与对应失效断言 |
| 原生费用权威 | Runtime只发送有界传输观察，SDK结果也只观察；native目录/凭据/协议/usage/USD目录/APIKey预算与BB日志/status消费/FX/持久stop由另一侧实现。未知费用不补0，transport phase不能改财务状态或重置CAS |
| SDK snapshot | 固定单一 OpenAI兼容gateway前端，删 request_format/DeepSeek分支，model id为有界str可含斜线alias；思考等级/输入模态等仍冻结。snapshot从服务端native目录派生，缺必需字段不能猜值就绪 |
| 生命周期 | 原可靠response.__call__/generator/Runtime.close共用finishing Task与active ownership，首响应头失败、未进入generator、断连及并发关闭均收敛FD/本地slot。观察最多2秒，不等待未知费用卡住控制/cleanup |
| MCP保留 | 官方低层pi-mcp、稳定原生调用身份、journal/admit、host保存结果确认、回包丢失仅查询原结果和失败冻结；未裸换高层extension/转换器，未新增本机工具/Helpers |

Runtime本地max_inflight默认8、超限直接429，无等待队列；它是传输资源上界，不是多副本/provider全局公平预算。V1 start保护仍关闭，完整公平调度/恢复/发布和K8s另批。

## 2. 实际验证

```sh
.venv/bin/pytest -q services/agent-runtime/tests/test_runner_model_proxy.py
.venv/bin/pyright services/agent-runtime/tests/test_runner_model_proxy.py services/agent-runtime/src/bbx_runtime/runner_proxy.py packages/contracts/src/bbx_contracts/runner.py services/agent-runtime/src/bbx_runtime/settings.py
cd services/agent-runtime/task-runner
node scripts/generate-protocol.mjs
node --test --test-concurrency=1 test/protocol.test.mjs test/runner.test.mjs
cd ../pi-seam
node --test --test-concurrency=1 test/gateway.test.mjs
```

- Native薄传输 **3/3通过**（约0.50秒）：原文/Task key/可信属性覆盖；不解析的opaque与gzip原字节及两种429同样透传；重复/换包/旧epoch不多发，不得替原并发调用提交清零观察；未固定gateway地址零发送；断流/取消/response.start失败/Runtime.close并发只记录一次传输观察并释放FD/slot。此组用MockTransport验证本侧，**不替代真实BB授权/native费用组合**。
- Runner受影响 **3/3通过**（约20秒）：原文跨语言/不确定确认/恢复与控制；两个真实SDK session同名工具隔离、统一OpenAI面high reasoning、prompt下次边界刷新、结果原文及丢回包不重派等。既有PVC算法未改，不重复其故障矩阵。
- 共享模型边界的必要P1调用方 **8/8通过**（约3.8秒）：主回合/两类压缩/一次发送/确认阻止发送/未知工具冻结等保留；无provider usage的SDK done明确只作SDK观察，不从零默认值结费。未重复daemon/插件/基础设施矩阵。
- 本侧Python修改与直接测试ruff lint/format、pyright通过，pyright **0 errors / 0 warnings**；协议由runner.py生成。

真实固定agentgateway 1.7能力probe另保存在 `.data/p3c-agentgateway-20261106`：共享累计/跨阈/隔离、native日志与重启窗口已实证；不是本侧持久消费者已完成。真实PG→Runtime→native gateway→假provider、日志去重/FX/status停工等由P0合并双方候选验证，最终只跑一次根门禁。本侧未重复整套 `make check`、未调用真实模型/靶场或读`.env`。

## 3. 原生MCP取舍与明确门槛

`.data/p3c-mcp-reuse-20261106` 的最小组合probe与8artifact已独审通过。实测MCP→MCP `_meta.bbx`可透传、JWT target/role/backendAuth能力可复用；高层扩展在“效果发生后注入404”故障下重发，tool_result确认hook抛错仍进入下一脚本模型；受限OpenAPI的403/409/422为isError=false且缺原status，平台meta不自动映射REST，故保现薄host复用domain。

该probe是自有内存counter/假StreamFn，不是实际board/PG/Sandbox事务或产品权限/持久证明；modelSends仅脚本计数，foreign只记录MCP POST含initialize。RS256/kid/role fixture不表示现BB HS256 issuer已接通。真实Gateway工具alias与backend原名须按受信发布配置和实际list固定唯一映射，现prepare精确匹配，不猜prefix。**本片只闭native模型费用，产品MCP alias/issuer统一须下一切片验证，不能据toy_meta声称全链路开放。**

## 4. 依赖、历史与后续

同进程HTTP薄入口仍用已安装/已锁Starlette和uvicorn。uv.lock只补bbx-runtime两个dependency与两个requires-dist引用，不升级包、source或下载hash。旧 `uv lock --offline` 曾因现有httpx2缓存缺失失败，已如实记录；没有宣称完整lock重算通过，已有版本结构/导入验证保留，未引入新包或升级legacy Pi锁。

Native USD与legacy CNY按正式方案和冻结FX区分。原生status与明细不能相加、counter回退不能撤销已确认费用/暂停；缺行/usage/cost/catalog待核。SDK completed/usage、空未决列表、本地HTTP结束/取消都不证明费用已知或远端命令已清理。必要控制、停止与cleanup不受模型预算阻断。

P0负责原生key稳定创建/失ACK读回、模型目录、原生日志去重/未核attempt/FX与status阈值持久stop；本侧仅接其NativeModelDispatchGrant和TransportReceipt。整批组合独审后由root统一Git窗口，不先提交半套接口。建议组合提交 `feat(runtime): integrate native gateway task cost thresholds`，注明实际GPT-6.1 Sol/max与任务P3c。既存WIP保留，本文阶段未stage/commit。
