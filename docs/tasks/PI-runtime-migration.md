# PI-runtime-migration

用户要求：用 https://github.com/earendil-works/pi 替换 MAF，尽量复用该仓库相关组件。

依赖当前工作树已经实现且本地部署的 CTF-performance-followup 与 CTF-command-input-feedback；保留这些未合并改动，不回退到 main 的旧实现。

1. 按 `docs/design/PI运行时迁移.md` 核查、选用并锁定 PI 原生组件，记录采用/不采用的实际原因。
2. 替换模型/Agent 执行层，覆盖普通黑板、CTF、成果整理与终态复盘；移除 MAF 生产依赖。Python 保留业务服务、持久事务与执行围栏。
3. 兼容旧会话/工具配对及新 PI 消息；保留模型设置、价格/用量、身份、预算和权限。未知副作用不重放，失败不变成正常完成。
4. 离线与隔离集成验证，不调用真实模型/目标，不读 `.env`，不自动续跑或更新活动环境。
5. 中文报告 `docs/tasks/PI-runtime-migration-report.md` 记录实现、实际检查、兼容限制及建议提交划分。
