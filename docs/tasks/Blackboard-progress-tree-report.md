# 成果树与成果整理 Agent 交付报告

日期：2026-10-19。分支 `feature/blackboard-progress-tree`，工作树 `/Users/yym/bbx-wt/platform-performance-audit`。

用户2026-10-18批准方案后，设计先单独提交为 `0c0b463`。此前已部署的对话输入修复独立保留为 `6f7eece`、`50036ad`、`cdf1780`，未混为成果树修改，也未在本轮合并main或推送。

## 已完成

### 1. 全局归纳与内容清晰度

新增独立成果整理 Agent，围绕原始目标和验收，把 Intent 当探索线索，跨意图归并主题、因果、反例、失败与排除结果。提示词禁止逐 Fact 建节点、按 Agent 分目录、固定题型套路和只总结最后一批。

总览说明全局当前认识、主要联系和未决问题；节点包含具体问题、当前发现及范围、对总目标的意义、下一步、局限和带来源的关键过程。标题须说明对象和发现/阻碍，不能用“已记录”“核对中”“取得进展”替代内容。同主题复用节点 ID，新证据补充或修订原节点。

每个 job 最多处理100条领域事件，分页推进，不跳过未读资料。积压允许连续分批回填；正常更新10秒合并、至少60秒间隔。上一棵树和已知认识随持久会话保留，增量小patch维护，不每次重发整树。来源读取水位、Session已知上界和图自身revision分别记录，不能把最后一页游标误当完整知识截止。

### 2. 前端

- 普通任务增加同级的“协作黑板 / 成果树”，正式路由 `/tasks/:taskId/progress`；不增加全局导航项。
- 明亮树状画布，目标、方向和成果/尝试/缺口分层；具体摘要、状态图标/文字、来源数量、贡献者可见。
- 默认收起右侧；节点详情展示问题、认识、意义、依据、关键过程、局限与下一步。复用React Flow、Ant Design/X、InspectorLayout及证据渲染器。
- 支持拖动、折叠、缩放、持久坐标和视角；只增量安排新增节点，数据更新不重排手动位置。选中被右侧遮挡时仅局部平移，不全图重缩放。
- 全局或节点范围提问，Enter发送、Shift+Enter换行、输入法确认保护；HTTP来源UUID兼容。重试沿用请求ID，关闭/重开面板和双视图切换保留草稿与选择，迟到发送成功不擦掉新草稿。
- 引用可点击读取对应截止版本的原始对象；证据走独立哈希校验入口，复用既有预览排版，不回退到可能覆盖后的普通URI读取。
- 领域争议、Intent变化和验收撤销会合并刷新状态，不等模型再次总结才取消旧状态；历史视图只读，不加载未来对话。
- 创建页明确显示默认额外¥2整理上限，可调整/关闭；旧任务显式启用。Agent配置增加独立成果整理表单，可编辑完整提示词、选择现有模型、配置限定工具。

### 3. 后端与MAF复用

新增 `task_progress` 与 `progress_jobs` 两表，迁移0019。图投影/租约/候选/费用与求解数据分开；会话和消息复用任务级存储，通过独立进展入口进行CAS和身份检查，不伪造AgentRun。

整理角色不创建Ubuntu、不占求解工作槽、不认领Intent、不派工、不裁定，不改变原 `task.usage`、预算、失败计数或完成条件。`progress.*` 是独立展示事件，求解投影和调度明确忽略其触发，不形成自激循环。

复用MAF Agent、AgentSession、工具、HistoryProvider、ToolResultCheckpointMiddleware、模型连接器、原生流式响应与已有运行锁。问答和图更新同任务串行，全局最多2个整理job；空闲且追平的终态任务不在每次轮询中获取任务锁。

`purpose=progress` capability绑定任务、job、租约和轮次。只读工具受来源上界约束，模型不能写求解接口或指定跨任务身份。新增 `update_progress_tree` 只保存结构化候选patch，服务端检查单根、无环、深度、大小、版本和真实来源，成功后原子发布。上限300节点、深度8、序列化1MiB。

正式状态来自原始来源与Close裁定，不把 proposed Fact、Agent结束或Intent关闭视为成功。验收引用必须匹配本任务真实裁定项，裁定撤销或引用变化会使旧摘要显示“依据有变化”。

### 4. 费用、恢复和生命周期

- 独立整理费用，不扣求解预算；按平台模型与价格快照预留，确认费用、有效预留及未知费用共同占额。页面分项展示求解、整理及人民币已记录合计。
- 每次完整模型响应先持久化Session和收据，再幂等结算；图校验失败仍记实际模型费用。SDK为本角色禁用自动重试，普通Worker默认重试不变。
- 网络中断且可能已调用时保留未知占额，禁止盲重放；明确调用前拒绝/取消不当作已经执行。持久响应可在租约恢复后补结算/发布，不重复模型调用。
- 普通失败允许显式刷新或提问恢复；未知费用/额度不足明确拒绝，不假装成功排队。最终失败同步消息状态，不永久停在“等待送达”。
- 续跑保留树和会话，旧轮提交fence拒绝；最终整理有界异步，不阻塞求解终态或执行容器回收。归档纳入当时图及会话，删除清两表和相关记录，迟到回调不能复活。
- 新Fact证据登记完整 `sha256`；旧记录不改。证据读取同时校验任务归属、登记版本、不可变内容哈希，初始附件还核对组绑定与文件清单。

