# P4j：原 CTF 工具镜像与公开创建验收

原 Kali 工具镜像已在 P1 Docker、P1/G1 Kubernetes 与官方 OpenSandbox 1.2.0 共同验证，包括低身份、cgroup 停止和卷保留。P4i 使用 **Ubuntu 测试变体**验证新增的逐 binding 采集、真实 MinIO 和 Linux 只读解析；该测试不能替代 Kali 与新增采集的组合验收。

## 固定 Kali 构建与本机验收

管理员先从已批准的源恢复**同一原镜像**，并确认原镜像所用的 dumpcap 发行版本。工具不会拉取缺失镜像，也不使用 `latest`、滚动 Kali 包源、替代镜像或旧 envd 服务。必须本机存在：

- Kali OCI index `sha256:c79ded7b50e9a7ea2433e9740c48417ce08601948196ec497ad403c779da58ae`。历史构建日志 `.data/ctf-input-feedback/build-kali.log` 的 amd64 manifest 以 `57982631` 开头，config 以 `249d4f26` 开头；Docker `.Id` 随存储实现可能不同，不能替代已验证的 RepoDigest。
- 官方 Execd `opensandbox/execd@sha256:c75836c46ef4302afccec182d5d7339eb2f905f51e3d73b7aa17bb76c832526c`。两者都必须提供实际 Linux amd64 视图。

```sh
python3 deploy/kubernetes/p4j/image_release.py \
  --source <reviewed-full-git-commit> \
  --dumpcap-version <approved-kali-version> \
  --environment-revision bbx-kali-binding-pcap-v1 \
  --state .data/p4j-kali-release-<new-run>
```

若管理员使用自有仓库，同一 OCI index 可用 `--base-reference <registry/repository>@sha256:c79ded7b50e9a7ea2433e9740c48417ce08601948196ec497ad403c779da58ae`，仍须预先加载。没有批准的原镜像和 collector 版本即拒绝。不会把 Ubuntu 的版本强加给 Kali。原始和派生镜像的 dumpcap SHA-256、发行版本和 libpcap 版本必须一致；原 P4i 根采集探针须在派生摘要下通过。构建使用已审提交的 Git 归档，`--pull=false --network=none`，不安装任何包；两份 FROM 先检查本机可用，这不是对 Docker daemon 全局网络状态的声明。

结果 `image-release.json` 的 `local_overlay_verified` 仅代表上述本机步骤成功，`publish_ready` 始终为 false。正式发布还须把同一派生摘要接入真实 Kubernetes，在公开 Task 创建后证明原官方 Execd、实际低权限 CTF 工具、授权 IPv4/TCP、封存归档、Linux 查询、原停止和卷保留；一条组合用例足够。固定资源配置的 `traffic_sandbox_image` 必须是发布后的同一派生 digest，`environment_revision` 必须与受信 Sandbox 模板一致。公开 capability 的管理员配置引用同一份 Runtime 资源配置，实际 claim 须匹配该任务已冻结的指纹。用户和模型不提供镜像或 Kubernetes 配置。

历史 Kali 工具层约 17.3 GB。导入前同时检查宿主和节点容器存储，节点按 G1 至少留 50 GiB，按原 `docker save | ctr import` 路径流式传递，避免另存大 tar。原制品不可得时停止原镜像接入路径；需要重建则按下一节操作。

## 历史层不可用时重建

