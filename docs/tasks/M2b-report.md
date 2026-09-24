# M2b · 单 Agent 跑通报告

日期：2026-09-24。

## 完成内容

- A.1：explore / derive / close 分别装配 7 / 4 / 4 个黑板工具，仅 explore 附加 MCP execute_command。事实直接提交，Agent 根据快照/增量自行判重；证据 stat→读取→SHA-256 路径→MinIO→黑板，50 MiB 双重上限，系统自动附 command_output。模型输入不接受 uri/auto；读取工具按 Agent 身份访问并转义不可信内容。final close 先上传固定报告 key。
- A.2：ToolLog→GraceGate 为函数中间件外到内顺序；完整工具结果落存储、追加统一 call_id，失败也记录。GraceGate 仅放行三种交接工具，服务端原子扣减成功返回 0 仍允许本次执行。BoardSync 保留点名/裁定全文、其他事件摘要/计数，过滤自身普通事件，conclude 指令注入一次；采用 M2a 验证的公开 Content 追加退路，所有角色心跳与用量入账。
- A.3：固定 profile.prompt_templates 正文渲染；Jinja sandbox + StrictUndefined，模板只收到普通数据。种子仅 L0；非种子 L1/L2；derive 含完整事实/意图和上次 excluded；close 含声明事实、快照和历史裁定。三份中文模板落地。
- A.4：按 ExploreReceipt / DeriveReceipt / 新增 CloseReceipt / RefusalReceipt 校验最后一个完整 JSON 对象；不规范格式返回规定的成功兜底并保留 raw_text，回执不替代工具提交。
- A.5：AgentRunner 读取固定版本配置，显式 session，拥有的 MCP HTTP 客户端 trust_env=False；正常/拒绝/错误/取消均调用 finish。finish 与资源关闭在独立受保护协程完成，二次取消也先等收尾。模型 HTTP 请求设置至多 120 秒超时，整个 Agent.run 受任务时长保护；CLI 支持 run-agent/conclude/state；只读/控制命令不需要模型、MinIO 或 envd 密钥；final 必须已 closing。
- A.6：脚本模型与 FakeEnvd 普通测试覆盖证据、工具、中间件、模板、回执、CLI 和取消；真实 PostgreSQL+MinIO+黑板 API 集成验证完整种子工具循环。

## 实际检查

- `uv sync --locked`、`make schemas` 完成。
- `make check`：**206 passed、17 deselected**（3.88 秒），Ruff 与 Pyright 全绿；不调用真实模型。
- `make test-integration`：**13 passed、210 deselected**（22.05 秒）。包括新增脚本种子：命令→提交 F1→提交并认领 I1→release→回执；验证 4 条工具记录、5 次心跳与精确 token 累计。
- `make eval-targets` 成功，仅构建已有评估素材，未开展 EVAL-2。
- 真实 DeepSeek + 真实 envd + 独立 PostgreSQL/MinIO + 实际 CLI 检查通过（task `35a1f05e-6b6c-4e76-bec9-fe8b416d18c9`）：**6 条事实、6 条 tool_backed 且均带 auto command_output、1 条意图、conclude 后 1 次 release**；15 次模型调用，结束 normal、回执合法，宽限剩余 1 次。测试在认领响应返回前通过另一 CLI 发 conclude，确保检查的是“仍持有意图时交接”而非已自然完成的路径。执行环境的代码位于 `/workspace/shared`；所有测试容器/网络清理完成。
- 本轮已记录 token：缓存命中 326144、未命中 28588、输出 13328（其中推理 4943）；按固定错峰价估算 **USD 0.013263432**。这是收到用量的账本估算，不是控制台实付核对。
- 另外一次自然结束检查产出 6 条工具证据事实与 1 条已关闭意图，17 次调用；conclude 后用允许的 post_fact 关闭持有意图，因此无需 release，该路径也核对过。初次检查有挂起请求，达到 harness 上限后正常取消并 finish；不记为通过。超时配置开发过程中一次客户端构造错误在模型调用前失败，修正并通过普通检查后才再次运行。
- 发现内容包括 Inventory.reserve 的无锁 check-then-act、并发断言位置、确定性 Barrier 复现、加锁对照；这些是种子提交的事实/验收声明，尚未经过 close 裁定，本任务不声称完整闭环完成。

