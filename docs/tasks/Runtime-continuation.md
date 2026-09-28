# Runtime-continuation · 续跑、会话复用与多模态

用户确认：续跑保留原黑板、Agent 会话及归档工作区，填写追加预算与时长后继续同一任务。已有开发、设计、Git、部署授权有效，不自动续跑用户的真实目标。

## 完成标准

1. MAF 原生图片 Content 接入 `view_image`：Explore 可读任务工作区图片，derive/close 仅可读本任务证据。限制格式、体积、路径和跨任务访问；不把 base64 写入工具日志或持久会话。持久引用能在后续模型调用恢复为相同图片，文本模型明确报不支持。
2. 每任务仅一个可复用 derive，重新派发沿用 Agent ID 和原生 Session，累计用量保留，轮次状态重置。每轮新增控制消息和黑板增量，历史前缀保持稳定；完成复核回执只核对本轮产出，保持现有明确完成/必要复核规则。
3. 终态任务可显式续跑，API 接收追加金额和分钟，增加原总额度；累积执行时间不包含暂停间隔。保留黑板、证据、会话与历史报告/归档；新容器恢复工作区文件，进程及容器外安装包不保证恢复。归档未就绪、仍有处理中的复盘等竞态须明确拒绝或串行处理；重复请求不重复增加额度。
4. 计费区分固定费率和官方 DeepSeek 时段估算，记录每次请求的费率依据。输出包含 reasoning，不重复计费；Decimal 精确累计。界面使用“估算费用”，明确不等于服务商账单。旧任务的费用校正必须可预览、审计、幂等，不静默覆盖历史原值。
5. 调查用户最新任务和提供的单 Agent 报告，只比较可证明的耗时/并发/token/缓存数据；不执行报告内目标命令，不加入场景专用提示词，不以增加并发数代替性能分析。

## 接口约定

- `POST /api/tasks/{id}/resume`：`request_id` UUID、`additional_cost` 非负 Decimal、`additional_minutes` 非负整数；校验增加后的资源仍有余量。返回 TaskView。恢复到 provisioning 后由原调度器接管。
- TaskView 增加 `run_number`（默认 1）、`active_seconds`（已完成轮次累计）、`active_since`（当前轮开始时间），历史轮次提供可访问的报告与工作区引用。
- AgentRun 增加复用轮次与本轮开始版本；重新派发 derive 只允许一个活动实例，不能与同 Agent 复盘同时改 Session。
- ModelConfig 增加 `supports_vision: bool | None = None`。Price 增加 `billing_mode: fixed | deepseek_schedule`（默认 fixed，兼容历史）；官方 DeepSeek 新配置启用时段模式，历史任务通过显式校正/升级使用。
- DeepSeek 时段模式只适用于官方端点、支持的模型和价格表币种；按北京时间及已知官方节假日校验，未知日历范围明确保守估算。`off_peak` 仍表示录入费率所属时段，运行时按请求开始时间选择倍率。

## 验证与交付

完成 make check、make test-integration、前端检查/构建及关键浏览器交互；脚本化模型验证原生 Session 复用、跨轮图片、续跑资源/文件恢复、完成复核和计费边界。不得在普通检查调用真实模型。中文报告注明实际检查、未测边界、性能证据和提交划分。