若原始 OCI index 和受信归档都不在，使用同一仓库原 `services/envd/Dockerfile.kali` 重建新的工具镜像。上游是 [Kali 官方容器](https://www.kali.org/docs/containers/official-kalilinux-docker-images/)固定 Linux/amd64 摘要 `5cc5e418…`（上游多架构 index `6ec743d9…`）。补齐原本由私有 `kali-mini` 提供的系统 Python 工具，不省去 Sage、angr、pwndbg 或 gem 层。APT 仍校验同一个 Kali 签名；先从官方 CDN 安装 CA，再切该镜像的下载到 HTTPS。滚动包的实际版本保存在 `dpkg-versions.txt`，系统 Python 的安装清单保存在 `python-versions.txt`。Kali 包使用 `Cryptodome` 命名，历史 `Crypto` 接口由 [PyCryptodome 3.23.0 的固定 amd64 wheel](https://pypi.org/project/pycryptodome/3.23.0/#files)补齐。

```sh
docker pull --platform linux/amd64   docker.io/kalilinux/kali-rolling@sha256:5cc5e418c51098ae3c48f9a1d5b3bc74a508588900765a3964259da783652b03
python3 deploy/kubernetes/p4j/rebuild_tools.py   --source <reviewed-full-git-commit> --state .data/p4j-kali-tools-<new-run>
python3 deploy/kubernetes/p4j/image_release.py   --source <reviewed-full-git-commit>   --rebuild-receipt .data/p4j-kali-tools-<new-run>/tools-rebuild.json   --dumpcap-version <version-reviewed-from-collector-version.txt>   --environment-revision bbx-p4j-kali-binding-pcap-v1   --state .data/p4j-kali-overlay-<new-run>
```

重建清单分别记录官方基础、替代本地基础、工具层和派生 Sandbox 的摘要及配方 SHA。新摘要不冒称原 `c79ded7…`，也不承诺 Kali 滚动依赖逐包一致。历史工具功能脚本在只具备本容器回环的隔离容器运行；启动服务器仍只用官方 Execd。重建组合验收用下节同一公开探针，增加 `--capture-receipt .data/p4j-kali-overlay-<new-run>/image-release.json`，会核对原节点身份和可用磁盘再导入实际派生镜像，只跑一个 Task。构建和本机烟测仍不产生生产发布授权。

重建只在暂存副本补两处兼容：为原 angr 的 SymPy 约束保留 [mpmath 1.3.0](https://pypi.org/project/mpmath/1.3.0/#files) 的本地覆盖而不卸载 apt 包，并用 [pycparser 3.0](https://pypi.org/project/pycparser/3.0/#files) 保持原 angr 的解析器接口。两份 wheel 都验证 SHA。若原 pwndbg 直链不可用，可以先从官方 release 下载同一个 .deb，以 `rebuild_tools.py --pwndbg-package <file>` 使用；仍须匹配原配方中的 SHA。同一实际输出的基础文件层与已核准输入一致，BuildKit 本地临时标签只用于构建寻址，不进入 Runtime 配置。

## 隔离的公开创建接缝

用审过的干净候选运行：

```sh
python3 deploy/kubernetes/p4j/infra.py
python3 deploy/kubernetes/p4j/build_fixture.py --candidate <clean-candidate>
PATH="$PWD/.data/p4j-native-ctf-product-20261108/k8s/bin:$PATH" \
  <clean-candidate>/.venv/bin/python deploy/kubernetes/p4j/probe_public.py --candidate <clean-candidate>
```

验证使用显式 `test_only` capability；无 Kali receipt 时只证明 Ubuntu 公开创建接缝。有 receipt 时同一流程运行实际重建 Kali 并执行代表性非 root 工具。两者均使用独立 kind/实际 Cilium、独立 Gateway EndpointSlice、真实 PostgreSQL/MinIO、固定官方 OpenSandbox 和脚本模型。用户先建立自己的空输入组，再用公开 `POST /api/tasks` 创建唯一原生 Task；一个实际 Lead 命令连接隔离目标，随后从实际对象存储读回 PCAP 和索引，用户只读 Linux 包/帧查询和同一原 Task 正退出留卷。生产加载器拒绝 `test_only`。阶段失败保留原环境，使用原 Task 的 `--stop-only` 核对停止，不重放创建。

本轮重建工具层本机标签 `bbx-p4j-ctf-tools:eae04778af44-sixth`，不可变摘要 `sha256:c0f841d8d0ac5c4d8e99372a777bfa2bdc071bb0bb0e8c48e347cb13072d44c4`。执行镜像本机标签 `bbx-p4j-kali-traffic:eae04778af44-1026cd2c`，不可变摘要 `sha256:c7386ae9055d1a31580bb92224b062aa62ef24f18d6301bd9a1f42dd60344f71`。节点实际启动的就是后者，原工具脚本 52 项、独立抓包烟测和完整公开 Task 均通过。留存证据位于 `.data/p4j-native-ctf-product-20261108/`，实际成功的配置修正验证保存在 `k8s/joint-kali-corrected/`，原失败和原停止正证保存在 `k8s/joint-kali/`。该独立验证未启用任何现有部署的发布选项。
