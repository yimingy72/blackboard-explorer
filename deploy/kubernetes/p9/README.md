# P9 · 现有本地 Kubernetes 平台

部署范围和切换条件见 [P9 任务说明](../../../docs/tasks/P9-平台全量本地Kubernetes-20261111.md)。这里的清单针对现有 `kind-bbx-local-native-k8s`；使用该集群的显式 kubeconfig，不切换全局 context。OpenSandbox controller、ingress、原执行镜像和 TUN 继续在现有集群中运行。

- `platform.yaml`：平台 Namespace、持久卷类、应用、共同 Runtime、内部 TLS、前门 Service。应用与 Runtime 初始为零副本；把两个 `:verified` 镜像引用替换成验收过的 OCI digest 后才启动。
- `storage.yaml`：PostgreSQL 和 MinIO。两个 StatefulSet 初始为零副本，最终卷名分别为 `data-postgres-0` 和 `data-minio-0`。卷使用 `bbx-platform-retain`；PostgreSQL 数据目录为 `/data/pgdata`，MinIO 为 `/data`。从现站迁移时，先恢复再启动写者。
- `runtime-rbac.yaml`、`sandbox-admission.yaml`：只给 Runtime 和 P9 OpenSandbox Server 自己的任务权限，限制两条创建任务资源的路径。Runtime 的 kubeconfig 读取轮换的 Pod tokenFile，不含用户或管理员凭据。`check_rbac.py --kubeconfig <文件>` 在可清理的任务空间复核实际允许和拒绝的操作。
- `network.yaml`：内部服务互联与任务控制边界。普通执行环境的已发布路由及 DNS 由任务配置提供。Sandbox host 的 `8766` 仍只绑定任务 Pod 内回环；Runtime 通过 Kubernetes API 短时核对原 Pod 的就绪，平台网络不对该端口放行。
- `sandbox.yaml` 和 `sandbox-config.toml.in`：P9 专属 Server 及其配置模板。`bbx-p9-sandbox-server-config` 的 `config.toml` 由部署者在内存中替换 resolver token 后写入 Secret。`bbx-p9-sandbox-template` 复制现有可信 Pod 模板，只更新模板名和 `BBX_ENVIRONMENT_REVISION` 为 P9 发布值；原 P8 ConfigMap 保持原样。

`bbx-platform` 需要 `postgres-auth`、`minio-auth`、`blackboard-auth`、`runtime-auth`、`blackboard-control-tls` 和 `runtime-control-tls` Secrets，以及 `platform-control-ca` 和 `platform-publications` ConfigMaps。`bbx-local-native` 内的 P9 Server 使用相同信任锚的 `bbx-p9-platform-control-ca` ConfigMap，以及现有 ingress 签名 Secret 的引用。服务 DNS `blackboard-control.bbx-platform.svc.cluster.local`、`runtime-control.bbx-platform.svc.cluster.local` 应分别出现在证书 SAN。原认证值从受保护的发布来源传入，保留加密配置的密钥；凭据不进清单和输出回执。

只在所用镜像、配置、身份、证书和数据均通过 P9 验收后开启平台写入。生产 `platform-publications` 必须包含匹配同一 Runtime 配置 digest 的两套已验发布与验收文件。仅预验夹具使用的入口、凭据和模型目录不得挂到正式 Deployment。发生故障时先按任务说明确认数据写入边界再回切。

本机访问使用现有 Service，不修改 Kind 的 API 端口映射：

```sh
kubectl --kubeconfig "$BBX_KIND_KUBECONFIG" --context kind-bbx-local-native-k8s \
  -n bbx-platform port-forward --address 127.0.0.1 service/blackboard 15800:80
```

打开 `http://127.0.0.1:15800/login`。应用 Pod 重建时重新运行上面的命令；即使浏览器转发断开，平台仍在集群内工作。
