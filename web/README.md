# 任务工作台

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

当前工作台支持登录、创建任务、实时黑板、证据与事件查看、Agent 状态、报告与工作区归档，以及 Profile 管理。真实任务由 agent-runtime 调度，启动和网络配置见[使用与部署](../docs/使用与部署.md)。开发时仍可使用不调用模型的 HTTP 模拟器验证画布事件；真实任务会调用配置的模型。

任务页使用紧凑顶栏，验收摘要与过滤器同排，全部已创建 Agent 持续显示在选择条中（包括已结束者）。点击 Agent 条目或拓扑节点打开右侧对话：初始上下文、逐轮黑板注入、模型实际返回的文本/推理、工具调用和结束回执按事件版本排列，正文按需读取。旧任务缺少的上下文不会补造。运行时长从任务开始计时，终态停止，回放截止于当时。创建页采用主编辑区和配置侧栏，长目标、背景与验收条件支持多行输入，窄屏自动堆叠。
