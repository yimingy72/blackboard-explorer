# P4l：原生 CTF 任务 VPN 出口验证

基线 `202258c`。按[任务方案](P4l-原生CTF任务VPN出口-20261108.md)新增管理员显式发布的 VPN 环境。默认旧任务仍用原直连路径和镜像。命令记录只读，不做审查或处置。

1. **权限与原操作**：仅原 Lead 能用现可信 MCP 配置、读状态和断开。配置只能选择冻结环境中的已发布 ID，不从模型、浏览器或共享附件传入任意 VPN 配置、路由脚本或凭据；已受理结果与原工具回执同事务保存，不称已连接。首片整个部署一个凭据占用；普通断开后模型、平台控制和读文件仍可用，求解命令仍关，凭据继续占到原 Runner/Host/出口/Sandbox 的实际全部正退出。已断或结果未知不能在同资源代重复登录。
2. **真实数据面**：外层 OpenVPN 传统 TUN，内层固定 Task WireGuard；独立出入口按原 IP、端口、接口和连接身份限制，保持原模型/控制路由。先由可信的同一路径数字 TCP 预检，再允许原 Agent 进入既有 P4i 归属/短期转发。外层内核许可和 Agent 内层许可相互独立、必须同时有效；迟到、撤权或原连接变化不允许复用旧流。P4c 确认暂停时关闭旧来源，若原外层连接仍有正证可在同 pause ID 受控恢复新 binding/source，不重复登录。P4f 要求原实际全部 CID 正退出后新代才允许显式配置。
3. **实际验收**：隔离 K8s 假 OpenVPN 对端与假目标，无真实来源/TSec、DNS、solver UDP/ICMP、回连或 IPv6。验收区分各独立 Task，未重放原配置请求：
   - `adfbfd2c…`：原 Lead 已受理唯一配置请求；隔离测试 CA 的叶证书缺 AKI，实际 Kali Python 严格 TLS 拒绝，未领取连接或发送外层请求。保留原失败及正停止证据。单独修复测试 CA，产品 TLS 不降级。
   - `97762721…`：严格 TLS 通过，已确认原两端 WireGuard 链路；实际 OpenVPN 2.7.8 在只读根容器里使用默认 `/tmp`，以 EROFS 退出。原状态 `failed`，未生成 TUN/短期转发/目标预检正证。原镜像只读独立复现。产品固定使用已有出口私有 tmpfs，不修改容器权限；原 Task 不重试。
   - `b675a6eb…`：两个实际通道、原外层 TUN 和探针专用许可均取得正证；出站探针实际转发到 TUN，但对端拒绝解密。原对端日志记录 `AEAD Decrypt error: error reading packet-id`；OpenVPN 2.7.8 固定白名单过滤了对端下发的必要 `protocol-flags`。保留原观察、计数器和 peer 日志。未准许 Solver 执行，原四容器正退出并保留两卷。只开放已认证对端的协议协商选项；独立 OpenVPN 实际 TUN 负正对照中，旧版 TCP 回显失败，修正版加密回显成功，两边都未采纳路由和 DNS 推送。
   - `cb1223f2-71a4-422d-9bf6-f710b457d817`：公开创建的最终 Task 实际通过 9/9。原 Lead 配置后通过低 UID 预检，再从 UID 10000 实际执行两个 TCP 回显命令；未授权端口被拒。对端确实推送路由和 DNS，原默认路由、对端控制路由及 DNS 摘要均不变。通过用户接口暂停后，原 OpenVPN PID/启动时间保持不变，恢复新 Lead binding/IP/来源而不重拨；两条命令作用域均封闭，MinIO 读回两份加密前抓包（21、31 包，分别含原唯一标记）。对端退出时已证有既存连接且目标仍在监听，原 Lead 仍可通过平台工具断开；凭据继续占用，原 Runner、Host、出口及 Sandbox 四个容器正退出后才释放，原两 PVC 保留。

