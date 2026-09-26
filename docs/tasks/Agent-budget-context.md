# Agent-budget-context · 预算由系统管理

来源：用户通过侧边对话明确要求移除 Agent 所见的费用和时间预算数值，保留调度器限制、记账以及 conclude/交接次数。

依据：概念设计3.1、5.2、8.1；实现架构7.1。先读 AGENTS.md。

## 范围与标准

1. 先同步设计并独立提交，再删去上下文生成器中的费用/剩余时间计算及两组 Explore 提示词的预算行。
2. 为旧固定版本 Profile 保留不含数值的 budget_left 兼容值，避免破坏历史模板。新模板不使用此变量。
3. 目标、验收条件、当前意图、黑板、种子认领要求、conclude 和交接调用次数保持；调度的预算/并发/步数/上下文硬限制与费用记账保持。
4. 使用假模型/渲染回归验证预算变化不会改变 Agent 所见内容，旧模板仍可渲染；运行 make check 和 make test-integration，不调用真实模型、不读取.env、不继续被拦截的评测。
5. 报告写到 Agent-budget-context-report.md，按用户已有授权合入main并更新HANDOFF。
