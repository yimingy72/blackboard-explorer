# CTF-close-context-fix · 收尾死锁与上下文耗尽修复

日期：2026-10-13。工作树 `/Users/yym/bbx-wt/ctf-cooperation`，分支 `feature/ctf-cooperation`，原 HEAD `0115a19`。本报告只记录本轮新增修复，不把前序未提交的 cooperation、预算、提示词和恢复改动归入本轮。GPT-6.1 Sol high 分别实现保存层、平台状态机并独立复核，主会话实现协调器、审查协议边界并整合验证。

## 1. 对两份事故报告的审查

- 收尾死锁的根因成立：已结束的 turn 仍留在 `unresolved_checkpoints`，旧写者被 fence 永久拒绝，closing 每 tick 重试。`checkpoint_failed` 覆盖原异常、4xx 被当作未确认写、缺少退避、静默 interrupted 和重试重建对象均有对应代码路径。
- 直接在结算时删屏障还不够：结束 turn 不证明最后 Session 或 usage 已保存。修复必须退役为持久故障，禁止按原 `goal_claimed/partial` 当作完整成功，并保留未知费用和执行环境。
- 上下文首位派工限制可裁范围、阻塞原因不可观测、恢复耗尽无通知及终态残留成立。`320000` 是阈值、输出预留及 20% 余量的计算结果，并非用户配置的 1M 没生效。
- **更正报告 D2/R6**：旧代码原本就接受未达到 60% 但已低于输入上限的结果；60% 不是硬门禁。**更正真实用量推断**：累计输入除以调用数，不能还原最后一次输入，也不能据此证明估计偏差。
- 自动恢复消息原本生成时 `deferred=False`；后来确认、替代指令、上下文再次阻塞、停止/移除可能使其延期或取消，不能用终态快照证明生成代码把它设成 true。当前代码只在显式 resume 重置本轮计数，旧轮历史不足以证明 Lead 指令重置额度。
- 移除成员保留 Session 和记录；“未登记成果丢失”不能等同于数据已删除。尚未主动登记的成果是否可交接，仍依赖会话、工具结果、文件及 Agent 的实际发布行为。未读取真实候选、官方答案或重跑判分，原报告不确定性保留。

## 2. 已实现

### 2.1 收尾和 checkpoint

- `_settle`、幂等 `finish_turn` 和旧 finished 屏障修复，将未确认项移入按 turn/轮次保存的 `checkpoint_failures`。已确认 usage 幂等结清，已发送但无法确认的预留转为 unknown，原 Session revision 和最终回答保留。
- `finalize_close` 保留真实运行回合、未决保存、活跃预算、成员操作及 drain 门禁；本轮保存故障或显式超限使结论变为 `system_failure`，保留风险说明。真实 matching drain proof、closing 和代次核验后，待停止/移除操作也可按失败路径结算。旧 finished 的孤儿成员指针安全清理，不能覆盖另一个运行写者。
- checkpoint 对**相同 payload 和 expected_revision** 最多三次有界保存重试，只有读回能证明未提交时才重发；已提交响应丢失直接对账确认。不重放整个模型或已经执行的工具。
- 4xx 明确拒绝不新建未决屏障；传输/5xx 不确定写才标记失败。标记写失败不会盖住第一现场。终态重试只有权威成员/代次状态及 usage 已确认时才丢本地项，只记录一次安全 WARN；未保存的本地用量或未落库故障继续保留。
- closing 普通重试退避为 1、2、4、8 秒，此后最多每 8 秒；八次尝试或六十秒后升级。先等本地写者退出，再确认远端 drain，才 recover/finalize 失败关闭。取消中的写者不会被再次 cancel 打断 shielded save；无法确认 drain 或持久层不可用时，每 30 秒恢复检查，停止同 checkpoint 高频写入，不伪造成功或销毁环境。
- `TurnBoundary` 的最终保存异常也保留本地 pending 和原结束原因，恢复后仍按 `context_blocked/step_limit` 结算，不误记 completed。interrupted 和收尾故障日志仅含身份、状态、错误类型及答案长度。
- 清理前重新读取权威状态；本轮保存/收尾故障阻挡 detach、归档和销毁，通过 `archive_blocked` 防重复清理报错。显式续跑保留历史故障，但清除本轮 `close_failure`，旧轮故障不污染下一轮。

### 2.2 上下文与成果交接

