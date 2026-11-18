# P3h：Sandbox 工具与命令记录

## 完成内容

在保留资源回收提交 `d55c43a` 之上，把真实 Task 的原生 Virtual MCP 扩成 business 与 sandbox 两个目标。Pi 仍是唯一 Agent 循环。受信 Runtime 先核原 Sandbox 的 SID、PodUID、环境版本与执行上下文 nonce，启动同 Task Runner Pod 内的工具服务，并核该原 Pod 的工具服务就绪后才下发 Node 启动文件。启用此能力的部署使用 `node-sandbox-host-v1`，同时冻结 Node 与工具容器的真实 CID，停止须确认两者与原 Sandbox 进程退出；旧单容器记录保留原身份规则。部署来源、镜像或原 UID 漂移仍保守停止，不能用新版配置覆盖活的旧部署归属。

工具服务有独立 Secret，仅持本 Task 的官方 execd 访问材料、网关入站能力与 Blackboard 回写能力。两种平台能力用途分离：网关收到的入站密钥不能提交执行证据。Blackboard 使用既有加密机制保存 Task 密钥与含入站密钥的路由配置；不把生命周期租户密钥、服务密钥、上游模型供应商密钥或工具挂载交给 Node；Node 仍只持 Task/绑定范围的短控制、模型和 MCP 凭据。Pod 不共享进程命名空间。官方 secure-access 与 ingress 路由头保留，文件一律使用官方低权限 UID/GID 10000，Lead 也不提权。

已发布运行命令、查询状态、读取输出、中断、读取文件、写入文件六种工具。原 Task、当前 Runner epoch/PodUID、当前 Agent/Assignment、确认的 SDK journal 与原调用参数共同约束一次性准入。Worker 不能用模型输入替换 Agent、题目、Assignment、执行 ID 或原 Sandbox；状态、输出、中断从平台命令 ID 解析已保存的官方 ID。新执行紧贴 SDK 请求再次检查准入与硬截止，前台执行最多 45 秒；较长任务使用后台启动回执。中断收到 ACK 不等于进程树已排空。

现有 `runner_calls` 是唯一派发与结果账。SDK 的真实初始化 ID、输出和返回证据先持久化，再返回工具结果；回写针对原派发，不依赖原 Agent JWT 仍有效。如果原工具结果已保存，丢回复后可读原件；缺结果的调用保持未知，不能再发原命令。官方状态查询可补充同一命令的真实状态和退出码，保存本次查询结果；不会用合成结果替代缺失的原调用结果。任务已退出但没看到命令终回执时保留未知，不补退出码。Assignment 停止复用原启动器的 cgroup 封口/排空证据；短令牌、预算暂停不当作 Assignment 停止。未确认旧 scope 或旧调用结果不能换绑；本片拒绝带 Sandbox 工具的跨 epoch attach，不提前做 P4 接管。

命令记录保存 Task、Agent、角色、题目、原 Assignment、代次/epoch、请求及官方执行 ID、观察时间、状态、退出码和有界输出。命令原文由原调用的加密参数读取，不重复落一份明文命令。用户可在现有 CTF 工作台打开只读命令记录，按 Agent 筛选、分页并看详情；API 对 Worker 仅开放当前 Assignment 的记录。界面没有审查、风险判定或处置入口。

## 验证

