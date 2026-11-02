# CTF 求解性能、数据共享与沟通后续改造

日期：2026-10-28。工作树 `platform-performance-audit`，分支 `feature/ctf-performance-followup`，基线 `55044a9`。用户要求继续优化求解速度、数据共享与沟通，并允许必要架构改造；三个 GPT-6 Astra high 子代理讨论、实现并交叉审核，主会话负责取舍、配置界面、完整验证与报告。

**源码交付（2026-11-02）**：本轮实现已从原发布快照独立提交为 `b69669c`，导出的提交源码重新通过2589项普通检查。后续Pi迁移和修复保持独立，现位于 `feature/pi-runtime-migration`；详见[交付报告](PI-runtime-delivery-report.md)。下文保留10-28的发布与未合并记录，当前仍未合并main或推送。

本轮没有新的真实评测运行。上一轮94.82%输入缓存命中、长模型输出与长命令占主要时间，因此本次采用小范围共享读回、通知生命周期、角色运行策略和显式异步命令，未重写MAF或数据库编排层。

## 一、完成内容

### 1. 精确共享读取与通知退休

- 将已有同任务精确记录读取开放为 `get_record(challenge_id, record_id)`，执行与只读复盘均可用；通知引用可一跳定位，不必先列出整题记录。
- 已知派工与权限时append_record无需每次先get_challenge；服务端仍验证owner/collaborator，冲突或身份不明时再刷新。
- 修复新成果通知不能退出恢复保护前缀的问题。只接受可信system的challenge_progress、challenge_verification，继续要求已确认投递、完整历史边界、消费turn完成及没有未知执行；不删除Session历史或修改已保存的预览。
- 异步command_result有独立证明：区分命令原始turn/generation与消息消费turn，命令必须已知完成，通知ACK必须持久化。当前输入、伪来源、未知命令、未确认通知仍保留保护。
- 长记录和复盘中的本人工具结果均可分页读回；不放宽跨成员私有会话权限，也不把跨题观察直接当作另题验收。

### 2. 可选角色运行策略

- 新增 `role_run_options`，Lead与队友各自可覆盖reasoning_effort、max_output_tokens；默认空、继承原基础模型。
- 使用现有Provider目录校验强度，覆盖上限不得超过基础模型有效输出上限；任务更换基础模型时重新验证所有角色策略。
- 每turn解析一次有效模型，请求参数、上下文容量、费用预留、记账、错误诊断和工具能力使用同一副本。既有turn_context记录实际强度、输出额度和上下文门限，便于后续核验。
- 非空策略存于既有profile JSON元数据，不新增表；空策略保持旧行往返摘要。保存请求省略run_options保留原值，显式null/空对象清除当前角色覆盖；编辑提示词不会清空策略。
- 配置页沿用Ant Design，按当前角色紧凑显示两个字段，支持继承；创建CTF任务的强度标为默认强度。旧配置、旧任务、原模型价格与`max`选择保持。
- 降低输出上限可能释放更多输入容量，不能声称一定减少输入费用或提高正确率。本轮未自动降低任何真实配置。

### 3. 显式异步长命令

- 新增白名单工具start_command、cancel_command、read_command_result，默认不启用。新发布配置选择start时需同时开放取消及结果读取，避免产生无可用读回入口的作业。
- start初始等待1秒；完成则返回结果，仍运行则返回command_id。每成员最多一个异步pending作业，envd原子阻止它与新同步命令交叉；旧同步批次保持原并行行为，其他成员独立执行。
- 复用envd现有registry、command_id、参数摘要、boot/generation及进程回收。未新增作业表、独立消息服务或通用资源池。
- runner遇临时final且命令仍pending时保持同turn/session，等待该成员的新消息或完成结果后串行继续。步骤、原截止时间与预算不重置；仅runtime有界查询状态，模型无需反复轮询。
- 结果先进入checkpoint，再由service-only通知操作从持久Session验证身份、boot及终态，生成确定ID短通知，最后保存ACK。旧通知只允许核验后读回既有回执，不能绕过代次创建新通知。
- 未确认完成通知阻止正常结算；临时模型费用占额不会误杀已启动作业。停止仍优先cancel/drain，确认回收后结束和归档；未知结果不重放。重启首版沿用drain/interrupted，不承诺跨重启保活。
- 终态复盘可以只读本人已保存的异步结果，支持分页；不会调用envd、执行、取消、修改任务或读取别人的Session。
- 新start在派发前检查root capability，旧镜像明确返回不支持，不写prepared、不派发。capabilities只出现在根状态响应，成员状态与drain旧合同不变。
- 同步HTTP读取超时改为命令timeout加30秒传输/收尾余量，避免合法1200秒命令被原650秒客户端上限提前截断；连接超时仍10秒，未缩短或扩张实际执行timeout。异步start读取超时30秒。

## 二、复用与互审发现

复用MAF Agent、AgentSession、ChatMiddleware、MessageInjectionMiddleware、现有历史与检查点机制；使用现有邮箱、CAS、预算预留和执行fencing。没有增加依赖、收费摘要Agent、动态模型路由、通知digest或新的工作流平台。

设计合同已先单独提交 `9bdc6e6`。互审与真实链路测试推动修复：

1. command_result跨turn才消费时，必须分开原命令和消费turn证明。
2. 通知写入成功但ACK丢失时，只读回原确定通知，不重新发信或执行命令。
3. 模型预算暂时占额不能被当作停止信号而drain正在运行的作业。
4. runner正常结束后释放Session引用；多次Agent.run不能复用上一次临时final作为后续异常的成功回执。
5. 角色有效模型不能带着其它角色更高额度覆盖重新验证，基础快照保持独立。
6. 新capabilities不能进入严格drain证明；envd保持旧成员状态形状，runtime验证身份与drained后投影既有证明字段。
7. Kali未在PATH提供python/uvicorn，隔离fixture改用镜像实际envd解释器及`sys.executable -m uvicorn`，不修改生产PATH或系统工具链。

