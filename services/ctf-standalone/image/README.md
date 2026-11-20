# 独立 CTF 工具基底

这里构建的是执行工具镜像，**不包含比赛 Lead/Worker 程序或比赛答案**。默认以 `10000:10000` 用户进入 `/workspace`，启动命令是 `/bin/bash`；后续 Agent 镜像需要声明自己的入口。

保留 Sage、Ghidra、pwndbg/GEF、原生编译与调试、angr、Java/JADX、数学密码和常用 CTF 工具；补齐网络/Web、Scapy/OpenCV、音频/二维码等。旧平台的 `/opt/bbx`、`/opt/envd`、`/opt/traffic`、控制入口和专属 sudo 日志配置不进入最终镜像，容器内 sudo 能力保留。移除缓存、工具自身 ELF 的调试段及部分捆绑文档，不修改用户题目文件；`slimming.json` 记录确切处理项。

紧凑化使用 headless OpenCV，保留图像处理而不打包 GUI 后端；删除重复 Node 24/npm 包，默认使用固定 Node 22。删除 JDK 的 `jmods`（不提供 `jlink` 自建 Java 运行时）、Sage 编译 sysroot 中的额外语言 archive、gettext 翻译、Ghidra **尚未安装的扩展 ZIP** 和 BSim 附带 PostgreSQL 源码包；保留已安装的 Ghidra 核心、FunctionID 数据、Java/javac/jar、Sage 和 pwndbg。

另移除 Ghidra 捆绑的 Windows dbgeng 安装包与 `*-src.zip` 源码包，保留 Linux 调试器、PyGhidra 和运行时 JAR。pwndbg 的 portable ELF 已经精简，不再次 strip。清除 Kali nmap 不兼容容器能力边界的文件 capabilities，使普通用户能执行 `-sT`；原始扫描仍取决于实际容器赋予的 capabilities。

Sage 的 `/opt/sage/lib/jvm` 复用系统同主版本 OpenJDK 25，保持原路径可调用；删除重复的捆绑 JDK。相关 Java 编译、JAR 运行和 JmolData headless 路径另有离线验证。

本轮根据体积和解题用途，省去 QEMU user、多架构 GDB、APKTool/dex2jar/Android 资源重打包链。保留原生 GDB/pwndbg、Ghidra 静态分析、Java 和 JADX。Sage 省去 Pandoc、Notebook/JupyterLab 浏览器资产、浏览器版 Jmol 及 GAP/Maxima 图文帮助；保留 JmolData.jar、终端帮助、IPython/Cython、数学静态与动态库，以及 C/C++/Fortran 编译器。John、Hashcat CPU、Poppler/qpdf 仍在。各项先按文件组测算压缩贡献，再合并构建，最终上传包另做实测。

额外安装 Volatility3 2.28.2、zsteg 0.2.14、bkcrack 1.8.1，按 `sources.json` 固定源文件摘要。SecLists 只提供 6 份 Web/用户名通用词典（固定提交、逐文件校验），加上 John 原有字典；不冒充完整 SecLists，也未安装完整 rockyou。构建源码、示例题和缓存不放入最终镜像。

## 构建和验证

在仓库根目录执行，基础工具镜像须已存在：

```sh
docker image inspect bbx-peer-sandbox:20261120 --format '{{.Id}}'
docker image inspect node:22.21.1-bookworm-slim --format '{{.Id}}'
docker buildx build --progress=plain --platform linux/amd64 --provenance=false \
  --output type=image,name=bbx-ctf-tools:20261120,compression=gzip,compression-level=9,force-compression=true \
  services/ctf-standalone/image
.venv/bin/python services/ctf-standalone/image/validate_image.py \
  bbx-ctf-tools:20261120 .data/ctf-standalone-image-20261120/validation
```

本次使用的两个本地基础镜像 ID：

- `bbx-peer-sandbox:20261120`：`sha256:8a103677e66db5fb6a45362ea509f62241167e9484c3bda0205c5f3f9e6deadb`
- `node:22.21.1-bookworm-slim`：`sha256:25b3eb23a00590b7499f2a2ce939322727fcce1b15fdd69754fcd09536a3ae2c`

