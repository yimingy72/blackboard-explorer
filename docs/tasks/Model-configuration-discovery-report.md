# 模型配置与发现交付报告

日期：2026-10-19。工作树 `/Users/yym/bbx-wt/platform-performance-audit`，分支 `fix/progress-curator-response`。本轮接续此前尚未提交的整理失败修复，未覆盖或清理旧改动。

## 方案与复用

沿用已批准的 B2 视觉规范，由主会话组织“连接设置 → 模型与输出 → 人民币计费”布局。复用 Ant Design AutoComplete、Select、InputNumber、Collapse 和现有 Form、凭据表单、配置存储与默认模型机制；MAF 继续承担各厂商执行连接器、AgentSession 和工具调用。

检查了本地 MAF 及已安装 SDK，并检索了成熟方案。MAF 没有统一的模型目录和完整配置管理 UI。AI Elements ModelSelector 只提供选择交互，依赖另一套 shadcn/Tailwind UI；LiteLLM 的发现和元数据能力成熟，但仅为配置页加入整套 SDK/Proxy 不合算。本轮未新增依赖，模型发现使用现有 httpx 调用标准元数据接口；公共参考使用 models.dev JSON API。

参考：[Ant Design AutoComplete](https://ant.design/components/auto-complete-cn/)、[AI Elements ModelSelector](https://elements.ai-sdk.dev/components/model-selector)、[models.dev](https://github.com/anomalyco/models.dev)、[LiteLLM 模型资料](https://docs.litellm.ai/docs/completion/token_usage)。

## 实现

- `ModelConfig` 增加最大输入/输出字段，默认上下文为 1,000,000。历史显式 null 快照继续兼容；配置页保存空上下文恢复 1M。共享 helper 解析实际输出和可用输入，输入为输出留空间，不要求两个独立最大值之和小于上下文。
- 普通 Explore/derive/Close、会话、CTF Lead/队员、成果整理统一模型请求的 max_tokens；CTF 与整理预留采用同一有效输出。删除整理页单独输出/推理控件，显示模型来源与实际上限。旧整理覆盖字段仍可解析，但不再控制执行。
- 新任务与运行时上下文阈值采用可用输入容量；不改写已有任务快照。配置输出优先，旧快照保留兼容默认（已知 DeepSeek 393216、其他模型 8192），界面可查看实际生效值。
- 大输出上限导致下一调用预留不足时明确显示 budget_exhausted，失败等待队列并停止自动重试。只处理平台 reserve 的 402，不将模型提供方 402 或未知用量误当未执行，不增加预算。
- `POST /api/platform/models/discover` 支持 OpenAI Chat/Responses/兼容、DeepSeek、Mistral、Anthropic、Gemini、Ollama 的模型目录。其他 Provider 明确提示手填。超时、响应大小、分页及详情并发有界，错误不回显提供方正文。
- 已保存凭据仅在 Provider、地址与连接选项一致时复用；地址变更需明确录入新凭据。认证、原有保存/删除/默认选择和草稿保护保留。
- `GET /api/platform/model-catalog` 缓存 models.dev 参考资料，显式选厂商和模型后应用能力，不改网关 ID。未知能力保持未知，输入/输出/缓存参考价保留原币种；用户填汇率并点击确认才导入人民币单价，不冒充完整分档或网关费率。

共享合同及生成 JSON Schema、OpenAPI、前端类型同步。主要新增代码是共享限制 helper、受限发现接口和现有表单的发现字段，不新增模型执行框架、会话系统或前端依赖。

## 验证

- `make check`：2335 passed、419 deselected，ruff、pyright 通过；未调用真实模型。
- `pnpm check`：155 passed，eslint、TypeScript 通过；`pnpm build` 成功。
- 相关 Playwright：26 passed，覆盖获取/选择/手填、迟到响应、显式参考导入、汇率、默认 1M、保存/删除/草稿保护、整理只读模型参数与 1440/390px 布局。布局截图由主会话复核。
- 整理数据库集成：28 passed。子代理另完成统一输出与预算/上下文定向测试，覆盖零输入空间、显式边界、预留不足恢复、未知用量不释放。
- 公共 models.dev API 只读核验 HTTP 200，约 5.8MB、232 个 Provider，当前字段结构兼容；未发起付费生成请求。

最终手机工具栏与获取模型行进一步压缩为常规双行操作区；桌面、390px 截图由主会话再次检查，2 项布局用例复测通过。Ant Design CLI 对新增配置组件检查为 0 问题。

## 发布与真实元数据验证

本地、公网均已发布 `model-config-20261019`。在没有活动任务、整理作业或待处理会话时切换；数据库 22 张业务表逐表摘要一致，MinIO 清单、数据容器、环境变量和端口配置未变。本地保留当时的 20 个任务、155 个会话、58906 个对象；公网保留 24 个任务、237 个会话、120066 个对象。此前备份继续保留。

两端真实配置页均通过已保存凭据读取到 37 个模型 ID；公共目录均返回 232 个 Provider，并读到 DeepSeek 的 4 个参考模型和输出限制。本地首次公共目录连接失败，独立直连复核及重试后 HTTP 200；没有修改全局代理、Docker 设置或 `.env`。该验证仅读取模型元数据，没有调用生成端点、创建任务或保存配置。

测试入口：[本地模型配置](http://127.0.0.1:58000/profiles?section=models)、[公网模型配置](http://101.201.116.63:58000/profiles?section=models)。审计材料和截图位于忽略目录 `.data/model-config-release/`、`.data/model-config-discovery/`。

## 偏差与边界

模型列表 API 通常只返回 ID，并不统一提供上下文、输入输出或价格。因此发现结果仅采用明确字段，公共目录用于显式补充。参考价只映射基础费率，不将分档价、汇率或网关加价自动猜成实际账单。

已有任务保持创建时模型快照；调整配置对新任务生效，已有任务沿用自己的模型版本。统一上限也会影响保守预留；额度不足时应明确调整所用模型上限或整理独立额度，平台不会擅自放宽费用限制。

建议提交：`feat: unify model token limits across workers`（合同、运行时、上下文/预算及测试）；`feat: discover models and expose configuration metadata`（平台接口、配置 UI、生成资料、测试与本报告）。前一轮整理修复可先按其报告独立提交。本轮尚未提交或合并。
