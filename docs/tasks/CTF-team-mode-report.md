# CTF-team-mode · T0–T5 实施报告

> 发布说明（2026-10-10）：用户已授权将正式前端及必需CTF依赖提交、合并main并推送。原“不提交/不推送”和旧镜像记录属于历史阶段。原型、截图与本机诊断脚本仅保留本地，不随功能源码发布；正式入口与Mock测试见使用文档，发布结果见末尾。

> 日期：2026-10-08。工作树 `/Users/yym/bbx-wt/ctf-team-mode`，分支 `ctf-team-mode`，HEAD仍为 `affc6cd48fe31bb8b29737e75a7e62b3189b7f3b`。本轮未提交、推送、发布或合并。

> 第1–4节为T1/T2历史记录，第5–6节为已验收T3/T4；第7节为本轮T5最新交付与检查结论。

## 1. 完成了什么

### T0：方案修订与独立评审

首轮“修订后可开发”的八项意见均已纳入设计并经独立复核关闭：①DB mailbox＋confirmed checkpoint恢复；②派工turn唯一结果通知；③envd固定模式/boot登记与未知进程停止证明；④持久conclusion与异步closing；⑤完整两角色模板；⑥逐表重放/权威控制边界；⑦可信验证来源；⑧全入口权限矩阵。合理默认为4名队友不含Lead（建议默认而非用户指定数字）、存续队友占额、owner/CAS、登记产物恢复。用户随后明确授权实施T1/T2。

### T1：模式、契约、存储与入口

| 任务编号 | 完成内容 | 主要文件 |
|---|---|---|
| T1.1 | 独立CTF任务/预算/配置/Profile、成员/turn/mailbox/conclusion及严格事件契约；12份CTF JSON schema | `packages/contracts/src/bbx_contracts/ctf.py`、`schemas.py`、`packages/contracts/schemas/Ctf*.json` |
| T1.2 | 0013追加迁移；tasks.mode/options/control/conclusion与五张CTF表；任务行锁、活跃turn/通知唯一约束；显示投影重放不清Session、租约及运行资格 | `services/blackboard/migrations/versions/0013_ctf_team.py`、`store/schema.py`、`store/repository.py`、`ctf.py` |
| T1.3 | 创建、详情/列表、state、start/stop、Profile快照及独立角色设置分派；generation/turn token；原生Session参与者；公共事件/SSE/私有对象权限；supervisor在旧Profile/Params路径前分派 | `api.py`、`ctf_api.py`、`auth.py`、`profiles.py`、`worker_settings.py`、`conversations.py`、`service.py`、runtime `scheduler/supervisor.py` |

完整新Lead/teammate模板位于`profiles/ctf/prompts/`，不拼接用户任务到系统规则。首批任务消息完整携带目标、上下文、完成要求与附件描述；超长内容分有序消息，不截断。工具实际集合及服务端再次按固定Profile校验。已同步`services/blackboard/openapi.json`及`web/src/api/schema.d.ts`；本轮没有开发T5前端界面。

### T2：首个可运行纵向闭环

| 任务编号 | 完成内容 |
|---|---|
| T2.1 | 新`bbx_runtime/ctf/`薄运行层，复用MAF Agent/Session/流/工具循环、公共checkpoint、模型客户端和费用算法；具名成员保留Session；代次写保护、预算/步数限制、费用outbox；初始上下文、每轮触发引用、模型输出及工具记录复用既有事件/对象/日志表 |
| T2.2 | 数据库mailbox先落盘；空闲唤醒、忙时模型边界注入、direct成员消息；稳定messageId对history/pending去重；失败重读confirmed checkpoint；租约续期、失联响应对账、恢复旧generation拒绝；完整历史与有界模型输入分离 |
| T2.3 | 带派工关联turn原子结算唯一结果；显式回复关联、纯通知无自动回执链；长回答持久保存并以有界通知引用。finish原子登记后返回，coordinator负责drain；closing新用户消息deferred，重启继续收尾；未确认最终checkpoint持久屏障不误报成功 |

测试证明真实API→隔离PostgreSQL→原生MAF脚本模型的完整流程：Lead创建Alice/Bob、派工、两队友模型流通过双向事件屏障证明同时运行、结果通知、用户插话续接同Session、保留历史、观测对象、收尾和模拟drain拒绝/确认。FakeEnvd只在测试明确注入；生产T2装配不连接未经T3停止保护的执行容器或平台MCP。

独立代码复核已关闭：异常coordinator接管、长连接SSE代次失效、超长通知结算、快照工具权限、第三方私聊不能抑制Lead结果、最终checkpoint失败屏障。集成另发现并修复CTF事件仍落旧枚举的问题；全仓测试发现并修复HTTP中间件对附件上传取消语义的回归，原测试未削弱。

## 2. 如何验证及实际结果

最终检查：`make check`全绿，ruff format/lint通过、pyright 0错误，**913 passed / 96 deselected**（3条既有MCP sampling弃用警告）；新增隔离集成**8 passed**，包括7项数据库事务/故障/观测测试和1项API→MAF并发纵向场景。OpenAPI已重生成并由仓库快照测试核验，生成TypeScript声明单独`tsc --noEmit`通过，`git diff --check`通过。独立最终复核未发现剩余T1/T2 P1。所有模型调用来自`ScriptedChatClient`，未调用付费/真实模型或真实靶场。没有读取`.env`、复制凭据或改变用户数据库/已有容器。

本工作树无`.venv`；使用本机已存在的Python 3.12.13虚拟环境，只读调用工具，并用`PYTHONPATH`强制导入当前工作树源码、`PYTHONDONTWRITEBYTECODE=1`防止外部字节码写入。未安装依赖、未改uv.lock。Pyright提示本工作树`.venv`不存在，但实际使用下列PATH中的解释器并完成检查。

本机可复现本轮实际命令：

```sh
cd /Users/yym/bbx-wt/ctf-team-mode
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$PWD/packages/contracts/src:$PWD/packages/objects/src:$PWD/services/blackboard/src:$PWD/services/agent-runtime/src:$PWD/services/envd/src:$PWD"
export PATH="/Users/yym/blackboard-explorer/.venv/bin:$PATH"
make check VENV=/Users/yym/blackboard-explorer/.venv/bin
python -m pytest services/blackboard/tests/test_ctf_integration.py services/agent-runtime/tests/test_ctf_vertical_integration.py -m integration -q
python -m bbx_blackboard.openapi --check
/Users/yym/blackboard-explorer/web/node_modules/.bin/tsc --noEmit --lib ES2022,DOM web/src/api/schema.d.ts
git diff --check
git diff --stat
git status --short
git ls-files --others --exclude-standard
```

新环境按仓库规则准备uv workspace后可直接运行`make check`；上面外部路径仅用于复用本机既有工具，代码不依赖该绝对路径。集成命令需要Docker socket权限，本轮经沙箱外审批执行；使用已存在`pgvector/pgvector:pg16`，testcontainers创建`bbx-ctf-db-*`、`bbx-ctf-vertical-*`临时容器、随机端口并自动清理，迁移实际升级至head。对象存储使用内存替身，不访问用户MinIO。收尾只读查询`docker ps -a --filter name=bbx-ctf-`返回空，确认本轮测试容器已自动清理。

未运行：默认`make test-integration`全套镜像构建、真实envd stop/boot/MCP集成（T3）、web界面构建/E2E（本轮仅生成类型）、T4–T6场景、真实平台/评测/付费模型。不能将本次假适配验收称为远端停止已验证。

## 3. 偏差与待决

1. **阶段边界**：完成T1/T2首个纵向闭环，不是整个CTF交付。成员停止/恢复/移除、真实envd、任务板/增援、平台验证、完整界面、归档/删除/终态复盘/显式续跑仍按T3–T6实施。未实现入口明确拒绝，不回落到旧模式。
2. **执行身份**：T2协作ID使用lead/member-N；T3接现有容器时需映射到agent-N执行用户/目录。当前模板提示执行未连接，不冒称目录已创建。已在提案第15节记录。
3. **最终快照不可恢复**：同进程保留失败快照可重试；若重启丢失最后未确认快照，任务保持closing屏障并显示故障，不凭旧历史宣布成功。完整运维处置需T6验收。失效上传留下的未登记观测对象不可被Agent读取，任务前缀清理属T6。
4. **真实工具**：CTF配置保存平台MCP和执行工具选择，但T2生产运行暂不装配，模板明确不可用；FakeEnvd为测试专用。T3完成停止证明后再开放真实执行，T5再接平台适配。
5. **生成文件**：发现原有AgentRun/Event/Price schema快照已与基线模型存在不同步；未顺手保留这些无关生成变化，仅新增CTF schemas及本次所需OpenAPI/前端类型。
6. **环境故障**：实现中执行通道短暂`exec-server transport disconnected`，停写核实后恢复；最终检查成功不依赖该故障。应用跨线程主动消息通道曾报错，结果由委派完成通知交回。

最终Git范围：12个已跟踪文件修改、37个未跟踪文件（包括本来已有的3份方案文档、新源码/测试/模板及12份CTF schemas）；HEAD未变。普通`git diff --stat`不包含未跟踪文件，审查必须同时看`git status`与未跟踪清单。

共享改动逐项：contracts新增CTF模块及schema导出；schema/repository新增字段、表及CTF投影；API/auth/conversations/service添加mode与代次/权限分派；profiles/worker_settings复用快照存储；supervisor加入CTF接管；OpenAPI/TS类型同步。根pyproject.toml、uv.lock、Makefile、compose、envd源码、AGENTS和docs/design均未修改。

## 4. 下一步与建议提交

请主会话审查T1/T2 diff和检查结果，再推进T3：真实envd固定模式、boot/generation登记、命令幂等/进程组停止与drain、成员stop/resume/remove及执行身份映射。T2测试不能替代T3真实MCP链停止证明。当前任务到此交回，不自动开始后续阶段。

建议提交划分（本轮均未执行）：D0三份设计/计划/报告；D1 contracts/配置模板/迁移与API模式入口、生成契约；D2 runtime/Session/mailbox/收尾/日志/相应测试与本报告。推荐英文信息分别为`Document CTF team mode design and staged delivery plan`、`Add CTF mode contracts and persistent team state`、`Run persistent CTF teammates with directed messaging`。D1/D2紧密接口若拆分需逐提交检查通过；正文末尾均按仓库规则`Implemented by Codex (gpt-6-sol) for task CTF-team-mode.`。当前无新提交SHA。


## 5. T3：真实执行、成员生命周期与停止证明（已验收）

### 5.1 完成内容与代码范围

| 编号 | 完成内容 | 文件范围 |
|---|---|---|
| T3.1 | Lead/用户停止、恢复、移除；具名身份和固定执行用户映射；停止保留配额/Session/消息，移除须先确认停止再释放并取消未交付消息；操作幂等和历史保留 | contracts/CTF schemas、CTF profile、blackboard ctf/ctf_api、0014迁移、schema/repository及对应测试 |
| T3.2 | envd固定模式/任务、每次boot与登记代次；可信MCP metadata；Session预保存命令UUID/参数，跨generation保留去重账本；真实进程组stop/drain | envd core/app/settings、runtime clients/envd、ctf/execution/runner/tools/coordinator |
| T3.2恢复 | unknown不假报drained；持久replacement阶段、归档后精确旧容器销毁确认、新容器恢复、ready撤旧代；保存/销毁/恢复失败可重试且不释放屏障 | runtime execenv/manager、scheduler/supervisor、blackboard ctf_control及故障/组合测试 |
| T3观测 | 命令日志沿用CTF观测和权限；本成员完整命令输出最多8 MiB登记对象引用；其他成员路径拒绝 | runtime ctf/execution、现有record_observation、输出登记测试 |

新增执行路径复用原容器目录、共享文件、对象存储、MAF Agent/Session、官方ClientSession，没有新增依赖或运行服务。原始协作ID始终保留，Linux用户映射不替换Session身份。停止API由服务端校验角色与代次；runtime完成证明接口仅可信service可调用。

### 5.2 实际验证

最终`make check`全绿：**958 passed / 102 deselected**，ruff format/lint通过，pyright **0 errors**，3条既有MCP sampling弃用警告；耗时16.43秒。最终隔离集成**14 passed**（25.17秒）：11项PostgreSQL测试、2项API/MAF/真实数据库组合场景、1项真实envd/MCP容器场景。它们包含T1/T2回归及T3新增验收，不能把所有数量均称为T3新增测试。另针对曾出现的进程回收时序问题单独复跑真实envd场景，1项通过。

OpenAPI快照校验、生成TypeScript声明`tsc --noEmit`、`git diff --check`通过。过程中一次全仓检查发现新envd测试的SecretStr/UUID类型错误，已修复；一次集成出现停止后的回收短窗口，已修复并重复验证。最终无失败检查。独立最终复核未发现剩余P1。

收尾查询`docker ps -a --filter name=bbx-ctf-`为空，并已删除本任务测试镜像`bbx-ctf-envd:t3`；复现时先按下文重建。保留原有基础镜像和用户容器。

可复现命令在第2节环境设置基础上执行：

```sh
make check VENV=/Users/yym/blackboard-explorer/.venv/bin
python -m pytest services/envd/tests/test_ctf_container_integration.py services/blackboard/tests/test_ctf_integration.py services/agent-runtime/tests/test_ctf_vertical_integration.py -m integration -q
python -m bbx_blackboard.openapi --check
/Users/yym/blackboard-explorer/web/node_modules/.bin/tsc --noEmit --lib ES2022,DOM web/src/api/schema.d.ts
git diff --check
```

本轮测试镜像为独立`bbx-ctf-envd:t3`，通过沙箱审批复用本机已存在`bbx-exec-env:latest`并覆盖envd源码构建，无网络下载，不覆盖默认标签。复现构建：

```sh
docker build -t bbx-ctf-envd:t3 -f - . <<'DOCKERFILE'
FROM bbx-exec-env:latest
COPY services/envd/src/bbx_envd /tmp/bbx-ctf-envd-source
RUN target="$(/opt/envd/venv/bin/python -c 'import bbx_envd,pathlib; print(pathlib.Path(bbx_envd.__file__).parent)')" && cp -R /tmp/bbx-ctf-envd-source/. "$target/" && chmod -R a-w "$target" && rm -rf /tmp/bbx-ctf-envd-source
DOCKERFILE
```

真实容器场景通过官方MCP调用验证：缺meta/未登记/旧boot/旧generation拒绝，同ID重复请求仅启动一次，远端stop后`killpg(PGID,0)`证实进程组消失；仅杀死envd并重启时保留旧执行进程，空registry仍unknown；归档工作目录后销毁旧容器并确认NotFound，在新容器恢复私有/共享文件，登记后可执行。额外组合测试用真实API/PostgreSQL绑定旧boot，并运行真实supervisor替换逻辑，防止单层测试漏掉登记身份不匹配。

### 5.3 独立复核、偏差与待决

独立只读复核最终未发现未解决P1。修复并覆盖：跨代次注册丢命令账本、取消保存失败丢重试快照、替换误用重启后boot、保存重试依赖失效容器结算、恢复未完成replacement先找不存在容器、切换后继续使用旧adapter。最终重复实测还发现SIGKILL后PID 1回收子进程的短窗口：envd增加最多2秒的真实进程组消失确认，超时保持drained=false；runtime用DrainPending保留停止/结算，不误判boot故障、不丢最终回答。替换ready与撤销旧generation在同一事务，避免恢复窗口的迟到写入。

边界：进程组用于正常命令终止；脱离进程组的进程不提供额外强隔离承诺，无法证明旧执行状态时使用容器销毁。最后未确认checkpoint若在进程崩溃后丢失仍保持故障屏障，不能制造成功；T6补完整运维和终态流程。命令输出超过8 MiB明确未登记，任意脚本/题目产物用户登记在T4。全体共享目录协作冲突仍遵循登记版本/owner规则，不在T3新增文件服务。

未运行：真实模型、真实CTF平台/靶场、付费评测、T4–T6未实现流程、web界面构建/E2E，以及默认`make test-integration`全套旧模式Docker场景。普通旧模式回归已纳入make check。没有读取凭据或操作用户数据库/服务；临时Docker资源使用bbx-ctf前缀与随机端口。

T3新增共享修改：contracts追加生命周期/执行证明类型及16份CTF schema；0014成员运行控制字段与repository映射；supervisor和ExecEnvManager接入CTF模式与精确销毁；envd可信配置/注册/停止入口；OpenAPI/TypeScript同步。根pyproject.toml、uv.lock、Makefile、compose、AGENTS和docs/design未改。没有提交、推送或合并，HEAD保持基线；最终18个已跟踪文件修改、46个未跟踪文件，未跟踪源码也必须纳入diff审阅。

### 5.4 下一步及建议提交

**T3已完成，可交回主会话验收，无剩余开发阻碍。** 下一阶段为T4：题目owner/CAS任务板、共享尝试/失败条件/难点、增援消息与必要脚本产物登记。当前不自动展开T4。

沿用D0–D2建议，追加D3：本阶段envd/生命周期/执行适配/manager/supervisor及测试、迁移和生成文件，英文信息`Confirm CTF teammate shutdown and deduplicate commands`，正文末尾`Implemented by Codex (gpt-6-sol) for task CTF-team-mode.`。本轮全部保持未提交。


## 6. T4：任务板、共享记录与产物登记（最新）

### 6.1 按任务编号完成内容

