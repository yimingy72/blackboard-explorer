# CTF-cooperation · 协作调度性能报告

日期：2026-10-11。工作树 `/Users/yym/bbx-wt/ctf-cooperation`，分支 `feature/ctf-cooperation`，基于 main `0115a19`。前端细节修改保留于 `ui-rebuild`，未复制到本分支。三个子代理统一 GPT-6.1 Sol high，分别负责预算准入、轻量调度和独立纵向验证；主会话审核、整合与发布核对。

## 1. 根因与实现

### 1.1 暂时占额不再变成重复中断

历史报告第 26 节记录了 87 个 turn 中 82 个 interrupted、77 个无用量及大量派工回执。原实现中，`claim_turn` 只检查剩余额度大于零，真正进入模型前才申请完整预留。预留不足返回通用 409，Runner 把它当中断结算，原派工仍在队列，下一次扫描立即再认领，形成空回合与无效通知。旧日志缺少逐次 409 正文，不能断言历史全部中断均为此原因。

修复后：

- 服务端明确区分 `ctf_budget_wait` 与 `ctf_budget_exhausted`。临时不足保持当前 turn、generation 与 Session，以 0.2 秒到最多 2 秒的有界退避等待结算；等待不进入模型、不增加模型步数、不提前交付消息、不结算回执。
- 真正不足以容纳下一次调用时预算收尾；正常 claim 不足返回无任务，不再当作系统故障。只有明确预算错误码才进入等待，停止、旧 generation 和其他 409 不盲重试。
- Watchdog 以已用费用、未知费用保守占额及时间判断终局；可释放的在途预留不再直接触发结束。未知账单仍占额，不把断流当作免费。
- 已有预留的重试、实际发送模型前都重新核验停止、环境替换和时间，去掉自身预留再检查，避免双扣。收尾后不能凭先前的预留继续发起调用。
- 原真实中断/失败的唯一结果通知、消息恢复、费用幂等和 generation 隔离保留。最大输出仍为对应 DeepSeek 型号的 393216，没有通过降低预留或放宽预算限制来提高并发。
- 交叉审核补齐平台外层重试边界：模型传输失败后先读取权威控制状态，本 turn 若仍有 `sent` 未结算预留，则不发第二次潜在收费调用，原关闭/恢复流程保留其未知费用。查不到可靠状态也不重试；已结算或尚未发送时保留原有有界重试。这里处理平台外层的再次调用，不改供应商 SDK 内部重试配置或声称掌握外部最终账单。

### 1.2 调度只读取所需状态

- 新增仅 service 身份可访问的 `GET /api/tasks/{id}/ctf/runtime-state`，返回任务控制、成员、当前 running turn 和待投递接收者。运行、恢复、停止、执行和只读复盘使用此接口。
- 现有 `/ctf/state` 与 `CtfClient.state` 完整历史语义保持；用户和 Agent 不能访问内部轻量状态。任务记录、消息与历史 turn 不因优化删除。
- 只对有 queued 消息的成员请求 claim；事务内 claim 与 generation 检查仍是权威。快照仅用于减少无效请求，新消息由下一次 0.2 秒补偿扫描捕获。
- 300 秒消息租约保持，成功续租间隔改为 60 秒。更换 turn/generation 立即续租；恢复、分离和关闭清理本地到期状态。长模型调用不会阻止续租。
- `list_members/list_team` 直接读取名册，不再先读取所有题目、记录和消息；返回成员字段及 `agent_id` 别名保持。
- 实际模型工具路由也改用轻量状态，避免服务层名册优化没有覆盖 HTTP 调用链；原工具许可、旧 generation 拒绝和成员私有字段过滤保持，并用真实 HTTP 回归证明不会读取完整 state。

### 1.3 长会话视图长度只计算一次

原历史裁剪每移除一段便对剩余消息重新序列化并求和，最坏为平方开销。现在每条消息计算一次长度，再递减已移除段长度。保留原切片规则、工具边界、最新输入及完整持久 Session，不增加摘要模型、不改变提示词或上下文窗口。

## 2. 验证与可测收益

| 场景 | 原方式 / 参考值 | 修改后实测 |
| --- | --- | --- |
| 三队友、只够两次最坏预留 | 旧代码会对第三派工反复中断/重认领 | 两人并行；第三只建立 1 个 turn，释放前 0 次模型调用；释放后原 turn 继续 |
| 在相同 held workers 上执行 6 次调度读取/续租 | 实际执行旧 HTTP 模式：6 次全量 state、18 次 renew | 6 次轻量 state、0 次全量 state、0 次重复 renew（首次续租已完成） |
| 两名 active 空邮箱成员，100 次扫描 | 旧循环按代码会执行 200 次空 claim | 0 次 claim / 0 次 runtime 写请求 |
| 同一 1001 条合成消息的裁剪 | 251001 次 `Message.to_dict`，本机约 2.776 秒 | 1001 次，约 0.012 秒；不是服务商缓存命中率或真实解题耗时 |
| 长任务的租约续期 | 原先每 tick 请求 | 假时钟 0–360 秒保持未完成 future；按 60 秒续期，换代立即续期 |

三队友纵向用例使用真实随机端口 PostgreSQL、ASGI HTTP、MAF Agent/Session/流式接口和脚本化模型。三个派工最终均来自原 turn、completed 且最终回答非空；每条消息在数据库交付事件、持久历史和模型输入中各出现一次。真实 `bill_usage` 请求按同 ID 重放后，总费用仍为 0.0006（测试费率）。永久不足用例在 0 次模型调用下以一次 turn 正确进入 budget_exhausted/closed。

这些计数证明减少调度开销和错误回合，不能当作真实 CTF 解题速度、成功率或费用下降百分比。没有增加并发名额；可实际获准的并发量仍受用户预算和单次最坏预留限制。

