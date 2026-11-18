# 本地 Kubernetes 部署与维护

本手册针对 P9 的单节点 Kind 拓扑：平台位于 `bbx-platform`，复用 `bbx-local-native` 中的 OpenSandbox controller/ingress、Cilium、TUN 和已验任务镜像。[P9 实施报告](../../../docs/tasks/P9-平台全量本地Kubernetes-20261111-report.md)记录了首次切换证据。下列命令从仓库根目录运行，使用显式 kubeconfig 和 context，不修改全局 context。

## 现有站点：检查、重新部署、接入

用部署时的 kubeconfig 和 context 替换变量。需要有权读取所列资源、在现有 App/PG 容器内执行检查，以及变更四个工作负载副本的运维身份。Runtime 的任务 ServiceAccount 不用于维护。

```bash
export BBX_KIND_KUBECONFIG=/path/to/the/owned/kubeconfig
export BBX_KIND_CONTEXT=kind-bbx-local-native-k8s
p9=(python3 deploy/kubernetes/p9/manage.py --kubeconfig "$BBX_KIND_KUBECONFIG"
    --context "$BBX_KIND_CONTEXT" --namespace bbx-platform)

"${p9[@]}" check
```

`check` 只读显示实际 Pod、镜像摘要、PVC/PV、数据库和对象库存摘要、待处理事项与 Runtime 锁，生成指纹。Secret 只读取 UID/资源版本，不输出值。开始前确认没有运行、暂停、删除、清理或未结算的任务与调用；排除未知结果，不能靠重试或清数据通过。重要数据先按下文留独立备份。

1. 暂停其他调用方，停止已确认属于本部署的 15800 浏览器转发，保留其他进程。浏览器页面与会话记录不会被重置。
2. 重新运行 `check`，复核目标集群、摘要与 `pending: false`、`holders: 1`。把该次输出的指纹传给下一条命令。
3. 运行原地重部署。默认只重建 App 和 Runtime；需要重建四个业务 Pod 时加 `--restart-stores`。

```bash
"${p9[@]}" redeploy --expect '<fingerprint-from-check>' --restart-stores
"${p9[@]}" check
```

命令关 App 后再次核对任务，等 Runtime 退出且原库锁清零；若重建存储，先停 MinIO 和 PG，再在原卷启动 PG、MinIO。随后启动 App、Runtime，核对原配置、数据库/对象库存、镜像、卷与唯一的新锁。工作负载规格只修改副本数，不应用零副本安装模板，不改密钥、PVC 或发布文件。若命令报错，保持入口关闭，检查现有集群对象和日志；恢复只使用现有工作负载和卷，不启动原 Docker 站点或覆盖数据库。若状态未明，勿直接再次执行。

转发是浏览器入口，与集群内写者和存储无关。重新打开时用：

```sh
kubectl --kubeconfig "$BBX_KIND_KUBECONFIG" --context "$BBX_KIND_CONTEXT" \
  -n bbx-platform port-forward --address 127.0.0.1 service/blackboard 15800:80
```

