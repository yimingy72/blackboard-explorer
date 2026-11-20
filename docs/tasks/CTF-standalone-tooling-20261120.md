# 独立 CTF 比赛版：工具补齐与 Worker 提示词

任务：`CTF-standalone-20261120`。本文件是工具设计与镜像审计结果的对照，不表示已经安装缺失工具或完成比赛镜像。主方案见[独立比赛版方案](CTF-standalone-20261120.md)。

## 1. 核对依据与结论

核对现有 `services/envd/Dockerfile.kali`、Sandbox Dockerfile、当前 Worker 提示词，以及另一个子代理对实际镜像的断网审计。实际镜像为 `bbx-peer-sandbox:20261120`，镜像 ID 为 `sha256:8a103677e66db5fb6a45362ea509f62241167e9484c3bda0205c5f3f9e6deadb`，Linux/amd64，Docker 报告大小约 4.16 GiB。

审计使用 Worker UID/GID 10000，`PATH=/usr/local/bin:/usr/bin:/bin`，临时容器断网；既看路径和版本，也用本地合成文件验证关键操作。原 Dockerfile 写过某工具、旧报告验收过其他镜像，都不能替代这份实际镜像结果。探测条数混合了版本、模块和功能检查，不当作“已安装多少种工具”。

工具需求参考群内分享的 [Des-CTF-Knowledge](https://github.com/Dest1ny-Sec/Des-CTF-Knowledge)，读取范围是 README、[脚本依赖索引](https://github.com/Dest1ny-Sec/Des-CTF-Knowledge/blob/main/CTF常用脚本及工具/SCRIPTS-INDEX.md)、[工具使用目录](https://github.com/Dest1ny-Sec/Des-CTF-Knowledge/tree/main/工具使用)、图片与音频的工具索引。没有审核全部 1049 篇 WP，也不将题目特定答案打包进比赛镜像。

**当前工具层的主要缺口在 Web 基础工具、PCAP/Python 补充库、音视频/二维码、内存取证和通用字典；Pwn、逆向和密码学已有不少可复用能力。** 需要修正的另一个问题是解释器和调用路径，不能用重复安装掩盖路径错误。

## 2. 当前镜像已确认的能力

| 类别 | 实际结果与调用注意 |
| --- | --- |
| 基础环境 | 默认 `/usr/bin/python3` 为 3.14.8；Node 为 24.21.0，JDK 为 25；Python 3.13 及服务专属 venv 另存。独立 Pi 宿主应使用其实际验证过的 Node 版本，不因同名命令存在就混用 |
| Pwn/ELF | gcc 编译与运行、gdb 调试合成 ELF、pwndbg/GEF 加载、checksec、ROPgadget 可用；pwntools、Z3、PyCryptodome、gmpy2、pyelftools、capstone、unicorn 等默认 Python 模块通过相应检查；Ruby 的 one_gadget、seccomp-tools 可用 |
| 逆向 | radare2 分析与 Ghidra headless 分析合成 ELF 可用；Ghidra 入口为 `/usr/share/ghidra/support/analyzeHeadless`。项目使用正常可写题目目录，不使用包含 `.tmp` 的项目路径 |
| Android/Java | Java 编译/JAR 运行、JADX 反编译自建 Java 11 JAR已验证；JADX 的输入与输出使用绝对路径，不能被包装脚本切换 cwd 影响。APKTool/dex2jar 入口存在并通过基础检查；未据此宣称所有 APK 都能处理 |
| Crypto/数值 | `sage` 指向 `/opt/sage/bin/sage`，Sage 10.9 的 factor/LLL 已通过；`/opt/sage/bin/python` 为 3.12.16，NumPy/SciPy 可用。默认 Python 3.14 没有 NumPy/SciPy，提示词必须指出区别 |
| Web/网络 | curl、wget、nc、socat、SSH、dig、ffuf、gobuster 等可用；ffuf/gobuster 对容器内随机端口的合成 HTTP 夹具已验证，不涉及外部目标 |
| 文件/Misc | ExifTool、pngcheck、ImageMagick、binwalk、foremost、steghide、7z、unrar 等可用；已覆盖 PNG 读取/提取、隐写往返和压缩往返等操作 |
| 流量文件 | tshark 已验证离线 PCAP 解析。保留题目附件解析能力，不为此恢复平台抓包服务 |
| 哈希工具 | Hashcat 可见 CPU OpenCL 后端；John 在 `/usr/sbin/john`，默认 Worker PATH 不含 sbin，但绝对路径自测通过。Hashcat 展示的设备内存可能来自宿主，不能据其数字突破容器 6 GiB 预算 |

Sage、Ghidra、gobuster、JADX 的早期失败来自审计表达式、项目目录、版本选项或相对路径，后续已纠正并通过，不能列作损坏工具。实际 angr 导入和最小 symbolic step 通过，但独立 `claripy`/`keystone` 模块缺失，不能据此保证旧脚本兼容，也不把 angr 整体列为不可用。

`httpx` 可在服务 venv/Sage 路由中找到；独立版拟删除 envd 服务，所以不能暗中依赖 `/opt/envd/venv` 为解题提供模块。必要模块要放到明确保留的工具环境，并重新测试。

## 3. 首版补齐清单

以下是构建阶段待实施的清单，不是当前已安装声明。优先复用现有工具和实际可用的语言环境；只为明确的兼容需要新增隔离环境，不给每个脚本建一套 venv。

| 优先级 | 待补内容 | 原因与完成条件 |
| --- | --- | --- |
| 必补 | nmap、sqlmap | 当前缺失，覆盖常见服务识别和 Web 数据库测试；固定版本，普通 Worker 身份的离线帮助/解析检查及本地夹具验证通过 |
| 必补 | Scapy、OpenCV 的无 GUI 可用安装 | 脚本索引明确使用 PCAP/HID/图像及盲水印处理；验证指定解释器的 `import`、合成 PCAP 读取、图像读写/矩阵操作，不能仅验证包名 |
| 必补 | ffmpeg、sox、multimon-ng | 音频格式、声道/频谱和信号解码；用本地生成的短音频完成转换与解码烟测 |
| 必补 | zbar 工具、qrencode、zsteg、bkcrack | 补齐二维码、LSB 图片与压缩包分析；用生成的二维码、图像、ZIP 夹具验证真实读取，避免只验证 `--help` |
| 必补 | 可离线使用的通用字典 | 当前无 SecLists/rockyou 标准路径。首版至少提供常见 Web 路径与密码字典，记录实际文件路径、来源和内容哈希。按镜像体积选择明确子集，不能把子集标成完整 SecLists；需要原始文本的字典在构建时准备好 |
| 建议补 | dirsearch、BeautifulSoup/lxml、独立工具环境中的 httpx | dirsearch 与 ffuf/gobuster 有重叠，但与知识库调用习惯一致；解析库便于自行处理本地页面。先确定统一解释器路由及兼容版本，再按同一环境测试 |
| 建议补 | Volatility 3 与有明确来源的离线符号资料 | 知识库的笔记主要是 Volatility 2，不能照抄命令。先验证 v3 入口、插件可用性和缓存清单；符号缺失时标注能力条件，不声称可离线解析任意内存镜像 |
| 按需要补 | claripy、keystone-engine、galois、专用解包/隐写库 | 只有通用任务或兼容脚本确实使用时再补；验证与现有 angr/Python 版本兼容，不为追随旧 WP 盲降级已验证环境。Sage 已覆盖一部分数论/有限域需求 |
| 按需要补 | Chromium/Playwright、OCR、SSTV 等专用工具 | 仅在首版覆盖目标确有需要时预装并离线验证；浏览器按重型工具计入资源，不常驻一份给每个 Worker。OCR 是无 GUI 工作方式的补充建议，不是已核实的知识库硬依赖 |

基础 shell、编译器、语言解释器、curl/SSH/DNS 工具与已有 Pwn/逆向能力继续保留。没有必要为了“装全”引入 Adobe Audition、ScreenToGif、Windows GUI 套件、整套桌面或新远程工具平台。常见音视频分析使用 ffmpeg/sox/Python 输出文件；条码使用 zbar；Ghidra/JADX 使用已验证的无界面入口。

Volatility 的符号与字典应在构建时准备和列明，运行时不依赖外网下载。镜像内有工具并不等于其所有可选资源已齐备；这一差别必须进入 manifest 和详细用法说明。

## 4. 解释器与路径约定

首版优先保留两条已有且可验证的 Python 路由：默认 Python 的 Pwn/常规库，以及 Sage 专有的 Python/数值库。补入的新模块安装到明确选定的工具环境；若默认 Python 的兼容版本无法构建，再建立一套专用兼容工具环境并固定路径。不要改名伪装为同一解释器，也不要依赖未来会删除的 envd/traffic venv。

示例应使用最终 manifest 的真实值；当前镜像中已确认的两种调用为：

```sh
/usr/bin/python3 -c 'from pwn import p64; print(p64(1).hex())'
/opt/sage/bin/python -c 'import numpy, scipy; print(numpy.__version__, scipy.__version__)'
```

John 可以在独立版正式 Worker PATH 中纳入已验证的 sbin 位置，也可直接使用 `/usr/sbin/john`；无论选择哪种方式都必须让提示词和测试采用相同路径。Ghidra/JADX 项目及输入采用绝对路径，工作目录由该 Worker 的命令参数传入，不能修改 Node 宿主的全局 cwd。

## 5. Manifest 与验证流程

不新建工具注册服务。镜像内放一个 JSON manifest 与分类使用说明，启动时读取一次，所有 Worker 共享只读内容；每个会话按任务需要读取详细条目。

一个工具条目最少记录：稳定名称、类别、实际调用路径/解释器、版本、验证范围、状态、必要资源/环境说明、详细用法文件。Python 模块要记录所属解释器，字典/符号要记录实际文件，不能只写包名称。

建议状态只有三种：

- `available`：规定的离线检查通过，可按条目所写能力使用。
- `conditional`：入口可用，但某些格式、符号、字典或设备条件未满足，明确写出条件。
- `unavailable`：当前镜像缺失或规定检查失败，不放入“已验证可用”的提示词清单。

验证强度分别标为 `version`、`import`、`functional`，避免把版本输出等同于完整功能。资源类别只供执行队列预估；实际内存和 CPU 仍由运行监控负责，不能信任工具自报的宿主资源。

构建与交付顺序：

1. 按固定来源和版本安装需要的工具；从原镜像剥离服务层时保留其工具依赖，不照搬 VPN、抓包服务、sudo 配置和旧服务入口。
2. 在最终镜像内扫描已安装工具并跑本地夹具，生成 manifest；验证使用正式 Worker UID、PATH、cwd、CPU/内存配额，断网且不增加比赛未承诺的特权。
3. 必需工具检查失败则镜像验收不通过；可选能力如实标记。检查错误应保留原因，不能将未知或超时转换为“成功”。
4. 构建完成后，在发布记录中绑定镜像摘要与 manifest 哈希。最终 OCI 摘要不塞进自身镜像内容形成循环依赖。
5. 启动只验证 manifest 存在和版本匹配，不让每个 Worker 重复跑全量探测。运行中实际失败应更新该次执行反馈，并提醒 Lead 清单与现场的差异，不悄悄改写旧验收记录。

详细用法只包含通用能力、准确命令与环境条件，不带比赛具体题目的已知答案。说明中的通用例子也用本地合成夹具生成和验证。

## 6. Worker 系统提示词模板

以下是待接入模板。占位符必须从最终镜像 manifest 与任务配置生成，不能把审计中的旧路径当作新镜像永久约定。

```text
你在 Linux {{architecture}} 的共享比赛容器内工作。
本 Worker 的工作目录是 {{worker_cwd}}，本题资料与产物目录是 {{challenge_root}}。

已验证可用的工具：
{{available_tools_by_category}}

Python/数值环境：{{python_routes_and_key_modules}}
完整工具清单：{{manifest_path}}；分类使用说明：{{tool_help_root}}。
需要不常用工具时先按名称或类别查看清单；调用时使用条目指定的解释器和路径。
清单中的 conditional 条目有前置条件，缺少符号/字典/设备时不要当作已具备该能力。
正式运行依赖已预装环境；不要因遗漏工具清单而反复探测或尝试联网安装。

按当前题目、工作代次和黑板状态执行。大输出保存到本题文件，回传必要摘要与路径。
耗时或高内存命令交给统一执行队列，保存结果后再推进下一步；等待同伴时结束本轮等通知。
```

`available_tools_by_category` 生成 6–8 行分类摘要，覆盖 Web/网络、Pwn、逆向、Crypto、文件/隐写、音视频/取证等；常驻内容列工具名和关键别名，不包含全部选项手册。完整清单可本地按需读取。所有 Worker 复用稳定的公共前缀，只追加其目录、工作和所需工具说明，避免每次事实更新重发整个清单。

Lead 使用相同工具摘要，并增加资源快照、团队人数和增员接口说明。工具可用性与成员管理权限分开，不因工具多就让 Worker 获得创建其他 Worker 的权限。

## 7. 实施验收与当前边界

后续至少覆盖：正确解释器能 import 对应库、同名工具不被 PATH 遮蔽、非 root 读取/写入、Ghidra/JADX 绝对路径、音视频/二维码往返、离线 PCAP、字典和符号声明、重型工具排队、manifest 变更时提示词同步，以及工具缺失时能得到真实错误。

当前依据为 `.data/ctf-standalone-design-20261120/tool-audit/` 内的 `inventory.json`、`probes.json`、`followup.json`、`final-targets.json`；统一摘要与 manifest 由审计代理生成。临时容器已由审计代理清理。当前文档没有新增工具、调用真实模型或发起比赛，也不承诺未知题型的工具已经全部覆盖。