## 检查点复现

运行前需要黑板、数据库、MinIO、internal 执行网络、egress-proxy 和 eval-targets；白名单允许 eval-targets 以及准备依赖所需的镜像源。使用已有 `eval/tasks/flaky-order-test/task.yaml` 创建任务并指定 default profile。生成评估 tar 包：`make eval-targets`。

配置变量由使用者注入进程环境；在宿主机运行设置 `BLACKBOARD_URL`、`MINIO_ENDPOINT` 为可达地址，`EXEC_NETWORK` 为实际 internal 网络名、`EXEC_ACCESS_MODE=relay`。容器内使用 network 模式。JWT 签名密钥仅留 blackboard。若选择本地配置文件，可执行：

```sh
uv run --env-file ~/blackboard-explorer/.env bbx-runtime run-agent --task <任务UUID> --type explore --seed
# 在另一终端使用上条命令先输出的 Agent ID：
uv run --env-file ~/blackboard-explorer/.env bbx-runtime conclude --task <任务UUID> --agent agent-1
uv run --env-file ~/blackboard-explorer/.env bbx-runtime state --task <任务UUID>
```

检查至少 3 条事实、至少 1 条 tool_backed 且带 auto command_output、提出意图；conclude 后 release、剩余宽限非负、合法回执、Agent 已结束。CLI 此阶段不自动调度其他 Agent或销毁任务容器，M3 提供完整监管。

## 价格与金额

已按 2026-09-24 [DeepSeek 官方价格页](https://api-docs.deepseek.com/quick_start/pricing/)填写 default 的 USD 高峰价：每百万缓存命中/未命中/输出 token 分别 0.006 / 0.30 / 1.20。推理 token 包含在输出内，不重复收费。默认用于保守预算；本轮真实测试另固定当时错峰价（高峰的一半）。价格快照的 off_peak 不自动切换时段，跨时段任务的精确账单校对仍需对应实际费率。尚未访问用户 DeepSeek 控制台，不声称已核对其账单。

## 共享文件与设计同步

- contracts 新增 CloseReceiptData / CloseReceipt，并更新 Receipt union、导出 schema；default profile 测试改为价格已完整配置。
- default 三模板与 models.yaml 落地；运行时使用黑板固定版本快照，历史任务不随本地文件变化。
- runtime pyproject 增加 bbx-runtime 入口；没有新依赖，没有根锁文件变更。
- 先单独提交设计 `8e13d90 Clarify successful final grace allowance`、`f42bcf0 Define the close agent completion receipt`、`0e46c91 Bound in-flight model requests and agent runs`，再实现。

## 偏差与待决

1. 设计原来缺 close 成功回执模型；已先补齐最小 note 型回执，不改变 submit_close 发布路径。
2. 证据关联的 call_id 检查格式与本任务日志对象存在；黑板按已登记调用的作者决定 tool_backed。当前没有调用归属的独立查询 API，跨 Agent 引用不会获得当前作者的 tool_backed 身份。
3. 对象上传成功而后续领域提交失败可能留下孤立对象；本任务不增加后台垃圾回收。成功的事实发布不会引用未上传对象。
4. 默认高峰价格是固定估算；完整控制台账单核对及跨时段精确计费尚未验证。价格为空仍记 token、金额 0 并告警。
5. 真实检查发现 MAF 循环时长只在请求间检查，无法中断挂起请求；已补 SDK 请求超时与外层 asyncio 超时，并验证挂起 run 会正常 finish。原始超时轮次已记录，不计为通过。
6. M3 才提供步数/时间/预算触发 conclude、清扫、任务归档销毁与失败监管；M2b 的 CLI 仅跑单 Agent，不能当作完整调度服务。

## 建议提交划分与下一步

设计已独立提交；contracts 与 schema 建议 `Add close agent receipt contract`；runtime、模板、价格及测试/文档建议 `Run agents with board tools and persistent updates`。已完成上述普通、集成与真实种子检查；按既有授权合并并打 m2 标签，再推进 M3a。无需用户补跑开发检查；用户可在 DeepSeek 控制台核对对应时段账单。
