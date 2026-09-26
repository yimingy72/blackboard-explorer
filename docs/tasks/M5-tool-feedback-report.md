# M5-tool-feedback · 工具参数错误反馈报告

日期：2026-09-26。从 M5-E2 提交 `5527dcc` 独立提取运行时修复；以 main `285a5f2` 为基线。

## 完成内容

- MAF 在工具执行前发现参数格式错误时，返回字段路径，例如“缺少必填字段 evidence[0].type”，让 Agent 按原 schema 修正后重试。
- 无效参数仍被拒绝，不补默认值、不降低证据或引用校验；失败调用仍持久化，错误反馈不回显参数值。
- 复用现有 ToolLogMiddleware，未新增依赖、配置或服务。调度、语义判重、Explore 声明与 Close 裁定保持 main 原有行为。
- 本提交只完成通用框架修复。M5-E2 的提示词实验、评测草稿和原始数据仍留在 `m5-e2` 工作树，不将未完成复核用于效果结论。

## 验证

- `uv sync --locked`：通过，锁文件无变化。
- `make check`：359 passed、39 deselected；ruff/pyright 通过。
- 回归使用真实 MAF 与脚本化假模型：缺字段时无黑板写入且有持久化错误反馈，修正后恰好写入一次；非法枚举及额外字段仍被拒绝，反馈不回显测试输入值。
- `make test-integration`：35 passed、363 deselected（107.94秒），所需镜像构建通过。
- `bbx-agent-runtime:latest` 已按本分支重新构建，使用 main 原有提示词；实验镜像另保留为 `bbx-m5-e2-runtime:3c17f81`。

复现命令：

```sh
uv sync --locked
make check
DOCKER_BUILD_ARGS='--add-host host.docker.internal:host-gateway --build-arg http_proxy=http://host.docker.internal:7897 --build-arg https_proxy=http://host.docker.internal:7897 --build-arg no_proxy=localhost,127.0.0.1,::1,host.docker.internal' make test-integration
```

代理与 UV_CACHE_DIR 沿用 AGENTS.md。本任务未调用真实模型，未读取或修改 `.env`。

## 偏差与待决

- 修复符合实现架构6.2已有的“失败返回原因和修正方法”，无需改变设计。
- 错误识别依赖已锁定 MAF 的参数校验异常链，由真实框架回归测试守护；其他内部错误继续只返回异常类型。
- 该修复已具备独立验证，不依赖被拦截的评测。M5-E2整体精确率、召回变化和成本收益仍未验证完，不能据此声称优化已全部完成。

## 共享文件与提交划分

无共享 contracts、Makefile、Compose、依赖或锁文件修改。

1. `Fix actionable tool argument feedback independently`：中间件、回归测试、本任务说明和报告。
2. 合并后 `Record completed tool feedback fix`：更新 HANDOFF，说明已合入的框架修复和仍保留的实验分支。

无需用户手动执行本修复的检查；未完成的评测复核继续明确标为未验证。
