# 任务 EVAL-2 · mini-shop 扩容

依据：`docs/design/黑板系统开发方案.md` 第 7.2 节；`docs/tasks/EVAL-report.md`（偏差第 2 条）。

在 M5 之前的任意时间进行，不依赖其他任务。

## ⚠️ 执行方式

EVAL 任务曾被 Codex 的模型服务以"可能的网络安全风险"中断（mini-shop 故意植入越权、注入等问题，并附验证脚本）。本任务**只增加正常业务代码**，但仍可能被拦截。**不要改写措辞绕过审核**。建议：

- 由人完成；或
- 交给 Codex 时只给它 `eval/targets/mini-shop/src-main/` 与 `commits/` 中**与植入问题无关**的文件范围，不让它读取 `eval/answers/`。

## 现状

Python 代码共 568 行（设计约 1500 行）；`feature/coupon-refund` 分支改动 323 行（设计约 500 行）。规模偏小时，单个 Agent 一次就能读完全部代码，削弱评估对"遗漏"的区分度。

## 要求

1. `main` 基线增加正常业务模块（例如：商品分类与库存管理、收货地址、用户资料、订单状态流转与取消、管理端统计），使 Python 代码总量约 1500 行，风格与现有代码一致，并配套测试。
2. `feature/coupon-refund` 分支增加 1–2 个与植入问题无关的正常提交（例如：退款记录查询、优惠券列表分页），使分支改动约 500 行。
3. **不改变 P1–P6 与 D1–D3**：涉及的文件与函数保持原样；如果新增代码与它们相邻，不能引入提示性的命名或注释。
4. 同步 `eval/answers/mini-shop/answer.yaml` 中可能变化的行号信息（如有）。

## 验证

```sh
make eval-targets
make eval-verify          # P1–P6 全部仍然成立
# 解压 dist/mini-shop.tar.gz：分支现有测试全部通过；git log 提交数 6–8
# 线索扫描（应无命中）：
grep -rniE 'bug|fixme|todo|vuln|race|inject|exploit|answer|hack|insecure|unsafe|planted|\bP[1-6]\b|\bD[1-3]\b' <解压目录> --exclude-dir=.git
```

## 报告

追加到 `docs/tasks/EVAL-report.md` 的"## EVAL-2 补充"：新增内容、代码量前后对比、验证结果。
