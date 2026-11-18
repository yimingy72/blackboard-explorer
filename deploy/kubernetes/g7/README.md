# G7 Server 生命周期接缝

这是固定 OpenSandbox 1.2.0 的测试 overlay。只有新 profile 开启补丁选项；P1 和原部署值不变。补丁不属于官方 1.2.0，不能拿官方镜像直接配置这两个新字段。

- `kubernetes.batchsandbox_informer_enabled=false`：仅 Server 的 `sandbox.opensandbox.io/v1alpha1/batchsandboxes` GET、LIST、创建等待不再申请 informer。沿用现有轮询、namespace/selector、read limiter，直读设连接/读超时 10/30 秒。其他 GVR 的显式 reactor 不变。
- `tenants.cache_max_entries>0`：HTTP TenantProvider 懒清理超过 TTL+max-stale 的条目，用 LRU 限制历史 key。淘汰只影响提示缓存，活跃 key 下次重新向原权威接口解析。新 profile 为验证两租户轮转故意设为 1；普通候选可按容量选择正上限。默认 0 保持旧缓存行为。
- 同一批 singleflight 的等待者直接使用已完成结果，避免结果刚被其他 key 淘汰便误报不可用。401、故障/max-stale、租户授权、Controller、Ingress 均保持原语义。

HTTP 缓存不是活跃 Task 清单。无租户上下文的后台查找先查有限提示集，再走上游原有 Sandbox ID label 限定的全 namespace LIST；需要原有 ClusterRole 的权限，不增加 RBAC。如果已有权限拒绝该 LIST，仍按上游未解析/重试处理，不保证自动清理。

## 固定来源与构建

`build.py` 校验 P1 缓存中的源码压缩包 SHA256，从压缩包读取固定源文件/官方 charts/CRD，只在 `.data/bbx-archv2-g7` 应用 patch；不改缓存源树或官方 tag。镜像基于官方 Server BOM digest，只替换 4 个运行模块并内置 patch 身份。没有新增 Python 运行依赖。

`setup` 将本次 identity、Dockerfile、Server profile、Task template 及运行地址的内容哈希写入官方 Chart 的 `server.podAnnotations`。`run.sh` 传入该 values 文件，所以相同镜像 tag 下的构建/运行内容变化也会更新 Pod 模板；相同内容的重复执行不主动更新。验证开始时严格比较远端 `/etc/bbx-g7-patch.json` 与本次 `.data/bbx-archv2-g7/identity.json` 的完整 JSON；不相等会在创建 Task 前拒绝。此处是构建版本变更发布，不用于周期重启或回收 informer。

使用 P1 已固定的 kind v0.30.0、Kubernetes v1.34.0、Helm v3.19.0。工具和源包未缓存时，先运行只准备下载缓存的 `bash deploy/kubernetes/p1/prepare.sh`。宿主 SDK/检查工具复用 `services/sandbox-execution/.venv` 的独立锁环境；缺少时在该目录执行 `uv sync --frozen`，不改根 workspace/锁。

```bash
python3 deploy/kubernetes/g7/build.py
docker run --rm --name bbx-archv2-g7-unit --network none --memory 768m --cpus 1 \
  -v "$PWD/deploy/kubernetes/g7/tests:/tests:ro" \
  --entrypoint /app/.venv/bin/python bbx-archv2-server-g7:bbx-g7-direct-1 \
  /tests/test_server_lifecycle.py
```

## 最小真实复现

```bash
bash deploy/kubernetes/g7/run.sh
bash deploy/kubernetes/g7/cleanup.sh
```

只创建 `bbx-archv2-g7` kind 单节点，使用专用 kubeconfig、命名网络和 2 CPU/6 GiB 限制。复用同名节点前先断言 kind ownership；不会修改默认 kubeconfig、docker-desktop CNI 或现有业务平台。只导入官方固定 Execd/Controller/Ingress、小型 Python 镜像与本批 Server overlay，不导 CTF 镜像。此集群使用 kindnet，仅验证控制链，不能作为 G2/Cilium 证明。

所有 key/签名均为本地新生成测试值，通过 Secret 引用，私有状态目录权限 0700、凭据文件 0600。输出只含统计、布尔断言及 patch 身份；不打印测试 token、Pod annotation 或原始事件。异常只输出类型和位置。

真实验证创建两个独立 Task namespace，对缓存上限 1 的 A/B 轮转验证重新解析、跨租户查不到对方 ID、原 key 能清理；20 次交替 GET 的 API 指标增量与客户端时延也写入证据。SDK 关闭生命周期自动重试，创建前落盘 attempt；中断后先按 namespace/label 查原 CR，只恢复已存在的单个绑定，未知结果不会直接再创建。若 attempt 已在而 CR 不存在，先人工核对该次确定结果；不要删除标记盲重试。

`cleanup.sh` 确认节点归属后删除本批集群/空网络、自建镜像与测试凭据；官方/Python/CTF 镜像和官方下载缓存保留。已完成 evidence、无凭据 attempt 历史可留在忽略目录。提交的脱敏样本在 `evidence/20261106.json`；本机最新原始统计在 `.data/bbx-archv2-g7/evidence.json`。

## 检查和边界

```bash
services/sandbox-execution/.venv/bin/ruff format --check deploy/kubernetes/g7
services/sandbox-execution/.venv/bin/ruff check deploy/kubernetes/g7
services/sandbox-execution/.venv/bin/pyright --project deploy/kubernetes/g7/pyrightconfig.json
bash -n deploy/kubernetes/g7/run.sh deploy/kubernetes/g7/cleanup.sh
PYTHONPATH=deploy/kubernetes/g7 services/sandbox-execution/.venv/bin/python \
  -m unittest discover -s deploy/kubernetes/g7/tests -p test_release_identity.py
```

本批三组验证为：1000 个 fake namespace/key 轮转及 legacy 默认；创建轮询的替换/404/503/504、不二次创建与淘汰后的后台定位；真实两个租户控制链。版本一致性修复额外只验证官方 Helm 模板随内容变化及旧 identity 的拒绝门禁。没有做全局长期内存压测、snapshot/template/pool 生命周期、生产租户服务容量或 VPN/Cilium 重验。每次 cache miss 都增加权威接口查询，direct 模式每个读取增加 API GET，需依场景为单 Server 设置读限流和容量；这里的短测不代表生产 SLA。
