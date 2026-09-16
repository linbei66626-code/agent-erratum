# timing交付复核：时点与索引尚未闭合

2026-09-16。只读核验 results/ae-simulator-eval-repair-r1-timing，SHA通过。未部署、未调用模型、未改生产代码；没有重复整组回归。以下三个反例直接执行当前生产审计方法，不是已发生的现场实验。

## 同一时序缺陷的四个具体位置

1. `ae_cloud_re_multiturn.py::_run_one_phase`先执行`bridge.run_stage`，之后才`pending(native)`并`orders_now(...,0)`。因此指令判定虽然不再丢失，指令状态却已经包含Agent首轮工具操作。应在native.start后、任何Agent/history工具操作前记录指令状态。

2. `_order_state_by_turn`用`created_by_tool[:tool_calls_so_far]`。前者是去重订单ID列表，后者是工具调用数，且驱动记录的是bridge累计调用数而审计读的是本阶段slice。不同索引空间不能相切。必须使用本阶段真实工具事件位置，并把每一用户边界与实际transcript/工具事件顺序绑定，不能只信自报计数。ID提取亦不能把任意出现order_id的查询回包当创建事件。

3. 审计只核订单ID集合，没有验证paid/unpaid/cancelled状态如何由此前成功工具调用产生。伪造paid目前能通过；状态投影可以保留，但必须与有序工具结果交叉核实。

4. `need(created_by_tool)`把未创建订单的正常失败阶段判成记录无效；要求末个用户轮状态等于终态，又误拒最后用户轮之后Agent创建订单再STOP的合法路径。用户轮快照与阶段结束快照应独立；未下单可业务失败，但不是审计必然失败。

## 三个已运行的最小反例

直接构造任务记录并执行真实`MultiturnInputAudit._order_state_by_turn`：

- 无工具、指令轮空表、终态空表 → REFUSED `phase_tool_evidence_records_no_order_creation`。应该允许有效记录无订单。
- 工具1=search results，工具2=order_id=AB1234567890；边界自报只完成1次工具，却已带该订单paid；终态该订单unpaid → ACCEPTED。这同时暴露调用序号/订单序号混用与状态不核验。
- 指令轮空表、随后工具创建订单、Agent直接结束，终态存在订单 → REFUSED `phase_order_missing_from_the_captured_table`。末个用户轮不应强制等于最终状态。

## 最小完成要求

先把上述三例与“指令在run_stage之前采样”作为验收，再修实现。沿用既有有序工具证据，不新建通用数据库重放框架；本场景只需识别已有创建/付款/取消语义，无法可靠解析时明确拒绝或未知，不能猜状态。

验收须覆盖：历史累计工具计数非零的下一阶段；查询在创建之前；创建后未付；付款后；取消后；零订单正常结束；用户最后回复后创建/付款再agent_stop。至少正常路径走真实驱动+真实native工具状态，不能仅手写永远一调用一订单的fixture。公开审计基线与负例都执行；不改评分、模型、任务、预算、不部署、不调模型、不全仓回归。

已关闭的指令判定领取一次逻辑无需推翻，只把状态采样提前。当前不建议完整RUN，原因是正常结束仍会被拒且错误时序仍能被接受。
