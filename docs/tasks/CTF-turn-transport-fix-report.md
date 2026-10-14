# CTF-turn-transport-fix · 回合结算传输故障修复

日期：2026-10-13 开始，2026-10-14 收尾。工作树 `/Users/yym/bbx-wt/ctf-cooperation`，分支 `feature/ctf-cooperation`，起点 `c1bd222`。本轮依据用户提供的 [事故报告](CTF-turn-transport-failure-report.md)，修复主故障并扩查相关路径；GPT-6.1 Sol high 分别实现运行时、平台协议及广度审计，主会话实现协调器、整合并发布。没有修改其他工作树的在办内容。

## 1. 对事故报告的核实

根因成立：回合主体退出后的 `settle()` 发起黑板读取，`httpx.RemoteProtocolError` 逃出异常边界；协调器看到 future 异常就请求系统失败，把一次控制链抖动放大成整队中断。此路径与模型 HTTPX2、上下文门限和模型 provider transient 是不同问题，不能把异常类型塞进模型重试表了事。

报告的结算调用链与本机源码相符。连接为何提前关闭没有足够服务端证据，不推断为 DeepSeek、网关、代理、连接池或具体接口导致。事故时生效的 1M 模型窗口/485427 输入预算不需要改动；本轮没有改阈值、模型、单价或配置版本。

**修正进度丢失的表述**：空 `final_answer` 不等于会话或全部模型进度被删除。对 `7408d5bc` 只读元数据核实，9 个 Session 的保存消息数分别为 134、216、117、90、150、45、95、122、54，合计 **1023 条**，revision 为 118–536。已持久历史和共享记录保留；尚未收到、确认或持久化的片段不能据此恢复或宣称完整。没有读取实际会话正文、候选或答案。

## 2. 主故障修复

- **D1 结算边界**：CtfClient 的幂等读取最多三次；保留完整原 member/turn/reason/answer 为 pending settlement，按 1/2 秒退避，三恢复周期后明确升级并保留状态。未知 `finish_turn` 只读回 exact turn/member/gen/结果，不重新提交或重跑原生 MAF 模型/工具循环。确定错误仍失败。
- **D2 协调器分类**：单个回合控制瞬断以本回合保存/结算或延期处理，其他 future 保留；持续控制故障依旧有界失败。真正的代码/4xx/本地协议错误继续走失败语义。关闭确认前保留失败来源，避免请求关闭未提交时把异常 future 先遗忘。
- **D3 收尾原因**：首次 closing 在任务锁内原子记录现有 `ctf.coordination.reminded`，包括 `cancellation_requested`、原因、操作 ID、当前 execution 回合/成员/代次及数量。重复请求不重复事件，事件失败回滚；它说明请求取消，而不是伪造已取消或丢失证明。
- **D4 诊断**：失败合同及安全日志加入有界 task/member/turn/generation。旧四项诊断兼容，原栈只用于受控日志；不复制异常原文、请求、凭据、工具结果或模型正文。
- **D5 类型边界**：复用现有 HTTPX 类型和共享薄控制异常 predicate，区分黑板/envd 控制与模型 HTTPX2。LocalProtocol/UnsupportedProtocol、4xx 不被当瞬态重放；没有全局写请求自动重试或依赖升级。
- **D6 登记入口**：`append_record` 与 `record_candidate` 权限保持。工具说明/拒绝反馈明确向 Lead 申请协作者，或向 owner 发消息；没有改提示词/profile，没有让未授权成员绕过同样的门禁。原日志不能证明具体哪个成员缺少协作登记。

## 3. 扩查并修复的相关问题

详见 [控制链代码扩查报告](CTF-control-audit-report.md)。重点包括：取消信号被失败观测覆盖、SDK 连接关闭盖住结果、启动/关闭应答丢失、envd 已注册但黑板绑定未确认、停止与新注册竞态、复盘领取 token/过期重取、复盘结算永久占位、旧复盘费用串到新回合、普通启动/调度及最终回执补偿。只补确证触发链，不按测试题目内容调整行为。

