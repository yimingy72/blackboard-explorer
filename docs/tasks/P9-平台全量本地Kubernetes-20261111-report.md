# P9 · 平台全量本地 Kubernetes 实施报告

P9 已在原 Kind 正式运行，使用原账号访问 [127.0.0.1:15800/login](http://127.0.0.1:15800/login)。网页/API、共同 Runtime、PostgreSQL、MinIO 和内部 TLS 代理均在集群内；本机的 `kubectl port-forward` 只连接浏览器。没有创建真实任务或调用真实模型、外部 MCP。

## 实施

1. **P9.1 平台与制品。** 沿用原集群、OpenSandbox controller/ingress、Cilium、任务镜像和 TUN。应用与 Runtime 分别核对 OCI 和实际 Pod imageID；PG、MinIO 使用独立 Retain PVC。专属 Kind 容器从 2 核、6 GiB 增至 4 核、10 GiB，内存与内存加交换上限相同；没有重建集群、修改 Docker 全局设置或降低原并发。
2. **P9.2 控制与隔离。** 两条内部服务使用独立 DNS/SAN 和 TLS。Runtime 只有任务命名空间内的操作授权；P9 专属 Sandbox Server 只获本任务资源权限，Pool、模板写入和旧 P8 任务命名空间被拒绝，Controller 生成的 Pod 也受固定镜像、启动器及卷约束。任务没有平台 SA 令牌。Runner 的 8766 继续仅在 Pod 内监听，Runtime 通过 Kubernetes API 按原 UID 短时转发，未开放 Cilium 直连。原求解网络的 Service CIDR 拒绝和公网 DNS 保持。
3. **P9.3 原位验证。** 使用同一 P9 Service、DNS、CA 和资源配置完成两种模式的合成验证。普通任务跑完两轮，旧附件、证据、归档和 8 条原费用记录读回一致，新轮只追加 2 条；CTF 默认路径命令、逐 Agent PCAP、独立 VPN 往返、断开及退出均有证据。并发专项实测同一个 Runtime 管理两个同时运行的合成任务，实际命令重叠 0.8112 秒。标准入口移除夹具后拒绝内部供应商，Runtime Pod 重建后单锁恢复。
4. **P9.4 数据与切换。** 旧 P8 入口和写者停止后，取 PostgreSQL 整库、globals 与 MinIO 全盘三份认证加密备份。目标 PostgreSQL 的新物理系统 ID 为 `7706919549140074525`，schema 仍为 `0032`；逐表内容、角色、权限、ACL 和序列的逻辑校验通过，正式启动仅使 fence 从 7 增至 8。原 MinIO 同镜像冷恢复，源权限保持，目标卷按非 root 运行要求做明确属主映射并核对内容。原账号、目录、密钥衍生配置、模型/MCP、历史数据和原卷保留。正式源当时没有任务或已存模型/MCP 密文，非空数据、真实 ACL、大对象、MinIO 两版本与删除标记、IAM 和密文解密由隔离演练证明；没有用空源盘点替代这些验证。
5. **P9.5 正式读回。** 原账号、两种创建能力、五个配置页、旧入口、角色工具和保存目录通过实际浏览器/HTTP 核对；新的 Runtime 唯一持锁且管理 API 有实际轮询。关闭旧宿主的 15811–15819，停止旧应用、Runtime、存储容器和旧 Sandbox Server Pod；旧卷、旧 Server 对象、历史 SQLite PVC、共享 Controller/Ingress 与独立 15830 预览保留。

## 验证

在隔离候选根目录执行：

```sh
export PYTHONPATH="$PWD/packages/contracts/src:$PWD/packages/objects/src:$PWD/services/agent-runtime/src:$PWD/services/blackboard/src:$PWD/services/envd/src:$PWD/services/sandbox-execution/src:$PWD"
make check
.venv/bin/python -m bbx_blackboard.openapi --check
.venv/bin/python -m bbx_contracts.runner | cmp services/agent-runtime/task-runner/protocol.schema.json -
```

- 最终 App 源 `e388370`：2,300 项 Python、5 项 Pi、Ruff、Pyright 全绿，454 项按既有规则排除；HTTP 和 Runner 协议快照一致。流量回归定向 50 项，真实 PostgreSQL 抓包测试 1 项通过。原 P9 普通任务第一轮的 4,704 字节、50 包 PCAP 通过最终 App 认证 API 下载，SHA-256 为 `77dd6ee0156d5a213ad39f3a91ae20685ae0218c9440b3a680fb9428e86cdf1a`；未认证、跨任务和跨轮次请求被拒绝。
- [部署目录](../../deploy/kubernetes/p9/README.md) 的 `check_rbac.py --kubeconfig <文件>` 对正式所用清单通过 44 项真实 API 权限验证。隔离数据演练使用正式恢复路径，确认加密包损坏时不会写目标，PG、MinIO 两个 Pod 更换后仍使用原目标 PVC，数据可读。
- App OCI 为 `sha256:2b70105fcc677fb7d98c93391c64bac42f442c584b94bf4c9405341c3ddfa2f6`，构建源 `e388370`。Runtime OCI 为 `sha256:93fecf984e6926fed773f7f5325dd672b479187e79d24f74ae8c3c47b4b2c916`，构建源 `212d548`。正式部署策略为 `b80adee`；App 只读修复没有改变 Runtime、契约、对象和沙箱执行文件。两镜像没有共用同一源码归档摘要，也未改写原 P7/P8 验收。
- 回执位于原工作区 `.data/p9-k8s-migration-20261111/`。正式为 `data/cutover-ccf0d4949a96/source-cutpoint.json` 与同目录 `old-sandbox-server-retirement.json`；原抓包读回见 `data/pcap-readback-and-standard-app-runtime.json`，并发见 `platform/seat-proof/concurrent-activity.json`。原工作区 HEAD `30ce04a3`、104 项已有状态与原暂存区的指纹均未改变。

## 偏差与待决

- 首次切换在目标业务写者启动前，因应用清单与已有 Secret 字段管理权冲突回退；原 P8 业务数据校验一致，仅原 Runtime 重启后 fence 增一。之后使用五个 Secret 的 UID/资源版本并做服务端 dry-run，再切换成功。保留首轮 `data/cutover-9d79baeb2343/` 的加密备份与回执。正式新库启写后以目标为权威，不能直接启动原源库承接写入。
- 本轮验证了同一 P9 发布中的普通任务续跑，没有验证从 P8 冻结发布跨至 P9 的旧普通任务续跑；切点校验未发现未处理的旧绑定。已有历史读取规则保持，原生 CTF 的续跑权限不变。

## 下一步

浏览器转发断开时按 [使用与部署](../使用与部署.md) 重连即可，集群内服务无需重启。原节点或卷丢失时按独立备份恢复；不将 Retain 卷视作备份。

## 提交与同步

- `c72b269` 与 `d8521a7`：设计正本和回环 8766 合同。
- `c7ba1958` 与 `212d548`：Kubernetes 产品源码和非 root 镜像构建修正。
- `b80adee`：最终部署权限、镜像准入与运维说明。
- `e388370`：普通任务原生流量只读修复。
- `Record the P9 Kubernetes cutover`：本报告、接入说明与交接，不改变运行制品。

同步范围仅为 `feature/p6a-pi-model-runtime`，不更新 `main`。
