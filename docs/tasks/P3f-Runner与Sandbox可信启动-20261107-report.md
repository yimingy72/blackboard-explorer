# P3f Runner 与 Sandbox 可信启动报告

2026-11-06 至 2026-11-07；实际开发：GPT-6.1 Sol / high。基线 `00fe583d012dc5a235894d8c8a75c1227de49364`。

## 完成内容

1. 新的受信入口 `POST /api/tasks/<id>/ctf/runtime/runner-bootstrap` 接收原 request_id、明确原生 model_alias 和 FrozenExchangeRate。平台先在 Task 锁内持久化唯一 operation、原始请求摘要、不可延长 hard deadline、Task namespace 和加密租户 key；重复请求读原记录，换请求不重开。显式状态 GET 区分 queued、资源准备、Runner 等待/运行、hold、停止、退出已证待域收口与 unknown。
2. 原 agent-runtime 新增明确的 `serve-runner-control` 模式。它与 legacy serve 使用同一个现有 RuntimeLock；旧进程仍持锁时，新模式拒绝启动，默认 serve 不变。新模式不启动 Docker manager、Python Agent 或 legacy Pi，也不要求 envd/MinIO/真实模型 Secrets；只消费已申请的 V1 operation，不暗称能同时接管旧任务。
3. Native controller 在同一持锁 PG connection 上分配持久单调 fence，固定整个进程生命；BB claim 与每次 action 同时检查全局 fence、Exclusive advisory lock、PID、backend_start、数据库与 Task controller_id。旧实例不能靠迟到 claim 或读取新 generation 夺回权力，也不能在新实例逐 Task 接管之前继续写。
4. Namespace/PVC/Secret/裸 Runner Pod 使用固定操作归属；官方 OpenSandbox Server/SDK/BatchSandbox Controller 创建执行环境。首次真实 create POST 紧邻 PG CAS，一次授权；SDK 显式禁重试，使用官方低层 create_sandbox，拿原 SID 先 checkpoint 再只读 connect/Ready，避免高层 create 的异常自动 kill。丢 ACK/重启按原 metadata 只读对账，缺失保 unknown，不另创环境。
5. 官方 HTTP TenantProvider 接真实 BB 权威：独立 resolver Secret、Task key 指纹到冻结 Namespace，原 key 密文保留。返回 ttl=0，Server max-stale=0/有界缓存；清理中 key 仍具有官方 namespace 生命周期权限，只由可信 Runtime 持有，不冒称 Server kill-only ACL。确认退出后拒绝解析；401 不冒称取消了已在途请求。
6. 先得到真实 Sandbox/CR/Pod UID、环境 revision 和 waiting Runner UID/containerID，再由产品事务创建原 Task 用户输入对应的 Lead execution turn。该事务幂等，不使用 install_fake_runner，不假造 Worker/题目或提早标记完整就绪。然后注册原 owner、冻结期限上限，复用原生预算/目录派生有效 SDK 模型、真实 binding 和 P3e MCP 路由，挂载仅 control/model/mcp 短能力，prepared 后才投递原邮箱 input。
7. Runner image 的 Node 启动门在打开 journal 前一次性校验 bootstrap schema 与 Downward PodUID，将该快照独占写入 tmpfs 后交给原 main；Runner 是非 root、无 Kubernetes token、无求解 workspace 的裸 Pod，restartPolicy Never。journal/workspace 各自独立保留 PVC；RWO 不是独占证明，原 Pod 身份/退出未核不重建第二 writer。
8. 凭据通过原 Secret UID/resourceVersion 更新整卷，不用 subPath。先预置 CA，避免 Node 只在进程启动读取 NODE_EXTRA_CA_CERTS 的时序问题。可信控制器每 20 秒续原 60 秒 owner 并提前轮换至多五分钟的能力；每次核全局 fence、原 PodUID/containerID、Task 意图和期限，不恢复已过期/撤销 owner，不延 Task hard deadline。
9. 预算 hold 保有限控制续期：只刷新原 control 记录，不重新签模型/MCP，不增加 permit，不激活或投递初始 input。资源适配器原样保留旧 model/mcp、bootstrap、CA；初次无 map 时最多写 control。Node 只核所请求 reference 的期限，不放宽其他权限。
10. 原 Task 事务同时写单调 stop admission marker 与资源 desired stop；模型、工具、真实领域事务与 live scope 立即拒新业务。closing 的控制 poll 仅允许原 owner 期限内 close/abort/shutdown，不发 prepare/input/新许可。长准备可被停止或失锁取消，本地 finally 不无条件 kill 新控制者的资源。
11. 清理先只读 recover 原 operation/UID，再关闭可确认的一侧；已 claim 但未知 SID 查原 metadata，不因缺 Runner 而永不清理 Sandbox。只有原 UID/containerID 的正退出证据可完成；404、kill ACK、TTL、lease 和 route retired 都不等于退出。PG 可证明从未授权执行、且只读确认未开始的分支单独收口，不借 unknown 冒零副作用。
12. 退出已确认但域 finalize 失败保留 exited 补偿态，重启继续域收口，不再 kill 原资源；正退出、SDK/账本、归档与最终 Namespace/PVC 退役分别保留。存在资源归属的 Task 本片拒 purge，防丢唯一 UID/key；只有从未 claim/config、无 create 授权及观测的取消请求可安全删除。保留卷需要后续受信回收或持久归属移交。

## 实际验证与停点

所有模型/租户/证书/密码都是本地合成值；不读 `.env`，不访问真实模型、靶场或 VPN。

