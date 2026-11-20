# 独立 CTF 执行工具镜像交付报告

任务：`CTF-standalone-image-20261120`。分支：`codex/ctf-standalone-image-20261120`。

已完成构建、归档导出、重新加载与加载后的工具验收。最终上传归档为 **2,787,377,598 字节（2.787 GB）**，低于 3,000,000,000 字节上限，剩余 212,622,402 字节（约 213 MB）。

## 1. 完成内容

1. **人数与增员方案。** 已单独提交设计修订 `567e5ba`：初始 20 个同级 Worker，后续由 Lead 查看实际资源并决定增员；删除容量预约、容量队列、自动阈值拒绝、强制分批、自动缩容和复杂 watchdog。必要的创建幂等、真实错误回执与正常退出清理保留。这是独立版方案更新，不表示当前工具基底已经包含 20 个运行中的 Worker。
2. **镜像准备。** 使用已有 CTF 工具镜像，保留 Sage、Ghidra、pwndbg 等核心工具，补齐工具并清理旧平台服务。通过清理后的文件系统生成新镜像层，避免删除的旧文件仍留在上传包中。Sage 复用系统同主版本 JDK；OpenCV 使用无界面版本；清除旧 Node 组件、开发 jmods、未安装的 Ghidra 扩展安装包及源码。保留 Zig、FunctionID、Go typeinfo、数学静态库及 C/C++/Fortran 编译器。
3. **上传合同。** 官方空表单已确认单文件支持 `.tar / .tar.gz / .tgz`，文案为“单包上限 3 GB”。本次严格按上传文件小于 `3,000,000,000` 字节验收，不用 Docker inspect 大小或展开文件系统大小代替。
4. **导出与校验。** `export_image.py` 将单个明确 tag 的 `docker save` 流压缩为 gzip，同时记录原始 tar 字节数；检查 gzip 完整性、单镜像及单 tag、镜像 ID 到配置摘要的关系和所引用层的存在，生成 SHA-256 与 JSON 回执。兼容本机 containerd 的 image ID 指向 OCI index 的形式；失败不发布残缺归档，不覆盖已有产物。
5. **并行复核与实际修复。** `tool_manifest_design` 负责构建与工具验证；`standalone_ctf_partner` 核对上传合同、Java 去重及交付代码；`resource_guard_design` 复核导出脚本、精简项并补齐本地功能测试。主会话完成最终构建、导出、加载与复测。修复回执发布失败后的归档回滚、Sage 首次缓存权限、nmap 文件 capabilities、重复 strip 损坏便携 pwndbg、Sage Cython 误用系统 pkg-config，以及验证日志跨数据块解析的问题。

## 2. 制品与实际工具

本轮交付执行工具基底。实际制品数据如下。

| 项目 | 实测结果 |
| --- | --- |
| 镜像 tag | `bbx-ctf-tools:20261120-r4` |
| 系统与架构 | `linux/amd64` |
| 镜像 ID | `sha256:69acd57cf8b5e1649727d09bfecd6dd869f8dd4c07de67f8a7280bb7d2ca6015` |
| Docker inspect Size | 2,805,608,890 字节 |
| docker save tar | 2,805,625,856 字节 |
| 最终 tar.gz | **2,787,377,598 字节** |
| 距离上传上限 | 212,622,402 字节 |
| 归档 SHA-256 | `418b75ecf46f2cc1199cb6e20ab1a9ef8d45a0267ee1f2a934eb3937a881b904` |
| 工具 manifest SHA-256 | `253771155d8cf28dd11676e21f26b2114d5a3fa4b415d7648488af43ba62b47b` |

归档位于仓库内 `.data/ctf-standalone-image-20261120/bbx-ctf-tools-20261120-r4.tar.gz`，同名 `.json` 保存导出回执。该回执保留 `docker_load_verified: false`，表示导出脚本自身的验证范围；独立执行 `docker image load` 后，`load-verification.json` 记录 `docker_load_verified: true` 与一致的镜像 ID，`validation-final/validation.json` 再关联加载后实际运行结果。

默认工具 Python 为 3.14.8，Sage Python 为 3.12.16，Sage 为 10.9，Node 为 22.21.1。镜像内 `/opt/ctf-tools/worker-tools.md` 已生成分类短清单，后续 Worker 系统提示词可直接使用。

首版候选 `docker save` tar 为 `4,192,182,272` 字节，gzip -9 流式实测为 `4,140,961,590` 字节。最终归档减少 **1,353,583,992 字节，约 32.7%**。只移除工具调试信息时仍超限，后续加入重复组件、无界面依赖与工具取舍。

用户随后明确允许删除解题价值低或重复的工具，并要求先估算再构建。本轮据此采用包文件清单、依赖差集与组件级压缩计数确定组合。r3 的压缩层合计为 `3,094,284,097` 字节，只作为估算锚点，不当成实际上传大小。最终精简组合为：

| 组件 | 组件压缩贡献/预算 | 能力变化 |
| --- | ---: | --- |
| QEMU user + gdb-multiarch | 实测分组 gzip6 共约 113 MB | 保留原生 Linux 调试及 Ghidra 静态多架构分析，不提供异构程序本地模拟和额外 multiarch GDB |
| APKTool/dex2jar/Android 资源包与专用依赖 | 估计 40–75 MB | 保留 Java/JADX 的 Java、DEX、APK 代码分析，取消 APK 资源重打包链 |
| Sage Pandoc、Notebook/浏览器资源、图文帮助 | 实测分组 gzip6 共约 125 MB | 保留数学、IPython/Cython 与终端帮助，不含网页交互界面和 Pandoc 文档转换 |

