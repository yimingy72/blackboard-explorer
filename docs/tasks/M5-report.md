# M5 · 评估与单 Agent 基线报告

日期：2026-09-25。前置 EVAL-2 已合并为 `f7db7a0`；本任务从 `b74921c` 开始。设计先行提交 `e34cb1c`。

## 完成内容

| 编号 | 完成内容 |
|---|---|
| E.1 | `Params.derive_enabled=true`、任务覆盖允许 bool、单 Agent Profile（仅此参数为 false）与启动注册。调度保留已有意图探索和最新裁定，再在静止时直接收尾；single 任务并发设为 1，close 的职责不变。同步 JSON Schema、OpenAPI、前端类型与终止说明。 |
| E.2 | 已有黑板运行器与隔离批量入口；固定整个批次的完整 Profile/version。创建后启动前持久保存 ID；串行等待终态、超时停止/交接，导出状态/事件/报告/归档/全部持久证据映射，拒绝覆写旧结果。失败、超时或导出缺项返回非成功。 |
| E.3 | 文件/函数/机制关键词自动候选、完整人工复核清单、P 的 0/0.5/1 评分、P/D 去重精确率、未知发现复核、接口覆盖、成本/缓存/过程指标。显式证据清单在全新离线容器重跑，保存退出码、输出与目标哈希。 |
| E.4 | Markdown 对比报告列平均、最差、有效样本数和结束原因分布；未知值不是零。两组各至少 5 次且复核完整才给正式召回判断；召回持平时展示耗时变化，不擅自定义“明显更短”。 |
| E.5 | HTTP 替身导出测试、评分/比较假数据、失败清理、路径校验、接口解析、真实离线重跑容器测试；完整 CLI 评分与报告生成已用两组模拟记录运行。 |

## 实际运行的检查

- `make check`：Ruff、Pyright 通过；**338 passed、38 deselected**。不加载 `.env`，没有真实模型调用。
- `make test-integration`：**34 passed、342 deselected**，96.81 秒。包括原 32 项、新 single 完整生命周期、证据在两个全新容器中运行且无状态残留；全部测试容器清理。
- `make web-check`：ESLint/TypeScript/Vitest 通过，**29 个测试**。
- `make image-blackboard image-agent-runtime image-eval-env`：构建通过。评估镜像基于已有 exec-env，预装目标已有的 FastAPI/pytest/httpx 等依赖，不新增项目依赖。
- 打包目标的路由差异解析得到 **14 个新增或修改接口**，保留 HTTP method 和路径；生成固定接口覆盖分母。
- `make eval-score EVAL_OUT=.data/m5-demo`：两组模拟结果的 score、review-template 和 comparison.md 生成成功，正确报告待复核/样本不足。模拟结果仅验证流程，不代表 DeepSeek 实测效果。
- `git diff --check` 通过；命令入口 `--help` 可运行。

**本轮没有运行 5+5 次付费 DeepSeek 评估，也未声称多 Agent 已证明优于单 Agent。** M5 任务说明将实际评估运行与最终人工复核列为用户可执行检查点；本轮完成运行器与验证，不用假数据替代该检查点。此前的真实 M3b 玩具闭环结果仍保留。

## 用户可执行的命令

详细参数、复核字段、指标口径见 `eval/runner/README.md`。镜像已在本机构建；其他机器或代码变更后先重建。

```sh
cd ~/blackboard-explorer
export UV_CACHE_DIR=/private/tmp/bbx-uv-cache
export https_proxy=http://127.0.0.1:7897 http_proxy=http://127.0.0.1:7897
export all_proxy=socks5://127.0.0.1:7897 no_proxy=localhost,127.0.0.1,::1,host.docker.internal
uv sync --locked

# 显式付费：独立Compose，default/single各5次，默认每次金额预算10、时限60分钟。
# uv子进程加载已有本地配置，代码不读取、打印或修改.env。
make eval-run EVAL_ENV_FILE=/Users/yym/blackboard-explorer/.env \
  EVAL_OUT=eval/results/experiment-01 EVAL_N=5
make eval-score EVAL_OUT=eval/results/experiment-01

# 根据每次review-template.json生成review.json并确认发现/接口；
# 选择归档中的独立Python证据脚本写replay.json，再重跑：
make eval-replay EVAL_OUT=eval/results/experiment-01 \
  EVAL_RUN=eval/results/experiment-01/default/run-001
make eval-score EVAL_OUT=eval/results/experiment-01
```

