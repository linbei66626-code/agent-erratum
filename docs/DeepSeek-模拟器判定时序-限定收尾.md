# p2复核：只收尾判定时序（2026-09-16）

复核交付 results/ae-simulator-eval-repair-r1-p2，SHA通过。前次数量uncovered、未付款不等于取消的反例已由修复与验收覆盖；本次不重开评分开发。未部署、未调模型、未改生产代码。

## 1. 不能用终态数据库重判早期用户话语

`ae_cloud_re_multiturn_input_audit.py::_replayed_simulator_decisions`循环之前从最终snapshot提取一次new_ids/paid_ids，所有用户轮次共用。运行时则在每次用户回复读取当时数据库。这不是相同输入的重算。

已直接执行生产函数的离线最小反例：早期用户说“订单 AB1234567890 怎么还没付款？”，当时该ID不存在，校验为SIMULATOR_PROTOCOL_VIOLATION/unknown_order_id；给审计的终态添加该订单后，重算变成USER_TURN_ACCEPTED。此为合成时序反例，不声称真实r5发生过此条。

修法：判定必须绑定用户轮次当时的可见输入及订单状态。可复用已核验的有序工具证据重建，或记录逐轮最小状态并与工具证据核对；不能直接相信新记录的状态标记，也不能用未来订单替过去提供证据。测试覆盖创建前/后、付款前/后、第二笔订单出现后的旧回复重判。

## 2. 指令判定不能等第一条user_reply才交回

NativeVita.start已生成指令判定，但只通过第一次user_reply携带给驱动。`_run_one_phase`允许Agent首次返回STOP后直接finish，此时simulator_decisions仍为空，transcript已有指令用户轮次，新审计将按缺判定拒绝正常结束。

已执行真实`_run_one_phase`（沿用tests/test_ae_user_simulator_evaluation_repair.py的离线Bridge/Native表面替身，Agent返回STOP，不调用模型），输出termination_reason=agent_stop，simulator_decisions=[]，user_turns=1。生产源码同时确认start事件已有判定、返回值未携带该判定。

修法：从start直接交回并立即收集指令判定，第一条user_reply不再重复交回。验收零次user_reply直接agent_stop，以及一次/多次user_reply，数量/顺序均准确；失败停止保留证据，不重复指令。

## 边界

只修上述同一判定时序链，不修改评分/模型/任务/记忆/预算/服务补丁，不部署、不调模型、不全仓测试。用真实NativeVita边界和生产驱动验证零次用户续轮路径；新独占目录交付定向证据后停止。旧封存字节不动。
