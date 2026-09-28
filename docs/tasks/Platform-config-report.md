# Platform-config · 平台配置中心报告

日期：2026-09-28。接续人民币/worker编辑任务，按用户追加要求移除版本对比，支持平台多模型与外部MCP。

## 完成内容

- Agent配置页分为Worker配置、平台模型、MCP工具。按worker表单选择模型、编辑完整提示词、启停内置工具、挂载多个MCP与工具白名单；调度参数、执行镜像/资源亦有表单，高级YAML为可选入口。移除版本对比按钮/页面，保留历史版本选择及不可变快照。
- 平台模型支持DeepSeek、OpenAI Chat Completions、OpenAI Responses、OpenAI兼容Chat Completions四种连接方式，实际使用MAF已安装连接器。填写URL/model ID/推理强度/CNY固定单价，API Key只写。新连接默认不传推理参数，价格须显式填写。已有DeepSeek复用环境密钥，使用其他模型不再强制填写DeepSeek环境key。
- Streamable HTTP MCP注册表、可选Bearer或自定义Header认证、分页工具发现；发现仅初始化和列工具，不执行业务工具或调用模型。Explore支持多服务及白名单，名称加前缀；外部MCP与执行工具受相同日志/交接门控。关闭外部MCP提示词与采样，derive/Close/结束复盘保留原权限。
- 连接配置不可变版本化，Profile保存时规范化平台模型快照，任务继续固定Profile。更新平台条目需重新选入worker并发布才供新任务使用；已有任务不会切换。MCP同名升级替换旧绑定，历史绑定有明确标记。
- 凭据Fernet加密入库，以现有服务端签名密钥作独立用途派生；不修改.env，公开接口及Profile不含明文。凭据端点限service身份，禁止user/agent，no-store；验证错误移除原始input，连接错误只返回类型。保存时空密钥沿用，MCP清除须显式勾选。
- 新迁移0005创建platform_configs、扩充agent_profiles.worker_tools；旧记录默认工具集不变。人民币CNY功能一并交付，历史USD不改写。

## 验证

- `make check`：435项通过，ruff/pyright通过。包括四种客户端、人民币计账、角色工具限制、密钥权限与错误脱敏、真实本地MCP鉴权/白名单/前缀。
- 使用已构建镜像运行`make -o image-exec-env -o image-egress-proxy -o image-agent-runtime -o image-eval-env test-integration`：47项通过。平台数据库测试验证加密、旧版本保留、密钥沿用/清除、Profile模型快照不可被冗余字段篡改。
- 前端37项单测/TypeScript/ESLint、生产构建通过；18项Playwright最终完整回归通过。桌面与手机截图已检查。
- 本次未调用真实收费模型。多provider使用脚本化HTTP响应验证，外部MCP使用本地测试服务；第三方真实模型需其正确地址、模型ID及密钥，不伪称已验证任意厂商服务。

## 偏差与待决

- 软件源出现证书过期，未关闭校验；通过已验证BuildKit依赖缓存离线构建runtime/blackboard。临时Dockerfile仅增加`uv sync --offline`，原源码Dockerfile不改。复用未变更的exec-env、egress-proxy与eval-env镜像；普通/容器测试无真实模型。
- 显式声明blackboard已在锁文件中的cryptography/mcp/httpx依赖，uv重新生成锁文件，未升级第三方版本。保存密钥需要备份现有AGENT_TOKEN_SECRET；直接换该密钥会导致旧凭据不可解密。
- MAF对关闭MCP采样的参数有弃用警告，当前行为及测试正常。前端保留原图谱chunk较大的构建警告。
- 目前MCP为Streamable HTTP，非stdio/SSE旧传输；外部MCP只给Explore。平台模型是连接器选择，不代表自动安装所有MAF提供商SDK。
- 固定高峰价/自填人民币单价为预算估算，off_peak仅价格表标记，不自动做峰谷折扣或汇率换算。

## 提交与交付

设计合同已先提交。实现分为平台连接存储/API及迁移、运行时provider/MCP、配置中心前端与文档。原3个已结束任务保留；本地部署后新任务使用default/single最新人民币配置。检查日志、截图和离线构建说明保存在主仓库`.data/checkpoints/platform-config/`。
