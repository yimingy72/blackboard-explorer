# Task Runner — P3a 生产接入模块

本目录是生产 Node 入口，Pi SDK 是唯一 Agent 循环。控制直接连接 Blackboard 持久命令入口，模型连接 Runtime 准入代理，工具只连接 agentgateway 后的 MCP。没有本机 bash/read/write/edit、stdio MCP、动态扩展、个人配置或通用执行接口。

本批完成 Runner 侧和可插入 HTTP 回执接口。Blackboard 的数据库事务、鉴权/lease、Runtime 模型代理及部署接线尚未合入，因此目前不能宣称单题全链路已完成。

```sh
cd services/agent-runtime/task-runner
npm ci --ignore-scripts --no-audit --no-fund
npm test
npm start -- /mounted/runner-bootstrap.json
```

只用独立锁定的 Pi coding-agent/ai/mcp 1.5.0；不升级 legacy `../pi` 的 1.4.0。公开协议正本是 `packages/contracts/src/bbx_contracts/runner.py`，本目录 `protocol.schema.json` 由正本生成，Node 在信任边界执行同一 JSON Schema。开发时在本目录运行 `node scripts/generate-protocol.mjs` 重新生成；生产镜像不需要 Python。

## 控制与身份

bootstrap 含 `fence`、Blackboard/模型代理/MCP 地址、`control_credential_ref`、受保护凭据挂载路径及 PVC 日志根目录，字段以 schema 为准。控制命令含 `protocol、request_id、command_seq、fence、payload_digest、payload_json`，只允许 prepare/input/abort/close/shutdown。payload_json 是一次序列化后保留的原文，解码后另按公开 RunnerPayload schema 校验。fence 检查先于幂等回执缓存，旧 epoch 不能取回当前成功回执，也不能覆盖当前操作状态。

摘要合同是确切 UTF-8 字节，不是跨语言 canonical JSON：payload_digest 覆盖原 payload_json；工具 arguments_json/result_json 同样保留原文并对原文求 SHA-256，接收端不能重新序列化再验签。数字形键顺序、1.0/1、1e-07/1e-7、负零或 Unicode 转义不同都可以产生不同摘要。Python runner_json_text 与 Node jsonText 只提供各自标准序列化，原文一经入命令必须重发原文。模型/控制 ACK 中的 request_digest/receipt_digest 则覆盖实际 HTTP 请求 body 的原始 UTF-8 字节。prepare 快照摘要取原控制 payload 文本；更新提示词走独立发布版本接口，不重写原 prepare。

`binding_id`、`platform_session_id` 和 Pi 自动生成的 `sdk_session_id` 分开。journal 路径由安全的 binding ID 确定；首次 prepare 独占创建并同步 Pi 原生 header，随后通过公开 SessionManager.open 载入。重发或进程重启重新打开同一文件；不是创建另一份 SDK 会话内容。原日志缺失、截断、header 版本或 SDK ID 不符时拒绝恢复，不创建替代会话或回退旧备份。

Runner 只接受显式 `business_contract: challenge-board-v1`，prepare 回执声明编译支持列表。该声明是能力信息；后续服务端还须核 Task 冻结合同、可信 Runner/readiness、当前 lease，不能仅凭调用方字符串放行。缺合同的旧 Task 由 legacy 路径处理。

控制 request_id 只标识控制操作。业务 operation_id/submission_id 仍由黑板服务稳定生成，platform call_id 保留关联；Runner 的模型 call/attempt 和 MCP invocation 只追踪调用，不生成业务提交/执行权威，也不改写 MCP args.operation_id。

binding.generation 是分配/身份版本，不能当作 legacy CtfMember 每次 claim_turn 推进的 generation。Node 只携平台 binding 引用；后端须按当前 binding 与 runtime epoch 映射并核对真实业务 Principal 的 generation/turn_id。业务 turn 推进或旧回执结算不能改写原 prepare、重建 SDK 历史，也不能让旧 epoch 凭据继续写。

