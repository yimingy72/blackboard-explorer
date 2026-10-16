# 服务器部署与完整迁移报告

日期：2026-10-16。工作树：`/Users/yym/bbx-wt/server-deployment`，分支：`feature/server-deployment`。

## 交付结果

平台已部署到 **http://101.201.116.63:58000/**，登录页为 `/login`。**账号、密码沿用原本机平台**，没有重置已有账号。公网实际读取成功，页面及 11 个静态资源共 12 个文件逐字节匹配已验证构建。

完整迁移结果：

| 内容 | 校验结果 |
| --- | --- |
| 任务 | **17**：10 finished、6 failed、1 stopped；迁移前无活动任务 |
| 持久 Agent 会话 | **99** |
| Worker/Profile 版本 | **29** |
| 平台配置版本 | **5**；其中 3 个当前模型均可正常解密凭据并在 API 读取 |
| 数据库业务表 | **21 张**；逐表行数及有序行内容 MD5 与源端一致，应用启动后再次确认全部一致 |
| MinIO 对象 | **28,239 个，306,228,867 字节**；每个对象在目标端重新读取、SHA-256 与源端清单逐项匹配 |
| 合成测试清理后 | 对象数量及总字节仍精确等于原始值；原有 17 任务/99 会话和 21 张表不变 |

本机数据库与 MinIO 原数据未删除；取得一致快照后，原本机 blackboard/runtime 已恢复运行。服务器已有 443 服务及其他项目未被停止或替换。本次恢复的是快照时点；之后本机新增数据不自动双向同步。

## 部署布局与版本

- Ubuntu 22.04.5，Linux 5.15，amd64，4 核、约 7.1 GiB 内存。完成后约 5.2 GiB 可用内存，磁盘剩余约 60 GiB；单任务并发上限为 1，原 Worker 的资源/提示词/模型设置保持。
- Compose 项目 `bbx-production`；执行网络 `bbx-production_exec`，默认直接出网。数据卷 `bbx-production_postgres_data`、`bbx-production_minio_data`。
- 常驻服务：blackboard、agent-runtime、Postgres、MinIO。仅 blackboard 发布 **58000**；数据库和对象存储不发布宿主端口。服务配置 `restart: unless-stopped`。
- 目录 `/opt/blackboard-explorer/current` 指向 `releases/454dbc1`。基础版本为 GitHub main `454dbc1`，附本任务最小 Linux AppArmor 兼容修复；正式主工作树和 GitHub 在本轮未变更。
- 私有运行参数 `/opt/blackboard-explorer/private/deployment.json` 为 root 所有、0600；父目录 0700。不是 `.env`，部署没有读取或修改任何 `.env`。
- 运行入口与覆盖配置位于 `/opt/blackboard-explorer/operations/compose.py`、`compose.server.yml`。入口从私有参数注入进程环境，并显式使用 `--env-file /dev/null`，不打印完整配置。

| 镜像 | 版本 |
| --- | --- |
| blackboard | `bbx-blackboard:454dbc1`，与本机已验证 V3 镜像根文件系统各层一致 |
| runtime | `bbx-agent-runtime:server-454dbc1-20261016`，ID `35c77daf8fbe…`；只在已验证基础镜像更新两份兼容源码 |
| Ubuntu 执行环境 | `bbx-exec-env:latest`，沿用已验证 `1735df94…` 文件系统 |
| Kali 执行环境 | `bbx-exec-env-kali:latest`，沿用已验证 `c83fc64a…` 文件系统 |
| 数据库 | `pgvector/pgvector:pg16` |
| 对象存储 | `pgsty/minio:RELEASE.2026-04-17T00-00-00Z` |

7 个原始镜像通过 SSH 流式传输，压缩约 1.43 GB，485 秒；两端逐镜像 RootFS layers 一致。Docker Desktop 与服务器 Docker 的 image ID 展示形式不同，未把显示 ID 不同误判为内容不一致。runtime 兼容层在服务器禁网构建，运行中两份模块 hash 与接受检查的源码相同。

## 一致迁移方法

1. 确认全部任务终态，短暂停止本平台 runtime 和 blackboard。Postgres、MinIO、其他本地项目继续运行。
2. 对全部 21 张表记录行数及内容摘要，使用官方 `pg_dump` custom 格式备份；MinIO 使用官方 SDK 逐对象导出，保留对象内容、Content-Type 和自定义 metadata，附逐对象 SHA-256 清单。
3. 两份备份经 SSH 发送到服务器 root 私有目录，验证传输 SHA-256 后发布。源端再次确认表摘要不变，恢复本机原服务。
4. 使用 `pg_restore --single-transaction --no-owner --no-privileges` 恢复到新建目标数据库；对象仅写入空的目标 bucket，不覆盖其他项目。
5. 目标端逐对象回读验证哈希，比较全部表摘要；随后启动应用并再次比较。基础设施数据库/MinIO密码使用服务器新生成值；为完整保留已加密平台凭据与原登录，原签名/加密材料及原登录配置仅通过 SSH 私有传输、从容器进程配置获取，没有读取 `.env`，没有输出秘密。

