# CTF 工具链补齐与图像查看实施报告

日期：2026-10-17。任务说明见 [CTF-toolchain.md](./CTF-toolchain.md)。工作树：`/Users/yym/bbx-wt/local-runtime-repair`，分支 `feature/ctf-toolchain`（基线 main `b98de57`）。

## 1. 做了什么

### 1.1 执行镜像补齐工具层（`services/envd/Dockerfile.kali`）

在 Kali 执行镜像上新增四层，仍然复用原有 envd 控制面、`bbx-agents`/sudoers 与 `python3.13` venv 约定：

| 层 | 内容 |
|---|---|
| apt 工具层 | 压缩：`bzip2 7zip unrar cabextract ncompress fcrackzip zip/unzip`；运行时：`nodejs npm`；Web：`ffuf gobuster`；逆向：`ghidra radare2`（含 JDK）；口令：`hashcat john` + `pocl-opencl-icd clinfo`（CPU OpenCL，否则 hashcat 无设备拒绝运行）；取证：`libimage-exiftool-perl python3-pil sleuthkit foremost steghide pngcheck imagemagick testdisk oletools`；移动：`apktool jadx dex2jar`；网络：`openssh-client bind9-dnsutils net-tools iputils-ping traceroute whois telnet`；其它：`php-cli python3-pip checksec gef impacket-scripts ruby ruby-dev libgl1 libxrender1 libxi6 libxtst6` |
| Ruby gem 层 | `seccomp-tools:1.6.1`、`one_gadget:2.1.1`（TUNA rubygems 镜像，安装后移除 `ruby-dev`） |
| SageMath 层 | conda-forge 的 `sage=10.9` + `python=3.12`，安装到独立前缀 `/opt/sage`，只把 `sage` 命令软链到 `/usr/local/bin`；micromamba 2.9.0 固定版本 + SHA-256 校验；不触碰系统 `python3`（3.14，成员默认解释器）与 envd venv（3.13） |
| Python/工具层 | 系统解释器补 `angr==10.0.4.post1`、`fpylll==0.6.4`、`cysignals==1.13.1`（上游 PyPI，镜像站版本滞后）；`pwndbg 2026.09.15`（GitHub Release 的 `.deb`，固定 tag + SHA-256 校验，自带 gdb/python，不影响系统 gdb 与 gef） |

排除项：Playwright / Selenium / Chromium 等浏览器自动化一律未安装（`scripts/verify-exec-env-kali-inner.sh` 中有反向断言）。

构建入口变化（`Makefile`）：

- 新增 `CURL_PROXY`（给 micromamba / pwndbg 下载用）、`GITHUB_MIRROR`（GitHub 下载前缀，可置空直连）、`KALI_BUILD_ARGS`（BuildKit 不注入 `host.docker.internal`，显式 `--add-host`）。
- `.dockerignore` 排除 `services/envd/Dockerfile*`：此前编辑 Dockerfile 会改动 `COPY services/envd` 的上下文，导致整条工具链层重建；排除后编辑配方不再触发全量重跑。

### 1.2 可复现的功能化验收（`scripts/verify-exec-env-kali*.sh`）

`scripts/verify-exec-env-kali.sh [image]` 在一次性容器里跑 47 项检查，全部是真实调用而非“文件存在”：

- 环境契约：envd 包、mitmproxy venv、系统 `python3` 与 envd `python3.13`、`bbx-agents` 组与 sudoers、`runuser`。
- 归档：7z/zip 往返解压对比、unrar、cabextract、ncompress。
- 运行与 Web：`node -e`、`npm --version`、起真实 HTTP 服务用 ffuf 命中文件、gobuster。
- 逆向：编译 C 程序后用 radare2 分析 `main`、`pwndbg --version` 与 pwndbg 自带 gdb 内导入、gef 在系统 gdb 中加载、checksec。
- 密码学：sage 做 `factor` / `discrete_log` / `LLL` / `GF(2)` 多项式除法；hashcat 用 CPU OpenCL 爆破已知 MD5；john 用词表爆破 raw-md5；系统 python 导入 pwntools/z3/pycryptodome/gmpy2/sympy/elftools；angr 加载真实 ELF；fpylll 做 4×4 LLL。
- 取证：Pillow 生成图片、exiftool 读元数据、pngcheck、imagemagick 转换、steghide/foremost/sleuthkit/testdisk/oletools。
- 移动、网络、pwn 辅助：apktool/jadx/dex2jar、ssh/dig/ss/ping/traceroute/whois/impacket/php、seccomp-tools/one_gadget。

### 1.3 CTF 成员的“看图”能力（复用普通模式原生图片通道）

普通模式已有 `bbx_runtime/image_view.py`（工具返回引用、字节存对象存储、下一次模型调用作为 MAF 原生图片内容注入、会话只留引用）。本次把它按“共享核心 + 两个适配器”重构，再给 CTF 团队模式接上同一条通道：

