# 完整 CTF 分支合并报告

任务：`CTF-full-release-20261120`。用户明确要求将完整 CTF 分支合并并提交到 GitHub。

## 1. 完成内容

1. 以远端 `main` 的 `a0085b23ebc0ac046317724dea09221a46fe7291` 为基线，在已有干净工作树建立 `codex/ctf-full-release-20261120`，合并完整的 `feat/ctf-worker-board-20261120`，源提交为 `268aec6b78ac681a56265664322319cbedda803e`。
2. 保留源分支的全部五个提交：实时工作台与启动修复、三次已审批的设计与 Spec 更新、同级 Worker 和直接执行完整实现。包含首次分配与后续自选、同级援助、题内 Fact/Intent、实时 steer、核验反馈、长期会话复用、累计解题提醒、文件证据、日志与费用展示，以及删除 CTF VPN、抓包和相关网络准备/检验链路。
3. 保留 `main` 已发布的费用刷新、文件归档和 Runtime 单任务错误隔离修复，以及新增费用接口和浏览器回归用例。
4. 解决两处文件冲突：Runtime 回归文件保留完整分支的全部测试；工作台保留完整事件订阅集合，并沿用主线更清晰的两秒费用刷新表达式。与源分支相比，产品行为没有新增调整。
5. 以保留提交历史的合并方式发布，完整源分支和集成分支均推送到 GitHub，集成分支通过 PR 合入 `main`。不涉及其他工作树的未提交内容。

## 2. 验证

本次在合并结果上运行；全部使用假模型或本地执行容器，没有调用真实模型 API。

| 检查 | 结果 |
| --- | --- |
| `make check` | Ruff、Pyright、5 项 Pi 测试和 2331 项 Python 测试通过；475 项 integration/live 用例按规则排除 |
| `pnpm --dir web check` | ESLint、TypeScript 和 165 项单元测试通过 |
| `pnpm --dir web build` | 生产构建通过；保留既有大分块提示 |
| Task Runner 测试 | 26 项通过，2 项既有可选测试跳过 |
| PostgreSQL 定向集成 | 25 项通过，覆盖并发领取、援助、核验、恢复、文件/Lead 证据、发布、控制器及费用接口 |
| CTF 直接执行容器 | 1 项通过，覆盖共享网络、文件、执行归属和后台进程收尾 |
| 生产构建浏览器回归 | 6 项通过，覆盖创建、原生会话与费用、桌面/手机题内黑板、CTF 设置 |
| `git diff --cached --check` | 通过 |

复现命令：

```sh
npm --prefix services/agent-runtime/task-runner run pretest
make check
pnpm --dir web check
pnpm --dir web build
npm --prefix services/agent-runtime/task-runner test
.venv/bin/pytest -q \
  services/blackboard/tests/test_worker_pool_integration.py \
  services/blackboard/tests/test_worker_pool_lifecycle_integration.py \
  services/blackboard/tests/test_peer_file_facts_integration.py \
  services/blackboard/tests/test_peer_lead_evidence_integration.py \
  services/blackboard/tests/test_native_ctf_publication_integration.py \
  services/blackboard/tests/test_runner_control_integration.py \
  services/blackboard/tests/test_pi_model_ledger_integration.py::test_ctf_task_owner_can_read_pi_fees
BBX_CTF_DIRECT_TEST_IMAGE=bbx-peer-sandbox:20261120 \
  .venv/bin/pytest -q services/sandbox-execution/tests/test_ctf_direct_integration.py
BBX_E2E_BROWSER='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' \
  BBX_E2E_PREVIEW_DIR=dist pnpm --dir web e2e \
  native-ctf-create.spec.ts ctf-peer-board.spec.ts ctf-live-native.spec.ts \
  ctf-workbench.spec.ts config-workbench.spec.ts \
  --grep 'native workbench|peer board|同一个创建入口|CTF Pi fees|CTF 只读' --workers=2
git diff --cached --check
```

检查日志保存在本工作树 `.data/ctf-full-release-20261120/`，不提交。数据库和执行容器均使用随机主机端口，并由测试自动清理；直接执行验收使用本机已有的完整 CTF 镜像。

## 3. 偏差与待决

没有新增业务设计或需要用户确认的合并决策。完整分支的设计正本更新沿用源分支已有的独立提交，本次没有额外修改设计。

本次未重跑全量浏览器套件和全目录 E2E TypeScript 检查。前次发布已记录旧浏览器行为断言及五条既有 E2E 导入错误；本次验证限于上表的六项受影响浏览器流程。未执行新的真实比赛题、性能压测或本地部署，不以本次合并验证宣称解题速度提升。

没有读取或修改 `.env`、全局配置及其他工作树内容。

## 4. 提交划分与共享文件

采用一个合并提交：`Merge complete CTF worker board release`。包含完整源分支、上述两处冲突处理及本报告，保留原五个提交的历史；正文末尾为 `Implemented by Codex (gpt-6-sol) for task CTF-full-release-20261120.`。

源分支涉及的共享文件为 `packages/contracts` 中协作类型、任务/成员字段、模型调用归属、Runner journal、Sandbox 工具合同及其 JSON Schema；生成的 OpenAPI 与前端类型一并合入。本次没有额外改动根 `pyproject.toml`、锁文件、`Makefile` 或 `docker-compose*.yml`。

## 5. 下一步建议

后续部署使用本次合并后的完整版本，并按照现有部署文档同步 direct Sandbox 镜像及发布配置。真实比赛题性能评估继续区分冷启动、事件通知、模型耗时、工具耗时和暖会话换题耗时。