已有黑板也可用 `uv run --no-sync python -m eval.runner.run --task mini-shop-review --profile default --n 5 --out <新目录> --base-url http://127.0.0.1:58000 --exec-image bbx-eval-env:latest`，凭据来自环境的 `SERVICE_TOKEN`。已有服务必须已提供 eval-targets 和出网白名单。

不传 `EVAL_OUT` 时 `eval-run` / `eval-score` 都使用 `eval/results/latest`；旧目录存在即拒绝覆盖，新实验应指定新路径。

## 数据与指标定义

- 每次 `run.json`：固定版本/完整 Profile/预算、task_id、终态与 outcome，启动至首次观察到终态的耗时；导出耗时另记 `export_seconds`，避免大归档下载扭曲对比。
- 黑板、事件、报告、workspace 与证据对象均保存本地；证据文件名是 URI 的 SHA-256，映射保留原 URI。没有把目标答案注入 Agent 上下文。
- 自动评分只给候选，人工确认的定位+机制计半分，再有可复现证据计一分；每个 P 取最高分，除以 6 得召回。
- 精确率对确认的 P/D 按 ID 去重，新发现按 `unique_key` 或 finding ID 去重。未知发现不自动算真，也不自动算假；未完成分类时不输出确定的召回/精确率。
- 接口分母从保存目标的 main..feature 路由函数差异生成，`review.json` 不能缩小；分子是人工确认报告已检查的接口。
- 复现率只针对清单中选择的脚本，未运行是 null；退出码/输出匹配不能替代机制确认。当前不自动提取或执行报告命令。
- 成本使用账本/固定 Profile 价格，缓存命中率为 hit/(hit+miss)；争议、裁定采用事件计数，satisfies 命中指最终 met 的支撑声明占全部声明的比例。

## 偏差与待决

1. 实际 5+5 运行、发现复核及真实比较结论仍未执行。代码完成不等于多 Agent 收益已经验证；不能据本报告进行效果宣传或针对答案调整提示词。
2. 原 exec-env 实际未预装 mini-shop 的全部依赖。增加 `bbx-eval-env`，两组统一使用，保留 default/single 本地 Profile 只有 derive 参数不同的原则；实际发布快照记录镜像覆盖。
3. 证据重跑第一版只支持归档中小于 200 KiB 的完整独立 Python 文件。工作目录/PYTHONPATH 固定为全新目标，旧端口/旧进程/额外脚本依赖不会自动恢复，明确失败或待人工处理。
4. 每项重跑使用非 root、只读根、无外网、无宿主密钥/socket的独立容器，限制CPU/内存/进程数，默认120秒；目标压缩包128 MiB上限，接口提取展开512 MiB/50,000成员上限，归档脚本复用M4受限预览。
5. 路由提取覆盖当前目标的静态 `@app/router.method` 与 APIRouter prefix，不承诺泛化到动态注册或任意框架。已有服务器单组运行没有自动接口清单时，需人工指定对应差异的分母。
6. “明显更短”在正本中没有数值阈值，因此只显示实际百分比并留待判断，没有擅自引入新的验收门槛。
7. 普通测试通过 HTTP 替身；容器测试不调用模型。最后还有人工判断边界，不能以生成 comparison.md 文件冒充完整实证评估。

## 共享文件与提交划分

- `packages/contracts`：Params、TaskSpec 的 bool 覆盖、Schema 与测试。
- `profiles/default` 与新 `profiles/single`：新增开关及完全一致的其余快照模板；blackboard启动注册两者，runtime调度守护。
- `Makefile`：将 eval/runner 纳入 lint，新增镜像/运行/评分/重跑入口，集成目标构建评估镜像。
- 根 `pyproject.toml`：将 eval/runner 的测试/类型检查纳入普通检查，并配置项目根导入路径；无依赖和 uv.lock 变化。
- `.gitignore`：忽略 eval/results。同步 OpenAPI、前端 Schema/参数测试与禁用推导的终止说明。

建议并采用：

