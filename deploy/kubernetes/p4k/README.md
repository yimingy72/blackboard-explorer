# P4k：原生工作卷初始附件

`shared-inputs-v1` 是管理员显式发布的能力。旧 P4j 镜像与配置不会开放附件。新任务仅接收用户明确选择并同意全队共享的文件：单文件最多 16 MiB，合计 24 MiB，最多 20 件。文件原字节保留，不解压，工作副本仍位于既有 `/workspace/shared/inputs/<file-id>/<filename>`。

受信 Host 在首次模型前串行获取精确附件。官方 SDK 只写原 Sandbox 的固定 root 私有暂存区；固定安装器只向降权进程交付已核的只读文件描述符。降权进程核对长度和 SHA、原子发布完整树且不覆盖已有路径，实际检查工作卷至少有附件大小加 16 MiB 可用。Blackboard 核准与原 PVC、Sandbox 和清单绑定的回执后，才允许 Lead 凭据。恢复沿同一已核 PVC 使用原回执，保留工作副本后续编辑或删除。网络请求中断不证明安装器已退出；停止仍取原 Sandbox 的物理退出证据。

从仓库根目录使用审过的干净候选：

```sh
python3 deploy/kubernetes/p4k/infra.py
python3 deploy/kubernetes/p4k/build_fixture.py --candidate <clean-candidate>
PATH="$PWD/.data/p4k-native-shared-inputs-20261108/k8s/bin:$PATH" \
  <clean-candidate>/.venv/bin/python deploy/kubernetes/p4k/probe_public.py \
  --candidate <clean-candidate>
```

构建复用 P4j 已验的固定 Kali 工具层，只增加只读安装器层；不重新安装 52 项工具，不替换 P4j 两份交付镜像。Docker 局部检查真实降权、同回执后文件编辑保留、不覆盖、符号链接、硬链接、FIFO、同长度错误 SHA 和空间不足。完整探针只用显式测试能力、私有 Cilium 与脚本模型：普通公开上传 16 MiB 二进制和零字节文件，在首模型回调前核对原工作卷回执，再由一个 UID10000 命令检查两个实际工作文件。正常停止保留原两卷。失败环境保留供诊断，可用 `--stop-only` 核对原 Task，不重放创建。无真实模型、靶场、VPN 或真实来源。
