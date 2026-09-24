# EVAL 报告

> 本报告由 Claude 补写。Codex 完成了绝大部分实现，但在最后阶段被模型服务的内容审核中断（判定为可能的网络安全风险——mini-shop 是故意植入问题的评估目标），未能写报告。以下验证结果均为 Claude 独立运行所得。

## 完成了什么

| # | 内容 | 状态 |
|---|---|---|
| V.1 | 玩具任务 order-service：库存"读—改—写"未加锁，`test_concurrent_orders` 偶发失败；含一个无关慢测试与一条误导性错误日志；`task.yaml`、`answer.md`、稳定复现验证脚本 | 完成 |
| V.2 | mini-shop：基线 + `feature/coupon-refund` 5 个提交，引入 P1–P6 与 D1–D3；`answer.yaml`；P1–P6 验证脚本与 D1–D3 说明 | 完成（规模偏小，见"偏差"） |
| V.3 | `build.sh` 生成两个 tar 包（mini-shop 含 git 历史）；`docker-compose.dev.yml` 的 `eval-targets` 服务；`make eval-targets`、`make eval-verify` | 完成 |

## 验证结果（Claude 独立运行）

| 检查 | 结果 |
|---|---|
| `make eval-targets` | 两个 tar 包生成成功 |
| mini-shop 提交 | `main..feature/coupon-refund` 共 5 个提交；分支上现有测试 4 项通过 |
| P1 越权读取订单 | bob 读取 alice 的订单返回 200 |
| P2 并发重复退款 | 20/20 复现 |
| P3 优惠券并发重复使用 | 20/20 复现 |
| P4 负数退款 | 退款 -100 返回 200，余额 10000 → 9900 |
| P5 搜索关键字拼接 SQL | alice 的搜索结果中出现 bob 的订单 |
| P6 叠加优惠券应付为负 | 应付 -2000，余额 20000 → 22000 |
| order-service 稳定复现方法 | 100/100 |
| order-service 偶发失败率校准 | 18/50 = 36%（要求 20%–45%） |
| tar 包答案线索扫描 | 搜索 `bug|fixme|todo|vuln|race|inject|exploit|answer|hack|insecure|unsafe|planted|P1–P6|D1–D3`：无命中 |
| `make check` | 50 项通过（目标系统的测试未进入根测试） |

## 偏差与待决

1. **Codex 被内容审核中断**：中断时正在扩充 mini-shop 的正常业务代码，且修改了搜索接口（增加筛选与分页参数），P5 的验证脚本未同步——其输入用 SQL 注释截断语句，连带注释掉了分页占位符，服务返回 500。Claude 已修正验证脚本（`verify/p5.py`），植入的问题本身未改动。
2. **mini-shop 规模小于设计**：Python 代码共 568 行（设计约 1500 行），分支改动 323 行（设计约 500 行）。规模偏小会让单个 Agent 也能一次读完全部代码，削弱评估对"遗漏"的区分度。评估任务在 M5 才使用，安排为 M5 之前的独立任务 **EVAL-2（扩容）**：只增加正常业务代码与测试，不改变 P1–P6、D1–D3，扩容后重跑 `make eval-verify` 与线索扫描。
3. `eval/targets/dist/` 为构建产物，已加入 `.gitignore`，用 `make eval-targets` 重新生成。

## 对共享文件的修改

- `Makefile`：追加 `eval-targets`、`eval-verify` 目标
- `.gitignore`：追加 `eval/targets/dist/`
- 新增 `docker-compose.dev.yml`

## EVAL-2 补充

完成日期：2026-09-25。用户明确将本任务交由 Codex 完成，替代原“人工完成”的安排。本轮只新增正常业务与测试，原评估点及验证脚本保持不变；未调用 DeepSeek。

### 新增内容

- 基线增加账户资料与收货地址：本人资料更新、地址增删改查、默认地址切换/删除后的提升；写操作使用事务，测试包含并发创建与切换默认地址。
- 基线增加商品分类、目录分页、价格/库存/名称筛选、商品详情、分类商品与库存汇总。目录管理仅为本地函数，没有开放未鉴权写接口。
- 基线增加本人订单历史、日期范围汇总、月度统计与已购商品列表；订单金额统计明确不扣除后续退款，所有账户查询限定本人。
- 审查分支增加第 6 个正常提交：`/account/refunds` 和 `/account/coupons` 的分页查询，支持本人订单与使用状态过滤。
- 公共分页参数统一为 `page` / `page_size`，包含总数和 `has_next`。新模块显式关闭 SQLite 连接。
- 打包脚本在构建基线及每个提交层时排除 Python/pytest 缓存；目标代码与本地生成的数据库分离。

