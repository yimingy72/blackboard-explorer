# Run3 独立审查与中断恢复报告

审查对象：2026-10-12 的测试任务 `cda295d2-82ce-4cdf-bcad-dce5eb191433`。来源报告为 `/Users/yym/bbx-wt/bbx-cybench-smoke/deploy/batch/CYBENCH-RUN3-AUDIT.md`；此外独立读取数据库、MinIO受控观测和现有SDK/服务源码。工作树为 `/Users/yym/bbx-wt/ctf-cooperation`，保留所有前序未提交改动；三个GPT-6.1 Sol high子代理分工，主会话交叉审核、补充模板、统一验证和发布。

## 1. 独立核实的数据

| 指标 | 本轮核实值 |
| --- | ---: |
| 成功模型回复及已入账调用 | 703 |
| 输入缓存命中 / 未命中 | 78,233,568 / 3,207,718 token |
| 已返回usage的token加权命中率 | **96.06%** |
| 不改变裁剪起点的693次调用 | **97.61%**命中 |
| 改变裁剪起点的10次调用 | **0%**命中，1,295,344未命中token |
| 预览工具结果的362次调用 | 96.88%命中 |
| 输出 / 其中推理 | 2,002,990 / 1,605,789 token |
| 配置价格费用估算 | CNY 25.56869872 |
| 回合 / 峰值同时运行回合 | 25 / **12** |
| 首名队友开始前的时间 | 338.68秒，约5.64分钟 |
| 全部回合覆盖时长 | 36.93分钟 |
| 模型调用elapsed_ms之和 | 225.35分钟，成员之间有重叠 |
| sent但未取得可信usage的预留 | 3条，共CNY 11.837184 |

task、execution turns、members和703条model_output的hit/miss/usage合计完全一致，没有遗漏完整usage或重复入账的证据。3条未知请求不在成功回复分母中，96.06%不能称为供应商所有尝试的完整命中率或实付账单。推理包含在输出中，不重复计费。

额外审查1149次工具调用：execute_command706、register_artifact84、update_challenge59、send_message48、list_records41、get_challenge40、set_verification_required33、create_challenge31、append_record30、record_candidate22、list_challenges21、create_teammate12、list_members10，以及少量生命周期操作。没有证据把所有查询都算作无效轮询；本轮没有根据这些计数强行删工具或写题目专属策略。13个成员包含Lead、后来新增者与被移除者，不等于13个同时工作者。

目前优先级是恢复可靠性和交付完整性，不以多发送历史、padding或重复请求提高百分比。低水位裁剪必然有少量冷启动损失；调用次数、输入规模、费用、延迟和实际产出要一起看，不能只优化百分比。

## 2. 对外部报告的更正

1. **瞬时模型错误确实触发整体失败。** 受控错误是ChatClientException包装RemoteProtocolError、response_read阶段、transient=true。原runner只捕获外层TransportError，包装后绕过恢复，coordinator按内部错误关闭。SDK的request重试不覆盖已返回stream的迭代失败；本机MockTransport配置max_retries=4，真实SDK断流仅发送一次HTTP。`attempt_limit=5`只是配置上限，不能当成本次已经重试五次的证明。http_status=null也不能证明没有收到HTTP响应头。
2. **停泊根因是SQL NULL，不是一次恢复额度耗尽。** 六个context_blocked条目都含null message_id；`id NOT IN (..., NULL)`对新消息产生UNKNOWN，无法进入ready_member_ids。member-11实际active/idle，有三条queued非deferred指令（自动恢复、两条Lead指令），报告漏了最后一条。member-10已被Lead移除，恢复指令合法cancelled，不能当作闲置待恢复算力，更不能自动复活。
3. **16不是平台accepted数量。** 读取grade.json得到16/31；grade.py按记录与报告合并文本检查参考答案字符串，属于离线输出匹配，未真正提交平台验收。13个completed是工作状态，候选/可信验证是另一维。未读答案文件、未重判分、未修改原报告；不采纳“看到flag自动completed/accepted”的建议。
4. **3条unknown不能因interrupted自动核销。** 它们已有sent证据但没有可信用量，分别属于Lead、member-4、member-6。终态、drain或没有最终输出不能证明免费。按已有confirmed Session/outbox幂等对账，只有匹配可信receipt才释放对应占额；旧账本不改写。
5. **产出改善不能单独归因于缓存。** 上轮6/31、本轮16/31是不同成员数、上下文/步数配置及运行路径下的观测结果，不是控制变量实验。上一轮d11f03ea是22.82%；更早3e4d2970按现有账本约29.20%，原表将22.8%放到第1轮有误。低难度标签和剩余时间也不能证明未解题一定可解。