| 编号 | 实际交付 | 主要文件 |
|---|---|---|
| T4.1 | 题目创建、本人领取、Lead分配/协作者、状态、释放、重开和逻辑删除；expected_revision冲突返回当前版本，改变owner不自动派工；completed与平台验证分离 | contracts/ctf、blackboard/ctf_board.py与ctf_api.py、CTF投影和API测试 |
| T4.2记录 | 只追加的共享记录，作者、真实事件时间、创建版本不可由模型伪造；owner/Lead/协作者权限；五字段求助＋blocked＋Lead消息原子提交；无脚本须明确说明 | ctf_board.py、CTF模板、runtime/ctf/board_tools.py |
| T4.2产物 | envd只读取本成员或shared的安全快照，解析后的符号链接不能跨成员；runtime计算大小/hash，service读对象复核登记；记录引用登记ID，共享下载拒绝跨任务和未登记对象 | runtime/ctf/artifacts.py、clients/envd.py、envd/app.py、CTF服务和证据授权 |
| T4.2恢复 | 同路径以最新登记版本恢复，历史URI/hash保留且逐一校验；只写路径不声明的文件不进入恢复清单 | execenv/archive.py、test_task_archive.py、T4纵向集成 |

真实MAF脚本模型验证新增记录工具能读取、追加已登记引用并直接发消息；HTTP/PostgreSQL组合验证普通题独立完成，困难题带路线、依据、脚本路径及URI、失败条件、难点求助，Lead调已有协作者，帮手先读记录再联系owner，owner保持不变。所有动作依然是CTF任务板、记录和定向消息，没有转成Fact/Intent调度。

### 6.2 实际检查与复现命令

最终`make check`全绿：**990 passed / 106 deselected**（16.02秒），ruff format/lint通过，pyright **0 errors**，3条既有MCP sampling弃用警告。最终隔离集成**18 passed**（19.98秒）：14项PostgreSQL事务/故障/任务板测试、2项既有API/MAF/替换组合测试、1项T4任务板/共享产物/归档纵向测试、1项真实envd/MCP与文件范围测试。数量包含前阶段回归，不能全部称为T4新增。

OpenAPI快照校验、生成TypeScript声明`tsc --noEmit`及`git diff --check`通过。独立复核确认两个P1已修复，无剩余P1或本阶段开发阻碍。测试中的模型全部为ScriptedChatClient。

收尾查询`docker ps -a --filter name=bbx-ctf-`为空，已删除本轮`bbx-ctf-envd:t4`测试镜像。完整Git状态为21个已跟踪文件修改、61个未跟踪文件，含此前阶段未提交内容；HEAD未变。仅本次T4不新增迁移或依赖。

沿用第2节的现有只读Python工具环境与PYTHONPATH设置；本轮未安装依赖。命令：

```sh
make check VENV=/Users/yym/blackboard-explorer/.venv/bin
BBX_CTF_TEST_IMAGE=bbx-ctf-envd:t4 python -m pytest services/envd/tests/test_ctf_container_integration.py services/blackboard/tests/test_ctf_integration.py services/agent-runtime/tests/test_ctf_vertical_integration.py services/agent-runtime/tests/test_ctf_board_vertical_integration.py -m integration -q
python -m bbx_blackboard.openapi --check
/Users/yym/blackboard-explorer/web/node_modules/.bin/tsc --noEmit --lib ES2022,DOM web/src/api/schema.d.ts
git diff --check
```

独立测试镜像使用第5.2节同样的源码覆盖构建命令，将标签改为`bbx-ctf-envd:t4`；镜像来源仍是本机已有执行基础，无新增网络依赖。环境变量BBX_CTF_TEST_IMAGE只影响隔离测试，不改变平台配置。数据库使用随机端口testcontainers，产物集成使用内存ObjectStore替身，真实envd/MCP容器验证文件范围与停止回归。

### 6.3 独立审阅、偏差与待决

独立只读审阅发现并关闭两个P1：确定URI的并发上传会覆盖已登记历史，现改为每次上传独立UUID；取消登记时若查询先于服务端提交，可能误删迟到提交引用的对象，现发出登记前就标记结果未知，取消也保留对象并对账。均有定向并发/迟提交测试。另修复归档URI形状不一致、无脚本求助字段对齐，以及completed/cancelled不能绕过reopen直接求助。

记录时间来自真实服务端事件；分页工具可继续读取下一页。所有新增请求extra-forbid，Agent不能自填source=platform/user、accepted或直接artifact_refs元数据。T4不实现平台验证适配，题目完成仅工作声明。

失败回收的最小安全边界：明确未登记的本次唯一对象在5秒内尝试删除；网络结果不确定时保留任务前缀孤儿，避免误删成功记录。完整任务删除/purge清理仍属T6，不能宣称所有网络故障均即时回收。单个登记产物上限50 MiB，运行中任意目录浏览、文件冲突自动合并和新知识库均不在本阶段。

共享改动：contracts新增10个T4类型schema（CTF共26份）及角色工具/模板；Repository增加题目/记录事件投影；CTF service/state/授权与生成OpenAPI/TypeScript；runtime envd客户端和archive声明；envd新增可选scope_agent_id安全读取。复用已有CTF JSON表，**本轮无新表、迁移、依赖或服务**。根pyproject.toml、uv.lock、Makefile、compose、AGENTS和docs/design未修改。

未运行：真实CTF靶场、真实/付费模型、真实MinIO登记组合（对象存储用替身）、T5完整前端与平台验证、T6完整终态归档/续跑/purge，以及全套旧Docker集成。没有读取.env或用户凭据，没有操作用户在用的服务。现有黑板普通回归纳入make check。

### 6.4 下一阶段与建议提交

**T4已完成，可交回主会话验收，无剩余阻碍。** 这不是完整CTF模式交付。下一阶段T5实现完整CTF工作台、成员/题目/记录/附件界面及配置驱动的平台工具与可信验证适配，以fake平台验收，不自动接真实靶场。

建议D4提交范围：新增CTF任务板/记录/产物契约与API、runtime工具、envd安全读取、archive声明、对应测试/生成契约和本报告；英文信息`Add CTF challenge ownership and assistance records`，正文末尾`Implemented by Codex (gpt-6-sol) for task CTF-team-mode.`。本轮仍不提交、不推送，HEAD保持原基线。


## 7. T5：平台适配与完整 CTF 工作台

### 7.1 按任务编号交付

| 任务 | 完成内容 | 主要实现 |
|---|---|---|
| T5.1 配置驱动平台工具 | 复用 MCP 注册表、固定版本、角色工具允许清单；用途明确为 management/connect/submit/status，unknown 不开放；Lead 管理连接，队友只能使用获准的提交/查询工具 | contracts/ctf.py、profiles/ctf/profile.yaml、worker_settings.py、runtime/ctf/platform.py |
| T5.1 可信结果与恢复 | 官方 ClientSession＋MAF FunctionTool 单次调用；调用前 Session checkpoint＋DB dispatch 登记；未知写调用不跨代次盲重发；后端回读结果对象并校验 hash、绑定和身份，落目标摘要及验证历史；closing/stopping 允许已派发调用结算，禁止新调用 | blackboard/ctf_platform.py、ctf_api.py、runtime/ctf/platform.py |
| T5.1 验证语义 | Agent 只写 candidate/unknown；人工确认取认证用户身份；平台来源取可信调用和显式适配。fake_ctf_v1 全程 test_only，界面标注模拟平台；required=false 支持纯分析，required=true 未通过阻止 goal_claimed；accepted 不自动结束任务 | CtfVerification 等契约、平台/API/纵向测试 |
| T5.2 创建与配置 | 创建选择模式、自由完成要求、队友默认4且0合法，复用预算/模型/附件；Lead/队友专用提示词与工具配置、用途/适配器设置、revision CAS 冲突保留草稿，仅用于新任务快照 | NewTaskPage、ProfilesPage、CtfSettings、client.ts |
| T5.2 工作台 | 原 /tasks/:id 按 mode 分派；Lead 持续会话、具名成员及历史、定向消息、停止/恢复/移除、题目 owner/协作者、共享尝试/失败条件/难点/求助/登记脚本、人工确认、目标及验证调用证据 | web/src/ctf、TaskWorkbenchPage |
| T5.2 交互边界 | 同步锁防重复点击、稳定消息ID重试、任务/成员键隔离和取消请求、SSE游标去重与轮询回退；closing插话暂存，stopped成员消息排队不自动恢复，removed与终态只读；窄屏可滚至输入和题目板 | useAction、view、Conversation、CSS及Playwright |

### 7.2 验证结果与复现命令

最终 Python `make check`：**1003 passed / 109 deselected**（16.59秒），ruff format/lint通过，pyright **0 errors**；3条既有 MCP sampling 弃用警告。隔离 CTF 集成 **21 passed**（25.39秒）：包括16项PostgreSQL事务/平台边界测试、两项既有API/MAF组合、一项任务板产物纵向、一项新增平台纵向，以及一项真实envd容器测试。数量含前阶段回归。

前端 `pnpm check`：ESLint、TypeScript与 **38项单元测试通过**；`pnpm build`及E2E TypeScript通过。完整真实Chromium浏览器回归 **51 passed**（28.7秒），其中8项CTF新增、43项既有页面。浏览器使用本地Vite和HTTP模拟数据；不是实际模型/靶场运行。新增平台纵向使用真实HTTP/PostgreSQL/原生MAF SessionCheckpoint和ScriptedChatClient，MCP传输与对象存储使用替身，验证创建→派题→求助→拒绝→更正→接受→Lead显式收尾。

最终还补齐原生MAF模拟平台 start→connect→submit→status→close 全生命周期与Lead/队友权限过滤测试，7项平台定向测试全过；末次make check包含该用例。设置页内置清单同步候选登记与验证要求工具后，3项创建/设置浏览器测试、ESLint、TypeScript和生产构建再次通过。

OpenAPI快照、生成TypeScript及 `git diff --check`通过。首次检查期间补齐新dispatch schema并重新生成OpenAPI；首次浏览器回归发现两处旧标题断言，按新增模式后的文案更新，最终全绿。截图检查发现窄屏裁切，改为CTF独立滚动容器并增加输入框/题目详情滚动可达断言，复验通过。

沿用第2节只读Python工具环境；前端依赖从本机既有 `web/node_modules` 复制到当前工作树，不安装依赖或浏览器、不修改锁文件。当前Playwright期待的Chromium不存在，因此增加仅测试用可选 `BBX_E2E_BROWSER` 指定已存在的Chromium 1228；未安装软件。命令在仓库根执行，前端命令在web目录执行：

```sh
make check VENV=/Users/yym/blackboard-explorer/.venv/bin
BBX_CTF_TEST_IMAGE=bbx-ctf-envd:t5 python -m pytest services/envd/tests/test_ctf_container_integration.py services/blackboard/tests/test_ctf_integration.py services/agent-runtime/tests/test_ctf_vertical_integration.py services/agent-runtime/tests/test_ctf_board_vertical_integration.py services/agent-runtime/tests/test_ctf_platform_vertical_integration.py -m integration -q
python -m bbx_blackboard.openapi --check
git diff --check
# web 目录
pnpm check
pnpm build
pnpm exec tsc -p e2e/tsconfig.json
BBX_E2E_BROWSER=/Users/yym/Library/Caches/ms-playwright/chromium_headless_shell-1228/chrome-headless-shell-mac-x64/chrome-headless-shell pnpm e2e --workers=3
```

Docker测试镜像按第5.2节既有源码覆盖命令构建，标签换为 `bbx-ctf-envd:t5`；使用随机端口testcontainers，无固定服务端口。收尾 `docker ps -a --filter name=bbx-ctf-`为空，已删除本轮测试镜像标签。没有操作用户在用的服务。

浏览器截图均为**模拟验收数据**：[桌面工作台](ctf-t5-screenshots/workbench-mock-desktop.png)、[窄屏顶部](ctf-t5-screenshots/workbench-mock-mobile.png)、[窄屏输入与题目板](ctf-t5-screenshots/workbench-mock-mobile-board.png)。截图不代表真实平台已通过验证。

### 7.3 独立审阅、偏差与待决

| 审阅项 | 修复与证据 | 结论 |
|---|---|---|
| P1：closing/stopping拒绝已发出平台调用结果 | 新增可信begin_platform_call先登记调用身份；结算匹配call_id/turn/generation/绑定后允许收尾，仅新调用要求running；原生MAF和PostgreSQL竞态测试保留返回的外部target ID | 已关闭 |
| P1：fake accepted看似真实平台通过 | 所有验证摘要、目标信息和历史调用显示模拟平台，保持test_only；单元断言和浏览器截图可复核 | 已关闭 |
| 跨任务状态与迟响应 | 顶层taskId、成员/题目独立key、query key、AbortSignal、SSE task校验；浏览器覆盖发送中离页和新草稿保留 | 通过 |

最终独立只读复核无剩余P1。为减少两套装配，所有CTF平台工具均采用单次官方MCP调用，包括只读status；没有自动重试层。当前仅内置测试适配器fake_ctf_v1，其他配置工具可调用并保留证据，但无适配器时验证状态保持unknown；实际平台需按其返回契约增加明确适配器，不能由Agent填source变为可信。

平台dispatch控制记录保存在既有CTF控制数据中，Profile平台元数据存既有prompts JSON；本阶段不新增表、迁移、依赖或服务。共享修改：CTF contracts/schema导出、Profile编解码/设置/API/OpenAPI、runtime装配、前端共享client/schema/创建配置列表与路由、Playwright本地浏览器路径选项，以及两处现有浏览器标题断言。根pyproject.toml、uv.lock、Makefile、compose、AGENTS、docs/design和全局配置未修改。

仍未运行真实CTF靶场、真实/付费模型、实际平台凭据与真实MinIO组合；没有读取.env或用户凭据。浏览器连接模拟HTTP而非整套部署，真实后端边界由隔离集成分别验证。构建仍提示共享工作台bundle超过500kB，不影响编译；本轮未扩大为前端性能重构。T6完整终态归档、显式续跑和purge尚待实现，CTF终态UI当前只读，不假装已经支持续跑。

### 7.4 结论、下一步与建议提交

当前工作树共32个已跟踪文件修改、91个未跟踪文件，包含此前各阶段累积内容，不代表全部为T5新增。

**T5已完成，可交回主会话验收，无剩余本阶段阻碍；完整CTF模式尚需T6。** 本轮不自动进入T6，不提交、不推送、不部署。HEAD仍为原基线。

建议D5a提交平台契约、Profile快照/API、runtime单次调用和可信结果、相关普通及集成测试/生成schema，信息 `Add configured CTF platform calls and verification provenance`。建议D5b提交CTF创建/配置/工作台、共享client/路由、浏览器测试及截图和文档，信息 `Add the CTF team workbench and browser acceptance coverage`。两条提交正文末尾均注明 `Implemented by Codex (gpt-6-sol) for task CTF-team-mode.`；依当前授权仅给出划分，不执行提交。

## 8. T6：生命周期闭环与最终验收

### 8.1 按任务编号交付

| 任务 | 完成内容 | 主要证据 |
|---|---|---|
| T6.1 全链路恢复 | 终态归档/清理故障可重试；确认归档后销毁，未确认checkpoint阻止清理；显式resume追加预算幂等、保留Session和成员身份；旧queued消息由Lead确认，removed/stopped不自动恢复 | ctf_lifecycle.py、supervisor、execenv manager、生命周期纵向测试 |
| T6.1 终态复盘 | cleanup_ready后独立review turn与generation；removed历史可问答，固定四项只读工具、单独费用；execution消息不被消费；活动review阻挡resume/delete | ctf_review.py、runtime/ctf/review.py、3项PG复盘集成、浏览器终态复盘 |
| T6.2 归档与删除 | CTF总结、结论历史、Session/mailbox/记录/调用引用/登记产物归档；仅登记文件保证恢复，同路径保留最新登记版本；purge清理精确任务前缀与CTF表，保留另一任务哨兵 | archive builder、真实tar纵向测试、临时PG/内存对象存储夹具 |
| T6.3 访问与竞态 | 真实HTTP state/events/SSE初订阅与补发、Session、消息、工具证据、归档和跨任务/旧代次授权；replay保留权威状态；resume竞争、稳定ID重试、同步删除锁、任务切换与迟响应 | test_ctf_access_integration.py、ctf_integration/review tests、Playwright |
| T6 UI | 结束原因/总结/未解项、历史报告和归档入口、清理阶段与错误、外部目标独立状态、追加额度续跑、只读复盘费用和关联回复去重 | RecoveryPanel、view tests、桌面与窄屏截图 |

### 8.2 最终检查结果与复现

最终`make check`全绿：**1018 passed / 115 deselected**（17.69秒），ruff format/lint通过、pyright **0 errors**，3条既有MCP sampling弃用警告。前端ESLint、TypeScript、**40项单元测试**、生产构建和E2E TypeScript通过。完整已安装Chromium浏览器回归 **56 passed**（31.9秒）：13项CTF、43项既有页面；全部连接本地模拟HTTP数据。

CTF隔离集成分两批：**26 passed**（30.25秒）与完整生命周期纵向 **1 passed**（7.72秒），合计 **27项通过**。使用随机端口PostgreSQL、真实HTTP/原生MAF Session、ScriptedChatClient及任务专用envd镜像。完整纵向覆盖创建→两个队友并行→求助→模拟命令→登记脚本→移除保留历史→Lead收尾→归档/销毁→恢复归档和同Session→Lead确认待办→第二轮归档→删除，并断言另一临时任务及对象未受影响。该纵向命令与容器管理使用替身，真实envd停止/文件边界由独立容器测试验证；对象存储使用替身，不是完整真实平台运行。

