# P4a：主 Worker 原生派工

## 完成内容

新建且显式选择 automatic 的 challenge-board-v1 Task 冻结工具目录 revision 2，按设定预建 1 至 20 个主 Worker 空席。空席没有模型或工具凭据，也不启动 Pi 会话。已冻结的旧 Task 工具权限不变，原未提交自动派工草稿不进入此提交。

Lead 通过 `board_dispatch_challenge` 明确标记自足题可派，或记录等待材料。已知有外部目标、没有题面或缺少可信材料可达证明的题不会被当作自足题；普通 pending 不会自动开工。撤回只取消工作，不冒充验收。已有可信 Flag 验收产生的 completed 进入同一收尾路径。已派题目的范围修改和重开不在本片开放。

派工在 Task 锁内按 Lead 优先级、同级等待顺序选题，核原 owner、共享预算、环境和空席，再一起写原 Assignment、Worker 代次和业务回合、原邮箱输入及 Runner prepare。每次分配有独立身份。继续复用同一个原 Runner Pod、官方 Sandbox 和 Pi 宿主中的隔离会话；Worker 按冻结 teammate 角色单独核推理、上下文与输出限制，复用同一 Task 模型选择和原生预算 key。

新绑定触发 Task 事件唤醒，可信 Runtime 立即更新同一 Pod 的短凭据挂载。Node 在原 lease 和绑定范围内等待实际投影，才打开 SDK journal 与 Pi；旧 Task 或错误绑定的凭据立即拒绝。准备和输入使用原幂等身份；输入只在原 SDK journal 已确认后提交一次。事实记录同题已保存的原命令可作为证据，不造对象 URI。Worker 的 Fact、Intent、Flag 候选沿原业务事务写同一 Assignment，事实会向 Lead 写持久消息并唤醒原会话。

普通 Pi 回合结束不会释放席位。撤回或可信完成先撤旧工作准入；原 close 的 SDK/journal 确认、原 cgroup 封闭排空以及未决执行与工具证据均符合要求才补位。已开始但无法确认的执行保留 unknown；只费用待核且传输已确认结束时，不把费用当零或解除现有 hold。关闭中只保留原未过期凭据供回写证据。真实清理进展沿现 Task 事件唤醒，短周期检查补漏。全 Task 停止以原三个容器实际退出为依据结束全部业务回合，保留原未决账和卷。

## 验证

- 干净 HEAD 候选中的 `.venv/bin/pytest services/blackboard/tests/test_native_worker_dispatch_integration.py -q`：真实 PostgreSQL 最终合同 1 passed，8.36 秒。验证并发派工、优先级、材料等待、原 Worker 输入幂等、同 Assignment 命令证据、旧运行写入口拒绝、原未知不释放、确认后补位和全 Task 退出后零运行回合，以及新增事件的 `GET /events` 正常读回。Kubernetes/SDK 输入与退出在此合同测试中是显式合成观察，不作为真实 SDK 执行证据。
- Node 5 项、资源 11 项定向验证通过。Node 覆盖凭据实际投影等待、lease 续期、另一会话可运行、关闭打断等待；资源定向验证覆盖只保留原未过期同绑定凭据。
- 唯一真实 Task `f8598052-cf05-4ec8-a4bf-bd252dcf9c0c`：私有 kind 1.34 上，原 Lead 通过原生网关建题并明确 ready，两 Worker 使用不同 SDK、Assignment 和 cgroup，实际命令重叠 3951 ms。原命令的事实证据唤醒 Lead；撤回 A 后原会话关闭、原范围确证封闭排空，再在事件 v42 释放 `member-1`、v46 以新 Assignment 补上 C，B 原分配不动。全 Task 停止实际确认原 Node/工具服务两个 CID 和 Sandbox 退出，原两 PVC 保留。五项主链证据见 `.data/p4a-multi-agent-20261107/k8s/joint/evidence.json`，命令详见 `deploy/kubernetes/p4a/README.md`。启动期有一次 `wait_sandbox_host_ready` 未确认，随后同原对象就绪，没有另建 Task 或替换执行对象；原观察快照只用于识别身份，退出以最终停止证明为准。
- 最终干净候选 `make check`：exit 0，2653 passed / 470 deselected，191.88 秒；ruff、pyright、Pi 通过。最终只新增就绪合同与事件枚举相应的 JSON Schema/OpenAPI/TypeScript，`pnpm exec tsc --noEmit` 通过。首次门禁暴露四个新事件尚未纳 `CtfEvent`，已修正并保留原日志。真实 Task 加载 core-v5 的执行代码；core-v6 仅增加事件读取枚举与 PG 读回断言，未重复真实命令。

验证未对新 Task 重跑 P3g 完整删除矩阵，普通停止保卷是本片验证的回收边界。原对象退出核实后，只清理本轮自建的测试集群、数据库、网络、镜像标签和合成凭据；原两卷只随测试环境拆除，未调用产品 DELETE。默认 Kubernetes context 与原官方网关镜像缓存保持。精确清理回执见 `.data/p4a-multi-agent-20261107/k8s/cleanup-result.json`。

## 边界

首片只做同原 Runner epoch 的主 Worker。两个 Worker 使用 Task 共享的 `worker-a` 网络，本片区分的是 Assignment、SDK 会话与 cgroup，不提供逐 Agent 网络或 PCAP 归属。未做 helper 借位、跨 epoch 接管、阈值恢复、公开 V1 启动和全场 20 席位容量证明；题型偏好尚无可复用的可信分类。命令记录仍只读，没有新增审查或处置。模型响应使用本地脚本，没有调用真实模型或靶场。

本片按用户指定由 GPT-6.1 Sol high 开发并独立审核。正式设计正本不改，保留原 38 个 tracked 改动和 72 个 untracked 工作文件。

## 提交划分

`Connect native main Worker dispatch and confirmed seat reuse`：Lead 就绪工具、Task 锁内派工、原生 Worker 凭据/准备、真实命令证据、正向收尾及必要验证。