## 三、实际检查

| 检查 | 结果 |
| --- | --- |
| make check | **2589 passed，429 deselected**；Ruff/Pyright通过 |
| 黑板服务CTF真实PostgreSQL集成 | **223 passed，323 deselected** |
| 前端check | **155 passed**，ESLint/TypeScript通过 |
| 前端生产构建 | 成功；已有大chunk提示保持 |
| 浏览器配置与创建回归 | **6 passed**，含独立保存、提示词编辑不清策略、清除继承、另一角色草稿与移动端布局 |
| Ant Design CLI | 修改组件检查0项问题，主会话查看了真实渲染截图 |
| Ubuntu候选容器与完整垂直链 | **3 passed，34.50秒** |
| Kali候选容器与完整垂直链 | **3 passed，35.44秒** |

垂直测试使用真实PostgreSQL、邮箱/Session、CtfRunner、候选envd与脚本化MAF；MemoryObjects替代对象存储，不调用真实模型或外部目标。受控长命令在实际收到协调消息后才放行，验证同turn/session先处理消息，再接收完成通知、读回实际结果并最终结算；计数文件只写一次，步骤累计为5。旧同步去重、错误身份、daemon重启、取消进程组及下一代次同样通过。

测试容器均使用随机端口和独立数据，完成后清理。没有用业务数据库跑迁移或写入测试数据。候选镜像为 `bbx-ctf-performance-exec:20261028` 与 `bbx-ctf-performance-kali:20261028`，基于本机已有镜像覆盖envd源码，保留原工具链，不替换latest标签或运行容器。

复现主要命令：

```sh
make check
.venv/bin/pytest -q services/blackboard/tests/test_ctf* -m 'integration and not live'
pnpm --dir web check
pnpm --dir web build
BBX_CTF_TEST_IMAGE=bbx-ctf-performance-exec:20261028 .venv/bin/pytest -q services/envd/tests/test_ctf_container_integration.py services/envd/tests/test_ctf_async_container_integration.py services/agent-runtime/tests/test_ctf_async_real_vertical_integration.py
BBX_CTF_TEST_IMAGE=bbx-ctf-performance-kali:20261028 .venv/bin/pytest -q services/envd/tests/test_ctf_container_integration.py services/envd/tests/test_ctf_async_container_integration.py services/agent-runtime/tests/test_ctf_async_real_vertical_integration.py
```

## 四、验证限度与发布边界

- 本轮已经证明协议能够在长命令期间处理协调消息，不能由此推断真实题目快多少或解出更多题。长模型响应本身仍要到安全边界才消费消息，没有强行截断推理。
- 精确记录工具减少已知引用的寻址步骤，通知保护前缀按已完成证明退役；原始数据库历史仍完整保留，未把所有Session内容删减成摘要。
- 共享文件/软件和按Agent网络归属保持。稳定任务代理/connector、外部资源ready/busy/unknown适配及跨题写证据扩展没有在本轮实现，不能从任意shell输出猜测资源状态或自动重放申请。
- 初次交付未替换运行环境；用户随后授权更新本地，实际发布结果见下节。没有自动启用异步工具、改变真实任务模型参数或续跑靶场，公网环境未变更。
- 只新增兼容JSON配置字段，无数据库迁移。旧profile、旧Session与默认同步行为继续可用。原有人工停止、未知副作用、预算和权限边界保持。
- 实现代码本轮尚未合并或推送；建议分别提交共享读回/退休、角色参数与界面、异步命令生命周期及回归、文档。设计提交与实现分开，真实解题对照另行受控安排。

## 五、本地发布（2026-10-28）

用户授权更新本地后，确认没有活动任务、复盘或整理作业，在空闲屏障内备份并替换blackboard、agent-runtime为 `ctf-performance-20261028`；Ubuntu与Kali的latest及同名版本标签推广为已验证的执行镜像。应用和执行镜像均保留独立回退引用，Postgres、MinIO及原卷未替换，数据库保持0020，无新增迁移。

- 替换前后22张业务表的完整行摘要一致，包含现有challenge_id字段；保留23个任务、191份会话及88640个对象（2182053652字节）。应用环境变量与端口保持原值。
- 显式新增 `ctf v19→v20`、`ctf-task-settings v15→v16`，更新到本轮模板，两角色仅追加get_record；模型、价格、执行资源、原有工具和MCP绑定保持。旧版本和全部任务快照不变。
- role_run_options原值保持，当前为空即继承；异步三工具仍未选中，不自动改变默认同步行为。新界面可以按需设置角色参数、显式启用异步工具。
- 发布后逐表复核，仅排除批准新增的两条配置版本后，所有原有行摘要仍一致。核心应用源码、合同和前端入口共7文件与工作树SHA256相同，两个执行镜像latest指向已测试的候选ID。
- 登录页HTTP200，实际设置接口读回v20、新get_record工具、继承参数及异步未启用状态；新配置前端静态文件校验一致。对旧任务执行只读精确记录查询成功，原任务仍为stopped。
- 没有创建或续跑任何业务任务，没有调用真实模型。私有数据库备份、前后摘要、配置发布与镜像回退记录位于忽略目录 `.data/ctf-performance-release/`。此次只更新本地，源码尚未合并或推送。
