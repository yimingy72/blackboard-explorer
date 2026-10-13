# CTF-batch-hardening · 评测报告复核与可靠性优化

日期：2026-10-12。来源为用户提供的 `bbx-cybench-smoke/deploy/batch/ANALYSIS.md`、任务 `3e4d2970-dbd9-493f-a6af-ef398e33f21a` 的落盘快照，以及本机数据库只读聚合和受控错误日志。原评测报告、任务状态、账本和判分文件没有改写。

工作树 `/Users/yym/bbx-wt/ctf-cooperation`，分支 `feature/ctf-cooperation`。前两轮调度与提示词改动仍在同分支；GPT-6.1 Sol high 子代理分别实施预算/模型边界、控制链/静默协调、报告索引，主会话复核并接 API。

## 1. 审核结论

报告发现了真实可靠性问题，但以下统计或因果结论需要更正。

| 原报告说法 | 已核实的情况 |
| --- | --- |
| 11 道 completed、21 道 in_progress 均无记录 | 31 题中 completed 10、in_progress 21；其中 20 题无逐题记录，LockTalk 的 in_progress 题已有 2 条记录。外部匹配 11、完成声明 10、可信 accepted 0 是三个口径。 |
| record_candidate 全程 0 次 | 按本任务 `events.type=tool_call.recorded` 的 `payload.tool` 查询，实际调用 13 次；持久候选 11 条，不能从错误计数否认候选已登记。 |
| 50 个证据均无法归因 | 32/50 个登记产物已通过题目记录 artifact_refs 关联到 11 题，18 个未被逐题记录引用。已有关系没有出现在旧报告中。 |
| 实付 ¥30.20 | `usage.cost=30.19506708` 是配置价表的 token 估算，未取得供应商账单，不能称为实付。 |
| 30.20+20×3.401728≥100 | 合计 98.22962708，未达到 100；但剩余 1.77037292 不够下一次 3.401728 的最坏预留，因此准入拒绝。 |
| 单次 provider RST 关闭第一轮 | 真实受控日志定位为 coordinator → runtime_state → 黑板 HTTP GET 的 RemoteProtocolError，属于控制链；不能归因模型 provider。 |
| 平均每成员回合约 3 分钟 | 7256÷39 是全队墙钟完成间隔。只读 turn 起止聚合得到平均 399.86 秒、成员回合时间合计 15594.35 秒；并行情况下两者不同。 |
| 缓存 miss 高证明前缀差、瓶颈不是题目难度 | miss 占输入约 70.8%、推理占输出约 84.2% 可核实，但这些比例不足以给出因果结论。需要逐调用耗时、上下文规模和用量。 |
| 官方逐题通关校验 11/31 | 现有外部 grade 脚本在任务记录/报告汇总文本内查找官方字符串，缺少逐题匹配源、运行轮次和快照指纹；应称外部字符串匹配，不等同平台 accepted。 |

核实的任务时间（北京时间）：2026-10-12 00:58:32 创建，01:18:19 第一轮控制链失败，03:02:04 第二轮结束。对应失败追踪 ID `3d88e06e-fd56-4d8b-85b7-0b253dc87e52`。

## 2. 已实施的优化

### 2.1 模型发送边界与上下文闭环

旧 Mailbox 在调用 MAF 后续流程前标记 sent，但原生历史处理仍可能拒绝超大上下文。本次离线真实 MAF 复现了连续三个 turn：provider 调用 0、sent 标记 3、自动续跑 3，可虚增未知预留。生产任务 member-8 也有 12 个零用量 context_limit turn；其最大已保存工具消息序列化约 499517 字符，大于当时 320000 字符模型视图上界。

- 使用固定 SDK 的叶子 provider 入口包裹，完整历史与最终输出视图准备完成后，才标记发送及消费模型步数；保留原生工具循环。
- 超大工具结果只在模型输入视图中生成明确的头尾预览，保留 message/call 身份、工具结果批次及完整 Session。用户目标、派工和调用参数不能剪成错误内容。
- 无法缩减的输入记录 context_blocked 指纹、规模和消息 ID，给 Lead 明确阻塞结果；旧输入不反复自动认领，新明确指令或显式恢复才解除阻塞。
- 成功调用 trace 增加 started_at、elapsed_ms、现有 usage，以及消息数/估计字符数/排除与预览计数。只保存聚合元数据，不进入模型提示，不把字符估计当实际 token。

