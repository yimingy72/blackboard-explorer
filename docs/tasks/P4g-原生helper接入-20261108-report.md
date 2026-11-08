# P4g：原生 Worker 前台 helper 接入

## 完成内容

- 新原生 Task 可在首次 launch 冻结启用前台 helper。当前主 Worker 通过模型工具循环委派一个有界局部目标，持久关联原 Assignment、父/子 binding、代次、题目范围与原调用。在同一 Task 锁下先处理可派主工作，再借剩余席位；无位立即返回。一个父只允许一个未清子；helper 借位会检查当前 epoch 的全部已注册和在途绑定，达到 32 条时拒绝新增 helper。原主派工规则不变，跨代次恢复仍有 P4f 的 manifest 上限。旧 Task 不热加能力。
- 前台实际调用锁定的 pi-subagents 公开工具，由可信宿主先供应同一个子 SDK 的模型、MCP 与独立 journal，经过原 journal 确认和模型网关。子工具只继承父已获准的 `read_file` 与前台 `exec_command`；固定角色路由及服务端范围校验。父原工具期限覆盖准备和执行，最长 60 秒且不超过原 Task/凭据期限。子短凭据仅在一次可信启动回执交给 Runner，不随结果返回。费用沿原 Task key 累计。
- 原调用重复只读回，已领取或结果未知不再启动。父等待结束不会证明子已结束。退出须核对同一插件 run、SDK、确认日志和 Sandbox 作用域；需要取消时先撤权，再等待独立退出/作用域正证，未知席位不复用。人工/预算暂停与跨 epoch 冻结捕获原子身份和日志；迟到可信结果可结算原父调用，不改写冻结席位，不投给新题。命令详情保持只读并显示 helper 与原父身份。

## 验证

- 干净候选 PostgreSQL：`.venv/bin/pytest services/blackboard/tests/test_native_helpers_integration.py services/blackboard/tests/test_native_worker_dispatch_integration.py -q -k 'native_helper or scope_revision or manual_pause or budget_pause'`，4 passed、3 deselected（13.55 秒）。验证主工作先拿空席、并发只发一次子启动、新旧 prepare 合同、禁止递归/后台命令/业务工具、暂停捕获原子 binding，以及原冻结身份和迟到结果精确对账。
- 真实 Pi SDK/锁定插件配本地假模型与 MCP：`npm --prefix services/agent-runtime/task-runner test` 对应完整 Node 10/10；最终前台与取消定向 2/2，部分绑定失败清理定向 1/1。三者单列，不合并计数。父模型通过真实注册工具触发 foreground child；子有独立会话与工具调用，终态 ACK 丢失后读回原结果；创建时撤权没有子模型发送。
- 固定 Agentgateway [v1.7.0 的 target condition](https://github.com/agentgateway/agentgateway/blob/26f5aebe99a36a037fe54f8080a608aca5547e15/crates/agentgateway/src/mcp/README.md#target-conditions) 用于在连接前排除 helper 的 Business target，保留业务服务端 403。新增实际运行配置缺条件的定向负例 1/1（0.72 秒）。用实际部署同一 Runner 镜像在 UID 1000、只读根文件系统内复核前台插件夹具；另在固定 Agentgateway 的独立条件路由验证实际 Lead/Worker/helper 目录 16/15/2 项，helper 未握手到 Business。两项是分段证据，不替代完整 Kubernetes Task；脱敏结果 `.data/p4g-native-helper-20261108/target-diagnostic/evidence.json`。
- 真实本地 Kubernetes 按 [P4g 探针说明](../../deploy/kubernetes/p4g/README.md) 运行第四次分阶段验证，exit 0、7/7。Task `25b0edd0-21c3-40b8-b251-aefd385d7567` 先确认 Worker 准备好，再进入 helper 阶段。同一 Task 中父和子各发送两次假模型调用，子用独立 SDK 和已确认 journal，在真实 Sandbox 以 UID 10000 执行命令。存储记录保留 helper 角色和父 Worker，子费用用原 Task key 并确认，子作用域封存后归还席位，父读回持久结果并继续。活跃子身份直接访问业务后端的 initialize/list/call 均返回 403。原 Runner、工具宿主和 Sandbox 正证退出，两个 PVC 保留。证据：`.data/p4g-native-helper-20261108/k8s-final/joint-final/evidence.json`。
- 前三次定向观察保留在 `.data/p4g-native-helper-20261108/k8s/`：初始 `joint/evidence.json` 到达原 helper 创建但上游 initialize 403，未发生子模型发送；原 Task 的 `joint/resume-evidence.json` 记录正证停止并保留卷。加目标条件后的 `joint-filtered/evidence.json` 因测试 Service 选择到两组网关 Pod，未发任何模型，不用于判断产品路由；`joint-filtered/resume-evidence.json` 记录该原 Task 正证停止并保留卷。选择器修正后的 `joint-scoped/evidence.json` 通过运行路由核对，Lead 完成建题与派工，Worker 获得 lease；但探针期限内无 Worker 模型或 helper 领取，原 owner 过期后保持冻结，没有续发；`joint-scoped/timeout-and-stop.json` 记录了原 Task 的退出确认。Worker journal 只到 header/binding，物理网关曾有 Worker 初始化及目录查询 200，但未保存可判定 prepare 具体停点的证据。这些早期观察不计入第四次 7/7。第三次未取得足以判定具体准备停点的证据。
- `make schemas openapi`、`pnpm --dir web types`、`pnpm --dir web exec tsc --noEmit`、OpenAPI 快照检查通过。干净候选 `make check`：2659 passed、479 deselected（210.58 秒，exit 0）；ruff、pyright 和 Pi 检查通过。

## 偏差与待决

只开放前台、每父一个有界 helper；后台/daemon、递归、通用子工作流和跨宿主内存续跑保持关闭。题目上下文由可信宿主注入当前范围内的题干、要求和 Fact 引用；没有开放 helper 通用 board 写入、Flag、Intent 关闭或命令处置，也不把 workspace 子目录当隔离边界。后台、20 席位压力矩阵和逐 Agent 物理网络归属未交付；没有调用真实模型或靶场，正式设计正本未改。只对可信 launch 明确开启的新 Task 发布；旧 Task 的路由与角色保持原合同。物理验证复用固定 Runner 镜像；其后补的 Worker 部分绑定失败清理由 Node 定向覆盖，不外推为同镜像已实测的故障场景。

## 提交与下一步

`Run scoped native foreground helpers`：前台宿主和固定补丁、事务借位与原父子控制、必要生成合同、证据和报告。GPT-6.1 Sol high 开发并经独立 Agent 复核。原 38 个 tracked 和 72 个 untracked 工作改动保留；仅本地提交，未推送。提交核对见 `.data/p4g-native-helper-20261108/commit-verification.json`。独立集群、数据库、测试网络、自建镜像和临时凭据已清理；四次证据保留，回执见 `.data/p4g-native-helper-20261108/k8s-final/cleanup-result.json`。

下一步单独验证后台 helper 的生产监督、父回合结束后存活及明确取消，再做 P4 席位并发和逐 Agent 物理网络归属。后台未过门槛前保持关闭。
