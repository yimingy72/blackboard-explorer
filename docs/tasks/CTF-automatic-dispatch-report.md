# CTF automatic 第一阶段实施记录

工作树：`/Users/yym/bbx-wt/platform-performance-audit`；功能分支：`feature/ctf-automatic-dispatch`，从 `feature/pi-runtime-migration` 创建。不推送、不合并 main，不读取 `.env`，所有模型边界使用测试替身。

## 设计审核

提交 `12a9065`：审阅并修订 v3 规格。修正时区、manual 兼容范围、discover 流式派生的前置、同源去重、冻结模板/资源授权、累计配额、附件读取边界。没有需要用户另行决定的阻塞项。v2 草稿未修改、未提交。

基线 `make check`：2605 passed、438 deselected，159.50 秒，Ruff/Pyright/Pi 原生检查通过。

## 步骤 1：停止竞态与 automatic 故障准入暂停

完成：

- runner 的直接成功结算回执也核对权威停止。drain 期间已被停止的同一 turn 不再因本地 completed 与持久 stopped 不同而误收整场；无证据的结果差异仍报错。
- automatic 的失败记录写入 task 控制状态，围栏并停止确切故障成员，保留检查点、执行和费用。确认前暂停新增 claim/执行准入；健康会话等待明确的准入拒绝，不重放结果未知的工具。
- 只有匹配 turn/generation 的停止证明、已处理的检查点/outbox 和平台调用状态才能解除成员级暂停。unknown 费用继续保留，不要求清零。Lead 或任务级未知错误保持任务级暂停，不伪造可恢复证明。
- manual 的未分类异常关闭语义和原测试断言保持。显式续跑清除旧的活动暂停标志，保留故障历史。

改动文件：runtime `ctf/client.py`、`coordinator.py`、`runner.py`；blackboard `ctf.py`、`ctf_api.py`、`ctf_lifecycle.py`、`ctf_platform.py`、新增 `ctf_failures.py`；三份针对停止与暂停的测试。

实际验证：

- 定向离线：107 passed，7.14 秒；全部使用假模型/内存或 HTTP 替身。
- 隔离本地 PostgreSQL：`test_ctf_failure_hold_integration.py`，3 passed，3.59 秒；复用预置 pgvector 镜像和随机端口的 testcontainers，已清理容器。覆盖停止前不放行、停止证据后放行、unknown 费用保留、任务级未知不自动解封、manual 拒绝新暂停入口。
- 提交前 `UV_CACHE_DIR=/private/tmp/bbx-uv-cache make check`：2609 passed、441 deselected，158.32 秒；Ruff、Pyright 与 Pi 原生检查通过。

本步限制：automatic 的公开配置合同由步骤2加入；任务级无法证明安全的暂停不自动解除，需要检查后停止/显式续跑。没有实际模型、真实 VPN/靶场或生产数据库写入。

复现命令：

```sh
.venv/bin/pytest -q services/agent-runtime/tests/test_ctf_turn_transport.py services/agent-runtime/tests/test_ctf_failure_hold.py services/agent-runtime/tests/test_ctf_runtime.py services/agent-runtime/tests/test_ctf_close_coordinator.py -m 'not integration and not live'
.venv/bin/pytest -q services/blackboard/tests/test_ctf_failure_hold_integration.py -m 'integration and not live'
UV_CACHE_DIR=/private/tmp/bbx-uv-cache make check
```

后续按规格顺序继续合同、计划/派工、首轮兜底、事件合并、sudo 和 UI/发布。共享 VPN 是主切片之后的独立项。本文件按每一步的实际结果更新，不把待开发项记为完成。

## 步骤 2：合同与旧快照兼容

新增 `dispatch_mode`、首轮限制、工作项/冻结 solve 模板、计划 CAS 请求和有界附件读取合同。旧快照缺字段视为 manual，序列化保留旧 Profile 摘要；显式 manual 仍可覆盖 automatic。增加公开附件 ID 的创建入口和持久记录，未分类附件不自动作为公开正文。

范围验证覆盖模板同源、资源不扩张、累计派生数量上限、禁止等待 discover 自己完成、重复工作 ID 与单次20项上限。automatic Lead 配置拒绝执行工具和 MCP。旧工具配置保留。

改动：contracts `ctf.py` 及生成 schema、一份精简合同测试；blackboard `api.py`/`ctf.py` 创建附件可见性入口。仅将本次步骤1新增的 automatic 测试改为直接使用公开合同，旧 manual fixture 未动。

本步只暴露合同，不自动发布或启用新配置；后续功能与界面尚需逐步接通。

实际验证：全量离线检查的 Ruff/Pyright/Pi 均通过，2612项测试通过，唯一失败是新增创建字段后的 OpenAPI 快照未生成。生成快照后，精确重跑快照与本步合同测试：5 passed，1.44秒。遵照用户最新减少重复测试的要求，不为生成物修正再重复运行已经通过的2612项；后续每步以受影响检查为主，集成完成时再跑一次全量。