- 在**实际 provider leaf** 输入达到硬预算 90% 时主动尝试裁剪，固定提示和工具 schema 继续计入预算；60% 仍是理想低水位，低于硬上限即可接受。
- 已确认且不属于 replay 分组的纯文本用户派工，允许原文迁移到持久恢复前缀，完整内容和 message ID 保留并计入同一预算。完整 `in_memory` 不变、跨重启前缀稳定，未知工具组和超大新输入继续保护。
- 阻塞保存实际 `branch/eligible/cut/remaining/input_limit/protected_hits`，输入 ID 过滤 null/空值。回执不再一律声称工具配对不可拆；末次真实单次 usage 与估计值并列，复用账本输入计算，包含 Anthropic/Bedrock 的缓存创建/读取口径；没有权威字段时为 None。
- 使用协议已有的 `record_observation(kind="turn_context")` 保存阻塞诊断，不新增事件枚举。实际平台不接受 `kind="context_blocked"`，已在整合时修正并增加 HTTP 贯通测试。
- 平台保留逐 turn `context_blocked_history`；恢复发放、禁用原因、耗尽、延期、取消均有结构化审计。耗尽通知 Lead 且去重；Lead 自身通知暂存，避免自唤醒循环。恢复额度仍每成员每轮一次，显式新指令不擦额度。
- 终态清空活动 `context_blocked_members`，保留净化后的历史诊断；幂等 closed 修复也不重开任务。Lead 停止或重派前先盘点在办成果，队友形成阶段成果后先登记与发布，再继续调查。

### 2.3 MAF 复用与共享文件

实查安装的 MAF core 1.19.0 的 `CompactionProvider`、`ContextWindowCompactionStrategy`、`TruncationStrategy` 和公开 `annotate_message_groups/GROUP_ANNOTATION_KEY`。继续复用 Agent、AgentSession、InMemoryHistoryProvider、注入中间件、原生工具循环、消息分组和 ResponseStream。没有新增依赖或升级 MAF，也没有再造工具调用配对器；标准库 dataclass 只记录协调器重试状态。

没有同时装配第二套原生 compaction：其旧工具驱逐/摘要会改变已发送前缀，与本平台稳定预览、持久 cutoff、派工 receipt 和预算门禁冲突。此次只补平台需要的安全迁移与有界状态机；不引入付费摘要调用或新安全余量配置。

共享合同只同步前一任务已审查并合入 main 的 `ctf.coordination.reminded` 枚举及守卫/HTTP 回归，避免这个较旧 dirty 工作树重新部署时退回事件 500。重新生成 CtfEvent schema、OpenAPI 和前端类型；不是这两份报告新增事件类型。没有改 `docs/design/`、数据库 schema、运行镜像配方、模型、预算或任务阈值。

## 3. 验证命令

```sh
cd /Users/yym/bbx-wt/ctf-cooperation
UV_CACHE_DIR=/private/tmp/bbx-uv-cache make check
UV_CACHE_DIR=/private/tmp/bbx-uv-cache TESTCONTAINERS_RYUK_DISABLED=true \
  make -o image-exec-env -o image-egress-proxy -o image-eval-env \
  -o image-agent-runtime test-integration
```

集成使用已构建且核对源码的本地 runtime 和既有 envd/proxy/eval 镜像，随机端口 testcontainers。`-o` 只跳过重复构建步骤，全部非 live 集成测试仍执行，不是缩小测试范围。完整检查、首次失败与本地发布结果见下方补充记录。

## 4. 偏差与待决

- 已结束旧写者永远不能再保存，故退役为失败并保留环境；不直接删除屏障后宣称成功。原 proposal 关于“快照丢失一直 closing”的文字补充为有 drain 证明时失败终态、无证明时低频等待；设计正本未修改。
- 八组事故超出量 770–4150 token 都有相同大小的**合成可裁边界**回归；不是原事故消息重放。原分支诊断已丢失，不能保证八个历史回合全部能救回或推定真实末次输入。
- 有些合法旧恢复回合会省略旧模型视图，完整 Session 保留且未知工具不重放；这沿用前序明确的新 admission 特许。不能声称所有旧内容仍在可见前缀。
- 新工具预览决定并非每次 dispatch 前都单独持久化；原稳定额度及同轮确定性重算保持。本轮修复压缩 epoch、派工迁移的发送前持久化，不额外扩大提交协议。
- review 的独立恢复状态机未扩展成 execution 收尾；此轮闭环测试针对两报告的 execution 故障。
- 系统无法从私人思考或未登记路径自动认定真实候选及必要产物。提示词要求及时发布；停止后原 Session、工具记录及文件保留，但不能自动伪造候选或 accepted。

## 5. 建议提交划分

先整理前序未提交修改，再按本轮相对前序基线拆分，不对整个 dirty diff 一次提交：

1. `fix(ctf): retire failed checkpoints without losing usage`：平台结算/关闭/成员操作、保存层同 payload 重试及直接回归。
2. `fix(ctf): bound close retries and preserve failed runtime state`：协调器退避/取消/超限、supervisor 清理、resume 隔离及 PG/HTTP 贯通回归。
3. `fix(ctf): preserve active inputs during context compaction`：安全派工迁移、主动压缩、真实原因及单次 usage、恢复通知/历史及回归。
4. `docs(ctf): clarify context limits and blocked-work handoff`：两角色通用模板、proposal/部署说明及本报告。

修复与部署阶段没有执行 Git 写操作；用户随后授权的提交与推送见第 7 节。没有重跑用户任务或调用真实模型/靶机，无需额外沙箱授权。

## 6. 最终检查和本地发布

