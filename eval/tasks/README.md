# 评估任务

任务输入定义目标、领域资料和完成条件，通用Profile不预置这些规则。

- flaky-order-test：代码诊断闭环回归。
- attendance-reconciliation：非代码数据核对回归。答案只在本目录，目标压缩包仅含CSV与公开规则。
- mini-shop-review：特定软件审查评测，配套评分器仅用于这个任务。

`make e2e E2E_ENV_FILE=<本地配置文件> E2E_ARGS='--task attendance-reconciliation'` 选择数据任务，默认仍为 `flaky-order-test`。该命令创建隔离的端到端工作台并调用 DeepSeek，会产生费用。不要把本地配置文件提交到 Git。`make check` 和 `make test-integration` 不调用真实模型。mini-shop 的批量评分与 single 配置比较见[评测运行器](../runner/README.md)；其他任务的闭环结果不能套用 mini-shop 专属评分器。