直接使用失败任务 member-8 的已保存会话，纯离线调用新模型视图逻辑：40 条完整消息不变，模型视图 2 条、估计 203412 字符，2 条工具结果预览；provider 调用 0。记录 `.data/ctf-batch-hardening/member8-view-check.json`。这证明该上下文阻塞可被处理，不证明后续题目一定解出。

### 2.2 可信费用对账

- outbox 固化原 agent/turn/generation/reservation/request 身份；新旧格式按原调用归属补账，不借新 turn 入账。
- 新增仅 service 可用的 reconcile_usage_outbox/reconcile_budget，费用从已确认 Session 读取，调用方不能上传自填 usage；核验原回合、持久步数与请求身份，幂等更新原账本。
- 准入永久不足时先独立提交可信旧用量，再完整重新检查身份/停止和预算；即使新调用仍被拒绝，已确认的旧费用不会因事务回滚再次丢失。
- 预算关闭前先对账并重读状态。没有可信 usage 的未知结果继续保留占额，不用 TTL、无输出或任务结束推定免费。执行 generation fence 没有放宽。
- 传统 bill_usage 的 task 级重复请求不加费、不释放当前回合的新预留；历史对账仍拒绝错配身份。

### 2.3 控制链恢复与有限协调提醒

- 控制链 TransportError 先有界退避，前两次保留当前成员；持续失败仍给出安全诊断和可恢复收尾，不吞掉其他内部错误或内容审核。
- 已提交 claim 的响应丢失时读回同 instance 的 running turn，再取得原 token；数据库幂等与逐任务 tick 锁防止重复回合或两个本地写者。
- 全队没有可执行输入且有题目板时，以五秒巡检节流、六十秒静默冷却给 Lead 持久去重提醒。题目 revision/记录变化才重置无进展计数；最多三次后明确 partial/stalled 收尾，不无限耗模型。
- 全部题目已声明完成时最多一次总结提醒；空板不盲制造任务。旧 deferred、停止成员队列和其他成员上下文阻塞可以由 Lead 协调，但不自动解除队列或复活成员。
- Lead 自身不可处理上下文时给出可审计阻塞结论。停止操作、活动回合、未确认检查点和资源恢复等屏障保留。同一轮重启保留提醒计数，显式 resume 才重置新一轮。

### 2.4 工具纠错与收尾报告

- confirm_messages 的无效 UUID/数量由 API 映射为安全 422，避免未捕获的手动验证异常变成 500。
- 确认仅解除合法 queued/deferred；已确认的 active/delivered 请求可安全重复。取消、失败和不可用成员消息不恢复，冲突返回限定的 ID/status/deferred 元数据，不开放私聊正文。
- resume 与协调提醒提供待确认消息元数据；精确 Challenge not found 返回重新读取本任务题目 ID 的提示。未知 404/403/500、旧身份与预算错误保留原保护，不自动重试写入。
- 零模型调用生成可读逐题汇总、已有记录与帮助卡点摘要、record ID/version/作者/时间、真实证据 ID/URI/SHA256，以及未引用产物列表。无记录明确写未记录，不推断从未尝试。
- 完成声明、候选、可信验证和测试验证分开；费用估算、活动预留与待核实占额分开。完整机读 report_summary 加入已有归档，原 report URI、事件和授权路径兼容；原记录完整保留，截取显式标记，Markdown 数据安全转义。

## 3. 验证