执行登记仍须真实 boot/member/gen；DB 无绑定不能声称远端已 drain，旧未排空/newer/foreign 身份仍拒绝。复盘按明确已退出的目标 writer 及已确认 Session/用量/观测收尾，不触碰其他复盘；保存屏障不放行时低频保留。历史用量仅从持久 Session 读取并记回原回合，不信任客户端上传任意旧账单。

普通最终回执沿用原生 Session.state，带 task/run/agent-run/derive-round 证据；原回执已确认时不重复写，原运行仍有效时使用现有服务的 task 锁/CAS 门禁补偿。清扫、重启恢复和终态 drain 优先匹配证据，不能用 generic heartbeat/runtime_restart 覆盖已完成业务结果。

## 4. MAF、合同和范围

已核对安装的 MAF core 1.19.0、原生 Agent/AgentSession/History/ResponseStream/工具循环及本机 SDK 关闭实现。没有另造会话库、模型调度器、摘要模型、工具回放器或可调余量策略。恢复复用既有 pending、Session、outbox、CAS、代次、registry 与 readback。

共享修改为 `CtfFailureDiagnostic` 四个可选元数据字段及自动生成的 CtfFailureDiagnostic/CtfConclusion/CtfTaskView JSON Schema。普通 API 的已有 FinishBody 增加可选任务/Agent 身份门禁，OpenAPI/前端类型自动生成；没有更改数据库 schema、事件枚举、前端样式、执行镜像/权限或三份 `docs/design/` 正本。

## 5. 验证

```sh
cd /Users/yym/bbx-wt/ctf-cooperation
UV_CACHE_DIR=/private/tmp/bbx-uv-cache make check
UV_CACHE_DIR=/private/tmp/bbx-uv-cache TESTCONTAINERS_RYUK_DISABLED=true \
  make -o image-exec-env -o image-egress-proxy -o image-eval-env \
  -o image-agent-runtime test-integration
```

专项通过数存在重叠，不相加充当总数。真实 HTTP 故障注入位于 ASGITransport：先实际处理 API，再丢响应或在发送前断连；它是 `httpx.RemoteProtocolError`，不是用 5xx 假充。原生 MAF 使用脚本客户端，不发真实模型请求。

普通检查：**1485 passed / 291 deselected，28.71 秒**；308 个文件格式、Ruff、Pyright 全绿。三条既有 MCP sampling 弃用提示。

完整隔离集成共 **287 项**：首次全套 **286 passed / 1 failed / 1489 deselected，474.74 秒**。失败仅为原运行镜像启动检查在固定十秒窗口内未观察到 advisory lock，没有原协议用例失败。镜像源码哈希匹配、`bbx_runtime.server` 无网络导入成功；原失败项同一源码/镜像单独复核 **1 passed，20.92 秒**，锁互斥、SIGTERM 零退出和释放均通过。没有改生产逻辑或放宽测试等待来过关；无法仅凭该一次超时断言 Docker/资源成因。所有 287 项均取得通过结果，但不能把首次全套命令描述成退出码 0。复核没有再次跑整套或收费模型。

集成涵盖真实 PostgreSQL、HTTP/SSE、MinIO、envd、运行镜像/实例锁与原生命周期；新 native MAF/HTTP 回归在两个成员运行中分别丢结算 GET/已提交 finish POST 响应，证明另一成员不中断、原 answer/reason/费用/checkpoint 保留、每模型仅一次、已提交 finish POST 仅一次。新专项对象存储为 MemoryObjects、排空为 SimulatedDrain，不能单独把它们描述为真实进程排空或真实 MinIO；既有真实 envd 集成另作验证。

