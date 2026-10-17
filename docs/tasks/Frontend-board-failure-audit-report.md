# 前端实时性能与近期失败审计报告

日期：2026-10-17。范围为 CTF 与普通黑板任务页的实时数据链路、任务失败归因，以及运行时错误日志。

## 近期失败根因

公网最近 3 天的失败任务中，`1da0366d`、`910dac90`、`b3e0db23`、`f331a882` 和 `0ef17097` 的最后失败都包含 HTTP 402 `payment_required`；`0ca486c9` 与 `4459ea9e` 的模型错误追踪也记录了同一 402。这不是黑板调度或执行容器故障，模型服务返回的是余额不足。

原归因有两个缺口：

1. 普通黑板达到连续失败阈值后只写“连续运行失败达到保护阈值”，丢掉了最后一次安全的模型错误类别。
2. CTF 把 402 显示为“模型调用异常，具体原因未确认”，且失败诊断契约不能携带安全的模型错误分类。

本次在任务失败事件中增加可操作原因，例如“模型账户余额不足（HTTP 402）”；CTF 失败诊断新增受限的 `model_error` 字段，只允许分类、HTTP 状态、供应商错误码、request ID、阶段和异常类型等固定字段，不保存供应商原文、提示词或凭据。

运行时进程此前没有配置 Python logging handler，容器日志中看不到模型错误、调度恢复等 warning。现在 CLI 启动时输出结构化前缀日志；未知本地异常还会在受控运行日志中记录实际异常类名，事件与 API 仍只暴露固定安全元数据。

## CTF 前端的重复请求风暴

使用公网已完成任务 `c70cb465-c9e7-47b9-a408-af49d05a4f59` 测量：

| 接口 | 当前响应体 |
|---|---:|
| `/ctf/state` | 420,499 字节 |
| `/ctf/challenges?limit=500` | 14,676 字节 |
| `/events?since=0` | 2,396,810 字节 |

原页面每 3 秒读取一次 state 和 challenges，每 10 秒读取一次完整事件历史；每个 SSE 批次又在 250 毫秒后失效整个 `['ctf', taskId]` 查询族。任务越大，重复下载和 React Query 失效风暴越明显。

现在的行为：

1. CTF state 在读取快照前返回 `event_version`，浏览器从该版本建立 SSE，不再为了得到游标先下载完整历史。
2. 成员、题目、记录和产物事件按版本合并进 React Query 快照，每 150 毫秒最多提交一次状态更新。
3. 完整事件历史改为按需加载：打开题目、Agent 会话、调度记录或系统事件时加载一次，之后与最近 512 条实时事件去重合并。
4. 单独的 `challenges` 查询已移除，初始和增量题目都来自 state 及其事件快照。
5. state 轮询仅作为 30 秒一致性兜底，终态任务停止轮询；对话消息仍按 3 秒更新，体积较大的原生 Session 从 3 秒改为 10 秒刷新。
6. 普通黑板的 Agent 编号直接由当前快照计算，不再为每次实时更新扫描全部历史事件；任务详情轮询从 5 秒改为活动期 10 秒。
7. 黑板任务页按任务模式懒加载 CTF 工作台和关系画布；任务详情首屏脚本从约 801 KiB 降到 508 KiB，ELK 布局包只有进入普通黑板画布时才加载。
8. 修复窄屏工作台因内部横向滚动导致主内容整体左移的问题；画布在小屏隐藏缩略图，避免缩略图遮挡节点点击。
9. 快照现在包含最新 `task.closing` / `task.stopped` 的原因，刷新页面后不会丢失“验收完成 / 用户停止 / 预算收尾”等结束依据。
10. MCP 清单读取按钮在 loading 时保留稳定的可访问名称，避免按钮标签在读取过程中消失。

## 验证

- `make check`：2163 passed，355 deselected；ruff、pyright、普通 pytest 全绿。
- `make test-integration`：351 passed，2167 deselected，572.61 秒。
- `pnpm check`：136 passed，24 个文件；ESLint、TypeScript、Vitest 全绿。
- Playwright 全量 E2E：122 passed。
- 新增前端测试覆盖事件去重、512 条实时缓冲、快照折叠及纯 trace 事件不触发快照重算。
- 新增后端测试覆盖任务级 402 原因、CTF 安全模型错误诊断和 state 事件游标。

## 偏差与待决

- 402 需要充值或切换可用模型；代码只能准确归因，不能恢复供应商账户。
- `0ef17097` 的 Derive Session 后续出现 21–29 ms 的本地 `OtherException`，没有 HTTP 状态。新的运行日志会记录实际异常类名；本轮没有删除或覆盖该任务的 Session，也没有自动重开已关闭的 Intent。
- 历史任务不会回写既有失败事件；新的失败归因只影响后续运行。

## 部署

- 本轮功能代码：`8b48fd375e9df49485d7dfc25d2db2a45ad21c0f`，已推送到 GitHub；本报告的部署记录随后作为 docs-only 变更合并到 `main`。
- 本地 CTF 环境：`bbx-ctf-blackboard:latest` 与 `bbx-agent-runtime:latest` 已重建，只替换 blackboard/runtime；`http://127.0.0.1:58000/login` 返回 200，容器内前端入口 SHA-256 与本地构建一致。
- 公网环境：release `/opt/blackboard-explorer/releases/8b48fd3`，运行 `bbx-blackboard:8b48fd3` 与 `bbx-agent-runtime:8b48fd3`；更新前数据库备份到 `/opt/blackboard-explorer/private/update-8b48fd3-20261017T141404Z/database.dump`。
- 公网部署前活动任务数为 0；只重建 blackboard/runtime，Postgres、MinIO 和其他项目未重启。登录页返回 200，CTF state 返回 `event_version=42220`，黑板完成态快照返回 `closing_reason=manual`。
- 已清理本地和公网旧应用镜像；本机构建缓存额外回收约 4.09 GB，容器镜像约 412.8 MB。当前版本镜像保留 `latest` 与 `8b48fd3` 标签。

