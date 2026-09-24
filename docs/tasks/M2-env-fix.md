# 任务 M2-env-fix · 镜像构建网络修正

在工作树 `~/bbx-wt/m2env`（分支 `m2-env`）上进行。本文件位于主仓库 `~/blackboard-explorer/docs/tasks/`，派发时用绝对路径引用。

先阅读 `AGENTS.md`。

## 背景

M2-env 的实现已完成（见 `docs/tasks/M2-env-report.md`），但镜像构建失败：Docker 构建中 `apt-get` 直连 `archive.ubuntu.com` 极慢并以退出码 100 失败；`pip`、`apk` 同样依赖海外源。另外 Codex 沙箱不能运行 `docker build`（buildx 需要写 `~/.docker`），**镜像构建与依赖镜像的集成测试由用户在沙箱外运行**。

## 任务

1. `services/envd/Dockerfile` 增加构建参数 `APT_MIRROR`、`PIP_INDEX_URL`（默认空 = 官方源）：`APT_MIRROR` 非空时改写 `/etc/apt/sources.list.d/ubuntu.sources` 中的 `archive.ubuntu.com` 与 `security.ubuntu.com`；`PIP_INDEX_URL` 非空时 `pip install` 加 `--index-url`。
2. `services/egress-proxy/Dockerfile` 增加 `APK_MIRROR`（默认空）：非空时改写 `/etc/apk/repositories` 中的 `dl-cdn.alpinelinux.org`。
3. `Makefile`：新增变量 `APT_MIRROR ?= http://mirrors.aliyun.com/ubuntu`、`PIP_INDEX_URL ?= https://mirrors.aliyun.com/pypi/simple`、`APK_MIRROR ?= mirrors.aliyun.com`，`image-exec-env` 与 `image-egress-proxy` 通过 `--build-arg` 传入；注释说明设为空即回到官方源。
4. `services/envd/README.md` 增加一节"构建"，说明上述变量。
5. `services/envd/README.md` 写明 token 约定：agent-runtime 持有 `ENVD_TOKEN_SECRET`，为每个任务派生 token，创建容器时以环境变量 `ENVD_TOKEN` 注入；envd 只读取 `ENVD_TOKEN`。

## 验证

- 你能运行的：`make check` 全绿；`docker build` 以外的静态检查（Dockerfile 语法、`sh -n`）。
- 需要用户在沙箱外运行的（写入报告，作为用户的验证步骤）：

  ```sh
  make image-exec-env image-egress-proxy
  make test-integration
  ```

## 报告

在 `docs/tasks/M2-env-report.md` 末尾追加"## M2-env-fix 补充"：改了什么、用户需要执行的命令、建议的提交划分。
