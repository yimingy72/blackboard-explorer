# M1-W 任务报告：实时黑板画布

## 完成情况

| 编号 | 结果 |
|---|---|
| W.1 | React 18 + TypeScript strict + Vite 前端，登录与退出，任务路由，Cookie API 客户端，OpenAPI 类型生成。 |
| W.2 | 任务列表与目标筛选；创建目标、领域背景、验收条目增删、金额/时间/并发预算、配置及固定版本、出网域名。 |
| W.3 | 初始化历史事件，再从最后版本建立命名 SSE 订阅；原生 EventSource 自动重连及 Last-Event-ID 续传，区分临时断线与永久关闭；切任务关闭旧连接并清空状态。 |
| W.4 | 完整事件数组保存在 Zustand；纯 reduce 按版本排序、去重并还原任务、事实、意图、Agent、验收；deriveGraph 产生四类节点和六类关系。争议状态使用后端事件，不自行裁定。 |
| W.5 | React Flow 自定义节点、方向箭头、ELK LEFT→RIGHT 布局、300ms 去抖、缩放和小地图。未交互时新增拓扑自动适配；交互后保持视口；未受影响节点保留坐标。 |
| W.6 | 基础对象详情、关联跳转、证据摘要、任务目标与验收、基础 Agent 状态；已选对象状态跟随 SSE，键盘可操作、Esc 关闭详情。 |
| W.7 | Makefile 追加 web-install/dev/build/check/types。 |

页面：`/login`、`/tasks`、`/tasks/new`、`/tasks/:id`。主要模块：App、LoginPage、TasksPage、NewTaskPage、TaskWorkbenchPage、DetailPanel、TopologyFlowCanvas、layout、board reducer/store/graph、API client/events。React Query 负责请求缓存，CSS Modules 与全局 OKLCH tokens 统一样式。

## 类型与数据

`web/src/api/schema.d.ts` 使用 openapi-typescript 从 M1b 的 `services/blackboard/openapi.json` 生成。请求/响应与 Event 类型来自生成文件；`board/types.ts` 描述事件还原后的显示结构。当前已接真实后端，不引入运行时 MSW 模式。

网络版本去重用于防止同一事件重放两次，与事实语义去重无关。按照用户的新设计，事实/意图是否重复仍由 Agent 基于同步黑板自行判断，没有本地模型或相似度步骤。

## 实际验证

- `uv sync --locked` 与 `pnpm --dir web install` 成功；pnpm 使用指定临时缓存及项目级镜像。
- `make web-types` 成功；OpenAPI 快照检查通过。
- 最终 `make web-check`：ESLint、TypeScript strict、Vitest **5 个文件 / 10 项测试通过**，覆盖乱序、重复事件、争议、生命周期、图关系、SSE 重连/关闭以及增量布局。
- `make web-build` 成功。初始路由 JS 约 220 KB（gzip 71 KB），画布/ELK 单独按需加载约 1.66 MB（gzip 512 KB）；构建器对该大块有提示，未隐藏警告。大图性能与进一步分包留 M4。
- `make check`：Ruff、Pyright 无错误，**141 passed**。
- `make test-integration`：**11 passed**，使用构建代理参数；含前序存储、envd、MinIO、API 与 SSE 测试。
- 构建带 `web/dist` 的 `bbx-blackboard:latest` 成功；镜像内 ASGI 烟雾检查验证首页、SPA 深链、实际 JS 资源返回 200，未知 `/api` 返回 404。
- 浏览器使用独立 PostgreSQL/MinIO/API、随机基础设施端口和测试账号完成真实交互检查：登录、任务列表、中文目标与两个验收项创建、固定 profile 版本、启动进入 provisioning、停止进入 closing、SSE 新事实/意图/认领/Agent 状态、节点详情、争议标记、缩放、小地图。
- 1440×900 与 390×844 视口检查通过；详情面板已修复长关联陈述造成的横向溢出。通过 DOM 实测确认面板 scrollWidth 与 clientWidth 一致。
- 实测未交互时新增节点进入视野；用户选择后新增节点与心跳不清空详情，视口 transform 保持不变。最终浏览器控制台无 error。
- 临时浏览器、视口覆盖、Vite/API 进程与测试容器均已清理。未读取 `.env`，未调用真实模型。

## 检查点命令

```sh
cd ~/blackboard-explorer
make web-install web-types web-check web-build
make check
# 使用 envd README 中的 DOCKER_BUILD_ARGS 时沿用同一值
make image-blackboard test-integration
```

由操作人准备配置并启动平台后，可运行真实 HTTP 模拟器观看事件变化：

```sh
make up
.venv/bin/python -m bbx_blackboard.simulator --base-url http://127.0.0.1:58000 --scenario demo --interval 0.5
make web-dev
```

默认开发代理指向 `127.0.0.1:58000`；使用随机测试 API 端口时以 `BBX_API_URL=http://127.0.0.1:<port>` 覆盖。上述模拟器不调用 DeepSeek；真实 Agent 运行在后续 M2 接入。

## 偏差与待决

- 浏览器发现前序模拟器只将任务结束、未结束 Agent。已通过独立 M1b 修复分支提交 `eed65d4` 并合并：7 个 Agent 均 finish，进入 closing 前后有 conclude，SSE 全量对齐测试通过；本分支已同步该修复。
- 为避免最初仅有目标时新增节点落在屏外，画布在首次用户交互前跟随拓扑变化；交互后保留视口，心跳/状态变化不会触发自动适配。这保持了增量浏览的可读性。
- 证据内容查看器、时间轴、完整 Agent 面板、报告页、profile 编辑、过滤折叠与大图优化按范围留 M4；本次只提供摘要、基础详情及产物链接。

## 共享修改与提交

本任务仅修改 `web/`、追加 Makefile 目标和本报告；前序模拟器修复已独立提交。建议完整提交：`Implement M1-W live blackboard canvas`。M1a、M1b 与 M1-W 均合并后可打 `m1` 标签，再开始 M2a。