### 已运行的检查

- `UV_CACHE_DIR=/private/tmp/bbx-uv-cache make check`：最终 Ruff、Pyright 通过，**1125 passed / 136 deselected**，16.62 秒，3 条既有 MCP 弃用警告。日志 `.data/ctf-cooperation/check-final.log`；早期 1116 项结果保留于 `check.log`。
- 新预算与原 CTF 数据库事务检查：31 项通过；离线预算及相邻检查 70 项通过。
- 新轻量调度及相关离线检查：88 项通过；补充名册等价断言后的新增 9 项通过。
- 独立纵向检查：新增 2 项通过（13.63 秒）；与原 vertical 合跑 4 项通过（16.68 秒），Ruff/Pyright 通过。命令、参考基线定义和计数见 `.data/ctf-cooperation/cooperation-validation.txt`。
- 交叉复核补充的实际工具路由检查 21 项通过；模型重试边界新增 7 项及相邻检查共 62 项通过。第一遍完整集成 **132 passed**，251.39 秒；两处补充修正后已重新打包并执行最终全套。
- 最终完整隔离集成：**132 passed / 1129 deselected**，249.00 秒（4 分 9 秒），exit 0。包含新并发等待/费用幂等、原 CTF 消息/执行/平台/生命周期、旧黑板、归档恢复和 runtime 镜像检查。日志 `.data/ctf-cooperation/integration-final.log`。最终镜像源码散列与接受检查的代码一致。
- OpenAPI 和 TypeScript API 声明已同步；`git diff --check` 通过。没有新增依赖、数据库迁移或前端行为修改。

完整集成首次执行标准 `make test-integration`，在重建未修改的 exec-env 时因 `host.docker.internal:7897` 代理连接中断而失败，尚未进入 pytest；原始日志保留于 `integration-build-failed.log`。没有改 Docker 全局配置或代理。依赖与执行环境源码未变，改为从已有镜像以 `--network=none` 打包本轮 runtime 源码，再复用已存在的 exec-env、egress-proxy、eval-env 镜像运行全部集成用例：

```sh
TESTCONTAINERS_RYUK_DISABLED=true UV_CACHE_DIR=/private/tmp/bbx-uv-cache \
  make -o image-exec-env -o image-egress-proxy -o image-eval-env \
  -o image-agent-runtime test-integration
```

部署候选 runtime 的全部 53 个 Python 文件、blackboard 的全部 28 个 Python 文件均与本分支逐个 SHA256 一致；blackboard 仅增量更新后端文件，保留上一轮已部署前端 index `4a704f801b3fde1e4346acd75ea2c0ee2453de13d81c31a1918e3d9fc6f7f9b8`。核对记录 `image-source-check.json`，没有用 main 的旧前端覆盖用户当前页面。

## 3. 偏差与待决

- 本轮保持 CTF 设计的单 Session 写者、可靠 mailbox、预算及停止契约，没有修改 `docs/design/`。内部轻量 HTTP 端点属于实现细化；公共历史读取保持。
- 暂时等待目前使用最高 2 秒退避，仍有少量准入查询；没有引入额外消息中间件或公平调度队列。多成员持续竞争时不承诺严格 FIFO。
- 没有调用真实模型、操作靶机或重跑用户任务；没有读取/修改 .env、模型凭据、已有提示词或模型配置。普通检查和容器检查均使用假模型。
- 既有模型推理速度、工具执行速度和前端真实流式显示不在本轮范围；不承诺固定解题加速倍数。

## 4. 建议的提交划分

1. `fix(ctf): wait for budget admission without restarting turns`：预算错误码、等待与停止边界、客户端错误解析及对应测试。
2. `perf(ctf): trim scheduling snapshots and throttle lease renewal`：轻量端点、ready 调度、续租、名册及历史长度优化、API 声明与回归。
3. `test(ctf): verify concurrent admission and scheduling overhead`：独立纵向验证、任务说明和中文报告。

本分支尚未提交、合并或推送；前端细节分支保持原状。

## 5. 本地部署与数据核对

已更新正式本地入口 <http://127.0.0.1:58000/login>。部署前确认 0 活动任务、0 running turn、0 待处理复盘；按顺序停止旧 runtime、更新 blackboard、等待登录 HTTP 200 后再启动新 runtime。没有启动或续跑用户任务。

- 本地镜像：runtime `13887a14eb7c5d258732ff2b41f314a7ffb5d6cd33b398f5218ff8e6131ac9e0`；blackboard `a342a203ef5938607363a6f3efcdc29be29e21881812df0c7ac74fbde1c886f0`。
- 两个运行容器的 53/28 个 Python 源文件再次逐一核对通过；均 running、restartCount 0、启动日志 ERROR/Traceback 计数 0。没有用仅镜像打包成功替代实际运行核验。
- 部署前后 tasks、task_runs、agent_sessions、agent_profiles、platform_configs、app_settings、CTF 成员/轮次/消息/题目/记录及 events 的数量和全表摘要完全一致。schema 保持 0014，登录 HTTP 200，前端 index 散列保持不变。
- Compose 提示既有旧命名卷标签不匹配；本轮仅重建两个无数据卷的应用服务，未操作 PostgreSQL/MinIO 容器或任何数据卷。原数据库与归档保持。
- 未读写 .env；Compose 仅按原部署入口正常消费现有配置，未回显凭据。未新增回退标签或改其他 Docker 项目。

核对材料在忽略目录 `.data/ctf-cooperation/`：`before-deploy.json` / `after-deploy.json`、`image-source-check.json`、`deployment-health.json`、`deployment.log`。当前无需用户执行构建命令；下一次新任务或显式续跑会使用新调度逻辑。