- ` .venv/bin/pytest services/blackboard/tests/test_sandbox_commands_integration.py -q`：真实 PostgreSQL 最终合同 1 passed，6.65 秒。验证双用途密钥、真实 SDK 准入只能一次、无准入回写拒绝、原 ID 不可换、Worker 越界拒绝、未知不重派、预算暂停不排空、跨 epoch attach 拒绝、官方状态补记录但不解除旧调用未知、停止后仍保存原晚到结果及文本界限。SDK 身份为夹具，真实原生工具接缝另验。
- 私有 Kubernetes 1.34、固定 agentgateway 1.7.0、官方 OpenSandbox SDK 1.2.0 和真实 PG/BB/Node：干净候选 `joint/fresh-evidence.json` 六项均真。原生 Virtual MCP 经真实 Pi 完成 `write_file`、`read_file`、`exec_command`，四次模型响应均为本地脚本。实际命令执行 ID `97b39be0aa5e41b68e8d0165d720fe45`，输出 `p3h-command uid=10000` 及实际文件内容，退出 0；三项工具结果均保存，用户命令记录 API 可读。直接访问工具服务缺入站密钥时拒绝。原 Assignment 实际封闭排空，另有官方控制执行 ID `7313d742f6954d84b7ebfa11d6c00a23`；原 Node/工具容器两 CID 和 Sandbox 均确认退出，普通停止保留原两卷。
- 先行 Task `d3ec3ddb-1263-4098-93de-895fd9efa303` 已完成官方环境和工具服务身份就绪，但一次原生计费不可用使它持久暂停，未发送模型/工具。之前已有成功预算读回，后来只读原预算亦匹配；不能断言当时故障根因。保留原暂停与失败日志，同原 Task 的正常停止实际确认三个容器退出并保卷。只在此之后新增独立验证 Task `f37c3b67-cf77-4789-8f49-a419d342a289`，夹具先核原生管理健康、排空转发输出，未修改产品暂停策略或重派原调用。
- 资源/SDK 的 27 项定向单元检查通过；现有工作台的只读命令记录用一个独立界面夹具验证筛选、翻页和查看，8.3 秒，TypeScript 与 lint 通过。详细复现见 `deploy/kubernetes/p3h/README.md`。
- 对已正常停止的同一实证 Task 走用户 DELETE 和原 Runtime：新增工具 Secret 原 UID、两 PVC/PV、Namespace 和封口策略均读回消失，原生 MCP 路由与模型 key 退休；最终 Task、原资源/能力账及命令记录行均由现有 purge 删除。首次物理回收成功后，夹具缺对象存储使数据库删除停下并保留原账；补同 P3g 的空对象夹具后，沿原 Task/PG/清单只恢复 purge。未建第三个 Task、未发模型或命令、未手改账；另一已停 Task 不受影响。完整四项证据见 `joint/retirement-evidence.json`，保留首次只完成原生回收的原证据。
- 最终干净候选 `make check`：exit 0，2653 passed / 469 deselected，203.29 秒，ruff、pyright、Pi 检查通过。生成契约、OpenAPI 和 TypeScript，组合候选 `pnpm exec tsc --noEmit` 通过。首次只在表索引格式处停下，修正未改运行语义。真实主链之后另用最终 PG 合同覆盖状态查询只更新命令记录、不合成原结果；未重复真实 Pi/SDK 调用。

## 边界

这是平台通过 Sandbox 工具发起的命令记录，不是所有 shell 子命令或内核进程的全量审计。前台计数是 SDK 已观察文本的 UTF-8 字节；断线时可能只有已持久片段的下界。每流最多保存 64 KiB 文本；后台以官方合并输出游标为依据，不保证原 stdout/stderr 分流或原始字节保全。工具服务对后台读取响应设置 1 MiB 上界并拒绝压缩/超限，原生 execd 在自己端读取文件的方式没有改变。未知、截断和丢失不会补造输出或退出结果。

工具仍在同 Task 低权限共享工作区；当前可信验证镜像使用该 Task 的 `worker-a` 网络身份和 Assignment cgroup，未宣称逐 Agent 网络隔离、PCAP 或 eBPF 全量采集。不调用真实模型或靶场，不重复 P1/G1 的网络矩阵或费用探针，不打开公共 V1 启动。

## 提交划分

`Connect Task Sandbox tools and read-only command records`：可信工具服务与资源身份、原生 Virtual MCP 目标、一次性派发和记录、只读查询/界面、必要真实接缝及生成契约。正式设计正本未修改。