1. `Define optional derivation for single-agent evaluation`：设计（已提交 e34cb1c）。
2. `Add single-agent evaluation profile and derivation control`：contracts/profile/调度/注册/生成文件/对应测试。
3. `Add reproducible evaluation exports and reviewed comparisons`：评估运行器、隔离重跑、评分比较、共享检查入口与文档。

合并后打 m5，清理工作树与分支，更新 HANDOFF。保留模拟流程输出和实际检查日志，真实评估结果以后单独形成检查点。

---

## 2026-09-25 真实 5+5 评估补充

本节补充 M5 实现合并后的真实评估检查点。原报告中“未运行真实 5+5”的说明仅描述实现合并当时的状态；本次已按同一批次配置完成 `default` 与 `single` 各 5 次 DeepSeek 运行、逐 run 产物复核、所选证据 replay 和总评分。原始结果目录为 `eval/results/real-20260925-01/`，因 `eval/results/` 被忽略且含完整 workspace/evidence 归档，不纳入提交；不含原始证据输出的精简 checkpoint 保存在 `.data/checkpoints/m5/real-20260925-01/`。

### 实际运行与复核

- `make eval-run EVAL_ENV_FILE=/Users/yym/blackboard-explorer/.env EVAL_OUT=eval/results/real-20260925-01 EVAL_N=5`：真实 DeepSeek 批次完成，`default` 5 次、`single` 5 次均为 success。命令由 uv 子进程加载既有本地配置；本轮没有读取、打印或修改 `.env`。
- 对 10 个 run 的 `report.md`、`state.json`、`events.json`、`evidence/index.json` 与归档脚本逐项复核，补齐 `review.json`、`REVIEW.md`、`replay.json`、`replay-results.json`。
- `make eval-score EVAL_OUT=eval/results/real-20260925-01`：重算 10 个 `score.json` 与 `comparison.md`，0 个 run 仍有 `review_pending`，全部 `provisional=false`。
- `make check`：Ruff、Pyright 通过；356 passed、39 deselected。不调用真实模型，不读取 `.env`。
- `make test-integration`：构建/刷新 `bbx-exec-env`、`bbx-egress-proxy`、`bbx-agent-runtime`、`bbx-eval-env` 后通过；35 passed、360 deselected，用时 100.37 秒。不调用真实模型。
- `git diff --check`：通过。
- 交叉复核时统一了一个评分口径：同一 `coupon_id/user_coupon` 在一次请求中重复传入能辅助证明 P6 负总额，但目标没有把“重复同一参数”定义为独立列表外业务漏洞，因此 `default/run-004` 与 `default/run-005` 各补记同一个误报键 `same-coupon-id-repeat-in-one-order`；两者 precision 从 7/9 修正为 7/10。
- 曾尝试把部分人工复核交给子代理，但两个子任务被模型服务以“可能的网络安全风险”拦截。按 HANDOFF 5.3，本轮没有改写措辞绕过，改由主会话人工复核。

### 指标结果

| Profile | Recall 平均/最差 | Precision 平均/最差 | 接口覆盖平均/最差 | 所选证据复现平均/最差 | 平均耗时 | 平均账本金额 |
|---|---:|---:|---:|---:|---:|---:|
| default 多 Agent | 0.9333 / 0.9167 | 0.7514 / 0.7000 | 1.0000 / 1.0000 | 0.4500 / 0.0000 | 287.9s | 0.187192 |
| single 单 Agent | 1.0000 / 1.0000 | 0.7631 / 0.6667 | 0.9714 / 0.8571 | 0.8500 / 0.5000 | 195.1s | 0.088848 |

按当前评分规则，真实 5+5 的结论是：多 Agent 平均召回提升不足一个问题，未达到“召回率显著提升”的判断标准；本批次 single 在召回、耗时、账本金额和所选证据复现率上均优于 default。default 的唯一稳定优势是接口覆盖均为 14/14；single 有一次只覆盖 12/14，但仍找齐了 P1–P6。

### 问题清单

这里说的“问题”不是测试失败，而是真实评估暴露出的效果问题与流程问题。

