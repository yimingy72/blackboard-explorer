# 最终报告：tests/test_orders.py::test_concurrent_orders 偶发失败根因裁定

## 1. 验收结论与证据链

### A1 指出导致失败的代码位置与机制 —— met
- 结论类型：**有原始资料 + 对照实验支持的确定结论**。
- 代码位置（逐行核对原始文件 `evidence/9918846a-fd8e-4596-b36e-bda9e33bee2d/agent-1/2abb4e510b38-orders.py`）：
  - `Inventory.reserve` 位于 **第 78-85 行**：第 81 行 `current = self.available(sku)`；第 82-83 行 `if current < quantity: raise ValueError("insufficient stock")`；第 84 行 `time.sleep(0.0011)`；第 85 行才 `self._set_quantity(sku, current - quantity)`。读到写之间既隔着判断又隔着约 1.1ms 的 sleep 窗口。
  - `Inventory` 内部**没有 Lock**；`OrderService.place` 的 `self._order_lock` 只在 `with self._order_lock: self._orders[order.id] = order` 处加锁，**不覆盖** `catalog.get` / `inventory.reserve`，故库存扣减完全未同步。
  - 上述行号与 F5/F1 所述一致（F1 为 self_reported 的代码分析摘要，已用原始 orders.py 原文独立核对）。
- 机制与对照实验：
  - 支撑事实 **F5**（inference，proposed）：库存只有 1 件、两个买家各买 1 件时，若两线程都在任一写回前执行第 81 行 `available()`，二者都读到 `current=1`、都通过检查、各自写回 0，两单都成交，第 59 行 `assert sum(order is not None ...) == 1` 失败（此时第 60 行 `available("BOOK")==0` 仍成立）。
  - 证据 `evidence/9918846a-fd8e-4596-b36e-bda9e33bee2d/agent-1/bc20536dab2b-code_inventory.txt`（reserve/place 摘录）与 `evidence/9918846a-fd8e-4596-b36e-bda9e33bee2d/agent-1/565902d79416-exp_out.txt`（jitter=0→200/200 失败、jitter=0.0005→199/200、不固定→0.4）与机制一致；`evidence/9918846a-fd8e-4596-b36e-bda9e33bee2d/agent-1/efdf4d0b8c63-repro_out.txt`（PART2：强制两次 `available()` 都在写回前返回 → `placed=2, available('BOOK')=0`）直接展示超卖交错。
  - 工具记录：`toolcalls/9918846a-fd8e-4596-b36e-bda9e33bee2d/c_7ul3kwut5olp.txt`、`toolcalls/9918846a-fd8e-4596-b36e-bda9e33bee2d/c_252bxiyalrnz.txt`、`toolcalls/9918846a-fd8e-4596-b36e-bda9e33bee2d/c_adwiq27m47f3.txt`。
- 归因排他（负对照）：支撑事实 **F7**（inference，proposed，resolves I2）：仅改副本、给 `Inventory` 的 receive/reserve/restore 加 `threading.Lock`（`evidence/9918846a-fd8e-4596-b36e-bda9e33bee2d/agent-1/f87d7b16c0cd-locked_orders.py`、`evidence/9918846a-fd8e-4596-b36e-bda9e33bee2d/agent-1/69551d4c41a9-counterfactual.py`），共享代码未改动；结果 `evidence/9918846a-fd8e-4596-b36e-bda9e33bee2d/agent-1/d4af18d66df0-counterfactual_out.txt` 显示 A) 真实测试固定抖动=0 跑 300 次失败 0、B) 无抖动并发驱动跑 200 次失败 0；同场景无锁原版为 `evidence/9918846a-fd8e-4596-b36e-bda9e33bee2d/agent-1/154ff1754ad5-repro_natural_out.txt` 200/200 失败（F6）。加互斥即消除、不加即稳定复现，排除其他独立失败机制。
- 观察性旁证 **F4**（observation，proposed）：自然偶发失败率约 30-41%（`evidence/9918846a-fd8e-4596-b36e-bda9e33bee2d/agent-1/74d86ac1b95f-rate_out.txt`、`evidence/9918846a-fd8e-4596-b36e-bda9e33bee2d/agent-1/1bc322cbc7b2-pytest_loop.txt`）。
- 说明（修正/限定）：F5/F1 中的行号经原始 orders.py 逐行复核**准确**，无需修正。F1 本身 provenance=self_reported，但其所引原始文件 `evidence/9918846a-fd8e-4596-b36e-bda9e33bee2d/agent-1/2abb4e510b38-orders.py` 使该代码分析可被独立核实，不构成孤证风险（见第 4 节）。

