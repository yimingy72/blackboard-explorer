# E1 default 统一语义复核（2026-09-26）

范围：只改本副本 `default/run-001/review.json`、`run-002/review.json`、`run-003/review.json` 和本说明。原始目录 `/Users/yym/blackboard-explorer/eval/results/e1-20260925-01/default` 未修改。未调用模型、联网、读取 `.env`、运行评估代码或执行归档脚本。以下依据为人工阅读 state/report、原脚本、原输出及代码快照；不是新的动态重跑。已逐项比较：原 P1–P6 assessments 的分类、定位、机制、可复现布尔值全部未变。

## 全批次一致的规则

1. 从全部 fact 和报告段落识别实际主张，保留原始内容、来源与旧分类。不能仅凭是否在隐藏答案、是否含风险词、是否填 satisfies、最终报告是否省略来判断问题。一次运行任一来源中存在肯定性错误缺陷主张，后续遗漏或改称观察不能将其洗掉；明确撤回可在说明中记载，但原误报仍留存。
2. 先区分缺陷断言、客观观察、利用步骤、明确待证假设，再核对不变量、机制、触发和结果。普通观察、明确假设与补证未完成，不自动成为业务 FP。肯定宣称某业务规则被违反，却没有目标/领域/代码不变量来源的，按过强断言记 FP；这不等于证明该业务绝对没有缺陷。
3. 缺陷按实际不变量、输入和修复边界归组。同机制的请求变体、下游表现及重复叙述只计一次；不同不变量/可隔离影响不能只因同一文件、同一大类危害或完整事务能一起修复就强行合并。重复叙述可 ignore，但须明确归入何项，不能把“非独立”作为 FP 的理由。
4. 列表外缺陷有明确定位、机制及实际触发结果时计 new；动态证据必须属于该 run，不能借另一 run 的实测升级证据。若只确定定位与机制，按现有字段给 0.5，存在无法判定的实质性分类疑点则维持待复核。可复现证据分与 replay 的执行成功率分开。
5. 同一 user_coupon ID 在单请求中多次抵扣，违反代码 `used` 所表达的单次消费约束；它区别于多张不同券正常叠加、区别于 total 无下界、区别于跨请求竞态。只要该 run 的脚本/输出已经证明唯一领取的同一 id 被按重复次数累计，不强制要求另有正总价样本才认定动态重复抵扣。正总价样本是更强的隔离证据，并非凭答案倒推出的新要求。

## 修改清单