首次统一普通测试 **1464 passed / 8 failed**：一项仍期待对已 running 状态重复 start，七项依赖旧整轮 retry。保留预算测试，修正 provider fake 使用真实 HTTPX2 类型、控制和提供者故障边界；明确不重放 provider、同 request_id 同 receipt 补账只计一次、unsent 零调用/零费用且 interrupted、没有 reservation 也不盲重试。Fake finish 提供真实终态/身份/账本证据，不能继续用空 `{}` 掩盖协议。不是删除或放松未知预算保护。新参数接口导致的 Python kwargs 推断、fixture 动态成员与 import 等静态问题均修复后统一通过；新增独立 review 发现的 stop/恢复/取消/派工竞态也已完成闭环回归。

## 6. 发布与数据保护

发布前的只读指纹：**0 活动任务、0 running turn**，10 个任务、65 个 Session、67 个成员、305 个 turn、499 条消息、160 个题目、786 条记录、14 条 task run、24 个 profile、27611 条事件。后续发布必须保持这些历史及当前前端；不重跑或自动续跑失败任务，不擅自回冲 17.44 CNY 未知预留。

2026-10-14 已完成本地发布：

- 两镜像从前次已验证基础镜像 `--network=none` 增量打包，不安装依赖或下载模型。runtime **`5a951bab45b6f9c2549b1ee6e82d60853e919ccd37ac63ddb5233509af2c6f4c`**，blackboard **`0c29ee3ab4cc524c2c7d0642fa39c2c84f216d105ffbba7d632cbd61b518246a`**；常用最新标签及 `bbx-ctf-transport-{runtime,blackboard}:20261014` 保留。
- 镜像及实际运行容器与本工作树逐文件 SHA256 相同，runtime **68**、blackboard **43** 个源码/合同/模板文件无差异。只更新 **15 个 runtime、10 个 blackboard** 载荷文件，原容器、环境、数据库、MinIO、数据卷和用户靶机保持。
- 紧邻暂停前再核验 0 活动任务/0 running turn。暂停两个应用服务、保存旧源码、更新后启动；登录 <http://127.0.0.1:58000/login> HTTP **200**，两个应用 running、restartCount **0**。
- 发布前后上述任务、会话元数据、成员、回合、消息、题目、记录、事件、task run 的数量及摘要 **完全相同**；所有 profile 行及任务其余控制字段摘要相同，没有新增配置版本、费用修正或事件。原未知预留没有清零，旧失败任务未自动续跑。
- 正式前端 index SHA256 仍为 `e1e29706da0d365de4bb7be44ad057a971ff0df38a366d101e709c06cc575f5d`，明亮环绕界面未回退。测试临时容器均由 fixture 清理，用户既有 Cybench/其他服务未关闭。

源码备份、故障/检查/镜像日志及发布指纹存于本工作树 `.data/ctf-turn-transport/`，不提交、不含 `.env` 或明文凭据。本次不需要用户另执行构建或沙箱授权命令。

## 7. 偏差、待决和建议提交

- API optional proof 字段与 Session 最终回执是实现层最小补偿，不修改通用求解/CTF完成规则；三份正本未改。需要统一设计索引时可据扩查报告同步，不增加当前任务不需要的抽象。
- 原运行的公网上游/内网控制断连源仍未知。可证明故障注入后单个控制抖动被恢复，不能承诺任意永久故障下都能继续工作。
- 没有真实模型、靶机攻击、判分、官方答案读取或新的求解/缓存效果样本；普通与隔离集成只验证恢复协议、权限、账本和生命周期。
- 暂未提交/合并/推送。建议按四组：`fix(ctf): preserve turns across control transport failures`（运行时/协调器/观测/控制客户端）；`fix(ctf): reconcile execution and review lifecycle safely`（登记/停止/复盘/账本及生成合同）；`fix(runtime): retain original final receipts across control drops`（普通启动/调度/最终回执及 API 门禁）；`docs: record transport fixes and control lifecycle audit`（报告与部署说明）。回归与对应生产文件一同提交，正文按仓库约定注明任务 `CTF-turn-transport-fix`。