- 真实 PostgreSQL 控制合同 `1 passed in 4.72s`：服务入口鉴权/FX 无时区拒绝且不落 operation、请求幂等、租户解析/不泄漏原 key、真实 Lead turn 一次生成、不可用环境拒 material、closing 只给 shutdown 无许可、全局 fence/会话旧值拒绝、退出 commit 后 finalize 中断可补偿。
- 未派发取消/保留归属/实际截止原因合同最终 `1 passed in 5.91s`：无 Lead 时不会用 None 查 turn，已 claim 资源行保留，未 claim 删除开始后不能再取资源；明确从未 claim 的请求可安全 purge；硬截止按 budget_exhausted 结论收口，不伪称用户停止。
- hold 控制合同 `1 passed in 4.78s`：闭合 binding 与预算 hold 不阻原控制续期，epoch/hard deadline 不变，不访问模型/MCP 发证路径，不派初始 input，不发 Session 许可。
- 真实 Kubernetes 组合第一段使用 clean 候选 core-v3 + 资源 v5：真实 Node/Pi 原生 journal 与已保存 business MCP 工具、同 Pod 超过 65 秒 owner 续期、受限 Secret 轮换、旧全局控制者拒绝四项均通过；第一次退出观察返回 unknown，整次原 evidence 保留失败，不称全绿。
- 该原 Task 随后沿修复的产品 recover/stop、同原 namespace/UID/CID 与新真实 RuntimeLock fence恢复：原 Sandbox raw watchcache MODIFIED/DELETED 确有同 CID 的 exit143，Runner 终态、原 CR 缺失且无替换 Pod、两原 PVC UID 保留；正常 cleanup action 与域 finalize 已达 stopped/exit_reason terminated。没有重建 Task、重发模型或手写 DB proof。cleanup-only evidence 三项为真并标 container_cleanup_confirmed；其 pass=false 是脚本明确区分仅补清理，不把它冒充重跑主链。
- 最终 clean HEAD 候选门禁退出 0：508 文件格式、lint、pyright 0 errors/0 warnings、Pi 5 项通过，pytest `2636 passed / 467 deselected in 192.14s`；独立 `tsc -b` 退出 0。Python/Node/Web 依赖按锁独立安装，导入路径全部核到候选。首次 gate 仅在新合同文件 Literal/空行格式检查停下（exit 2，尚未 lint/type/test），只格式化该文件后完成以上门禁；原停点日志保留。门禁后只有文档补实测，不再重复应用测试。

候选真实组合的源码来自 clean HEAD 导出与本片精确补丁，记录 BB/Runtime/SDK 的实际 import 路径和模块 SHA。首段 core-v3 SHA `352668f4…`、资源 v5 `733eef2…`；恢复增加允许的容器名/ResourceVersion 窄投影（cursor 不是退出证明）及修后的原生 raw watch。两个源码版本/阶段分别保留证据，不把最终字节未经重跑的分支伪称已跑。固定 kubectl 1.34.0 的独立 live 原生 Pod 回归另验证生产 raw-watch 解析器（SHA `963376f6…`）确实取得原 UID/CID 终止事件，并已精确删除该无 Secret/PVC 的 Pod 与 namespace；不重跑整体 Agent。api.py 原 WIP profiles import 上下文不匹配时，仅把同一自有增量缩为明确锚点应用；不复制旧 CTF automatic 模块。

初始必要停点均保留：POST bootstrap 被 legacy runtime catchall 抢先匹配，已调整具体 router 顺序；Helm post-render 多文档实际输出连续 JSON，改标准解码逐文档输出；首段退出 watch 使用 kubectl get 不支持的 resource-version 参数，正确保持 unknown，后改 raw 原生 watch URL并通过真实终态事件收口原 Task。早期部署停点在任何业务 Task 创建前清理，未拿本地 attempt 文件替代 PG 操作账。

## 运行边界

- 本片验证可信宿主 native-controller，使用任务专属 kubeconfig/固定兼容 kubectl 与私有 kind 1.34+Cilium。首段实际 kubectl 为 1.36.1，跨两 minor，不能当作官方支持的客户端 skew 组合；排障后仅本任务目录固定并校验 kubectl 1.34.0，原 Task 读取/恢复与后续窄 watch 回归使用该客户端，不改全局工具。默认 Runtime Dockerfile 仍是 legacy serve；若在容器采用新模式须明确提供兼容 kubectl、受限 Kubernetes 凭据及配置。本片没有扩大为整个平台镜像/服务重新部署。
- Model proxy 本身提供内部 HTTP listener；Node 三个 public origin 必须使用受信 TLS 反代/Ingress，safeUrl 不放宽。native 模式能将 resource config.ca_file 合入默认验证上下文，支持直接私有 CA 控制通道；不依赖关闭验证。
- Task Runner、新建 Ubuntu 工具变体/G1 控制层、官方 OpenSandbox/G7 overlay 都按实际 digest/environment revision 绑定。Ubuntu 验证变体不替换产品的受信镜像配置，不冒称 Kali/Sage/angr 全集通过。
- 公共 V1 start/legacy claim、V1 legacy resume 仍未解锁；只有新的 service bootstrap 可进入本片。没有交付 Sandbox MCP 命令/文件工具、VPN、PCAP/eBPF、审计 UI、P4 派工/helper 或完整恢复。
- FX、source/model alias、资源部署与期限冻结；不猜汇率、不回退旧 provider 计价，不为恢复另发 Task 预算，也不把 TTL 额外清理窗口加回 Task 可工作期限。

## 提交与后续

建议标题 `feat(runtime): bootstrap fenced native runner and sandbox resources`；最终 clean gate 已通过，按精确候选补丁本地提交，不 push、不合并 main。后续先完成受信保留资源回收/移交，再推进既定 P4 与 Sandbox 工具、网络和审计接线。
