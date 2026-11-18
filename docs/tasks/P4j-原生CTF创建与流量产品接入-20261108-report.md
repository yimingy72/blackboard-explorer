# P4j：原生 CTF 创建、流量查看与工具镜像验证

基线 `eae0477`。本片完成[任务方案](P4j-原生CTF创建与流量产品接入-20261108.md)中的正式创建入口、现有流量查看器接入，以及重建工具镜像的实际验证。普通部署默认仍走原创建流程；配置了专用原生 CTF 部署才开放新环境。旧任务历史仍可读，不承诺与旧执行器同库混跑。命令记录只读，不做审查或处置。

1. **公开创建**：创建页展示管理员发布的环境和固定模型版本。浏览器只提交环境引用、模型和本人空输入组；原 Task、输入组绑定和原生启动意图同事务入库。重复请求返回原 Task；服务器冻结模型别名、汇率和实际 Runtime 配置，不把浏览器备注转成网络授权。Runtime 只接受与原配置指纹一致的领取请求。当前不支持初始附件或用户指定目标。
2. **流量查看**：复用原 Task 的流量入口、等待封存、来源筛选、Linux 包与帧查看及授权 PCAP 下载。实际覆盖限管理员预先设置的数值 IPv4 TCP 目标。新创建入口不开额外 helper 或推理配置开关，原有受信接口不变。
3. **镜像接入**：本机可用镜像和历史制品中未找到原 `c79ded7…`，按原 CTF 工具配方在已固定的 Kali 基础上重建为新版本。原配方及有效重建配方 SHA、实际包/工具清单均保留。最初工具烟测为 51/52；修正旧 angr 的解析库兼容后，最终本机隔离容器工具烟测 52/52。派生镜像保留工具层并使用官方 Execd，实际本机抓包烟测封存 22 包，无丢包和截断。未把工具容器的 52 项测试当成 Kubernetes 低权限执行的全部覆盖。

| 本机保留制品 | Tag | OCI manifest |
| --- | --- | --- |
| 重建 CTF 工具层 | `bbx-p4j-ctf-tools:eae04778af44-sixth` | `sha256:c0f841d8d0ac5c4d8e99372a777bfa2bdc071bb0bb0e8c48e347cb13072d44c4` |
| 派生捕获 Sandbox | `bbx-p4j-kali-traffic:eae04778af44-1026cd2c` | `sha256:c7386ae9055d1a31580bb92224b062aa62ef24f18d6301bd9a1f42dd60344f71` |

隔离 Kubernetes 通过公开 `POST /api/tasks` 创建真实 Task `45ad93db-1291-4bad-b975-48e0d3ddbceb`，7/7 通过。脚本模型调用一次真实 Lead 命令，以 UID 10000 连隔离目标，并实际导入 pwntools、Z3、Crypto、angr、构造 angr Project 和执行 GDB。MinIO 读回同一来源原 PCAP 26 包与原索引；用户授权的 Linux 解析按原 binding 查询包及帧，原 Runner、Host、Sandbox 停止有正证，两卷留存。普通发布加载器随后使用该次的实际配方、制品和验收记录干读，接受 `verified`，拒绝 `test_only`，不公开私有配置且没有启用部署或再创建 Task。

首次公开 Task `4f4c4fbb-da80-48c0-9e85-65f1b73c6c54` 启动时发现任务的自动派工选项覆盖了原角色配置，导致重载时工具合同不合法；原 Task 按原身份停止，未签发执行凭据、没有模型或命令执行。修复将两份配置分别冻结，同一 PG 测试验证持久角色配置可实际重载；修正版用同一镜像新建独立 Task，不改写原 Task。

修正版干净候选 `make check` 退出 0：2659 通过、483 排除（212.02 秒），Pyright 0、Pi 5。定向 PG 1/1（6.38 秒），核对旧摘要、原子回滚、原组权限、并发幂等、发布戳/重新认证、持久角色配置重载、原 Runtime 领取和空流量状态。真实 Chrome 定向创建场景 2/2（11.0 秒），合同生成、TS 和定向 lint 通过。部署/镜像工具定向 8 项与原 Runtime 资源相关定向 15 项通过。复现命令见 [P4j 部署说明](../../deploy/kubernetes/p4j/README.md)，管理员配置见[任务方案](P4j-原生CTF创建与流量产品接入-20261108.md)。

在干净候选运行必要的定向检查（Chrome 可用时把 `BBX_E2E_BROWSER` 指向实际可执行文件）：

```sh
.venv/bin/pytest -m integration -q services/blackboard/tests/test_native_ctf_publication_integration.py
pnpm -C web e2e e2e/native-ctf-create.spec.ts e2e/ctf-settings-create.spec.ts --grep '原生 CTF 创建|CTF 创建允许' --workers=1
.venv/bin/pytest -q deploy/kubernetes/p4j/tests/test_image_release.py
```

证据根目录 `.data/p4j-native-ctf-product-20261108/`：`kali-preflight/image-release.json`（原镜像缺失）、`kali-rebuild/sixth/tools-rebuild.json` 和工具烟测、`kali-overlay-final/image-release.json`、`k8s/joint-kali-corrected/evidence.json`（SHA-256 `fb892cd16fe84e5cd967d52544a91086b3581e9bee8b17db2e3026e3dd9969bc`）、同目录封存 PCAP/索引、首次的 `k8s/joint-kali/material-profile-readback.json` 和 `original-stop-readback.json`、`normal-publication-readback.json`、`native-ctf-create.png`。

原 110 个工作文件保留；6 个与本片重叠的文件以独审后的可逆差量回灌，其余 104 个字节和模式原样。建议一笔本地提交 `Publish scoped native CTF task creation`，包含发布合同、Blackboard、Web、部署/镜像工具及本报告。私有 kind、两套 PostgreSQL/MinIO、独立网络、12 个临时镜像标签与临时凭据已清理，见 `k8s/cleanup-result.json`；默认 kube context 未变，表中两份交付镜像及实际证据保留。未推送 Git 或镜像，也未切换已有部署。

下一步补齐附件安全物化到原工作卷，再按需求验证真实来源和 VPN。若要对 20 个重型会话承诺容量，另测实际负载；本片不扩大 P4i 的席位并发结论。