| Run / finding | 原分类 | 新分类 | 依据与理由 |
|---|---|---|---|
| 001 F1：same-coupon-id-repeat-in-one-order | false_positive | new，location/mechanism/reproducible 均 true | 原脚本 P5 段只 claim 一次后传同一 id 四次；输出 total=-23500=500-4×6000，原券行只有一条 used=1。逐元素折扣与 total 下界是两处独立缺口。证据 E1/E2。 |
| 001 F1：库存不足绕过/超卖 | 原 note 已知为错却未计分 | 增补一次 false_positive，key=coupon-stock-bypass-overclaim | F1(4) 断言负 total 可绕过库存不足，F1(6) 断言超卖/库存负数。代码使用 `stock < quantity or balance < total`，库存校验独立存在；原输出 P7 库存97→94、余额20000→8000，没有超卖。真实重复用券仍由原 P3 计分，错误影响也不能忽略。证据 E1/E2。 |
| 001 R4 | ignore | ignore，修订去重说明 | 问题及库存表述来自 F1，均已在 F1 计分/保留误报；不是最终报告省略后免责。 |
| 002 F2：same-coupon-id-repeat-in-one-order | false_positive | new，三布尔均 true | negative 脚本只有一次 claim 得到 id=1，请求 [1,1,1] 后总价为10000-3×6000=-8000；代码对同一券行重复计入并只将其 used 置1。此样本与 stacking 脚本的五个不同 id 分开复核。证据 E3/E4。 |
| 002 F2：repeat-claim-without-limit-policy | false_positive | ignore | F2(b) 表述连续 claim 得五个不同 id 后触发负总价，是已知问题的真实利用步骤；未独立断言每人同码只能领一次。F7/F8/R4 重复此路径，不额外计问题。证据 E3。 |
| 003 F5：same-coupon-id-repeat-in-one-order | false_positive | new，三布尔均 true | 同 run F9/F10 的独立输出明确：500商品、同一200券id两次→200 total=100、余额-100、仅一券行 used=1，正常一次抵扣应付300。负total修复或跨请求互斥均不足以消除单请求重复id。证据 E5/E6。 |
| 003 F6：repeat-claim-without-limit-policy | false_positive | ignore | F6 原文是重复领取的客观结果与已知资金问题放大器；不独立认定同码一次上限。但报告 R5 的肯定性升级另记 FP，整次运行的该误报未消除。证据 E7 与 report R4/R5。 |
| 003 R5：claim 无幂等 | ignore | false_positive，同 key=repeat-claim-without-limit-policy | 报告把 claim 无幂等并列已确认资金/并发缺陷，R4以F6支撑A1 met。实测只说明可领多张不同券，未建立发券必须幂等/限次的不变量，属于过强业务缺陷断言。证据 E7。 |
| 003 F9/F10 | ignore | ignore，修订去重说明 | 正总价重复券的补强证据已归入 F5 的 new，不能因这两项 ignore 而丢掉独立发现。证据 E5/E6。 |
| 003 F11 | new | new，三布尔未变，仅修订理由 | 原脚本使用两张不同券仍能余额透支；另一用例失败500后券已消费却无订单。与同一券竞态可隔离；余额/失败回滚按同一结算原子性问题只计一次。证据 E8。 |
| 003 R9 | false_positive | false_positive，仅修订理由 | I2/R13 的假设不算误报，但R9中段肯定宣称既有/orders/history等接口会被遮蔽导致422。真实路径有/account前缀，肯定性机制错误；末句“未证实”不能覆盖中段断言。证据 E9。 |

## 保持不变的业务项与 ignore 扫描结果

- 001 F3/F4：重复 claim 明确仅是放大因子，公开目录明确无保密规则、只记观察；保留 ignore。券唯一性、金额、退款及接口结论均是 F1 的重复/覆盖记录。环境 F2 与其余报告段落为结构、证据链和状态说明，未发现需新增计分的独立业务缺陷断言。
- 002 F9：保留 public-coupon-catalog FP。原 fact 明说“最小权限问题（已复现）”，但仅证明匿名目录返回券码及面额，没有私有数据/授权边界依据。最终 R5 将其改称观察不抵消原断言。读取了 coupons.py 的无用户数据查询与相关接口矩阵；没有凭答案列表判断公开目录应否存在。
- 002 F7/F8/F10：接口检查、问题汇总和利用路径保留 ignore；独立重复券已经在 F2 计 new。F11 的负订单污染旧报表是负total的下游影响，合并原 F2/P6；F12 默认退款超过 remaining 是原 F4 的变体，不新增问题。两者均有 E10 原输出。F13 为测试结果背景，其余报告为去重/过程说明。
- 003 R13：明确未确认/未否定的线索，保留 ignore；不是“补证失败即误报”。R14 是验收缺口说明，未单独增加第二个路由误报；R9 的同根因肯定错误已计一次。F2、其余报告段落为环境、已有问题去重或验收过程。

## 证据索引

下列 URI 均可在对应 run 的 `evidence/index.json` 找到原始本地文件。未修改证据、脚本、state、report、score 或 replay 文件。