## 实际验证

- `make check`：**2247 passed，406 deselected**，Ruff/格式/Pyright通过；未调用真实模型。
- 前端 `pnpm check`：**155 passed**；生产构建通过。新增成果组件Ant Design CLI检查0问题。
- PostgreSQL与真实ASGI HTTP/MAF脚本联通：**20 passed**。包括109条来源拆成两job、全分页进入模型、稳定前缀、图/会话/费用真实落库、checkpoint响应丢失、过期恢复零重复模型调用、旧知识截止不倒退、402和调用前取消、URI跨任务拒绝、任务原状态/费用/水位不变。
- 完整浏览器回归：**151 passed**。最终导航/初始启用状态调整后，成果树、普通黑板和相关详情**17项**再次通过；重复运行不叠加计数。
- 两端正式部署后，使用实际登录态从原黑板进入成果树，均看到显式启用和¥2默认上限，无页面异常。此核验只读，没有为已有任务启用整理。
- `git diff --check`通过。没有模型真实调用、靶场访问、任务续跑或Ubuntu测试容器。

主要复现命令：

```sh
UV_CACHE_DIR=/private/tmp/bbx-uv-cache make check
.venv/bin/python -m pytest services/blackboard/tests/test_progress_integration.py \
  services/agent-runtime/tests/test_progress_transport_integration.py -m integration -q --tb=short
cd web
pnpm check
pnpm build
BBX_E2E_BROWSER='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' pnpm e2e --workers=3
```

检查日志位于 `/private/tmp/bbx-progress-*`，发布材料与只读UI核验位于忽略目录 `.data/progress-release/`。不是用独立假后端分别猜测协议：Postgres、HTTP、原生MAF工具及持久层已经联通验证。

## 本地与公网发布

镜像：`bbx-blackboard:progress-tree-20261019`、`bbx-agent-runtime:progress-tree-20261019`。新迁移0019仅新增两张成果表；原20张业务表摘要与对象清单在切换前后完全一致。

| 环境 | 任务 | 会话 | MinIO对象与字节 | 入口 |
|---|---:|---:|---|---|
| 本地 | 19 | 150 | 58,663 / 1,677,884,800B | http://127.0.0.1:58000/ |
| 公网 | 23 | 223 | 111,078 / 9,903,573,559B | http://101.201.116.63:58000/ |

切换前及数据库短暂写屏障内均无活动任务、待处理对话或运行CTF回合。只替换blackboard/runtime，Postgres、MinIO和其他服务未重启，执行镜像未重建；环境变量、端口绑定和旧备份保持。发布后两应用正常运行、重启计数0，日志未发现运行错误。

旧任务没有被自动启用整理：两端新表均为0行，未增加预算或模型费用。原任务可进入“成果树”显式启用；新任务创建页明确勾选额外整理额度。

备份：

- 本地 `.data/progress-release/update-progress-tree-20261019-20261018T164558Z/`。
- 公网 `/opt/blackboard-explorer/private/update-progress-tree-20261019-20261018T164557Z/`。
- 公网源 `/opt/blackboard-explorer/releases/progress-tree-20261019`，运维入口已指向此发布。

源包SHA256：`e4c12b4e230042022d63d1c80201641d24782dc13386fe2adaee60464ab174cc`。源包明确排除AppleDouble元数据，镜像构建额外清理继承层元数据并完成Python编译检查。

## 偏差与待决

- 归纳质量最终依赖所选模型；本轮强化了全局组织、内容字段与来源合同，未通过真实模型样本宣称效果或缓存命中率已经提升。稳定系统前缀和增量复用已实现，命中以实际供应商usage为准。
- 旧证据缺少完整历史哈希时显示不可校验，不用当前文件伪装成历史证据；原黑板及归档保持可查。新登记证据补齐哈希。
- 首版只接普通黑板；CTF原工作台与调度保留。成果Agent是独立展示角色，不扩展三种求解Worker枚举。
- 共享文件变化包括contracts/schema、事件枚举、鉴权、API、投影忽略、存档删除、runtime生命周期/模型可选重试参数与前端路由。生成快照同时同步了前序已存在但漏导出的 `expected_started_at` 及流量字段，不新增这些旧功能的行为。
- 不引入新依赖、工作流引擎、消息队列或向量库。真实模型调用并非本轮部署检查的必要操作，留给用户在明确额外额度下测试。

## 提交划分

1. 已完成设计独立提交 `0c0b463`。
2. `cfc8eef feat(progress): add fenced MAF curation and intent-guided progress storage`：合同、迁移、后端、MAF运行与提示词、集成回归。
3. `5c39470 feat(web): add readable task progress tree and curator conversation`：画布、详情、对话、配置、创建入口、双视图与浏览器检查。
4. `b6cc4bf docs: record progress tree delivery and deployment`：报告、交接与正式前端规范；后续文档补记提交标识。

本轮不合并main、不推送GitHub；用户测试确认后可独立发布Git。
