# Workbench-UX · 工作台与 Agent 对话报告

日期：2026-09-26。设计提交：`1b7400a`。主代理确定布局与验收，GPT-6 Sol（high）分别实施后端、工作台和创建表单；因当前会话子代理数量已达上限，通过本机 Codex CLI 启动三项独立实施，主代理整合。

## 完成内容

1. **任务内编号与可见性**：代码、两个实际任务和 PostgreSQL 并发测试均确认后端编号本就按任务从 `agent-1` 开始。此前图谱隐藏已结束 Agent，导致用户可能先看到仍活动的 `agent-3`。新增常驻 Agent 选择条，展示当前任务的全部登记记录并从 1 编号；过滤、拓扑、详情和运行记录使用一致标签，引用仍保留原 ID。切换任务清除旧数据和选择。
2. **Agent 对话**：点击选择条或拓扑 Agent 打开同一右侧面板。按事件版本合并初始上下文、黑板注入、模型回复、工具调用与结束回执；长正文按需读取、可复制下载。正文位于对象存储，SSE 只传摘要；历史回放不会显示后续记录。旧任务无 trace 时明确提示，活动 Agent 尚无数据时显示等待状态。
3. **真实记录链路**：新增 service 专属 trace 登记接口与 `agent.trace.recorded`。验证任务、Agent、对象归属及初始记录唯一性。记录不改变 Fact/Intent 或调度的 `last_change_version`。DeepSeek 显式返回的 `reasoning_content` 以元数据保留，不作为普通回复再次发送给模型；推理 token 数不会被当作推理正文。记录故障只写警告，不阻断用量记账与交接。
4. **工作台密度与时间**：压缩标题、结束说明和费用信息，验收摘要与过滤器共用工具栏；完整理由与缺口按需展开。未选择节点时图谱占满主区，选择后侧边栏可关闭。任务时间从开始计时，终态冻结，回放按截止事件时间冻结；实时计时只在运行/收尾期间更新。
5. **创建表单**：主编辑区集中呈现目标、背景和多行验收项；预算与固定版本 Profile 放在设置侧栏，目标域名默认折叠。输入框随内容和视口宽度调整高度，窄屏自动堆叠；取消会遮挡输入的 sticky 操作栏，保留原提交、验证与默认配置逻辑。

## 实际验证

- `uv sync --locked`、前端锁文件安装通过，未新增依赖。
- `make check`：ruff/pyright 通过；**380 passed，42 deselected**。全部使用假模型。
- `make web-check`、`make web-build`：**33 个测试通过**，生产构建通过。保留既有 ELK 工作台 chunk 较大的构建提示。
- `make test-integration`：**38 passed，384 deselected**，真实 PostgreSQL/MinIO/Docker 验证通过；包括跨任务并发独立编号、trace 隔离/回放，以及脚本模型经真实黑板与对象存储保存每轮记录并通过鉴权读取。
- Playwright：**9 passed**，覆盖对话正文和模型返回推理、工具、历史隔离、拓扑/标签联动、运行时间增长、跨任务编号与选择、创建表单长文本及 390px 无横向溢出；原登录、证据、报告、归档、Profile 流程保持。
- 浏览器截图复核：1440px 工作台图谱高度超过 500px，对话栏标题与关闭按钮在内容滚动时保持可见；桌面与移动创建表单无输入遮挡。
- `git diff --check` 通过；执行环境、runtime、代理/评测测试镜像与 blackboard 镜像构建通过。没有调用真实 DeepSeek，没有读取或修改 `.env`。
- 本地 blackboard/runtime 已更新；HTTP 页面与最终构建一致，新 trace 接口可见，runtime 已加载对话与推理字段适配。原两个 finished 任务和两份工作区归档均保留。截图与检查日志保存在主仓库忽略目录 `.data/checkpoints/workbench-ux/`。

复现：`make check web-check web-build`；Docker 构建代理按 envd README 设置后运行 `make test-integration image-blackboard`；浏览器运行 `make web-e2e`（可复用主仓库 `.data/playwright` 缓存）。

## 偏差与待决

- 没有重置或重写历史 Agent ID。问题源于活动节点显示不完整，持久选择条与任务内展示编号解决可见性问题；内部引用身份保持有效。
- 逐轮记录从新版运行时启动的 Agent 开始产生，历史任务未保存的上下文/模型输出无法恢复。只显示模型实际返回的推理文本，不额外生成思考内容。当前按完整模型轮次显示，不提供逐 token 流式播放。
- 使用现有事件表和对象存储，无数据库结构迁移；生成 TaskSpec schema 时顺带同步既有“默认直连、域名字段仅记录”的字段描述。

## 提交划分与后续

1. `Design agent conversation records and a compact workbench`：设计与任务说明，已提交。
2. `Record task-scoped agent conversation traces`：后端记录、模型适配、schema/OpenAPI 与测试。
3. `Build a compact workbench with agent conversations`：前端、浏览器验证、文档与本报告。

合并后部署当前本地工作台。刷新浏览器即可查看布局；新建任务后点击 Agent 标签或节点查看完整逐轮记录。无需用户补跑本轮检查。
