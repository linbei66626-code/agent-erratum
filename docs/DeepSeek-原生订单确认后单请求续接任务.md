# 原生订单确认后：单请求续接诊断

2026-09-13。用户已授权实际测试；Codex将其范围说明为确认后支付分支，最多1次真实请求。既有入口每次新建空状态，不能用于续接。按项目分工待人工转交，本任务仅离线实现；核验后Codex执行一次，不重复申请同一预算。

## 唯一问题

上次模型真实创建订单OTb95a8ee52a，收到真实回包后询问是否确认支付。明确回复确认后，能否用已有ID真实支付？不是重跑原小案例，不恢复R/E，不证明稳定能力。

## 范围与输入

仅新增 scripts/ae_01_native_order_confirm_probe.py、tests/test_ae_native_order_confirm_probe.py，以及独占 results/ae-native-order-confirm-20260913-r1/。复用已有工具绑定/CloudAuditProxy/私密密钥加载/固定Vita类，不修改既有代码或封存文件。

权威源：transfers/ae-native-order-probe-live-20260913-r1/ae-native-order-probe-20260913-codex-r1-run/ 的plan.json、result.json、chat-wire.private.jsonl；摘要见上级remote-SHA256SUMS。远端源在deployment/runs/同名目录。要求显式源目录及新独占输出目录，不依赖Mac默认路径。

按源文件与捕获相互核对后，恢复final_native_snapshot为真实Vita环境，恢复前后完整snapshot精确相同（不是再次调用create伪造原订单号）。原订单真实ID、unpaid、数量及全部字段保持。保留第二次真实upstream请求消息，追加其真实assistant回复，再追加唯一新user消息：『确认支付，请现在完成支付。』不在新消息补订单号，不改系统/原历史，不加防循环提示。PLAN冻结所有来源SHA、原始状态、追加文本、实际请求、模型与预算，禁网。

## RUN

max_requests=1、不重试、65秒节流、temperature0、max输出2048、parallel_tool_calls=false、不传seed，沿用原模型与两个完整工具schema；保持CloudAuditProxy严格传输与密钥权限。模型调用按原顺序与原ID/参数执行，整批处理，不代填/修正ID。未声明/参数非法完整留证，不确定副作用立即停、不重放。只有真实provider响应算能力证据。

本分支成功只定义为：对封存唯一订单执行来源匹配的pay，最终该订单paid，其他订单字段与原状态保持且无新订单、无未处理调用/不确定结果；重复支付可单列冗余。原attributes=7分糖原样保持，不用此分支检验原来的规格规范字符串，也不回头改原结果。无调用/错误ID/再建单/未支付均如实记录。只一请求，不能补第二次确认或再次试验。

报告明确restored_from_snapshot=true（不是同一个持续存活进程），new_user_confirmation=true，fixture_used与请求数，scientific_result=null；不称原两请求任务变成功，不称R/E通过。完整wire私密留档，失败也保存状态。旧目录拒绝零写入。

## 离线验收与交付

真实Vita恢复源snapshot精确一致；真实历史与原回包逐字保留；模型fixture从实际历史提取ID支付后唯一订单paid；虚构ID、不调用、额外创建、未知工具、不确定异常不误报；最多1请求无重试；源SHA/状态/助手回复错配拒绝；旧输出目录零写入；PLAN禁网。fixture不是真实能力。

只跑新增模块及必要相关验证，不做全量回归。交付两个文件完整字节/SHA、测试日志、准确PLAN/RUN命令与新输出位置。DeepSeek本轮不联网/上传/部署/执行RUN。Codex核验后先无密钥HTTPS检查校园网，再执行已授权单请求；不用真实RUN探网络。