凭据只从受保护挂载按 credential_ref 解析，audience 限定 runner-control/model/mcp，并核 Task、binding 和有限数值的未来到期时间；缺失/非数字 expires_at 拒绝。混入 service audience 的挂载拒绝使用；Token 不进入命令、journal、回执或启动参数。每个会话创建自己的 ModelRuntime、MCP client/transport 和 SDK-call→invocation 映射，无全局 current invocation 或 process.env 会话切换。

## 平台接缝

下列路径相对 bootstrap 中的对应 base URL。它们是本批确定的客户端合同，真实后端实现留待 P3b 接入。

| 所属入口 | 请求 |
| --- | --- |
| Blackboard | `GET /commands?after_seq&runner_id&epoch&pod_uid` |
| Blackboard | `POST /commands/{request_id}/receipts` |
| Blackboard | `POST /bindings/{binding_id}/acquire` |
| Blackboard | `POST /bindings/{binding_id}/journal/confirm` |
| Blackboard | `GET /bindings/{binding_id}/prompt?known_revision`，未变更可返回 304 |
| Blackboard | `POST /bindings/{binding_id}/tools/admit` |
| Blackboard | `POST /bindings/{binding_id}/tools/{invocation_id}/result` 或 `/unknown` |
| Runtime 模型代理 | `POST /attempts/admit`、原生 `/chat/completions`、`POST /attempts/{attempt_id}/receipt` |
| agentgateway MCP | 标准 initialize、tools/list、tools/call、取消及关闭 |

控制轮询、命令处理与回执发送分开；prompt 或 journal 确认等待不会阻止收到 abort。回执丢失显式重发原 request ID，不新建操作。input 先记录控制意图，使用公开 sendCustomMessage 在原生日志写入稳定 input_id；返回或排队成功只算 accepted，只有对应 native entry 的游标确认后才发送 applied。

input 沿现有平台上限为 20,000 个字符，并在接收前检查原文有效 UTF-8 和 JSON 编码不超过 128 KiB；最坏控制字符转义也远低于 native journal 的 2 MiB 单记录上限。不会先 accepted 再因可预测的编码膨胀失败。

SessionSpec 携带可信有效 model 快照：当前仅 OpenAI-completions、text/image 模态、reasoning、thinking_level 与 thinking_level_map。公开 SDK 使用所选值，若 SDK 不能原样满足或传入本路线不支持的显式 thinking_budgets，则拒绝 prepare。SDK cost 固定为计数展示中性值，不能作为平台 CNY 账本。

角色提示词每次主模型边界都按 binding 条件读取最新已发布版本，丢通知仍可发现更新；新版本写入原生 system 消息/版本标记并确认后才发送本次请求。只替换 bbx_role 指令，不改模型、工具、Skills 或环境，不重放工具；模型准入记录固定 profile_revision 和动态 prompt_revision。Pi 自身压缩模板保持其用途，仍经过同一模型/日志准入。

模型前、工具前都确认 journal；错误和 unknown 共用会话冻结闩锁。工具 invocation 从 Task/binding、SDK session、持久 assistant entry、原 tool_call_id 和工具名派生，经平台准入后放进 MCP 的独立 `_meta`，不依赖全局 current。工具结果须有匹配的平台持久回执才交回 Pi；未知状态保持冻结，不静默重放。模型和工具隐式重试、预热均关闭。

MCP 初始化/目录读取使用独立短元数据超时（bootstrap 默认 15 秒）；tools/call 则使用平台 Permit 的 authorized_wait_until_ms，通过官方 McpRequestOptions.timeoutMs 等待剩余获准时间。期限由后端结合任务截止时间计算，模型参数不能延长它。abort 仍可独立取消等待；等待超时只表示结果 unknown，不证明远端命令已停止。

abort 关闭新准入、清空未应用队列、等待 SDK 停下，再做有界 journal 确认。回执区分 sdk_idle 与 journal_confirmed，列出本进程观察到的未决调用；空列表不证明平台账本或远程命令已全部清理。cleaner/模型代理/业务 MCP 仍须核对各自权威记录。

