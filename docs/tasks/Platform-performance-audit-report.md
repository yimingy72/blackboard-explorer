# 全站加载性能与后端稳定性审查

日期：2026-10-17。基线 `main@add10d2`，工作树 `platform-performance-audit`，分支 `fix/platform-performance-audit`。

本轮按用户补充要求扩大范围：不局限于黑板页面或既往 HTTP 402，而是审查全站加载、跳转、渲染，以及任务接口、执行恢复、状态一致性、用量结算、对象存储和执行环境生命周期。保留既有界面风格、任务数据、预算与配置。不修改 `docs/design/`，不调用真实模型，不操作公网任务。

## 前端

- 登录与应用导航分包；任务列表、新建、配置、报告、任务模式入口使用 React Router 原生 lazy。鼠标或键盘聚焦入口时预取页面模块及任务元数据。切页等待模块时保留当前页面，内容到齐后做短淡入；键盘焦点、未保存草稿阻止离开和减少动态偏好保留。
- 普通黑板与 CTF 工作台按模式加载。普通黑板画布模块与快照请求并行准备；Agent/对象详情、复盘内容、流量解析、模型/MCP/CTF 配置按需加载。流量 Drawer 立即打开，重解析组件在面板内部加载，关闭动效与焦点恢复仍由 Ant Design 负责。
- 复用 ELK 官方 workerFactory，把普通黑板布局放入 Web Worker。浏览器主线程不再解析/执行整个布局引擎；Node 离线布局测试保留已有 bundled 入口。没有另造布局算法或改变节点布局规则。
- 运行时长组件单独更新，避免每秒重渲染整个列表、画布与会话；标签页隐藏时暂停计时器。历史回放按选中事件时间计算。
- 任务列表活动期轮询从 2 秒调整为 5 秒，共享 5 秒查询缓存；收起导航时不另发最近任务请求。已结束普通 Agent 会话停止空轮询，存在 queued/processing 复盘回复时继续刷新。元数据与消息请求接入 Query AbortSignal。
- 修复普通黑板乱序增量从空状态重放而丢失快照的问题：保留初始快照，增量只覆盖快照后的版本；历史请求增加访问代次检查，离开后重新进入同一任务也不接受上一趟的迟到响应。
- 修复 CTF 快照刷新与 SSE 竞争：请求期间收到的新事件合并回该响应；已经包含在新快照内的旧事件不再重放，避免任务分配或运行状态回退。

### 构建实测

同机同一 Chromium、生产构建、合成 API 数据、各页面独立浏览器上下文；统计页面资源的 decodedBodySize。普通黑板等待节点完成布局。此处是未压缩脚本资源量，不是公网速度或 Core Web Vitals 的百分比。

| 页面 | 修改前 KiB | 修改后 KiB | 减少 |
|---|---:|---:|---:|
| 登录 | 981.8 | 651.7 | 33.6% |
| 任务列表 | 981.8 | 915.0 | 6.8% |
| 新建任务 | 981.8 | 933.4 | 4.9% |
| Worker 配置 | 1070.4 | 851.7 | 20.4% |
| 普通黑板（含布局 Worker） | 3245.0 | 2639.8 | 18.6% |
| 报告 | 1100.7 | 902.0 | 18.1% |

普通黑板的上述新总量含约 1398.5 KiB 的独立布局 Worker；主线程脚本约 1241.3 KiB，较原先主线程 3245.0 KiB 减少约 61.7%。Worker 并没有消除网络下载量；它主要改善布局计算对输入、点击和滚动的阻塞。首页仍有通用组件代码，不能据此声称全站加载均提升 34%。

## 后端确证问题与修复

1. **复活 Explore 被旧请求终止**：原来同任务轮次、同 Agent ID 不足以区分先后执行。注册返回既有 started_at，finish、heartbeat、checkpoint 和活动执行的消息完成携带 expected_started_at，在任务锁内校验。旧结束请求不能将新执行标成 failed、释放它刚认领的 Intent，旧心跳/Session 也不能覆盖新执行。首次执行兼容旧请求，复活后要求有效围栏。没有数据库迁移。
2. **有效旧用量与新执行隔离**：跨执行代次到账的用量仍凭持久 Session 证明和 request ID 幂等入账，但仅计费用，不修改新执行的步数、上下文、心跳或状态。缺少新时间字段时保持旧 heartbeat digest，历史已确认回执仍可重试。
3. **冷恢复的未配对工具调用**：复用已有 repair_unpaired_tool_calls，使用已持久化结果补齐已执行调用，未知结果标记中断，并在下一次模型请求前保存 MAF Session checkpoint；不重新执行旧工具。
4. **刚登记就取消漏清理**：运行器进入受保护区后才接收取消，确保登记/认领之后即刻取消也执行持久 finish。清扫器、重启恢复与最终回执补偿检查同一执行时间。明确过期的旧 proof 返回未恢复，未知传输结果仍保留重试机会，避免过期 proof 永久阻止超时清理。
5. **CTF 快照与 SSE 水位不一致**：任务、游标、成员及题目改在同一个 REPEATABLE READ 快照内读取；避免游标已包含结束事件，task 却还显示 running。
6. **CTF 带附件创建重试不稳定**：幂等查询提前到模型/配置解析之前，重试返回已创建任务的实际 profile 版本，不受其后配置修改影响。
7. **列表 N+1 与完整黑板读取**：任务列表、详情直接批量读取所需字段；不再为首页读取全部 Fact/Intent/消息/Session。普通任务 1→2→3 个均为 4 次 SQL，混合 5 个任务为 5 次；所有字段共用事务快照。既有授权和 CTF public_member 脱敏保持。
8. **配置历史全量读取**：数据库只选每个模型/MCP 连接的最新版本，避免每次枚举全部版本及执行无用转换。
9. **上传取消与文件生命周期**：ObjectStore.put 等待 SDK 上传线程退出后再传播取消，防止调用方先关闭线程仍在读取的文件；覆盖重复取消。
10. **归档、恢复和下载清理**：归档生成取消后回收生成物，下载发送失败也清理归档临时文件；恢复遭重复取消时等线程退出再删除目录。流量片段发送失败关闭句柄，按已声明 Content-Length 精确读取，不把持续增长的尾部写进本次响应。
11. **取消创建容器遗留资源**：等待 Docker 创建操作完成，移除本次刚创建的容器后传播取消，覆盖执行容器与 relay。
12. **命令大输出占用内存和主线程**：文件支持的有界预览与完整输出保存移到工作线程，避免先 read() 全部 stdout/stderr 再截断；完整输出依旧可查询，流量凭据脱敏保持。