构建期从 Kali rolling 安装依赖，额外工具的顶层版本及源文件摘要固定，gem/pip 的间接依赖仍由包源解析。最终 Debian、Ruby、Python 包版本写入 manifest；这并非长期可重现的完整依赖快照。保存并校验最终制品后应以制品 SHA-256 和镜像 ID 为准，不用标签代替内容标识。

构建阶段先以 UID 10000、断网执行功能测试，再把结果写入镜像内 manifest。`validate_image.py` 用 testcontainers 再次验证最终镜像，断网、4 CPU、6 GiB 内存、512 PID，不覆盖最终镜像的线程环境变量，不使用宿主固定端口，结束自动清理。结果保存为 `validation.json` 和 `smoke.jsonl`，并将镜像 ID 与 manifest SHA-256 绑定。

本目录静态和离线单元检查：

```sh
.venv/bin/ruff check services/ctf-standalone/image
.venv/bin/ruff format --check services/ctf-standalone/image
.venv/bin/pyright --pythonplatform Linux services/ctf-standalone/image
.venv/bin/python -m unittest discover -s services/ctf-standalone/image -p 'test_*.py'
```

清理脚本在 Linux 构建阶段访问文件 capabilities，因此类型检查显式使用 Linux 目标；macOS 本机不执行该脚本。普通 UID 验证包含 Sage 首次创建缓存，避免 root 构建时的 HOME 污染遮蔽权限问题。

## 导出上传制品

```sh
.venv/bin/python services/ctf-standalone/image/export_image.py \
  bbx-ctf-tools:20261120 \
  .data/ctf-standalone-image-20261120/bbx-ctf-tools-20261120.tar.gz
docker image load --input \
  .data/ctf-standalone-image-20261120/bbx-ctf-tools-20261120.tar.gz
```

导出脚本只接受单个明确 tag，使用 `docker image save` 加 gzip，检查 gzip 完整性、归档 manifest、tag、配置摘要、层存在性及严格小于 **3,000,000,000 字节**；超限不会发布制品。附带 JSON 回执记录普通 tar 字节数、压缩制品字节数、SHA-256、镜像 ID、系统和架构。不会覆盖已有产物。`docker load` 是独立验证步骤，完成后再次执行上面的 `validate_image.py` 并比对镜像 ID；不要把脚本内 `docker_load_verified: false` 当成已完成导入。

Docker inspect、容器展开大小和最终上传归档大小是不同口径。只按实际 `.tar.gz` 大小验收，后续 Agent/SDK 需要重新导出测量，不能保证剩余空间一定足够。此目录不会上传、发布镜像或发起比赛。

## 工具说明与能力边界

镜像内：

- `/opt/ctf-tools/manifest.json`：命令路径、Debian/Python 包版本、Python 环境路由、实际功能测试及 `available/conditional/unavailable` 能力范围。
- `/opt/ctf-tools/manifest.sha256`：manifest 内容摘要。
- `/opt/ctf-tools/worker-tools.md`：后续注入 Worker 提示词的简短工具说明，避免每次列举全部包。
- `/opt/ctf-tools/slimming.json`：移除项和工具调试段缩减记录。
- `/opt/ctf-tools/sources.json`、`wordlists.json`：额外工具来源与精选词典的 SHA-256。

默认 Python 与 Sage Python 是两个环境；科学计算使用 `sage-python`，实际解释器为 `/opt/sage/bin/python`。`sage` 和 `sage-python` 只在各自子进程中使用 Sage 的 pkg-config/编译器，支持 Cython 扩展，不替换其他工具的默认 Python。JADX 输入输出须使用绝对路径；Ghidra headless 在 `/usr/share/ghidra/support/analyzeHeadless`，项目路径不要包含点开头的隐藏目录。PATH 包含 `/usr/sbin`，可直接运行 John；基础字典是 `/usr/share/john/password.lst`。pwndbg 的可选 debuginfod 下载默认关闭。

功能测试使用本地合成 ELF、Java、PCAP、PNG、音频和压缩包，说明这些路径可工作，不代表所有历史脚本或任意题型都兼容。Hashcat 只验证 CPU OpenCL 设备，不假定有 GPU；原始网络报文权限及内存取证的题目专用符号依赖运行环境。镜像不启动 VPN、抓包守护进程或旧平台网络检查流程，保留离线 PCAP 分析工具。
