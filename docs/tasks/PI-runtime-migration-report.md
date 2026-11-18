# PI 运行时迁移报告

日期：2026-10-30。工作树 `/Users/yym/bbx-wt/platform-performance-audit`，分支 `feature/pi-runtime-migration`。设计已先独立提交 `c421120`；前序 CTF 性能后续与执行前参数反馈的未合并改动完整保留。

**源码交付（2026-11-02）**：前序CTF、迁移和三组复审修复已按依赖独立提交，迁移主提交为 `5a0a7b9`；当前2605普通、155前端、5原生及8项定向集成通过，候选72文件一致。完整分组、检查和合并边界见[交付报告](PI-runtime-delivery-report.md)。未合并main、推送或部署。

**最新修复（2026-10-31）**：[复审](PI-runtime-re-review-report.md)发现的2项P1、3项P2均已修复。新增21项普通回归及4项HTTP集成场景，最终2605普通、15项相关集成及5项原生Node检查通过。新候选为 `bbx-pi-runtime:20261031`，容器内烟测与13项源码摘要核对通过，详见 [修复报告](PI-runtime-re-review-fix-report.md)。未合并、推送或部署。

## 实现

1. 移除全部 `agent-framework-*` 生产依赖，`uv sync` 实际卸载8个框架包及其专属依赖，共34包。生产代码不再导入 `agent_framework`。
2. `pi-agent-core 1.4.0` 执行真正的模型/工具循环、schema校验、顺序/并行执行及继续生成；不是给 MAF 换名字，也没有复制一个 Python 工具循环。
3. `pi-ai 1.4.0` 接入现有模型配置；流式、图片、思考、工具协议、缓存及输入输出用量由 PI 适配器处理。保留平台配置标识、上下文/输出额度、角色覆盖和人民币账本；映射为 PI 原生 provider 标识及 API。
4. 普通 Explore/Derive/Close、CTF Lead/队友、成果整理及终态复盘共用 PI 执行边界。Python 继续负责原有业务工具、邮箱/黑板、权限、预算、CAS/outbox 和执行容器，不引入第二个调度器。
5. 旧会话外层格式兼容读取，不重写业务数据库。新响应保存 PI 工具 ID、文本/思考签名和提供方元信息；转换区分 Responses 未加密摘要和加密内容，使既有网关观察投影继续生效。原始历史不因请求兼容而删除。
6. 模型完整响应与费用收据先持久化，再放行其工具。CTF 借 PI 原生 `onPayload` 扩展点确认即将发送，格式构造失败与准入拒绝不会先调用模型。未知写入、权限/代次错误及无法确认的 checkpoint 会退出原生循环并取消同批挂起工具；不重放命令。
7. 通过现有 MCP SDK 管理连接、发现、调用和校验；命令仍使用原单派发边界。服务端工具目录变化不自动扩大任务固定白名单。明确的参数/领域拒绝保留结构化反馈，工具观测和保存结果保留错误标识。
8. `pi-telemetry 1.4.0` 原生请求追踪回接现有 OTel/流量归档，工具逻辑调用继续按 Agent 归属。预览有界脱敏，图片二进制不塞入逻辑预览；原有 PCAP/HTTPS/协议解析保持。
9. Node 进程按运行传递上下文和调用 ID，避免复用 reader 的旧上下文；版本与启动就绪先检查，进程断开不自动重放在途调用。流式生产路径有背压，不在多层包装中重复保存所有 token 更新。
10. runtime 镜像包含 Node 22 与锁定的 PI 依赖。平台最低 Node22.21，复用其环境代理支持并保留 HTTP(S)_PROXY/NO_PROXY；不加载 `~/.pi`、CLI 扩展、默认本地读写/bash 工具或 `.env`。

## 组件复用与边界

| 组件 | 本轮取舍 |
| --- | --- |
| pi-ai / pi-agent-core | 直接使用原生 API 与工具循环；离线模型仅替换模型响应源，工具循环仍为实际 PI |
| pi-telemetry | 使用原生追踪契约/参考实现，再适配现有归档处理器 |
| pi-durable | 已审查。官方仍标 experimental，现成存储没有 PostgreSQL，且有自己的任务、恢复和重试语义；本轮沿用已验证的 PG 事务/围栏，避免与现有编排并列 |
| chord | 已审查其 JSON link/宿主与状态复制。没有现成 Python 端；本轮只需要本地语言桥接，不为此引入插件宿主和第二套复制状态 |
| pi-coding-agent / pi-tui | 当前产品为网页协作平台，执行在每任务容器；不接入 CLI 自动资源发现及本地文件工具 |
| eventsource-parser 4.1.3 | 只对 SDK 抢先抛出的 SSE 错误提取有界、无正文的诊断标识；使用现成解析器，原字节不变，模型协议继续交给 PI |