### 规模与既有评估点

| 项目 | 扩容前 | 扩容后 |
|---|---:|---:|
| 基线 main 的 Python 行数（含测试与 seed） | 249 | 1434 |
| feature/coupon-refund 的 Python 行数（含测试与 seed） | 568 | 1942 |
| 分支相对 main 的改动 | 323 行（原报告口径） | 512 行新增、4 行删除 |
| 分支提交数 | 5 | 6 |
| 基线普通测试 | 1 | 42 |
| 分支普通测试 | 4 | 54 |

任务要求的“main 约 1500 行、分支改动约 500 行”按各自口径满足。正常业务实现复用既有 FastAPI、SQLite 和鉴权，没有新增依赖。

P1–P6 对应的 detail/refunds/coupons/search 函数、D1 测试辅助、D2 排序、D3 充值函数均未改动。只在两份 app.py 末尾追加新模块注册，因此既有函数行号没有变化；`answer.yaml` 不需要调整，也未修改。新增层只读现有表，不修复、扩大或提示既有问题。

### 实际检查结果

- `make check`：Ruff/Pyright 通过，309 个普通测试通过，36 个集成/live 测试排除。
- `make test-integration`：相关镜像构建通过，32 个集成测试通过；容器自动清理。
- `make eval-targets`：两个 tar 包生成成功，mini-shop 包含 main 与 6 个 feature 提交；无测试缓存或生成数据库。
- 解压后的 feature 分支：54 个测试通过；切换 main：42 个测试通过。两者只有 Starlette 上游弃用警告，不影响结果。
- `make eval-verify`：玩具任务稳定复现 100/100；P1 返回 200，P2/P3 各 20/20，P4 余额 10000→9900，P5 返回另一用户订单，P6 金额 -2000/余额 20000→22000，与原结果一致。
- 任务指定的完整答案线索表达式扫描最终目标全部跟踪文件，无命中。
- 新模块/测试 Ruff 检查通过（沿用 FastAPI 的依赖默认参数写法，忽略 B008）。保护路径的 `git diff --exit-code` 通过。

复跑命令：

```sh
cd ~/blackboard-explorer
export UV_CACHE_DIR=/private/tmp/bbx-uv-cache
export https_proxy=http://127.0.0.1:7897 http_proxy=http://127.0.0.1:7897
export all_proxy=socks5://127.0.0.1:7897 no_proxy=localhost,127.0.0.1,::1,host.docker.internal
uv sync --locked
make check eval-targets eval-verify
make test-integration DOCKER_BUILD_ARGS='--add-host host.docker.internal:host-gateway --build-arg http_proxy=http://host.docker.internal:7897 --build-arg https_proxy=http://host.docker.internal:7897 --build-arg no_proxy=localhost,127.0.0.1,::1,host.docker.internal,mirrors.aliyun.com'

# 独立临时目录中检查打包目标，避免覆盖用户目录。
eval_dir=$(mktemp -d)
tar -xzf eval/targets/dist/mini-shop.tar.gz -C "$eval_dir"
repo_python="$PWD/.venv/bin/python"
(cd "$eval_dir" && SHOP_DB="$eval_dir/local.sqlite3" PYTHONPATH=. "$repo_python" -m pytest tests -q)
git -C "$eval_dir" switch main
(cd "$eval_dir" && SHOP_DB="$eval_dir/main.sqlite3" PYTHONPATH=. "$repo_python" -m pytest tests -q)
```

### 偏差与待决

本轮没有内容审核拦截，没有更改密钥或全局配置。完整目标约 1942 行，是基线扩容到约 1500 行后叠加约 500 行分支改动的结果；不以空行或无用抽象凑数。目标测试仍独立于主项目普通测试，按上面的构建/解压命令运行。

初次线索扫描对测试中的人名产生误匹配，最终改用普通中性样例名后完整扫描通过；这只是目标包的答案线索隔离检查，不涉及模型服务审核规避。

### 共享文件与提交划分

- `eval/targets/build.sh`：只增加缓存目录清理；根依赖、contracts、Makefile、设计文档均未改。
- 提交 1：`Expand mini-shop baseline customer workflows`，包含三个基线模块、测试、README、两处 app 注册和打包清理。
- 提交 2：`Add paginated account history to evaluation branch`，包含第 6 层正常功能及本补充报告。
- 合并后更新 HANDOFF，EVAL-2 前置依赖解除，下一步进入 M5 的运行器、评分和单 Agent 基线。