### A2 给出能稳定复现失败的方法（复现率不低于 90%）—— met
- 结论类型：**有工具输出支撑的确定结论**。
- 方法一（支撑事实 **F3**，observation，proposed）：`/workspace/shared/.venv/bin/python /workspace/agents/agent-1/repro.py 100`。脚本 `evidence/9918846a-fd8e-4596-b36e-bda9e33bee2d/agent-1/b94901a39e25-repro.py` 直接加载并调用仓库真实测试函数 `tests/test_orders.py::test_concurrent_orders`，仅在调用前把 `test_orders.random.uniform` 固定为返回 0.0（去掉测试自身的 0-6ms 启动抖动，不改动任何被测代码）。输出 `evidence/9918846a-fd8e-4596-b36e-bda9e33bee2d/agent-1/efdf4d0b8c63-repro_out.txt`：`PART1: N=100, failures=100, rate=1.000`，失败点即 test_orders.py 第 59 行断言。抖动扫描 `evidence/9918846a-fd8e-4596-b36e-bda9e33bee2d/agent-1/565902d79416-exp_out.txt`：jitter=0.0→200/200、jitter=0.0005→199/200，均≥90%；不固定抖动仅约 0.4。工具记录：`toolcalls/9918846a-fd8e-4596-b36e-bda9e33bee2d/c_252bxiyalrnz.txt`、`toolcalls/9918846a-fd8e-4596-b36e-bda9e33bee2d/c_adwiq27m47f3.txt`。
- 方法二（补充证据，支撑事实 **F6**，observation，proposed）：`/workspace/shared/.venv/bin/python /workspace/agents/agent-1/repro_natural.py 200`。脚本 `evidence/9918846a-fd8e-4596-b36e-bda9e33bee2d/agent-1/7388650c6d49-repro_natural.py` 直接 import 未改动的 orders.py，复刻被测场景（2 买家、库存 1 件、Barrier(2)），仅省略测试自身的随机启动抖动，**不对测试或服务做任何插桩**；输出 `evidence/9918846a-fd8e-4596-b36e-bda9e33bee2d/agent-1/154ff1754ad5-repro_natural_out.txt` 为 200/200 失败（rate=1.000）。工具记录：`toolcalls/9918846a-fd8e-4596-b36e-bda9e33bee2d/c_dnr5tppljb22.txt`。
- 背景对照（F4）：原始自然运行复现率仅 30-41%，远低于 90%，故确需上述确定化手段；这属方法说明，不削弱≥90%的达成。
- 合并说明：F3 与 F6 是**同一结论（存在≥90%复现方法）的两条互补证据**——F3 走“跑真实测试函数 + 固定抖动”，F6 走“完全不动测试、只复刻场景”，二者结论方向一致、互相独立，归并保留。

## 2. 被否定的 intent 及排除依据
- **无被否定的 intent**。I1（偶发失败是否源于 Inventory.reserve 缺同步导致超卖）result=confirmed，closed_by F5；I2（加互斥后失败是否消失的反证）result=confirmed，closed_by F7。两者均得到支持而非否定。

## 3. disputed fact 及双方证据
- **无 disputed fact**。快照中 F1-F7 均无 disputes 字段记录，无未决争执链。

## 4. relied_by=0 且 provenance=self_reported 的单一来源 fact
- **F1**（kind=observation，provenance=self_reported，relied_by=0）：内容为 orders.py 中 Inventory.reserve / OrderService.place 的代码分析，声称 `reserve` 为无锁 check-then-act、`_order_lock` 不覆盖库存扣减。
- 单来源提示：F1 自身标记为 self_reported（无直接工具调用绑定，call_id 为 null）；但其 evidence 列表包含原始文件 `evidence/9918846a-fd8e-4596-b36e-bda9e33bee2d/agent-1/2abb4e510b38-orders.py`，本次裁定已读取该原文逐行核对，F1 的代码描述与原文一致。因此 F1 作为“单一来源自述”列出，但其实质内容已由原始文件 + F5 + F7 交叉印证，不构成未决孤证风险。

## 5. inconclusive intent 与 notes
- **无 inconclusive intent**。I1、I2 均已 closed 且 result=confirmed，无 notes 待人工判断项。

## 6. 仍未满足的验收条件
- **无**。A1、A2 均判 met，全部验收项通过。
- 边界说明（不阻塞验收）：A1 归因唯一依赖“库存读改写缺互斥”这一机制，F7 负对照已把该场景下的失败完全消除，故未发现残余的其他失败机制；若需绝对完备，可另行验证 `receive`/`restore` 的同类读改写竞态，但二者不参与 test_concurrent_orders 的失败路径，不影响本裁定。