- 最终 `make check`：**1359 passed / 232 deselected**，26.08 秒，283 个文件格式、Ruff、Pyright 通过；三条既有 MCP sampling 弃用提示。
- 完整集成命令：**228 passed / 1363 deselected**，354.44 秒。包括真实 envd/运行镜像、PostgreSQL、HTTP/SSE、既有归档/MinIO测试及新收尾、上下文回归；所有模型均为脚本客户端。
- 新增回归覆盖 finished+未决的有限 tick 失败收尾、matching drain 后待停止/移除、旧孤儿指针、重复取消中的 shielded save、真正运行未决阻挡、无 drain/DB 不可用不伪终态、已确认/未知费用、保存响应丢失、marker 失败、本地 pending 保留、恢复后原结束原因、续跑轮次隔离、90% 预压、派工原文/ID/重启、八种小溢出量、六类压缩分支、末次单次用量和恢复通知历史。
- 两个新 HTTP 贯通用例使用真实 PostgreSQL/ASGI/CtfClient/运行器：旧 finished 屏障最终仅一次 recover/finalize，候选费用和最终回答保留；上下文阻塞经生产 observation 校验、事件注册与 `/api/evidence` 读回，与 Session/回执一致。此两个用例的对象后端是既有 `MemoryObjects` 替身，不能将它们单独描述为真实 MinIO 验证。
- 初次完整集成 **227 passed / 1 failed**：旧纵向测试在 drain 刚确认后立即 tick，未推进新增退避时钟；更正测试时间模型，不修改关闭证明门禁。定向 2 项通过后完整 228 项重跑全绿。普通阶段 import/format 已修正；八种溢出合成用例初次使用的合法可裁前缀不足以覆盖最大溢出，四项正确阻挡，将 fixture 改为确有足够空间后八项通过，没有放宽生产上限。
- 构建只使用已验证的本地基础镜像，`--network=none` 打包源码与模板，不装依赖；初次把本地 sha256 ID 用作 Dockerfile FROM 被当成远端仓库，失败后改为明确本地 tag，最终两镜像成功。构建未读取 `.env`。常用最新 tag 为 `bbx-agent-runtime:latest` 和 `bbx-ctf-blackboard:latest`，另保留本轮任务 tag。
- runtime 镜像：`35b6e6816f45ea8379d784d09b58e1a267813513a7bf06d9a26ba1e85078651c`；blackboard 镜像：`28ccb5ce094ceac9d97bb9e5af84ccc1f4438099db44d6f67ad6c57885bbda84`。逐文件验证 runtime **68**、blackboard **41** 个源码/合同/模板与工作树相同，当前运行容器也逐一匹配。
- 发布前确认 **0 活动任务、0 running turn**，暂停两个应用容器、更新源码后启动，原容器 ID、环境和服务绑定保留；没有重建数据库、MinIO、任务数据卷或靶机。登录 HTTP **200**，两个服务 running、restartCount **0**。
- **9 个任务、56 个 Session、278 个 turn、448 条消息、129 个题目、641 条记录、24179 条事件、13 条 task run** 的部署前后数量/摘要完全相同；Session 摘要使用 revision/updated_at 元数据。前端 index SHA256 仍为 `e1e29706da0d365de4bb7be44ad057a971ff0df38a366d101e709c06cc575f5d`，没有回退已部署明亮环绕 UI。
- CTF Worker 设置 revision **11 → 13**，通过带 expected_revision 的原 API 只追加两条通用交接/及时发布规则，保留当前用户模板其他内容及模型、工具、价格、资源；原 19 个 profile 保留，新增 2 个快照。旧任务没有重跑，既有 opening instructions 不重写。

本地测试入口仍为 <http://127.0.0.1:58000/login>。源码备份、检查/首次失败日志、镜像与部署指纹存放在 `.data/ctf-reliability-fixes/`。没有复跑付费 31 题，不承诺解决数量、缓存命中百分比或原事故八个回合的实际恢复结果；这些需要新的同口径运行验证。

## 7. 用户授权的提交与推送

2026-10-13 用户要求“提交推送”。本工作树累计的前序 CTF 代码与本次修复相互依赖，不能只提交最新回归而漏掉运行时模块。范围审查共 92 个文件，无凭据、真实候选、`.env`、缓存目录、设计正本或其他工作树的 UI 实现。

按当前可完整验证的边界调整第 5 节的建议：

1. `ded93ee` — `Harden CTF cooperation, context recovery, and closing`：73 个后端、测试和生成契约文件，包含前序协作/预算/上下文恢复与本轮收尾修复。
2. `1f4341c` — `Clarify CTF evidence handoff and bounded recovery`：两角色提示词。
3. `Document CTF reliability fixes and verification`：17 个说明、任务与报告文件。

提交前再次执行 `make check`：1359 passed / 232 deselected，24.63 秒，格式、Ruff、Pyright 通过；完整 228 项集成结果仍对应同一生产代码。合并及推送记录写入 main 的 HANDOFF 第 0 节。其他工作树保持原分支与未提交内容，审计/镜像材料保留在本工作树，Git 发布不重启服务。
