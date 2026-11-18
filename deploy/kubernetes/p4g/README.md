# P4g：原生 Worker 前台 helper 验证

在仓库根目录使用已准备 Python/Node 依赖的干净候选运行。只使用本批独立 kind、Cilium、官方 OpenSandbox、固定 agentgateway 与本地脚本模型；不使用默认 Kubernetes context，不访问真实模型或靶场。

```sh
BBX_P4G_FINAL_ATTEMPT=1 python3 deploy/kubernetes/p4g/infra.py
BBX_P4G_FINAL_ATTEMPT=1 python3 deploy/kubernetes/p4g/build_images.py --candidate <clean-candidate>
BBX_P4G_FINAL_ATTEMPT=1 PATH="$PWD/.data/p4g-native-helper-20261108/k8s-final/bin:$PATH" \
  <clean-candidate>/.venv/bin/python deploy/kubernetes/p4g/probe.py \
    --candidate <clean-candidate> --scoped-attempt
```

探针让 Lead 发出一个真实可派题目，Worker 由模型选用受控委派工具，前台插件创建独立 helper SDK。helper 在原 Task Sandbox 以非 root 身份运行一个脚本化命令，费用用同原 Task 的稳定 key；父收到已保存结果后继续，席位只在子最终日志、SDK 退出和命令作用域均确认后释放。探针也在 helper 活跃时核验业务后端拒绝该子身份。最后沿原路径确认停止，保留原两卷。

上述参数使用独立的 `.data/p4g-native-helper-20261108/k8s-final/joint-final/`，不需要先运行早期失败的版本。启动 Task 前要求 Gateway Service 只选中原定 Pod，拒绝任何外来端点。该合成 Task 的硬截止自创建固定为 15 分钟；前台 helper 的 60 秒限制不变。其 `evidence.json` 保存脱敏结果；已有同次 Task 时，可加 `--stop-only` 按原身份确认停止，不重建 Task。

本地 Node 检查：

```sh
cd services/agent-runtime/task-runner
npm ci --ignore-scripts --no-audit --no-fund
npm test
```

构建固定 Node 22.21.1、Pi 1.5.0、pi-subagents 0.85.0 的精确源码和两层 SHA-256 校验补丁；生产镜像不依赖 P1 的忽略目录。新前台模式需要可信 Task 启动明确启用；单次本地委派受最多 60 秒及原 Task 截止限制，超时保留未决并继续收尾。后台 helper、通用插件入口、递归、用户扩展与本机工具均未启用。
