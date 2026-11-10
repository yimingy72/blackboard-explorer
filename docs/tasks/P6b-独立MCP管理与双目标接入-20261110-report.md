# P6b · 独立 MCP 管理与双目标接入报告

本阶段在本地隔离候选交付 MCP Server 可视化与原生双目标接线，未部署到现场。父提交 `2c789edf9abc42a52e7f287871e0ebc9e47f861c` 单独记录了用户补充的 Native Lead/Worker 外部工具范围；其产品基线为 P6a `4ac69dcd0482fe103737d801871ac4ef365e13d6`。

## 完成项

1. **P6b.1 · 可视化管理与外部工具。** 平台账号进入 MCP Server 页。指定操作者可保存公开 HTTPS 地址和静态认证，测试连接、浏览由实际 MCP Server 发现的工具，再明确发布工具及适用角色。详情显示工具描述、参数类型/必填项、输入与输出 Schema、版本、连接状态和服务自报注解。发现不执行工具、不授予角色；最新启用草稿保留前一个可选发布版，停用后阻止新授权，已冻结任务继续原版本。普通 Explore 仍在可信 Runtime 运行；原生 CTF 新建任务为 Lead、Worker 分别选择，均可留空。发现与两条执行网络分别展示；Helper、普通 Derive/Close 和只读复盘不继承外部工具。
2. **P6b.2 · 直连与生命周期。** 原生 Pi 1.5 继续使用官方低层 MCP 客户端，经私网 Business-v2 与同 Pod Sandbox 两目标分别认证。新的冻结任务不发布旧网关路由；旧冻结任务不切换到新身份。业务入口只暴露当前 Task、角色和绑定允许的工具；外部工具用固定命名空间，不能覆盖 Blackboard/Sandbox 内置工具。可信 Blackboard 宿主用现有 Python MCP SDK、原 TLS/Host 和固定公网地址执行，外部 Server 只收到其静态认证和选定工具参数。新工具不进入旧 Flag 或自动 Lead 本机命令适配；当前 Assignment 约束平台调用人，远端资源权限由操作者配置的上游账号和服务端决定。没有将内部凭据或上游密钥交给模型。
3. **P6b.3 · 一次发送与兼容。** Native 通过原 invocation、许可、journal 和 Task 事务发送；读取远端 Schema 后复核当前许可，成功结果落盘后返回。原请求已执行但回执丢失时保留未知，同 Task 的另一角色或新绑定不能再发。普通 Runtime 同样在发送前用原 Agent 会话检查点做持久门槛，跨 Agent 也阻止新外部调用；只有相同 attempt、tool call ID 和原成功结果一同写入才能解除。原生恢复保留直连接入方式和授权快照；旧账、命令、抓包、原 Task 和网关路由未被重写。

## 验证

从任务候选目录运行，使用现有虚拟环境但显式指向候选源码。回执核对了 `bbx_contracts`、`bbx_objects`、`bbx_blackboard`、`bbx_runtime`、`bbx_envd`、`bbx_sandbox_execution`、`eval.runner` 的实际导入路径。

```bash
export PYTHONPATH="$PWD/packages/contracts/src:$PWD/packages/objects/src:$PWD/services/agent-runtime/src:$PWD/services/blackboard/src:$PWD/services/envd/src:$PWD/services/sandbox-execution/src:$PWD"
make check
.venv/bin/pytest -q services/blackboard/tests/test_p6b_mcp_integration.py services/blackboard/tests/test_native_recovery_integration.py services/blackboard/tests/test_store_integration.py::test_external_mcp_unknown_holds_a_new_agent_until_original_receipt services/blackboard/tests/test_native_ctf_publication_integration.py
(cd services/agent-runtime/task-runner && node --test --test-concurrency=1 test/mcp-direct.test.mjs test/runner.test.mjs)
pnpm --dir web e2e mcp-platform-catalog.spec.ts catalog-feedback.spec.ts --grep 'MCP|切换MCP|原生新任务' --workers=1
```

- `make check`：通过：Ruff / Pyright、Pi 脚本 5 项及 pytest 2578 项。首轮仅旧会话测试把窄查询硬编码为单列失败；现在校验 revision 加待确认标记仍不读取完整 session，CAS、脱敏和旧返回语义断言保留。定向复验通过。全套复验启动后，真实 PG 新增的恢复断言发现还需保留原外部工具快照；末次仅补此字段，并以 Ruff、Pyright、真实 PG 6 项验证最终字节。
- PG 定向 6 项通过。使用临时 PostgreSQL、真实 API 与实际 Python MCP 客户端，外部 Server 与 Kubernetes 资源观察是隔离合成输入。实际原生创建、初次双凭据、真实 Lead/Worker 绑定与外部调用、同名内置隔离、Schema 漂移拒绝、旧及交叉能力拒绝、跨角色未知保护和两种冻结路径恢复分别验证。新原生启动未访问 AgentGateway。
- Runtime 定向 68 项通过，后续官方 SDK 脱敏与外部发送回归 13 项通过；Chromium 3 项通过，覆盖草稿/发布/Schema 反馈、原生创建角色权限、默认无授权和窄屏。实际 Pi Runner 定向 7 项通过。
- 证据目录：原工作区 `.data/p6b-mcp-20261110/` 下的 `import-provenance.txt`、`core-pg.log`、`make-check.log`、`pi-runner-sdk.log`、`runtime-pytest.log`、`runtime-external-final.log` 和 `chromium-native.log`。无付费模型或真实外部工具执行。

## 偏差与待决

- 新 Native 接线由 Blackboard 的 `RUNNER_MCP_DIRECT_V2` 启用（默认关闭）；现冻结旧任务继续原路，外部工具只消费已明确冻结的清单；缺清单不视为通配授权。仅支持公开 HTTPS 和当前静态认证，未加私网、stdio、OAuth 或新客户端插件。需要未冻结的新任务从本阶段界面实际选用发布工具。现场是否可切换仍需两站私网入口、出站和清退证据，本阶段没有停用或删除 AgentGateway。
- 未创建新 Git worktree；继续使用 P6a 已隔离候选和独立索引。原工作区 HEAD、默认索引及原有 104 项维持原样。无新数据库表、依赖或锁文件；共享变更为 `.env.example` / Compose 直连选项、共享 MCP/CTF/Runner 合同，以及三份生成 API/Runner schema。
- 提交划分：设计 `Define external MCP grants for Native task roles` 已单独提交；产品、必要测试、生成合同和本报告合为 `Manage MCP servers and connect Native task roles directly`，仅更新本地 P6 分支，不推送。

下一步 P6c 先完成两站迁移与零 AgentGateway 启动验收，补独立模型测试入口，再按原计划退役旧网关资源并保留既有数据和账务。
