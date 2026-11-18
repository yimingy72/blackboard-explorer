# P3a：生产 SDK Task Runner 前半实现

日期：2026-11-06。前置：P1 Pi 接缝及完整后台增量已提交 `8b6fb68`。本批只实现已分配的 Node 生产模块、公开契约及必要 P1 反向引用，不把结果写成单题全链路已通过。

## 1. 完成内容

| 项目 | 实现 |
| --- | --- |
| 生产入口 | 新增 `services/agent-runtime/task-runner/`，可用 `npm start -- bootstrap.json` 启动。Pi coding-agent/ai/mcp 固定 1.5.0，只有 SDK 自身 Agent 循环；有限控制消费者只处理 prepare/input/abort/close/shutdown |
| 跨 Pod 协议 | 控制直接长轮询 Blackboard 持久命令入口，回执发送和 prompt 分开；模型准入接口只连接 Runtime proxy，工具只走 agentgateway MCP。没有把本地 stdio 当跨 Pod 通道 |
| 共享契约 | 新增 `packages/contracts/src/bbx_contracts/runner.py`；Node 使用由它生成的 JSON Schema 校验信任边界，不手工维护第二份消息定义。未改共享 exports/schema/auth/API |
| 业务版本 | 明确只接受 `challenge-board-v1`，prepare 返回 supported_business_contracts。与 P2b 对齐：声明不是授权，未来服务端仍须核 Task 冻结合同、真实 Runner/readiness 和 lease；缺标记旧 Task 仍走 legacy |
| ID 边界 | 平台 binding_id、platform_session_id、Pi 自动生成 sdk_session_id 分开。控制 request_id 只标识控制操作；业务 operation_id/submission_id 仍由黑板服务生成，原 platform call_id 保留关联；Node 不改写 MCP args.operation_id |
| 业务身份映射 | 已与 P2 作者对齐：binding.generation 是分配/身份版本，不是 legacy claim_turn 的业务成员 generation；后端按 binding+runtime epoch 映射有效 Principal 的真实 generation/turn_id，业务 turn 变化/旧回执不能重建 SDK 历史或续用旧 epoch 写入 |
| prepare 恢复 | 以 binding 定位固定 PVC journal，首次独占写入并同步 Pi 原生 header，再公开 open。ACK 丢失重启仍打开原 header/SDK ID；日志丢失、截断、版本或 ID 不符时拒绝，不创建替代历史 |
| PVC-first | 常规模型/工具确认只流式读取 offset 后增量，推进 seq/hash/leaf，再同步文件并确认元数据。没有 FileReceiptStore，也不增加每调用对象上传硬门槛 |
| 恢复完整性 | 恢复已有确认 journal 前，一次性流式验证确认前缀的 hash、seq、记录边界和 leaf，之后再交 SDK。早期行等长改写、尾锚不变仍拒绝；普通每轮不重新读/hash 历史 |
| 不确定确认 | journal ACK 不确定时保留原确认请求，先以完全相同 ID/游标核对或重发，再推进下一增量，避免旧 previous 游标接到已提交的新游标之后 |
| 并发持久顺序 | 固定本次范围并读取后，再 sync、提交该范围；sync 之后新追加不进入本次 cursor/leaf。排队确认自身可取消，执行前再查 signal，不丢原 pendingConfirmation/id |
| 人工输入 | 原生 sendCustomMessage 携带稳定 input_id；排队/返回只算 accepted，只有相应 native entry 已确认才发 applied。上限对齐原平台 20,000 字符，接收前核有效 UTF-8 和 JSON 编码不超过 128 KiB，避免合法输入落盘后才超 2 MiB 记录限制 |
| 会话隔离 | 每 Session 独立 ModelRuntime、MCP client/transport、凭据和 SDK call→可信 invocation 映射，无全局 current invocation 或 process.env 切换。可信 ID 关联原 native assistant entry |
| 权限与结果 | 仅注册获准的远端 MCP 工具，无本机四工具、stdio MCP、环境扩展、个人配置或通用 Node 执行。模型/工具共用冻结闩锁；工具结果须有匹配持久回执，未知不重放 |
| 提示词热更 | 每次模型边界按 binding 条件读取已发布提示词，丢通知仍可获取新版本；主回合应用当前版本，原生 system/版本标记确认后才发送。模型、工具、Skills、环境快照不扩权、不重放工具；Pi 压缩模板保留原用途 |
| 有效模型配置 | 可信快照包含当前 API、input 模态、reasoning、thinking_level/map；公开 SDK 按值构造且核对实际 thinking level。不支持 API/显式思考预算等配置拒绝，不静默降级。本地 SDK cost 是中性展示值，不替代平台 CNY 账本 |
| 短许可续期 | lease_expires_at 明确为短期会话许可，与 RuntimeLease/Task deadline 分开。control poll 可返回受信 lease_renewals，核当前 fence、完整 binding/Assignment、SDK ID、单调 revision 后重设 timer；已过期不能迟到自复活 |
| 有限关闭 | close(binding_id) 先放进程内终态标记，再停止进行中的 prepare/SDK；有界确认后保留 PVC journal、游标、原 pending_confirmation 和未决引用，等待 SDK 的收尾确认后释放 MCP/SDK/FD。sessions 与已完成 preparing Promise 不再保留历史，迟到 prepare/input 拒绝、合法旧 renew 不复活；单个 MCP close 失败不跳过其余清理且回执如实 rejected。该本地证据不宣称远端清理或 Worker 席位释放 |
| 长工具等待 | 初始化/目录读取使用短元数据超时，已准入 tools/call 按平台 Permit 的 authorized_wait_until_ms 通过官方 timeoutMs 等待；模型不能延长期限，超时仍 unknown，不能认定远端已停 |
| 受限凭据 | 挂载仅允许 runner-control/model/mcp audience，核 Task/binding/到期；混入 service audience 拒绝。命令保存 credential_ref，Token 不进入命令、journal、回执或启动参数 |
| P1 复用 | 已审模型 StreamFn/错误流/冻结逻辑提取到生产 model-boundary，P1 反向引用。P1 全读 hash 与文件 fixture 存储继续留在测试目录，没有搬进生产 |

