# 任务 M1-W · 画布最小版

依据：`docs/design/黑板系统实现架构.md` 第 8 节（前端）；`docs/design/黑板系统开发方案.md` M1-W 与检查点；`packages/contracts/schemas/`（数据结构）；M1b 完成后的 `services/blackboard/openapi.json`。

可以与 M1b 并行开始。先阅读 `AGENTS.md`。

## 范围

只修改 `web/`，以及 `Makefile`（追加前端目标）。做：前端骨架、登录、任务列表与创建、SSE 驱动的拓扑画布、详情侧栏基础版。**不做** M4 的内容（证据查看器、时间轴、Agent 面板、报告页、Profile 管理、过滤折叠）。

## 已定的实现决定

1. **技术栈**：pnpm、Vite、React 18、TypeScript（strict）、react-router v6、`@tanstack/react-query`、`zustand`、`@xyflow/react` v12、`elkjs`。样式用 CSS Modules，不引入组件库。
2. **pnpm 存储**：Codex 沙箱写不了用户目录，安装时使用 `--store-dir /private/tmp/bbx-pnpm-store`（写进 `web/.npmrc`：`store-dir=/private/tmp/bbx-pnpm-store`）。
3. **类型**：
   - M1b 的 `openapi.json` 已存在时：用 `openapi-typescript` 生成 `src/api/schema.d.ts`，API 客户端基于它。
   - 尚不存在时：用 `json-schema-to-typescript` 从 `packages/contracts/schemas/` 生成类型，按实现架构 4.2 的接口清单写客户端；接口与 SSE 用 MSW（Mock Service Worker）模拟，事件来自一份录制好的事件样本 `web/src/mocks/events.demo.json`（覆盖事实、意图、认领、争议来回、裁定、agent 生命周期）。M1b 合并后改为 OpenAPI 生成，MSW 只保留给测试。
4. **状态**：`zustand` 中保存完整事件数组；`reduce(events) → { facts, intents, agents, acceptance, task }` 为纯函数，实时更新与（M4 的）时间轴回放共用。争议状态以后端给出的为准（事件载荷或 `/state`），前端不重新实现领域规则。
5. **画布**：节点 goal / fact / intent / agent，边 derived_from / based_on / resolves / disputes / retry_of / claim，视觉按实现架构 8.3 的表；`deriveGraph(state) → { nodes, edges }` 为纯函数；elkjs `layered`、方向 LEFT→RIGHT，新事件到达后 300ms 去抖重排。
6. **开发与部署**：Vite dev server 把 `/api` 代理到 `http://127.0.0.1:58000`；`pnpm build` 输出 `web/dist`，由 blackboard 静态托管。

## 任务

| # | 内容 |
|---|---|
| W.1 | 项目骨架、路由、登录页（`/api/login`）、API 客户端与类型生成脚本 |
| W.2 | 任务列表页；创建页（goal、domain_context、验收条目的增删、budget、profile 选择） |
| W.3 | SSE 接入：`EventSource` 断线自动重连并用 `Last-Event-ID` 续传；事件进入 store |
| W.4 | `reduce` 与 `deriveGraph` 纯函数 |
| W.5 | TopologyFlowCanvas：四种自定义节点、六种边、elkjs 布局、增量重排、缩放与小地图 |
| W.6 | 详情侧栏基础版：点击节点显示对象全文（`/objects/{oid}`） |
| W.7 | `Makefile` 追加：`web-install`、`web-dev`、`web-build`、`web-check`（eslint + tsc + vitest）、`web-types` |

## 测试

- vitest：`reduce`（含乱序到达、重复事件去重、争议来回）、`deriveGraph`（节点与边的种类、样式标记）、SSE 续传逻辑（模拟断线）。
- 不要求 Playwright（M4 再做）。

## 完成标准

1. `make web-check` 全绿；`make web-build` 成功；`make check` 不受影响。
2. 用户可执行的检查点（写进报告）：
   - M1b 未合并时：`make web-dev`，在 MSW 模式下播放 demo 事件，画布逐步生长、争议变色、意图状态变化、Agent 节点出现与消失。
   - M1b 合并后：`make up`，运行 M1b 的模拟器，浏览器中看到同样的效果。
3. 报告 `docs/tasks/M1-W-report.md`：页面与组件清单、类型来源、共享文件修改、建议提交划分、偏差与待决。
