# CTF-prompts-tools · 系统提示与协作工具报告

日期：2026-10-11。继续在 `/Users/yym/bbx-wt/ctf-cooperation`、`feature/ctf-cooperation` 工作；上一轮调度优化仍在本分支，ui-rebuild 前端改动保持在原工作树。三个 GPT-6.1 Sol high 子代理分别实施提示词、工具反馈和 SQL 分页，主会话交叉审核与部署核对。

## 1. 系统提示词

两份模板保留独立完整正文，按目标/上下文、分工共享、工具证据、验证交付分节，Lead 另有显式续跑规则。

- 已有消息和 Session 足够时直接继续；按信息缺口查询相关名册、题目和记录，避免把读取全表当成每轮前置步骤。记录按需分页，后续可读新增部分。
- Lead 根据独立产出、依赖和负责边界派工，优先复用现有具名成员。增援先登记协作者并保留 owner，再发送 instruction；已有且确定有效的协作者不重复登记。
- 关键发现、失败条件和阻塞及时追加共享记录，影响他人工作时定向通知，随后继续不依赖回复的目标；不为每个动作广播，不用模型轮询等待。
- 已接受派工的最终回答由系统结算唯一结果通知；提前交付时显式回复，最终交付保留 Lead 需要的结论及共享引用。普通成员问题需要答复时必须显式回信并关联 reply_to，不能误以为最终回答会自动转发；用户消息在当前会话答复，纯通知不回复“收到”。
- 明确 request_help 只供非 Lead 的题目 owner 使用，工具本身已记录阻塞并通知 Lead；帮手告知 owner/Lead。资源启停与平台连接信息管理由 Lead 通过已配置工具负责，队友使用任务给定连接和实际授权工具；模板不增加权限。
- 保留可信身份/来源分隔、CAS、必要文件登记与已有 artifact ID 复用、复制后修改共享文件、未知副作用先查询、候选与真实验证分离、多题整体核对、不可用外部验收 partial 收尾、closing 后停止、显式续跑确认 deferred 消息等规则。

没有写入靶场地址、固定漏洞套路、操作命令、答案或 flag 格式，也没有增加自动语义去重或后台决策规则。重点是使用正确上下文和减少无效交接，不能据文本改写就宣称真实模型效果提高。

## 2. 工具接口与反馈

原生 MAF 离线复现发现，服务端 CAS 冲突虽含 current_revision，模型实际只收到 `Error: Function failed.`。现在内建协作工具把明确的 revision_conflict 转成 JSON，提供合法正整数版本和“先读取最新题目再决定”的固定提示；不自动重试写入，也不把新版本直接当作覆盖许可。

- 成功结果统一为 JSON，中文、null、布尔值、分页信息和 artifact 数据保持；命令执行原始输出不改格式。
- 仅白名单中的参数校验 422 转成固定安全提示；权限 403、旧 generation、停止、预算、未知 422/409、5xx 和网络异常按原路径传播。没有开启全局详细错误或回显原始服务异常。
- 错误对象仅额外保存 CAS 的正整数 current_revision，不向模型复制未审查的响应字段。测试中的私有哨兵字段不会进入模型结果、日志或检查点反馈。
- 工具 schema 补齐 work_status 枚举、revision 下界、服务端已有字符串/超时边界；说明 action 对 owner_id/collaborator_ids/work_status 的对应关系，证据引用须是已登记 artifact ID，求助须满足 owner 与角色条件。
- 工具日志不再记录 `<Content object at ...>`，而是保存实际 Content 的 JSON 内容；可纠正错误反馈也能随中间检查点恢复。

原生 MAF 脚本化回归执行了 stale update → 收到冲突 → get_challenge → 明确以新版本更新，实际仅两次显式写调用，没有隐藏重试。恢复检查点后仍能读取冲突反馈和版本信息。

## 3. 分页与增量读取

之前每一页都先查询并反序列化整段历史，再在 API 内切片。现在 list_challenges/list_records 在 SQL 中使用 OFFSET 和 LIMIT limit+1，额外一条仅用于确定 next_offset；工具执行、只读复盘和 HTTP GET 走同一分页方法。

记录工具新增可选 after_version，使用真实 ctf_records.version 严格筛选较新的记录，仍限定同任务/同题目。一次分页过程固定 after_version 并沿 next_offset 读取；下一次增量才使用已完整读过的最高 created_version，offset 归零。默认不传 after_version，工具不会发送 null。原默认页大小、排序、tombstone、返回形状及服务内部未传 limit 的全量调用保持。