分组计数受层顺序、依赖共享和 gzip 级别影响。构建前预算约减少 0.27–0.31 GB，预测最终约 2.8 GB；最终压缩层合计 2.806 GB、上传包 2.787 GB，与预算接近。证据保存于 `component-estimates.jsonl` 和 `slim-review/`。后续沿用先按包清单、共享依赖和组件级压缩贡献做预算，再对选定组合统一构建与验收的顺序。

本轮自行选择删除上述工具组，相关能力变化已在表内列明。保留 Sage、Ghidra、pwndbg、原生 Pwn 调试、Java/JADX、Hashcat/POCL、数学静态库与编译器。PDF 工具压缩贡献较小，继续保留。图片处理通过 headless OpenCV 提供。

## 3. 如何验证与实际结果

最终 `make check` 通过，包含 Ruff、Pyright、Pi 测试及 **2331 项 Python 测试**，475 项 integration/live 用例按规则排除，pytest 耗时 209.83 秒。日志为 `.data/ctf-standalone-image-20261120/make-check-final.log`。验证使用脚本化假模型与本地合成夹具。

镜像源脚本的 **11 项离线测试**通过。导出 8 项覆盖传统与 OCI 配置、containerd index、错误或多 tag、配置不一致、缺失层、gzip 损坏、严格大小边界、已有产物保护及回执竞争失败的清理。下载 3 项覆盖 SHA-256、Git blob 摘要及损坏下载拒绝。另通过 Ruff、Linux 目标 Pyright、两个 shell 包装入口的 `sh -n` 与 `git diff --check`，记录在 `image-source-checks-final.log`。

最终 r4 构建阶段 UID 10000 断网 **65 项检查全部通过**，该构建步骤耗时 44.3 秒。上传归档重新加载后，在 testcontainers 临时容器中以默认 UID 10000、4 CPU、6 GiB、512 PID、断网配置再次执行，**65 项全部通过，退出码为 0**，各探测耗时合计 45.747 秒，临时容器已清理。记录见 `validation-final.log` 和 `validation-final/`。

检查包括 Sage 首次初始化、LLL/有限域/代数系统/Cython/终端帮助、pwndbg/GEF/GDB、Ghidra/JADX/angr、普通用户 nmap TCP 扫描、sqlmap 本地 SQLite 检测、离线 PCAP、PNG/QR、音频与 DTMF、ZipCrypto 解密、PDF、John 与 Hashcat CPU 设备枚举、Node SQLite。清单内命令均存在，镜像内 manifest 摘要一致。

```sh
make check
.venv/bin/python -m unittest discover -s services/ctf-standalone/image -p 'test_*.py' -v
.venv/bin/ruff check services/ctf-standalone/image
.venv/bin/ruff format --check services/ctf-standalone/image
.venv/bin/pyright --pythonplatform Linux services/ctf-standalone/image
git diff --check
```

实际构建和验收命令如下。导出目标已有文件时，复测需使用新的文件名。

```sh
docker buildx build --progress=plain --platform linux/amd64 --provenance=false \
  --output type=image,name=bbx-ctf-tools:20261120-r4,compression=gzip,compression-level=9,force-compression=true \
  services/ctf-standalone/image
.venv/bin/python services/ctf-standalone/image/export_image.py \
  bbx-ctf-tools:20261120-r4 \
  .data/ctf-standalone-image-20261120/bbx-ctf-tools-20261120-r4.tar.gz
docker image load --input \
  .data/ctf-standalone-image-20261120/bbx-ctf-tools-20261120-r4.tar.gz
docker image inspect bbx-ctf-tools:20261120-r4 --format '{{.Id}}'
.venv/bin/python services/ctf-standalone/image/validate_image.py \
  bbx-ctf-tools:20261120-r4 \
  .data/ctf-standalone-image-20261120/validation-final
```

构建依赖 README 中列出的本地基础镜像。当前 Docker 存储后端导出的 tar 已包含 gzip 层，额外 gzip 封装减少约 18 MB；上传文件、镜像层合计、展开文件系统应分别记录。

## 4. 偏差与待决

- 本轮仅准备工具基底。独立 Node/Pi Lead/Worker 程序、20 人实际求解负载、模型调用和正式比赛均未执行，不能从工具烟测推断解题速度或真实 Worker 容量。
- 官方精确字节常量、CPU 架构列表、解压后限制和独立 OCI archive 支持尚未确认；本次不据此放宽 3 GB 限制。实际交付使用带 Docker `manifest.json` 的 `docker save` 归档。
- 工具组件的取舍按用户要求在本轮自行完成。异构程序模拟、APK 资源重打包和 Sage 浏览器交互功能已移除，具体清单记录在镜像 `slimming.json` 与 manifest 中。后续增加 Agent/SDK 层时仍需测量最终归档，当前余量约 213 MB。
- Kali rolling 与额外工具的间接依赖仍由构建时包源解析。已固定主要额外工具版本和下载摘要，全部实际安装版本记录在 manifest，长期复用以交付归档 SHA-256 和镜像 ID 为准。
- `docs/tasks/CTF-standalone-20261120-report.md` 记录的是前一次设计阶段；其中初始 10 人与复杂资源保护由本次已提交的 20 人、Lead 自主增员方案覆盖。

## 5. 提交与下一步

设计修订已独立提交：`docs: simplify Lead scaling and specify compact CTF image`（`567e5ba`）。

镜像实现提交包含 `services/ctf-standalone/image/` 和本报告，提交信息为 `build: add compact standalone CTF execution image`，正文末尾为 `Implemented by Codex (gpt-6-sol) for task CTF-standalone-image-20261120.`。上传归档与验证证据保存在本地 `.data`。实现提交的共享文件变更为零。

下一阶段接入独立比赛核心与 Pi 会话，再验证最终 Agent 层加入后的上传体积及 4 核、6 GiB 下的实际组合负载。
