# Agent-budget-context · 预算上下文调整报告

日期：2026-09-26。依据用户侧边对话转达的偏好，由主代理直接实现；未调用子代理。

## 完成内容

1. 概念设计3.1/5.2/8.1、实现架构7.1已先同步，设计提交 `a6ae0ee`。
2. 删除 OpeningContextProvider 计算剩余金额、剩余分钟数的代码，以及 default/single Explore 提示词中的预算行。两组提示词保持一致。
3. 已发布的旧模板若仍引用 `budget_left`，只得到“由系统管理”，不含数值。这样历史固定版本 Profile 仍能渲染，不必改写其正文。
4. 目标、验收反馈、当前意图和黑板保留；种子认领的步数提示与 conclude/交接调用次数保留。调度器继续执行费用、时间、并发、步数和上下文限制，费用/token记账与用户工作台预算显示未变。

## 实际验证

- `uv sync --locked`：通过，未改锁文件。
- `make check`：369 passed、39 deselected；ruff、pyright通过。
- 回归覆盖default/single，以及种子、普通Explore、derive、Close judge/final：大幅改变金额/时长/用量，甚至移除渲染输入中的budget/usage，得到相同Agent上下文；旧budget_left模板仍正常渲染。原目标、交接、裁定和预算执行测试保持通过。
- `make test-integration`：35 passed、373 deselected（104.80秒）；执行环境、代理、runtime及eval镜像构建通过。

复现命令：

```sh
uv sync --locked
make check
DOCKER_BUILD_ARGS='--add-host host.docker.internal:host-gateway --build-arg http_proxy=http://host.docker.internal:7897 --build-arg https_proxy=http://host.docker.internal:7897 --build-arg no_proxy=localhost,127.0.0.1,::1,host.docker.internal' make test-integration
```

代理与UV_CACHE_DIR沿用AGENTS.md。本轮没有调用真实模型，没有读取或修改.env。

## 偏差与待决

- 保留的budget_left是兼容旧模板的非数值说明，新模板不再使用。没有将预算写成虚假的零，也没有削弱调度器限制。
- 本次只移除系统自动注入的费用/时间额度，不过滤用户目标或黑板业务事实中的数值。
- 开始修改前真实评测已经停止，未在运行中修改实验配置。既有被拦截评测仍未补评，不能借本任务宣称其效果已验证。

## 共享文件与提交划分

未修改contracts、Makefile、Compose、依赖或锁文件。

1. `Keep financial and time budgets out of agent context`：设计与任务说明。
2. `Remove budget values from agent prompts and context`：上下文生成器、两组提示词、回归与本报告。
3. `Record agent budget context update`：合并后更新HANDOFF进度与6.1。

本项无需用户手动执行检查。后续继续保留Agent自主语义判重、Explore声明达标、Close裁定并总结的机制。