用原账号打开 [127.0.0.1:15800/login](http://127.0.0.1:15800/login)，核对黑板和原生 CTF 两种创建能力、Worker/CTF/成果整理/模型/MCP 页面以及已有任务数据。此检查不用创建任务、调用模型或发现外部 MCP。

## 构建与载入候选镜像

先完成源码检查和独立复核。构建机需要 Git、Python 3.12+、Docker Buildx、Node 和与 `web/package.json` 一致的 pnpm。现有脚本载入名为 `bbx-local-native-k8s-control-plane` 的专属 Kind 节点；使用其他集群时，先由操作者把已验 OCI 制品载入目标节点并核对 CRI digest，不能无声导入当前 Docker 节点。

```sh
BBX_RELEASE_SHA=$(git rev-parse HEAD)
BBX_RELEASE_GIT_DIR=$(git rev-parse --absolute-git-dir)
BBX_RELEASE_OUTPUT=/path/to/new/private-artifact-directory
python3 deploy/kubernetes/p9/build_runtime.py --git-dir "$BBX_RELEASE_GIT_DIR" \
  --revision "$BBX_RELEASE_SHA" --component blackboard --output "$BBX_RELEASE_OUTPUT/app"
python3 deploy/kubernetes/p9/build_runtime.py --git-dir "$BBX_RELEASE_GIT_DIR" \
  --revision "$BBX_RELEASE_SHA" --component runtime --output "$BBX_RELEASE_OUTPUT/runtime"
```

输出目录的父目录预先建立且对外不可读。构建只从 Git 归档取源码，使用固定 Linux/amd64 基础镜像和依赖锁，检查非 root 导入、App 网页资源和节点 CRI digest。回执记录 Git commit、tree、源码归档及 OCI 身份；发布使用 `repo@sha256:...`，不用构建标签。重启既有站点无需重新构建已验镜像。更换镜像或发布配置是另一次升级：先在同一拓扑验收新组合及回退，再在关闭写入、释放旧 Runtime 锁的维护窗口逐项更新。保持 Secret、PVC 和数据权威；不要用首次安装命令升级，也不要以旧验收声称新组合通过。

## 首次安装前置与私有输入

首次安装只用于没有现有平台或数据卷的新环境；已有站点使用上方命令。此目录不是创建任意新集群的一键安装器。先核对：

- Kind 有可用的单节点 Linux/amd64、Cilium、现有 OpenSandbox BatchSandbox/controller/ingress 及 TUN。具备 `standard` 任务卷类、Retain 平台卷类对应的 local-path provisioner，以及清单固定的存储、代理和执行镜像；核对实际资源上限能满足两任务并发。
- 已确认 `kube-system` 的 UID、P9 命名空间和 Server 身份，已隔离合成环境。P9 的集群角色、准入策略、网络和 8766 回环规则须通过服务端与实际正反向测试。历史 P7/P8 验收不替代 P9 的真实 Service、DNS、CA 与两个模式。
- 已验私有发布目录包含 `board.json`、`ctf.json`、`board-runtime.json`、`ctf-runtime.json`、`board-acceptance.json`、`ctf-acceptance.json`。两个发布中的 `runtime_resource_config` 分别与对应 Runtime 文件完全相同；验收引用绑定原文件 SHA、规范资源摘要、制品和实测能力。P9 的 `runtime_artifact_sha256` 填已验 Runtime OCI manifest digest 的 64 位十六进制正文，独立记录源码归档 SHA。[字段与占位样例](release-inputs.example.json)不是可用发布或验收。
- 使用严格 TLS SAN：`blackboard-control.bbx-platform.svc.cluster.local` 和 `runtime-control.bbx-platform.svc.cluster.local`。证书含 `subjectKeyIdentifier` 和 `authorityKeyIdentifier`，分别运行 `openssl verify -x509_strict -CAfile <CA> -verify_hostname <服务DNS> <证书>`。证书轮换等 Secret 文件投射，再重载代理并核对新叶证书。
- 拟安装卷为空或已完成受控恢复。MinIO 目标目录必须允许其非 root UID 写入，先用隔离卷实测 provisioner/文件权限；不把旧源卷重新初始化。已有数据、密钥、Vault 和目录的迁移必须另验。

准备一个仓库外的私有目录，权限 0700，认证文件仅操作者可读。上面 JSON 只列字段；四份 `.vars` 实际按每行 `KEY=value` 编写。Runtime 与 App 的服务 token 配对，发送 token 与 App 的 Runtime token 配对；原站迁移/恢复保留原签名及 Vault 密钥。OpenSandbox 签名 Secret `bbx-p3f-signing` 由已验基础设施提供；把同一 resolver token 填入 `sandbox-config.toml.in` 的私有副本。用已验 OpenSandbox 模板做 `sandbox-template.json`，环境 revision 与发布一致。Secret、证书私钥、生产环境值和验收私有目录不进入 Git。

## 首次安装操作

先确认目标没有现有 `bbx-platform`、目标平台卷和同名发布对象；已有任一项时停止，按现有站点或恢复流程处理。以下仅在该前提下执行，`BBX_RELEASE_INPUT` 为上段已验私有目录：

```bash
k=(kubectl --kubeconfig "$BBX_KIND_KUBECONFIG" --context "$BBX_KIND_CONTEXT")
p=deploy/kubernetes/p9
"${k[@]}" apply -f "$p/platform.yaml" -f "$p/storage.yaml" \
  -f "$p/runtime-rbac.yaml" -f "$p/sandbox-admission.yaml" -f "$p/network.yaml"

for name in postgres-auth minio-auth blackboard-auth runtime-auth; do
  "${k[@]}" -n bbx-platform create secret generic "$name" \
    --from-env-file="$BBX_RELEASE_INPUT/$name.vars"
done
for name in blackboard-control runtime-control; do
  "${k[@]}" -n bbx-platform create secret tls "$name-tls" \
    --cert="$BBX_RELEASE_INPUT/$name.crt" --key="$BBX_RELEASE_INPUT/$name.key"
done
"${k[@]}" -n bbx-platform create configmap platform-control-ca \
  --from-file="ca.pem=$BBX_RELEASE_INPUT/ca.pem"
"${k[@]}" -n bbx-platform create configmap platform-publications \
  --from-file="$BBX_RELEASE_INPUT/board.json" --from-file="$BBX_RELEASE_INPUT/ctf.json" \
  --from-file="$BBX_RELEASE_INPUT/board-runtime.json" --from-file="$BBX_RELEASE_INPUT/ctf-runtime.json" \
  --from-file="$BBX_RELEASE_INPUT/board-acceptance.json" --from-file="$BBX_RELEASE_INPUT/ctf-acceptance.json"
"${k[@]}" -n bbx-local-native create configmap bbx-p9-platform-control-ca \
  --from-file="ca.pem=$BBX_RELEASE_INPUT/ca.pem"
"${k[@]}" -n bbx-local-native create configmap bbx-p9-sandbox-template \
  --from-file="template.json=$BBX_RELEASE_INPUT/sandbox-template.json"
"${k[@]}" -n bbx-local-native create secret generic bbx-p9-sandbox-server-config \
  --from-file="config.toml=$BBX_RELEASE_INPUT/sandbox-server-config.toml"
"${k[@]}" apply -f "$p/sandbox.yaml"
"${k[@]}" -n bbx-platform set image deployment/blackboard "app=$BBX_APP_IMAGE"
"${k[@]}" -n bbx-platform set image deployment/runtime "runtime=$BBX_RUNTIME_IMAGE"
```

两个镜像变量必须为各自已验的 `repo@sha256:...`。用零副本配置先校验 Service、准入、Secret 引用、模板及镜像，再按顺序启动并等待就绪：PostgreSQL → MinIO → App → Runtime。对于确认是全新初始化、或已完成存储恢复的环境，可逐项使用 `kubectl ... -n bbx-platform scale statefulset/postgres --replicas=1` 和 `rollout status statefulset/postgres`，随后同样处理 `statefulset/minio`、`deployment/blackboard`、`deployment/runtime`。先关闭用户入口，用已验合成流程证明两种模式、身份拒绝、归档、流量、存储重建与唯一 Runtime 锁；正式阶段使用原身份和真实数据的只读核验。缺任一验收项不启正式写入。服务端权限实测工具为 `python3 deploy/kubernetes/p9/check_rbac.py --kubeconfig <文件>`；它会创建并清理测试命名空间，不是常规只读检查。

## 原生 CTF 会话展示（P13 候选，已暂停）

以下是尚未发布的 P13 设计与部署预案，仅供恢复开发后的验收和评审。当前 P12 没有该收集器、准入策略或归档导入接口。暂停期间不要执行本节操作。当前状态与恢复顺序见 [P13 暂停交接](../../../docs/tasks/P13-会话显示与职责边界-20261112-handoff.md)。

会话收集器是独立部署，不接管 Runtime、也不启动或恢复任务。它只读取当前原 Runner 的已确认日志；原 Pod 消失后保留已发布的有界展示。需要补回终态记录时，由平台操作者用已校验原始备份和数据库确认游标导入展示，页面注明来源。原会话、费用和未确认调用均不变。

发布前先用同版本隔离集群验证准入。通过后，用同一份已审源码运行 `python3 deploy/kubernetes/p9/conversation_admission.py` 生成准入策略及绑定，先应用这两项，再应用 `conversation-collector.yaml`。绑定失败或固定投影命令未通过允许/拒绝实测时，保留零副本。移除时先停止收集器并撤销角色绑定，再移除准入。通过后使用已验 Runtime 制品摘要覆盖收集器占位镜像；不要改变执行 Runtime 部署。

专用 Secret `bbx-conversation-collector-auth` 的 `token` 只供收集器和 App：App 的 `CONVERSATION_COLLECTOR_TOKEN` 引用同一键。不要复用服务、Agent 或模型令牌。收集器不需要数据库、MinIO、模型或 OpenSandbox 凭据。

## 备份、恢复与回退

- Retain 只防止 PV 随声明自动回收。节点、卷或宿主丢失仍需要独立备份。记录工作负载/镜像、密钥引用、配置摘要及卷身份；在仓库外另保全原签名/Vault 密钥、数据库/对象认证和信任链。
- PostgreSQL 从停写切点备份整库（`pg_dump --format=custom --create`）和全局对象（`pg_dumpall --globals-only`），分别认证加密。独立恢复到新卷，严格核对角色、所有者、ACL、扩展、库/表数据、序列和大对象。逻辑恢复的物理系统 ID 可以不同；本手册的同卷重启必须相同。
- MinIO 同镜像、同版本、同目录与单盘形态才使用 P9 的全盘冷备，包括隐藏元数据、桶版本、删除标记和 IAM。先停源写者及 MinIO，再只读归档；不要用 `mc mirror` 替代版本历史。目标为空卷，先验证认证包和归档路径，再恢复并校验字节/属主；只在目标按原运行身份做必要的权限映射。启目标后读取版本与对象、校验 IAM 和数据。
- [data_transfer.py](data_transfer.py) 提供 P9 实际使用的认证加密流、恢复前认证、归档清单与 PostgreSQL 语义摘要原语。先在隔离目标实做恢复演练，再申请生产切点；工具不替操作者决定源/目标和写入权威。不要把恢复流或凭据放在命令行、日志或 Git。
- 新目标的 App 或 Runtime 启动即可能写入。只有在目标业务写者从未启动、无新写且旧写者仍可受控恢复时，才能直接回旧环境；启写后保目标权威，冻结并验证独立备份再做前向修复或另行迁回。原地同卷重启不触发任何恢复或迁库。
