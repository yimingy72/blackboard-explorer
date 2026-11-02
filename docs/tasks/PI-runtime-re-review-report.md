# PI 运行时迁移复审

日期：2026-10-30。工作树 `/Users/yym/bbx-wt/platform-performance-audit`，分支 `feature/pi-runtime-migration`。审查当前未提交的 PI 迁移实现及其调用方，不把前序 CTF 性能/参数反馈改动混算为迁移新增问题。

**源码交付（2026-11-02）**：五项修复已随三个独立提交保存，完整源码交付和本轮重新验证见[交付报告](PI-runtime-delivery-report.md)。下文保留复审时的证据和位置；尚未合并main或部署。

**修复状态（2026-10-31）**：以下五项现均已修复，新增21项普通回归和4项HTTP集成场景；最新2605普通及15项相关集成通过，详见 [修复报告](PI-runtime-re-review-fix-report.md)。尚未部署。正文保留复审时的问题与位置，作为历史依据。

2026-10-30本轮只审查、离线复现和记录，当时未修改生产代码、设计、配置或运行服务。**当时发现2项P1、3项P2，建议先修复再发布。** 上一轮2584普通、430隔离集成及155前端检查确实通过，但覆盖不足以证明以下边界正确。

## 1. P1：外部 MCP 的工具目标可以被参数覆盖

位置：`services/agent-runtime/src/bbx_runtime/pi/tools.py:170`，调用方为普通 Explore 的 `runner.external_mcp_tools`。

- 作用：把任务固定的外部 MCP 工具白名单转换为模型可调用的工具。
- 问题：动态包装器使用 `invoke(_remote_name=remote_name, **arguments)` 固定远程名称，但模型参数可以覆盖该默认参数。输入 schema 允许额外字段时，PI 原生校验不会拒绝 `_remote_name`；`call_tool` 又没有核验白名单。
- 实际复现：白名单仅有 `allowed_read`，在真正的 PI Agent 循环中调用该工具并带上 `_remote_name=not_allowed_write`，模拟 MCP 会话实际收到的目标为 `not_allowed_write`。未连接任何真实 MCP 服务或执行写入。
- 最小修复：将远程目标放入不可通过关键字覆盖的闭包，模型参数以独立字典传入 SDK；派发前再次检查固定白名单。也应覆盖远程工具本身合法使用 `tool_name` 等参数名的情况，避免与包装器参数冲突。
- 不修复的后果：普通 Explore 可以调用未被任务授权的远程工具；当前工具白名单不能作为可靠边界。

## 2. P1：外部追踪器的“仅元数据”限制失效

位置：`services/agent-runtime/src/bbx_runtime/pi/observability.py:16`、`:47`、`:64`，边界定义在 `traffic/observation.py:register`。

- 作用：接入平台逻辑流量采集，同时兼容进程中已经存在的 OpenTelemetry 提供方。
- 问题：已有外部提供方时，`_sensitive_pipeline=False`，界面也声明只采集元数据；新 PI 适配器的 `set_preview` 仍把正文直接写入共享 span。事后在平台自有 Processor 中脱敏，不能撤销其他 exporter 已读取的属性。
- 实际复现：只安装内存 exporter，注册平台 collector 后确认限制标志为 false；模拟工具参数 `synthetic-private-task-material` 仍出现在该外部 exporter 的 `gen_ai.tool.call.arguments` 属性中。未使用网络 exporter。
- 最小修复：在设置正文属性之前统一执行现有敏感数据开关；模型输入、输出和工具参数、结果均须受同一限制。原生 PI span 的属性也只转发允许的安全元数据，不能依赖后置 Processor 承担所有导出前保护。
- 不修复的后果：开启外部追踪的部署可能把任务正文发送到外部追踪后端，与平台既有行为和提示不符。

## 3. P2：Gemini 限流被误判为不可恢复异常

位置：`services/agent-runtime/pi/worker.mjs:253`，下游为 `pi/client.py` 的错误分类及 `ctf/runner.py:575` 的恢复准入。

