# CTF-context-unclosed-fix · 上下文未关闭项复核与修复

日期：2026-10-13。工作树 `/Users/yym/bbx-wt/ctf-cooperation`，分支 `feature/ctf-cooperation`，起点 `118b393`。依据用户提供的 [未关闭项报告](CTF-context-exhaustion-unclosed-report.md)，结合当前源码、已安装 MAF、实际平台只读元数据和隔离测试复核；GPT-6.1 Sol high 分别实现平台通知/清理、配置发布，并独立审查。主会话决定容量方案、整合测试及发布。

## 1. 审核结论及处理

| 项 | 复核与处理 |
|---|---|
| U1 模板发布 | 最新 `ctf` v13 已有两条成果保护指导；旧任务固定旧版本不能证明新任务漏发。默认创建从最新 `ctf` 复制模板，再生成任务快照，已有路径正确。补齐显式运维同步工具，发布 `ctf` v14、`ctf-task-settings` v7，两个家族模板一致，各项指导只出现一次。 |
| U2 容量 | 采用报告 P1 的提高阈值方案：可复用 task-family 的阈值 400000 → 606784，硬输入预算 320000 → 485427。保留最大输出、20% 余量、九成提前触发及六成理想水位，不放宽门禁、不修改旧任务。 |
| U3 延迟输入 | 平台已有 `confirm_messages`，无需再造释放入口。补齐 Lead 提示词及耗尽/闲置通知：明确延期 ID、诊断、确认或较小新指令。实测 cancelled 不复活，被阻塞的原输入不能仅因确认就重放。 |
| U4 恢复次数 | 保留每成员每 run 一次。回归证明新 instruction、成员 stop/resume 不清额度；第二次阻塞产生耗尽通知。 |
| U5 历史残留 | 新增默认只读的终态清理 CLI，复用 task 锁与 `clear_context_blocks`。已将 `d11f03ea` 的一条旧活动阻塞转存历史，非空终态 blockers 为 0；会话、消息、计数和结论保持。 |
| U6 聚合诊断 | 复用耗尽与闲置通知，直接附 branch/eligible/cut/estimated/limit 及延期引用；不新增聚合读取工具或公共黑板字段。 |

### 原报告需要更正的事实

- U1 附表将全部历史任务概括为 v6 不准确：现场 9 个任务分别固定 v1/v2/v3/v4/v6，最新任务为 v6。旧快照不跟随 Worker 更新是当前 CTF 契约。新增 ASGI 回归证明默认新建读 `ctf` 最新模板，而非复用旧 task-family。
- U2 的“680k 富余”没有扣除 393216 输出预留，不能直接作为可追加输入。1M 窗口的可用输入容量先减为 606784，再保留 20% 估计余量，才是 485427；源配置窗口未知时维持原值，不从目标家族倒灌模型设置。
- U3 已有显式确认入口。Lead 可以作相关性决策，并非必须用户手动干预；`confirm_messages` 只处理仍排队的输入，取消及上下文原输入保护继续有效。
- U5 原验收 SQL 只检查键存在不正确：正常状态保存 `context_blocked_members: {}`。应检查非空对象，不为使键消失而改状态格式。
- V4 已查清，不是同 run 清零：`cda295d2` 的 `member-1` 两次自动恢复来源回合分别在 **2026-10-12 15:45:06 UTC** 与 **23:38:33 UTC** 结束；中间 **23:21:49 UTC** 有 `task.resumed(run_number=2)`。第一次属 run 1，第二次属 run 2；最新计数为 1 符合显式续跑重置规则。查询只涉及 turn、消息来源及事件时间，没有读取真实候选正文。

## 2. 实现和边界

### 平台通知与清理

`CtfService._recovery_diagnostics` 只取有界的延期引用及数值：最多 50 条消息、20 个阻塞成员，带截断标志；branch 使用已知值，非数值或异常范围的诊断不向通知输出。不读取或广播消息正文。耗尽通知和闲置提醒共用这条路径，停滞结论也保留参考，仍不自动确认输入或重置额度。

`cleanup_terminal_context_blocks` 持任务锁，要求终态 closed，无 running execution/review、成员 current turn 或 pending operation。非空标记转存原历史、过滤空 ID，并记录已有 `ctf.coordination.reminded`；空残留不写、不追加事件。CLI 默认预览，支持按 UUID 选择，应用时重新检查所有门禁；异常只输出类别，避免 SQL JSON 参数或配置值落进 traceback。

### 配置发布

