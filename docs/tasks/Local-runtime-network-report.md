# 本地最新任务创建失败修复报告

日期：2026-10-16。工作树：`/Users/yym/bbx-wt/local-runtime-repair`，分支：`fix/local-runtime-network`。

## 故障原因

最新任务 `323fe82f…`（普通黑板模式）于北京时间 10:41 启动后，在 **创建执行环境** 阶段失败。只有 task.created、task.provisioning、task.failed 三类事件，started_at 为空、run_started=false、Agent 数为 0；没有调用模型，也未执行任务目标。

运行记录与 Docker 状态交叉确认：

- 旧 runtime 实际 ID 为 `9180de464a3a…`，hostname 却为 `70b600b93ba1`，来自之前更新时复制的旧容器配置。
- runtime 已实际接入 `bbx-ctf-team-mode_exec`，端点 MAC 与容器实际接口一致。
- `_execution_network` 把 hostname 当作容器 ID 查端点，因此误报“未接入执行网络”。这是此前本地更新与校验实现的兼容问题，与目标描述和模型服务无关。
- 失败后 `find()` 又先校验网络，再判断是否存在执行容器；尽管本次根本没建出容器，清理仍反复触发同一个错误，cleanup_ready 一直为 false，并占用调度槽位。

## 修改

1. 取消用 hostname 或其字符串前缀推测容器 ID 的分支，统一通过本地实际接口 MAC 与指定 Docker 网络端点核验。跳过不可读取、空和全零地址；无匹配仍拒绝，不因 hostname 自定义而跳过网络检查。避免短 hostname 恰好命中其他容器 ID 前缀。
2. `find()` 先查权威任务标签。没有执行容器则返回 None，归档只需读取已持久化数据；存在容器时仍核验网络，再启动/恢复。provision 继续在创建前验证网络类型和实际接入，不放宽 direct/proxy 策略。
3. 本地替换 runtime 时，识别并不再复制旧容器的默认十六进制 hostname，由 Docker 为新容器生成；真正的自定义 hostname 保留且由新逻辑支持。
4. 没有引入依赖或新框架，没有修改模型、提示词、预算、前端、迁移或权限；仅修改共享 ExecEnvManager 及对应测试。服务器部署分支的 AppArmor 补丁和远程实例保持。

## 实际检查

| 检查 | 结果 |
| --- | --- |
| `UV_CACHE_DIR=/private/tmp/bbx-uv-cache make check` | **2133 passed，351 deselected，56.89 秒**；ruff/pyright 全绿，含26项manager相关单测 |
| `pytest services/agent-runtime/tests/test_execenv_integration.py services/agent-runtime/tests/test_ctf_real_vertical_integration.py -m 'integration and not live' -q` | **7 passed，62.53 秒**；含普通/短 hostname 两个真实控制容器创建envd并读取健康状态、未接入网络拒绝、direct/proxy归档恢复及三轮脚本化模型CTF实际容器链路 |
| 更新后的本地 runtime 实测 | 最终安装源码 hash 与工作树一致；实际 provision、Agent 用户/网络建立、MCP 命令、sudo、合成 UDP、采集/MinIO 归档通过；生成 **26 条流量摘要**、原生输入非空，状态 complete |
| 清理 | 合成环境及其精确 UUID 前缀的对象已清理；无真实模型调用、无原目标访问 |

没有重跑全部 Docker 集成或前端测试：改动仅执行环境识别/查找，已运行对应真实 Docker 生命周期与 CTF 链路；前端未变。验证日志、部署快照和构建记录在忽略的 `.data/local-runtime-repair/`。

## 本地更新及任务状态

- 已从原已验证 runtime 镜像增加单文件修复层，禁网构建，无依赖安装。最终镜像 `bbx-local-runtime-network:20261016`，ID `083083471701…`，已更新 `bbx-agent-runtime:latest`；前面的过渡校验结果不当作最终版本结果。
- 新 runtime 默认 hostname 与其自身ID一致，实际网络核验正常；ID和配置摘要见本地部署记录。原 blackboard、前端、Postgres、MinIO、靶场和其他服务未替换；服务器 `101.201.116.63` 未更新。
- 原任务保持 **failed** 的真实历史，未篡改失败原因或伪造完成；已保存工作区归档并标记 cleanup_ready=true，继续任务入口恢复可用。未自动开启新一轮或调用收费模型。
- 更新前后 **18 个任务的标识/状态/用量摘要、30 份 Profile、5 份平台配置、应用设置及99会话数量** 一致。仅增加原任务应有的归档/清理事件及存档，不删除原会话或配置。
- 本地入口仍为 **http://127.0.0.1:58000/**。刷新后可对原任务选择继续任务。

## 偏差与提交建议

hostname 不等于容器 ID 是部署实现细节；设计要求 runtime 实际接入执行网络，新实现继续满足该约束，未修改设计文档。这里的 MAC 仅用于控制服务核验自身网络接入，不改变流量采集的 Agent/source 归属，也不从可变业务 IP 推测数据包作者。

尚未提交、合并 main 或推送。建议一个提交：`fix(runtime): validate custom-hostname network attachment and unblock cleanup`，包含 manager、两份回归及本任务/报告。服务器 AppArmor 修复留在其原分支，未混入本次本地修复。

## 合并验证补充（2026-10-16）

用户随后明确要求将本地修复与服务器部署补丁一起提交、合并 main、推送 GitHub 并更新服务器，取代上文检查点的未发布状态。本地修复提交为 `f5fe10b`，服务器兼容代码/文档为 `6894cac`、`cebb39d`，二者自动合并无冲突。

合并后重新执行 `UV_CACHE_DIR=/private/tmp/bbx-uv-cache make check`：**2135 passed、351 deselected，59.22 秒**，ruff/pyright 全绿；对应 Docker 生命周期/真实 CTF 链路 **7 passed，73.93 秒**。集成测试同时只读挂载当前 manager 与 Settings，保证旧基础镜像也验证合并源码，而非混用新管理器和旧配置类。未调用真实模型或访问原任务目标。合并验证证据位于 `.data/combined-release/`；服务器实际发布与数据核对另见 Server-deployment 报告的补充记录。

代码已合并 main 为 `d1dffd3` 并推送 `origin/main`，远端 HEAD 已核对。服务器已更新两份兼容补丁，Ubuntu/Kali 两模式实测通过；本地已安装的网络修复和原18任务保持，未将本机新增数据覆盖服务器。后续发布文档与 HANDOFF 的提交不改变上述运行代码。
