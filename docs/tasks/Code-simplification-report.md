# Code-simplification · 全仓库代码审查与精简报告

日期：2026-09-30。依据用户明确要求及code-simplification技能。主会话负责前端、工程和最终审核；三个子代理均使用GPT-6.1 Sol high，分别覆盖blackboard、runtime、packages/envd/proxy/eval。原测试全部保留，没有修改测试来适配重构。

## 完成内容

完成全仓库结构清点和按模块的调用链、历史及测试审查，在11个生产文件中实施12处行为保持的精简。生产代码合计93行增加、93行删除；收益是减少重复实现和条件嵌套，不以行数或性能提升作为结论。

| # | 文件/区域 | 精简及保持的行为 |
|---|---|---|
| 1 | blackboard/domain/rules.py | calculate_cost复用已有contracts.billing.token_cost，保留公开签名、Decimal算式顺序及“价格未配置”反馈 |
| 2 | blackboard/domain/rules.py | derive_parallel多层三元表达式改为明确分支，review、expected和活动Explore判断保持 |
| 3 | blackboard/api.py | 两个异常处理器复用模块已有JSONResponse导入，状态码和返回内容保持 |
| 4 | runtime/tools.py | 删除只返回exc.message的_remote_error包装，11处直接读取同一属性，异常边界保持 |
| 5 | runtime/clients/blackboard.py | heartbeat/conclude/grace/finish/trace的URL复用已有_agent方法，转义和请求内容保持 |
| 6 | runtime/models.py | 客户端类型选择改为if/elif，provider映射、kwargs、流式解析及重试参数保持 |
| 7 | runtime/chatworker.py | 两处复盘用量读取收敛为_review_usage；保留读取顺序、已记账对象引用、缺失用量和调用时机 |
| 8 | eval/runner/interfaces.py | APIRouter扫描用提前continue减少嵌套，AST遍历、literal_eval及异常保持 |
| 9 | eval/runner/score.py | dict/list行筛选共用一次推导式，返回新列表、原行引用及顺序保持 |
| 10 | web/board/agents.ts | 两处时长文本共用durationLabel，调用者原有日期检查、舍入和特殊数值行为保持 |
| 11 | web/board/conversation.ts | 历史消息状态多层三元改明确分支，浅拷贝、状态优先级及错误默认值保持 |
| 12 | web/components/PlatformCatalog.tsx | 模型/MCP保存改明确分支，重复payload清理抽取为私有纯函数；凭据过滤、API调用及保存反馈顺序保持 |

没有更改API/数据库合同、调度策略、提示词、价格规则、权限、归档范围、依赖、锁文件、部署配置或docs/design/。没有新增框架或通用抽象；公开入口和既有调用者不需要迁移。

## 审查覆盖与保留原因

结构清单记录291个已跟踪代码、测试、构建及生成/历史文件，共51,252行（审查起点计数）；按.py/.ts/.tsx/.css/.sh/.mjs、Makefile、Dockerfile统计，另行审查JS、Compose、TOML、模板及配置。这个数字不代表逐行重写，也不包含第三方依赖。清单在本地`.data/checkpoints/code-simplification/inventory.json`。

| 分区 | 审查范围 | 结果 |
|---|---|---|
| blackboard | 21个生产Python模块；API/鉴权、领域规则/快照、事件投影/重放、账本、会话、平台及工作区；迁移env和0001–0011 | 精简2个文件；历史迁移和事务顺序保留 |
| runtime | 38个Python模块；模型/流式、工具/中间件、runner/session/chatworker、scheduler、execenv、测试替身、入口及镜像配置 | 精简4个文件；取消、记账、派发和恢复边界保留 |
| packages | contracts的9个源模块和4个测试文件；objects的2个源模块和2个测试文件；生成schema用途 | 已有公共实现足够直接，无需改动 |
| envd/proxy | envd的4个源模块和5个测试文件、Dockerfile；代理入口和Dockerfile | 无收益足够明确的精简；保留进程/用户/文件安全处理 |
| eval | runner的7个模块和6个测试文件、Dockerfile；25个目标/fixture/构建文件、8个目标测试和9个答案校验脚本 | 精简2个runner文件；评测目标中的故意缺陷保留，不执行真实评估 |
| web | 46个非生成src文件（含CSS）；API/SSE、状态/历史/图谱、全部页面和面板；原单元/浏览器测试及构建检查配置 | 精简3个文件；JSX/DOM/CSS和产品交互保持 |
| 工程/配置 | Makefile、两份Compose、服务Dockerfile、workspace及包配置、Profile/template和生成/锁文件用途 | 沿用既有入口及固定依赖，不机械去重 |

针对大函数审查了create_app、domain.decide、repository.apply、run_agent、make_board_tools、chatworker.process、scheduler.decide、archive.build_archive等。没有按长度强拆：API闭包共用鉴权/服务，领域规则和投影对应原子事务及事件重放；拆分并不自动降低复杂性。

以下复杂性承担具体职责，保留：

- Session CAS、claim token、取消后shield提交与不确定提交对账；derive轮次隔离及历史分段。
- Responses工具回放顺序、流式EOF/完整终态检查、逐请求账本和SDK重试。
- Close重复状态查询、版本校验、阶段门控；把它们合并可能改变并发可见状态。
- 文件流close/release的finally、进程组取消、恢复路径/链接/容量校验、失败保留工作区及明确登记产物归档。
- 评分完整检查、人工复核/未知指标处理；把all([...])换成短路生成器会跳过后续检查。
- 前端事件顺序、历史浅拷贝、ELK布局缓存和API薄包装；没有引入跨模块通用化重构。

