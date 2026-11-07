# P3h Task Sandbox 工具与命令记录

Runner 仍使用原 Pi 和受信 canonical→wire 工具映射。同一原生 agentgateway Virtual MCP 路由下，Sandbox 工具走独立 Task host。新增可信 `node-sandbox-host-v1` profile：原 Runner Pod 的 Node 与 `sandbox-host` 两容器均绑定原 UID/CID；旧单容器 profile 沿用原规则。

- Host 使用独立 Secret（入站、Blackboard 回写两个不同的 Task key、原生 execd 两个官方 ingress headers、原 execd token 和冻结 context），不挂 Node 凭据、journal、workspace 或 Kubernetes token。Node 不挂 Host Secret，Pod 不共享进程命名空间。Host 身份就绪后才下发 Node bootstrap。
- 复用官方 OpenSandbox SDK 1.2.0、固定 G1 launcher 和 Assignment cgroup。模型只选工具参数，不能指定 execution ID、UID、网络命名空间、capability、环境或后端 token。`worker-a` 是本 profile 的 Task 共享网络，不是每 Agent 独立网络。
- Blackboard 是唯一调用和命令记录权威。先持久准入，再发 SDK 一次；保存真实 execution ID、已观察输出与结果后回 MCP。未知不重新派发。命令记录仅记录平台收到的工具命令及其回执，不做审查、风险判定或处置，不声称捕获 shell 的全部子命令。
- 前台保留每个流前 64 KiB SDK 文本字节，记录已观察总量和截断；原始管道字节不冒充完整留存。后台 SDK 客户端只对原生日志响应加 1 MiB 接收上限，超限返回明确未保全，不编造 cursor 或改原命令状态；固定 execd 服务端自身仍用 `io.ReadAll`。文件用 UID/GID 10000 的官方 byte stream，仅支持最大 64 KiB 前缀，不先聚合整文件；低 UID 是内核权限边界，词法 workspace 路径检查不等于额外 symlink 隔离。
- `interrupt_execution` 的 ACK 只表示请求获受理；独立 G1 scope 回执才确认原 Assignment 已封闭且为空。Task 普通 stop 要原 Node/Host 两 CID 与原 Sandbox 实际退出，并保留原 PVC；本轮不为 H 重跑 G 的卷回收矩阵。

## 验证

只用此前已核 UID/endpoint 的私有 kind 1.34（非默认 `docker-desktop`），复用原生 Cilium、G7 Server/Controller/Ingress 和已审 Ubuntu 验证层。P3h 新写入均位于 `.data/p3h-sandbox-tools-20261107`。AGW 在该集群的独立控制命名空间，Task 只新增对固定 AGW/Ingress Pod selector 和端口的访问。宿主使用私有 port-forward，Sidecar 通过同原生 Ingress 的可信集群内 authority 保留实际 SID 路由头/secure-access 头。模型响应全由本地脚本提供。

```sh
python3 deploy/kubernetes/p3h/build_images.py --candidate <reviewed-clean-candidate>
PATH="$PWD/.data/p3g-retained-20261107/bin:$PATH" \
  <candidate>/.venv/bin/python deploy/kubernetes/p3h/probe.py --candidate <candidate>
```

- 首 Task `d3ec3ddb-1263-4098-93de-895fd9efa303` 确认 Host 原身份就绪，但原生计费判不可用并持久 hold，0 模型/工具发送。后来只读预算状态 200 且精确匹配冻结字段，不能据此断言原瞬间故障原因。保原失败 `joint/evidence.json`，按原操作、原 PG 和实际退出证据收口 `joint/resume-evidence.json`（stop-only，非全链通过）。
- 原 Task 正退出后，只新增一个独立 Task `f37c3b67-cf77-4789-8f49-a419d342a289`。夹具在提交前确认原生管理接口健康，持续 drain 私有转发输出，并记录脱敏故障分类；实际运行没有计费或转发诊断。未修改产品 hold 策略。
- 第二 Task 在真实 Pi、原生 AGW、官方 Sandbox 与 PG 完成 `write_file`、`read_file`、`exec_command`。原命令输出 `p3h-command uid=10000` 与实际文件内容，退出码 0；实际 execd execution ID `97b39be0aa5e41b68e8d0165d720fe45` 持久在 PG，用户记录 API 可读。无 Host 入站 key 的直接请求 401。真实 close 后原 Assignment 封闭为空，独立 scope execution ID `7313d742f6954d84b7ebfa11d6c00a23`。原 Node/Host/Sandbox 正向退出，普通 stop 保留原 PVC。六项实测及原 UID/CID 见 `joint/fresh-evidence.json`。

未调用真实模型或靶场、未重复旧费用/65 秒/G1 大矩阵。Ubuntu 只验证本片工具和生命周期接缝，不称完整 Kali 工具层。

在第二 Task 正退出后另做一次同 Task 的用户 DELETE：先保存原用户命令记录响应，再用原 PG/AGW、原配置和新真实 RuntimeLock 走 P3g 生产退休。原 Host Secret UID `56212f71-8291-4a7a-aeae-ecaadee262c9`、Namespace、两 PVC/PV 和 VAP/binding 均确认消失。首段 PG purge 因夹具未接对象存储而超时，保 `retirement-native-only-evidence.json`；沿原 Task 的已退休 manifest，复用 G 的空对象夹具（本次无对象工件）恢复生产 controller 的原 purge。`retirement-evidence.json` 四项通过：原 Task/能力/命令记录行消失，另一原停止 Task 权威保留。本段没有模型或工具发送，也未以最终私有集群销毁代替产品退休。