`ctf_profile_publish` 默认 dry-run，应用必须提供两个最新预期版本。复用 `ProfileStore.create_in_connection` 的 advisory lock、内容去重和 CAS，在一个事务内发布；任一家族并发编辑则全回滚。发布前验证成果指导和稳定 SHA256 模板摘要，不依赖 PostgreSQL JSON 的排版/md5。

源 `ctf` 内容保持，目标仅复制源模板及明确指定的阈值；两家族的模型、价格、工具、MCP、执行镜像/资源、其他选项各自保留，无法无损转换的旧模型拆分明确拒绝。可选输出预留参数仅显示计划容量，不配置或降低实际模型输出。重复相同内容不新增版本。

新建任务省略阈值时，原有创建流程已继承所选模型窗口；1M 模型对应的硬预算同样为 485427。若创建脚本仍明确传 400000，平台继续尊重它。新发布的 v7 不会改写已固定 v6 或其他版本的任务；已有任务续跑也保持原模型与阈值。没有增加静默迁移或隐藏的重试次数。

### MAF 与范围

复核本机 MAF core 1.19.0 的 `CompactionProvider`、`ContextWindowCompactionStrategy`、`TruncationStrategy`、`annotate_message_groups`。本轮修复在平台事务、通知和运维发布层，沿用原生 Agent/Session/History/ResponseStream 及已实现的安全裁剪；不叠加另一套压缩、摘要模型或工具配对器，不升级依赖。

没有修改 contracts、OpenAPI、数据库 schema、三份 `docs/design/` 正本、前端、执行权限或真实任务完成标准。proposal 与使用文档仅补充当前机制及运维入口。

## 3. 实际运行的验证

```sh
cd /Users/yym/bbx-wt/ctf-cooperation
UV_CACHE_DIR=/private/tmp/bbx-uv-cache make check
UV_CACHE_DIR=/private/tmp/bbx-uv-cache TESTCONTAINERS_RYUK_DISABLED=true \
  make -o image-exec-env -o image-egress-proxy -o image-eval-env \
  -o image-agent-runtime test-integration
```

- **普通检查：1373 passed / 246 deselected，23.86 秒**，291 个文件格式、Ruff、Pyright 通过；三条既有 MCP sampling 弃用提示。
- **完整隔离集成：242 passed / 1377 deselected，312.57 秒**。没有缩小 CTF 用例范围；包含真实 PostgreSQL、HTTP、MinIO、envd 及运行镜像检查，模型均为脚本客户端。`-o` 只跳过已有镜像重复构建。
- 新增 **14 普通、14 集成**：容量对照、发布模板/字段保留/窗口未知/请求快照、CAS/事务回滚/幂等、通知 ID 与安全诊断、确认/取消保护、同 run 额度、终态/活跃复盘清理及 dry-run/异常脱敏。独立审查的定向 14 项通过，无未关闭代码阻断项。
- 同一组完整已保存消息，在旧预算达到提前触发点而产生一次压缩；新预算下不压缩，epoch 为 0，模型前缀及完整历史保留。此为合成离线对照，不是原事故的真实消息重放，也不是缓存命中率实测。
- 首次 `make check` 因两个新测试的参数类型标注失败，更正 fixture 后全绿；容量测试最初只调用历史加载，未经过九成触发的 provider leaf，补齐真实 leaf 路径后通过，没有放宽生产断言。独立审查发现 CLI 未捕获数据库异常可能打印私有 SQL 参数，已修复并加入回归。

## 4. 本地发布与数据验证

发布前/紧邻服务更新均核验 **0 活动任务、0 running turn**。只暂停及更新两个应用服务，保留原容器、环境、数据库、MinIO、数据卷及靶机；运行容器源码和未来新容器镜像都更新，正式登录入口 <http://127.0.0.1:58000/login> HTTP **200**。

