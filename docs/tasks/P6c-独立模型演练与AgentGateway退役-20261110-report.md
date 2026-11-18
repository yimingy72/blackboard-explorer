# P6c · 独立模型演练与 AgentGateway 退役报告

实施从已审 P6b `1174b2b1` 开始，独立设计提交 `6f988b33`。本报告区分候选验收和现场切换结果。

## 完成内容

1. **P6c.1 · 独立模型演练。** 主站指定操作者用正常平台账号，在模型页对有凭据及明确输入、输出限制的已启用模型做单轮文本测试。演练用独立 purpose 和同一 Pi 持久账本，无 Task、平台虚拟 API key 或工具授权；上游模型仍需配置真实凭据；同编号只执行一次，回读原结果与计量，未知结果不重发。缺少中转费率仍可演练和只读复盘，答复可正常显示，费用标为未知；带预算的执行仍要求可证明费率。真实旧配置缺少凭据、上下文或输出能力时作为可编辑草稿保留，不能发送。
2. **P6c.2 · 独立执行与旧记录。** 活模型目录、网关管理/发送/对账、旧 MCP 签名与动态路由、旧前端入口、部署变量及旧依赖探针退出。普通任务和原生 Runner 用既有 Pi 可信发送；Native Blackboard / Sandbox 双目标保留，外部 MCP 仍仅在明确的 Task 与角色授权范围内。Task/Agent 费用、成果整理和跨站只读复盘保持各自用途与去重。原本以 CNY 保存的金额、旧 USD 费条、来源和未知记录直接从本地读取，新版读取不调用旧网关，也不重算旧价。不能证明迁移的旧执行显示阻塞，可读/导出，服务端不分配新资源或许可。原生终态只读复盘按确证停止、原 Runner 已撤权且无执行回合检查，不再等待普通工作区压缩包；原清理、删除和费用待确认字段不变。只读复盘使用已有持久记录；没有原会话时使用空的复盘上下文，不伪造旧历史。
3. **P6c.3 · 部署与保全。** 首次切点的两站 PG、MinIO 和旧网关 SQLite/WAL/SHM 已做加密备份并核验。旧网关 46 条原始日志全保留；没有足以认定金额或归属用户的记录，不据此生成已结算费用。隔离验收发现发布归档解包把 Runner 的 `protocol.schema.json` 设成 0600，测试运行账号无法启动；首次切点已释放，两站原镜像和 Relay 恢复，生产数据库未迁移，未调用真实模型。通过保留 Git 文件权限修复发布镜像，不放宽容器权限；修正镜像以实际 UID 1000 通过相同初始化入口的离线读取。第二次用修正镜像执行，Runner 初始化通过，但在发放 Runner 凭据前进入资源状态未知。第二次的两站加密备份也已核验，旧服务恢复、冻结释放；生产没有迁移或退役。另用同集群的隔离容器复现了转发在监听未就绪时退出的竞态；第二次没有保留足够诊断，不能把该竞态直接写成第二次失败原因。新版只在同一 Pod 及容器身份、原 120 秒内重建只读就绪转发；认证和身份错误仍立即拒绝，不重复模型或工具。相同集群的延迟监听证明，重建后可取到原身份的就绪响应，并关闭全部转发进程。第三次实机验收确认默认网络与 VPN 两条原生路径均完成合成模型、两类工具和原 Agent 捕获；VPN 实际往返、断开，并复读归档部分与索引，合成对端和密钥已清理。两站在 2026-11-11 上海时间 01:21:55 通过现场核验，01:22:45 退役旧网关。新版 Native 发行使用 schema 3 证据，绑定当前 Runtime 配置、Runtime/Runner/Sandbox host 制品、Pi messages 与直连 MCP；原环境和网络/采集要求保留。新版本不能借用旧 host 的验收记录。

   最终切点 `1b0a0e50-e219-4a2a-9a8a-c52ff85f95cf` 下确认两站保留原 PostgreSQL 系统身份、原 PG/MinIO 数据卷，向前迁移至 0032；主站只导入原模型名、连接地址和上游标识，缺少的凭据、能力与价格保持待完善。没有回写旧数据库备份。退役时按原 UID 移除 2 个工作负载、3 个服务、2 个专用部署 Secret，关闭旧转发入口；原 SQLite PVC 保持同一身份。01:23:58 的退役后复查再次验证两站认证、原库与数据卷、0032、旧 Pod 和端口不存在；原 OpenSandbox 的 3 个工作负载仍就绪。

## 如何验证

候选源码通过显式 Python 路径加载，避免虚拟环境的旧 editable 安装；证据目录记录 contracts、objects、blackboard、runtime、envd、sandbox execution 和 eval 的 7 项候选导入。

```bash
export PYTHONPATH="$PWD/packages/contracts/src:$PWD/packages/objects/src:$PWD/services/agent-runtime/src:$PWD/services/blackboard/src:$PWD/services/envd/src:$PWD/services/sandbox-execution/src:$PWD"
make check
.venv/bin/pytest -q services/blackboard/tests/test_pi_model_sender_integration.py::test_operator_playground_sends_through_real_runtime_listener
.venv/bin/pytest -q services/blackboard/tests/test_pi_model_ledger_integration.py::test_native_terminal_review_uses_confirmed_exit_without_workspace_tar
.venv/bin/pytest -q services/blackboard/tests/{test_historical_fees_integration,test_pi_playground_integration,test_pi_model_ledger_integration,test_p6b_mcp_integration,test_native_recovery_integration,test_native_ctf_publication_integration,test_native_runner_bootstrap_integration,test_native_helpers_integration,test_native_seats_integration,test_native_worker_dispatch_integration,test_native_resource_retirement_integration,test_native_traffic_integration,test_sandbox_commands_integration,test_runner_gateway_integration}.py
```

