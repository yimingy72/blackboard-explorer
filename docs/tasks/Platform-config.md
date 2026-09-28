# Platform-config · 平台模型与工具配置

接续 CNY-worker-config，用户要求移除版本对比、平台级可复用多模型、worker系统提示词/工具/模型表单编辑，并明确支持外部MCP。

## 接口约定

- GET `/api/platform/models`：最新平台模型列表，元素 `{name,version,label,config:ModelConfig,credential_source:'environment'|'stored'|'none',has_secret:boolean,enabled:boolean}`。ModelConfig新增可选 `platform_id`、`platform_version`；旧配置省略兼容。来源environment仅支持既有DeepSeek密钥，stored密钥由界面输入，none用于无需认证的兼容端点。
- POST `/api/platform/models/{name}`：`{label,provider:'deepseek'|'openai_chat'|'openai_responses'|'openai_compatible',model,base_url,reasoning_effort,price:Price,credential_source,api_key?:string,enabled:boolean}`。人民币完整价格表；密钥留空表示沿用同名上一版，返回脱敏元素；保存新增不可变版本。
- GET `/api/platform/mcp-servers`：元素 `{name,version,label,url,auth_header,auth_scheme,has_secret,enabled}`。
- POST `/api/platform/mcp-servers/{name}`：`{label,url,auth_header:'Authorization',auth_scheme:'Bearer',secret?:string,clear_secret?:boolean,enabled:boolean}`。Streamable HTTP，密钥留空保留，clear_secret显式清除。
- GET `/api/platform/mcp-servers/{name}/versions/{version}/tools`：连接初始化/list_tools，返回 `{tools:[{name,description}]}`，不执行工具、不调用模型；失败仅返回脱敏错误类型。
- 服务端私有 GET `/api/platform/{models|mcp-servers}/{name}/versions/{version}/credentials`：仅service身份，返回`{secret:string}`，user/agent禁止。
- AgentProfile新增 `worker_tools`，对象explore/derive/close，每项 `{builtin:string[],mcp_servers:[{name,version,allowed_tools:string[]|null}]}`；可省略，省略使用原角色工具集。Explore内置可选`post_fact,post_intent,claim,release,get,search,read_evidence,execute_command`；至少post_fact/release。derive可选post_intent/get/search/read_evidence，至少post_intent；Close可选submit_close/get/search/read_evidence，至少submit_close/get/read_evidence。外部MCP仅Explore可挂载，conclude时均禁用；结束后复盘始终仅内置只读工具。
- ModelConfig引用platform_id/version时，Profile保存端从平台版本规范化完整模型快照，不信任提交的冗余价格/provider字段；未来平台编辑不改变已发布Profile或已创建Task。

## 界面

Agent配置页顶部“Worker配置 / 平台模型 / MCP工具”，去掉对比按钮和差异视图。worker可选平台模型、修改完整提示词、切换内置工具、选择MCP及工具白名单，参数和执行环境通过表单编辑；高级YAML仅作为可选入口。密钥password输入，不回显已有值。保存提示“已有任务固定原配置，新任务选择更新后的配置”。

平台密钥Fernet加密入库，使用既有服务端签名密钥派生独立用途加密key，不修改.env，不下发浏览器，不进入Profile/事件。依赖仅显式声明已有cryptography和mcp；任务说明列出的密钥字段不得记录在日志或错误详情中。数据库迁移0005。
