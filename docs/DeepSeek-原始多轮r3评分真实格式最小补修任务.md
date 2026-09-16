# 原始多轮r3：只补评分真实格式与内容核对

本轮严格缩小范围：仅多轮审计的评分门、对应fixture与定向测试。禁止联网/SSH/部署/模型RUN，不再扩部署/全仓回归。r1-r3、回放与Codex证据全部保留。新增独占r4交付，冻结before完整字节与SHA。

## 必须先复现的真实差异

Codex证据：results/ae-multiturn-r3-codex-review-20260914/real-score-format-check.json。源是已封存真实单t4代理wire与result，非合成回复。

- 实际首窗口编号跳过空工具回包的[4]；r3 _expected_windows用range生成连续编号，错误拒绝。应使用固定原生格式化函数的实际输出，而非猜测每个Message都打印一行。
- 实际evaluator回复包含```json围栏；r3 json.loads(content)拒绝。必须复用/忠实执行固定Vita真实解析路径，不能自定更窄格式。
- 实际最终nl_rubrics项为nl_rubric/met/justification；r3核rubric_idx/meetExpectation不符合原生输出。聚合须依据固定真实聚合函数，不凭fixture自定all-or-nothing。
- r3目前只比窗口编号，没有逐字核窗口真实正文；rubric_section只留索引与bool，没有核原始评分要求文本。最终状态也未逐项与最后窗口决策绑定，final缺失/空值还会跳过聚合核对。这些语义缺口要一起在评分门内闭合。

## 完成方式

先读取固定evaluator源码及既有真实t4的所有评分请求/响应/最终reward，复用真实窗口构造、消息格式化、提示模板、回复解析和聚合。期望评分请求绑定本任务原始rubric、实际transcript及应有环境信息。导入的函数/消息类必须核实际模块路径与固定源码SHA，不能只sys.path.insert后信任缓存import。

新增真实旧t4评分链独立只读验证（只验评分组件，不把旧provenance改成新代码、不声称整轮新审计通过），要求3窗口、围栏回复、空消息省略、原生最终字段全部正常通过。

定向反例：窗口正文改字但编号不变、rubric文本变更、缺/多/重复/错序窗口、最后窗口决策与最终met不一致、final缺失/空、奖励篡改；保留合法低分正例，不能只能全对通过。所有期望来自固定原生函数而非fixture与checker彼此复制格式。

完成评分定向验证后，仅跑一次0.2完整18阶段fixture回归（含多窗口），不重复全仓。记录未测项，不把fixture当真实能力证据。交付精确diff与短说明后停止。