- 真实 PostgreSQL / API 候选：26 项通过，包括没有网关配置的真实应用启动、缺字段模型草稿、旧 CNY 及原 USD/unknown 读取、旧执行阻塞、演练并行重放、复盘原用途、Native 冻结/恢复、工具授权与 sandbox/capture 生命周期。最后的模型演练重放/超期与直连协议改动另有 4 项通过；末次旧模式拒绝门槛经 8 项 Native PG 定向验证。
- `make check` 通过 Ruff、Pyright、Pi 脚本 5 项及 pytest 2567 项。完整检查开始后的最终 Native 旧模式门槛由上述真实 PG 和静态检查复验。
- 实际已登录 POST→Uvicorn 可信 Runtime→Pi→合成 TLS 上游→同一真实 PG 账本，验证已定价、缺价和同编号回读。Native Node 定向 18 项通过（另有 2 个默认跳过的集成变体）；浏览器 1 项通过，验证断线后按原编号取回、费用未知而不重复发送。
- Owned Kind 第一次实际创建了合成 Task 与 Sandbox，Runner 初始化失败前未发送模型请求；测试命名空间已清理。恢复回执为上海时间 2026-11-11 00:06:59，两站确认释放原冻结、恢复原镜像和 Relay，生产数据库 schema 未改变。重试必须使用相同的 Native 私网配置，不能用不同端口的证明替换正式发行。
- 两次现场证据在 `.data/p6c-agentgateway-retirement-20261110/`：`cutpoint-19a18664-54df-4a67-b755-d98c9ed83054/receipt.json`、`native-kind-failure-receipt.json`、`rollback-19a18664-54df-4a67-b755-d98c9ed83054.json`、`exact-image-provenance.json`；第二次为 `cutpoint-4f645922-4718-4493-a5cf-7cc9c89dd8ab/receipt.json`、`native-kind-second-failure-receipt.json`、`rollback-4f645922-4718-4493-a5cf-7cc9c89dd8ab.json` 以及 `readiness-forward-race-proof.json`、`readiness-forward-fixed-kind-proof.json`。最后新增就绪修复 4 项定向测试通过。第三次实机的默认路径 5 次合成模型实际 POST、24 个原 Agent 数据包已归档；该次回执未复读临时对象。VPN 路径 16 次合成 POST、22 个包、归档部分及索引复读验证、实际断开成功，两个任务均确认停止。对应 `readiness-release/native-sdk-fixture-{default,vpn}-receipt.json` 和 `native-v3-publication/gate-receipt.json`；新 v3 已通过本地发行解析。合成模型和工具用于验证；不触发真实提供方费用。现场最后以同一第三切点验证两站登录、同一模型草稿与 MCP 目录、跨站复盘费用、私有演练接口无授权拒绝、Native v3 发布及旧网关端口关闭。实际启动时先发现旧 Native 包装器仍要求已退役的签名挂载，改用新版直接入口后通过，没有恢复旧 Secret；生产数据库只向前迁移，不覆盖原数据。对应 `production-native-entrypoint-fix-1b0a0e50-e219-4a2a-9a8a-c52ff85f95cf.json`、`verified-live-1b0a0e50-e219-4a2a-9a8a-c52ff85f95cf.json` 、`retired-1b0a0e50-e219-4a2a-9a8a-c52ff85f95cf.json`、`production-final-1b0a0e50-e219-4a2a-9a8a-c52ff85f95cf.json`。
- 末次 Blackboard 终态复盘修正经真实 PostgreSQL 及定向 Ruff/Pyright：退出未确认或原 Runner 仍活时，待办发现、领取或实际发送拒绝；退役写者确认后，只读 Pi 请求按原身份取得一次发送许可，模拟完成回执入账但缺价仍未知（这一补充用例不额外发送上游请求），原 `cleanup_ready` 保持 false。Running Task 内已结束成员的复盘行为继续通过回归。该修正只重建 Blackboard，不改已验证的 Runtime、Runner 或 Sandbox 镜像。

## 偏差与待决

- 原始模型配置与可验证费率须以现场盘点为准。遇到未映射旧任务或未确认请求，保全历史并阻止重新发送；不会为完成退役而填入假的能力或费用。
- 两站现场已切换，旧网关备份和原 PVC 只读保全。当前导入的原模型缺少可确认的密钥、上下文/输出限制和价格，须由操作者在平台模型页补齐后才可试发；有额度的任务还需要经确认的 USD 价目。已产生新写入时不以旧库覆盖回退。
- 本轮不物理删除用户原数据库、卷或历史账务。原工作区 104 项及默认 Git 索引保持原样，只有隔离本地分支更新，不推送。设计提交为 `Define operator model tests and gateway retirement`；产品、必要测试、生成物与本报告合为 `Retire AgentGateway and finish independent Pi operation`。
- 本地普通站点 [127.0.0.1:15800](http://127.0.0.1:15800/login)，原生站点 [localhost:15801](http://localhost:15801/login)。实际 Chrome 独立会话验证两个入口均返回 HTTP 200，不同主机名的会话 Cookie 互相隔离；只换端口、沿用同一主机名时 Cookie 会共享。验证只使用隔离浏览器中的合成值，不读取或写入用户登录会话。
