# 执行容器 PID 配额故障报告

日期：2026-10-17。工作树：`/Users/yym/bbx-wt/exec-pids`，分支：`fix/exec-pids`。

## 故障结论

任务 `dbccd857-3bc1-4863-a67d-fd7f9d20d230`（“查找免费socks5 代理”）在 08:49:49 UTC 因连续三个 Agent 运行失败触发保护阈值。Agent-9 的工具输出明确为：

```text
runuser: cannot create child process: Resource temporarily unavailable
```

该任务有 5 个并发 Agent，同时运行命令、抓包和代理扫描。执行容器的 256 PID cgroup 配额耗尽后，Agent-9 的工具调用无法再创建子进程，随后两个替换 Agent 在启动准备阶段也失败。任务金额仅使用 ¥3.42/¥10，时间也远未到 60 分钟上限，因此不是预算或模型失败。

## 修复内容

1. runtime 新增 `EXEC_MIN_PIDS`，默认 1024；创建执行容器时使用 `max(profile.exec_resources.pids, EXEC_MIN_PIDS)`，保留 profile 可设置更高值的能力。
2. Compose 与 `.env.example` 增加该配置；服务器部署设置为 1024。
3. 说明文档补充多 Agent 并发命令、抓包和扫描场景下的 PID 需求。

## 验证

```sh
UV_CACHE_DIR=/private/tmp/bbx-uv-cache make check
```

相关回归覆盖 Settings 默认值/覆盖值，以及 manager 实际传入 Docker 的 `pids_limit`。任务续跑前将从同一黑板、会话和预算建立新执行容器；切换 runtime 镜像会等待当前其他任务结束，不终止正在运行的 Agent。

实际续跑结果：

- 任务通过平台续跑接口进入 `run_number=2`、`status=running`，`failure_streak=0`，原黑板、会话、附件和 ¥10/60 分钟预算保留。
- 为避免重启当时仍在运行的另一任务，本次续跑创建的新容器先通过 Docker 原子更新为 `PidsLimit=1024`；实际值已在容器状态中核对。
- 续跑后持续产生工具调用、Agent 进度和轨迹记录，未再出现 `Resource temporarily unavailable` 或执行工具响应串扰。

## 偏差与待决

- 已存在的旧执行容器不会就地修改 PID 上限；新容器才使用该下限。当前任务在续跑后的新容器生效。
- 没有新增依赖或数据库迁移。
