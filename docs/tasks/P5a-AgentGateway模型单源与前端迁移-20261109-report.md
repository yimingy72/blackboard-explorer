# P5a · AgentGateway 模型单源与前端迁移报告

## 1. 完成内容

1. 两套平台的模型页均改为只读 AgentGateway 目录。配置模型、Provider、上游密钥、费率由 AgentGateway 1.7.0 原生管理界面负责；平台只提供打开该界面的入口和已应用模型。删除了平台的模型编辑器、旧默认模型、上游发现、旧模型写 API、Provider 凭据分发和环境密钥导入。旧配置行、历史快照、币种及账单没有删除。现有 MCP 管理与黑板 Worker 的实际工具选择保留。
2. 模型管理入口用官方原生认证。未认证的底层管理端口留在内部。管理入口仅在两站确认空闲、按同一个维护 ID 关闭新建、发送与恢复时允许修改模型资源；只原样转发一次原生操作，原生确证后恢复准入。未确认结果不重复写入。平台没有第二套编辑表单、模型配置库或发布器。
3. 普通黑板剩余模型调用迁至已有 AgentGateway；原生 CTF 继续使用现有网关代理与 NativeFees，选择模型不再依赖旧平台 ID/版本。环境验收使用 v2，不把旧有绑定合成模型的验收记录直接启用为新发布。原生 CTF 设置显示实际发布的角色、提示及黑板工具；不再显示修改后不生效的旧草稿和派工切换。
4. 新任务和成果整理预算直接使用 USD，原始 key 的累计阻断和同 key 更新、续跑回执保留。黑板求解与成果整理各自占用独立 key；原生费率不在平台维护副本。旧 CNY 和确有原历史汇率的快照按原方式读，不转币种或混加。新求解费用视图区分已确认与待核，原估算只供原调度/预留使用。
5. 用户向已结束 Agent 的只读问答及终态复盘共用同一长期 AgentGateway review key，不设金额上限。普通活跃 Agent 的执行消息仍由求解额度控制，旧消息不会在 Agent 重启或任务续跑后换成复盘。原生 CTF 复盘 token 只允许指定的读工具，不能运行命令或写入任务。两站确认费用和待核调用以原生来源、日志及原调用归属对账；求解、成果整理和复盘不串账。
6. 流量和命令记录沿现有只读查看。没有新增命令审查或处置，也没有声称本片已迁移全部 MCP 或验证全部原生供应商协议。

## 2. 必要验收

- 干净候选 `make check` 通过：Ruff、Pyright 0 错误、Pi 检查及 2,568 项 Python 测试通过，486 项按默认范围未选，Python 测试 172.43 秒。原始日志：`.data/p5a-agentgateway-models-20261109/make-check-final.log`。合并预览另用实际合并源码重导 OpenAPI、TS 并通过类型检查。
- 隔离 Chrome 完整验收 `.data/p5a-agentgateway-models-20261109/native-browser-save-to-task.json`：实际使用官方页面点击 **Save model** 和 **Custom Costs → Save**（均 HTTP 200），真实守护入口按两站空闲回执允许原生保存并读回；两个平台的只读目录随后显示同一真实别名、修订和已改费率。浏览器选用该别名实际创建 USD 0.05 的 Task，未启动推理。原生页面匿名 401、认证可访问；截图 `native-model-save.png`、`native-cost-save.png`、`readonly-gateway-models-rendered.png`。另验未认证、跨来源、修改虚拟密钥和站点忙时不转发，原生日志 `local-rollout/native-ui-fixture-final.log`。
- 真 PostgreSQL 的合并作用域验收包括：原价目维护期间的目录读取不能跨代提交；原生复盘费用不进入求解总额；无日志调用仍待核；追加 A、B 后迟到的 A 重试不再次更新；活动 Task 的已结束 Agent 可由用户提问，旧活跃消息不能被改成复盘、错原代际或自动调用被拒；原生 CTF review token 不能触达求解写入口。针对运行中的 CTF 停止成员，用 PostgreSQL 验证：原执行代 8 绑定的两个问答分配独立只读代 9/10；恢复到执行代 11 后旧只读权限失效，活动但空闲的成员不符合问答准入。该定向组合 5/5。
- 实际 AgentGateway 与 Pi 传输：普通模型调用、原 key 累计阻断、独立成果整理、两个真实终态会话使用同一个无上限 review key，记录于 `.data/p5a-agw-20261109/probe/real_board_gateway-combined-final.json`；同原 key 续跑和真实 MinIO 原工作卷归档另见 `real_board_gateway-resume.json`。最新直接 USD 且 Task 仍运行的已结束 Agent 问答见 `active-review-usd-final.json`，原活动消息拒绝转作复盘，普通原生额度直接使用 USD 0.009，不经汇率。
- 原生 CTF 另有一个真实 Kubernetes Task：公开创建、官方原生 Sandbox/Runner 与固定 AgentGateway 实际链，USD 0.009 不带 FX，原发送及费用回执已核，实际原生费用 USD 0.0006。原件 `.data/p5a-agentgateway-models-20261109/native-solver-proof/k8s/native-v2-solver-proof.json`，原 Task 正向停止见 `k8s/joint-native-v2-exact/resume-evidence.json`，私有集群/资源清理见 `k8s/native-v2-solver-cleanup.json`。所有上游推理验收用合成响应，未调用真实付费模型。

## 3. 偏差、保全和交付

- 本片没有重建 AgentGateway。新增接缝集中在只读目录、已有原生接口的可信许可与账单归属、两站管理互锁，以及普通/整理作用域迁移；原生模型编辑由官方 UI 提供。删除旧直连工厂、旧 DeepSeek 环境导入、平台 Provider 模型表单/发现和废弃测试。最终差量较广，包含既有运行调用和回归夹具调整及生成物，并非全仓净删除。
- 任务、角色模型的可信选择仍需要现有原生目录足够的活动能力与价格证据；缺证的别名不作为可用选项。旧原生环境 v1 不会冒充 v2。MCP 的现有产品路径继续工作，未将其统一为模型网关。两套运行模式仍按此前独立部署。
- 原 110 个文件完整备份；17 个共同路径的合并预览可逐字节和模式反向恢复。原工作树除未跟踪的 `docs/tasks/.DS_Store` 外均精确；Finder 元数据有外部变化，原版本和观测版本均留存，未被本阶段覆盖或提交。被替代的旧编辑器 WIP 已保留原字节，不重新接回活源。非重叠的原自动派工、权限与独立测试保留。
- 建议仅提交一笔 **Migrate platform model access to AgentGateway**：共享合同、BB 运行与费用、Runtime 发送、原生管理入口、Web 与生成物、必要验证夹具和本报告。不推送、不合并主线、不修改正式设计。两套本地站点仅从精确干净交付源更新，部署和用户登录凭据不纳入源码；用户环境不放测试模型。部署回执单独记录，旧 OpenSandbox 不接触。