- 最终 `UV_CACHE_DIR=/private/tmp/bbx-uv-cache make check`：**1220 passed / 179 deselected**，19.67 秒；Ruff/Pyright 通过，3 条既有 MCP 弃用警告。日志 `.data/ctf-batch-hardening/check-final.log`。
- 完整隔离集成：**175 passed / 1224 deselected**，267.49 秒（4 分 27 秒），exit 0。包含新预算/outbox/控制/巡检、旧 CTF 协作/执行/恢复/平台、黑板和归档；日志 `integration.log`。
- 使用脚本化 MAF 模型、一次性 PostgreSQL 和隔离执行容器，没有真实模型或靶机调用。成功 metadata streaming/nonstream 耗时断言 750/1500ms；零 provider 不记录成功派发。
- 首轮普通检查在旧取消测试挂住：fixture 使用裸 object 而非合法模型客户端，新的 provider 边界在启动前拒绝它，测试又无限等待 started。已改用 ScriptedChatClient、等待有界并暴露提前失败；没有在生产代码绕过发送边界。最终全套通过。
- OpenAPI 与 TypeScript API 声明同步，`git diff --check` 通过。无新依赖、迁移、docs/design 或 .env 修改。

复跑（依赖/执行环境未变，源码镜像用本地 network=none 增量构建）：

```sh
cd ~/bbx-wt/ctf-cooperation
UV_CACHE_DIR=/private/tmp/bbx-uv-cache make check
TESTCONTAINERS_RYUK_DISABLED=true UV_CACHE_DIR=/private/tmp/bbx-uv-cache \
  make -o image-exec-env -o image-egress-proxy -o image-eval-env \
  -o image-agent-runtime test-integration
```

## 4. 后续优化与边界

- 可信评测器应在评测侧持有答案，记录逐题匹配源、轮次和输入指纹，再通过可信适配记录结果；本轮没有把官方答案提供给 Agent，也没有把候选自动升级 accepted。
- 不自动调低用户选定 max、不强制填满 12 名成员。先用新增逐调用指标分析等待、上下文与模型时间，再按同一题集/预算做可选配置 A/B。
- 20 道未完成题缺记录仍是模型行为与交接质量问题；本轮增强可靠运行和可见报告，不凭 owner 自动把多题成员的命令归给某一题。显式题目/调用关联和投入统计可另行实现。
- 旧任务状态、估算账本、unknown 和原评测文件没有自动修写或续跑；没有可信收据的旧占额不能直接清零。真实解题率和速度改善尚未通过重新跑 31 题验证。
- 代码仍在 `feature/ctf-cooperation`，尚未提交/合并/推送。建议分组提交发送/对账、控制与巡检、工具反馈、报告及文档；与前两轮改动分别审阅。

## 5. 本地部署

已更新 <http://127.0.0.1:58000/login>。部署前 0 活动任务、0 running turn、0 待处理复盘；依次更新 blackboard/runtime，没有启动或续跑评测任务。

- 运行镜像 runtime `f64c070ded3d37afc66b2c86454f2816ad6709ae0c74fae9b5c2ceb78f44a6f2`、blackboard `2e6adc05a88daf208f712ea1f6b3942408045ca61241ae547a0aff785e9d96dd`。55/29 个运行 Python 文件与接受检查的源码逐一 SHA256 一致；两服务 running、restartCount 0，启动 ERROR/Traceback 计数 0。
- 原 6 任务、7 轮次、20 Session、10 profile、2 平台配置，以及 CTF 成员/轮次/消息/题目/记录和 events 的数量与全表摘要部署前后完全一致。原失败任务的 unknown 预留及结束原因保留。
- schema 仍 0014，登录 HTTP 200，前端 index SHA256 保持 `4a704f801b3fde1e4346acd75ea2c0ee2453de13d81c31a1918e3d9fc6f7f9b8`。没有重建或删除 PostgreSQL/MinIO 数据卷、靶机服务或归档。
- 本轮未发布新提示词/模型配置，也没有读写 .env；原部署入口正常消费已有配置，无凭据回显。

部署及核对材料 `.data/ctf-batch-hardening/`：`before-deploy.json`、`after-deploy.json`、`image-source-check.json`、`deployment-health.json`、构建/部署/检查日志。原 ANALYSIS.md 和外部 grade 文件没有修改。