## 2. 摘要与回执合同

采用原文/原字节 SHA-256，不定义半套跨语言 canonical JSON。

- 控制只传一份 `payload_json` 文本及 `payload_digest`；解析后按 RunnerPayload 校验。持久队列必须保存并重发原文，不能先解析再序列化。
- 工具准入保留 `arguments_json`，结果提交/恢复保留 `result_json`；各自摘要覆盖该文本的确切 UTF-8 字节。工具参数/结果必须为 JSON object；数值表示、属性顺序或 Unicode 转义等价不意味着摘要相同。
- 控制 ACK 的 receipt_digest、模型 ACK 的 request_digest 覆盖实际 HTTP 请求 body 原始字节，不对补默认字段后的 DTO 再序列化求摘要。
- journal 哈希链覆盖原生 JSONL 记录的原始字节（含换行）；恢复核对确认前缀，不使用另一份 Python history。

Python `runner_json_text` 和 Node `jsonText` 仅提供各自标准序列化。互验覆盖 `1.0`/`1`、`1e-07`/`1e-7`、数字形键、Unicode 与负零表示；双方验证保留原文，不假定序列化结果相同。这些是命令/调用表示和必要结果回执，不新增重复会话历史。

## 3. 实际验证

在仓库根目录：

```sh
cd services/agent-runtime/task-runner
npm ci --ignore-scripts --no-audit --no-fund
node scripts/generate-protocol.mjs
npm test
```

必要场景 **4/4 通过**，没有扩展真实模型矩阵：

