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