最终物理证据在 `.data/p4l-native-vpn-20261108/k8s/joint-aead/evidence.json`（SHA-256 `9f90b4b69c48fbd96164b71550312e9977b71cb1a14f1f3a25f5c7cb9ab4abc6`），同目录 `safe-data-stage-readback.jsonl`、`initial-sealed.pcap`、`resumed-sealed.pcap`。此前三次 `joint`、`joint-strict-tls`、`joint-egress-tmpdir` 各自保留 `original-user-stop-proof.json`；第三次另有 `preflight-peer-session.log`、`preflight-readonly-counters.json`。同 native 目录 `capability/` 保留 TUN/WG、严格 TLS、只读 OpenVPN 参数和加密 TCP 负正对照；`k8s/build-source.json` 记录最终外层出口与 Sandbox 使用同一 VPN 镜像 `sha256:082f630d60d602a9867e456e273f46c6f4ac528f8d87d88c27a0b4e8d0902695`，原直连镜像分开。正常发布加载器只干读该隔离实证，接受冻结镜像与回执，拒绝 `test_only` 和错误出口镜像；证据在 `.data/p4l-native-task-vpn-20261108/normal-publication-readback.json`，未新建 Task 或启用部署。P4l 实际验证 MinIO 原始抓包读回，沿用既有 Linux 查看链，未重复单独验收查看器或实际跨资源代恢复。

干净候选 `make check` 退出 0：2667 通过、484 排除（246.03 秒），Pyright 0、Pi 5。原应用检查、定向 PG 与最终启动脚本摘要见 `.data/p4l-native-task-vpn-20261108/`。PG 原 Task 组合 1/1（6.17 秒）：原 Lead 可从已存未发的 VPN-down 命令拒绝继续配置、Worker 拒绝、两个 Task 同槽争用、一次连接请求、已下发/已实际应用分开、超实际窗口证明拒绝、同 ID 暂停阶段授权、未知和断开保留占用、错 CID 不释放。旧 P4k 组合 PG 1/1（9.77 秒）。TS/定向 lint 通过。最终 `task-vpn.py` 核对实际策略规则与原已启用 WG/TUN，并固定 OpenVPN 私有临时目录；另允许固定版本的必要协议协商。这些改动发生在全套检查后，独立脚本不在 `make check` 范围。最终脚本 Ruff/编译、只读容器实际 argv、独立加密 TCP 对照和部署路径按证据列出，Runtime/Host Python 与全套检查相同。

复现命令（从干净候选运行；PG 用一次性容器和假模型）：

```sh
make check
.venv/bin/pytest -m integration services/blackboard/tests/test_native_vpn_integration.py
.venv/bin/pytest -m integration services/blackboard/tests/test_native_ctf_publication_integration.py
(cd web && pnpm exec tsc --noEmit && pnpm exec eslint src/pages/NewTaskPage.tsx)
.venv/bin/ruff format --check services/sandbox-execution/launcher/{task-vpn,assignment-network}.py
.venv/bin/ruff check services/sandbox-execution/launcher/{task-vpn,assignment-network}.py
.venv/bin/python -m py_compile services/sandbox-execution/launcher/{task-vpn,assignment-network}.py
```

隔离集群、设备/内核检查与真实路径的复现命令见 [P4l 验收说明](../../deploy/kubernetes/p4l/README.md)。保留各次原证据及原 110 项工作文件：五个重叠文件以独审的可逆差量回灌，其余 105 个文件原字节与模式保持。按归属清理两套私有 kind、四组 PostgreSQL/MinIO、私有网络、八个临时镜像标签及临时凭据，默认 kube context 不变；产品普通停止保留两卷先于夹具拆除。清理回执见 `.data/p4l-native-vpn-20261108/local-qa-cleanup.json`。此前四个交付镜像及下列两份本轮验收镜像保留本机，旧部署保持原配置，未推送：

| 本机制品 | Tag | OCI manifest |
| --- | --- | --- |
| VPN Sandbox / Runner 出口 | `bbx-p4l-vpn-sandbox:20261108-aead` | `sha256:082f630d60d602a9867e456e273f46c6f4ac528f8d87d88c27a0b4e8d0902695` |
| Sandbox Host | `bbx-p4l-sandbox-host:20261107` | `sha256:10a36e5a44bef6555d9a2f425dd9042353498cb27b647c9ad127a4249c8b6cfa` |

本次单一提交建议为 `Add scoped OpenVPN egress for native CTF tasks`，包含冻结合同及生成物、BB/Runtime、Sandbox 网络、隔离验证、最小 Web 提示与方案/报告。不改正式设计。

下一步用管理员明确批准的真实来源配置做适配及验收，再按实证开放 DNS、UDP/ICMP 或回连范围。先不把本地假对端、短期停复或两段流量归档扩称为生产线路或全协议支持。
