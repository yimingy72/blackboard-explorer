# P1 / G3 Pi 接缝验证

这是独立接缝样例，不是生产执行器。生产 `../pi`、Python 调度、根依赖和现有 WIP 均未修改。

固定版本为 Pi coding-agent / ai / agent-core **1.5.0**，已发布的 pi-subagents **0.85.0**，以及其源码提交 **b6647222bd2e849021054292927ef39fea0793ed**。独立 `package-lock.json` 固定 npm 包、源码 tarball 和完整性摘要。源码别名仅用于隔离补丁验证。

运行：

```sh
cd services/agent-runtime/pi-seam
npm ci --ignore-scripts --no-audit --no-fund
npm test
npm run test:daemon
```

安装只在本目录进行。测试全部使用本地随机端口假 HTTP 服务，不调用真实模型，不启动 Docker/Kubernetes，不读取 `.env` 或用户 Pi 配置。后台 fixture 只继承测试必需的环境变量。安装阶段需要获取固定依赖；安装完成后的测试无需外网。

## 主会话边界

`src/gateway-session.mjs` 使用公开 SDK 创建真实 `AgentSession`，禁止环境扩展、Skills、上下文文件和本机内置工具。只开放显式 `customTools`，模型固定为受限网关 provider。

主回合和 Pi 默认手动/自动压缩共用 `session.agent.streamFunction`。包装器先同步 Pi journal 和父目录、确认持久游标，再调用捕获的原 StreamFn；确认失败锁定当前边界，后续输入不能继续发送。普通 `subscribe` 只用于观测，不能承担持久屏障。

替换函数不会继承 Pi 私有的 ModelRuntime 认证标记。本样例不深导入 `markRuntimeStreamFunction`；它清除压缩传来的预解析认证，并在最后 HTTP transport 固定网关地址、独立调用身份、认证头和禁止重定向策略。未获准的模型或目的地址直接拒绝。关闭 cache warmer、Pi 自动重试、摘要重试、provider 重试；transport 同时拒绝同一 call 的第二次发送。

最终 HTTP 请求才分配 attempt 并调用 `admit`，与 wrapper 共用 call/attempt。假网关核对已有预留，不再次预留。每次实际发送只有一个 attempt，429、503、断流和取消不被解释为零费用。公开 provider stream hook 观察原始 usage；正常 done 未提供可信 usage 时也保留 `usage: null / unknown`。StreamFn 失败编码为 error/aborted 事件及终态，不向调用者 throw/reject。结算失败或非协议 stream 异常锁定当前边界。

工具入口使用可等待的 `execute`：确认 journal → 原子保存 invocation 的发送意图 → 执行 → 同步保存结果 → 交回 Pi。invocation 来自可信 Task、Pi session、持久 assistant entry、工具名与 tool ID；不同 turn 重用供应商 tool ID 不会复用旧结果。恢复同一 invocation 返回已保存结果；已发送但无结果、缺失回执或参数冲突均拒绝重放。工具与模型共享当前会话的冻结闩锁：确认/存储失败或副作用结果 unknown 后，同批后续工具、新 invocation 和模型入口都被拦住；正常返回并持久保存的已知业务拒绝不冻结。恢复已保存结果须由核对后的新宿主绑定重新接入，不解除原会话闩锁。

## 子插件补丁

官方 0.85.0 的公开 API 尚不接受 `sessionHost`，其 `ChildSessionFactory` 仍是内部模块。因此 `helperModes.foreground` 和 `.background` 均为 **false**；本目录也未被生产路径导入。

`patches/pi-subagents-0.85.0-session-host.patch` 是三文件的最小上游提案：在公开 required-child-extension 描述中增加 `sessionHost: true`，要求 `requireForAllRunners: true`，并沿用既有快照传到后台。对应可信模块必须导出：

```ts
export async function installChildSessionHost({ session, run }) {
  // Install host-owned stream/tool admission on this raw AgentSession.
}
```

真实 child factory 在 `createAgentSession` 之后、`session_start`、MCP readiness 和首次 prompt 之前等待安装；缺失或失败立即 dispose 并拒绝启动。启动、模块载入后、每次安装完成后和 child 发布前均核验 factory 代次及取消信号；安装挂起期间被 dispose/abort 的会话不能发布，已安装的宿主通过 shutdown 清理。宿主模块不由模型指定。补丁仅复制并应用到被忽略的 `.fixture/pi-subagents/`；不改 npm 原包、不改变生产依赖，也未向上游提交。

