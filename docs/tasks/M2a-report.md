# M2a · 技术验证与 agent-runtime 骨架报告

日期：2026-09-24。

## 完成内容

- R.1 / R.7：安装固定版本 `agent-framework-core==1.19.0`、`agent-framework-openai==1.14.4`，验证清单 11 项均有对应测试；原生行为的两项不符合与采用的退路见下表。
- R.2：Settings 覆盖服务地址、服务 token、envd 派生 secret、MinIO、Docker 网络、宿主 relay、DeepSeek 与任务并发配置；不自动加载 `.env`，不持有 JWT 签名密钥。
- R.3：BlackboardClient 支持任务、状态、快照、事件、固定版本 profile、事实/意图/裁定、Agent 登记/心跳/conclude/grace/finish、工具日志。每个 Agent 使用服务端颁发的 JWT；`with_token` 共用连接池。EnvdClient 支持健康、用户、文件与归档；对象存储复用 bbx_objects。
- R.4：ExecEnvManager 按 profile 创建隔离容器，验证网络 internal 属性、使用完整 UUID 归属标签、防止短名称误认、失败清理、恢复查找、建用户、临时文件流式归档到 MinIO、幂等销毁。宿主 relay 固定目的地址且只发布 localhost 随机端口；执行容器没有外部网络或宿主端口。
- R.5：make_client 配置步数/时间限制、reasoning_effort；最小 DeepSeek 用量适配保留缓存字段。profile 支持本地加载与服务端固定版本（含模板正文）快照。
- R.6：ScriptedChatClient 使用真实 MAF 函数调用与中间件层，支持条件跳转、用量、消息记录；FakeEnvd 提供真实 MCP/ASGI 接口、临时文件与预设命令，不执行 shell。

模块与调用接口详见 `services/agent-runtime/README.md`。

## 技术验证结论

| # | 结果 | 对 M2b 的影响与退路 |
|---|---|---|
| 1 | 符合：真实 DeepSeek 连续 5 次工具调用，至少 6 次模型请求 | 使用 Chat Completions 客户端，无需切换 provider |
| 2 | 原生不符合：MAF 丢弃 DeepSeek 顶层缓存计数，raw_representation 也为空；适配后离线与真实调用均通过 | DeepSeekChatClient 仅覆盖用量解析，保留标准字段和 hit/miss，不依赖不存在的原始响应 |
| 3 | 符合：列表前项为外层，先进入、后退出 | ToolLog 放函数中间件最外层 |
| 4 | 原生不符合：enqueue 增量在工具循环首轮可见，下一轮消失 | 增量追加到公开 Content.result；首轮追加到已有 user Content.text。三轮验证两条增量各出现一次并持续保留，不操作私有历史 |
| 5 | 符合但有时序影响：最终回执后入队会再调用一次模型，队列空后结束 | 运行期取消 MessageInjectionMiddleware/入队机制，使用第 4 项退路，避免额外循环 |
| 6 | 符合：替换结果且不 call_next，对本地与 MCP 工具都有效，模型继续 | GraceGate 拒绝时设置结果，不 terminate |
| 7 | 符合：max_iterations=1 后再发最终请求，tool_choice=none | 客户端配置步数余量；不能把该兜底当领域层精确预算 |
| 8 | 符合：static_headers 鉴权成功，FakeEnvd 两命令同时在途 | explore 使用 MCP 工具；不需要 header_provider |
| 9 | 符合：5 个并行工具共用原子额度，3 成功、2 拒绝 | 生产使用黑板 grace 原子接口；正常返回 remaining=0 是最后一次已获准调用，只有扣减失败才拒绝 |
| 10 | 部分真实验证、确定性故障验证通过：真实 5 路并发成功；注入 429 两次后第三次成功，连续超时三次后抛出 ChatClientException | 使用 SDK 默认 2 次重试；没有人为压垮服务制造真实 429。runner 仍要记录失败并 finish |
| 11 | 有界探针符合：中文长上下文、一次工具调用与合法 JSON 回执，真实连续 3 次通过 | 完整 order-service 业务探针依赖 M2b 工具、提示词与 CLI，留在 M2b 检查点；本次不能视为完整玩具任务成功 |

补充：MockTransport 确认 reasoning_effort 传到 HTTP 请求体，MAF 的 max_tokens 选项转换为 max_completion_tokens。运行使用显式 Agent session。

## 实际验证

- `uv sync` 完成，锁文件包含兼容版本；无本地模型下载。
- `make check`：Ruff/格式/Pyright 通过，**173 passed、16 deselected**（3.65 秒）；普通测试不调用真实模型。
- `make test-integration`：**12 passed**（19.26 秒），包含真实 envd 生命周期、MCP 命令、MinIO 归档和无残留断言；镜像构建使用 envd README 的 DOCKER_BUILD_ARGS。
- 显式运行 `.venv/bin/python -m pytest -m live services/agent-runtime/tests/verification/test_deepseek_live.py -q --tb=short`：**4 passed**（11.03 秒）。按用户说明，由测试启动进程加载主工作树 `.env` 中 DeepSeek 所需配置；未显示或修改文件/密钥，输出按密钥值脱敏。业务进程和普通检查仍不自动加载 `.env`。
- 额外只读认证探针 `/models` 返回 200，确认配置的 deepseek-flash 可用。

复现：在有代理的终端设置 AGENTS.md 第 6 节代理及 UV_CACHE_DIR 后执行 `make check`；按 envd README 设置 DOCKER_BUILD_ARGS 后执行 `make test-integration`。若要再次付费验证，在进程环境注入 DEEPSEEK_API_KEY 后执行 `make test-live`；本次已经执行，无需用户补跑。

## 共享文件修改

- 根 `pyproject.toml`：新增 pytest `live` 标记。
- 根 `Makefile`：普通/集成目标排除 live，新增检查密钥环境变量的 `test-live`。
- `.env.example`：runtime 的 blackboard 地址、执行网络、访问模式、代理地址占位；真实 `.env` 未修改。
- `uv.lock`：固定 MAF 及所需传递依赖。`socksio` 解决当前 ALL_PROXY 环境下 SDK 初始化；不引入全部 provider extra。
- 三份设计文档与 M2a/M2b 任务：先独立提交网络访问、token 归属及实测框架退路，未改 contracts 或黑板 API。

## 偏差与待决

1. 技术清单第 11 项采用中文长上下文工具探针，不冒充完整 order-service 运行；完整业务检查属于 M2b。
2. 第 10 项真实并发未触发 429；重试边界由无网络注入验证，避免无意义的大量收费请求。
3. 测试替身不仅继承 BaseChatClient，还加官方 FunctionInvocationLayer 与 ChatMiddlewareLayer，否则不会运行真实工具循环。
4. DeepSeekChatClient 依赖固定版本的用量解析扩展点；升级框架需先运行验证测试，不能静默升级。
5. profile 价格表仍为空，M2b 真实金额检查需按官方价格填写；M2a 仅核对 token 字段，未声称核对控制台金额。

## 提交划分与下一步

已单独提交设计：`1d5d853 Specify isolated host access for runtime environments`、`1256820 Keep JWT signing secrets in blackboard`、`701e214 Adapt runtime synchronization to verified MAF behavior`。

实现与验证、依赖、README 和本报告建议一起提交：`Build runtime clients and verify MAF integration`。合并后更新 HANDOFF，再开始 M2b；M2b 合并才满足 m2 标签条件。当前无需用户执行命令。
