# Worker-settings · 直接配置与动态提示词

用户要求：不显示历史版本/配置名称/浏览器默认；worker仅explore、derive、close分别配置。平台保存所有凭据，自动生成内部标识，扩展MAF Python实际provider；创建任务选择模型，支持平台默认模型。用户确认系统提示词保存后对运行Agent的下一次模型调用生效。

## 固定接口合同

- GET `/api/settings/workers` → `{revision:int,profile:AgentProfile}`，底层default最新快照但UI不显示版本/名称。
- PUT `/api/settings/workers/{role}`，body `{expected_revision:int,prompt:string,tools:WorkerTools}` → 同GET。保存本角色全文和工具，Jinja语法及变量验证；并发版本不符409。界面“保存并应用”，提示词下次模型调用生效，工具仅新任务生效。
- PUT `/api/settings/runtime`，body `{expected_revision,params,exec_image,exec_resources,privileged_allowlist}` → 同GET；参数/执行环境仅新任务生效。
- GET `/api/settings/prompts/{role}?since=N` → `{revision:int,prompt:string|null}`，null表示未变化，runtime每次模型调用前用当前任务上下文重新渲染更换系统指令，Session记住revision/正文，原trace保留。其他role变更造成revision增大但正文不变时不重复注入。复盘保持只读指令。
- GET `/api/platform/providers` → MAF Python实际Provider目录，元素 `{id,label,options_fields:[{name,label,required}],credential_fields:[{name,label,required}],allow_no_auth,default_base_url,base_url_required}`，字段label可缺省用name。根目录常见provider field names: api_key; AWS access_key_id/secret_access_key/session_token; Google service_account_json; Azure tenant_id/client_id/client_secret。ModelConfig新增provider_options:dict[str,str]，均为非密选项。
- GET `/api/platform/models` 原元素新增 `is_default:boolean`、`configured_credentials:string[]`（仅字段名），config包括provider_options。前端不显示内部name/version/凭据来源。
- POST `/api/platform/models` 自动生成内部name，body `{label,provider,model,base_url,reasoning_effort,price,provider_options?:{},credentials?:{field:string},api_key?:string,enabled}`；POST `/api/platform/models/{name}` 编辑沿用ID；密钥留空保留。统一平台加密保存，免认证服务可不填。旧credential_source字段仅历史兼容，不提供环境选项。
- POST `/api/platform/models/{name}/default` → `{default_model_id:name}`；默认设置存服务端，跨浏览器。编辑同名模型后默认跟随最新启用内容；已创建任务固定旧快照。
- POST `/api/platform/mcp-servers` 同现有body但自动生成ID；编辑使用现有带name端点，UI隐藏标识与版本字样。
- POST `/api/tasks` 在兼容旧body基础新增 `model_id?:string,model_version?:int`。新UI仅目标/验收/预算/模型，内部agent_profile='default'；总是传用户所见模型id/version。服务端从当前worker设置和该模型生成不可变任务快照，三角色共享所选任务模型。不选model的普通default任务使用平台默认模型。旧显式profile_version API保留旧语义，single仅评估用。
- service专用POST `/api/platform/import-environment-key` body `{api_key:string}`，把runtime既有DeepSeek环境key一次性加密导入遗留environment记录/seed未配置记录，不覆盖用户已存密钥，不打印不回显。GET credentials同时返回 `{secret,credentials:{field:string},credential_source}`。旧DeepSeek无platform refs亦读deepseek-default v1平台凭据。

## 设计和范围

底层快照/事件审计保留，用户不用操作版本。新增schema0006 app_settings保存平台默认模型。更新prompt不切换模型、工具或预算；已结束会话不自动启动探索。默认运行镜像基于Linux，Foundry Local只连接现成HTTP服务不下载模型；不伪称.NET-only ONNX/Dapr为Python可用。未提供第三方密钥，不做真实收费调用。