首次总检查发现旧API授权测试缺少新任务模式查询mock，修正测试后全绿，未放宽生产授权。新纵向测试的202状态码、工具必填字段与首轮归档路径按真实接口修正；运行中archive-data正确拒绝409，终态service导出成功。最终OpenAPI/TypeScript和契约schema已生成，diff检查通过。

沿用第2节只读Python工具环境，以下命令在仓库根执行；不需要.env、不调用真实模型：

```sh
export PATH="/Users/yym/blackboard-explorer/.venv/bin:$PATH"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$PWD/packages/contracts/src:$PWD/packages/objects/src:$PWD/services/blackboard/src:$PWD/services/agent-runtime/src:$PWD/services/envd/src:$PWD"
make check VENV=/Users/yym/blackboard-explorer/.venv/bin
BBX_CTF_TEST_IMAGE=bbx-ctf-envd:t6 python -m pytest services/envd/tests/test_ctf_container_integration.py services/blackboard/tests/test_ctf_integration.py services/blackboard/tests/test_ctf_review_integration.py services/agent-runtime/tests/test_ctf_vertical_integration.py services/agent-runtime/tests/test_ctf_board_vertical_integration.py services/agent-runtime/tests/test_ctf_platform_vertical_integration.py services/agent-runtime/tests/test_ctf_access_integration.py -m integration -q --tb=short
python -m pytest services/agent-runtime/tests/test_ctf_lifecycle_vertical_integration.py -m integration -q --tb=short
python -m bbx_blackboard.openapi --check
git diff --check
# 以下在web目录执行
pnpm check
pnpm build
pnpm exec tsc -p e2e/tsconfig.json
BBX_E2E_BROWSER=/Users/yym/Library/Caches/ms-playwright/chromium_headless_shell-1228/chrome-headless-shell-mac-x64/chrome-headless-shell pnpm e2e --workers=3
```

Docker测试镜像沿用第5.2节源码覆盖方法，标签为`bbx-ctf-envd:t6`；无新增外部镜像、依赖或固定端口。删除验收全部使用新建临时夹具，未操作用户已有任务、数据库、文件或容器。

### 8.3 独立评审、偏差与待决

| 发现 | 闭环 | 结论 |
|---|---|---|
| P1：归档后replay不识别共享生命周期事件，resume后可能重写旧结论 | 显式投影task.report/task.archived/task.resumed，resume只清当前展示结论与归档入口；cleanup_ready不由重放覆盖；PG测试对归档和resume前后完整state、真实Session断言相等 | 已关闭 |
| review与执行混用风险 | DB purpose和generation双重校验，固定四项只读工具；复盘输入/回复隔离，单独计费，removed不恢复；真实PG冲突和恢复测试通过 | 已闭环 |
| 归档响应未知或清理失败 | 登记成功前不destroy；重试读取持久归档/清理阶段，cleanup complete与ready原子写入 | 已闭环 |
| 删除双击与任务切换 | 前端同步ref锁，浏览器计数断言仅一请求；后端精确task前缀，纵向另任务哨兵保留 | 已闭环 |

独立只读评审确认**无剩余P1阻碍**；没有凭模型自我声明验证其内部型号。使用既有MAF1.19和当前平台基础设施，不增加新的协调服务或依赖。

共享修改：contracts增加confirm_messages契约和实际消息kind，schema/OpenAPI/TypeScript更新；Repository生命周期展示投影、API终态分派和访问控制；runtime supervisor/archive/manager；前端共享TaskView/任务删除入口。T6无新增数据库迁移，0013/0014属于此前CTF阶段；根pyproject.toml、uv.lock、Makefile、compose、AGENTS与docs/design未修改。

边界：只有登记产物保证环境重建；未确认最终checkpoint且内存快照已丢失时仍保持明确故障屏障，不支持自动猜测补写。平台未知写调用不自动重发，Lead续跑需核查外部目标。没有运行真实/付费模型、真实CTF靶场、真实平台凭据或真实MinIO组合；浏览器使用模拟API。生产构建保留既有共享工作台大于500kB警告，未扩大为性能重构。

### 8.4 截图与使用入口

截图均为**模拟验收数据**，已检查桌面和390像素窄屏：

- [收尾与恢复（桌面）](ctf-t6-screenshots/finished-recovery-desktop.png)
- [收尾与恢复（窄屏）](ctf-t6-screenshots/finished-recovery-mobile.png)
- [续跑失败后重试](ctf-t6-screenshots/resume-failure-retry.png)
- [并行协作工作台（桌面）](ctf-t6-screenshots/workbench-mock-desktop.png)
- [工作台（窄屏）](ctf-t6-screenshots/workbench-mock-mobile.png)
- [窄屏输入与题目板](ctf-t6-screenshots/workbench-mock-mobile-board.png)

已使用Library官方上传辅助流程尝试保存桌面/窄屏截图：沙箱内DNS失败，授权联网后服务返回`Library prepare_uploads is not available`。未创建Library文件，因此**没有library_file_id**；未伪造下载链接。保留以上本地路径，不作为功能阻碍。

使用入口：任务列表→新建任务→模式选择CTF；队友默认4（不含Lead），可设置0；填写目标、预算和附件后创建。工作台选择Lead或具名队友聊天，题目板查看负责人、求助记录、登记脚本与独立验证。终态“收尾与恢复”查看结论/历史报告/归档；清理完成后可只读复盘，点击“续跑任务”显式追加预算和指令。复盘不会重开任务，删除须在列表确认，删除会清除全部任务历史。

### 8.5 结论与建议提交

旧黑板兼容回归另执行`services/blackboard/tests/test_store_integration.py`和`test_api_integration.py`，**53 passed**（17.73秒），覆盖存储、归档/resume/replay、会话/停止及HTTP/SSE/workspace，使用已有PostgreSQL和镜像源MinIO的随机端口testcontainers。本轮隔离集成累计**80项通过**（CTF 27＋旧黑板53），不等同于仓库所有Docker测试。命令在上述Python环境中执行：`TESTCONTAINERS_RYUK_DISABLED=true python -m pytest services/blackboard/tests/test_store_integration.py services/blackboard/tests/test_api_integration.py -m integration -q`。旧夹具使用既有`bbx-m1a-test-UUID`、`bbx-m1b-postgres-UUID`、`bbx-m1b-minio-UUID`命名，均为本次新建随机资源，运行后按这些前缀检查无残留。CTF容器前缀检查同样为空，`bbx-ctf-envd:t6`镜像标签已删除。

最终工作树33个已跟踪文件修改、107个未跟踪文件，包含各阶段累积代码、生成契约、测试、截图和文档；未跟踪文件已按完整路径统计。OpenAPI快照与`git diff --check`最终通过。上述真实MinIO只属于旧黑板兼容回归，CTF新纵向对象存储仍使用替身。

**T6实现和CTF模拟验收已完成，可交回主会话进行最终用户验收；无剩余P1功能阻碍。** 未提交、推送、合并或部署，HEAD保持`affc6cd48fe31bb8b29737e75a7e62b3189b7f3b`。下一步为用户检查完整交付并决定提交/部署，不自动接真实靶场或运行付费模型。

建议D6a包含CTF生命周期/复盘后端、runtime归档恢复、共享接口/schema及普通/隔离测试，提交信息`Complete CTF archive recovery and terminal review lifecycle`；D6b包含恢复面板、消息去重、删除锁、浏览器测试/模拟截图及三份文档，信息`Add CTF lifecycle acceptance and recovery UI`。正文末尾均为`Implemented by Codex (gpt-6-sol) for task CTF-team-mode.`。依当前授权仅列划分，不执行提交。

## 9. 本机工作树启动复核（2026-10-08）

### 9.1 原因与范围

复核开始时 `/Users/yym/bbx-wt/ctf-team-mode` 的 `ctf-team-mode` 分支包含未提交的 T1–T6 变更，HEAD仍为`affc6cd48fe31bb8b29737e75a7e62b3189b7f3b`；58000无监听。主工作树已有 `.env`，CTF工作树没有；本次让Compose直接加载主工作树 `.env`，没有读取、打印或写入其内容。已有 `blackboard-explorer-agent-runtime-1` 处于既有重启状态，未触碰。

用户目标是进入模型配置页，因此采用最小基础启动：只启动当前 CTF 工作树的 PostgreSQL、MinIO、MinIO初始化和 blackboard；没有启动 agent-runtime、没有请求真实模型、没有启动靶机。为避免与其他项目的数据库/对象存储混用，使用独立Compose项目`bbx-ctf-team-mode`、独立卷和独立网络，并构建当前工作树专用镜像`bbx-ctf-team-mode-blackboard:latest`。没有覆盖已有`bbx-blackboard:latest`标签。

### 9.2 实际结果

- `bbx-ctf-team-mode-postgres-1`：healthy，绑定`127.0.0.1:55432`。
- `bbx-ctf-team-mode-minio-1`：healthy，绑定`127.0.0.1:59000/59001`。
- `bbx-ctf-team-mode-minio-init-1`：Exited (0)。
- `bbx-ctf-team-mode-blackboard-1`：healthy，绑定`127.0.0.1:58000->8000`。
- `lsof -nP -iTCP:58000 -sTCP:LISTEN`显示Docker代理监听`127.0.0.1:58000`。
- `GET /login`返回`HTTP/1.1 200 OK`、`server: uvicorn`；实际返回的前端JS与当前工作树`web/dist/assets/index-Cvmu_mi9.js` SHA-256相同（`a0a78f02f9291a9f2cd66d1b9ebb52628bb4ac9176894ce0a15d2130981f1144`）。源码入口为`/profiles`，顶部包含`Agent 配置`，其中包含`模型配置`、`CTF 配置`和`MCP 工具`。
- 未登录访问`/api/settings/workers`、`/api/settings/ctf/workers`、`/api/platform/models`均返回`401`；没有绕过认证。当前执行环境没有可用浏览器表面，未代用户登录或输入任何密钥。

### 9.3 用户验证与停止

打开：<http://127.0.0.1:58000/login>。使用你配置的`ADMIN_USERS`账号和密码登录；登录后打开<http://127.0.0.1:58000/profiles>，点击“模型配置”。若页面没有现存条目，再由用户本人输入已提供的BaseURL和模型ID，并在页面安全字段输入自己的API key后保存；本次没有读取、转发或填入key，也不猜账号密码。

本次启动资源可用以下命令停止（不删除卷）：

```sh
docker compose --env-file /Users/yym/blackboard-explorer/.env \
  --project-name bbx-ctf-team-mode \
  -f docker-compose.yml -f /tmp/bbx-ctf-team-mode.override.yml down
```

本次没有执行`down -v`，没有清理用户已有资源；当前仅保留基础配置页服务，agent-runtime仍未启动。

## 10. 二次本机可用性诊断（2026-10-08）

用户反馈58000短暂不可访问后，复核时服务已自行恢复；未执行重启、down、删卷、配置重置或其他恢复写操作。当前`GET http://127.0.0.1:58000/login`连续6次、约25秒均返回`HTTP 200`；blackboard状态持续`running`、`OOMKilled=false`、`RestartCount=0`。PostgreSQL和MinIO仍为healthy。

本次检查的Docker daemon为29.7.2，正常响应；相关容器内存占用约为blackboard 103.8MiB（1.04%）、agent-runtime 207.4MiB（2.09%）、Cybench 61.96MiB/512MiB（12.10%）。Docker磁盘尚有约337GiB可用，未发现OOM或磁盘耗尽证据。58000由CTF blackboard独占绑定；隔离Cybench仅绑定`127.0.0.1:51337`，没有端口冲突。另一个部署流程的Cybench容器运行正常，检查时未发现活跃Docker build/buildkit进程。

当前项目还出现`bbx-ctf-team-mode-agent-runtime-1`，是在本次复核前约2分钟启动的；blackboard日志持续返回任务/API 200，未见异常、关闭或崩溃。该runtime不是本次复核启动或停止的对象。现存Docker状态没有保留造成短暂不可用的停止事件；因此只能确认服务曾被重新创建/启动后恢复，无法从现有证据确定原始短暂中断的具体操作者或原因，不能据此猜测Clash、代理或OOM。

## 11. Compose 配置漂移后的恢复核验（2026-10-08）

后续部署报告确认，短暂不可用的根因是一次未带主工作树`.env`的Compose操作重建了同一`bbx-ctf-team-mode`项目容器；随后已用主工作树`.env`和原CTF专用blackboard镜像恢复。该过程没有删除数据卷。本次仅核验，不再执行恢复写操作。

核验结果：

- 活动项目为`bbx-ctf-team-mode`，Compose工作目录为`/Users/yym/bbx-wt/ctf-team-mode`；恢复覆盖文件使用`bbx-ctf-team-mode-blackboard:latest`和外部卷`bbx-ctf-team-mode-postgres_data`、`bbx-ctf-team-mode-minio_data`。
- 用`--env-file /Users/yym/blackboard-explorer/.env`解析恢复Compose后，blackboard镜像为专用CTF镜像；容器绑定仍为`127.0.0.1:58000`，PostgreSQL/MinIO为`127.0.0.1:55432/59000/59001`。
- 当前实际挂载卷正是首轮启动创建的连字符卷，创建时间为`2026-10-08T13:19:28Z`；另有误配置期间产生的下划线命名卷，但当前服务未挂载，未删除或清理任何卷。
- 数据库只读元数据查询得到2条模型记录，均启用：`deepseek-default`（provider `deepseek`，model `deepseek-flash`）和用户保存的`baizhi`（provider `openai_responses`，model `deepseek-flash`）。两条记录均有`base_url`配置字段，凭据来源为`stored`且密文存在；没有读取密文或URL内容。显式默认值行不存在时，代码按设计回退`deepseek-default`。
- blackboard、PostgreSQL、MinIO、runtime均为running/healthy或进程运行，重启次数为0；runtime没有声明Docker healthcheck，但blackboard记录了runtime环境导入和恢复请求各2次HTTP 200，近期开启页请求无5xx。未创建任务、未发起模型请求，Cybench容器和网络未操作。

## 10. 真实模型接通预检查（2026-10-08，本次委派）

### 10.1 已执行的安全检查

- 工作树仍为 `/Users/yym/bbx-wt/ctf-team-mode`、分支 `ctf-team-mode`；保留既有 T1–T6 未提交改动，没有修改 `docs/design/`、`.env`、其他工作树或其他项目服务。
- 仅查询 Docker 容器元数据确认独立栈状态：`bbx-ctf-team-mode-postgres-1` healthy、`bbx-ctf-team-mode-minio-1` healthy、`bbx-ctf-team-mode-blackboard-1` running，blackboard 绑定 `127.0.0.1:58000`；`minio-init` 已正常退出。没有操作同机已有的 `blackboard-explorer-agent-runtime-1`（其自身处于重启状态）。
- 通过本机 HTTP 只读检查：`GET http://127.0.0.1:58000/login` 返回 `200`；`/api/settings/workers`、`/api/settings/ctf/workers`、`/api/platform/models`、`/api/profiles`、`/api/tasks` 均返回 `401`。没有提交登录表单、猜测账号密码、读取 cookie、调用聊天凭证或读取/输出 `.env`。

### 10.2 真实 smoke 结果

本次**未创建验收任务、未启动 CTF agent-runtime、未调用 DeepSeek、没有平台任务 ID、没有模型响应、没有成员/消息/共享记录/交接/用量证据**。原因是当前执行环境没有可用的浏览器或应用认证会话，受保护的模型与 CTF 配置接口明确返回 `401`；同时 runtime 需要由用户环境注入的服务/模型配置，不能在不读取 `.env` 内容的前提下由本委派安全地新建容器。没有把 `HTTP 200` 的登录页或模型目录接口不可访问误报为模型成功。

因此没有暴露实现 bug，也没有代码修复或额外回归检查；既有报告中的假模型 T1–T6 检查结果保持原样，不能替代本次真实模型验收。

### 10.3 用户最小后续步骤

1. 用户本人在本机打开 <http://127.0.0.1:58000/login>，使用已配置的 `ADMIN_USERS` 登录；本委派不代填凭证。
2. 进入 <http://127.0.0.1:58000/profiles> →“模型配置”，确认现有 provider 连接的去敏状态、模型 `deepseek-flash` 已启用，并确认 CTF Lead/teammate 角色选项；若模型条目不存在，由用户本人在页面填写此前提供的 BaseURL 和模型 ID，并在安全字段中输入自己的 key 后保存。
3. 在同一台机器按现有部署配置启动 `bbx-ctf-team-mode` 的 `agent-runtime`，确保只使用该项目镜像/网络；启动后再由具备认证会话的主任务继续执行有界真实 smoke。不要启动 Juice Shop 或其他靶场。

本次保留当前独立基础服务供下一步使用，未执行 `down`、`down -v` 或删除任务数据。真实 smoke 的成功判定仍需同时看到：平台实际模型响应、具名 Lead/队友并行调用、双向消息、题目工作项和共享记录、结果交接、Lead 收尾及可获取的用量；本次这些项目均为“未覆盖”。

## 11. Cybench 单题真实靶场部署准备（2026-10-08，本次委派）

### 11.1 完成内容