- 作用：复用 Google 原生 SDK，同时把安全的错误元数据交给平台现有恢复机制。
- 问题：Google 适配器不接受自定义 fetch，因此绕开 `observeResponse`；锁定的 PI 版本在 Google 请求失败时也不调用这里传入的 `onResponse`。状态码没有传回 Python，429最终变为 `unknown`、`transient=false`。
- 实际复现：以进程内模拟 fetch 向真正的 SDK 返回相同429；OpenAI链路得到 `rate_limit/429/transient=true`，Gemini链路得到 `unknown/null/transient=false`。所有请求均由模拟函数直接响应，没有网络模型调用。
- 最小修复：在 Node 的实际 HTTP 边界保留状态码、Retry-After 和安全请求标识，交回现有分类器；继续由 PI/厂商 SDK 负责协议，不分析自由格式错误正文或增加第二套重试循环。Vertex共用的观察缺口也应补充对应检查。
- 不修复的后果：CTF 队友不会进入既有 `provider_transient` 恢复路径，本可退避处理的限流会升级为执行异常。此处只证明 Gemini 429，未宣称所有云厂商故障都已验证。

## 4. P2：运行时只读取外部 MCP 工具目录第一页

位置：`services/agent-runtime/src/bbx_runtime/pi/tools.py:161`。

- 作用：加载用户在模型配置页面选择的外部工具。
- 问题：配置页面的 `platform.list_mcp_tools` 已正确处理 `nextCursor`，但新运行适配器只调用一次 `list_tools`。用户可以选择后续页的工具，运行时却看不到该工具。
- 实际复现：模拟两页目录、白名单只选择第二页工具；只观察到一次无 cursor 请求，暴露给模型的工具列表为空。
- 最小修复：沿用现有配置端的 cursor/重复游标检测做法，遍历目录，再按任务固定白名单筛选；不根据目录变化自动扩大权限。
- 不修复的后果：配置成功但工具实际缺席，Agent 会误判工具不可用或寻找低效替代方式。

## 5. P2：外部 MCP 的嵌入式文本资源被静默丢弃

位置：`services/agent-runtime/src/bbx_runtime/pi/tools.py:217`。

- 作用：把 MCP 工具结果传给 PI 和持久会话。
- 问题：新转换器只处理 `text`、`image`，其他合法内容类型没有保留或降级。MAF 原有转换器明确支持 `EmbeddedResource(TextResourceContents)`；迁移后该结果会变成空列表。
- 实际复现：模拟 MCP 返回内嵌文本资源 `synthetic required evidence`，新适配器返回 `[]`，内容既没有交给模型，也没有留在此工具结果中。
- 最小修复：使用官方 MCP 内容类型转换内嵌文本资源；对资源链接及暂不能直接输入模型的类型保留规范的文本/元信息表示，避免静默丢弃。保持原有 structured-first 策略，不能用整体字符串化替代所有图片处理。
- 不修复的后果：检索/证据工具表面调用成功，Agent 却收不到已有证据，会重复调查或给出不完整结论。

## 本轮检查与范围

- 已读 PI 原生循环、Python/Node 边界、消息序列化、会话与费用 checkpoint、流式/取消、MCP 生命周期、模型/工具追踪及普通/CTF/成果整理调用方。
- 重点核对当前共享 runtime 的多任务和多 Agent 场景，保留单会话写入、预算及未知副作用不重放的约束；未把已记录的每客户端 Node 进程内存成本改造成无证据的性能缺陷。
- 运行6个离线场景：白名单覆盖、目录分页、资源转换、外部追踪，以及429的OpenAI对照与Gemini场景；以上5项问题均有实际复现输出。
- 复现文件和结果保存在忽略的 `.data/pi-migration/review/`，只用合成内容、假 MCP 会话/模型响应和内存 exporter；未读取 `.env`、使用真实密钥、连接模型服务、靶场或生产数据库。
- 本轮未修改生产源码，因此未重复整仓检查或重建镜像；上一轮全量通过结果仍是同一候选实现的回归证据，不能计作这些新缺陷的修复验证。

复现命令（在本工作树执行）：

```sh
.venv/bin/python .data/pi-migration/review/probes.py
.venv/bin/python .data/pi-migration/review/telemetry_probe.py
.venv/bin/python .data/pi-migration/review/provider_probe.py
```

建议修复分为三组：MCP 白名单/分页/结果转换；追踪出口权限；Google 错误元数据与恢复衔接。每组将本轮反例补为正式回归，再运行相关检查；最终发布前重新检查候选源码和镜像一致性。设计所规定的 Pi 原生循环、PG 事务与执行隔离边界无需因此改变。