真实随机 PostgreSQL 回归覆盖 0/1/整页/多页/超尾 offset、过滤后 offset、旧调用兼容、任务/题目隔离，并监听 SQL 确认存在 LIMIT/OFFSET 且查询上限为 limit+1。没有新增迁移、索引、依赖或工具权限。

## 4. 实际检查

- 最终 `UV_CACHE_DIR=/private/tmp/bbx-uv-cache make check`：**1186 passed / 143 deselected**，19.50 秒，Ruff/Pyright 通过；3 条既有 MCP 弃用警告。日志 `.data/ctf-prompts-tools/check-final.log`。
- 提示词渲染与合同：44 项通过；工具及相邻离线检查 96 项通过，最后参数提示补充的独立工具检查 17 项通过；分页 API 普通检查 57 项、真实 PostgreSQL 分页 7 项通过。
- OpenAPI 与 TypeScript API 声明已同步，`git diff --check` 通过。提示词条款回归验证的是渲染及约束存在，不冒充模型行为评测。
- 完整隔离容器检查：**139 passed / 1190 deselected**，242.32 秒（4 分 2 秒），exit 0，日志 `.data/ctf-prompts-tools/integration.log`。涵盖新增分页 PostgreSQL、CTF 真实执行/消息/平台/生命周期、普通黑板、归档恢复和 runtime 镜像。集成期间只补充普通问题回信和协作者登记的模板文字，随后 44 项模板合同及最终 1186 项普通检查复验通过；工具与服务代码未再变动。

依赖与执行环境没有变化，使用本地既有镜像 `--network=none` 增量打包源码和 CTF 模板，复用 exec-env、egress-proxy、eval-env。完整容器检查命令：

```sh
TESTCONTAINERS_RYUK_DISABLED=true UV_CACHE_DIR=/private/tmp/bbx-uv-cache \
  make -o image-exec-env -o image-egress-proxy -o image-eval-env \
  -o image-agent-runtime test-integration
```

## 5. 边界与建议

- 本轮只调整 CTF 两角色及内建协作工具；普通 Explore/Derive/Close、外部 MCP 的权限与副作用重试策略保持。没有添加结构化消息关联字段、独立 get_record 工具或新决策系统。
- 没有调用真实模型或操作靶机；脚本化测试证明协议与链路，不证明模型会始终遵循提示或带来固定速度/费用收益。真实行为需要用新任务另行验证。
- 不修改 docs/design 或 .env；模型、价格、启用工具、资源和既有任务固定快照不得随模板发布被覆盖。前端没有重新构建，保留上一轮界面。
- 本分支尚未提交、合并或推送。建议在上一轮调度提交之后分别提交 `Improve CTF collaboration prompts and tool feedback`、`Page CTF records in the database and support incremental reads`，再提交说明和报告。

## 6. 本地部署与配置发布

已更新 <http://127.0.0.1:58000/login>，并通过原 `/api/settings/ctf/workers/{role}` 接口发布 Lead/teammate 模板，CTF 配置 revision 3 → 5。每次写入携带 expected_revision，发布前验证原模板与配置指纹；若用户并发修改，会停止发布，不覆盖其修改。两份平台实际模板的散列与本地最终模板一致。

- 仅两角色的 prompt_templates 变化。模型、价格、启用工具、MCP 绑定、资源和其他 profile 字段散列保持；平台模型配置表保持。
- 发布前 0 活动任务、0 running turn、0 待处理复盘。既有 5 任务、11 Session、全部 CTF 记录/消息/事件及原 profile 行的数量和散列保持；仅新增两条 CTF 配置版本。已有任务快照和 opening instructions 不重写，新建任务使用新模板。
- runtime 镜像 `2dafde5997746745c7d931a882db5c29a37b54dd75cc40e7bb8be35ba42b5ca2`；blackboard 镜像 `3603dd2858f23e31bea0a62722780210ebfbc61fdb2e98363f1ac7959a24eff1`。运行容器的 54/28 个 Python 文件与工作树逐一核对通过，均 running、restartCount 0、启动 ERROR/Traceback 计数 0。
- schema 仍 0014，登录 HTTP 200，前端 index SHA256 保持 `4a704f801b3fde1e4346acd75ea2c0ee2453de13d81c31a1918e3d9fc6f7f9b8`。没有重建或删除 PostgreSQL/MinIO 容器、数据卷和归档。
- 仅正常服务配置在进程内用于管理 API 认证，没有读取/写入 .env 或回显凭据；未触发真实模型调用、重跑用户任务或改写历史会话。

核对记录位于忽略目录 `.data/ctf-prompts-tools/`：`prompts-before.json`、`prompts-published.json`、`before-deploy.json`、`after-code-deploy.json`、`after-deploy.json`、`deployment-health.json`、`image-source-check.json`、检查和构建日志。