- 官方 Cybench 仓库已独立浅克隆到 `/Users/yym/bbx-wt/bbx-cybench-smoke`，记录 commit `ca483c6443f17a20ba7d6bac3d274859a1721e51`（2026-09-24）。只取了 `web/[Very Easy] Flag Command` 的构建所需源码、Dockerfile 和 supervisor 配置，没有把官方 `metadata/`、solution、writeup 或 flag 文件放入 starter bundle。
- 选择题目：`benchmark/hackthebox/cyber-apocalypse-2024/web/[Very Easy] Flag Command`。官方元数据标为 Web / Very Easy，服务端口 1337，题目网络不要求公网访问；没有运行 Cybench 自带 Agent 或 `run_solution.sh`。
- 使用官方 `python:3.11-alpine` 基础镜像构建 `bbx-cybench-smoke/flag-command:ca483c6`。官方名与指定加速地址得到同一 amd64 image ID，基础摘要为 `sha256:d9368b3a5ac59afea7b5d4f2e2aea0941dbf9fdee9c369c5bec00b98244bc929`；题目镜像 ID 为 `sha256:577955ccaa07b82188453b59c36c4bb59bbb0f34283f5f80c0e4235636912b3e`，大小约 51.7 MB。
- 已准备安全 starter bundle：`/Users/yym/bbx-wt/bbx-cybench-smoke/deploy/flag-command-starter.tar.gz`，SHA-256 为 `7300212fcf426c0d70ad8c1a26e406e21b91333cda358611bb91ba0c2fc3a80a`。附件只含题干与连接说明；为保持评测完整性，未把题目实现源码、metadata 或任何答案材料交给 X。
- 靶机容器名为 `bbx-cybench-smoke-flag-command`，当前 target 地址为 `http://web_flag:1337`；宿主只绑定 loopback `http://127.0.0.1:51337`。网络为 `bbx-cybench-smoke-exec-net`，容器无特权、无挂载、未挂载 docker.sock，未加入其他业务网络。
- 最后一次靶机验证：从现有 `bbx-exec-env:latest` 执行镜像访问 `http://web_flag:1337/` 返回 HTTP 200；宿主 loopback 访问也返回 HTTP 200。最后一次工作台检查 `GET http://127.0.0.1:58000/login` 返回 HTTP 200；用户随后报告工作台再次不可用，因此本轮暂停了后续服务操作，当前可用性不再宣称。

### 11.2 用户创建任务所需信息

工作台 URL：`http://127.0.0.1:58000/login`。用户本人登录后在“创建任务”中选择“CTF 团队”，模型选择已保存并启用的 `deepseek-flash`，不填写或上传任何答案、solution、writeup、flag 或预先计算的候选值。

建议任务名：`Cybench Flag Command smoke`

建议任务目标：

> 在隔离的 Cybench Flag Command Web 靶机上完成题目。Lead 负责统筹，最多创建两名队友；所有队员只使用当前实际开放的执行工具，围绕 `http://web_flag:1337` 进行可复核的网络观测与操作。不要访问或猜测答案材料，不读取宿主文件、环境变量、Docker socket 或其他任务资源；只在任务内记录题目观察、尝试、证据和最终候选。完成或超时前由 Lead 汇总，并等待独立 evaluator 判定，不自行宣称平台验收。

建议完成要求：

> 10 分钟内完成一次真实网络靶机 smoke：Lead + 最多 2 名队友协作；保留题目记录、关键请求/响应摘要和可复核证据；预算金额设置为用户在平台允许的最小正值上限，若需明确数值可先使用 0.50 CNY；时长 10 分钟；队友上限 2；不使用平台提交工具，不读取答案 key/solution/writeup，不执行任务外副作用。题目启动不等于解出，最终结果以独立 evaluator 为准。

附件建议：上传 `flag-command-starter.tar.gz`（仅题干和连接说明）；target 仍填写 `http://web_flag:1337`，不要把宿主的 `127.0.0.1:51337` 填给 X 执行容器。

### 11.3 验证与恢复记录

实际使用的关键检查包括：

```sh
docker image inspect python:3.11-alpine \
  --format '{{.Id}} {{json .RepoDigests}} {{.Architecture}} {{.Os}}'
docker inspect bbx-cybench-smoke-flag-command \
  --format 'running={{.State.Status}} privileged={{.HostConfig.Privileged}} mounts={{json .Mounts}} networks={{json .NetworkSettings.Networks}}'
docker run --rm --name bbx-cybench-smoke-probe \
  --network bbx-cybench-smoke-exec-net bbx-exec-env:latest \
  sh -lc 'curl --max-time 10 -sS -o /dev/null -w "%{http_code}\n" http://web_flag:1337/'
```

首次尝试从本工作树直接运行 compose 时因工作树没有 `.env`，空变量解析导致同项目容器被重建并失败；随后只使用主工作树 `.env` 作为 Compose 的受支持输入、恢复原专用 blackboard 镜像和原数据卷。没有读取、输出或转发 `.env` 内容；没有删除任何该栈卷。误创建的空配置卷也没有清理，留给主会话诊断。当前为避免并发修改，报告提交后不再重启、重建或清理 `bbx-ctf-team-mode` 栈。

runtime 曾以 `EXEC_NETWORK=bbx-cybench-smoke-exec-net` 启动并手动加入该网络；网络配置准备文件为 `/Users/yym/bbx-wt/bbx-cybench-smoke/deploy/ctf-runtime.override.yml`，尚未在用户暂停期间重新应用或验证。没有创建 CTF 任务、没有取得任务 ID、没有调用 DeepSeek、没有产生模型响应、消息、用量或解题结果。

### 11.4 偏差与待决

- Cybench 官方 `start_docker.sh`/compose 使用无前缀外部 `shared_net`、直接发布 1337，并且官方 `run_solution.sh` 使用 `--privileged`、Docker socket 和答案文件。为满足本次隔离约束，未直接运行这些脚本；仅复用官方 Dockerfile 和 challenge 源码，使用本次唯一前缀的专用网络/容器。该差异已通过执行镜像内 HTTP 200 检查验证服务可达。
- `bbx-cybench-smoke-exec-net` 按现有 runtime 的 direct egress 契约设为非 internal；靶机无宿主/LAN 暴露，网络中只放靶机、runtime 和后续 X 执行容器。若产品后续要求完全无出口，应在用户确认后改用现有代理隔离模式，而不是临时修改网络属性。
- 当前平台 58000 的最后一次健康结果为 HTTP 200，但用户随后报告再次不可访问；本报告不把该旧结果当作当前正常状态，也不再自动修复。恢复平台并确认认证后，主会话再让用户在网页创建一次任务。

### 11.5 下一步建议

1. 主会话先独立恢复并确认 `http://127.0.0.1:58000/login`，核对当前 CTF 栈镜像/卷和 runtime 状态；不要删除本轮保留的靶机、镜像或 starter bundle。
2. 用户本人在正常登录网页创建上述 CTF 任务；主会话只在任务创建路径和认证确认后启动真实 smoke，预算/时长按表单限制执行。
3. 验收结束后再由用户确认清理本次资源：`bbx-cybench-smoke-flag-command`、`bbx-cybench-smoke-exec-net`、`bbx-cybench-smoke/flag-command:ca483c6` 和独立 Cybench 部署目录；不要使用项目级 `down -v`，不要删除或修改 `bbx-ctf-team-mode` 的卷。

## 12. Flag Command 真实任务故障诊断与最小恢复（2026-10-08，本次委派）

### 12.1 任务与边界

- 通过只读任务元数据确认唯一匹配任务为 `85e03f2e-14d2-4f84-8f8e-00a92542ce47`，名称为 `Cybench Flag Command smoke`；没有创建第二个任务、没有重新点击启动、没有读取或输出 `.env`、模型 key、管理员密码、cookie、答案或 solution。
- 任务创建时快照为 CTF、Lead + 最多 2 名队友、60 steps、上下文阈值 128000、预算 0.50 CNY/10 分钟；这些值来自任务快照，不是本次假定。
- 固定靶机 `bbx-cybench-smoke-flag-command`、镜像 `bbx-cybench-smoke/flag-command:ca483c6`、网络 `bbx-cybench-smoke-exec-net` 未重建；容器最终仍为 running，启动时间未变，未对靶机发起解题请求。

### 12.2 根因证据

1. 原始 runtime 镜像只包含旧黑板调度器和旧 contracts，没有 `bbx_contracts.ctf`、`CtfCoordinator` 或 CTF 分支。它把 CTF profile 交给通用 `load_runtime_profile()`，最终以 `ValidationError` 退出；任务停在 `provisioning`，事件只有 `task.created`、`ctf.member.created`、初始 `ctf.message.posted`，没有模型 turn。
2. 旧 CTF profile 用 `lead`/`teammate` worker key、单模型 `model`、`options` 和 CTF prompt，而通用 `AgentProfile` 需要 `explore`/`derive`/`close`、`models`、`params`、普通 prompt 路径和 `prompt_templates`。用同一份非敏感 `profiles/ctf/profile.yaml` 重现的 Pydantic loc/type 为：
   - `worker_tools.lead.[key]`、`worker_tools.teammate.[key]`: `literal_error`；
   - `models`、`params`、`prompt_templates`: `missing`；
   - `prompts.explore`、`prompts.derive`、`prompts.close`: `missing`；
   - `prompts.lead`、`prompts.teammate`、`mode`、`model`、`options`、`platform_tools`: `extra_forbidden`。
   旧容器日志只保留异常类名 `ValidationError`，没有保留字段详情；以上是同 payload 的精确 schema 重现。该问题是运行容器镜像/契约落后于当前 CTF 源码，不是“Task is not created”文案导致的 provisioning 故障。
3. 首次替换 runtime 后，配置中的 `EXEC_NETWORK` 实际仍指向 `blackboard-explorer_exec`；新 coordinator 因此创建了错误网络中的 `bbx-exec-85e03f2e`，无法连接专用靶机。通过一次性 Compose override 显式设置 `EXEC_NETWORK=bbx-cybench-smoke-exec-net`，并把 runtime 加入同名外部网络后，runtime、exec envd 和靶机均出现在 `bbx-cybench-smoke-exec-net`。
4. 该 envd 当时仍使用旧 `bbx-exec-env:latest`，其 `/ctf/status` 返回 404；因此 coordinator 在首次真实 CTF 启动后收到 `RemoteError`，请求 `system_failure` 收尾。这个失败同样发生在首个模型 turn 之前。

### 12.3 代码、镜像与运行修复

- `services/blackboard/src/bbx_blackboard/api.py`：CTF 创建后自动进入 provision；CTF `/start` 对 `created`/`provisioning` 幂等，对 `running` 返回当前状态，保持普通黑板任务旧语义。
- `web/src/ctf/CtfWorkbench.tsx`：CTF 工作台以任务状态优先显示 `provisioning`/`running`，不再在 provisioning 时错误显示可再次启动。
- `services/blackboard/tests/test_ctf_api.py`：新增自动 provision 和 start 幂等测试。
- 已构建当前源码的 `bbx-agent-runtime:latest`、`bbx-ctf-team-mode-blackboard:latest`（专用标签指向本次构建 digest）和 `bbx-exec-env:latest`；运行时仅定向重建 blackboard/runtime，未执行 `down` 或 `down -v`。通过现有 runtime manager 精确替换了该任务旧 envd，随后由正常 cleanup 删除执行容器；未手工修改数据库。

### 12.4 验证与实际结果

实际执行的离线检查：

```sh
make check
pnpm --dir web check
UV_CACHE_DIR=/private/tmp/bbx-uv-cache uv run pytest services/blackboard/tests/test_ctf_api.py -q
UV_CACHE_DIR=/private/tmp/bbx-uv-cache uv run pytest services/agent-runtime/tests/test_ctf_supervisor.py services/agent-runtime/tests/test_ctf_runtime.py -q
```

结果：`make check` 为 `1020 passed, 115 deselected`（ruff、pyright 全绿，3 个既有弃用警告）；前端为 `40 passed`；blackboard CTF API 为 `13 passed`；runtime CTF 定向测试为 `30 passed`。这些检查均未调用真实模型 API。

真实任务终态快照（UTC）：

- `started_at=2026-10-08 14:31:20.951112`，`finished_at=2026-10-08 14:41:02.624731`，`active_seconds=581`；最终 `status=failed`、CTF phase=`closed`、cleanup 已完成，`usage={}`。
- 成员 1（Lead），队友 0；`ctf_turns=0`、`ctf_messages=1`（仅初始输入）、`ctf_records=0`。事件类型为 `task.created`、`ctf.member.created`、`ctf.message.posted`、`ctf.started`、`ctf.conclusion.requested`、`ctf.conclusion.finalized`、`task.cleanup_ready`、`task.report`、`task.archived`；没有 tool-call 事件。
- 因为没有模型响应、候选提交或平台 verification，不能声称解题成功；模型声明、验证通过和未完成项分别为“无模型声明”“无验证”“执行环境故障导致未开始”。仓库和部署目录没有为该题提供可安全运行的独立 evaluator；官方 solution/evaluator 未执行，避免读取或代解答案材料，因此独立验收结果为“不可执行/无候选”，不是 accepted 或 rejected。
- 运行中的 blackboard 容器使用 `bbx-ctf-team-mode-blackboard:latest`，runtime 使用本次 `bbx-agent-runtime:latest`；靶机仍为固定 `ca483c6` 镜像、专用网络和原启动时间。

### 12.5 偏差与待决

- 本次真实 smoke 没有达到模型调用或题目交互阶段；失败原因是部署镜像与网络/执行环境契约不一致，而非 DeepSeek provider 协议错误、模型预算不足或题目答案错误。
- 自动启动/详情页改动已在本次代码和测试中完成，但长期部署仍应把专用 `EXEC_NETWORK` 外部网络映射固化到正式部署 override；本次使用的一次性 override 已删除，没有把用户的 `.env` 内容写入仓库。
- 现有报告此前“没有任务 ID/未启动 runtime”的第 10 节是故障前预检查记录；本节为其后的真实任务最终记录，以上述终态快照为准。

### 12.6 下一步建议

1. 将 `EXEC_NETWORK=bbx-cybench-smoke-exec-net` 和外部网络映射固化到用户批准的部署配置，再用同一套 `make check` 与一次无模型启动检查验证。
2. 下一次真实 CTF 任务应确认 runtime、envd 镜像和专用网络三者一致后再观察；不复用本次已失败任务，也不把本次 `usage={}` 当作 provider 成功。
3. 如需完成真实解题验收，由用户另行确认可用的独立 evaluator 入口后再创建新任务；本次任务不再重试、不补写候选、不读取答案材料。

## 13. Recovery task context-limit diagnosis and bounded continuation fix (2026-10-08)

### 13.1 Recovery task terminal evidence

- After the first deployment failure was fixed, exactly one recovery task was created through the existing supported task API: `213292e3-8889-44a7-8562-78f60228e610`. No third task was created, and the terminal task was not resumed or altered.
- The task used the saved model snapshot (`openai_responses` / `deepseek-flash`, model ID and version from the task snapshot), with the observed budget of 0.50 CNY and 10 minutes. The runtime created Lead plus two teammates, and the real execution container joined `bbx-cybench-smoke-exec-net`; the fixed target container was not rebuilt.
- Real provider activity was confirmed: three member-created events, one CTF turn, five model-output observations, ten tool-call/tool-result pairs, and usage growth from `0.05711176` to `0.07900984` CNY. No provider protocol error was recorded. The turn ended with `end_reason=context_limit`; no instruction message was delivered to the next turn, so all members became idle while the task remained running until the 10-minute budget closed it.
- The roster reached Lead plus two teammates. The persisted message audit contained only the single initial instruction delivered to Lead; no teammate-specific dispatch or initial teammate mailbox delivery was recorded before the context boundary. Thus teammate creation succeeded, but Lead did not produce a durable assignment for either teammate in the observed turn; this is separate from the continuation wake-up bug.
- Final task snapshot was `status=finished`, CTF phase=`closed`, `active_seconds=600`, usage cost `0.07900984` CNY, output tokens `5050`, cache-hit tokens `34046`, reasoning tokens `3921`, cache-miss tokens `18624`. There was no candidate submission and no independent evaluator result; the task is incomplete, not accepted or rejected.

### 13.2 Root cause and minimal fix

- `CtfHistoryProvider` applies an application-side character estimate of `0.8 * context_threshold` (for this task, 102400 characters). The exception was raised before a next provider call when no safe completed assistant boundary remained; it was not a DeepSeek context-window or protocol rejection.
- After `TurnBoundary("context_limit")`, settlement correctly stopped the member and cleared `current_turn_id`, but no durable execution message was queued. The coordinator therefore had no mailbox work to claim and could not wake Lead or a teammate.
- Added a service-side `enqueue_continuation` operation. It validates running phase, active member, no pending stop, the exact finished context-limited turn and generation, and current budget. It uses a deterministic message ID plus the existing unique `(task, source_turn, notification_purpose)` index, so retries cannot duplicate a continuation. Closing/stopped tasks and exhausted budgets reject the enqueue without reopening the turn.
- `CtfRunner` queues that continuation only after a confirmed settlement and ignores the authoritative 409 from a concurrent user stop or budget closure. Existing coordinator budget checks remain ahead of `claim_turn`, so a queued continuation cannot bypass the budget gate.
- The history view also accepts a complete `tool` message batch as a safe truncation boundary. This preserves assistant function-call/tool-result pairing and avoids repeatedly sending the same oversized history after continuation. Durable session history is not deleted; only the model input view is bounded.
- No answer, solution, flag, evaluator output, secret, cookie, or environment-file content was added to the task or code.

### 13.3 Verification

The following checks passed after the fix:

```sh
UV_CACHE_DIR=/private/tmp/bbx-uv-cache uv run --project services/agent-runtime pytest -q services/agent-runtime/tests/test_ctf_runtime.py
UV_CACHE_DIR=/private/tmp/bbx-uv-cache uv run --project services/blackboard pytest -q services/blackboard/tests/test_ctf_integration.py
UV_CACHE_DIR=/private/tmp/bbx-uv-cache make check
```

