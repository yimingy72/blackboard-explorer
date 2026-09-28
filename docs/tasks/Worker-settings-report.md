# Worker-settings 报告

日期：2026-09-28。用户确认提示词更新在运行Agent下一次模型调用生效。

## 完成

- Worker配置直接按explore/derive/close分区编辑保存；移除配置名称、历史版本、发布版本、高级YAML、浏览器默认入口。底层快照与审计保留但不要求用户操作。
- 平台自动生成模型/MCP标识，界面仅显示名称与厂商模型ID。服务端保存平台默认模型，创建任务预选该模型并允许选择其他模型；提交固定页面所见连接版本，三角色共享本任务模型。保留领域背景和目标域名字段。
- 所有模型凭据平台加密保存，支持API Key、AWS凭据、Azure服务主体、Google服务账号JSON，查询只返回已配置字段名；旧DeepSeek环境key由服务内部幂等导入，不输出/不读取.env，不覆盖用户已存key。默认模型配置存schema0006 app_settings。
- MAF Python提供的连接器已接入：OpenAI Chat/Responses、Azure OpenAI两协议、Foundry、Foundry Local HTTP、Anthropic及Foundry/Bedrock/Vertex托管端点、Ollama、Bedrock、Gemini/Vertex、Mistral，加DeepSeek/兼容接口共17种连接方式。Provider目录驱动表单和字段验证；本轮仅安装Python连接器依赖，没有下载模型。
- 实际构造各provider原生客户端并转换工具/历史；统一关闭SDK底层连接及Azure凭据资源。Foundry Local连接用户已运行服务；不将.NET-only ONNX/Dapr伪装为Python可运行选项。推理强度仅在支持传参的连接器显示，模型默认表示不传参数，兼容端点需自行支持该参数。
- 系统提示词每次模型调用前按role拉取最新模板，按当前任务上下文渲染并替换单份instructions，不添加为普通用户消息。Session保存当前文本/revision，trace保留更新记录，重启恢复历史；同正文仅revision变化不重复注入。保存有CAS冲突保护及Jinja语法/变量校验。
- 工具、任务模型、预算与执行环境仍固定；提示词更新不打断在途命令/模型请求，不重跑工具；结束复盘始终只读。Anthropic/Bedrock缓存读写的费用与上下文统计已修正。

## 实际检查

- `make check`：481通过，ruff/pyright通过（3条既有MCP采样弃用警告）。
- `make test-integration`：标准构建命令及全部48项容器集成通过。首次镜像回归因模拟黑板未实现新增凭据导入端点失败，补全测试fixture后全绿。
- `pnpm --dir web check`：32项单测，TypeScript/ESLint通过；生产构建及e2e TypeScript通过。
- Playwright 17项全绿，包含无历史/无浏览器默认、三Worker直接保存/409保留草稿、动态Provider认证字段、平台默认模型、创建任务模型/领域资料/域名提交。桌面和手机截图已检查。
- 原生MAF工具循环验证下一调用使用新系统指令、旧system不继续生效、历史与工具结果保留、revision不重复、重启恢复。所有17连接方式有离线客户端/请求转换/资源释放检查；OpenAI兼容协议另有完整MockTransport工具往返。
- 模型、云账号与MCP相关认证均使用测试数据或本地模拟。本轮未调用真实收费模型，未伪称验证了用户未提供的云资源或密钥。

## 偏差与边界

- Anthropic/Bedrock原生input_token_count不含缓存读写，已分别计入；价格表当前没有缓存写入专价，写入按普通未命中输入单价估算，非精确供应商账单。固定人民币单价不自动换汇或切换峰谷。
- Provider覆盖范围以[MAF Python官方清单](https://learn.microsoft.com/en-us/agent-framework/integrations/by-component/model-providers/)为准。Foundry Local原生部署涉及系统组件/下载模型，本平台采用现成HTTP服务，Linux runtime不自动部署本地模型。
- 依赖锁由uv生成，保留core1.19.0/openai1.14.4，新增对应原生Provider包；shared ModelConfig增加provider_options，schema0006仅新增app_settings。旧Task/Profile/Session兼容。
- 已隐藏配置历史但保留底层快照；系统提示词热更新有意取代旧“全部配置固定”语义。模型和工具修改仅新任务生效。

## 提交划分

设计合同先提交；平台设置/凭据/任务选模型；原生Provider与提示词热更新；简化配置前端；部署报告和交接记录。无用户手动构建要求，完成本地部署后直接刷新页面使用。

## 本地部署确认

已部署blackboard/runtime，迁移0006，两个容器运行且重启计数0，runtime持有唯一数据库锁。服务实际返回17种Provider，worker角色为explore/derive/close；DeepSeek默认连接已从environment迁移为stored，has_secret=true，仅公开已配置字段名api_key。runtime在给凭据解析函数传空环境key的情况下仍能从平台读取并构造客户端，随后正常关闭，没有模型调用。

三个原finished任务保留，币种仍USD。当前worker内部revision为4（界面不展示），任务页/配置页HTTP200。备份`.data/backups/pre-worker-settings.dump`；最终日志与截图在`.data/checkpoints/worker-settings/`。无需手动部署或重新填写已有DeepSeek密钥，刷新浏览器即可使用。
