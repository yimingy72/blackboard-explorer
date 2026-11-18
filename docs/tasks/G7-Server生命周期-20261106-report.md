# G7 Server 生命周期接缝验证报告（2026-11-06）

状态：主逻辑及版本一致性增量均已独立审核通过，获准独立提交。开发执行：actual GPT-6.1 Sol，max。仅新增 `deploy/kubernetes/g7/` 与本报告，不修改官方基线、P1/P2/P3/Runtime/shared 锁。

## 1. 完成范围与固定依据

1. 固定 1.2.0 Server 的 `_informers` 按 GVR/namespace 常驻，正常运行中没有 namespace/TTL/容量淘汰；`stop_informers` 是进程级退出清理。已有 resync/watch 参数不能关闭该生命周期。[固定 K8sClient 源码](https://github.com/opensandbox-group/OpenSandbox/blob/62a64f0b48cdd6b410f36e53c74dc7559c87719a/server/opensandbox_server/services/k8s/client.py) 与 [官方移除旧开关的 PR #1926](https://github.com/opensandbox-group/OpenSandbox/pull/1926) 支持这一判断。
2. 已检查公开后续修复：[#2599](https://github.com/opensandbox-group/OpenSandbox/pull/2599) 针对按对象写入失效，不负责 namespace informer 回收；[#2571](https://github.com/opensandbox-group/OpenSandbox/pull/2571) 是 Go Controller 静态 namespace 范围，与 Server 动态 Task namespace 的生命周期不同。2026-11-06 查询的[最新稳定发布](https://api.github.com/repos/opensandbox-group/OpenSandbox/releases/latest)仍是 1.2.0。本批没有升级组件或采用周期重启。
3. 采用已批准的 Server-only A 接缝：新布尔选项默认 `true`；只有显式 `false` 的新 profile 对精确 BatchSandbox GVR 不创建 Server informer，现成 GET/LIST/创建 polling fallback 直接工作，保留 namespace/selector/读限流；新 direct GET/LIST 带 10 秒连接、30 秒读超时。已有 cluster LIST 本来就有同样预算。Controller/Ingress/其他 GVR reactor 不改。
4. 补齐关联的 HTTP TenantProvider 历史缓存：正数 `cache_max_entries` 才启用 TTL+max-stale 懒清理/LRU；默认 0 保留旧行为。lookup/list/store 三条路径清理，最大条目数有限。活跃 key 被 evict 后重新由原 HTTP 权威接口解析；singleflight 等待者保留已完成结果，不依赖该条目仍在 LRU。401、故障/max-stale、RBAC 没有放宽。[固定 HTTP Provider](https://github.com/opensandbox-group/OpenSandbox/blob/62a64f0b48cdd6b410f36e53c74dc7559c87719a/server/opensandbox_server/tenants/http_provider.py) 的无限历史提示集不再拖着后台扫描已过期 namespace。
5. 后台无租户上下文的定位仍沿用上游顺序：默认/已观察/有限租户提示后，执行原有按 `opensandbox.io/id=<id>` 限定的 cluster LIST。缓存不是活跃 Task 清单，缓存被淘汰不会改变 Task Namespace，也不需要新增授权或 Controller 生命周期。既有 cluster LIST 若被权限拒绝，仍按原未定位路径处理，本批不补新的授权策略。
6. 独审后收紧构建版本一致性：`setup` 按本次 identity、Dockerfile、Server profile、Task template 和运行地址计算内容 SHA256，通过 `run.sh` 已传入的 runtime values 写入官方 Chart 的 `server.podAnnotations["bbx.archv2/content-sha256"]`。同 tag 内容改变也会更新 Pod 模板；同内容稳定，不引入定时重启。`probe.verify` 在 Task 创建前严格比较远端 `/etc/bbx-g7-patch.json` 与本地本次 `STATE/identity.json` 的完整 JSON，拒绝旧 patch 或旧源文件身份，不能只把远端身份记录为通过。

固定基线源码 `62a64f0b48cdd6b410f36e53c74dc7559c87719a`，源压缩包 SHA256 `c2ca1bd964d5f1933f69f01d210aabfe83d8174761b1487a5b1bd5f790abdf2b`。官方 Server BOM digest 为 `sha256:25bebdcf1cf7724eb923faecbdbbf82d4038c6ab35c736151574726d14006f30`。仅在忽略目录解包/patch，5 个上游文件（4 个运行模块及配置说明）固定 patch SHA256 为 `b401a740b89ff2e6cfe8bea0adda1bcc671d55b8ef3639886324c006cf02dade`。

实际测试 overlay `bbx-archv2-server-g7:bbx-g7-direct-1` digest `sha256:12afc58df70d5b38a23fe51e63756af09a2233638db2147980c8b789d3cc3aa8`；重建的 BuildKit attestation 可改变 root index，运行源文件与 patch 哈希可按内置 identity 核对，不把该 root hash 冒充可位级复现输出。官方 Execd/Controller/Ingress 沿用固定 1.2.0 BOM，均未改源码。

## 2. 必要验证与实际结果

以下命令在仓库根目录执行；完整重现步骤见 `deploy/kubernetes/g7/README.md`。

```bash
python3 deploy/kubernetes/g7/build.py
docker run --rm --name bbx-archv2-g7-unit --network none --memory 768m --cpus 1 \
  -v "$PWD/deploy/kubernetes/g7/tests:/tests:ro" \
  --entrypoint /app/.venv/bin/python bbx-archv2-server-g7:bbx-g7-direct-1 \
  /tests/test_server_lifecycle.py
bash deploy/kubernetes/g7/run.sh
bash deploy/kubernetes/g7/cleanup.sh
PYTHONPATH=deploy/kubernetes/g7 services/sandbox-execution/.venv/bin/python \
  -m unittest discover -s deploy/kubernetes/g7/tests -p test_release_identity.py
```

| 必要组 | 实际证据 | 结果 |
| --- | --- | --- |
| fake 1000 namespace/key 轮转 | GET/LIST/subscribe 覆盖全部 1000 namespace；Batch informer start 调用 0、map 条目 0；tenant cache 始终 ≤32，TTL 后 list 为空；evict active 后可权威重解析；完成的 singleflight 结果不因 LRU 淘汰丢失 | 通过 |
| 创建 fallback 与历史提示淘汰 | subscribe 返回 None 后 pending→404→不同 UID ready，轮询完成；503 延续到 504 `KUBERNETES::POD_READY_TIMEOUT`；无额外 create_workload；active 被 evict 后原 label-scoped cluster LIST 能定位待清理对象；默认 true/0 的行为和其他 GVR 显式 watcher 保持 | 通过 |
| 真实轻量双租户 | 单节点 kind v1.34.0，2 CPU/6 GiB，kindnet；Server/Controller/Ingress/两 Task namespace；真实 create→Pod Ready→查询→删除；LRU 上限故意为 1，A 被 B 淘汰后重新 resolve 查询与清理；B 查 A ID 为 404；两个 CR 均删除 | 通过 |

最终本地 2 个测试方法（前两组）全部通过，约 1.019 秒。第三组第一次实际贯通成功：A 创建至 SDK 就绪约 3.990 秒，B 约 2.061 秒。20 次交替 `get_info` 的 Kubernetes `apiserver_request_total` 增量是 GET=20、LIST=0、WATCH=0、POST=0、DELETE=0；客户端观测 min=17.031 ms、P50=22.107 ms、P95=41.851 ms、max=46.438 ms。权威测试接口最终 A/B 各解析 13 次。该指标含单节点同时运行的官方组件，不作为 Server 独立容量上限或生产 SLA。

新 profile 明确设 `read_qps=20/read_burst=40`、`cache_max_entries=1/max_stale_seconds=0`。每次 cache miss 和 direct API 读的负载均是有意代价；20 次交替读体现了最小 LRU 下每次查询重新解析的成本，未声称缓存命中场景同样产生这些请求。

版本一致性增量的 2 个定点方法通过（约 0.229 秒）：真实固定官方 Chart 的 `helm template` 渲染证明同 tag 的 patch identity 改变、运行地址改变分别得到不同 Pod 模板注解，同内容哈希稳定；脚本化 kubectl 返回旧 patch 哈希/旧文件哈希时，verify 在端口转发和 Task create 前拒绝，而完整匹配可通过身份门禁。增量未改变上游运行模块或集群控制路径，没有新建集群、重跑 1000 namespace 或工具矩阵。原三组实际证据仍按原运行记录保留，不把渲染/模拟负例当作另一次真实 Pod 发布证明。

`ruff format --check`、`ruff check`、独立 `pyright --project deploy/kubernetes/g7/pyrightconfig.json`、两个 shell 的 `bash -n` 均通过。Pyright 只覆盖宿主脚本；上游运行模块/本地两组断言在固定官方镜像真实导入并执行。未新增运行依赖/共享锁，未运行无关 root 全量测试；本批目录不在根 Makefile lint、pyright、pytest 输入范围内，本批相对提交前 HEAD 不改变根覆盖输入。主代理已确认复用既有相同输入的通过门禁与本批专门检查，不重复根全量或集群验证。

脱敏可提交证据：`deploy/kubernetes/g7/evidence/20261106.json`；本机证据在 `.data/bbx-archv2-g7/evidence.json`。测试凭据未打印、未提交；异常不输出可能携带 route token 的正文。创建前有独占 attempt 落盘，中断恢复先核原 CR，不把 unknown 当作不存在重派。

## 3. 清理、偏差与未验证

- 本批只创建 `bbx-archv2-g7` 集群/网络、system 与两 Task namespace；无 CTF 大层。节点归属在变更配额前验证，使用专用 kubeconfig/本地固定 kind/Helm。默认 `docker-desktop` context 不变，既有业务平台/CNI 未动。
- 已执行 scoped cleanup：本批集群、网络、自建 overlay tag、测试凭据和专用 kubeconfig 删除；官方/Python/CTF 镜像及官方下载缓存保留。复现的脱敏统计、无凭据 attempt 历史保留在忽略目录。
- **本批只提供 G7 非池化 BatchSandbox + HTTP 租户提示缓存的局部有界证据，未宣称整个 G7 或 G1/G2 通过。** 其他 GVR 的 snapshot/template 显式 reactor、pool、长时进程 RSS/线程 soak、生产高并发权威接口、租户凭据轮换/故障的全部矩阵均未测试。
- 新 kind 使用 kindnet；没有替换现有 CNI，没有重新跑 Cilium/G1 工具矩阵。VPN、可信出口与生产授权仍属后续独立接缝，本次不以两租户控制链推断这些能力。
- direct 读取故障最多受单次 API connect/read 预算限制；上游创建等待可能被单次同步 API 调用超出其较短逻辑 deadline，不能承诺严格等于 polling timeout。权威接口不可用且 LRU 已淘汰时按原 unavailable 语义失败，不以历史提示冒充授权。

## 4. 下一步与提交建议

主逻辑与官方 Pod 模板内容身份、远端完整 identity 拒绝门禁的独立复核均已通过。获准单独提交 `deploy/kubernetes/g7/` 的 18 个文本文件 + 本报告，标题 `fix(infra): bound OpenSandbox batch and tenant caches`；正文按 AGENTS 标识并注明 `Actual developer: GPT-6.1 Sol / max`。不夹带 P2/P3/旧 WIP，不 push/merge。

再决定是否为生产新 Task profile 采用该候选及 tenant 上限；维持每 Task 独立 Namespace，以明确的新部署边界开启。全局 G7 若要覆盖 snapshot/template/pool，需要另行分配其生命周期方案，不能将本批局部结果外推。