## 3. 已完成的修复

### 3.1 成员级瞬时故障恢复

复用既有model_error_metadata分类、MAF原生AgentSession与checkpoint、finish_turn结算和同事务continuation。仅明确识别的provider瞬时异常进入`provider_transient`；本地协议错误、无效请求、鉴权/内容审核及内部TypeError/ValueError/RuntimeError不因此重试。

当前成员保存历史与工具结果，取得远端drain/检查点证明后结算回合，保留其他成员执行。自动新回合每成员每run最多三次，退避2/4/8秒，时间和次数存入ctf_control。runtime_state和claim_turn共用门禁，重启不清次数。新的明确用户/Lead指令可响应，不重置自动额度，并将被替代的旧自动指令保留为deferred；耗尽通知Lead，Lead自身通知deferred以免无限自唤醒。显式resume重置本轮额度。

不在同一sent预留上重复发送请求；原未知费用仍保留，新回合使用新准入。未确认checkpoint、远程停止证明和结果未知工具沿已有安全路径处理。清除coordinator无效的generic“三次判断”，真正内部异常仍fail-close。

### 3.2 封锁消息恢复

派发条件过滤旧空ID，今后block_context和结算只记录有效消息ID。旧NULL条目无需直接迁移或清表，新服务读取时兼容。

context_blocked释放未确认leased输入时保留为queued/deferred；legacy新指令认领前隔离旧封锁输入。不会把原过大输入与新恢复指令一起重新租赁。新Lead instruction返回明确投递状态；自动一次限额、预算、phase、generation及成员生命周期保持。取消、移除和终态复盘不自动执行。

### 3.3 发送边界的安全上下文处理

原生MAF逐leaf都会读取历史，并非只在agent.run开始读取。原缺口是随后追加工具结果/新输入跨线：离线复现745 token旧历史＋80 token新批次超过800门限，虽有旧历史安全边界却直接封锁。本轮六个320145–323875估计值与此机制相容，但终态规模不能逐条证明同一原因。

现在预览之后，仅对**已经成功提交、准确对应当前outgoing的完整旧前缀**批量降到低水位。提交证明来自实际成功payload或丢响应后的saved快照，不从等待期间可能变化的当前内存冒认；同Content顺序/签名映射不依赖None message_id或文字相同。复用MAF公开分组，保留reasoning/并行工具批次及未知结果保护。cutoff/epoch在发送前保存，只有epoch变化才额外checkpoint；恢复前缀缓存一次，旧预览和完整Session不改写。

当前回合的用户/派工、未确认pending和未完成工具组不可裁。锚点阻止继续裁剪或映射不能证明时仍明确context_blocked，由有界新回合接续；不承诺单回合无限滑窗。没有直接启用会排除本轮新输入的默认TruncationStrategy，也没有新增摘要模型、tokenizer依赖或历史框架。

### 3.4 原子交付与可信验证保护

扩展现有`record_candidate`的可选`work_status`（in_progress/blocked/completed），owner/Lead显式传入时在同事务保存记录、证据、工作状态与revision；省略时兼容旧调用，帮手不能替owner更改状态，已闭题需先reopen。

复用原CAS、请求幂等、artifact校验和事件写入，避免先候选成功、后状态写入失败的窗口。Agent只声明candidate/unknown；新的候选记录仍保存，但不会覆盖已有非测试platform/user验证。不能用答案文本自动完成或通过。

两角色模板补充原子交付提示，Lead遇provider_transient时保留分工、等待已安排的有界恢复并推进其他独立工作，耗尽后再决定增援或显式重派。没有写入题名、答案、具体漏洞套路，也没有自动降低max思考强度或改变成员数。

## 4. 实际检查

| 检查 | 最终结果 |
| --- | --- |
| `make check` | **1295 passed / 205 deselected**，19.31秒；Ruff、Pyright及274文件格式通过，3条已有MCP弃用提示 |
| 完整`make test-integration` | **201 passed / 1299 deselected**，280.98秒，随机端口PG与隔离执行环境 |
| B前端`pnpm check` | TypeScript、ESLint通过，**81测试通过** |
| OpenAPI/TypeScript声明 | 已生成并同步两个工作树；使用ui-rebuild已有CLI，无新增依赖 |
| diff与发布 | `git diff --check`、部署源码SHA256、旧数据/配置/前端指纹通过 |

有意义的回归包括：真实SDK一条stream中断不自动HTTP重试；脚本化MAF先成功工具再部分delta断流，新回合工具执行仍一次；403/400/LocalProtocol/内容审核不恢复；远端drain未证实保留pending；两成员一失败另一仍执行；退避/重启/上限/预算/显式指令/resume；NULL SQL先失败后修复；旧封锁输入不再重新租赁；真实序列化与None ID映射、变更内存不冒认提交；stream/nonstream三个leaf且pending不提前确认；原子候选写中途错误整事务回滚、旧revision、重复请求、协作者拒绝、可信accepted保护、flag样式文字不自动完成。

