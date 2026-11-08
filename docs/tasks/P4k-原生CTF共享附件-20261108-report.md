# P4k：原生 CTF 共享附件验证

基线 `adf416e`。按[任务方案](P4k-原生CTF共享附件-20261108.md)完成用户附件进入同一个原生 Task 工作卷。仅显式发布 `shared-inputs-v1` 的专用环境开放；旧 P4j 发布保持无附件，legacy 上传继续使用原 50/200 MiB 上限。命令记录只读，不做审查或处置；本轮未接入 VPN、真实来源或逐题文件 ACL。

1. **明确共享与限额**：创建页说明所选附件由同 Task 团队读取。原生上限单件 16 MiB、合计 24 MiB、最多 20 件；浏览器、上传 API 和创建冻结使用相同限制。既有用户输入组的选中 ID 与共享 ID 必须相等，同一创建事务固定原文件路径、字节数和 SHA-256；重复请求不重建 Task。文件最终权限 0600、同 Task 共同低权限 UID/GID 10000，所以团队可读，不是 Lead 私密文件。用户原件下载保留。
2. **可信物化**：原 Host 只可用原控制身份领取同 Task、PVC 与 Sandbox 的冻结列表和字节。官方 SDK 只写固定的 root-private 临时区，受信安装器校验后降到原低 UID，通过不跟随符号链接的目录 FD 和不覆盖的原子发布写原工作卷；不解压或运行附件。原 64 MiB PVC 不扩容；固定根暂存和工作卷各至多一份数据；安装前核工作卷可用空间至少为本批大小加 16 MiB。冲突、损坏、无空间或回执不明时原对象保留，不发执行凭据。
3. **先确认再运行**：Host ready、Blackboard material（先于 owner、预算和路由）及 activate 分别要求原确认。响应丢失只对账原结果；stop 不等待附件 I/O，也不把取消请求当成正退出。P4f 通过已审批的原恢复历史、原执行身份及同一 PVC 保留首次确认；不重读或写回团队之后修改、移动、删除的文件。

唯一隔离 Kubernetes Task `69e832b1-789b-4943-a2ba-6a12cc115c75` 从普通用户上传和公开创建开始，6/6 通过。实际上传 16,777,216 字节的含 Unicode、引号名称的二进制文件和一份 0 字节文件；第一条脚本模型请求之前，已读回原 PVC 与 Sandbox 的确切确认。随后一个真实 Lead 命令在 UID 10000 下读到两个原始 SHA、大小与 0600 权限；原 Runner、Host、Sandbox 正退出，原两卷留存。实际镜像以已验证的 P4j 工具层为基，使用新固定安装器与 Host。普通发布加载器干读该次实际验收，接受正确镜像对，拒绝 `test_only` 与替换 Host 镜像的回执，未新建 Task 或启用部署。

证据见 `.data/p4k-native-shared-inputs-20261108/` 的本地安装器探针与 `k8s/joint-inputs/evidence.json`（SHA-256 `3c46f9baea747f9006e08e0dca552496142debad4173321f1a7dbba183836ac9`）、同目录 `before-first-model.json` 及 `pg-readback.json`。发布干读与干净测试回执在 `.data/p4k-native-attachments-20261108/`。本地安装器定向验收覆盖正常写入、重读、既有目标不覆盖、父目录符号链接拒绝、FIFO 拒绝和空间不足。恢复历史链通过 PG/代码验证；没有额外创建实际跨代 Task。

定向 PG 1/1（11.06 秒），涵盖 P4j 原配置重载、限额/共享、三处启动门、错原 Pod/PVC、领取/确认与同 PVC 历史；Chrome 1/1（14.8 秒），涵盖旧能力无上传、共享提示、超限前拒和同请求重试；类型与定向静态检查通过；Runtime 定向 20 项通过。物理验收之后仅调整 legacy 上传调用不附带新限额 kwargs，原生实证分支完全相同；旧上传定向单测通过（1.38 秒），未重跑物理。最终干净候选 `make check` 退出 0：2667 通过、483 排除（213.75 秒），Pyright 0、Pi 5。

在干净候选可按下列命令复验；物理及安装器探针见 [P4k 部署说明](../../deploy/kubernetes/p4k/README.md)：

```sh
.venv/bin/pytest -m integration -q services/blackboard/tests/test_native_ctf_publication_integration.py
.venv/bin/pytest -q services/blackboard/tests/test_input_groups.py -k upload_spools_and_hashes_before_store_write
pnpm -C web e2e e2e/native-ctf-create.spec.ts --workers=1
make check
```

七个重叠文件以独审的可逆差量回灌，其余 103 个原始工作文件保持字节与模式。建议一笔本地提交 `Materialize shared native CTF task inputs`，包含合同、Blackboard、Web、可信安装器/Runtime、隔离验收与报告；未推送或切换已有部署。私有 kind、PostgreSQL/MinIO、独立网络、五个临时标签与临时凭据已按归属清理，默认 kube context 未变；见 native 证据目录的 `k8s/cleanup-result.json`。原两份 P4j 镜像以及以下两份本轮制品留在本机，未推送：

| 本机制品 | Tag | OCI manifest |
| --- | --- | --- |
| 附件安装 Sandbox | `bbx-p4k-shared-inputs:20261108` | `sha256:cf92f672344d39569af7c206cff10b723f3da334160e7269b7809bfac16f2c7d` |
| Sandbox Host | `bbx-p4k-sandbox-host:20261107` | `sha256:3f61de372d44bf98693f9cf362cae7edd4a98087392c9d6c44b20a4547dd950f` |

下一步再接入实际授权的任务来源与 VPN；本轮只支持创建时选定的初始附件，不添加运行中换件或自动重写。
