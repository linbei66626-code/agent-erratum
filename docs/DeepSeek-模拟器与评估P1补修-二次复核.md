# P1补修二次复核（2026-09-16）

只读核对 results/ae-simulator-eval-repair-r1-p1；SHA校验通过；未部署、未联网、未调用模型、未改生产代码。复用交付测试中的真实投影与生产链fixture执行以下反例。

## 已关闭

P1-1原三类正常回复反例有针对性修复；0.5原先wrong_multiturn_schema阻断已消失，完整离线fixture基线公开审计VALID。不是推翻整轮修复。

## P1-2仍未完全关闭：按主题词宣称覆盖

生产函数`standard_item_is_covered`只要含“下单/预约/商家/地址/配送”就可能声称覆盖，不能证明有对应检查。

直接复用`tests/test_ae_user_simulator_evaluation_repair_p1.py::DeterministicSubItemTests`的真实t4订单与任务：
1. 原始：complete=true。
2. 在standard_items和rubrics同时增加“下单数量必须是2杯”（新rubric ID），保持真实订单quantity=1：complete仍true，uncovered为空。
3. 将原正确订单status改为unpaid：payment_required=false，但order_not_cancelled=false、complete=false。名为“未取消”的检查实际上排除了所有unpaid，暗中强加付款/完成态，和不要求付款的声明冲突。

最小闭合：覆盖必须由实际生成并执行的明确检查对应，而非关键词；未支持的数量等标准直接uncovered即可，禁止为通过这个反例扩大成通用评分器。区分“订单未取消”与“付款已完成”，后者仅在任务要求时施加。保留原reward与已修错误糖度/地址/取消反例。

## P1-3仍未完全关闭：审计相信自报结果

真实公开入口复现方法：
- 使用新增套件AuditAccepts0_5Tests的build_full_chain，0.5基线VALID/input_audit_passed=true。
- 将result各阶段simulator_decisions全部清空。
- 各evaluation.phase.business_completion设置requirements={}、complete=true、unmet_requirements=[]、uncovered_requirements=[]、uncovered_standard_items=[]、new_order_ids=['FAKE_ORDER']。
- 真实写回该临时fixture的result.json（不是封存r5），重新调用chain.audit_of。
- 仍VALID/input_audit_passed=true；repair_protocols.simulator_decisions=0。

这不是要求加新的安全体系：原要求就是独立核验实际模拟器与评估证据。当前门只遍历存在的列表并核对自报空列表，缺记录不拒绝，虚构订单也不核数据库。

最小闭合：
- 按arm/task/真实用户轮次绑定判定记录，数量与内容缺失/重复/错配拒绝；从已核验回复及对应阶段状态重算可复算判定，不能只信violations/claims/stop。
- 评估逐arm/task与已核验任务标准及原始阶段数据库重算/比对；拒绝假订单、删requirements和伪造完整性。复用生产纯函数即可，不复制一套新评分器。
- 公开入口基线通过；分别删除判定、伪造业务记录各自拒绝，不只测两个错误合并。
- 旧0.1–0.4路径不放宽，不覆盖历史轨迹。

## 停止边界

仅上述两个仍开放的P1子项；不重新开发已关闭部分，不部署、不模型调用、不全仓测试。新独占目录交付反例前后输出与直接影响回归即可。暂不完整RUN，避免再次付费后发现收尾不能信任。
