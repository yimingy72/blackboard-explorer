# CTF 题目详情 · A 方案实施报告

日期：2026-10-14
状态：实现、验证并部署到本地；未提交 Git。

## 完成内容

### 1. 题目卡片与状态

- 去掉 `external_id` 与题名的重复展示。只有明确形如 `标题 [类别/难度]` 的后缀拆为元信息，完整原题名仍用于可访问名称和来源。
- 落实用户选择的 A 方案：状态由图标、文字、浅色底和边框共同表达。
- 状态统一为待分配、解题中、受阻、已交付、已解出、已取消/归档。`completed` 只表示已交付；只有可信、非模拟的平台或用户成功结果才显示已解出。候选、模拟通过和整次任务结束不会冒充成功。
- 节点显示成员头像、技术进展数和文件数；保留环绕布局、关系线、动效、拖动坐标和持久化位置。

### 2. 题目详情信息架构

- 页签改为概览、解题流程、文件。
- 概览显示参与成员、Agent 登记简述、连接（说明中未重复时）、交付要求、负责人工作结论和可展开关键进展。
- 创建题目、负责人/协作者调整和纯工作状态变化收进派工动态，不再混作技术进展。
- 移除概览中的重复最新记录、验证信息和查看全部记录入口。历史候选、平台/用户来源及原始依据保留在历史共享记录的低频只读入口。
- 文件使用紧凑标签，点击后在标准 Modal 中读取文件信息、证据预览和下载；保留原权限和完整字节下载。
- 解题流程展示负责人/Lead署名、任务轮次、思路、关键尝试与取舍、真实突破（存在时）、结论、未解项、复现入口以及可定位的记录/附件引用；支持复制 Markdown。
- 缺流程、错误指针、精确读取失败和重新开题均有明确状态。旧总结不会冒充本轮结论，历史任务不自动生成或改写流程。

### 3. 后端约束与 MAF 工具

- 复用现有 CTF JSON 记录、附件登记、CAS、request_id 幂等、turn/generation fence 和 MAF `@tool`/Pydantic schema；未新增表、迁移、依赖或报告服务。
- 新增 `CtfProgress`：阶段、最多 40 字标题、最多 160 字摘要。
- 新增 `CtfSolution`：结论、思路、1–6 个关键步骤、最多 3 个未解项及复现入口；总正文最多 1600 字。允许真实未解、排除和未定结果，不要求凑突破或成功。
- `append_record` 可附短进展。两个正常 `completed` 入口必须同次附 solution；总结记录、候选、附件和工作状态在一个事务落盘。
- 仅 owner、Lead 或用户可完成交付；协作者不能代交。记录引用必须属于同一道题，附件必须在本任务登记且不能借另一题独占证据冒用。
- 重开清 `solution_record_id`，旧追加记录不改写。精确只读 API 按 task/challenge/record 三重范围读取总结或依据，跨题与跨任务返回 404。
- 交付参数错误使用固定 `invalid_solution_delivery`；运行器只对这一精确错误返回短安全修复提示，不回显服务正文、不自动重放工具调用。
- 附件和记录引用改为批量投影查询。真实 PostgreSQL 多引用用例确认完成路径只需 3 个记录查询，不读取整任务记录正文。

### 4. 提示词与配置

- Lead 与 teammate 提示词增加三项通用规则：题面保真、有效/无效尝试及时提交短进展、owner 正常完成时整理公开解题流程。
- 规则明确保留失败和阻塞、不编造题面/突破、不转录私有逐字推理、不把最终回答当成交付、不让预算/取消/异常收尾被格式阻塞。
- 已通过既有 Profile 版本机制发布 revision 15：在 revision 14 末尾各追加 3 条规则。模型、工具、MCP、价格、上下文、并发和执行配置逐字段保持不变；旧任务快照不变。

## 实际验证

- `make check`：**1806 passed / 320 deselected / 24 warnings**。Ruff format/lint、Pyright 全绿；警告为既有 MAF MCP sampling 弃用提示。
- 完整 `make test-integration`（复用已构建执行镜像）：**316 passed / 1810 deselected**。首次检查发现两个旧集成用例未附新 solution；补齐合成交付后完整复跑全绿。
- 前端 ESLint、TypeScript、Vitest：**18 files / 107 tests passed**；Vite production build 成功。
- CTF 浏览器回归：既有 **45 项通过**；新增 **5 项通过**。覆盖 390/1440px、标题去重、六类状态、结构化成功/失败/未定步骤、精确引用回读、404重试、错误指针、重开旧流程、100+1分页、真实文件字节下载、焦点、调宽、坐标和事件。
- Ant Design CLI 对 Tag、Tabs、Collapse、Button、Timeline、Modal 查询官方 API；三处正式组件 lint 均 **0 issues**。
- 未调用真实模型、靶机、外部平台、答案或评分接口；未读取 `.env`。

## 部署与数据保持

- 本地入口：`http://127.0.0.1:58000/login`，HTTP 200。
- runtime 镜像：`bbx-ctf-challenge-runtime:20261014` / `bbx-agent-runtime:latest`，`sha256:5f3fd2daac1878a8b507ed6ce55864159e554e21d1e69a62118f61617f7b9193`。
- blackboard 镜像：`bbx-ctf-challenge-blackboard:20261014` / `bbx-ctf-blackboard:latest`，`sha256:acdf639ede8407dae4ead6be10aa4e44c2c89eb30beb24f2fb31c8c676a0378d`。
- 候选镜像与运行容器的 runtime 69 个、blackboard/frontend 156 个受控文件逐字节一致；容器重启计数为 0，启动后未见 ERROR/Traceback/Exception。
- 部署前 0 个活动任务、0 个运行回合。11 个任务、15 个运行批次、74 个 Session、76 个成员、339 个回合、634 条消息、191 道题、997 条记录、3 个平台配置、1 个应用设置和 34181 个事件的逐表内容摘要部署前后完全一致。
- 仅 `agent_profiles` 由 24 增至 25，用于新增 CTF revision 15；schema 保持 `0016`。未重建 PostgreSQL、MinIO、执行容器或靶场。

验证日志和快照位于 `.data/ctf-challenge-release/`；合成浏览器截图位于 `/Users/yym/bbx-wt/ui-rebuild/.data/ctf-challenge-ui/`。

## 偏差与待决

- 已部署前端源码继续位于 `/Users/yym/bbx-wt/ui-rebuild`，后端/运行时位于本工作树；两个工作树都有此前已授权但未提交的相关改动，后续提交时需分别保留完整依赖关系。
- 升级前的旧格式 `completed` 请求若恰好丢失应答，并在升级后以同一 request_id 无 solution 重试，会被新参数约束拒绝。此次部署前没有活动任务或运行回合，因此没有待恢复的实际请求；新格式重试保持幂等。
- 历史任务没有结构化 solution，界面会如实显示“尚未提交解题流程”并保留历史共享记录。未用模型回填历史思路。

## 建议提交划分

1. `feat(ctf): require structured challenge delivery`：contracts、blackboard、runtime工具、提示词、相关测试和生成 schema/OpenAPI。
2. `feat(web): present CTF challenge status and solution flow`：`ui-rebuild` 的题目卡片、详情、类型、浏览器/单元测试及 DESIGN。
3. `docs(ctf): document challenge detail delivery`：任务说明、部署说明和本报告。

提交正文按仓库规则添加 `Implemented by Codex (gpt-6-sol) for task CTF-challenge-details.`。本轮未执行 Git 提交、合并或推送。