close(binding_id) 在等待进行中的 prepare 前先放置进程内终态标记，随后沿 abort 路径停止并有界确认，保留 PVC journal、确认游标、原 pending_confirmation 和未决模型/工具引用。SDK 的收尾确认完全结束后才释放 journal FD；MCP 关闭失败仍执行 SDK dispose 和 FD 关闭，并返回 rejected。完成后移除 sessions/preparing 的历史、凭据引用和连接，只留下轻量身份及回执摘要；重复 close 幂等，迟到 prepare/input 拒绝，合法同身份 renew 忽略。该本地关闭不证明远端命令已清理或 Worker 席位可释放，跨进程关闭权威仍须由后端持久命令与 binding/fence 接通。

## PVC-first 日志

硬持久边界是受保护单写者 PVC：先固定本次文件范围，只读取 confirmed offset 后至该边界的完整 JSONL 记录，按原始字节推进 seq/hash/leaf，再同步文件并提交该范围的元数据。sync 期间或之后追加的记录留给下一轮，不能把未同步新数据计入确认。seq 包含 header。平台不接收整段历史内容，确认不要求同步上传对象存储；对象备份/归档是后续异步职责。

元数据 ACK 不确定时，先重发完全相同的确认 request_id/游标，再推进后续增量。恢复已有确认日志时，在交给 SDK 前一次性流式校验整个确认前缀的 seq/hash/记录边界和 leaf，并只读校验未确认尾部的 UTF-8、JSON、换行边界和记录大小，坏尾不让 SDK 修补或跳过；普通模型/工具确认仍只读取增量，不能每轮重读历史。即使早期记录等长改写且尾锚不变，恢复也拒绝。运行中检查 inode、大小与完整记录，拒绝替换、截断及丢卷。该策略依赖受保护不可由求解工具修改的 PVC、平台单写者 lease 和底层存储完整性。

确认在队列中的等待本身也响应 AbortSignal，真正执行前再次检查取消状态；取消的排队项不能稍后执行。它不会丢弃正在对账的原 pendingConfirmation/request_id，后续仍核对原写入。

生产没有 FileReceiptStore。平台 acquisition/确认/工具/模型事务是可插入接口；目前假 HTTP 服务仅在 test 中提供这些回执。

lease_expires_at 是短期会话运行许可，不是 Task deadline 或全局 RuntimeLease。控制 poll 可返回受信 lease_renewals；只在原许可仍有效、fence、完整 binding/Assignment、SDK ID 一致且 lease_revision 单调时延长并重设 timer。SDK ID 已知后收到准备期 sdk_session_id:null 的旧许可直接忽略，不能延长开放会话；关闭标记也只忽略该 null 或匹配已知 SDK ID 的续期，冲突非空 SDK ID 仍拒绝。已中止/关闭/过期/冻结 binding 的同身份迟到续期不复活，也不终止健康会话；真正 fence/身份不符仍拒绝。每次模型/工具仍分别调用权威准入，后端必须核实时 RuntimeLease/Assignment、预算和硬截止时间；Node 许可不代替这些事务。

## 本批必要验证

`npm test` 只有四个必要场景：恢复前缀/坏尾完整性、普通增量与丢卷拒绝；Python↔Node JSON 文本摘要互验；独立进程 prepare ACK 丢失重启、原平台最大输入、确认及 abort/close；两个真实会话并发身份隔离，同时验证 prompt 热更、非默认 high reasoning、短许可续期/过期拒绝复活、受信工具等待和逐 binding 资源释放。双会话场景在测试进程直接使用生产 TaskRunner 类，便于核 SDK/MCP/FD 和进行中的 prepare 已释放；独立进程场景仍启动真实生产入口。全部模型、MCP 与平台回执使用本地随机端口假服务，不依赖真实模型、靶场或 Docker；摘要互验需要开发工作区已安装的 uv/Python 依赖。

P1 的模型边界已改为引用本目录生产实现；其全读 hash、FileReceiptStore 仍只保留在 P1 测试 fixture。完整阶段说明见 [P3a 报告](../../../docs/tasks/P3a-SDK-Runner-20261106-report.md)。