- `image_view.py`：抽出 `vision_refusal()`（模型能力拒绝规则）、`pending_images()`、`stage_image()`（限长/格式/哈希/去重入库/入队）、`inject_pending_images()`、`run_with_image_injection()`；普通模式的 `make_view_image_tool`、`append_pending_images`、`ImageViewMiddleware` 改为调用这些共享实现，对外行为与导出不变（`test_image_view.py` 13 项全部保持通过）。
- 新增 `bbx_runtime/ctf/images.py`：`make_view_image_tool()`（成员版：路径限本成员工作区/共享目录，证据 URI 限本任务；10 MiB；PNG/JPEG/WebP；能力拒绝；checkpoint 未就绪拒绝）与 `ImageInjectionMiddleware`（在 CTF runner 的中间件链中注入并剥离图片字节）。
- 契约与配置：`CTF_COMMON_TOOLS` 增加 `view_image`（lead/teammate 都可用，仍由 profile 的 `worker_tools` 白名单决定是否启用）；`profiles/ctf/profile.yaml` 种子启用；`ctf/tools.py` 在持有执行容器与对象存储时装配该工具；`ctf/runner.py` 传入任务模型并挂载注入中间件；前端 `web/src/ctf/CtfSettings.tsx` 的工具清单同步。
- 新增测试 `services/agent-runtime/tests/test_ctf_view_image.py`（5 项）：入库去重、越界路径/外部 URI/坏字节拒绝、能力拒绝（supports_vision=false、bedrock、deepseek-v4-pro）、无执行容器拒绝、中间件注入-剥离-再水合。

### 1.4 提示词

`profiles/ctf/prompts/{lead,teammate}.md.j2` 各加两行：执行容器已预装工具链清单（先用现成工具，确有必要再安装），以及 `view_image` 的用途说明。内容是通用工具清单，不针对任何具体靶场。

## 2. 实际运行的检查

### 2.1 镜像构建与功能验收

```
make image-exec-env-kali CURL_PROXY=http://host.docker.internal:7897 GITHUB_MIRROR=
  → BUILD EXIT=0，镜像 sha256:0eb53fca5610606c3c6c37cade0b4ea8afe328bedc7ee5ad99b1ffe53228da26
  → docker tag bbx-exec-env-kali:latest bbx-exec-env-kali:20261017（回滚副本）
构建基线：kali-mini sha256:f0bb6f7085ab27b06c36189bc2d0cd6a4a9153c30baaa22dba6a4f4ad519c844
镜像分层合计约 12.5 GB（SageMath 前缀 5.7 GB、apt 工具层约 3 GB、pwndbg 645 MB、angr 583 MB、envd/traffic venv 758 MB）
```

```
scripts/verify-exec-env-kali.sh bbx-exec-env-kali:latest
  → ALL CTF TOOLCHAIN CHECKS PASSED（48/48）
```

要点（全部为真实调用，非“文件存在”）：7z/zip 往返解压一致；ffuf 对本地真实 HTTP 服务命中目标文件；gobuster 3.8.2；radare2 分析编译出的 C 程序；`pwndbg --version`（自带 gdb 17.2）与其 gdb 内导入 2026.09.15；gef 在系统 gdb 加载；checksec；`sage` 完成 `factor(2^64-1)=3*5*17*257*641*65537*6700417`、`discrete_log(Mod(6,7),Mod(3,7))=3`、`Matrix(ZZ,[[1,2],[3,4]]).LLL()`、`GF(2)` 多项式除法；hashcat 用 CPU OpenCL 爆破 MD5(`abc`) 成功、john 爆破 raw-md5(`password`) 成功；系统 python 导入 pwntools/z3/pycryptodome/gmpy2/sympy/elftools；angr 加载 `/bin/true`（arch AMD64）；fpylll 完成 4×4 LLL；Pillow/exiftool/pngcheck/imagemagick/steghide/foremost/sleuthkit/testdisk/olevba；apktool/jadx/dex2jar；ssh/dig/ss/ping/traceroute/whois/impacket/php；seccomp-tools 1.6.1、one_gadget 2.1.1；反向断言浏览器自动化不存在。

另外以非 root 用户（模拟成员身份）复跑：`runuser -u t1 -- sage -c ...`、`python3 -c "import pwn, angr"`、`hashcat --version` 均通过。

### 2.2 平台契约与回归

```
EXEC_ENV_IMAGE=bbx-exec-env-kali:latest uv run pytest services/envd/tests/test_integration.py -q -m integration
  → 2 passed

make check            → 2140 passed, 351 deselected（含 ruff format --check、ruff check、pyright、pytest）
make test-integration → 347 passed, 2144 deselected in 657.97s
```

### 2.3 图像查看通道

```
uv run pytest services/agent-runtime/tests/test_ctf_view_image.py services/agent-runtime/tests/test_image_view.py -q
  → 18 passed（CTF 新增 5 项 + 普通模式既有 13 项，普通行为未变）
```

### 2.4 未运行

- 真实模型任务：需要费用与授权，本轮未跑（“看图”能力的真实端到端验证需要支持视觉的模型；当前 CTF profile 用的 DeepSeek flash 不支持图片，工具会按能力门控直接拒绝）。
- 未访问任何靶机；未调用 live/e2e/eval 命令。

## 3. 需要用户执行的命令（可选清理）

