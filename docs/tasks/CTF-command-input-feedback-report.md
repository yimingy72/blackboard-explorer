# CTF 执行前参数拒绝被升级为全队失败

日期：2026-10-28；任务 `400d5309-4da8-49be-acc1-de7adbabe66f`，本地版本 `ctf-performance-20261028`。沿用 `feature/ctf-performance-followup` 工作树，保留前期改动。

**源码交付（2026-11-02）**：本轮修复已从原发布快照独立提交为 `1b5b69d`，导出的提交源码重新通过2601项普通检查。后续Pi迁移保持独立，详见[交付报告](PI-runtime-delivery-report.md)。下文为10-28事故和本地发布记录；当前仍未合并main、推送或续跑原任务。

## 事故与证据

- 任务15:34创建，约15:58:57触发system_failure，活动时长1495秒；原预算¥100/80分钟，账本约¥4.60，未到预算或时长限制。
- 首个异常来自member-5、turn `29b8679e-edcb-4955-ba12-2b27fc0e21f6`。失败追踪ID `ac6fdca0-eba9-46cf-9290-a25991aa5717`，ExceptionGroup堆栈落在执行MCP客户端退出阶段，model_error为空；其它仍运行成员随后被全局收尾中断。
- 该成员前23条命令都使用默认cwd；最后一条首次指定 `/workspace/agents/agent-6/e1`，命令开头才准备mkdir同一目录。持久Session里23个命令已完成，最后一个命令 `0733bb4c-6aee-4184-997a-404bb1367266` 只有prepared、没有结果。
- envd先用严格路径解析校验cwd，再启动shell；因此在cwd尚未存在时，shell里的mkdir没有机会执行。原容器已回收，旧日志只记录ExceptionGroup外层堆栈，没有保留子异常文本；不凭此断言网络或模型服务故障。
- 用相同参数模式、无害mkdir/计数命令和旧镜像隔离复现，得到同一MCP ExceptionGroup，子异常是UnknownCommandOutcome，包含执行器返回的FileNotFound拒绝。确认平台错误分级缺陷：可纠正的执行前参数错误被当作未知执行失败，最终关闭全队。

## 修复

1. envd增加私有CommandInputError，仅用于启动任何命令进程之前的cwd、timeout、实际NUL字符校验。保留工作区路径限制，不自动创建目录。
2. MCP对已明确拒绝的输入返回有界结构化反馈：state=rejected、dispatched=false及固定error.code/message；同步和普通模式可直接交给模型纠正，不再通过SDK异常组逃逸。
3. 异步任务同类拒绝保留为已知failed结果，携带未执行证明，不占运行进程槽、不伪称副作用未知；其它进程错误、身份/代次错误及真实未知结果继续沿原保护路径处理。
4. 工具说明明确cwd必须事先存在，创建目录时先省略cwd或使用已存在目录。这是通用执行前置条件，不含特定题目套路。
5. CTF失败诊断增加可选、有界leaf_error_types。只保存类型名，不保存异常消息、RPC正文、命令或凭据；未知组不再只留下一个没有子类型的ExceptionGroup。

未改写原任务失败历史或最后prepared记录，未更改预算、自动重放命令或续跑任务。

## 实际检查

- 修复前旧镜像的隔离复现失败，明确显示FileNotFound→MCP工具错误→UnknownCommandOutcome→ExceptionGroup。
- 定向离线36项通过，包含无进程启动/无audit写入、路径越界仍拒绝、超时/NUL反馈、异步拒绝与取消/读回、类型诊断不泄露正文。
- `make check`：**2601 passed、432 deselected**，Ruff/Pyright通过。
- Ubuntu、Kali各**6项**真实容器/垂直回归通过。分别覆盖同步与异步参数拒绝、修改参数后的新调用执行一次、原同步/异步身份及回收、真实PG/Session/脚本化MAF链路中同turn纠正错误、团队保持running。
- 前端155项通过，生产构建成功；没有调用真实模型或外部目标，测试容器已清理。

对应复现与验证：

```sh
make check
BBX_CTF_TEST_IMAGE=bbx-ctf-input-exec:20261028 .venv/bin/pytest -q services/agent-runtime/tests/test_ctf_command_rejection_integration.py services/envd/tests/test_ctf_container_integration.py services/envd/tests/test_ctf_async_container_integration.py services/agent-runtime/tests/test_ctf_async_real_vertical_integration.py
BBX_CTF_TEST_IMAGE=bbx-ctf-input-kali:20261028 .venv/bin/pytest -q services/agent-runtime/tests/test_ctf_command_rejection_integration.py services/envd/tests/test_ctf_container_integration.py services/envd/tests/test_ctf_async_container_integration.py services/agent-runtime/tests/test_ctf_async_real_vertical_integration.py
```

## 发布与限制

本地已更新为 `ctf-input-feedback-20261028`，blackboard、runtime及Ubuntu/Kali当前执行镜像同步切换；确认无活动任务后备份并部署，数据库保持0020。发布前后22张业务表完整行摘要、17个任务、94份会话及36017个对象（565460714字节）一致。原环境变量、端口、Postgres/MinIO容器与卷保持，CTF配置仍为v20；没有修改profile、预算或失败历史。

部署后核验核心合同、runtime工具说明及诊断源码SHA256与工作树一致，两执行镜像latest指向通过6项检查的候选镜像。登录HTTP200，旧失败任务仍为failed/run 1、预算¥100/80分钟。私有数据备份和回退镜像引用在 `.data/ctf-input-feedback-release/`。

新逻辑只把明确的执行前参数拒绝交给模型纠正，不根据任意错误文本推测未执行，也不将任意ExceptionGroup降级为可重试。真实任务是否继续运行仍由用户决定；此次没有恢复旧任务、调用真实模型或更新公网，源码尚未合并推送。

私有事故证据在忽略目录 `.data/latest-ctf-failure/`，隔离检查在 `.data/ctf-input-feedback/`。建议独立提交 `fix: return pre-dispatch command refusals as correctable feedback`，源码和文档不包含真实凭据或目标命令。