1. **PVC 增量与恢复故障**：正常确认读取量等于新增字节；恢复先校验确认前缀，随后普通确认不重读前缀。确认前缀早期记录等长改写、header/尾锚不变时，SDK 创建前拒绝。截断/丢卷不重建替代 journal，不修改已损坏文件。
2. **Python↔Node 原文摘要**：两个方向互验 Unicode、数字形键、小数/指数和负零 JSON 表示，证明不依赖重序列化；并检查原平台最大输入及超限拒绝。
3. **真实独立 Node 控制**：prepare 回执落库后断连接，再 SIGKILL 已拥有的测试进程；重启同 binding 保留原 sdk_session_id，只有一份 JSONL。20,000 个控制字符可合法落盘；journal ACK 挂起期间只 accepted、零模型发送，poll 仍能收到 abort，明确返回 sdk_idle=true/journal_confirmed=false；旧 epoch 输入拒绝。
4. **两个真实 SDK Session 并发**：同名工具、相同参数和相同 SDK tool_call_id，同时进入两个独立 MCP transport，可信 invocation/SDK ID/凭据不串。一次未发控制通知的旧→新 prompt 更新在下次主模型调用生效，工具始终只有 remote_echo、没有重放；high reasoning 实际进入 reasoning_effort，输入能力快照保留。短许可连续续期跨过初次到期继续运行；中止一个会话后，它的合法迟到续期不杀 Task Runner，另一个会话继续产生正常模型结果；过期合法续期不复活，真正 fence 错误仍拒绝。同场景把元数据超时降为 500ms，而受信工具 Permit 允许 10 秒；MCP 等待超过该短阈值仍在 Permit 内完成，未实际等待 15 秒或新增业务矩阵。

所有平台、模型代理和 MCP 服务均为随机本地端口假 HTTP 服务；独立进程场景启动真实生产入口，双会话场景直接使用生产 TaskRunner 类及 Pi 1.5.0 SDK 以检查实际资源，没有把脚本化消息循环当执行器。接收端 fixture 每次模型/工具准入都核当前 fence、binding 与许可；真实数据库中的 RuntimeLease/Assignment/预算事务尚待后半接入。

独立审核后修复四个实际窗口，仅重跑受影响的 journal/控制/双会话场景 **3/3 通过**：注入 sync 后追加，确认只覆盖已同步固定范围且下一轮接新记录；旧确认挂起时取消排队调用立即返回，排队项稍后不能执行且原 pending ID 保留；合法迟到 renew 隔离到本 binding，健康会话继续；缺失和非数字 expires_at 都拒绝。没有扩展业务矩阵。

收尾补上有限 close，并按独审意见修复两个边界：SDK open 前只读验证未确认尾部，完整 JSON 缺最后换行和截断坏 JSON 均拒绝且原字节不变；fence 检查先于幂等缓存，已知 request_id 的旧 epoch 不返回成功或覆盖当前操作。close 确认超时保留原 request_id/cursor；已观察到的 unknown 模型/工具引用保留；确认队列与 SDK 收尾结束后才关 FD；prepare 进行中先 close 不得晚插 sessions；重复 close、关闭后迟到 prepare/renew 不复活，另一会话仍完成模型调用；注入 MCP close 失败仍释放 SDK/FD，回执 rejected。只重跑下列受影响的 **3/3**（约 18 秒），独立定点复核通过：

```sh
cd services/agent-runtime/task-runner
node scripts/generate-protocol.mjs
node --test --test-concurrency=1 test/journal.test.mjs test/runner.test.mjs
```

根定点审核再确认一个准备期窗口：native SDK ID 已产生、首次确认尚未返回时，平台合法旧许可仍可能 sdk_session_id:null。现先严格核 fence、完整 binding 和 request_id，再忽略旧 null，不延长开放会话许可；closed 标记保留 actual SDK ID，同时忽略准备期旧 null，冲突非空 ID 仍拒绝。将原场景的 prepare 等待点移动到 MCP tools/list（native header 已有、SDK 尚未创建），关闭后发送原 null 许可，另断言开放会话旧 null 不推进 lease。仅重跑双会话 **1/1 通过**（约 8.5 秒），没有新增业务矩阵：