定向检查和首轮fixture/类型/MAF返回形状修正均保留日志；最终完整普通与集成均一次通过。整个检查没有真实模型或目标调用。前端仅更新生成声明，未重跑浏览器全量或重建B静态资源。

复现：

```sh
cd /Users/yym/bbx-wt/ctf-cooperation
UV_CACHE_DIR=/private/tmp/bbx-uv-cache make check
TESTCONTAINERS_RYUK_DISABLED=true UV_CACHE_DIR=/private/tmp/bbx-uv-cache \
  make -o image-exec-env -o image-egress-proxy -o image-eval-env \
  -o image-agent-runtime test-integration
pnpm --dir /Users/yym/bbx-wt/ui-rebuild/web check
```

审计与日志：`.data/ctf-run3-audit/`下call-metadata.json、summary.json、independent-ledger.json、parked-messages.json、concurrency-summary.json、tool-summary.json、check.log、integration.log、frontend-check.log及发布记录。工具诊断只保留名称计数，不保存命令、参数、输出正文或答案。

## 5. 本地发布与保留项

入口仍为 <http://127.0.0.1:58000/login>，可新建任务或由用户显式续跑旧任务；本轮没有自动创建、续跑或重新评分。

- 发布前8任务全终态、0活动回合、0待复盘、0待清理；保持PostgreSQL/MinIO、数据卷、靶机、归档与当前容器ID。
- network=none本地源码镜像，仅打包合同、服务、模板和OpenAPI；应用停止后更新源并启动。41个blackboard/合同/模板文件、68个runtime/合同/模板文件与检查版本完全一致，OpenAPI也一致；服务running、restartCount 0，启动ERROR/Traceback计数0。
- 两角色模板通过原CAS接口发布，revision **7→9**，仅prompt_templates变化；所有旧profile、模型/价格/工具/资源及task/session/mailbox/record/events/账本指纹保持。三条旧unknown没有清零。
- 未来重建镜像runtime `465d3eefbcff478b0ed2775f43e3b2ba18c5c86553b657e7381b4974a7996b8f`、blackboard `a450071e2460ca77e63f58c581826b7e6f6dc2f521f5a99b7e301671f274862d`；当前运行容器使用已核对更新源码。
- schema仍0014，登录HTTP200，B前端index SHA256保持`030eea19b74cb4d35a812b84f643545f5df02be80cd8246aacf5547dbcd71cbf`。源码备份在before-source，原数据库备份未动。
- 没有读写.env、回显凭据、读取官方答案、运行报告附录的靶场命令，未改docs/design、依赖、全局设置或Git。

## 6. 偏差、验证边界与提交建议

采用已有MAF组件和平台事务约束；原生compaction默认保护范围、供应商计量、稳定前缀与已确认receipt不能直接由一项Agent参数同时解决，必要的CTF薄适配保留。当前token计量是保守估计，框架浅拷贝/分组依赖按固定版本验证，升级需重跑边界测试。

新工具schema会在下一次请求出现，旧任务opening instructions仍固定；新模板用于新任务。未知真实费用需要供应商可信收据，不用终态或取消推定免费。当前回合输入不可裁时仍可能封锁，有界恢复耗尽后仍需Lead/用户明确继续；不无限重试。

尚未重跑收费31题，不承诺新版本得分、速度或缓存百分比。缓存96.06%来自这一轮实际数据；本轮修复后的生产效果须下一轮再观测。起步5.64分钟与任务/模型时间分布是进一步性能分析的线索，不能据工具次数直接宣称全部为浪费，也不能从16/31线性预测跑满预算的成绩。

代码仍在feature/ctf-cooperation，未提交/合并/推送。建议在前序修改之后分别整理：

1. `fix(ctf): recover transient provider failures per member`：分类/runner、持久退避、单成员结算及普通/PG恢复测试。
2. `fix(ctf): unblock queued recovery messages and compact confirmed context`：NULL兼容、deferred旧输入、提交证明/MAF安全前缀、边界测试。
3. `feat(ctf): atomically record candidate delivery and work status`：现有工具参数、合同、原子写入/可信验证保护、schema和回滚测试。
4. `docs(ctf): document Run3 findings and bounded recovery`：模板、方案、任务、使用文档与本报告。

共享ctf.py、middleware等包含前序修改，不能将全部dirty diff归本轮；按实际差异拆分。无需用户执行额外沙箱命令。
