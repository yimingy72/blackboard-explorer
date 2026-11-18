# P3g 保留卷回收验证

普通 stop 仍保留 journal/workspace PVC。本片只在用户 DELETE Task 或明确回收策略已进入 Blackboard 持久意图，且原执行已退出、无未决调用、原生模型凭据/MCP 已退休时，允许 Runtime 驱动资源回收。Blackboard 是唯一状态权威；资源适配层不创建另一份任务账。

## 边界

- `retire` 先只读核 source、cluster、Namespace/PVC UID、全部 PV 的 claimRef 与 Delete 策略；Retain、unbound、未知或外来对象在任何删除前拒绝；旧记录没有创建时的存储策略快照，不能从当前 Pending/无 Pod 回填历史在途证明。
- 动态发现所有 served API version 的 namespaced 资源，按原生 UID 去重；无法完整列举时停止。仅原生不持久的 Bindings/LocalSubjectAccessReview 可跳过。默认 SA/rootCA 和原 UID 引用的 Events/Endpoint 元数据留 Namespace GC，证据不保存 Events.message。
- 原生 ValidatingAdmissionPolicy 按精确 Namespace 名称、原 UID、managed/retiring 标签拒 CREATE；通过 core/v1 Namespace 参数核 UID。固定 1.34 的 `namespaceObject.metadata.uid` 类型检查存在差异，不能把编译/执行错误当作有效拒绝。policy/binding 模板及实际 UID 先持久，server dryRun 需得到本 policy 的明确 Deny。
- 自动 Namespace 回收仅接受部署冻结、实际核对的单 apiserver PodUID/NodeUID 和唯一 API Endpoint。HA 或配置不明拒绝破坏操作。
- 删除都用原生 UID 前置条件；Namespace 仅在二次完整盘点、原资源删除与原 PV 消失后删原 UID。PV 删除交原 provisioner，不手删 PV/host 路径，不声称安全擦除。ACK 不明只读对账，原 UID 账保留。

## 本地运行

仅操作本任务私有 kind，私有 kubeconfig 与所有新证据位于 `.data/p3g-retained-20261107`。原 default context/docker-desktop/CNI 不变。固定 kubectl 1.34.0 的官方下载 SHA 沿 P3f 缓存核验；Cilium、官方 CRD 和 HEAD 导出的 Ubuntu 工具层复用现有构建流程。它不代表 Kali 完整工具集。

```sh
python3 deploy/kubernetes/p3g/infra.py
python3 deploy/kubernetes/p3g/build_images.py
python3 deploy/kubernetes/p3g/vap_probe.py
PATH="$PWD/.data/p3g-retained-20261107/bin:$PATH" .venv/bin/python deploy/kubernetes/p3g/probe.py --candidate <reviewed-clean-candidate>
```

组合仅真实 PG/BB DELETE/RuntimeLock 与原生保留卷回收。短 binder Pod 只让两个 PVC 绑定并留下无害标记，取得实际 UID/CID 退出后即按 UID 删除；没有派发 Pi、模型、官方 Sandbox 或靶场执行，不把它称第二次 P3f Agent 全链验证。失败保留唯一 PG 回收权威，不从 404 或空列表推断曾执行进程退出。

P3g 首次准备误复用了固定 P3f STATE，旧辅助 `build-source.json` 被覆盖且无法按原 SHA 恢复；原位置留明确 missing incident，覆盖字节保存在新目录。P3f safe-artifacts 逐项复核，除此辅助文件外均保持原 SHA，frozen/joint/cleanup 历史未变。后续所有入口通过本目录 common 的独立 STATE 复用准备逻辑。

## 本片实测分段

真实 Task `1821bed6-3e07-4053-b33d-89eb2bf85497` 的 binder 退出码 0；普通 stop 后原两个 PVC 保留。用户 DELETE 后，实际 `NativeRunnerController.run` 完成原 Namespace、PVC、PV、policy/binding 回收并通过 Blackboard 原 `retire_complete` 和 purge。

夹具初次 cleanup 用本地 dict 字节计算摘要，被 PG JSONB 回读顺序差异正确拒绝；同 Task/原 PG 保持权威，新真实 fence 从权威 observation 恢复。随后夹具两次完成轮询有误（deleting Task 的公共 GET 先隐藏；purge 后控制路由实际返回 404），均保留 `completed=false`，没有把失败整段改绿。实际适配层返回 `retired=true` 并由产品 controller 完成 purge 的证据保留在 `joint/evidence.json` 与 `manifest.json`。`--verify-retired` 对同原资源和 PG 只读验证，确认无原 Namespace/两 PV/fence/孤立资源与 Task/resource 数据行，输出 `joint/final-verification.json` 后精确清除该测试 PG。轮询修复未通过另建 Task 重跑；不宣称完整夹具从头成功。

独审发现并修复 CEL 小探针的同名冲突清理：现在先核私有 endpoint，只登记成功 CREATE 返回的 UID，失败创建绝不清同名对象，清理全部带 UID 前置。碰撞模拟定向测试通过，未重跑已绿 CEL 矩阵。