| # | 问题 | 证据 | 含义 |
|---|---|---|---|
| 1 | 当前 default 多 Agent 没跑赢 single 基线 | default 平均 recall 0.9333，single 平均 recall 1.0000；default 只有 1/5 次 P1–P6 满分，另外四次各有一个答案只拿半分：run-001 的 P5、run-003 的 P3、run-004 的 P2、run-005 的 P4。single 5/5 次 P1–P6 全满分。 | M5 原本要验证“多 Agent 更不容易漏问题”。这批结果没有支持这个假设；不能说当前多 Agent 方案有效。 |
| 2 | default 的接口全覆盖没有转化成更高召回 | default 接口覆盖 14/14；single 平均接口覆盖 0.9714，最差一次 12/14，但仍每次找齐 P1–P6。 | default 确实更像“扫全接口”，但额外覆盖没有带来额外正确发现，反而出现证据不够精确的半分项。 |
| 3 | default 成本和耗时明显更高 | default 平均 287.9 秒、账本金额 0.187192；single 平均 195.1 秒、账本金额 0.088848。default 约为 single 的 1.48 倍耗时、2.11 倍金额。 | 当前调度、并发探索和 derive 带来了开销；在没有召回收益时，这些开销不可接受。 |
| 4 | default 的可复查证据质量更差 | 所选证据 replay 平均值 default 0.45、single 0.85；default 最差 run 为 0。失败原因包括归档脚本写死旧路径 `/workspace/shared/repo`、依赖历史 token、依赖原服务状态或旧端口。 | Agent 报告能说出问题，不等于留下了可独立重跑的证据。后续要把“可移植复现脚本”作为更硬的产物要求。 |
| 5 | 两组都存在业务规则误报，default 没有更会压误报 | 常见误报包括：把匿名券目录当漏洞、把同一 code 可重复领取当漏洞、把同一请求重复传同一 coupon_id 当列表外独立漏洞。复核口径是：这些行为可以辅助说明 P6 负总额，但目标没有定义“券目录必须私有”“每人只能领取一次”“重复同一参数是独立漏洞”。 | close / report 阶段还不够会区分“已证明的安全问题”和“没有业务约束支撑的可疑行为”。后续提示词或裁定规则应要求先说明业务约束来源。 |
| 6 | 评分与 replay 工具第一版表达力不够，本轮已修 | 原评分只适合一个 finding 对一个分类，不能清楚表达“一段同时命中 P6 又包含一个误报”；原 replay 不支持启动目标服务、seed、额外 helper 文件和 seed token 映射。 | 这会让人工复核变含糊，也会低估或高估 precision/replay。本轮已在 `e0b5cc5` 修正，但它说明评估工具本身也需要继续硬化。 |
| 7 | 子代理复核在安全审核上不可依赖 | 两个复核子任务被模型服务判为可能的网络安全风险；按 HANDOFF 5.3 没有改写措辞绕过，最终由主会话人工完成。 | 后续涉及 mini-shop 这类故意植入漏洞的材料时，不能把“多子代理自动复核”当稳定流程，必要时保留人工复核路径。 |

下一步要查的是 default 为什么产生这些半分和低 replay：按 run 对比事件与报告，统计重复劳动、每类事实对应的证据强度、close 是否过宽、derive 是否在制造噪声，再决定改 profile、调度策略还是证据产物规范。现在不应该直接为 mini-shop 调提示词。

### 偏差与待决

1. 本轮评估没有证明当前 default 多 Agent 方案优于 single 基线；不能据此宣传收益，也不应立刻为了这道题改提示词。下一步应先分析 default 的重复劳动、裁定宽松、证据脚本可复现性和并发成本，再决定是否调整 profile 或调度策略。
2. 复现率只针对人工挑选的归档脚本。部分失败来自旧脚本硬编码 `/workspace/shared/repo`、历史 token 或旧服务状态；失败会降低“可独立复查”指标，但不自动推翻原运行已保存的事实证据。
3. 原始评估产物含完整 workspace/evidence 和目标归档，保留在本机 ignored 目录；提交只保留不含原始输出的精简 checkpoint 与本文指标。
4. 当前 m5 标签仍指向 M5 工具实现提交 `d1f13f5`；本补充是评估检查点，不移动既有里程碑标签。

### 补充提交建议

1. `Harden evaluation scoring and replay review`：`eval/runner/score.py`、`eval/runner/replay.py`、对应测试与 README，保证多 assessment、误报去重和隔离 replay 配置能支撑本次复核。
2. `Record real M5 evaluation checkpoint`：本补充报告、HANDOFF 状态更新与 `.data/checkpoints/m5/real-20260925-01/` 精简结果。
