# P4f：同一 Task 的原生跨代次恢复

本探针使用独立的 kind 1.34、Cilium、官方 OpenSandbox、固定 Agentgateway 和本地假模型；不使用默认 Kubernetes context、不调用真实模型或靶场。用不包含其他未提交改动的干净候选运行：

```sh
python3 deploy/kubernetes/p4f/infra.py
python3 deploy/kubernetes/p4f/build_images.py --candidate <clean-candidate>
PATH="$PWD/.data/p4f-native-recovery-20261108/k8s/bin:$PATH" \
  <clean-candidate>/.venv/bin/python deploy/kubernetes/p4f/probe.py --candidate <clean-candidate>
```

探针实际启动原 Lead，通过真实 Sandbox 命令写入工作卷并累计费用。第二个进程竞争原 journal 锁必须失败；随后停止原 Node Runner，等实际冻结后立即请求恢复。原 Runner、工具宿主与 Sandbox 的退出、两卷与本机 PV 身份、原 journal 确认边界均需通过；未确认副作用不会重放。新代次使用新的 Runner、Sandbox、binding 和 Pi 会话。新命令读取原工作卷里的内容，并证明原费用 key、阈值、累计和硬截止时间保留。

支持的部署明确开启 `journal_writer_lock: true`，采用原 Sandbox host profile、单个 Ready 节点及两卷绑定的 local-path PV。未开启能力的旧 Task 仍走原停止和回收路径，不能借用新恢复流程。原 writer 与 verifier 对同一文件 inode 取独占锁，原 journal 只读核验，复制件在新代次的 `recovery-evidence/`；新的 Pi 工作文件独立写入，原日志不作为执行指令导入。未覆盖通用 CSI、跨节点、控制面 HA 或任意崩溃后自动续跑。

运行只写 `.data/p4f-native-recovery-20261108/k8s/`，其中 `joint/evidence.json` 保存脱敏结果。已有原 Task 时，`--stop-only` 只按原状态尝试确认停止，不能另建 Task 或覆盖首份结果。若结果不明，保留原证据，不强删原 Task 资源。
