# Pi 本地部署与历史镜像清理报告

日期：2026-11-02。用户要求更新本地环境并删除之前的历史镜像。发布源码基线为主线 `db38e7d`，生产源码与已验证的 `b19ea6a` 一致；后续 `56fa9e0` 仅记录主线交接进度。

## 完成内容

本地应用和 runtime 已切换为 **`pi-20261102`**，入口仍为 [本地平台](http://127.0.0.1:58000/)。应用使用本轮新构建的前端与后端；runtime 使用经完整源码核对的 Pi 候选镜像，新增正式发布标签。当前实际运行的是 Pi，Node 为 **22.23.4**，`pi-agent-core`、`pi-ai`、`pi-telemetry` 均为 **1.4.0**，没有安装 MAF。

| 用途 | 保留的当前引用 | 镜像 ID 前缀 |
| --- | --- | --- |
| 应用 | `bbx-blackboard:pi-20261102`、`bbx-blackboard:latest`、`bbx-ctf-blackboard:latest` | `8b43cfb33fd7` |
| runtime | `bbx-agent-runtime:pi-20261102`、`bbx-agent-runtime:latest` | `b3216aea81f0` |
| Ubuntu 执行环境 | `bbx-exec-env:pi-20261102`、`bbx-exec-env:latest` | `b8dd1d02f153` |
| Kali 执行环境 | `bbx-exec-env-kali:pi-20261102`、`bbx-exec-env-kali:latest` | `c79ded7b50e9` |
| 可选出网代理 | `bbx-egress-proxy:latest` | `e627a6481847` |

Ubuntu/Kali 实际加载的执行器代码已与主线一致，因此保留现有镜像 ID 和工具链，仅增加同版别名。

## 数据与配置保留

发布前确认没有活动任务、普通会话消息、成果整理作业、CTF turn 或复盘请求。短写屏障内完成数据库备份及摘要记录后停止旧应用与 runtime，再依次启动新应用和新 runtime。没有创建或续跑业务任务。

- 数据库迁移版本保持 **0020**。
- **22 张业务表**的完整行摘要，在发布前、新应用启动后、新 runtime 启动后及镜像清理后均一致。
- 保留 **17 个任务、94 份会话**，包括原预算、配置版本、失败历史与存档引用。
- MinIO 保留 **36017 个对象、565460714 字节**；对象名称、大小和 ETag 的整体摘要一致。
- Postgres、MinIO 的容器 ID 与卷挂载保持，应用与 runtime 的环境变量、端口、挂载和网络保持。
- 配置值仅从运行容器读取并在进程内传递，没有读取 `.env`。保存的 Compose 覆盖文件仅含环境变量占位符，没有写入明文凭据。

数据库备份、前后摘要、Pi 健康结果、镜像清理清单与日志位于主工作树忽略目录 `.data/pi-local-release-20261102/`；数据库备份所在私有子目录权限为0700。

## 历史镜像清理

确认新版本就绪且数据一致后，删除了 **25 个历史镜像 ID、51 个旧标签**，包含旧应用、旧 runtime、旧执行环境、旧 Pi 候选及回退标签。当前5个镜像 ID及其必要引用保留；已删除的旧别名没有改变当前容器使用的镜像 ID。

清理仅匹配本平台 `bbx-` 开头且不含仓库路径分隔符的历史引用。其他项目镜像、`bbxcyb/` 靶机、独立 Cybench 环境、数据库/MinIO 镜像与数据卷均保留。没有执行全局 prune，共享构建缓存保留。删除前逐组复核容器引用与镜像 ID，删除后当前容器配置、启动时间和镜像引用保持一致。

## 实际验证

- 前端重新执行 `pnpm --dir web build` 成功；新应用镜像构建成功。首次HTTP镜像源下载失败，切换为HTTPS和仅用于构建的代理后成功，运行配置未改变。
- 应用、迁移及前端资产 **181 个文件**，runtime/合同/对象存储与 Pi 组件 **106 个文件**，以及 Ubuntu/Kali 实际加载的执行器各 **10 个文件**，均逐一核对SHA256。
- 运行容器内的 Pi 启动与健康握手通过，返回上述锁定版本，`agent_framework` 不存在；runtime 已取得数据库运行锁。
- 登录页、鉴权后的任务列表、CTF角色配置接口均为 **HTTP200**；读回任务数为17，实际静态首页与本轮构建字节一致。
- 应用与 runtime 均为运行状态、重启次数0；数据和配置最终核验全部通过。
- 本轮没有更改生产源码，沿用刚完成的主线 **2605项普通测试、5项Pi原生检查及静态检查**，以及同版155项前端、8项定向集成结果，没有将其记为本次重新运行的检查。

可执行的只读复核：

```sh
curl -fsS http://127.0.0.1:58000/login -o /dev/null
docker ps --filter name=bbx-ctf-team-mode --format '{{.Names}}\t{{.Image}}\t{{.Status}}'
docker exec bbx-ctf-team-mode-agent-runtime-1 node --version
docker exec bbx-ctf-team-mode-agent-runtime-1 python -c 'import asyncio; from bbx_runtime.pi.bridge import ensure_pi_runtime; asyncio.run(ensure_pi_runtime())'
docker image ls --filter 'reference=bbx-*'
```

## 偏差、待决与下一步

- 没有新增生产逻辑、依赖声明、配置版本或数据库迁移，没有更改公网环境或推送Git。
- 历史及回退镜像已按用户要求删除；如需回退旧版本，须从已提交源码重新构建。数据库备份及原数据卷继续保留。
- 真实模型、云账户鉴权和实际解题表现没有实测。下一步可由用户选择任务，在新版本上显式创建或续跑后验证；本轮没有自动运行旧失败任务。
- 主线进度已单独记录于 `56fa9e0`。本报告提交划分为 `docs: record verified local Pi deployment and cleanup`，仅提交本报告。