## 验证

- 前端 ESLint、TypeScript、Vitest：139 passed。
- Ant Design CLI 检查 src：0 issues。
- 生产构建：通过；生产冷加载测量覆盖 6 个主要路由，确认普通黑板实际创建独立 ELK Worker。
- 全站 Playwright：126 passed（4.7 分钟），覆盖草稿保护、失败重试、任务切换、列表查询参数、返回焦点、桌面/窄屏、流量、历史回放、坐标保存及减少动态效果。
- 新增关键 E2E：代码下载期间保持旧页面；终态会话停止空轮询但待回复仍刷新；迟到 CTF 快照不覆盖 SSE 新分配。
- `make check`：Ruff、格式、Pyright 通过；2202 passed、362 deselected。
- 黑板服务与对象存储全部 PostgreSQL/MinIO 集成：302 passed、579 deselected（217.77 秒）。
- 最后输出快照与迟到回执补强后的定向回归：107 passed；生成契约后的前端 TypeScript 再检查通过。
- 全站截图已检查列表与普通画布布局；新布局 Worker 和客户端快照竞争均有浏览器或离线回归。

可复查命令（不调用真实模型）：

```sh
make check
.venv/bin/pytest -m 'integration and not live' services/blackboard/tests packages/objects/tests
pnpm --dir web check
pnpm --dir web build
pnpm --dir web e2e --workers=2
BBX_E2E_PREVIEW_DIR=dist pnpm --dir web e2e loading-performance.spec.ts --grep 'records cold' --workers=1
```

若本机缺少 Playwright 默认下载的浏览器，可使用既有 BBX_E2E_BROWSER 参数指定本机 Chromium；不依赖生产登录状态或真实任务。

## 偏差与待决

- 本轮是通用一致性、生命周期和加载策略修复；没有更改 Fact/Intent/Explore/Derive/Close 职责，没有新 MAF Workflow、前端状态框架或组件依赖。MAF Session 与原有修复器、React Router/Query、ELK 和 Ant Design 继续复用。
- expected_started_at 覆盖结束、心跳与 Session 写入；没有把它扩展到全部 Fact/Intent/claim/conclude/grace 工具协议。旧执行在下一次模型或工具调用前检查当前执行时间；已进入服务端的工具请求仍遵循现有事务和幂等规则。
- 旧模型账户 HTTP 402 仍需可用账户；本次没有以此代替代码审查，也没有擅自恢复或增减业务任务预算。
- 同步创建系统用户、归档逐对象检查/下载仍有优化空间；此次没有在缺少测量依据时进一步改变其并发与锁规则。
- 未重跑包含全部执行镜像构建的 `make test-integration`；本轮运行的是全部黑板/对象存储集成，以及 runtime/envd 离线回归。容器创建取消用内存 Docker 替身精确控制时序，不能替代 Docker daemon 故障注入。
- 本次测量未模拟所有公网时延/丢包与移动设备；没有把离线通过等同于真实模型供应商或公网长时运行验证。
- 代码尚未合并/推送，运行中的本地及公网服务未替换。生产启用须同步更新 blackboard/runtime；envd 修复需重建执行镜像后供新建任务使用。升级期间应保留正在运行的任务及其执行容器，不让旧 runtime 与新的复活执行围栏长期混跑。

## 建议提交划分

1. `fix: fence resumed agent executions and preserve recovery accounting`：contracts、blackboard/runtime 的执行时间围栏、Session 冷恢复、即时取消、相关契约生成物与回归。
2. `perf: batch task views and keep CTF snapshots consistent`：列表/配置读取、CTF 创建幂等及快照事务。
3. `fix: make runtime file and container cleanup cancellation safe`：objects、envd、execenv 创建取消和大输出处理。
4. `perf: split page loading and isolate workbench rendering`：前端加载、切页、Worker、计时器、快照竞争和 E2E。
5. `docs: record platform performance and reliability audit`：本报告及界面加载规范补充。
