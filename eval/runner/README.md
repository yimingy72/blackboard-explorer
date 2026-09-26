# 评估运行器

M5 比较同一目标上的 `default` 与 `single`。两份 Profile 只有 `derive_enabled` 不同；single 的任务并发为 1，close 仍负责裁定与终结。每批固定 Profile 版本，结果保留完整配置与预算。命令不向 Agent 提供答案目录。

## 准备与运行

普通测试 `make check` 使用 HTTP 替身，不读 `.env`、不访问真实模型。以下 `eval-run` 是显式付费操作：默认两组各 5 次，每次任务预算最多 10 个配置币种单位、60 分钟。当前默认价格币种为 USD，实际费用按服务商计费。

```sh
uv sync --locked
pnpm --dir web install --frozen-lockfile --store-dir /private/tmp/bbx-pnpm-store
make image-blackboard image-agent-runtime image-eval-env
make eval-run EVAL_OUT=eval/results/experiment-01 EVAL_N=5
make eval-score EVAL_OUT=eval/results/experiment-01
```

构建代理参数沿用 envd README。`bbx-eval-env:latest` 基于已有执行环境，仅预装 mini-shop 依赖；两组使用同一镜像。密钥从进程环境传入。已在本地 `.env` 配置时，可以显式传 `EVAL_ENV_FILE=/Users/yym/blackboard-explorer/.env`，由 uv 子进程加载；不要将密钥贴入命令或报告。`eval-run` 使用独立 Compose 项目、随机 localhost 端口、隔离卷，结束或失败时停止运行器并清理本次执行容器、服务及卷。

不传 `EVAL_OUT` 时，两条 Make 命令都使用 `eval/results/latest`；目录已经存在会拒绝覆盖，下一批请换名称。`eval/results/` 已忽略。不会自动推送结果。

连接已有黑板时可运行一组：

```sh
# SERVICE_TOKEN 必须来自已有服务对应的进程环境。
uv run --no-sync python -m eval.runner.run \
  --task mini-shop-review --profile default --n 5 \
  --base-url http://127.0.0.1:58000 --exec-image bbx-eval-env:latest \
  --out eval/results/experiment-02/default
```

`--profile single` 创建单 Agent 组；`--timeout` 控制每次运行等待上限，超时会请求停止并留出交接/归档时间。失败、超时、导出不全均不会返回成功。已有服务必须提供 eval-targets，并允许执行环境访问它；建议优先使用隔离入口。

## 结果文件

每组下有 `run-001` 等目录：

- `run.json`：task_id、固定 profile/version、完整配置、预算、status/outcome、耗时及导出错误。
- `state.json` / `events.json`：最终黑板投影与完整事件。
- `report.md` / `workspace.tar.zst`：终结报告与归档；未产出时保留明确的非成功状态。
- `evidence/index.json`：完整对象 URI 到本地 SHA-256 文件名的映射，不使用对象路径拼宿主文件名。
- `interfaces.json`：隔离入口根据目标 main..feature 的新增/修改路由函数生成的预期接口清单。

批次根还保存 `target.tar.gz`、目标 SHA-256、批次配置和脱敏日志。独立运行器没有批次接口清单时，复核者需依据对应目标的差异填入预期接口。

## 自动候选与人工复核

`make eval-score` 先生成每次 `score.json`、`review-template.json`，再生成根目录 `comparison.md`。自动按文件、函数和机制关键词提出候选，优先列出有 satisfies 或持久化工具证据的事实。它不会把自动匹配当作确诊，也不会将报告中否定干扰项的句子自动算成误报。

把 `review-template.json` 复制为 `review.json`，逐项填写：

- `classification`：`problem`（已知 P 问题）、`distractor`（报告把 D 当问题）、`new`（列表外已确认的新发现）、`false_positive`（列表外经复核不成立的发现）或 `ignore`（背景、重复叙述、明确排除的问题）。实际提出但不成立的发现须记为 `false_positive`，不能用 `ignore` 从精确率分母中移除。
- `answer_id`：problem 填 P1–P6，distractor 填 D1–D3。
- `location`、`mechanism`、`reproducible`：problem/new 由复核者填写布尔值；定位和机制齐全计 0.5，再有可复现证据计 1。
- `unique_key`：同一个列表外新问题或误报跨事实/段落重复时使用同一值；已知 P/D 按答案 ID 自动去重。`false_positive` 不需 `answer_id`，计入精确率分母但不计真阳性。
- `checked_interfaces`：逐一确认报告实际检查过的 `METHOD /path`。如存在导出的 `interfaces.json`，它固定覆盖率分母，review 不能缩小分母。

一条事实或报告段落可包含多个独立发现。此时在该 finding 下填非空 `assessments` 列表，每项沿用上述分类、答案编号和证据布尔字段；列表中**每项**都有效才算该 finding 已复核。例如：

```json
{
  "id": "F2",
  "assessments": [
    {"classification": "problem", "answer_id": "P4", "location": true, "mechanism": true, "reproducible": true},
    {"classification": "problem", "answer_id": "P5", "location": true, "mechanism": true, "reproducible": false}
  ]
}
```