官方依据：[PI 包目录](https://github.com/earendil-works/pi)、[原生 Agent](https://github.com/earendil-works/pi/tree/main/packages/agent)、[durable 边界](https://github.com/earendil-works/pi/tree/main/packages/durable)、[Node 环境代理](https://nodejs.org/download/release/v22.23.0/docs/api/cli.html#--use-env-proxy)。

## 初版实际检查（2026-10-30）

- 迁移前 `make check`：2601通过、432未选，Ruff/Pyright通过。
- PI 原生 Node 检查：5通过，含持久化回调先于工具、schema拒绝无副作用、OpenAI/Anthropic/Google原生图片与协议。
- 新 PI 适配/网络离线检查：18通过，含旧/新签名、逐run上下文、发送围栏、格式拒绝无调用、流式背压、异常用量、`max`及实际缓存计数。
- 本机动态端口模拟代理：2通过，实际验证 Node 原生 HTTP 代理与 NO_PROXY，不连接真实模型。
- 候选镜像已成功构建为 `bbx-pi-runtime:20261030`，容器内 Node 为22.23.4。最终逐字节摘要核对13个 PI Python/Node/依赖清单文件，与当前工作树一致；镜像内没有 `agent-framework-*` 包。
- 最终 `make check`：**2584 passed，434 deselected，169.74秒**，Ruff/Pyright和5项Node检查全绿。
- 前端：**155 passed**，ESLint/TypeScript通过，生产构建成功，原有chunk体积提示保持。
- Kali实际容器/垂直链：**6 passed，36.07秒**，包括输入拒绝后同turn纠正、同步/异步身份及回收、长命令期间收信并续接。
- runtime镜像隔绝外网的实际烟测：由镜像内PI完成2次模拟模型请求、1次工具副作用，缓存命中计数14，全部请求为流式。
- 首轮完整隔离集成422通过、6失败、2跳过：三项依赖已清理的旧专用镜像，现改用当前执行镜像；状态重放补稳定排序；调用实例ID增加run隔离，避免重复厂商call_id碰撞。
- 整理预算断言在未修改的main/MAF基线也失败（单独隔离复现）；已按现有预算合同区分budget_exhausted与paused，没有放宽预算或改变生产结算。
- 上述失败定向复查**7项全通过**。发现的MAF私有客户端/序列化断言已改为真实PI请求验证；注入丢失、额外收尾请求假设按新行为修正，不重建旧框架缺陷。
- 最终完整隔离集成：**430 passed，2588 deselected，649.05秒，没有失败或跳过**。普通/CTF两种模式的流量碎片归档与容器销毁后读取均实际通过；测试兼容标签只指向同一个当前执行镜像，不替换运行镜像，完成后已移除该临时标签，当前执行镜像仍保留。

复现入口：

```sh
UV_CACHE_DIR=/private/tmp/bbx-uv-cache uv sync --locked
make pi-install
make check
BBX_RUNTIME_TEST_IMAGE=bbx-pi-runtime:20261030 BBX_EVAL_TEST_IMAGE=bbx-pi-eval-env:20261030 .venv/bin/pytest -m 'integration and not live'
```

上述检查不读取 `.env`、不调用真实模型或外部题目，不自动续跑业务任务。

## 共享文件与合并范围

- `Makefile`：增加锁定 Node 依赖安装及 PI 原生检查入口，纳入现有 `make check`。
- `uv.lock`：移除 MAF 框架及其专属依赖，保留现有 Python 服务和厂商鉴权所需依赖；Node 依赖独立锁在 `services/agent-runtime/pi/package-lock.json`。
- 本轮 PI 迁移不增加数据库迁移、模型配置字段或跨服务 API。工作树中 `packages/contracts`、OpenAPI/前端生成类型、envd 异步命令及角色参数表单的相关变动来自前序两轮任务，不能误归为 PI 新功能或遗漏提交。
- `services/blackboard/src/bbx_blackboard/ctf.py` 中快照稳定排序是完整集成发现的独立修复；避免依赖数据库偶然返回顺序，不改变记录内容、权限或事务边界。

## 偏差与待决

- 保留了 Python 会话外壳、业务 hook 与语言适配，因为旧 PG 快照、投递确认、用量 outbox、代次/收尾证明必须继续共用事务。它们不负责模型协议或模型/工具循环。
- Foundry 项目鉴权/端点和 Anthropic Vertex 的入口差异使用厂商 SDK或 PI 的传输扩展；没有自行解析这些模型的正文协议。云端真实账户连通性及真实求解速度未由离线检查证明。
- 每个存活模型客户端复用一个受控 Node 进程，空闲单进程实测约61MiB。未引入共享进程池；高并发容量仍需在真实负载下核验，本轮不宣称替换框架即能提高解题速度。
- PI 的代理组件不支持纯 SOCKS/PAC；模型侧使用 HTTP(S) 代理入口，执行容器的出网策略不变。没有静默修改用户代理配置。
- 没有更新本地或公网服务、修改已有任务/预算/配置、迁移数据或续跑失败任务。生产版本仍以 HANDOFF 的已部署记录为准，候选镜像不替换运行镜像标签。

## 提交划分（2026-11-02 已执行）

1. 已提交设计：`docs: define native Pi runtime migration boundaries`。
2. 前序 CTF 性能和参数反馈分别为 `b69669c`、`1b5b69d`，均在独立导出的提交源码上重新通过完整检查。
3. CTF 快照排序独立修复为 `260673f`；原有 Node 最低版本说明单独提交为 `8a97c71`。
4. PI 组件、适配、模型/会话、业务接入与运行所必需的镜像/依赖/验证统一提交为 `5a0a7b9`：`refactor: replace MAF execution with native Pi runtime`，避免中间提交缺少运行依赖。
5. MCP、追踪、Google三组修复分别为 `a178b47`、`7c319e9`、`640ec64`；报告及HANDOFF单独提交，详见交付报告。

设计、实现与报告已在任务分支完成提交，尚未合并main或推送GitHub。无需用户补跑沙箱外检查；真实模型兼容性与负载表现仍属后续验证。发布需继续保留原数据库、对象存储、配置和可回退镜像，在无活动任务的窗口切换。