```sh
node --test --test-concurrency=1 --test-name-pattern='two native sessions' test/runner.test.mjs
```

根门禁使用 `9af4802be1a9ce7fe316b9f354fd4c1587adebb2` 的干净归档加本批完整 20 文件，未混入 P3b 或其他 WIP；复用已安装依赖，并显式设置候选源码 PYTHONPATH，验证五个 Python 包均从候选导入。`make check` 一次运行退出 **0**（约 204 秒）：ruff format **469 文件**、lint、pyright **0 errors / 0 warnings**，legacy Pi **5/5**，pytest **2614 passed / 450 deselected**（约 179 秒）。日志保存在 `/private/var/folders/y7/cm4yy73d40x660zp12pg71_h0000gn/T/bbx-p3a-9af4802-4b1ct56s/p3a-make-check.log`。

该门禁运行期间补的准备期续期修正仅涉及本批 Runner 的两个 JS 源文件和两个测试文件，随后补文档说明；root make check 覆盖的 Python/legacy Pi 及配置均保持原哈希，提交前仅删除 model-boundary 末尾多余空行，P1 接缝未再改。按根审核要求复用本次根门禁，以以上双会话定点结果覆盖新增分支，未重复整套检查。

最终冻结后，对提取/修改的 P1 gateway 调用方做完整模块定点回归：

```sh
cd services/agent-runtime/pi-seam
node --test --test-concurrency=1 test/gateway.test.mjs
```

结果 **8/8 通过**（约 2 秒），保留主/两类压缩、零发送拒绝、unknown 费用、结果保存失败冻结及可信 invocation 的原不变量；没有重复未变的 P1 daemon/插件/基础设施矩阵。

新增公开 Python 文件的 ruff lint、format check、pyright standard 均通过；本批共 20 个文件（含 task-runner/.gitignore，仅忽略依赖和测试产物），收尾无自有 Runner 测试进程残留。未调用真实模型、靶场、Docker/Kubernetes，未读 `.env` 或真实密钥，未全局安装，未暂存或提交。

## 4. 本批未改动与后续接线

未改 P2 正在占用的 schema/migration/auth/API/exports，未改 Runtime supervisor/coordinator，未升级 legacy Pi 1.4.0 锁。没有新增外部队列、通用执行服务或第二 Agent 循环。

下一批需要由主代理分配共享文件后接通：

1. Blackboard 持久有限命令、原始 payload_json/幂等回执、binding/SDK ID、journal CAS、现有输入 claim→applied 同事务。
2. 真实 Runner 授权、RuntimeLease/Assignment fence、短许可续期和固定 Task deadline；不能只凭 supported_business_contracts 或 Node sdk_idle 放行/回收。
   close 终态标记目前是进程内状态；跨进程必须用后端持久 binding/命令权威拒绝已关闭身份重新 acquire，不能仅依靠 Node 本地缓存。
3. Runtime 模型 HTTP 准入 proxy、逐 attempt 预留和 CNY 结算；服务端每次核实时租约/预算，不能用客户端长截止时间或 SDK cost 代替。
4. 业务 MCP/沙箱 MCP 的 invocation 准入、结果读取/确认、命令审计与独立 cleaner，复用现有 submission/execution 权威；unknown 上报不得覆盖已完成结果。
5. Task Runner 镜像、受保护单写者 PVC/Secret、网络策略、真实 readiness、对象异步备份/归档与会话分页读取。卷丢失冻结核对，不能静默恢复旧备份后继续。

本段不自动恢复已经发送、结果未知的模型/工具，不将尾部有效记录等同外部账本已核清；复盘、Skills 资源、附件上下文及更多 provider 路线仍需按固定发布能力接入。生产 helpers 保持关闭。

本批可审核的主文件为新 runner.py、task-runner 全目录、P1 gateway-session 的必要反向引用和本报告。建议提交信息：`feat(runtime): add fenced Pi SDK task runner transport`；提交仍由主任务完成。