已审的 13 例基线调用补丁源码的真实 foreground factory、foreground launch builder 和 background runner launch builder；后台承载于独立 Node 子进程。基线验证日志确认、工具继承、父会话结束后的存活、supervisor 撤权、取消、迟到输入和明确退出。为触达尚未公开的安装位置，**只有这部分隔离测试 fixture 导入内部源码**。后续完整 daemon 增量独立列在下节，不把旧 factory 测试当作完整插件路径。

## 本地候选包与完整 daemon 增量

`npm run build:patched` 将锁定源码复制、应用已审 patch，用固定 TypeScript 5.9.3 转译，并生成 `.fixture/pi-subagents-0.85.0-bbx.p1.tgz`。这是明确标记的本地私有候选包，包含原始扩展、公开 exports、真实后台 bootstrap/daemon 和 watcher，未改官方 npm 包。`bbx-patch.json` 记录源提交、patch SHA-256、编译器及各输出文件摘要；`.fixture/patched-artifact.json` 记录 tgz 完整性。两次构建得到相同 tgz 摘要。构建只做语法转译，不冒称上游完整类型检查通过。

`npm run test:daemon` 先构建候选包，再只运行两个完整路径场景。它加载候选包的公开扩展入口及公开 required-extension/ceiling API，通过 SDK 公开 `getToolDefinition("subagent").execute(...)` 和 `createToolContext(...)` 调用真实注册工具。调用由宿主直接发起，未验证完整父模型生成 subagent 调用及其原生工具循环。插件把 subagent 声明为 model-only，嵌套 `ctx.executeTool` 按设计不可调用它；本增量不声称绕过这一公开限制，也不使用私有 child factory。

1. 正常后台闭环：实际 subagent 工具启动原始 `async-execution → subagent-runner-bootstrap → subagent-runner`，受限 helper 调用一次假远端工具，真实 result watcher/notify 交付一次完成消息，父会话经过同一模型准入唤醒一次，取得原始 process-terminal `observed` 退出证明。
2. 取消/失联：假远端副作用已经完成但回执挂起；宿主撤销父子模型/工具准入、停止父会话并调用公开 subagent stop。真实 daemon 取消后交付结果通知并退出，原 invocation 仍为 sent/unknown；公开 resume 明确拒绝重放已停止 run，未出现第二次副作用、子模型发送或停止父会话的新模型发送。

两个新增场景使用干净父进程环境和本地 HTTP 限制，后台进程复用插件实际启动路径。原 13 例已审基线与这 2 例增量分别验证。helper 生产 gate 仍为 false。

另有一个失败清理回归：公开 stop 失败且终止证明属于其他 run/Runner 时，teardown 抛错并保留目录。清理不按旧 PID 直接发信号，只调用插件公开 stop，再有界等待与启动时 run ID、Runner instance 均匹配的 observed 关闭证明；超时不视为成功。`test:daemon` 因而包含 2 个业务场景与 1 个清理故障场景。

## 生产接入前的边界

- journal 目前为接缝故障验证而全读、全 hash；生产须接原有持久确认事务，改成增量游标/哈希链，保持 Pi 为内容权威，并验证目标存储及单写者租约。
- `FileReceiptStore` 是小型故障 fixture；生产须接已有 invocation/预算事务、epoch、Assignment、generation 和可证明费用上界。样例验证只预留一次，不宣称实现了严格金额预算。
- HTTP 服务模拟模型网关及准入，未运行真实 agentgateway v1.7.0；实际部署仍须单独验证其认证、路由及无重试配置。
- 当前锁定补丁候选包已经验证正常与取消两条真实 daemon/watcher 路线；外部 Runner、并发后台工作流、跨宿主恢复、完整 Kubernetes Task supervisor 等仍未验证。进入生产须使用经明确审核的锁定构建并接任务租约/预算/命令清理，当前未发布 helper 能力。

完整结果见 [P1 报告](../../../docs/tasks/P1-Pi接缝验证-20261106-report.md)。