服务器保留一致备份：

```text
/opt/blackboard-explorer/private/migration-20261016-94f5022f/
  database.dump           70,190,880 bytes
  objects.tar.gz         207,646,049 bytes
```

恢复平台凭据还必须同时保留私有运行参数中的签名/加密材料，不能只复制数据库。备份、私有参数及业务原文均未加入 Git。MinIO 对象 Last-Modified 为迁移写入时间；对象原始内容、归档内文件信息与数据库业务时间保持。

## Linux 兼容修复

服务器启用 AppArmor，Docker 默认策略禁止挂载；原流量采集的 SYS_ADMIN 并不能独立解决 `ip netns add`。实际裸容器预检复现拒绝，内核审计明确给出 mount 拒绝；Kali 新版 iproute2 另使用 `/run/netns` 的 rprivate 标志。

- 新增可选 runtime 配置 `EXEC_APPARMOR_PROFILE`，服务器设置为 `bbx-exec-netns`。未设置时保持原 Docker 默认行为；配置名限制为 `bbx-` 前缀，不接受 `unconfined`。
- 新增 [定向策略](../../deploy/apparmor/bbx-exec-netns)，以 [Moby v27.5.1 默认策略](https://github.com/moby/moby/blob/v27.5.1/profiles/apparmor/template.go) 为基础，保留 proc/sys 与跨策略进程保护，仅许可必要的 namespace、sysfs 和 DNS 挂载。AppArmor 将 nsfs 来源表示为 `/` 的内核行为已通过实际审计处理；目标仍限制在 `/run/netns/*`。
- 文件安装到 `/etc/apparmor.d/bbx-exec-netns` 并加载，AppArmor 开机加载已启用。没有修改 `docker-default`、Docker daemon 配置或关闭主机防护；没有使用 privileged 或 host network。
- 配置仅用于任务执行容器，relay 原 no-new-privileges/cap-drop 保持。实际 sudo 命令验证：Agent 为 root 时仍不具有 SYS_ADMIN；不允许无关的 `/run`→`/tmp` 挂载。
- 这是实现架构未展开的 Linux 部署兼容细节，采用最小可选实现，不改变既有 Agent 文件共享、网络归属、调度和权限职责。说明见 [AppArmor 部署说明](../../deploy/apparmor/README.md)与 [Docker 官方配置方法](https://docs.docker.com/engine/security/apparmor/#load-and-unload-profiles)。

## 实际验证

| 检查 | 实际结果 |
| --- | --- |
| `UV_CACHE_DIR=/private/tmp/bbx-uv-cache make check` | **2131 passed，349 deselected，70.71 秒**；ruff/pyright 全绿 |
| 执行环境管理相关单测 | **24 passed**，包含可选策略和 relay 隔离、禁止无策略值的回归；属于整仓结果范围，不额外累加 |
| 公网页面与静态产物 | `/login` **HTTP 200**，12 个文件 hash 与已验证构建相同 |
| 登录与 API | 匿名任务接口 **401**；原账号登录 **200**，会话 cookie 继续访问可读回 17 任务 |
| 模型配置 | 3 个当前模型均可解密并在 API 读取，不暴露密钥；DeepSeek/百智云端点网络可达，未调用真实推理 |
| Wireshark 校验 | 实际原生引擎 **4.4.19**，合法表达式返回 valid=true |
| Ubuntu 普通黑板执行 | 2 Agent，sudo/移除SYS_ADMIN/独立命名空间/UDP/HTTP/HTTPS；324 条合成记录，3 个采集源，归档 complete |
| Kali CTF 执行 | Lead/member-1，CTF代次注册及排空、sudo/UDP/HTTP/HTTPS；341 条合成记录，3 个采集源，归档 complete |
| 两模式原生解析 | `udp.port == 32765` 各匹配 2 包，准确归属两个 Agent；真实协议树及字节非空；`http.response.code == 200 && tls` 得到解密 TLS 数据源 |
| 清理与数据复核 | 两模式执行容器已销毁；合成对象全部清理；剩余恰为原 28,239 对象/306,228,867 字节，21 张表未变 |

没有执行新的完整345项 Docker 集成或前端129项测试：前端与原基础镜像未变且已比对根文件系统/产物，新增兼容逻辑完成整仓普通检查，并在实际 Linux 主机做上述两模式集成。没有调用真实大模型，没有访问用户靶场，没有重跑历史任务。

检查日志、镜像清单、传输/数据库/对象证据位于本工作树 `.data/server-deployment/`；不入 Git。预检发现的问题均修复后复核，没有降低断言或把失败当作空数据。

## 运维与后续

```sh
ssh root@101.201.116.63
python3 /opt/blackboard-explorer/operations/compose.py ps
python3 /opt/blackboard-explorer/operations/compose.py up -d --wait blackboard agent-runtime
```

`compose.py config` 故意拒绝输出完整含密钥配置；静态验证使用 `config --quiet`。不要执行 `down -v`，不要清理上述正式数据卷。重建应用应使用该覆盖文件及固定执行网络，不能直接用仓库默认 Compose 覆盖。

- 当前为用户选择的独立 HTTP 端口，未配置域名证书；443 现有服务保留，未调整云安全组。
- 迁移覆盖平台数据、会话、配置与存档，不包含本机 Cybench 靶机或其他项目容器。查看旧结果正常；若续跑任务依赖本机靶机/私网服务，需要另外提供服务器可达的目标环境。
- 运行环境与真实模型费用未做付费验证；用户可在新地址自行创建任务或显式续跑。
- 兼容修复保留在 `feature/server-deployment`，本轮未提交、合并或推送新补丁；main/GitHub 仍为 `454dbc1`。服务器实际运行版本为该基线加本报告的兼容层。

建议提交：`fix(runtime): support scoped AppArmor network namespace setup`（设置/管理器/回归/策略/许可与说明）；`docs(ops): record server deployment and complete data migration`（任务及报告）。私有参数、数据库/对象备份、`.data` 和镜像包不得提交。

## 合并验证补充（2026-10-16）

用户随后明确授权两份修复共同提交、合并、推送及更新服务器，取代上文的未发布检查点。兼容代码与部署记录分别提交为 `6894cac`、`cebb39d`，与本地网络/失败清理修复 `f5fe10b` 合并无冲突。最终合并源码 `make check` 为 **2135 passed、351 deselected，59.22 秒**，对应 Docker 生命周期/CTF 集成为 **7 passed，73.93 秒**；未调用真实模型。服务器更新前重新确认无活动任务，并记录全部业务表摘要、存档总量及既有容器标识，发布后补录比对结果。

## 正式发布与服务器更新（2026-10-16）

- 运行代码合并 main 为 **`d1dffd3`** 并推送 GitHub，远端 HEAD 已核对。服务器按该提交归档源码、核对传输 SHA-256，再禁网构建 `bbx-agent-runtime:server-d1dffd3`（`2e51b194e91c…`）。运行中的 manager/Settings 与已验收源码 hash 一致。blackboard、前端、contracts、Profile 与锁文件无代码差异，`bbx-blackboard:d1dffd3` 复用原验证镜像（`f34091142dda…`），没有重新安装依赖或覆盖 Worker 配置。后续仅文档提交同步 release 源码，不重复重建相同运行代码。
- 更新前确认服务器无活动任务；只重建本平台两个应用服务，复用原数据库、MinIO 和执行网络。AppArmor 策略与仓库文件完全一致且 enforce；不改主机全局策略，不重启其他项目。原镜像、release 和迁移备份保留；另在 root 私有目录 `private/update-d1dffd3-1792120428715338747/` 保存更新前数据库 custom dump 及恢复所需的旧覆盖配置/release 指向。
- 首次发布在备份脚本处遇到 Python 文件接口兼容错误，自动恢复旧服务；修正为标准 `open()` 后重新发布成功。失败发生在切换版本/写业务数据之前，未采用跳过备份的方式继续。

| 发布后复核 | 实际结果 |
| --- | --- |
| 数据库 | 全部 **21 张业务表**的行数及完整有序行摘要与本次更新前一致：17 任务、99 会话、29 Profile、5 平台配置版本、1 应用设置 |
| 对象存储 | 更新前后均为 **28,227 对象、303,462,819 字节**；只清理本次两份精确 UUID 前缀的合成采集数据，没有回灌本机或早晨快照 |
| 原账号与模型 | 匿名访问任务返回401；原账号登录200，可读取17任务；3个模型均正常解密凭据，没有输出凭据 |
| 公网静态文件 | `/login` 200，页面及全部静态资源共12文件与既有已验证产物逐字节一致 |
| Ubuntu 普通模式 | 两Agent、sudo及能力边界、独立网络、HTTP/HTTPS/UDP通过；328条合成摘要、3采集源、归档complete，执行容器已销毁 |
| Kali CTF模式 | Lead/member-1、代次注册/排空、sudo及HTTP/HTTPS/UDP通过；359条合成摘要、3采集源、归档complete，执行容器已销毁 |
| 原生流量详情 | Wireshark **4.4.19**；两模式各2个UDP包准确归属各自Agent，协议树/原始字节有效，HTTPS返回解密TLS数据源 |
| 其他服务与清理 | Postgres/MinIO和其他项目的容器ID/镜像保持；无新增遗留测试容器，全部合成对象及临时结果清理后再次核对数据一致 |

本次开始前，早晨迁移后的临时输入组已由15组变为14组，对象数量亦已变化；上述保留校验使用本次更新前现状，未误用早晨的28,239对象作为当前基准。原17任务及其会话/Profile/平台配置与早晨摘要也相同。

入口仍为 **http://101.201.116.63:58000/**，沿用原本机账号。更新/验证日志与非秘密摘要在 `~/bbx-wt/local-runtime-repair/.data/combined-release/`，未进入 Git；没有读取 `.env`、调用真实模型、重跑旧任务或访问用户靶场。此次未重跑未变更的前端套件或全部Docker集成。
