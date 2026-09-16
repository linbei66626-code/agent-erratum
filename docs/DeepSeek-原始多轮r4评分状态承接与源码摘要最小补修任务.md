# 原始多轮 r4：只闭合状态承接与源码摘要比较

范围：审计评分路径及直接相关定向测试；若固定摘要声明确需调整，仅最小调整相应 provenance 记录并说明。不得联网、SSH、部署、调用模型，不重复全仓。保留 r1–r4 和核验证据完整字节，新增独占交付。

## 已确认通过

r4 交付 SHA256SUMS 全部通过。Codex 复跑 ArchivedRealScoringFormatTests：1 项 OK，真实两臂三窗口格式通过。不要重做跳号、围栏、最终字段兼容。

## 两个必须先复现的缺口

证据：results/ae-multiturn-r4-codex-review-20260914/probe.py 与 probe-results.json。这是直接执行生产 judge_gate 的组件探针，不是完整传输审计绕过证明。

1. 第二窗口 current_rubrics 的第一项 meetExpectation 翻转并改 justification，同步对应 window_evaluations.user_prompt，评分门仍接受。当前只有第一窗口全 false 的检查，后续窗口没有与上一窗口固定原生状态转换结果比较。修复应由 pinned 原生初始化与状态更新规则推导每一步 carried state，完整核 rubric、meetExpectation、justification（包括初始理由）；不要只加真假值比较。合法多窗口链保持通过。
2. _pinned_modules 只是计算并记录文件 SHA，没有同任何已核固定期望值比较。在独立临时源码副本中给 evaluator_traj.py 加一条注释，仍接受。不是要求拒绝注释的业务行为，而是交付声称的固定字节验证实际上没执行。将实际导入路径和文件 SHA 与可追溯 pinned baseline/已核声明比较；不能把当场被核文件自身的摘要再当期望。registry.py 未变不能证明 scorer 六个模块未变。临时副本不得改动原 checkout，原归档不得覆盖。

## 最小验收与停止点

复跑原控制与以上两个反例；补后续窗口 bool/justification 各一反例、初始状态异常反例、固定模块字节异常反例，正常状态推进保持通过。复跑真实旧 t4 评分组件以防引入误拒；完成一次现有多窗口链的针对性回归即可，不重复全部 47 项或全仓。交付 before 完整字节、精确 diff 与短结果后停止。

不要扩展 env_info database、usage、三窗口以上变异、辅助调用或其他生产路径；这些不属于此次任务。无真实 RUN 结果，不改变旧有效性结论。
