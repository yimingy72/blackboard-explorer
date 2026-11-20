# 工作台费用与历史文件归档修复

## 发布内容

- 已登录用户在 CTF 工作台查看 Pi 费用时，任务模式检查曾误回 `409 mode_mismatch`。费用查询现可到达原有的只读接口。任务运行期间，工作台每两秒更新费用。结算事件、预算事件和手动刷新也会重新读取费用。
- 历史文件归档同时支持旧文件名和以 SHA-256 命名的成员快照。创建归档时核对快照名称、声明的哈希和实际内容，同一路径按登记版本恢复最新文件。
- 单个任务的归档数据校验失败时，Runtime 记录任务 ID 和异常类型，继续处理其他任务。错误日志省略文件内容。

## 验证

在新工作树中按锁文件安装 Python、前端、Pi 和 Task Runner 依赖，并执行 `npm --prefix services/agent-runtime/task-runner run pretest` 生成测试所需的本地产物。

```sh
make check
pnpm --dir web check
pnpm --dir web build
.venv/bin/pytest -q services/agent-runtime/tests/test_task_archive.py services/agent-runtime/tests/test_native_runner_recovery.py
.venv/bin/pytest -q services/blackboard/tests/test_pi_model_ledger_integration.py::test_ctf_task_owner_can_read_pi_fees
pnpm --dir web exec playwright test e2e/ctf-workbench.spec.ts --grep 'CTF Pi fees'
```

`make check` 通过，包含 2,316 项 Python 测试和 5 项 Pi 测试。前端 160 项单元测试及生产构建通过。PostgreSQL 集成用例验证了费用接口的登录要求和美元估算值。浏览器用例在开发服务与生产构建上均通过，覆盖运行中自动刷新及停止后的结算更新。本机使用 Chrome，通过 `BBX_E2E_BROWSER` 指定可执行文件。归档与 Runtime 的 35 项定向检查通过。

## 合并范围

发布分支从 `main` 的 `2d7cf1b` 建立，提取本地已提交的维护改动。代码、对应回归用例和本报告放在同一笔 `Fix task fee refresh and archive validation` 提交中。

另行运行全目录端到端测试的 TypeScript 检查时，已有四个测试文件共报出五条模块或类型导入错误。在原 `main` 上复核得到相同结果。本次修改的浏览器用例单独通过了类型检查。
