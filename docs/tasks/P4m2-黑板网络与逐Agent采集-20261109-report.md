# P4m2：黑板任务网络与逐 Agent 采集

基线 e23ef9d。按 [P4m2 任务方案](P4m2-黑板网络与逐Agent采集-20261109.md) 在原旧 Docker 黑板运行路径实现平台可选网络。新默认任务正常执行且采集；配置 VPN 或 SOCKS5 后，不给 Agent 修改、关闭或直连旁路。原生 CTF 不在此片修改；命令记录只读，不做审查或处置。

- **任务与平台职责。** 现用户创建原子冻结内部执行策略；不加用户选择，不把文字说明、浏览器 params、环境变量当作路由权限。服务端防旧调度器的列表、删除与已知 Task 路径绕过；真实运行要求当前原网络和采集确认。原运行回执绑定 Task/run、原容器及实际启动时间、独立执行/管理/出口 bridge。旧历史保持，设置变化不改原 Task；外部 VPN 一次领取，原效果未知保留，旧执行资源只按实际证据撤权、停止和清理。
- **网络边界。** 默认维持 Docker 正常直连/NAT/DNS，每 Task 有隔离执行桥和原固定管理 relay。配置模式用独立 internal bridge；能力探针发现 Docker 会丢目标在桥外的直发包，后以已有 WireGuard 技术承载可信 envd↔网关链路，解包后只经原 TUN 或真实 SOCKS5。透明 IPv4 TCP SOCKS 用固定 Ubuntu Redsocks，经真实 SOCKS5 上游认证；就绪由真实同路 HTTP(S) 响应状态确认。不把本地 SYN、HTTP CONNECT、代理变量或无路由故障后的直达当作 SOCKS 成功。控制 relay 不做代理，Agent 不拿网关凭据，也不具备用 sudo 或原始权限改接口的能力。
- **采集与只读。** 三路径均按真实 Agent veth 在封装/汇聚前原始采集，不能被旧 TRAFFIC_ENABLED=false 关闭；失败、丢包和达限按原证显示。保留历史 HTTP 流量读取，新路径无 MITM。原生包查看复用受授权的现有分析；系统预检另标，不从共享出口整包推测 Agent，不宣称 TLS 明文或全协议。普通停止先撤转发，再按原证明封存与清理；控制通道仍可读，不因存档故障重开网络。
- **兼容性。** 已配置旧 HTTP 代理的部署不会静默切成直连。新保护 Task 不允许旧 HTTP-only 模拟器伪装运行；模拟器仅能对隔离的、未启动的历史示例运行，给新 Task 会返回原 ID 与清楚的 real Runtime/capture 要求；历史 SSE 演示用真实隔离 PostgreSQL/MinIO 通过。现原生 CTF 专用部署仍独立，不迁移原锁或共存配置。
- **实际证据。** 独立 Docker 探针证明 TUN、internal WG、低权限改链路失败、真实透明 SOCKS5 CONNECT、禁止监听/端口和上游停止后无直连。最终三条正常创建路径如下，均确认原执行资源退出且采集 writer 已结束：

| 模式 | Task | 原正证 |
| --- | --- | --- |
| 默认 | `d9011839-3787-4009-9430-e93fd56bcd73` | 两个 Agent 的真实低权限 TCP 和独立 PCAP；既有连接在撤权后失效；无 VPN 或 SOCKS 配置。 |
| VPN | `35bf427a-fcc3-4064-900b-c93f91dbc689` | 真 OpenVPN/TUN，通过普通 HTTP 健康端点的 204 响应；同路命令、Agent 原 PCAP 和停止。 |
| SOCKS5 | `cac2e668-b0d9-4775-8b5d-cd7ddd35a5c2` | 真标准 SOCKS5 上游与认证，清空代理环境仍沿透明出口；同路 HTTP 204、原 PCAP、断路无旁路和停止。 |

配置路径的健康检查是受限 GET，默认路径不需要外部探测。可复用普通健康端点，无专用 echo 服务前置；TCP 回显仅是隔离命令样例。HTTPS 分支的严格校验由定向测试证明，物理配置路径使用 HTTP。IPv4 TCP 是当前实证/配置范围，不代表最终需求限于该协议或 PCAP 含 TLS 明文。

