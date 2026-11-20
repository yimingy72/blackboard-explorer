# 独立 CTF 执行镜像与 Lead 扩员简化

任务：`CTF-standalone-image-20261120`。用户明确要求初始 **20 个 Worker**，容量由 Lead 查看实际占用后自行决定，不使用容量排队/准入框架；由子代理实际准备执行工具镜像，上传制品控制在 3G 以内。

## 1. 范围

1. 更新三份设计正本、独立版主方案和资源说明，移除容量预约、模型/工具容量队列、自动阈值拒绝、强制分批、自动缩容与复杂 watchdog。资源信息用于 Lead 判断；创建幂等、真实回执、业务认领和正常退出清理保留。
2. 在 `services/ctf-standalone/image/` 增加执行镜像 Dockerfile、必要构建/导出脚本、工具 manifest 与离线验证、使用说明。
3. 使用已有工具审计确定补齐项，实际构建、测量上传制品、验证 archive/tag 和关键工具。保留 Sage、Ghidra、pwndbg 等已确认核心能力，不为体积目标静默删除。
4. 本轮交付执行工具基底；独立 Lead/Worker 求解程序仍由后续代码阶段实现。工具基底不冒充可直接完成比赛的 Harness。

不改运行中的平台或其他工作树，不调用真实模型、不发起正式比赛、不实际上传或发布镜像。研究、日志和导出制品放 `.data/ctf-standalone-image-20261120/`，不提交二进制。

## 2. 上传大小

按严格 **3,000,000,000 字节**验收实际上传文件，预留后续 Agent/SDK 层空间。已通过官方控制台“新建镜像 → 手动上传”空表单确认支持 `.tar / .tar.gz / .tgz`，文案为“单包上限 3 GB”；该入口限制上传归档文件，而不是 Docker inspect 或展开根文件系统大小。前端精确字节常量未确认前沿用上述更保守的十进制上限，采用单 tag 的 `docker save` gzip 归档。

同时记录：Docker inspect/存储口径、普通 `docker save` tar 大小、受支持压缩制品大小。三者不能混用。验证 archive 可读取、包含预期镜像 tag，并记录 SHA-256；有条件用可逆导入/容器启动验证制品与原候选一致，不覆盖正在使用的镜像标签。

通过干净构建层、删除构建缓存与旧平台服务、合理依赖和压缩减少制品；不把 gzip 文件改名成 tar 来伪装符合格式。若保留核心工具无法达到限制，先给出实测尺寸和具体取舍，不自行削掉能力。

## 3. 工具与验证

依据[工具计划](CTF-standalone-tooling-20261120.md)补齐通用网络/Web、PCAP/图片库、音频二维码、字典等实际缺口。精确版本、最终解释器路径和镜像工具差异记录到构建产物。manifest 以最终镜像普通 Worker 身份的实测为准，分类短清单用于后续提示词。

构建使用任务前缀的镜像；需要新增基础镜像时先验证可以 pull。测试用临时容器、随机容器内端口或无网络，不使用宿主固定端口，结束后清理。验证包括普通用户权限、Python 模块、Pwn/逆向/数学、音频二维码/PCAP、本地 Web 夹具和 manifest 一致性；不执行外部目标测试。

## 4. 提交与完成

先单独提交设计修订：`docs: simplify Lead scaling and specify compact CTF image`；再提交镜像实现和任务报告。提交前 `make check` 通过，镜像另做实际构建与离线验收。

后续实现提交建议：`build: add compact standalone CTF execution image`，范围只包含 `services/ctf-standalone/image/` 和本任务中文报告；正文末尾为 `Implemented by Codex (gpt-6-sol) for task CTF-standalone-image-20261120.`。

不合并 main、不推送；任务完成报告实际制品路径、tag/digest、准确字节数、验证结果和仍未具备的能力。