- **镜像**：blackboard `b188b815a57de1db771dd8e859bc14fdb16be74d0c9d52027cc3ffe9339bd2a4`；runtime `112127e403916632bdf3964dc7c54e028ea43866478e9ecafad6489ce7c2351c`。常用标签 `bbx-ctf-blackboard:latest`、`bbx-agent-runtime:latest`，另保留 `bbx-ctf-context-{blackboard,runtime}:20261013`。
- 使用前次已验证基础镜像 `--network=none` 增量打包，没有下载模型、安装依赖或变动 Kali 镜像。镜像内运行实际 `input_token_limit/ctf_max_output_tokens`，验证 393216 输出预留和 485427 输入预算。
- 工作树、镜像与实际运行容器逐文件哈希一致：runtime 68 个源码/合同/模板文件、blackboard 43 个，无差异。
- Worker CAS API 将 `ctf` **v13 → v14**，只追加一条 deferred 语义段；随后发布工具将 `ctf-task-settings` **v6 → v7**，只同步模板及提高阈值。稳定模板 SHA256 均为 `6e5e6e5454e1f53725132c5460de6fd73b7e4ce97615f07031a49031a7ab5c67`。再次应用完全幂等，不新增版本。
- 清理预览仅命中 `d11f03ea` 的一个旧活动标记，按该 task-id 应用并转存历史；再预览无候选。新增的审计事件经真实 `/events?since=` HTTP 读回，状态 **200**，契约校验通过。
- **9 个任务、56 个 Session、58 个成员、278 个 turn、448 条消息、129 个题目、641 条记录、13 条 task run** 保留。消息、会话元数据、成员、回合、题目、记录及 task run 数量/摘要均不变；所有旧 profile 行摘要不变。任务其余字段及除阻塞/诊断历史以外的 control 摘要不变；只新增 **2 个 profile 版本、1 条清理审计事件**。
- 已部署前端 index SHA256 仍为 `e1e29706da0d365de4bb7be44ad057a971ff0df38a366d101e709c06cc575f5d`；当前明亮环绕 UI 保留，两个应用服务 running、restartCount 0。测试临时容器均已清理。

发布探针初次错误引用了并不存在的 `tasks.version` 列；按真实 schema 修正后重新核验再更新源码。镜像容量探针初次省略 ModelConfig 必填项、服务刚启动时首次 HTTP 尚未就绪，分别改用真实 bundled profile 及有界就绪重试后完成核验。这些是检查工具问题，没有放宽运行门禁或修改用户模型配置。

全部日志、源码备份、计划/发布摘要、数据指纹在本工作树 `.data/ctf-context-followup/`；该目录不提交，不含 `.env` 或明文凭据。

## 5. 运维复核命令

下列预览与幂等命令对应本次发布完成后的 **14/7**；未来版本有变化时先重新预览，不能照抄旧 CAS 值。`--reserved-output-tokens` 是当前 runtime 的容量诊断参数。

```sh
docker exec bbx-ctf-team-mode-blackboard-1 /app/.venv/bin/python \
  -m bbx_blackboard.ctf_profile_publish \
  --task-context-threshold 606784 --reserved-output-tokens 393216

docker exec bbx-ctf-team-mode-blackboard-1 /app/.venv/bin/python \
  -m bbx_blackboard.ctf_profile_publish --apply \
  --expected-ctf-version 14 --expected-task-settings-version 7 \
  --task-context-threshold 606784 --reserved-output-tokens 393216

docker exec bbx-ctf-team-mode-blackboard-1 /app/.venv/bin/python \
  -m bbx_blackboard.ctf_context_cleanup
```

本次不需要用户补执行构建、发布或清理。上述命令不会重跑靶场。

## 6. 偏差、待验证及后续建议

- 报告 P1 采用提高阈值，P2 复用已有 Lead 显式确认，P3 保留一次，不新增余量配置、工具或自动释放策略。提高上限可减少不必要压缩，但更长输入及预算准入成本也可能提高，不能宣称必然省钱。
- **V1/V3 的真实效果仍未测量**：没有收费模型调用、实靶重跑、官方答案读取或判分；未承诺原事故八条回合全部可恢复、缓存率提高至某比例或求解数量增加。下一次用户正常测试需读取新存储的末次单次 usage、epoch、context_blocked 分支及实际费用，再评估估计偏差；不以累计输入平均数替代末次输入。
- V2 已以完整 242 项集成补齐；V4 已用真实 resume 事件时间解释。旧诊断保留原 `limit_chars` 等字段与单位，不重新解释成当前 token 门限。旧任务快照保持原样，新模板/容量不回填既有 Session。
- 修复与部署阶段未执行 Git 写操作。用户随后要求“就提交一下，不再测试”，已按三组提交：`493a97e` — `fix(ctf): make context recovery decisions explicit`（平台及恢复回归）；`c31ace3` — `fix(ctf): publish aligned profiles and clean terminal blockers safely`（两个 CLI、相关回归及容量对照）；`docs(ctf): record context follow-up fixes and deployment`（提示词、proposal、部署说明、本报告及用户复核原报告）。沿用已经全绿的检查结果，没有重跑测试；各提交正文按仓库约定注明任务 `CTF-context-unclosed-fix`。本次仅提交到任务分支，未合并或推送。
