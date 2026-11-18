# 已提交分支合并与 GitHub 同步报告

## 完成内容

- 用户明确选择合并并上传全部已提交分支。整合从 GitHub `main` 的 `5cdd480` 开始，合入 Pi 平台分支 `feature/p6a-pi-model-runtime` 的 `b91878e`，覆盖本地 `main`、CTF 自动派工分支及其已提交祖先。
- 保留 `349cecf`、`5cdd480` 的会话刷新、归档归属、附件读取和私有事件权限修复。两处冲突按双方契约合并。附件读取同时执行任务身份、模式、成员代次校验和原生共享附件限制，增加两个原生任务拒绝用例。
- Task Runner 的合成进程增加 `GIT_CEILING_DIRECTORIES`，阻止 helper 的 Git 差异快照遍历到外层开发仓库。修复合并期间大量暂存差异造成的事件循环阻塞、helper 截止超时和测试失败。
- 剩余 `feature/ctf-toolchain` 的两个提交仅补充已有工具链发布记录，随后单独合并。

## 验证

在本报告所在仓库根目录执行。

```sh
make check
make web-check
make web-build
npm --prefix services/agent-runtime/task-runner test
UV_CACHE_DIR=/private/tmp/bbx-uv-cache make -C services/sandbox-execution check
BBX_E2E_BROWSER='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' \
  pnpm --dir web exec playwright test e2e/conversation-continuity.spec.ts --workers=1
git diff feature/p6a-pi-model-runtime --check
```

- Web 检查和构建通过，160 项单测通过。
- 会话连续性与复盘刷新浏览器回归 5 项通过，使用模拟 API。
- Sandbox 执行包格式、lint、Pyright 通过，8 项普通测试通过，6 项集成测试按标记排除。
- 合并冲突相关附件、归档与 CTF API 定向测试 62 项通过。
- 首轮 `make check` 得到 2,312 项通过、1 项 helper 测试失败、454 项按标记排除。修复合成进程的 Git 搜索范围后，最终 `make check` 全绿，Ruff、Pyright、5 项 Pi 测试和 2,313 项 Python 测试通过，454 项按标记排除。
- Task Runner 的 24 项普通测试通过，2 项需要私有 Runtime 的用例由独立入口运行。Python 全量检查包含这两项的模拟供应商回归，其中原失败的 helper 用例单独复验也通过。
- 以 Pi 分支为基准，本轮新增差异的空白检查通过。Pi 分支原有的固定摘要补丁文本包含空白告警，保留其已校验的原始字节。

## 偏差与待决

- 平台、UI、配置等旧工作区的未提交内容继续保留在原工作区。本次发布范围由用户明确选择为已提交分支。
- 本轮验收覆盖离线检查、模拟服务和浏览器回归。真实模型调用与 Docker 集成环境验收另行安排。
- 本轮自行增加的实现修改包括冲突整合、两个附件权限用例及合成测试进程的 Git 搜索范围。

## 提交划分

1. `Merge Pi platform into maintenance mainline`，合入 Pi 分支、维护修复、冲突整合、测试隔离与本报告。
2. `Merge remaining CTF toolchain release records`，合入工具链旁支的两项文档提交。

## 下一步

验证全部本地分支均为 `main` 的祖先，推送 `origin/main`，再核对 GitHub 分支头与本地提交一致。