- E1（001动态）：`evidence/ad5623e9-4ec6-447d-82e9-9ac6a4ca939a/agent-1/9c003e733b80-probe_all.py`；`evidence/ad5623e9-4ec6-447d-82e9-9ac6a4ca939a/agent-1/3e7f96290d7f-probe_all.out`，P5/P7段。
- E2（001代码）：`evidence/ad5623e9-4ec6-447d-82e9-9ac6a4ca939a/agent-1/5a3b8a816931-diff_shop.txt`，coupons.py逐元素读取、used守卫、sum、stock OR balance守卫。
- E3（002动态）：`evidence/29eaa23a-51fa-455a-b163-e9eb2c4d9de3/agent-1/b21aa8bc758f-repro_coupon_negative.py`；`evidence/29eaa23a-51fa-455a-b163-e9eb2c4d9de3/agent-1/4004fa2d1dc7-repro_coupon_negative.out`；`evidence/29eaa23a-51fa-455a-b163-e9eb2c4d9de3/agent-1/3138e47cbefc-repro_coupon_stacking.out`。
- E4（002代码）：`evidence/29eaa23a-51fa-455a-b163-e9eb2c4d9de3/agent-1/24f6e6c3ade2-coupons.py`。
- E5（003正价隔离）：`evidence/a5f6288d-e505-4299-8bb7-f0541e216493/agent-2/228186957796-repro_i1.py`；`evidence/a5f6288d-e505-4299-8bb7-f0541e216493/agent-2/1c21c3b9f691-repro_i1.out`，E段。
- E6（003补强）：`evidence/a5f6288d-e505-4299-8bb7-f0541e216493/agent-2/4a110bd358af-coverage_neg.out`；`evidence/a5f6288d-e505-4299-8bb7-f0541e216493/agent-2/24f6e6c3ade2-coupons.py`。
- E7（003领取）：`evidence/a5f6288d-e505-4299-8bb7-f0541e216493/agent-1/6a87fe32712e-repro_static.out`第7节；同run report R4/R5与state F6。
- E8（003原子性）：`evidence/a5f6288d-e505-4299-8bb7-f0541e216493/agent-2/b3b39a8d8aa9-repro_overdraw.py`；`evidence/a5f6288d-e505-4299-8bb7-f0541e216493/agent-2/6a6dc87cc2bb-repro_overdraw.out`；`evidence/a5f6288d-e505-4299-8bb7-f0541e216493/agent-2/b61be715c1a4-db.py`。
- E9（路由事实，仅用于证伪，不借用其他run给动态发现加分）：本工作树 `eval/targets/mini-shop/src-main/shop/activity.py:11` 为 `APIRouter(prefix="/account")`；同目标001归档 `evidence/ad5623e9-4ec6-447d-82e9-9ac6a4ca939a/agent-4/8f0fe1a173c7-route_table.txt`列出/account/orders/history、summary、monthly。003的diff没有修改activity.py，保留旧review已确认的前缀事实。
- E10（002下游/默认退款）：`evidence/29eaa23a-51fa-455a-b163-e9eb2c4d9de3/agent-1/107cf87c42c8-repro_downstream_and_default_refund.out`。

## 疑点、保留限制与根代理复核点

- 003领取项的边界明确分开记录：F6客观放大器不单独计分，但R5/R4将其提升为确诊，所以整次运行仍有一次FP。这是上下文判断，不能变成“无幂等”关键词规则。
- 001 F1的“商品库存 -3”单独看可能表示减少3，但同fact又明确声称“超卖”“库存不足也通过”，与独立库存守卫冲突，因此FP落在错误业务机制与影响，不只依据一个数值歧义。报告R4的重复表述不再另加FP。
- 002 F12“余额比下单前多16000”数字措辞错误：输出90000→106000是退款前后增加16000，若下单前为100000则净增6000；它不改变已确认的超额退款机制，本轮不把同一问题的数值笔误拆成另一个业务FP。关于可“反复”的概括也不能理解为同一已全退订单无限重试均成功。
- 003 F5 的首个余额起点20000不准确，原输出在此前退款后已经是28000，随后33500；实际单次增量5500成立，不影响原P6评分。001并发样本没有证明余额透支；本轮不把真实可能机制的证据不足直接判成另一个业务FP，也没有为它加new。
- 001/002重复券动态样本带负总价，但证明的独立行为是同一券按次数抵扣；并非要求根据未执行的正价样本推断可复现。003有正价隔离佐证，但未用于提升其他run。
- 不建议把上述修订后的precision与legacy precision直接作提示词改进对照。legacy应保留，本复核作为独立补充口径；新旧批次需同规则、全量fact和报告一起复核后才能比较。