## 实际检查

审查前基线：make check为733项通过、73项排除；前端33项通过。每个独立精简后先跑相关原测试，然后完成全量检查。

| 检查 | 实际结果 | 本机日志 |
|---|---|---|
| make check | Ruff格式/lint、Pyright通过；733 passed、73 deselected，16.16秒 | /private/tmp/bbx-simplification-check.log |
| make test-integration | 69 passed、737 deselected，180.72秒；执行环境/代理/runtime/eval镜像构建成功 | /private/tmp/bbx-simplification-integration.log |
| pnpm --dir web check | ESLint、TypeScript及33项原Vitest通过 | /private/tmp/bbx-simplification-web-check.log |
| pnpm --dir web build | 构建成功，12.23秒 | /private/tmp/bbx-simplification-web-build.log |
| 浏览器测试TypeScript | 通过 | /private/tmp/bbx-simplification-web-e2e-types.log |
| 完整Playwright | 27项原测试通过，29.5秒 | /private/tmp/bbx-simplification-web-e2e.log |
| git diff --check | 通过；原测试/schema/config/prompt均未变 | Git差异检查 |

独立精简后的原测试：领域97项；API鉴权/OpenAPI 10项；领域/账本/contracts计费110项；工具10项；客户端/Session/trace/取消/中间件/派发63项；模型/流式/推理134项；复盘/Session 20项；接口解析1项；评分10项。前端每处改动后都跑原33项，时长相关浏览器4项、配置相关浏览器12项也通过。不同组合有重叠，不相加作为新增覆盖。

额外只读交叉复核比较实际新旧函数：时长1,656组、历史消息50组、配置保存payload/调用序列120组、Decimal计费122组，未发现差异。包括NaN/无效日期/小数、消息对象引用、凭据真假值、保存失败路径及低精度Decimal flags/traps。此对照是补充证据，不能替代原测试或证明所有输入等价。

补充对照脚本保存在本地`.data/checkpoints/code-simplification/compare-frontend.sh`和`compare-decimal.sh`，旧源码固定为fcab4e1，保存后再次执行并通过；`cross-review-results.md`区分原始执行、保存时提交及固定基准复现。审查清单、脚本和全量检查日志同步至主仓库同名目录，均使用合成样例。

普通和集成检查使用假模型；没有运行live/e2e/eval的真实模型命令，没有读写.env。浏览器检查使用拦截API的样例，没有更改真实平台配置或任务。

复现命令（项目已安装依赖时）：

```sh
UV_CACHE_DIR=/private/tmp/bbx-uv-cache uv sync
UV_CACHE_DIR=/private/tmp/bbx-uv-cache make check
UV_CACHE_DIR=/private/tmp/bbx-uv-cache make test-integration
pnpm --dir web check
pnpm --dir web build
pnpm --dir web exec tsc --project e2e/tsconfig.json --noEmit
PLAYWRIGHT_BROWSERS_PATH=/Users/yym/blackboard-explorer/.data/playwright pnpm --dir web e2e --workers=2
git diff --check
```

本机集成镜像构建实际附带：

```sh
https_proxy=http://127.0.0.1:7897 \
http_proxy=http://127.0.0.1:7897 \
all_proxy=socks5://127.0.0.1:7897 \
no_proxy=localhost,127.0.0.1,::1,host.docker.internal \
UV_CACHE_DIR=/private/tmp/bbx-uv-cache \
make test-integration DOCKER_BUILD_ARGS='--add-host host.docker.internal:host-gateway --build-arg http_proxy=http://host.docker.internal:7897 --build-arg https_proxy=http://host.docker.internal:7897 --build-arg no_proxy=localhost,127.0.0.1,::1,host.docker.internal'
```

## 偏差与待决

- 没有确认出需要另开功能修复的缺陷；没有把审查结论当成无缺陷保证。KeyError异常处理器缺少直接独立测试，本轮只移除冗余导入，不虚报该分支的单测覆盖。
- services/agent-runtime/README.md保留早期“derive/close仅心跳”及固定USD价格等描述，与当前实现和主部署文档不一致。作为后续文档同步项记录，没有借纯重构改动行为或模板。
- 原有三条MCP sampling弃用警告及前端>500kB chunk提示仍在；没有通过屏蔽警告或调整阈值掩盖。
- Env恢复owner分支的精简收益小且原分支测试不足，未实施；没有为了“精简所有代码”强行重写状态机/取消/归档逻辑。
- 本轮不能证明解题更快、缓存率提升或费用下降；这些需要独立性能任务和可比实测。

## 提交划分与交付

已按可审阅的小批次提交：

| 提交 | 内容 |
|---|---|
| e9538f1 | Define repository code simplification scope and behavior constraints：任务说明 |
| f2e67fc | Reuse shared token costing and clarify blackboard branches：blackboard两个文件 |
| 5b28986 | Remove runtime wrappers and consolidate review usage lookup：runtime四个文件 |
| 3801444 | Flatten route parsing and share evaluation row filtering：eval两个文件 |
| cbe269e | Clarify catalog saves and consolidate duration formatting：前端三个文件 |
| 本报告提交 | Record repository simplification audit and unchanged test results |

所有提交为Code-simplification范围；没有测试变更、迁移或新依赖。按照用户此前持续授权，检查通过后快进合并main，不推送远程。当前运行服务不因本次纯重构重启，避免打断用户任务；本轮交付为源代码及审查报告，部署在正常更新服务时加载。

无需用户补跑沙箱外命令。下一步建议先同步runtime局部README中的历史说明；性能优化应独立测量，不与行为保持的精简混合。