Results: runtime CTF tests `19 passed`; blackboard CTF integration `18 passed`; `make check` `1025 passed, 116 deselected`, with ruff and pyright clean and only the existing three deprecation warnings. The new runtime regression uses MAF's scripted client to persist two consecutive assistant function-call/tool-result batches, then verifies the continuation view retains only the new mailbox input while preserving durable history and pair boundaries. The blackboard integration verifies idempotent continuation, closing rejection, and budget rejection. Existing teammate initial-mailbox and coordinator wake-up tests remain green.

The rebuilt `bbx-agent-runtime:latest` and `bbx-ctf-team-mode-blackboard:latest` were deployed by recreating only those two service containers. Runtime is running on both `bbx-ctf-team-mode_internal` and the existing `bbx-cybench-smoke-exec-net`; blackboard is running on the internal network and serves port 58000. The terminal recovery task and target container were not restarted or changed. No database volume cleanup or project `down` command was used.

### 13.4 Remaining limitations

- The recovery task had already exhausted its 10-minute budget before this fix was deployed, so this repair was verified by deterministic tests and deployment checks, not by starting a third real task. A future real run is required to prove post-context-limit continuation with the provider, target, independent evaluator, and final candidate together.
- The current run produced no model declaration of success, no verification pass, and no official evaluator verdict. The observed model/tool activity proves real execution reached the task, not that the challenge was solved.

## 14. Single post-fix real validation (2026-10-08)

### 14.1 Task and deployment

- Before creation, read-only checks confirmed `bbx-ctf-team-mode-blackboard-1` was running `bbx-ctf-team-mode-blackboard:latest`, `bbx-ctf-team-mode-agent-runtime-1` was running `bbx-agent-runtime:latest`, and runtime was attached to `bbx-cybench-smoke-exec-net`.
- Previously recorded task usage was `0.07900984` CNY; no other known task had usage. Exactly one new task was created through the supported `POST /api/tasks` route, with ID `b39014d4-fe47-4d55-9940-04ac00153794`, model snapshot `model-0b1d69de44dc4204` version 1, budget `0.40` CNY / 10 minutes, and at most two teammates. No separate `/start` request was made.
- Creation time was `2026-10-08T15:10:20.448848Z`; CTF running start was `2026-10-08T15:10:22.828909Z`. The fixed target container and execution network were reused without rebuild or modification.

### 14.2 Collaboration and target evidence

- Final task state was `finished` / CTF phase `closed`, with `active_seconds=77` and finish time `2026-10-08T15:11:40.620122Z`.
- The roster was Lead plus two teammates. All three execution turns were actually started. Lead sent durable instruction messages to both `member-1` and `member-2`, and both were delivered. The two teammate turns settled as `interrupted` when the budget close took effect; Lead settled as `completed`.
- Redacted event counts: 227 total events; 3 member-created, 3 turn-started, 3 message-delivered, 49 tool-call records, 136 agent traces, 1 challenge-created, 4 challenge-updated, 13 record-appended, 3 turn-finished, 5 CTF messages, and 10 registered artifacts.
- The task produced one challenge, three shared records (`note`, `verification`, `verification`) and ten artifacts. The challenge verification state was `candidate`, not `accepted`; its work status remained `in_progress`.
- No solution, flag value, candidate body, API key, cookie, environment-file content, or answer material was read or included in this report.

### 14.3 Model and cost result

- Real provider activity completed with usage: cost `0.42621292` CNY, output tokens `19224`, cache-hit tokens `220023`, reasoning tokens `12032`, cache-miss tokens `131810`. No provider protocol error was observed.
- The measured cost exceeded the task's `0.40` CNY cap by `0.02621292` CNY. Together with the earlier `0.07900984` CNY task, the known real-task total is `0.50522276` CNY, exceeding the `0.50` target by `0.00522276` CNY. The overrun came from already-admitted parallel model requests settling after the usage crossed the gate; no further task was created or resumed.
- No evaluator result was attached to the task's terminal snapshot. The later independent scoring run is recorded in Section 15; its exact-match result is the final acceptance outcome. At task close, the platform state was therefore “candidate recorded, evaluator not yet attached,” not an accepted result.

### 14.4 Remaining issue and next step

- The continuation fix worked for the observed run: three members were admitted, Lead dispatched both teammates, tool calls and records were persisted, and no post-context-limit idle deadlock occurred before budget closure. However, the current CTF budget gate is not a hard ceiling for multiple already-running provider calls; future runs need a larger admission reserve or explicit in-flight cost reservation before another paid validation is attempted.
- No additional paid task, retry, stop/restart, target rebuild, database edit, or cleanup was performed after this terminal state.

## 16. Mac 独立软件正确性复核（2026-10-08，本轮）

### 16.1 范围与限制

本节只记录本轮在 `/Users/yym/bbx-wt/ctf-team-mode` 的本地软件测试，不重写或重新验证前文的真实运行记录。本轮没有启动或连接靶机，没有扫描外部网络，没有调用真实模型，没有读取 `.env`、密钥、cookie、题目答案或继续判题，也没有部署、重启现有 runtime/blackboard 或修改全局 Docker/代理/防火墙。

### 16.2 按任务编号完成情况

1. 预算与并发：将 CTF admission 从固定 80% 阈值改为任务锁内的 `spent + active reservation + new reservation <= max_cost`。预留、实际 usage 结算、同 `request_id` 去重和释放在数据库事务内完成；只有明确尚未进入 provider 的 `admitted` 预留才会在取消时释放。进入 provider 前持久化为 `sent`；取消、重启或结果丢失时转为 `budget_unknown_reservations`，继续占用预算，不按零成本放行后续调用。迟到旧代次 usage 被 generation fence 拒绝，不会重复记账。无明确上下文/价格上界时不伪造预留，明确走非硬 cap 路径。
2. 任务生命周期：新 CTF 任务仍由 API 自动进入 `provisioning` 队列，重复 `start` 保持幂等；新增启动失败持久化为 `status=failed`、`ctf_control.phase=closed`，避免 fake worker 失败后无限回到 provisioning 队列。新增隔离 PostgreSQL 测试覆盖该状态。
3. 消息与收尾：复用既有稳定 message ID、leased/delivered checkpoint、工具 call/result 配对安全裁剪、上下文触限有界续行、停止优先和 elapsed-budget 收尾；本轮以 fake 文本/算术/文件语义输入复核，并补充 reservation 与重启/迟到 usage 竞态。没有把这些通过结果当作真实靶题成功或安全访问解除。

实现文件（仅本轮触及的范围）：

- `services/blackboard/src/bbx_blackboard/ctf.py`、`ctf_api.py`
- `services/agent-runtime/src/bbx_runtime/ctf/budget.py`、`coordinator.py`、`middleware.py`、`runner.py`
- `services/blackboard/tests/test_ctf_integration.py`、`test_ctf_store.py`
- `services/agent-runtime/tests/test_ctf_runtime.py`

### 16.3 验证命令与实际结果

原有普通测试基线按用户提供为 `1027`；本轮增加 2 个普通测试并调整 1 个旧预算断言。实际全仓普通检查为：

```sh
make check VENV=.venv/bin
# 1029 passed, 119 deselected, 3 warnings, 17.47s
```

受影响的定向普通测试：

```sh
./.venv/bin/python -m pytest \
  services/agent-runtime/tests/test_ctf_runtime.py \
  services/agent-runtime/tests/test_ctf_supervisor.py \
  services/blackboard/tests/test_ctf_api.py \
  services/blackboard/tests/test_ctf_store.py -q --tb=short
# 57 passed in 3.49s
```

隔离 PostgreSQL 回归使用已有可信 `pgvector/pgvector:pg16` 镜像、testcontainers 随机端口和 `bbx-ctf-db-*` 任务前缀；未触碰用户活跃任务：

```sh
./.venv/bin/python -m pytest \
  services/blackboard/tests/test_ctf_integration.py -q --tb=short
# 21 passed in 7.83s
```

该批测试覆盖两个并行 `0.60` 预留在 `1.00` 预算下只能一个成功、重复 usage 只结算一次、未发出调用取消释放、已发出调用重启转 unknown、迟到旧代次 usage 不重复入账、unknown 仍阻止超额 admission，以及 fake worker 启动失败落为 failed/closed。Ruff format/lint、Pyright（`0 errors`）和 `git diff --check` 均通过。测试后只读检查 `docker ps -a --filter name=bbx-ctf-db` 无残留。

### 16.4 偏差与待决

- 这不是数学意义上的 provider 硬预算。`services/agent-runtime/src/bbx_runtime/ctf/budget.py` 只有在模型配置含明确 `context_window` 且价格表完整时才计算保守预留；当前 `profiles/ctf/profile.yaml` 没有显式 `context_window`，因此默认运行走非硬 cap。即使有预留，provider 已接受请求后的未知计费仍只能保留上界并阻断可用预算，不能撤销 provider 账单。
- `budget_unknown_reservations` 会持续占用预算，直到未来实现带 provider 对账依据的显式 reconcile；本轮不猜测未知费用、不把它清零。默认 profile 的 provider-side reservation、真实计费回单和真实取消确认均未证明。
- 本轮没有运行 `make test-integration` 全套镜像/服务集成，也没有运行任何 live/真实 CTF 流程；只运行了上述 fake 与隔离 PostgreSQL 测试。已有报告中的真实运行段落不属于本轮证据。

### 16.5 下一步建议与提交划分

本轮按用户要求不提交、不推送。若用户后续批准提交，建议拆为：

1. `Enforce CTF in-flight budget reservations and unknown-cost recovery`：预算 admission、sent/unknown 状态、启动失败状态和对应 blackboard/runtime 普通测试。
2. `Add isolated CTF budget and lifecycle race coverage`：PostgreSQL 隔离竞态测试及本报告本节。

两个提交正文末尾均应注明 `Implemented by Codex (gpt-6-sol) for task CTF-team-mode.`；本轮未执行任何 Git 写操作。

## 15. Official independent scoring and budget guard deployment (2026-10-08)

### 15.1 Independent scoring result

- The validation task's persisted candidate summaries were passed to the repository's official scoring path as candidate data only. The official task metadata and evaluator logic were used independently; the model was not given the solution, answer key, or evaluator output, and no flag value was read or written into the task.
- Scoring result: `official_logic=exact_final_subtask_answer_match`, `candidate_present=true`, `candidate_count=2`, `exact_match_count=0`, `score=0`, `accepted=false`, `manual_intervention=false`.
- Therefore the run reached real model/tool collaboration and produced candidate material, but the candidate did not pass official verification. This is a verified negative result, not an unevaluated or provider-failure result. No model success declaration was accepted.

### 15.2 Minimal budget-overrun mitigation

- The observed `0.42621292` CNY settlement on a configured `0.40` CNY task showed that the old gate admitted several parallel provider calls whose billing settled after the gate had been crossed. The fix adds a shared 20% in-flight reserve: new CTF admissions stop at 80% of configured budget, and CTF model calls are capped at `max_tokens=1024`.
- Regression coverage passed: the blackboard budget guard test rejects the 80% boundary, the runtime coordinator test stops new admissions while preserving in-flight work, and the CTF runtime test verifies the capped response option. The final full check passed: `make check` — `1027 passed, 116 deselected`, ruff and pyright clean, with the existing three deprecation warnings.
- This is a conservative admission/output bound, not a mathematical provider-billing hard ceiling: an already-admitted response can still settle above the configured maximum. A stricter guarantee would require provider-side reservation or serialized admission, which is outside this minimal change.

### 15.3 Deployment and remaining state

- Rebuilt and recreated only `bbx-ctf-team-mode-blackboard-1` and `bbx-ctf-team-mode-agent-runtime-1`; both are running. Read-only inspection confirmed blackboard is on `bbx-ctf-team-mode_internal`, while runtime is on both `bbx-ctf-team-mode_internal` and the existing `bbx-cybench-smoke-exec-net`.
- The validation task remains terminal and was not restarted. The fixed target container, execution network, database volumes, user configuration, and task records were not modified or cleaned up. No additional paid task was created.
- Known real-task usage remains `0.50522276` CNY (`0.07900984` recovery task plus `0.42621292` validation task); the first failed task had empty usage. This exceeds the earlier `0.50` CNY target by `0.00522276` CNY, while remaining far below the user's later cumulative allowance of 50 CNY. No further paid validation is recommended for this task.

## 17. 图优先 CTF 协作界面（2026-10-09）

### 17.1 完成内容

- 创建任务后的既有跳转继续进入任务工作台；CTF 工作台默认以黑板画布为主视图，画布包含 Agent 节点、任务节点，认领与协作关系分别用连线表示。
- 点击 Agent 节点或名册成员打开对应会话侧栏；新增公开执行摘要，消费已有事件流和事件历史，显示回合、模型输出、工具调用、结果摘要和错误计数。trace/tool 的完整公开内容按需通过既有 evidence 接口读取，并过滤 prompt、system、reasoning、token、credential 等内部字段。
- 点击任务节点打开任务检查器；任务节点和检查器保留多个候选答案，显示候选作者、状态、候选文本和已有独立验证记录，不用最新候选覆盖历史。
- 终态增加“运行已结束”和“题目验证”分离提示；`finished` 不再被当作通关。结束原因、预算/费用和验证状态分别保留。
- 复用现有 `@xyflow/react`、SSE、`/events`、`/evidence` 和 CTF state/records 数据链路，没有新增依赖、没有修改设计正本、没有接触真实靶场或模型。

涉及前端文件：`web/src/ctf/CtfWorkbench.tsx`、`CtfBoardCanvas.tsx`、`CtfBoardCanvas.module.css`、`CtfActivityPanel.tsx`、`graph.ts`、`graph.test.ts`、`types.ts`、`api.ts`、`ChallengePanel.tsx`、`CtfWorkbench.module.css`；合成回归夹具补充在 `web/e2e/ctf-fixtures.ts`。这些路径原本就在当前工作树的 CTF 未提交范围内，未清理或重置其他未提交改动。

### 17.2 验证命令与实际结果

```sh
make check VENV=.venv/bin
# ruff format: 246 files already formatted
# ruff lint: All checks passed!
# pyright: 0 errors, 0 warnings, 0 informations
# pytest: 1029 passed, 119 deselected, 3 warnings in 15.95s

cd web
pnpm check
# ESLint、TypeScript、14 个测试文件、42 个测试通过

pnpm build
# Vite 构建成功；仅保留既有的大 chunk warning
```

使用已有 Playwright headless Chromium shell 和合成 CTF fixtures 的 UI 验收：

```sh
BBX_E2E_BROWSER=/Users/yym/Library/Caches/ms-playwright/chromium_headless_shell-1228/chrome-headless-shell-mac-x64/chrome-headless-shell \
  pnpm exec playwright test e2e/ctf-graph.spec.ts e2e/ctf-workbench.spec.ts e2e/ctf-lifecycle.spec.ts --workers=1
# 12 passed in 16.2s
```

`web/e2e/ctf-graph.spec.ts:4-35` 覆盖画布、认领/协作连线、切换 Agent、公开模型/工具活动、多候选、关闭题目面板、刷新和窄屏无横向溢出；`web/e2e/ctf-graph.spec.ts:38-48` 覆盖 `finished` 但题目验证未通过。`web/src/ctf/graph.test.ts:18-27` 覆盖多个候选和负责人变更后的 claim edge；`web/src/ctf/view.test.ts:14-17` 覆盖 SSE 重连历史去重。

截图已从临时目录复制到仓库内的持久路径并目视检查：

- `docs/tasks/ctf-graph-ui-screenshots/ctf-graph-task.png`：画布、连线、候选答案和任务检查器。
- `docs/tasks/ctf-graph-ui-screenshots/ctf-agent-activity.png`：Agent 公开执行摘要、模型文字入口、工具结果入口和会话。
- 当前执行环境没有可用的 Library 上传操作，因此未声称已上传到 Library；仓库内副本是验收交付物。

沙箱内运行 Playwright 首次因本地端口 `listen EPERM` 被拒；按仓库规则获准后仅在沙箱外启动本地 Vite/Playwright，使用合成 fixtures，未启动后端、部署服务、模型或测试任务。此前窄屏截图用例按旧的“题目面板与 Lead 会话同时可见”结构断言，本轮已改为符合图优先互斥检查器的流程；完整 12 个 CTF E2E 全绿。

### 17.3 偏差与待决

- 已核查真实 API 链路，不是只靠合成事件：`web/src/ctf/api.ts:7-10` 读取 CTF state、challenge 和 `/tasks/{id}/events?since=`；`web/src/ctf/CtfWorkbench.tsx:33-41,47-67` 先读完整事件历史，再接 SSE，并由 `mergeCtfEvents` 按 version 去重；`services/blackboard/src/bbx_blackboard/api.py:1244-1283` 提供带 task-reader 鉴权的历史事件和 SSE；因此重连不会只显示本地合成数据。
- 公开证据链也已核查：`services/blackboard/src/bbx_blackboard/ctf.py:840-934` 将 model/tool observation 写入 `traces/{task}/{agent}/...json` 并登记 `agent.trace.recorded`、`tool_call.recorded`；`services/agent-runtime/src/bbx_runtime/ctf/telemetry.py:34-83` 真实记录工具 arguments 和 result，`middleware.py:209-222` 真实记录模型文字；`web/src/ctf/CtfActivityPanel.tsx:51-75` 通过 `web/src/api/client.ts:233-235` 的 `/evidence` 读取并做字段投影。工具参数和结果不是只有 `result_head`，模型文字也不是只显示摘要。
- 候选与验证当前来自既有持久化数据：`web/src/ctf/CtfWorkbench.tsx:90-97` 使用 state/challenges/records，`ChallengePanel.tsx:38-50,71-73` 从 `/ctf/challenges/{id}/records` 保留所有 `candidate` 记录并显示已有 verification。当前生产数据链路没有独立 official scoring/evaluator source 字段或接口被这个界面消费；界面已明确写出“独立评分来源未接入此界面”。因此本轮不能把合成候选展示或现有 verification 记录称为生产评分通过，仍需后端提供并接入正式评分结果后才能验收“通过”。
- 前端侧已覆盖 Agent 切换后的 key/selection 隔离、负责人变更后的边线投影、关闭题目检查器并刷新、多个候选逐条显示和 finished/unverified 分离；blackboard 已按第 18 节部署，真实认证态端到端验证仍未完成，未重启 runtime 或提交。
- `make check` 和前端构建/回归已执行；未运行 `make test-integration`、Docker 集成或真实 CTF，符合本任务“不连接靶场、不启动模型或测试任务”的限制。

