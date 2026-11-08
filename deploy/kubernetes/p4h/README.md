# P4h：受监督的后台 helper 验证

从仓库根目录运行，使用已安装 Python 与 Node 依赖的干净候选：

```sh
python3 deploy/kubernetes/p4h/infra.py
python3 deploy/kubernetes/p4h/build_images.py --candidate <clean-candidate>
PATH="$PWD/.data/p4h-native-background-helper-20261108/k8s/bin:$PATH" \
  <clean-candidate>/.venv/bin/python deploy/kubernetes/p4h/probe.py --candidate <clean-candidate>
```

探针使用本轮独立 kind、唯一 Gateway EndpointSlice、固定 Agentgateway 和官方 OpenSandbox。真实 Lead 建题，原 Worker 经脚本模型委派后台 helper。两次子模型各受原授权截止约束：先等父普通回合和子运行许可续期，再让子跨过首次许可的到期时点。验证原 Sandbox 非 root 命令、同 Task 预算、子最终 journal/退出/封存与一次原父 follow-up。合成 Task 创建时固定 15 分钟硬截止，产品子期限仍最多 180 秒。

记录位于 `.data/p4h-native-background-helper-20261108/k8s/joint/`。准备阶段有 660 秒等待；若验证受阻，保留原环境和安全时间线供诊断，不另建 Task。确证停止后才清理该私有环境；可用 `--stop-only` 核实同一原 Task 的停止，不作重放。探针不使用默认 Kubernetes context，不访问真实模型或靶场。