实际验证前曾遇 Docker SDK 拓扑请求形状和可信网关父目录权限问题，修正后才取得配置路径正证。最后默认补验冲突来自测试自身控制桥分配了与 LAN 重叠的 192.168.96.0/20，原政策正确丢弃；将夹具改为真实不重叠分配并清理本轮原有冲突后通过。默认排除无关桥是独立变更，原实际执行、控制、其他受保护和旧执行网络禁区仍保留，有定向测试，不以本次碰撞作为放宽理由。停止路径曾误把只用模型的收尾 Agent 也挡在数据面门外；现门仅保护执行命令的 explore，实际 SOCKS 停止与真实 PG 均复验。

- **必要检查。** 真实 PG 冻结/旧服务/退出回执 1/1，收尾角色定向 1/1（5.18 秒）；常规 API/旧 CTF 98/98，只读流量接口 37/37，历史 HTTP/SSE 1/1（8.94 秒），历史调度兼容 69/69。普通健康合同 1/1，HTTP 与严格 HTTPS 定向 2/2；类型及 Web 定向 lint 通过。Chrome 一条 raw-only 实际交互 1/1（7.5 秒）：无需过滤器默认显示包；选 Agent 来源发送精确 source_id 并只显示该源。最终干净 make check 退出 0：2676 通过、489 排除（189.00 秒），Pyright 0、Pi 5；见 p4m2-make-check-receipt.json。

证据在 `.data/p4m2-legacy-network-20261109/` 的 `physical-acceptance.json`（SHA-256 `933f7823be9e10348a11ed643fe8d293755a0cc2623e7135f9e555a60deae91f`）、`physical-default-final-clean.log`、`physical-http-three-paths.log` 的 VPN 正记录、`physical-default-socks-final.log` 的 SOCKS 正记录，以及 `image-source-proof.json`、`raw-only-browser.log`。保留其他尝试与原退出/诊断，不把夹具失败算成全绿。最终默认的拒绝范围等价地抽为 helper，其原实际桥隔离另有定向检查。

从干净候选可复验：

```sh
make check
.venv/bin/pytest -q -m integration services/blackboard/tests/test_legacy_network_integration.py
.venv/bin/pytest -q -m integration services/blackboard/tests/test_api_integration.py -k full_http_demo
.venv/bin/pytest -q packages/contracts/tests/test_legacy_network.py services/envd/tests/test_network_probe.py
BBX_E2E_BROWSER=/path/to/Chrome pnpm --dir web e2e e2e/traffic-recovery.spec.ts --workers=1 --grep '仅原始流量'
BBX_P4M2_DIRECT_IPV4="$fixture_host_ipv4" \
  BBX_P4M2_ENVD_IMAGE=bbx-p4m2-envd:20261109-http-final \
  BBX_P4M2_GATEWAY_IMAGE=bbx-p4m2-gateway:20261109-permission-final \
  .venv/bin/pytest -q -m integration services/agent-runtime/tests/test_protected_legacy_network_integration.py
```

最后一条使用 Docker Desktop 可达且不与私有测试 bridge 相交的本地 IPv4。原生 CTF 专用发布仍独立；未改调度锁或现有部署。普通管理员配置见方案中的 LEGACY_NETWORK_POLICY_FILE、LEGACY_CONTROL_NETWORK 与原配套 envd 版本，不要求 Task 填代理字段。不调用真实模型、VPN、SOCKS 或靶场。

独立 Docker 夹具的受保护容器、网络及中间标签按原归属清理；回执 `runtime-private-cleanup.json`。旧八交付镜像、原 110 项工作文件和两份本轮制品留本机。10 个重叠工作文件按独审可逆差量回灌，其余 100 项原字节与模式保持，不推送或切换用户部署。

| 本机新制品 | 不可变引用摘要 |
| --- | --- |
| `bbx-p4m2-envd:20261109-http-final` | `sha256:7642db6c22c559800780978f8bb67c10387104c5fd37fb70afc587db17b314b7` |
| `bbx-p4m2-gateway:20261109-permission-final` | `sha256:d707a2c73f21570391a22df40c68a8117576bf32cb2ba7a68b0b7e97591cbef1` |

建议一笔本地提交 `Enforce and capture legacy Blackboard networks`，包含合同、Blackboard、Runtime、EnvD/网关、只读页面、隔离验证与报告。后续按实际授权需求扩展协议、部署与来源验收，不以本轮的范围替代用户长期需求。
