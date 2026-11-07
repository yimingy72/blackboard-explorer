# P4a 原子派工与 Worker 生命周期验证

显式启用 `automatic` 的原生 V1 Task 使用原 Task Runner Pod 和 Sandbox host。Lead 发布已准备的自包含题目，Blackboard 在 Task 事务中分配空闲 Worker、冻结 Assignment 与 binding、写现有持久唤醒。Worker 不自行选题或领取其它席位。会话沿用 Pi，每个 binding 有独立 SDK 会话、journal 和模型/MCP 凭据。

- Runtime 看到 Worker binding 集合变化即发布原 UID/RV 的 Secret，发布前复查停止和当前集合。正在收尾的 binding 只保留 Secret 里同 Task、原 binding、未过期的已投影凭据，不续发；过期或缺项不复制，身份冲突拒绝。Node 取得原许可后只等本地两项原 binding 凭据投影，期间其它会话和轮询继续；错误身份、关闭、过期拒绝，不开 journal 或 SDK。
- 旧席位必须经过原 Node close、账本未决门和原 Sandbox cgroup 封闭为空，才交给下一 Assignment。SSE 只唤醒现 PG 对账，3 秒看门狗补漏；不靠模型轮询容量。
- 同 Task 的 Sandbox 网络、工作目录和 UID 10000 仍共享。不同 Assignment 可识别不同命令和 cgroup，不宣称每 Worker 文件、网络或 PID 权限隔离。保留命令记录，未加审计处置。helper、跨 owner 接管、恢复暂停与公平容量不在本片。

## 实际验证

```sh
python3 deploy/kubernetes/p4a/infra.py
python3 deploy/kubernetes/p4a/build_images.py --candidate <reviewed-clean-candidate>
PATH="$PWD/.data/p4a-multi-agent-20261107/k8s/bin:$PATH" \
  <candidate>/.venv/bin/python deploy/kubernetes/p4a/probe.py --candidate <candidate>
```

新写入都在本轮独立 `.data/p4a-multi-agent-20261107`。私有 kind 1.34、Cilium、官方 OpenSandbox 1.2.0、固定 G7 和原生 agentgateway 使用已核定版本。使用 HEAD 导出的 Ubuntu 验证层和合成模型，不是 Kali 完整工具集。旧阶段证据未改，默认 `docker-desktop` 未用于写操作。

唯一实际 Task `f8598052-cf05-4ec8-a4bf-bd252dcf9c0c`：真实 Lead 通过工具创建并发布三题，A/B 真实 Pi 会话分别进入原生 execd 命令，两段 shell 时间重叠约 **3.95 秒**；两 Worker 的真实命令证据生成 Fact 并唤醒 Lead 撤回 A。原 A 的 SDK 会话关闭，原 Assignment scope 经独立官方执行 `596e24e5b1c7482daabf4cc1f809cd31` 确认封闭为空。PG release 事件 42 在补位事件 46 之前；C 接到原 member-1 席位并使用新 Assignment、SDK 会话与 cgroup，原 B 保持原绑定。随后原 Node/Host 和 Sandbox 正向退出，普通 stop 保留原两 PVC。五项实证及原 UID 见 `k8s/joint/evidence.json`。早期 Host ready 读回曾未确定，原对象重查恢复，未重建或猜测原因。

实际主链运行 core-v5；最终 core-v6 增加公开事件类型的序列化，另用 PG GET 验证。实测证明持久清理与补位顺序，未测量 SSE 或看门狗的单独唤醒延迟。Node 相关 5 项测试通过，资源相关 11 项通过；保留 A 旧凭据、发布 B 新凭据、拒绝过期或错 binding 的定向补充通过。未重复旧费用/网络/G1 矩阵或调用真实模型和靶场。
