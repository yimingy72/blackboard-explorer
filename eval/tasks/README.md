# 评估任务

任务输入定义目标、领域资料和完成条件，通用Profile不预置这些规则。

- flaky-order-test：代码诊断闭环回归。
- attendance-reconciliation：非代码数据核对回归。答案只在本目录，目标压缩包仅含CSV与公开规则。
- mini-shop-review：特定软件审查评测，配套评分器仅用于这个任务。

`make e2e E2E_ARGS='--task attendance-reconciliation'` 选择数据任务，默认仍为flaky-order-test。需要显式提供模型配置，会产生费用；普通检查不调用模型。
