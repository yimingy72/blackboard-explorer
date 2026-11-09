# P4m1：原生 CTF 任务网络与逐 Agent 流量

基线 d61dbfbd。按 [P4m 计划](P4m-任务网络职责分离-20261109.md) 交付同一个原生 CTF 创建入口的默认网络和 Lead 共享 VPN。黑板默认/VPN/SOCKS 是计划中的 P4m2，本片没有实现或验收。命令记录只读，不做审查或处置。

- **按任务选择。** 现有任务说明由原 Lead 消费，服务端不猜关键词、不要求用户重复填模式。首次选择与原工具回执同一事务冻结；未选择的 Solver 命令明确未发送。默认正常使用已证直连策略、逐来源采集，不领取 VPN 材料、不占连接槽、不拨号、不看 VPN ready。共享 VPN 需实际 TCP 预检与短许可，无默认回退。两路径共用同一已发布资源模板，节点需提供 TUN 容量；闲置出口仍按原容器身份停止。
- **按来源连接。** Lead 可直接使用任务说明中无凭据的获准 HTTPS URL，或同 Task 用户私下上传的引用；普通共享附件不是前提。HTTPS 固定实际已验 IP 和原 TLS 名称，限制时间和字节，禁止重定向或携带授权。配置只接受受支持的 OpenVPN 子集、加密保管，唯一专用出口可领取；原材料不下发给模型或工作区。密钥验证由有界可取消独立进程处理，采集、控制和 Task 停机不等待其 CPU 工作。
- **按范围转发与捕获。** 外层 peer、系统预检和求解许可分离。可信范围拒绝管理、节点、内部和底层禁区；Worker 访问范围内的后返地址不逐题登记。默认直连和 VPN 转发都先于汇聚按真实 binding、Assignment 和来源采集。既有控制通道独立保留调用观测；IPv4 TCP 实证不代表 TLS 解密、UDP/ICMP、IPv6、任意配置或真实 TSec 已交付。原暂停恢复、撤权及容器终态沿原身份。
- **物理验收与证明边界。** 首个原 Task a30e1cc0… 未越过 Host 早期装载，受信配置中空目标字段在 BB 与 Runtime 间预期不一致，原四容器已确认停止；只修 Runtime 对原空字段形状的核对。修正后同一运行中的发布创建默认 Task a17e3d43-6d62-4e19-9790-aaa27e4603f8 与 VPN Task 94839ad0-f62c-479b-915d-537993180cb7。默认 Lead 有实际 TCP 与 21 包归档，未拨 VPN；VPN Worker 执行 UID 10000，实际读取预检后返回的 10.71.0.5:18889，拒绝直通底层，原 33 包归档。两 Task 均确认原四容器停止；旧 VPN Task 暂停撤许可，但额外网络指令只被原 Lead 接受、未确认应用，保留拒绝恢复的原证。已改为在原第一条输入内附指导与私有 ID，不改 SDK 或伪写回执。以上联合物证的解析是前一版线程隔离。随后 Task f74c9965… 的实际下载触达后，隔离解析进程重载了测试夹具的错误导入路径，未领料或拨号，按原四个容器停止；生产入口本身有 main guard。修正夹具并单独验证真实解析进程后，最终 VPN 生命周期 Task 1076957e-cf5d-4fd2-bd6d-bcc670a93aeb 使用最终独立进程与单条 Lead 输入，7/7 通过：VPN 实际预检、后返题目 TCP、暂停前原输入全部确认、同一 VPN 进程下换绑定续跑、Lead 主动断开、两份独立来源 PCAP（各 33 包）和原四容器正退出、原两卷保留。
- **检查。** 真实 PG 3/3（11.35 秒），最后单条初始输入修正另验真实 PG 1/1（5.64 秒）；独立进程验证 1/1（2.61 秒）、Chromium 私有上传定向 1/1（8.2 秒）、类型/定向 lint 通过。最终干净 make check 退出 0：2669 通过、485 排除（195.94 秒），Pyright 0、Pi 5。解析进程故障的真实 API 组合 1/1（5.70 秒）证明上传失败返回通用 503，合法输入可再绑定；原获取未知不会重取或拨号。保留原 110 项工作文件及六个此前镜像；七个重叠文件以独立审查的可逆差量回灌，其余 103 项字节与模式保持。

证据目录为 `.data/p4m-task-network-20261109/`：同一发布两路径和旧暂停缺证见 `k8s/joint-both-fixed/ctf-network-branches-proof.json`、`same-frozen-publication-proof.json`、`vpn-pause-original-input-readback.json` 及原停止/PCAP；最终生命周期见 `k8s/joint-vpn-lifecycle-final/evidence.json`（SHA-256 `c8f3bbffe391b1eef584eab94f20ae84964853db88e2849740c157b4598efa46`）及同目录两个 PCAP。最终物理源码身份见 `k8s/public-lifecycle-final-product-source.json`，最终检查见 `p4m1-make-check-receipt.json`。正常发布加载器仅干读上述实际证据：接受匹配的两路径发布，拒绝 test-only、旧证明/缺少双路径标记和错误出口镜像；见 `p4m1-normal-publication-readback.json`，未新建 Task 或启用部署。

复验采用隔离假模型和本地测试源，不访问真实模型、VPN 或靶场；物理命令见 [P4m 夹具说明](../../deploy/kubernetes/p4m/README.md)。

```sh
make check
.venv/bin/pytest -q -m integration services/blackboard/tests/test_native_vpn_integration.py services/blackboard/tests/test_native_ctf_publication_integration.py
.venv/bin/pytest -q services/blackboard/tests/test_native_vpn_sources.py
BBX_E2E_BROWSER=/path/to/Chrome pnpm --dir web e2e e2e/native-ctf-create.spec.ts --workers=1 --grep '私有 VPN'
```

私有 kind、独立网络、四组 PostgreSQL/MinIO、假对端、五个临时标签与临时凭据已按归属清理，默认 kube context 不变。五个实际 Task 均有独立原正停止证据；保留各次抓包和诊断。清理回执见 `runtime-private-cleanup.json`。以下两份本轮制品与此前六份镜像保留本机：

| 本机制品 | Tag | OCI manifest |
| --- | --- | --- |
| 双路径 VPN Sandbox / Runner 出口 | `bbx-p4m-vpn-sandbox:20261109-final` | `sha256:05a46bc49dc8e0123063a304b4c531020f651b3b82c5b0a2aeea4f4ee8181ccb` |
| Sandbox Host | `bbx-p4m-sandbox-host:20261107-final` | `sha256:0e40832402e854557a9cf2fd206ce66c8ee5d942c0e1118ea3e12e784dbb818e` |

建议一笔本地提交 `Support task-selected native CTF networks`，包含本片合同、Blackboard、Runtime、Web、隔离验收和报告；不推送或切换既有部署。

后续先交付并实测黑板三条执行路径。默认网络仍要求采集；平台绑定 VPN/SOCKS 时在 Agent 无权修改的边界强制，不包装 HTTP CONNECT 或代理环境变量。仍保持旧部署和历史数据的执行/读取边界。