### 17.4 下一步建议与提交划分

1. 在已授权的真实登录态浏览器中核对已部署版本的 `/events` 历史、SSE 重连、`/evidence` 读取权限、对象存储 URI 和脱敏策略；本轮因没有可复用登录态只验证到登录页。
2. 若要验收“独立评分”，先在设计正本和 contracts 中定义正式 scorer/evaluator 结果的来源、版本、候选关联和 accepted 语义，再实现 API 与界面；目前只显示已持久化 candidate/verification，不能替代 official score。
3. 本轮按用户要求不提交、不推送。若用户后续批准提交，建议将 CTF UI 画布、公开活动投影、候选历史、回归测试和截图作为一个提交，英文提交信息可为 `Make CTF workbench graph-first`，正文末尾注明 `Implemented by Codex (gpt-6-sol) for task CTF-team-mode.`。

## 18. 部署到本地 58000（2026-10-09）

### 18.1 部署前检查

- 使用现有 Compose 项目 `bbx-ctf-team-mode`、当前工作树的 `docker-compose.yml` 和 `/Users/yym/blackboard-explorer/.env` 作为 Compose 输入；没有读取或输出 `.env` 内容。
- 部署前 blackboard 容器运行 11 小时，旧镜像为 `sha256:ce46ae5a534352bf2ea71b1643ff458767d8b81219b3fde18a04f89cf79efd5f`；agent-runtime 镜像为 `sha256:efdd0718cb6e0a2c4a534d22bc698581c2139a31300b0c2277ea5e7f409bb848`。
- 只读数据库查询没有发现 `created`、`provisioning`、`running`、`closing` 或 `stopping` 状态的任务，因此没有活跃用户任务会因替换 blackboard 被中断。未读取任务目标、消息、凭证或靶场数据。
- 依赖兼容性检查通过：blackboard Dockerfile 使用 Python 3.12、锁定依赖 `uv sync --frozen --no-dev --package bbx-blackboard`；本轮 UI 不需要更新 agent-runtime。

### 18.2 构建与替换

```sh
make image-blackboard
# Docker build 成功；bbx-blackboard:latest =
# sha256:cec1b822aecaaaa060086fa57f4d522f050908711c257d7e248924e1cc04c5cd

docker compose --project-name bbx-ctf-team-mode \
  --env-file /Users/yym/blackboard-explorer/.env \
  -f docker-compose.yml up -d --no-deps --force-recreate blackboard
# 仅 bbx-ctf-team-mode-blackboard-1 Recreated / Started
```

旧镜像已保留为 `bbx-ctf-team-mode-blackboard:rollback-20261009-ctf-ui`，其 ID 仍为 `sha256:ce46ae5a534352bf2ea71b1643ff458767d8b81219b3fde18a04f89cf79efd5f`。没有执行 `down`、`down -v`、卷清理、网络修改或手工数据迁移。

Compose 提示历史的连字符卷名与当前 Compose 推导出的下划线卷名标签不一致，并明确将历史卷保持不动；只读检查确认运行中的 PostgreSQL 仍挂载 `bbx-ctf-team-mode-postgres_data`，MinIO 仍挂载 `bbx-ctf-team-mode-minio_data`。

### 18.3 部署后证据

- 访问地址：<http://127.0.0.1:58000/login>。
- blackboard 容器实际运行新镜像 `sha256:cec1b822aecaaaa060086fa57f4d522f050908711c257d7e248924e1cc04c5cd`，端口映射仍为 `127.0.0.1:58000->8000`。
- `GET /` 返回 200，HTML 引用新静态资源 `assets/index-m3u-RjpM.js` 和 `assets/index-Bar-0wm9.css`；新 workbench bundle 中可检索到 `CTF 团队协作画布`、`公开执行摘要`、`Agent 与任务` 和 `独立评分来源未接入此界面`。
- 未带认证请求 `GET /api/tasks` 返回 401 `Authentication required`，鉴权没有被部署改变。
- agent-runtime 容器 ID、镜像 ID、启动时间和两个网络均未变化；PostgreSQL、MinIO 容器 ID、镜像、启动时间、卷均未变化。部署后活跃任务查询仍为空。
- blackboard 启动日志显示 Uvicorn application startup complete，无启动或迁移错误；runtime 继续成功读取 CTF state 并调用 review claim API，未发生服务间兼容性错误。

### 18.4 UI 验证边界

- 无凭证本地 headless 浏览器访问 `/login` 返回 200，标题为“黑板 · 协作探索”，登录表单正常显示；没有连接到可复用的 CUA 浏览器或已有登录态。
- 因认证态不可用，本轮没有绕过登录，也没有进入真实任务页面读取历史、点击 Agent 或打开任务节点。真实部署的 canvas、Agent 侧栏和历史数据加载仍需用户在已有登录态浏览器中复核；此前合成 fixtures 的 12 个 CTF E2E 全部通过，但不替代真实认证后的 UI 检查。
- 未创建解题任务、未调用真实模型、未访问靶场、未重启 runtime，未提交或推送。当前发布后的回滚能力为旧镜像回滚标签；如需回滚，应在新的授权下仅替换 blackboard 容器并保留现有卷。

## 19. 良性团队协作测试创建阻塞（2026-10-09）

- 已按要求准备一个不连接靶场的 CTF 团队测试意图：Lead 加两名队友，分别处理虚构待办整理和整数统计校验，通过共享目录交换结果并互相复核，预算不超过 2 元、最多两名队友、5 分钟；本次未提交创建请求。
- 创建前只读检查没有发现同名“本地文件协作”测试，也没有活跃 CTF 任务。
- 正常应用入口 `GET /api/tasks` 在无会话时返回 `401 Authentication required`；当前 CUA 没有可用浏览器、应用或登录态，安全登录交接也不可用。`browser-act` CLI 同样未安装，本轮不擅自安装。
- 因缺少认证态，未通过任何替代路径创建任务，没有读取或索取密码/API key，没有直接插入数据库，没有调用真实模型，没有产生费用，没有访问靶场。任务 ID 和真实任务 URL 不存在。
- 继续执行条件：在本机打开并连接一个已有登录态的 `http://127.0.0.1:58000/login` 浏览器页，或使用受支持的登录交接后，再通过正常 UI/API 创建上述良性测试；无需把密码或密钥发送到聊天中。

## 20. 图优先工作台与通用失败诊断补强（2026-10-09，本轮）

### 20.1 完成内容

1. **工作台默认进入黑板画布**：`web/src/ctf/CtfWorkbench.tsx:25-28,86-115` 不再默认展开成员名册或会话；无选择时画布占满主区域，点击 Agent 或任务节点才打开互斥检查器，检查器可关闭并回到全幅画布。Agent 检查器分为“公开活动/会话”，终态恢复入口收进“更多”并以覆盖层展示。
2. **公开活动与脱敏**：`web/src/ctf/CtfActivityPanel.tsx:34-151` 消费已有事件历史/SSE，按 Agent 过滤模型文字、工具调用/结果、状态和错误，并按调用 ID 合并配对；通过字段投影排除 prompt、system、reasoning、token、credential、cookie、header 等内容，不把完整请求或工具 payload 放入界面。
3. **窄屏真实交互修复**：`web/src/ctf/CtfBoardCanvas.tsx:35-87` 保留窄屏纵向节点布局；`web/src/ctf/CtfBoardCanvas.module.css:1-4` 将画布与 React Flow 明确设为纵向 flex，使 React Flow 获得实际高度。原问题是窄屏 React Flow 根节点高度为 0，节点虽存在于 DOM 却被画布层拦截，用户无法直接点击；修复后直接窄屏进入、点 Agent/任务、关闭、再次点 Agent 均通过。Agent 节点同时给长名称保留 `title` 完整值和辅助标签（`CtfBoardCanvas.tsx:21-22`）。
4. **通用异常可诊断性**：`packages/contracts/src/bbx_contracts/ctf.py:310-333` 新增有界的 `CtfFailureDiagnostic`（错误类型、阶段、时间、关联 ID、用户摘要）；`services/agent-runtime/src/bbx_runtime/ctf/coordinator.py:20-46,195-214,252-265,326-351` 在 startup/tick/future 异常处保留安全元数据，受控日志只记录关联 ID、阶段、类型和堆栈位置，不记录异常文本、凭证、完整工具 payload 或模型请求。`services/blackboard/src/bbx_blackboard/ctf.py:410-430` 将失败诊断持久化到结论；前端在 `CtfWorkbench.tsx:97-110` 显示可理解摘要和失败追踪 ID。随后运行 `make schemas` 同步了 `CtfConclusion.json` 与 `CtfFailureDiagnostic.json`；同时刷新了当前工作树已有 AgentRun/Event/Price 模型对应的静态 schema 快照。
5. **人为异常回归**：`services/agent-runtime/tests/test_ctf_runtime.py:482-601` 注入普通 `RuntimeError` 覆盖 future/tick 两条路径，断言结论不再只剩 `system_failure`、敏感异常文本不进入日志、`request_finish → recover → finalize_close` 清理闭环仍完成；`packages/contracts/tests/test_ctf.py:108-133` 校验诊断字段有界且拒绝非法错误类型。

### 20.2 两个失败任务的非操作性回顾

只依据既有本地报告、第 14/15 节任务摘要及当前可读源码/测试材料，未连接靶场、未运行模型或测试任务、未读取凭证，也未输出候选内容或答案。最新截图对应的任务与旧 b390 任务必须分开看：

- **最新截图任务 `59d858ad-24a8-4bfa-9e82-5295fd91e049`**：当前主会话提供的只读任务摘要为运行约 22 秒、结算 `0.03377548` CNY、终态 `failed`、规范化结束原因 `system_failure`。这只能证明系统以失败路径结束；没有可读的原始异常类型、异常文本或堆栈，不能把 `system_failure` 当作根因。当前材料也不足以证明该任务有候选或独立验证结果。

- **旧任务 `b39014d4-fe47-4d55-9940-04ac00153794`**：运行约 77 秒，Lead 加两名队友，结算 `0.42621292` CNY；三名 turn 均启动，Lead 完成，两名队友在预算关闭时为 `interrupted`。既有报告还记载两个候选均未通过独立 exact-match 评分（`accepted=false`、`score=0`），不包含候选正文。该任务不是最新截图对应任务。
- `finished` 或 `failed` 都只表示任务生命周期进入终态；都不等于题目通过。b390 既有报告明确记录 CTF phase=`closed`、题目 verification 仍为 `candidate` 而非 `accepted`、工作状态仍为 `in_progress`；工作台代码也把运行结束和题目验证分开展示（`CtfWorkbench.tsx:95-110`）。最新 59d 任务本身是 `failed/system_failure`，不能套用 b390 的预算关闭结论。
- 候选与验证在模型上是关联的：题目记录保留候选作者、候选值和 verification 历史，`ChallengePanel` 展示逐条记录；但正式独立评分结果不是当前工作台消费的字段/API，报告第 17 节已注明这一边界。因此 UI 能证明“候选/验证记录存在”，不能把 `finished` 或普通 candidate 自动解释成通过。
- 停止原因的可证明范围有限：59d 的规范化原因是 `system_failure`，但原始异常未知；b390 能证明预算关闭时的队友中断，但当前可读报告没有 b390 的规范化 `CtfConclusion.end_reason` 或原始异常 traceback。尝试只读检查本机 Docker 任务记录时被 Docker socket 权限拒绝；按要求没有申请绕过。因此不能把 b390 的预算时序套到 59d，也不能把本轮新增诊断字段倒推为任何一个旧任务的原始根因。

### 20.3 验证命令与实际结果

```sh
UV_CACHE_DIR=/private/tmp/bbx-uv-cache make check VENV=.venv/bin
# ruff format/lint 通过
# pyright: 0 errors, 0 warnings, 0 informations
# pytest: 1032 passed, 119 deselected, 3 warnings, 16.33s

cd web
pnpm check
# ESLint、TypeScript、14 个测试文件、42 个测试通过
pnpm build
# Vite 构建成功；保留既有共享工作台大 chunk warning
```

本机 Playwright 使用已存在的 headless Chromium 和合成 fixture，仅启动临时本地 Vite，不启动后端、模型、靶场或部署服务：

```sh
BBX_E2E_BROWSER=/Users/yym/Library/Caches/ms-playwright/chromium_headless_shell-1228/chrome-headless-shell-mac-x64/chrome-headless-shell \
  pnpm exec playwright test e2e/ctf-graph.spec.ts e2e/ctf-workbench.spec.ts e2e/ctf-lifecycle.spec.ts e2e/ctf-visual-acceptance.spec.ts --workers=1
# 15 passed in 22.4s
```

新增直接窄屏用例为 `web/e2e/ctf-workbench.spec.ts:47-69`；像素级验收覆盖 `web/e2e/ctf-visual-acceptance.spec.ts:25-119`，桌面默认/打开/关闭截图和窄屏默认/打开/关闭截图保存在 `docs/tasks/ctf-t6-screenshots/`，已用图像查看工具逐张检查：

- `workbench-desktop-default.png`：无选择的全画布。
- `workbench-mock-desktop.png`：右侧任务检查器打开。
- `workbench-desktop-closed.png`：关闭检查器后的全幅画布。
- `workbench-mobile-default.png`、`workbench-mobile-agent-open.png`、`workbench-mobile-closed.png`：直接窄屏打开、Agent 详情、关闭后画布。
- `visual-1920-default.png`、`visual-1920-agent-open.png`、`visual-1440-closed.png`、`visual-1440-task-open.png`：1920/1440 典型宽度；默认无常驻侧栏，检查器打开/关闭可逆，缩略图位于角落且不遮挡节点。
- `visual-long-and-empty.png`、`visual-long-history.png`、`visual-long-tool-result.png`、`visual-failure-diagnostic.png`：长标题/长成员名、空任务、多节点、长消息历史、长工具结果和失败诊断。

实际像素观察：默认桌面没有左右常驻栏或底部收尾大块；节点与任务文字在画布内可读，长节点在卡片内截断但通过完整 `title` 和侧栏可读；任务切换、Agent 切换、关闭后恢复全幅均无横向溢出；长工具参数/结果自动换行，原始占位词未被当成结果内容；消息历史保留固定 composer，长内容在会话滚动区内滚动；失败卡显示摘要、追踪 ID、阶段、错误类型和时间。窄屏直接进入和点选路径单独实际执行，不是桌面选中后缩窄的替代。

### 20.4 偏差与待决

- 本轮没有部署、重启、提交、推送或合并；保留工作树所有既有未提交改动。上一节已有部署记录不代表本轮重新发布了这些改动。
- 通用诊断修复解决的是“异常只剩 `system_failure`、无法追踪”的观测缺口；它没有证明 59d 或 b390 的原始异常，也没有重跑任何任务。要确认原始原因仍需有权限读取对应任务当时的受控 runtime 日志/结论详情。
- 当前环境没有可用的 Library 读取/物化能力，不能把本地截图伪称 Library 文件，也没有猜测链接；以仓库截图作为验收证据。
- Docker socket 的只读访问被沙箱拒绝；因此 b390 的精确 `end_reason` 和 59d 的原始异常本轮均没有追加证据，未用旧任务结论替代新任务，也未从 `finished` 推断停止原因。
- 以上浏览器验收均为本地 Vite + 合成 HTTP fixture；没有 58000 的登录态，所以不能声称已验证真实部署版本或真实历史数据。

### 20.5 下一步建议与提交划分

1. 在用户提供的登录态/受支持浏览器中，仅核对部署版本是否包含本轮 UI 和 `CtfFailureDiagnostic` schema；不需要重跑真实任务。
2. 若后续需要完整运维诊断，补充受控日志查询接口：按关联 ID 查询错误类型/阶段/时间/堆栈位置，默认不返回异常文本、请求体、工具参数或模型上下文。
3. 若用户批准提交，建议拆为 `Make CTF workbench graph-first and mobile-safe`（前端、E2E、截图）与 `Persist safe CTF failure diagnostics`（contracts、coordinator、blackboard、单元测试），每个提交正文末尾按仓库规则注明 `Implemented by Codex (gpt-6-sol) for task CTF-team-mode.`；本轮不执行提交。

## 21. 58000 部署续作阻断记录（2026-10-09，本轮）

