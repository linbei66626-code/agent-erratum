# 给 DeepSeek：R/E r2 顺序关联最小补修

项目 `/Users/lrc/Documents/研究生资料/实验/agent-erratum`。先读项目 AGENTS.md。r2 的原先三个具体反例已被 Codex 用当前代码新生成捕获核验（没有跳过 provenance），50 项定向全过。不要重做已通过的 driver 修复。

本次只补 **checker 两处“按内容搜索整个未消费集合”导致的乱序漏检**。允许修改 `ae_cloud_re_input_audit.py`、`tests/test_ae_cloud_re_pair.py`，并新增 `results/ae-cloud-re-pair-20260912-r3/` 交付物；不改 driver、旧 capability/辅助模块、捕获端、proxy、配置/预算/模板/更新语义、上游源码、旧 raw/报告/日志。保留 r1/r2 及两轮 Codex 探针。无 SSH/部署、服务启动或模型调用，不重跑 r4。

证据与可复现脚本：`results/ae-cloud-re-pair-20260912-r2/codex-review-r2/README.md`、`probe_order.py`、`order-probe-summary.json`。

## 必修 1：辅助调用不能跳过较早记录去匹配未来响应

当前 `auxiliary()` 的 `next(c for c in candidates if request/response内容相符)` 仍是集合匹配。探针中实际 provider 响应为 B→A，自报 native/事件/Agent 输入为 A→B，审计 VALID，映射顺序从云请求 sequence 40 返回 28。正常 control 为 VALID。

按本臂真实捕获顺序逐次对齐 native 与云调用，比较下一条的请求/参数/响应；或者使用已有可靠调用关联并显式核该顺序。不得跳过不匹配的较早记录去找未来记录。保留当前阶段窗口、完整内容核对、唯一消费及未消费关闭检查，不放宽它们。

新增反例应如现有探针：在 fixture opener 交换实际 provider 的两个不同响应，native 自报/事件/Agent 输入不变，CloudAuditProxy 生成真实格式记录。不能只改 runtime_user 正文、不能由明显损坏的日志序号代替语义门。覆盖正常多次继续、同值重复文本、真实糖度澄清通过；不同响应跨次错配拒绝。必要时同时核回传事件与实际请求之间的先后，不能让未来响应解释较早 Agent 输入。

## 必修 2：成功更新与 PATCH 必须保持真实调用/执行顺序

当前 `memory_gate()` 搜索任意未消费同值 PATCH，允许两个不同更新事件逆序。探针生成同臂 replace p007→add p008，交换两条 client_tool_result 的 event payload，原 timestamp/sequence 留在原槽位，其他 raw 不变，仍 VALID；映射顺序为 memory-2/http-23→memory-1/http-17。

按本臂下一条成功 memory_update 与下一条对应 PATCH/确认逐次对齐，核调用 ID、请求/返回及实际顺序；bridge PATCH 帧的消费也不能用“跳过不同记录去找相符内容”绕过顺序。不能仅靠增加计数、取首个内容相符项、调整时间窗或去重解决。工具调用可能批量出现，应按已有实际调用关联核对，不能禁止合法批量调用来省略验证。

新增保持日志结构有效的同臂不同更新 payload 乱序反例；同时保留两次同值 replace（包括快速 fixture）、原 double、无更新/错误更新、低分正常任务为有效观察，缺/多 PATCH、缺确认/确认篡改、E PATCH 等既有反例继续拒绝。相同字节不可区分的边界不用于解释不同调用 ID/不同块值的乱序。

## 交付

新 r3 交付保留改前两文件、最小 patch、新 SHA、定向与适当全量禁网测试日志。用当前 r3 代码重新生成正反例捕获并跑完整审计（不跳过 provenance）；可另附旧捕获只读复算，但须单独标明跳过门，不能取代新捕获验收。不要覆盖任何既有交付或探针。

本次不扩展研究定义、模型调用数或多轮 t4→t12，不收紧 requestor 取值级检查。若需要超出文件范围，先说明具体必要性和最小 diff 建议。
