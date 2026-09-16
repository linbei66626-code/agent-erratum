# 模拟器与评估补修 r1：限定复核（2026-09-16）

交付：results/ae-user-simulator-evaluation-repair-r1。Codex只读复核；未部署、未调用模型、未修改生产代码。交付SHA校验通过；定向套件独立执行47 passed /108 subtests。以下反例在当前生产函数上直接执行，说明已有测试未覆盖关键情况。

## P1-1 正常用户回复会被误拒

`ae_sim_eval_protocol.py::validate_user_reply`：

- `请送到公司。\n不要加糖。` → simulator_reply_format_invalid。
- `助手，你能帮我确认地址吗？` → simulator_wrote_assistant_turn。
- 合法引用助手完整推荐再确认 → simulator_reproduced_incoming_agent_line（并可能因多行再拒绝）。

一轮用户发言不等于一行文本；称呼助手不等于写助手台词；引用不等于冒充。当前规则会把正常交互变成协议失败并终止运行。

最小修复：移除多行/长句复述/角色词出现本身即拒绝的充分条件；识别有明确证据的跨角色续写。保留r5已确认反例。上述三类正常回复必须通过。不要堆针对这三句话的白名单，不增加重试或输出改写。

## P1-2 子项检查被写成整体业务完成，且缺项默认通过

`deterministic_business_result` / `order_meets_purchase`：

使用真实prepare_sample → scoring_scope的t4任务与r5 R t4新订单：原始订单complete=true；只将同订单status改为cancelled、attributes改为“规格: 全糖”，仍complete=true。

同一次真实投影得到delivery_at_work_address.rule=None，地址项因无规则直接通过。缺target规则同样自动通过。距离仅记录数值，未进入requirements。糖度/规格/数量等未覆盖，却汇总为business_complete。

最小修复优先采用收窄声明，而非再实现一整套评分器：明确输出“确定性子项检查”，记录checked/uncovered/unknown；未覆盖全部任务条件时不能输出整体complete=true。已取消订单不能当有效完成订单；缺标准不可记met=true。真实地址对象与字段来源按数据结构处理，不能靠str(dict)==address。评分映射应使用实际数据集计分标准，不将按列表位置编号描述为已核验rubric ID绑定。

验收：真实t4正确订单与错误糖度、取消、错误地址、缺标准最小变异；完整性不足必须标明，不能虚报成功。不要改变原生reward，不补写旧数据。

## P1-3 新0.5未接进事后审计

真实`MultiturnInputAudit._declare_run_contract`对新候选、plan/result一致时立即拒绝：`AuditFailure: wrong_multiturn_schema`。现有审计只接受0.1..0.4。新CLI能PLAN/PREFLIGHT不等于完整链已闭合，不能再次等付费跑完才发现。

最小修复：显式接入0.5及新模拟器提示/协议/评估记录核验，沿用0.4传输容量与预算合同；旧协议不放宽。用公开审计入口跑一份离线完成态真实形状fixture，另验证篡改协议或模拟器提示/评估记录会被拒绝。不得仅增加schema白名单后跳过新字段校验，不以mock前置门宣称通过。

## 操作结论

本交付尚不适合完整RUN。修复仅限上述三项，不跑全仓、不重新封存历史实验、不部署、不调模型。已读代码显示新增协议在每次run的驱动进程加载；无证据要求重启Letta/模型长驻服务。下次新起驱动即可加载，若另有常驻进程需重启，必须指明其实际导入依赖。

README关于R t7未付的段落也需纠正：原r5 R t7订单为paid；五个未付款阶段仍是R t8/t11与E t5/t7/t10。不要修改原始轨迹。

新建独占补修交付目录，完成三个反例及直接影响回归后停止；不要扩展其他开发。