1. 新执行镜像只对**新建任务**生效；已运行/已结束的任务保留原容器与旧镜像，属于预期行为。
2. 本机：旧执行镜像已变为无标签的悬空镜像，构建缓存约 247 GB（磁盘剩余约 130 GB）。确认新任务正常后可回收：
   ```bash
   docker image prune -f        # 只删悬空镜像
   docker builder prune -f      # 释放构建缓存（会失去后续重建的加速）
   ```
3. 本机的两个空卷 `bbx-ctf-team-mode_postgres_data` / `bbx-ctf-team-mode_minio_data` 是本次误建的空卷（无容器挂载、无数据），确认后可删：
   ```bash
   docker volume rm bbx-ctf-team-mode_postgres_data bbx-ctf-team-mode_minio_data
   ```
4. 服务器：旧执行镜像已被 `docker load` 重命名为悬空镜像，旧平台镜像 `bbx-blackboard:d1dffd3` / `bbx-agent-runtime:server-d1dffd3` 仍保留作为回滚点；确认后可 `docker image prune -f`。

## 4. 偏差与待决

- **镜像体积**：新镜像约 12.5 GB（旧 2.8 GB），主要是 SageMath（conda 前缀 5.7 GB，含 notebook/jupyter/rpy2 等）。若后续要压缩，可换 `sagelib` + 自建 `sage` 包装脚本，但需要重新验证 `sage.all` 的可用性；本轮优先保证“开箱即用”。
- **pwndbg 与 gef 并存**：pwndbg 用自带 gdb（`pwndbg` 命令），gef 用系统 gdb（`gef` 命令或 `source /usr/share/gdb/gef.py`），互不干扰；普通 `gdb` 仍是 Kali 原版。
- **Kali rolling 版本不固定**：apt 层工具随 Kali 滚动；上游固定版本的是 micromamba、SageMath、angr/fpylll、pwndbg（均有 SHA-256 或精确版本）。
- 未做：真实模型任务端到端跑一轮（需用户授权与费用）；服务器部署见第 3 节。

## 5. 部署记录（2026-10-17）

### 5.1 本地

- 以 main `408369e` 重建前端（`pnpm --dir web build`）、`bbx-blackboard`、`bbx-agent-runtime`；打标签 `bbx-ctf-blackboard:latest`、`bbx-ctf-toolchain-blackboard:20261017`、`bbx-ctf-toolchain-runtime:20261017`（旧镜像 ID：blackboard `cdf594fc…`、runtime `443bbaea…`，未删除，可回滚）。
- 重新部署 `bbx-ctf-team-mode` 栈：blackboard/agent-runtime 用新镜像，PostgreSQL/MinIO 卷不变；`http://127.0.0.1:58000/login` 返回 200，任务接口正常。
- 发布 CTF profile **v18**：`exec_image=bbx-exec-env-kali:latest`（新镜像）、`worker_tools` 两角色加入 `view_image`、`prompt_templates` 更新为仓库版本（含预装工具链与 view_image 说明）。`exec_resources` 仍为 8g/3.0cpu/1024pids。

**过程中发生并已修复的事故**：`scripts/deploy_ctf_team_mode.sh up -d`（不带 `--no-deps`）把 PostgreSQL/MinIO 重建到 Compose 默认推导的 `bbx-ctf-team-mode_postgres_data`/`_minio_data`（下划线）卷上，平台因此读到空库（任务与 profile 看起来“消失”）。真实数据一直在历史连字符卷 `bbx-ctf-team-mode-postgres_data`/`-minio_data` 中。修复：在 `docker-compose.ctf-team-mode.yml` 中显式 `name:` 固定这两个历史卷（提交 `f8d2afd`），重新 `up -d` 后容器挂回原卷；核验 19 个任务、`ctf` v17 及全部会话/存档均在，随后才发布 v18。误建的两个下划线空卷仍保留（无容器挂载），确认无用后可删。

### 5.2 服务器（101.201.116.63）

- 通过 SSH 流式传输镜像：`bbx-exec-env-kali`（含 `20261017` 标签）、`bbx-blackboard:408369e`、`bbx-agent-runtime:server-408369e-20261017`；源码发布到 `/opt/blackboard-explorer/releases/f8d2afd` 并切换 `current`。
- `operations/compose.server.yml` 的镜像标签由 `d1dffd3` 系列改为新标签（原文件备份为 `compose.server.yml.bak-20261017`）；`compose.py up -d --wait blackboard agent-runtime` 重建成功，PostgreSQL/MinIO 未重启（17 小时运行、卷不变）。
- 发布服务器 CTF profile **v16**（v15 → v16，同样内容）；`http://101.201.116.63:58000/login` 返回 200。
- 部署前确认没有运行中的任务（1 stopped / 6 failed / 10 finished），未删除任何数据卷。

### 5.3 Git

- `534800a feat(exec-env): add the CTF toolchain layer to the Kali image`
- `1e2ebf9 feat(ctf): let members send container images to the model`
- `408369e docs(ctf): record the CTF toolchain and image-view work`
- `f8d2afd fix(compose): pin the CTF stack to its historical volumes`
- 均已合并到 main 并推送到 GitHub。