旧的单项 `classification` / `answer_id` 格式继续有效；模板中 `assessments: null` 表示仍可直接填写这些旧字段。多个 `new` 或 `false_positive` 项如果没有 `unique_key`，会按 finding ID 与列表位置分别计数；确属同一个发现时显式填写相同 `unique_key` 去重。P/D 始终按 `answer_id` 跨列表和跨 finding 去重。

随后重跑 `make eval-score`。原 `review.json` 不会被覆盖；自动模板会更新。所有事实和报告段落都需分类，防止只确认命中的条目而忽略潜在误报。

复核先判断该段是否在主张缺陷，再判断证据是否支持它。实际观察、利用步骤、明确待查的假设不自动成为独立问题；重复叙述或同根因变体合并，而不是计为误报。答案列表不是完整的缺陷清单：若修复已知问题后另一触发仍成立，应按独立不变量、位置、机制与证据判断 `new`。没有已知答案编号、补证尚未完成或被合并，都不能单独作为 `false_positive` 的理由。肯定性的错误或无依据缺陷主张仍需计入复核，即使最终报告省略或改称“观察”；在复核理由中区分已被反证与缺少业务规则依据。

发现历史复核口径错误时保留原始结果，在副本中同时复核 default/single，并记录逐项旧分类、新分类、证据与理由。新旧实验应采用同一口径；不能用口径修正带来的分数变化宣称提示词或架构提升。

## 显式证据重跑

不会自动执行报告里的任意命令。为单次运行写 `replay.json`，选择归档里可独立运行的 Python 脚本及可观察结果：

```json
{
  "scripts": [
    {
      "id": "F1-proof",
      "path": "agents/agent-1/reproduce.py",
      "service_port": 8021,
      "seed": true,
      "files": ["agents/agent-1/repro/common.py"],
      "expected_exit_code": 0,
      "expected_output": "observed result: 2"
    }
  ]
}
```

```sh
make eval-replay EVAL_OUT=eval/results/experiment-01 \
  EVAL_RUN=eval/results/experiment-01/default/run-001
make eval-score EVAL_OUT=eval/results/experiment-01
```

每项新建无外网、非 root、只读根目录的容器，限制内存/CPU/进程数；只读挂载显式选择的脚本、附件和保存的目标。目标解包到 `/workspace/shared/mini-shop`，该目录也是 cwd/PYTHONPATH；归档原脚本原文复制到 `/workspace/<path>`，其父目录会创建，不改脚本内容。依赖预装，不继承宿主环境密钥，不挂 Docker socket。归档脚本必须是正常相对路径、非链接、完整 UTF-8 Python 文件且小于 200 KiB；默认 120 秒后停止并清理。

`service_port` 可选，范围 1024–65535；设置后先按 `seed`（默认 true）运行目标原 `scripts/seed.py`，再在容器内 `127.0.0.1:<service_port>` 启动 `uvicorn shop.app:app`，等待 `/products` 就绪，执行原脚本后停止服务。未设置端口沿用无需服务的旧行为。`files` 可显式列出归档内至多 16 个额外普通 UTF-8 文件，每个最多 200 KiB、合计最多 2 MiB；路径仅可在 `agents/` 或 `shared/` 下，不得覆盖目标 `shared/mini-shop` 或主脚本。文件逐个原文复制到 `/workspace/<path>`，不会恢复原运行时的数据库、虚拟环境或任意旧状态，也不接受 shell setup。

若原脚本写死了目标 seed 用户的旧测试 token，可在 `seed: true` 且指定端口时附加 `seed_tokens: {"alice": "<32位十六进制>", "bob": "<32位十六进制>"}`。此设置仅在全新数据库运行原 seed 后，以参数化 SQL 更新这两个测试用户的 token，不改变余额或订单；不可填 API key。`replay-results.json` 记录端口、seed、附件列表、主脚本和附件 SHA-256、测试 token 的 SHA-256、退出码与受限输出，不保存 token 明文。

脚本必须自行建立测试数据/服务并验证对应现象；依赖原进程、原端口或额外文件的脚本会明确失败，不能算已复现。当前只支持独立 Python 脚本，未选择或未执行的证据记为“未测”。重跑成功表示所选脚本的退出码和输出符合声明，不替代对问题机制的人工确认。

## 指标与结论

- 召回率：每个 P 的最高确认分（0/0.5/1）之和除以 6。
- 精确率：已确认有效发现数除以全部已确认问题数；已确认 D 是误报，P/D 按 ID 去重，列表外发现需人工确认。同一 P 只有定位和机制仍是有效发现，但召回只计半分。
- 复现率：显式选中并执行的脚本中通过的比例，未运行是 null。
- 接口覆盖：确认检查的接口与预期变更接口交集除以预期接口数，空分母是 null。
- 时间：运行器记录任务运行时段，导出时间单列；金额使用状态账本；缓存命中率为 hit/(hit+miss)。
- 过程：争议事件数、裁定次数、最终仍 met 的证据声明占全部 satisfies 声明的比例、Agent 结束原因分布。

对比报告列两组的平均值、最差一次和各自样本数；失败/缺失值不伪造为零。至少两组各 5 次且复核/复现/覆盖数据完整，才判断平均多找到至少一个问题。召回持平时报告实际耗时变化，“明显更短”的门槛留给人工判断。不得用模拟数据声称多 Agent 在真实任务上占优。