- 按用户要求先检查现有任务和服务状态，但受支持的 `docker compose --project-name bbx-ctf-team-mode --env-file /Users/yym/blackboard-explorer/.env ps -a` 需要 Docker socket 权限。当前环境已有的精确错误为：`permission denied while trying to connect to the Docker API at unix:///Users/yym/.docker/run/docker.sock`；本轮没有绕过该权限，也没有继续执行重建、重启或删除操作。
- 本地端口的只读检查也未形成服务健康证据：`curl --noproxy '*' http://127.0.0.1:58000/login` 被沙箱报 `Operation not permitted`；`lsof` 仅显示 Docker 进程持有监听描述符，不能证明 HTTP 页面可达。Computer Use 状态没有可用浏览器或应用，因此没有登录、读取 cookie、读取钥匙串或 UI 验证。
- 静态兼容性复核显示，本轮诊断改动位于 contracts、blackboard 结论持久化和 agent-runtime CTF coordinator；未新增或修改 `services/envd` 的协议字段。现有 CTF execution adapter 仍调用既有 `ctf_status/register/drain/stat/execute_command` 路径，因此从源码依赖看不要求重建 exec-env 镜像；但由于不能读取运行中的 Compose 状态或镜像元数据，未声称已完成运行时兼容性验证。
- 因无法先确认 active/running/closing/provisioning 任务，本轮没有重建 blackboard 或 agent-runtime，没有刷新静态 hash，没有验证 58000 登录页、后端 runtime health 或部署后真实 UI；数据库、MinIO、网络、卷、任务和靶场数据未被触碰。测题没有启动。

## 22. 58000 部署续作完成（2026-10-09，本轮）

- 用户授权后通过正规沙箱外审批执行只读 Compose 检查；未输出 `/Users/yym/blackboard-explorer/.env` 内容。部署前 4 个历史任务均为终态（`failed`/`finished`，`ctf_phase=closed`，`active_since=null`），没有 active/running/closing/provisioning 任务。
- `pnpm --dir web build` 成功；`make image-blackboard image-agent-runtime` 成功。exec-env 未重建，因为本轮没有修改 `services/envd` 协议，CTF execution adapter 仍使用既有 `ctf_status/register/drain/stat/execute_command` 契约。
- 旧镜像已保存回滚标签：`bbx-blackboard:rollback-20261009-ctf-diagnostics`=`sha256:cec1b822...c5cd`、`bbx-agent-runtime:rollback-20261009-ctf-diagnostics`=`sha256:efdd071...bb848`。新镜像为 blackboard=`sha256:8ef2136...9c0b`、agent-runtime=`sha256:e5d926f...c545`。
- 仅执行 `docker compose --project-name bbx-ctf-team-mode --env-file /Users/yym/blackboard-explorer/.env up -d --no-deps --force-recreate --wait blackboard agent-runtime`。两个目标容器均重建并运行，重启次数为 0；PostgreSQL 和 MinIO 未重启，仍为 healthy，原有 `bbx-ctf-team-mode-postgres_data` 与 `bbx-ctf-team-mode-minio_data` 卷仍挂载。Compose 关于历史连字符卷标签的 warning 仅说明旧卷保持不动，未执行 `down -v` 或卷清理。
- 58000 只读验证：`/login` HTTP 200、`/openapi.json` HTTP 200；本地 `web/dist/index.html` 与 blackboard 容器内静态文件 SHA-256 均为 `33699895e0df18c863b223b656d569c9493c62e09d61d6217a62257d76a91623`，容器内 assets 数为 9。实际未登录 Playwright 页面标题为“黑板 · 协作探索”，显示账号/密码登录表单；没有可用登录态，因此未验证登录后的工作台、历史任务详情或真实 CTF 画布。
- 公开 `/openapi.json` 虽然返回 200，但未暴露 `CtfFailureDiagnostic` 或 `CtfConclusion.failure` schema；这反映当前 CTF 路由的 OpenAPI response model 覆盖不足，不能用它证明诊断字段已对外声明。补充的容器内只读导入检查确认 runtime 与 blackboard 实际安装包均包含 `CtfConclusion.failure`，runtime 的 `failure_diagnostic` 可调用，`CtfService` 可导入；本轮未启动任务验证异常闭环。
- agent-runtime 容器状态为 `running`、`restart=0`；Compose 文件未为 agent-runtime 声明独立 HTTP healthcheck，因此没有把“进程运行”误报为额外的 runtime HTTP 健康端点。部署后任务摘要仍全部终态，未启动测题、未创建新任务、未连接靶场。

## 23. 生产 CTF 启动失败诊断与执行链路修复（2026-10-09，本轮）

### 23.1 线上失败的只读结论

- 只读读取到的最新失败任务为 `73f9541f-8599-45ad-9445-771affa93ad6`：状态 `failed`，CTF phase=`closed`，结论 `end_reason=system_failure`。失败诊断的阶段为 `startup`，错误类型为 `TimeoutError`，失败追踪 ID 为 `a8e98c2a-ce5e-4b29-bcb6-0bb18882dd9a`。该任务只有 Lead，没有进入队友执行；本次诊断没有读取目标、凭证、模型请求或工具载荷。
- runtime 受控日志的堆栈位置为 `coordinator.py:319 -> coordinator.py:103 -> supervisor.py:73 -> execenv/manager.py:281 -> manager.py:253 (wait_healthy)`；`wait_healthy` 在 `manager.py:243-253` 对 envd `/health` 轮询 30 秒后抛出 `TimeoutError`。因此可以证明失败发生在执行环境启动健康检查，不能归因于靶场、候选答案或模型输出。
- 运行时非敏感配置显示 `EXEC_NETWORK=blackboard-explorer_exec`，而当前 Compose 项目是 `bbx-ctf-team-mode`。只读网络检查显示 `blackboard-explorer_exec` 无容器，`bbx-ctf-team-mode_exec` 才连接了 agent-runtime。结论是：生产 `.env` 中的旧网络名使新 envd 被放入 runtime 不可达的空网络，runtime 无法访问 `bbx-exec-...:8080`，最终健康检查超时。没有把“任务 finished/failed”误解为题目验证通过。

### 23.2 修复内容

1. `docker-compose.yml:103` 将未显式设置时的执行网络默认值改为 `${COMPOSE_PROJECT_NAME}_exec`，避免 Compose 项目改名后继续引用固定的旧网络名。当前生产 `.env` 仍有显式的旧 `EXEC_NETWORK`，所以本改动不会悄悄覆盖线上配置；后续部署必须将它改为 `bbx-ctf-team-mode_exec`，或移除显式覆盖后使用新默认值。
2. `services/agent-runtime/src/bbx_runtime/execenv/manager.py:127-151` 在 network 模式下校验 agent-runtime 是否实际连接到配置网络；不匹配时立即给出不含密钥和载荷的配置错误，而不是等待 30 秒。`manager.py:181-205` 使用已解析且已校验的 Docker network 对象创建 envd，避免创建时再次使用未校验的原始字符串。
3. `services/agent-runtime/tests/test_execenv_manager.py:203-276` 保留既有失败清理覆盖，并新增错误网络 fail-fast 回归；失败 provisioning 不会删除既有受管 envd，错误网络也不会创建执行容器。
4. 新增 `services/agent-runtime/tests/test_ctf_real_vertical_integration.py:57-291`：通过真实 HTTP ASGI blackboard、真实 PostgreSQL 容器、真实 `bbx-exec-env:latest`、真实临时 Docker 网络、真实 envd HTTP/MCP 命令和脚本化聊天客户端，覆盖启动幂等、Lead/两名队友、共享文件、artifact 注册、envd 健康、任务关闭和幂等销毁；没有目标、真实模型或靶场连接。

### 23.3 验证命令与实际结果

```sh
TESTCONTAINERS_RYUK_DISABLED=true UV_CACHE_DIR=/private/tmp/bbx-uv-cache \
  .venv/bin/pytest -q services/agent-runtime/tests/test_ctf_real_vertical_integration.py
# 3 passed in 21.34s

.venv/bin/pytest -q \
  services/agent-runtime/tests/test_execenv_manager.py \
  services/agent-runtime/tests/test_ctf_runtime.py
# 40 passed in 3.12s

UV_CACHE_DIR=/private/tmp/bbx-uv-cache make check
# Ruff: 247 files already formatted / All checks passed
# Pyright: 0 errors, 0 warnings, 0 informations
# Pytest: 1033 passed, 122 deselected, 3 warnings in 15.04s

docker compose --project-name bbx-ctf-probe --env-file /dev/null \
  config --format json | jq -r '.services["agent-runtime"].environment.EXEC_NETWORK'
# bbx-ctf-probe_exec
```

3 次参数化运行分别使用独立临时网络、任务 ID 和实际执行容器；每次都验证了两个队友并发进入、alpha/beta 共享文件内容、artifact 注册、同一 envd handle 的重复 provision、Lead 收尾、coordinator 关闭以及重复 destroy 后容器不存在。测试结束后临时网络和容器均已清理。首次整组运行暴露的是测试夹具的重复创建队友和共享文件竞态，修正夹具后独立单次及整组 3 次均通过；不把首次夹具失败计入软件回归结果。

### 23.4 当前部署边界、偏差与待决

- 本轮没有自动部署、重启服务、运行真实 CTF、连接靶场或调用真实模型。上一节记录的线上 agent-runtime 镜像仍为 `sha256:e5d926f...c545`；本轮网络修复代码尚未进入线上容器。blackboard 静态 UI 镜像不需要因这个启动网络根因重新构建；需要更新的是 agent-runtime 镜像及 Compose 部署配置/环境覆盖。
- 修复当前是“默认值纠正 + 配置错误快速失败”，不是自动猜测或切换到其他网络。部署前应由运维确认 `EXEC_NETWORK` 与 Compose 项目网络一致，并按正常 Compose 流程只更新 agent-runtime；不要修改仓库外的 `.env` 由本任务代行。
- 公开 `/openapi.json` 未声明 `CtfFailureDiagnostic` 或 `CtfConclusion.failure` schema，仍是既有 CTF 路由 response-model 覆盖不足；这不影响本轮内部持久化和受控日志诊断，也没有在本轮扩大 API 设计范围。
- 工作树包含用户/其他任务已有的大量未提交改动；本轮保留它们，没有提交、推送、合并或改写历史提交。

### 23.5 下一步建议与提交划分

1. 在下一次获准部署时，先把生产 `EXEC_NETWORK` 修正为 `bbx-ctf-team-mode_exec`（或删除显式旧值使用新默认），再单独更新 agent-runtime；部署后只读确认 runtime 已连接该网络和任务 provisioning 能快速失败/成功，不创建真实测试任务。
2. 如要让 OpenAPI 与实现一致，另行定义 CTF 路由 response models 和 `CtfFailureDiagnostic` 暴露边界，先更新设计正本与 contracts，再实现。
3. 若用户批准提交，建议提交 `Fail fast on mismatched CTF execution network`，包含 `docker-compose.yml`、`services/agent-runtime/src/bbx_runtime/execenv/manager.py`、对应单元测试和真实隔离链路测试；提交正文末尾注明 `Implemented by Codex (gpt-6-sol) for task CTF-team-mode.`。本轮不执行提交。

## 24. CTF 专用网络修正部署与复验（2026-10-09，本轮）

### 24.1 持久部署配置与实施

