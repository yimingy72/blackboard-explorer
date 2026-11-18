# PI 迁移复审问题修复报告

日期：2026-10-31。工作树 `/Users/yym/bbx-wt/platform-performance-audit`，分支 `feature/pi-runtime-migration`。对应 [2026-10-30复审报告](PI-runtime-re-review-report.md) 的2项P1、3项P2，五项均已修复并验证。

**源码交付（2026-11-02）**：本轮三组修复已分别提交为 `a178b47`、`7c319e9`、`640ec64`，并在分层导出的源码上重新验证。当前完整检查仍为2605普通全绿，详见[交付报告](PI-runtime-delivery-report.md)；尚未合并main或部署。

## 修复内容

| 复审项 | 实现与验证 |
| --- | --- |
| P1：MCP调用目标被覆盖 | 为每个远程工具建立独立闭包；目标参数仅按位置传入，模型字段保留在独立参数字典中；白名单冻结并在调用SDK前再次校验。真正的PI工具循环验证 `_remote_name` 无法改目标，合法 `tool_name` 字段正常传递，另一白名单工具也不被串绑；直接越权派发被阻止。 |
| P1：外部追踪输出正文 | 模型输入/输出、工具参数/结果在写span前统一遵守现有内容采集开关。外部提供方仅得到元数据，自有采集保持脱敏预览。原生PI属性按其已有追踪合同筛选并在转交前脱敏，不转发任意额外属性。内存exporter实测四种正文均受控。 |
| P2：Google限流被误判 | Google不支持PI自定义fetch且缺少有效的响应回调，因此使用Node原生AsyncLocalStorage为其实际SDK请求绑定仅观察响应头的回调。保留状态码、请求ID和Retry-After，原Response及流体原样交给SDK，不增加重试或另写模型协议。Gemini/Vertex的429、503可恢复，401仍为鉴权错误；同进程并发的两个请求不串响应元数据。 |
| P2：MCP目录缺页 | 复用SDK cursor分页，采用平台现有的重复游标检查方式；完整读取后一次发布工具目录，再按固定白名单暴露。选择第二页工具和同连接重启均通过，重复游标不暴露半份目录。 |
| P2：MCP嵌入资源丢失 | 按官方MCP类型读取内嵌文本及资源来源信息；资源链接和其他类型保留规范JSON表示，不静默丢弃。文本证据确实进入PI下一轮上下文与会话，原生图片及既有structured-first结果策略保持。 |

生产改动集中于 `services/agent-runtime/src/bbx_runtime/pi/tools.py`、`pi/observability.py` 和 `services/agent-runtime/pi/worker.mjs`。继续复用PI循环、PI模型适配器、MCP SDK及现有平台恢复/预算路径，没有新增运行框架、依赖、重试循环或模型配置项。

新增正式检查为 `test_pi_mcp.py`、`test_pi_observability.py`、`test_pi_google.py`，共21项普通回归；既有代理集成增加4项Google成功/限流场景。CTF的实际运行器回归证明限流进入 `provider_transient`，已发送请求的不确定预算保护保留，不因恢复而释放未知费用或重放命令。

## 实际检查

- `make check`：**2605 passed、438 deselected，171.53秒**；Ruff、Pyright均通过，另含5项PI原生Node检查。
- 定向PI/MCP/恢复/追踪回归：124 passed。随后全量检查包含最终格式及类型修正后的版本。
- 实际HTTP代理/直连集成：**6 passed，3.40秒**；OpenAI原链路保持，Google成功流、429、请求ID、Retry-After和NO_PROXY均验证，每个模拟调用只到达一次端点。
- 相关容器集成：**9 passed，115.80秒**；新镜像启动/互斥/正常退出、执行环境网络与文件隔离、CTF平台工具以及普通黑板/CTF的流量归档通过。
- 构建候选镜像 **`bbx-pi-runtime:20261031`** 成功。隔绝外网的容器内烟测通过：PI启动就绪、MCP目标/白名单及资源保留、外部追踪正文限制均符合预期，未安装MAF。Node为**22.23.4**。
- 镜像与当前工作树的13个PI Python/Node源码及依赖清单文件摘要逐一一致。
- `git diff --check`通过；前端代码未改，本轮没有重跑浏览器检查。上一版430项全量集成保留为历史验证，本轮运行上述15项相关集成。

复现入口：

```sh
make check
.venv/bin/pytest -q services/agent-runtime/tests/test_pi_proxy_integration.py -m 'integration and not live'
BBX_RUNTIME_TEST_IMAGE=bbx-pi-runtime:20261031 .venv/bin/pytest -q services/agent-runtime/tests/test_runtime_image_integration.py services/agent-runtime/tests/test_execenv_integration.py services/agent-runtime/tests/test_ctf_platform_vertical_integration.py services/agent-runtime/tests/test_traffic_archive_integration.py -m 'integration and not live'
```

日志与镜像核验材料在忽略的 `.data/pi-migration/review/`。所有检查使用模拟模型/MCP响应、内存exporter或随机端口的隔离容器，不连接真实模型或靶场、不读取 `.env`。Google/Vertex检查仅替换网络响应与云鉴权，协议及流式仍使用实际PI和厂商SDK。

## 边界与待决

- 五项复审缺陷已关闭；设计所规定的PI组件职责、PostgreSQL事务、工具权限和未知副作用保护保持，无新增设计待决或数据库迁移。
- 非图片二进制资源保留为规范表示，没有新增音频模型输入或媒体转换能力；真实云账户鉴权、真实模型表现及高并发容量仍需后续实测。
- 本轮没有再修改共享锁文件、API/生成契约或前端，也未重置前序CTF性能及参数反馈的未合并源码。
- 本地仍运行 `ctf-input-feedback-20261028`，公网未操作；任务、预算、配置、数据库及对象存储未迁移或修改，未续跑历史任务。新标签仅为候选，旧运行标签及镜像保持。
- 无需用户补跑沙箱外检查。实现仍在任务分支，未合并main、推送或切换运行服务。

## 提交划分（2026-11-02 已执行）

前序CTF源码与PI基础迁移已独立归组；本轮修复按原建议保留为三个提交：

1. `a178b47` — `fix: enforce pinned MCP tools and preserve MCP resources`：MCP适配及回归。
2. `7c319e9` — `fix: honor telemetry content capture permissions`：追踪出口及回归。
3. `640ec64` — `fix: retain Google provider HTTP diagnostics`：Node响应元数据、Google/Vertex及HTTP集成回归。
4. `docs: record Pi migration source delivery`：复审关闭记录、报告和HANDOFF状态，没有更改上述生产源码。
