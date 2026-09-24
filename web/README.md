# 黑板画布

React 18、TypeScript、Vite、React Query、Zustand、React Flow 与 ELK。接口类型从已提交的 OpenAPI 生成；运行时使用真实 HTTP API 与 Cookie 登录。

```sh
make web-install
make web-types
make web-check
make web-build
make web-dev
```

开发服务器默认将 `/api` 代理到 `http://127.0.0.1:58000`。使用独立测试服务时可覆盖目标：

```sh
BBX_API_URL=http://127.0.0.1:<随机测试端口> make web-dev
```

pnpm 缓存在 `/private/tmp/bbx-pnpm-store`，项目级 npm 镜像配置见 `.npmrc`。构建产物位于 `web/dist`；执行 `make image-blackboard` 后，由 blackboard 静态托管，支持页面深链。

页面包括 `/login`、`/tasks`、`/tasks/new`、`/tasks/:id`。工作台展示四类节点、六种关系、实时状态和对象详情。初始事件按版本还原；后续使用命名 SSE 事件，原生 EventSource 负责断线与 Last-Event-ID 续传。

未操作画布时，新增拓扑自动进入视野；平移、缩放或选择节点后保持视口。切换任务会关闭旧订阅并重置状态。事件去重只处理网络重传的同一版本，不判断事实或意图的语义重复；语义重复由 Agent 根据黑板同步自行判断。

真实 Agent 运行由后续 runtime 提供。当前可启动黑板后运行 M1b 的 HTTP 模拟器，查看节点生长、认领、争议和生命周期变化；模拟器不调用模型。证据查看器、时间轴、完整 Agent 面板与 profile 编辑属于 M4。
