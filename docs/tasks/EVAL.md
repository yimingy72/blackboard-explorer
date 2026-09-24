# 任务 EVAL · 玩具任务与评估任务的目标系统

依据：`docs/design/黑板系统开发方案.md` 第 7 节（7.1 玩具任务、7.2 评估任务 mini-shop）与 M2 的 2.13、M5 的 5.1。

先阅读 `AGENTS.md`（尤其第 7、8 节）。

## 背景与原则

这两个目标系统是给**被测的探索 Agent**（DeepSeek 驱动）用的"考题"。Agent 只能下载打包好的 tar 包，看不到本仓库。因此：

- **tar 包里不得出现任何答案线索**：没有 `BUG`、`FIXME`、`vulnerable`、`race` 之类提示性的注释或命名，没有答案文件、验证脚本。代码要像一个普通团队写的普通项目。
- 答案、验证脚本、评分要点放在仓库中 tar 包之外的目录。
- 目标系统的测试**不能**被根目录的 `make check` / `pytest` 收集（根 `pyproject.toml` 的 testpaths 只含四个 Python 包，保持不变）。

## 范围

只修改 `eval/` 目录，以及 `docker-compose.dev.yml`（新建）、`Makefile`（追加目标）。

## 任务

### V.1 玩具任务：order-service（开发方案 7.1）

- `eval/targets/order-service/`：约 500 行的 Python 3.12 订单服务（库存、下单、订单查询），pytest 测试。
- 植入缺陷：库存扣减是"读—改—写"且未加锁，`tests/test_orders.py::test_concurrent_orders` 偶发失败。**校准**：连续运行该测试 50 次，失败率应在 20%–45% 之间，报告中给出实测数字。
- 两个干扰项：一个与之无关的慢测试；一条看似相关、实际无关的错误日志输出。
- 依赖只用标准库 + pytest（执行环境镜像里会预装 pytest）。
- `eval/tasks/flaky-order-test/task.yaml`（按开发方案 7.1 的内容）与 `answer.md`（缺陷位置、机制、一种稳定复现方法，以及评分要点）。
- `eval/answers/order-service/`：一个验证脚本，证明答案中的"稳定复现方法"复现率 ≥ 90%。

### V.2 评估任务：mini-shop（开发方案 7.2）

- 规格完全按开发方案 7.2：Python 3.12 + FastAPI + SQLite，约 1500 行；`main` 分支的基线功能；`feature/coupon-refund` 分支 4–6 个提交，引入 P1–P6 与干扰项 D1–D3；分支上的现有单元测试全部通过；`scripts/seed.py`；README 说明如何本地启动。
- **git 历史用脚本生成**：你不能写 `.git`，所以把源码组织成"基线 + 按顺序应用的提交"（例如 `eval/targets/mini-shop/src-main/` 加 `eval/targets/mini-shop/commits/01-…/` 覆盖层与提交信息文件，或补丁序列，由你选择），再写 `eval/targets/build.sh`：在临时目录中建 git 仓库、按顺序提交、打包成 `eval/targets/dist/mini-shop.tar.gz`（含 `.git`）。如果沙箱不允许在临时目录执行 git 写操作，把脚本写好并在报告中说明，由 Claude 执行。
- `eval/tasks/mini-shop-review/task.yaml`（按开发方案 7.2）。
- `eval/answers/mini-shop/answer.yaml`：P1–P6 与 D1–D3 的结构化答案（id、类别、文件、函数、机制关键词、触发方式、复现步骤），供 M5 的评分脚本使用。
- `eval/answers/mini-shop/verify/`：对 P1–P6 每一项的验证脚本（启动服务、seed、发请求或并发脚本），证明问题真实存在；对 D1–D3 各一个说明为什么不是问题。并发类问题（P2、P3）的验证需稳定复现（≥ 90%）。

### V.3 分发

- `eval/targets/build.sh` 同时打包 `order-service.tar.gz`。
- `docker-compose.dev.yml`：服务 `eval-targets`，镜像 `python:3.12-slim`，用 `python -m http.server` 只读提供 `eval/targets/dist/`，接入 `exec` 网络（网络别名 `eval-targets`）。这个文件与 `docker-compose.yml` 叠加使用。
- `Makefile` 追加：`make eval-targets`（运行 build.sh）、`make eval-verify`（运行 answers 下的验证脚本；不属于 `make check`）。

## 完成标准

1. 两个 tar 包能生成；解压后 mini-shop 的 `git log --oneline main..feature/coupon-refund` 显示 4–6 个提交；分支上现有测试通过。
2. order-service 失败率校准结果与稳定复现验证结果写入报告。
3. mini-shop 的 P1–P6 验证脚本全部证明问题存在，报告中给出每一项的输出摘要。
4. 检查 tar 包内容：用 `grep -ri` 搜索 `bug|fixme|vuln|race|inject|exploit|answer` 等词，报告中给出结果（应为空或全部是正常业务用词，逐条说明）。
5. `make check` 仍然全绿（未把目标系统的测试引入根测试）。
6. 报告写到 `docs/tasks/EVAL-report.md`：目录结构、如何重新生成、验证结果、对共享文件的修改清单、建议的提交划分、偏差与待决。
