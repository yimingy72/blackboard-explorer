# Model-success-health · 网络恢复后的故障计数修复

日期：2026-09-29。任务：`4891d8c7-7720-45de-8aec-5562809c57a3` 第3轮。

## 诊断

第3轮在17:00:27续跑，17:11:51因“模型服务连续不可用”结束。事件记录有342次 model_output、15次 connection（Explore 14、复用Derive的一轮1次）及1次unknown。用户确认期间本地有短暂断网，百智云后台没有相应错误记录。这与连接/流读取中断相符，但旧trace没有底层异常类型，不能逐条反推出故障发生在哪个网络环节，也不能确定unknown那条的具体原因。

终止计数实际为：17:05:22的unknown计1；17:07:22首个connection窗口计1；17:11:51第二个connection窗口再计1，达到3。前两次之间有33次成功模型输出，两个connection窗口之间有80次成功输出；旧代码仅在整个Agent正常结束时清零，未把这些成功调用当成恢复信号。最后一次是transient，就把混合计数写成“模型服务连续不可用”，该表述不能代表实际持续不可用。

只读连通性检查：当前runtime没有显式HTTP代理配置，网关域名解析成功；无认证GET模型列表端点约1.4秒返回401，说明检查当时DNS/TLS/HTTP可达。该检查不调用收费模型，也不能反证此前未发生断网。`attempt_limit=5`是配置上限，原记录并没有实际HTTP尝试次数。

本轮17:13:44已生成4,151,080字节的run-3归档，17:13:46清理完成；归档和Ubuntu回收机制工作正常。

## 实现

1. 对纯模型故障（model_transient/model_error），合法活动Agent的成功模型调用所产生的正steps进度事件显式记录reset_failure_streak，事务投影清空计数和窗口，无需等待整个Agent结束。无标记旧事件保持旧投影语义；零步心跳、宽限额度扣减、旧derive轮次和终态迟到进度不会清除故障状态。
2. 普通本地运行错误、无效回执不被模型回复成功掩盖；混合故障用mixed保守标记。正常Agent结束清零规则、120秒同窗合并、无成功调用时的阈值保护和Intent重试限制保留。失败理由改为“连续运行失败达到保护阈值”，不武断归因服务商。
3. 模型错误增加受控exception_type/cause_type/transport_type、固定failure_phase和SSE event_type，保留验证后的provider_code。区分连接、代理连接、响应读取、请求写入、缺少流完成事件与HTTP响应错误；未知类型仍明确未知。原文、headers、请求/响应内容和密钥不进入错误元数据。
4. 不完整流仍拒绝成为有效回执，不重放已经执行的工具，不增加应用层重试或修改提示词。日志改为中性Model call failed，避免把配置上限误说成实际耗尽次数。

## 验证

- 子代理定向：领域97项、PG6项、runtime59项；原无效复核场景、三次模型故障和并行故障合并场景通过，ruff/pyright通过。
- 主代理 `UV_CACHE_DIR=/private/tmp/bbx-uv-cache make check`：729项通过，73项integration/live排除，ruff、pyright通过。日志 `/private/tmp/bbx-model-health-check-final.log`。
- 主代理 `UV_CACHE_DIR=/private/tmp/bbx-uv-cache make test-integration`：69项通过，733项非集成排除，176.45秒。日志 `/private/tmp/bbx-model-health-integration-final.log`。初版全套发现模型成功清零会掩盖无效derive回执；收窄为模型故障家族并保留mixed保护后，原保护用例及完整套件全绿，没有降低断言。
- 最终blackboard/runtime镜像已构建；本轮无真实模型调用或目标操作，原任务未自动续跑。

## 边界与提交

网络中断本身不能由计数补丁消除。新增诊断只对之后的错误生效，旧unknown原因仍无法还原；不修改已有失败事件或原任务状态。混有本地错误的计数保守保留，以防模型正常回复掩盖真正的运行/回执缺陷。

设计已先提交；建议将Blackboard计数与runtime诊断分别提交，报告与部署记录独立提交。没有新依赖、数据库迁移或配置项，旧档案与会话保留。

## 本地部署

实现 `eaa5619`、诊断 `0c2962e`、报告 `5a02f54` 已合并main；blackboard镜像 `d28bd9c281c6`、runtime镜像 `3006c926b7b3` 已部署。部署前无活动任务，部署后两服务running、重启次数均0、运行锁1个、`/login` 返回200。五个修改模块的容器内SHA-256与main逐项一致，容器内离线ReadError诊断检查通过。

原任务保持failed/run3，cleanup_ready=true，run-3归档及会话保留，未自动续跑。后续执行使用新的恢复健康规则；历史失败事件和其原始文字不回写。