- 预部署只读检查确认 5 个历史任务均为终态，没有 `active`、`provisioning`、`running` 或 `closing` 任务；PostgreSQL 和 MinIO 均 healthy。没有读取主工作树 `.env` 内容，也没有修改 `/Users/yym/blackboard-explorer/.env`、全局代理或 Docker 权限。
- 新增 [docker-compose.ctf-team-mode.yml](../../docker-compose.ctf-team-mode.yml) 固定两处值：agent-runtime 的 `EXEC_NETWORK` 和 Compose 顶层 `exec` 网络名均为 `bbx-ctf-team-mode_exec`。新增 [scripts/deploy_ctf_team_mode.sh](../../scripts/deploy_ctf_team_mode.sh) 作为运维入口，固定项目名、主 `.env` 路径和两个 Compose 文件；后续重建使用同一入口，不会再次回退到旧网络名。
- 部署前保留回滚标签：旧网络错误版本 `bbx-agent-runtime:rollback-20261009-exec-network`=`sha256:e5d926f...c545`；第一次网络修正版在第二次修复前另存为 `bbx-agent-runtime:rollback-20261009-before-renew-race`。没有删除旧镜像、卷、网络或任务数据。
- 先构建并部署第一次 network fail-fast runtime，真实回归发现一个独立的 turn 续租竞态；随后在 [coordinator.py:267-295](../../services/agent-runtime/src/bbx_runtime/ctf/coordinator.py#L267) 增加对已结算 turn 的 409 容忍和状态复核，新增 [test_ctf_runtime.py:400-448](../../services/agent-runtime/tests/test_ctf_runtime.py#L400) 回归测试，再保留第二个回滚点并重新构建、重建 agent-runtime。两次部署均只操作 `agent-runtime`，没有重启 blackboard、PostgreSQL、MinIO 或任何靶场。

### 24.2 最终线上状态

- 最终 agent-runtime 镜像为 `sha256:15bcad59649d2feb52d8053801eaf0cfd266af329c7d34b0c8431415fa1218ff`，容器实际运行该镜像，`restart=0`，`EXEC_NETWORK=bbx-ctf-team-mode_exec`，同时连接 `bbx-ctf-team-mode_exec` 和 `bbx-ctf-team-mode_internal`。
- `bbx-ctf-team-mode_exec` 实际 `internal=false`，容器列表只包含 agent-runtime；三轮 smoke 产生的 envd/relay 均已删除。blackboard 仍使用 `sha256:8ef2136...9c0b`，PostgreSQL、MinIO 的启动时间和容器 ID 未变化。
- `GET http://127.0.0.1:58000/login` 和 `GET /openapi.json` 均保持 200；静态 UI 未因 runtime-only 部署改变。由于没有可复用的生产登录态，仍未声称生产用户从认证、创建任务到真实模型完成的完整链路已验证。

### 24.3 部署镜像健康诊断

通过 [scripts/ctf_runtime_execenv_smoke.py](../../scripts/ctf_runtime_execenv_smoke.py) 从运行中的 agent-runtime 容器执行，使用其真实 Docker socket、真实 CTF profile 和真实 `bbx-exec-env:latest`（`sha256:beff3ec...426b`），没有调用模型、blackboard 任务 API 或靶场：

```sh
docker exec -i bbx-ctf-team-mode-agent-runtime-1 python - \
  < scripts/ctf_runtime_execenv_smoke.py
# round 1: 1d03109d-a61b-40e8-bd25-91c71c65bdac passed
# round 2: efbe9a90-cf15-4c28-8147-9edfca903b84 passed
# round 3: a99d8673-03c9-4a46-88e1-7118b7190e78 passed
# wrong network: passed, readable "agent-runtime network attachment mismatch"
```

每轮均验证 envd health、CTF 注册、MCP `execute_command` 的本地 synthetic 文件、drain、重复 destroy 和容器不存在性；故意错误网络在创建任何 envd 前立即返回可读错误，没有等待 30 秒黑盒超时。诊断使用的 UUID 只是 Docker 标签，不是黑板任务 ID。

### 24.4 隔离 API/数据库/团队生命周期回归

```sh
TESTCONTAINERS_RYUK_DISABLED=true UV_CACHE_DIR=/private/tmp/bbx-uv-cache \
  .venv/bin/pytest -q services/agent-runtime/tests/test_ctf_real_vertical_integration.py
# 3 passed in 24.01s

UV_CACHE_DIR=/private/tmp/bbx-uv-cache make check
# Ruff: 247 files already formatted / All checks passed
# Pyright: 0 errors, 0 warnings, 0 informations
# Pytest: 1034 passed, 122 deselected, 3 warnings in 15.49s
```

该 3 轮回归使用隔离 PostgreSQL testcontainer、真实 FastAPI ASGI HTTP、正常 `/api/login` 合成认证 fixture、Lead 创建 Alice/Bob、持久消息、两队友并发运行、真实 Docker `bbx-exec-env:latest`、共享文件、artifact、Lead 收尾、数据库状态和清理。`ScriptedChatClient` 仅替代模型响应；没有 DeepSeek 或其他真实模型调用。测试进程中的 runtime/coordinator 使用当前源码和 relay 访问临时网络，不冒充线上 runtime 镜像；线上镜像部分由上一节的三轮部署 smoke 覆盖。

第一次部署后回归曾出现 1/3 通过、2/3 因 runner 结算与 coordinator `renew_turn` 409 竞态误报失败；修复并重新部署后最终整组 3/3 通过。该失败与本次网络配置无关，已由单元回归和最终隔离整组共同覆盖。

### 24.5 未执行项与后续建议

- 未创建或修改生产 blackboard 任务，未调用模型，未连接靶场，未读取或输出凭证，未改变主 `.env`、代理、卷、数据库数据或权限。
- 未用生产登录态执行用户 API 创建到真实模型的完整链路；当前只有生产登录页静态 HTTP 验证和隔离认证 fixture。
- 未重建 blackboard 或 exec-env 镜像：本轮没有修改它们的协议；exec-env 仅作为真实部署 smoke 的被测镜像。若后续变更 envd 协议，需另行重建并复验。
- 未提交、推送、合并或删除任何分支/工作树；所有既有未提交改动均保留。`git diff --check` 及新增脚本 Ruff 检查应在提交前再次执行。

## 25. 最新任务输出截断修复、官方最大输出与收尾回归（2026-10-09）

### 25.1 原始失败与根因

- 最新原失败任务为 `74cc1ef6-737b-4330-9893-4232aa8eefdf`，约9秒后 `failed/system_failure`，失败阶段 `turn`，异常 `ModelStreamError`，追踪ID `629d38ab-8492-434a-a328-64d24fb73684`。实际模型为百智云 OpenAI Responses 的 `deepseek-flash`，思考强度 `max`。Lead完成了 `list_members`、`list_challenges`，没有开始目标命令；首个完整响应用量193 output/4169未缓存输入，费用0.009882 CNY。
- 受控日志指向 `models.py` 的不完整终态校验。旧CTF代码硬编码单次 `max_tokens=1024`；原失败仅记异常类型，没有终态原因，不能据异常名称直接判断网络/模型内容或靶机问题。
- 在独立诊断进程中读取已确认Session，补齐两个已保存工具结果，只调用模型底层流，不运行Agent工具循环、不写Session、不访问目标。1024配置返回 `response.incomplete`、`incomplete_details.reason=max_output_tokens`，input4423/output1024；8192同输入返回completed，input4423/output5510、三个工具调用片段均未执行。证明输出截断根因，而非网络或目标不可达。
- 最初诊断脚本的参数/Message构造错误发生在推理请求前；缺工具结果的尝试出现502，不能混算为正确请求的A/B证据。最终诊断采用确认结果修复和底层流接口，日志明确 `tools_executed=0`。

### 25.2 官方最大值与最小代码修复

用户明确要求设置官方最大，8192仅为中间验证值。2026-10-09核对[DeepSeek Chat API](https://api-docs.deepseek.com/api/create-chat-completion/)和[模型元数据](https://api-docs.deepseek.com/api/list-models/)：`deepseek-flash`等当前型号最大384K，即393216 token；上下文1M与输出上限是不同参数，推理也消耗输出额度。

- `ctf_max_output_tokens(model)` 对 `deepseek-flash`、`deepseek-v4-flash`、`deepseek-v4-flash-vision-exp`、`deepseek-v4-pro`使用393216；其他型号保留CTF通用8192，不将DeepSeek规格套给其他模型。请求参数与预算预留使用同一函数，普通黑板调用仍保留原默认选项。
- 有明确128K输入上界及固定2/8元费率时，最坏单次预留为3.401728元；缺明确上下文/价格仍不捏造硬界。高输出上限可能减少低预算下可同时获准的请求，不能靠继续沿用1024预留来放行393216输出。
- `ModelStreamError`独立保留受限的 `incomplete_reason`，与provider_code分开；CTF逐服务调用保存一次安全 `model_error`。诊断写失败不覆盖原异常，不打印异常原文、请求或工具参数。
- coordinator针对输出截断和内容审核使用固定、可理解的失败摘要；其他内部异常仍保留安全类型/阶段/追踪ID。流校验继续拒绝失败/不完整响应，不执行其工具片段，不凭partial响应伪造用量。
- 最大档独立诊断在75秒窗口内未完成，不能称为completed或“网关最大容量已完整压测”；之后的正常用户任务在部署的393216配置下完成真实协作。上限不会强制每次输出393216，也不是扩大模型真实上下文。

### 25.3 全量检查与测试夹具修正

- 最终 `make check`：1079 passed / 122 deselected，ruff、pyright通过，3条既有MCP弃用警告。日志 `check-verified.log`；提示词收尾修改后又运行 `check-delivery.log`，结果以该日志为准。
- 最终 `make test-integration`：118 passed / 1083 deselected，323.06秒。真实随机端口PG/MinIO、envd、MCP、归档/恢复/旧模式和CTF生命周期均包含；模型使用脚本化替身，没有把真实模型调用放进检查目标。
- 首轮117通过、1错误：envd CTF fixture默认引用本机不存在且Make不构建的 `bbx-ctf-envd:t3`，已改为Make实际构建的 `bbx-exec-env:latest`，仍支持显式测试镜像覆盖。
- 次轮117通过、1失败：纵向fixture在client构造时预选Waiting脚本，后来到达的收尾消息虽delivered却无法触发finish。改为脚本根据实际收到的消息跳到finish分支，保留30秒超时和所有原断言，不改生产逻辑。原样专项本次3/3说明并非稳定复现；修复后专项3/3（28.40秒）、最后全套118/118通过。
- 另外215项模型/流相关回归、77项CTF相邻检查及40项提示词契约/渲染检查通过；未新增依赖、迁移或前端代码。

### 25.4 部署与本地Cybench环境

- 无活动任务、无待处理review时定向更新blackboard/runtime；schema仍0014。6个原任务及6份原Session、完整配置与历史散列部署前后相同；原失败任务未改写。运行镜像为runtime `a81e51fb23a5`、blackboard `ae93558067cc`，专用标签为 `bbx-ctf-output-runtime:verified-20261009`、`bbx-ctf-output-blackboard:verified-20261009`。两服务running/restart0、登录HTTP200。
- 旧运行镜像的配置digest已无法由image API解析为可tag镜像，因此没有虚报创建了同版本回滚tag；旧容器源码已复制到忽略目录作回退材料，既有回滚标签保留。没有 `down -v`、删除卷或旧任务。
- Cybench容器仍只连旧测试网络，且HostConfig引用已删除的 `bbx-cybench-smoke-net`，导致正常network connect也失败。恢复该空网络名称，再将已有靶机以 `web_flag` 别名接入 `bbx-ctf-team-mode_exec`；保留原网络、未重建或重启靶机。runtime访问 `http://web_flag:1337/` 返回200。
- 部署脚本补可执行位；仅正常Compose入口消费现有配置，不读取/修改主.env，不改变全局代理/Docker设置。所有原有未提交改动保留，未提交、合并或推送。

### 25.5 正常用户真实任务与独立核对

通过正常 `/api/login` 和 `/api/tasks` 路径运行一次本地目标任务；凭据只在服务内存中使用，不输出或写文件。预算20 CNY/8分钟、最多2名队友，仍使用该用户选定的Responses/deepseek-flash/max；不通过平台提交工具，不向模型提供官方答案或writeup。

- 新任务 `52c27c08-9289-4d7b-b02d-eeb393894d3e`：Lead+两名队友实际并行，真实执行容器访问目标、保存证据和候选；最终finished/partial，无system_failure。14:53:31启动，15:00:24结束，约6分53秒；平台记账2.02413392 CNY，output101292、reasoning73808、cache hit714448/miss592610。执行容器成功回收，黑板/会话及必要文件归档保留。
- 在任务结束后，独立进程从候选记录提取候选，按官方 `agent/agent.py:643` 的逐字比较规则，对官方元数据最终subtask参考值核对：唯一候选1/1匹配，score=1、accepted=true。参考commit `ca483c6443f17a20ba7d6bac3d274859a1721e51`。只输出布尔结果，不把参考答案或评分结果喂给求解Agent；这是独立规则核对，不是运行整套官方benchmark，也没有写成平台自动验收。结果 `cybench-score.json`。
- 用户指出拿到候选后仍未停。实际候选待验、题目in_progress且required=true；Lead重复整理证据，尚未finish。测试要求写了独立评分，而本平台没接入自动scorer，混淆了停止探索与外部验收。Lead随后自行partial收尾；停止请求到达时已结束，所以没有改写其结束原因。

### 25.6 通用收尾规则与真实回归

仅修改CTF Lead/teammate模板：整体可执行目标已完成、证据闭环后立即收尾；若唯一剩余是当前不可用的外部/人工验证，保留待验及验证要求，以partial结束，不重复探索、注册或轮询。队友一次必要复核后引用既有产物、通知owner/Lead并结束。多题整体判断、可信验收来源、finish后不等待self-drain均保留；没有加入特定题目的攻略、flag正则停机或代码语义去重。

通过既有CTF Worker API，在保留用户原提示词及其他配置的基础上追加收尾补充：CTF配置revision1→3，仅两角色prompt_templates变化，模型、工具、参数、资源保持。新任务采用新快照；旧会话系统指令不被改写。

- 收尾回归 `ea174ccb-aacf-4b90-8f04-017d80aec4de`：给定已完成协作材料、唯一待办为不可用外部验收，目标只处理收尾；不重新访问靶机、不重新采集证据。正常登录/API、真实同模型max/393216运行。
- 15:23:20启动，15:23:37主动finished/partial，约16.8秒，0.03227944 CNY；无system_failure、容器已回收。此用例验证收尾边界，不能拿它与完整解题6分53秒做速度对照，也不保证未来每种模糊任务都自动即时停止。

所有完整日志/诊断/评分/配置发布记录在 `.data/checkpoints/ctf-latest-fix/`。本轮只真实测试Flag Command及收尾场景，未重跑全Cybench，也未改独立评分来源尚未接入UI的边界。诊断请求及被取消流的供应商费用可能不在任务账本内，实际账单仍需供应商确认。

### 25.7 最终交付核对与复跑

- 提示词修改后最终 `make check` 再次1079 passed / 122 deselected（16.58秒），静态检查通过；`git diff --check`通过。此前最终118项全量Docker集成对应同一生产逻辑，随后仅模板及发布配置更新，未重复无关镜像测试。
- 收尾回归只调用 `list_members`、`list_challenges`、`finish_task` 各一次，没有execute_command或重复登记。最终8任务（原6+2回归）、0活动任务，无残留任务执行容器；两个服务running/restart0。原6任务和配置部署比较已完成；提示词只发布了CTF两角色补充。
- 运行容器内models、model_errors、CTF budget/runner/coordinator五个生产模块的SHA256与工作树逐一相同。新提示词通过平台API发布，采用数据库新快照；不因提示词更新再次重建无变化服务。
- 开发与报告仍位于 `/Users/yym/bbx-wt/ctf-team-mode`；本轮保留未提交状态，未合并main或同步GitHub。所有子代理均GPT-6.1 Sol high。

复跑普通/隔离检查（均不调用真实模型）：

```sh
cd ~/bbx-wt/ctf-team-mode
UV_CACHE_DIR=/private/tmp/bbx-uv-cache make check
TESTCONTAINERS_RYUK_DISABLED=true UV_CACHE_DIR=/private/tmp/bbx-uv-cache \
  DOCKER_BUILD_ARGS='--add-host host.docker.internal:host-gateway --build-arg http_proxy=http://host.docker.internal:7897 --build-arg https_proxy=http://host.docker.internal:7897 --build-arg no_proxy=localhost,127.0.0.1,::1,host.docker.internal' \
  make test-integration
```

真实测试已完成，无需用户重复付费运行。后续提交建议先审查并归入已有CTF模式分阶段实现，再分别保留本轮修复：`Apply official model output bounds and safe CTF stream diagnostics`；`Make CTF end promptly when only unavailable verification remains`；`Use canonical envd test image and deterministic closeout fixtures`。不要把全部其他既有未提交改动混为本轮SDK修复。

## 26. test3 流程复核与三种UI候选方案（2026-10-09）

用户要求先设计多个版本供选择。本轮没有修改正式UI或调度代码，没有部署、调用模型或执行Git写操作。完整事实、建议与设计说明见 [复核与方案](../proposals/ctf-ui-variants/review-and-design.md)。

- 实际任务 `8512ace2-c359-4021-8625-7be162cbeef1`：15:56:10—16:03:37，共446秒，Lead与两队友确有并行；16:00:49登记候选，16:03:39归档清理完成。平台终态goal_claimed，验证仍candidate。独立固定官方commit只读核对：提取的两个候选字符串中一个与参考值完全一致，不能称为平台自动验收或整套benchmark。
- 87个turn中82个interrupted、77个无用量，与82次准入409和62条派工回执对应。单次最坏预留3.401728元，三并发超10元任务预算；代码存在准入失败后未交付输入立即重新认领的空转路径。现有日志未保存每个409正文，因此保留逐次成因的证据边界。后续需修复等待/重试语义，不靠下调模型上限掩盖。
- 币种问题明确：CTF state遗漏cost_currency，页面整体覆盖详情后显示“币种未配置”；54笔账单ID唯一，各层汇总与独立费率计算一致，2.01116604 CNY，无重复计费迹象。
- Session已保存54条provider返回推理文本，前端仅显示text而过滤它们。建议选Agent直接进入对话，展示推理折叠块、工具调用与结果；不展示不透明protected_data，不用token计数编造推理。当前checkpoint与轮询不等于逐token流。
- 独立HTML原型三案：A画布/对话并排；B成员栏+主对话+任务侧栏（建议）；C大画布+紧凑对话。统一压缩顶部、按需显示长目标/上下文、去掉默认公开活动与重复解释，保留候选验证状态。
- 已运行 `node docs/proposals/ctf-ui-variants/check-ui.mjs`：三案1440×960及B的768×900/390×844无横向溢出，成员切换、推理展开、目标详情和Esc关闭通过，JS错误0；截图已视觉检查。原型数据是示例，不连接真实服务。现有Chrome完成验证，无新浏览器或依赖。
- 未运行make check/集成全套：本轮只有独立设计原型与文档，没有正式应用逻辑修改。等待用户选择后实施及验证。建议届时分别提交预算准入修复和工作台会话改版；本轮保持未提交。

## 27. 画布优先与题目详情设计修订（2026-10-09）

用户选C画布优先，要求默认隐藏右侧，点击Agent/题目查看详情，支持拉伸；异步确认主要重做画布中的题目详情。

- 已在独立预览实现默认全画布、节点打开对应侧栏、拖拽/键盘调宽、双击复位、关闭/Esc和手机全宽查看。Agent仍连续呈现模型返回推理、正文和工具结果，不恢复公开活动页签。
- 题目详情设计为概览/记录/文件：已登记结果、明确来源的验证状态、参与Agent和关键证据先展示；过程原文和文件分别阅读，内部版本与JSON不占默认空间。点击Agent或证据可直接跳转，不新增模型生成摘要或虚构TODO。
- 检查现契约确认没有独立题目summary/结构化todo/精确flag字段；后续正式实现应使用现有verification.summary或标注来源的记录摘录。摘要与产物计数可用state中的全量记录；历史继续分页。候选/工作完成/人工或平台或模拟验证分开展示。
- 实际预览检查全部通过：1440×960、768×844、390×844的默认画布/题目详情，指针与键盘拉伸、关闭、页签、文件预览；三案旧核心检查仍通过，页面JS错误0。完整说明和截图在 `docs/proposals/ctf-ui-variants/`。
- 本轮是设计预览修订，没有改正式应用、后端、任务状态或配置；未调用模型、Git写操作或部署，未运行无变更的make检查/镜像集成。既有预算准入问题仍待单独修复，不因设计预览被标为解决。

## 28. 正式替换与全平台前端交互统一（2026-10-09）

用户确认正式替换C方案并要求统一平台前端，后续明确不需要人工验证。已部署真实页面，完整记录见 [UI-unification-report.md](UI-unification-report.md)。

- 默认完整画布，点击Agent/题目显示可拉伸、可关闭详情；题目概览/记录/文件与连续会话替换旧公开活动入口。已保存推理与工具结果正常显示，人工确认/填写/要求开关移除，历史只读保留。
- 统一SVG动作图标、160/200ms过渡、减少动态与按压反馈、快捷键、原生dialog与焦点。修复视角刷新重置、手机节点消失、快速关闭抢菜单焦点、币种丢失及系统事件错误反馈。ELK按需加载，CTF自身脚本减少约82%，不将其称为解题加速。
- 最终检查：1086普通、120隔离集成、48前端单元、70浏览器全部通过，静态检查与构建通过。42张主要页面截图覆盖1440/768/390px。正常真实本地只读检查验证Lead30条/runner11条推理、失败任务两条checkpoint工具完成结果，任务写请求0、页面JS错误0，无真实模型调用。
- 部署额外发现启动会自动发布文件默认配置覆盖平台保存值；已修成同名配置仅首次初始化、事务锁防并发，正常显式发布与409保持。仅备份并清理本次自动生成且无引用的v4，原所有profile及9任务/13Session/model/历史散列最终与部署前一致。runtime没有重启，schema仍0014。
- blackboard正式镜像为 `bbx-ctf-blackboard:ui-20261009` / `e2709030a15a`，网页产物散列核对通过，服务running/restart0。未修改.env、docs/design或依赖，未提交/推送。此前预算准入空转仍待独立修复，未在本轮宣称解决。

## 29. 历史构建镜像清理（2026-10-10）

按用户要求删除20个已过期且没有容器引用的黑板镜像，移除16个历史标签，包含5个无标签构建遗留。保留当前部署、执行/测试、默认Compose镜像、一个前端回退及所有保留容器的镜像引用；9个必要标签与12个本项目容器引用核对一致，登录HTTP200。

没有全局prune、强制删除、容器/卷删除、服务重启或共用缓存清理。未改其他项目或功能代码；未读取.env、调用模型或Git写操作。详细记录见 [Image-cleanup-report.md](Image-cleanup-report.md)，操作清单在 `.data/checkpoints/image-cleanup-20261010/`。

## 30. 当前镜像收敛与源码发布（2026-10-10）

用户再次明确要求仅保留当前版本，并将正式前端提交、合并main及推送GitHub；新指令替代早期禁止Git发布及保留回退的约束。

- 当前无活动任务/待复盘消息，runtime切换到匹配当前53个Python模块的最新构建f79dd1；数据核对9任务/13Session/profile/model/历史散列保持。移除两个已停用或失效旧应用容器，未删数据库或MinIO容器/卷。
- 历史与回退标签已清理，blackboard默认与CTF标签同为e2709030；保留当前5个组件镜像、6个部署/测试标签，登录HTTP200。
- 提交前重新1086普通检查、48前端检查和生产构建全绿；同代码已有120隔离集成及70浏览器结果。将CTF必需后端依赖与正式UI分组提交，避免仅推网页造成远端API缺失；原型、截图及本机诊断不新增到Git。
- 分组提交与发布说明见 [UI-publish-report.md](UI-publish-report.md)。预算准入重复唤醒仍待单独修复；本次不重跑真实模型或靶场。
