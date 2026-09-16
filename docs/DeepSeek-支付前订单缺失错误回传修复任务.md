# 支付前订单缺失：最小错误回传修复

2026-09-13。用户已要求查因修错；按项目分工待用户人工转交，尚未派发。仅离线实现和验证，不 SSH/部署/启动服务/调用模型。

## 已核原因

真实新配对 `transfers/lab-cloud-re-multicall-run-20260913-r2/`：E 未调用 create_delivery_order，自称已创建，模拟用户接着声称支付成功，模型最终以无来源订单号 O202406231430001 调用 pay_delivery_order。环境订单为零，provider 调用与 Letta 审批 ID/名称/参数完全一致。旧运行仍 INVALID，不能改写为有效失败或给 E 补零分。

固定 Vita `src/vita/domains/delivery/tools.py` 的 pay_delivery_order 先调用 _get_delivery_order；缺订单分支抛 ValueError，在支付状态修改之前。原生 Environment.get_response 将异常转为 error=True 的 Error: ... 工具回包。项目 environment_bindings 仅支持已核 READ 商品不存在分支，LettaBridge 又拒绝 WRITE RecoverableToolError，故普通前置失败被升级为 BridgeBlocked。

本轮 Codex 实际离线探针：`results/ae-payment-precondition-diagnosis-20260913-r1/probe.py` / `probe.json`。真实 Vita 对象、同一 ID、禁止外连；原生入口返回错误且 db 不变；当前桥接停止且 db 不变、client_tool_result=0。探针为 fixture，不是新云捕获。

## 允许修改

仅 `ae_task_run.py`、`ae_adapter.py`、`tests/test_ae_tool_errors.py`、`tests/test_ae_cloud_re_pair.py`；新增独占 `results/ae-payment-precondition-20260913-r1/`。先冻结四文件原字节及完整 SHA。如实际必须改其他文件，说明具体断点再提范围，不自行扩展。

## 实现要求

1. 仅增加 pay_delivery_order 的固定原生“Order {order_id} not found”前置失败恢复。保持其真实 WRITE 分类；不得改为 read_only，不得全局接纳 WRITE 的 ValueError/RecoverableToolError，不得只按工具名或错误文字认定安全。
2. 以已核真实代码身份、调用路径、精确缺失 raise 分支、本次 order_id 对应建立有界判定。核真实 pay 方法及 helper/绑定来源；同名假方法、包装方法先写后调用原 helper、其他 raise 位置不可借真实 helper 冒充安全。证明受支持调用链在该分支之前无环境写入。冻结的参数调度不能被 _name/tool_name 等参数改绑。
3. 桥接保留一般 WRITE 异常拒绝，只接纳可信绑定对本分支产生的专用错误证明。返回与原生入口同义的 Error: <原文>、status=error、原 tool_call_id，一次执行一次回包，真实 transcript 标 error。不得自动创建订单、修正 ID、自动重试原调用、注入“应该创建订单”提示或支付成功消息。
4. R/E 共用同一处理。保持现有 READ 恢复、严格多调用顺序/全长 ID、预算、评分停止规则、审计全部门。不要修改 user_simulator、模型/提示词/配置、数据集/评分、固定上游、旧 raw/报告/manifest/receipt。不要为了旧 provenance 报错放宽审计；旧证据需冻结字节复算时单列其边界。
5. 这项修复只恢复模型接收环境错误并继续决策的机会，不声称修复模型幻觉。随后仍不创建订单、再次猜 ID、最终失败都是合法模型行为；不得强制成功。

## 必须验收

- 真实固定 Vita 类：精确缺失订单产生原生错误，前后完整 db 相同；已存在 unpaid 订单仍实际支付成功。显式源码路径，缺固定源不可用 skip 冒充通过。
- 真实桥接链：错误一次执行、同 ID 回传到实际下一请求、transcript 正确；再由明确 scripted 模型响应调用真实 create_delivery_order，从实际返回提取订单 ID，再调用真实 pay，订单 paid。不得手填真实 ID 或在测试外代做创建；明确这是 fixture 恢复链，不是模型能力实证。
- 负例：WRITE 修改后抛错；同文字不同异常/不同位置；假 helper/替换 pay；先写再调用原 helper；不匹配 order_id；其他订单错误（如非 delivery）；其他 WRITE/未知工具；序列化错误，都继续停止且不重试。原有读恢复与参数防改绑回归保持。
- 两臂生产 driver fixture + 完整审计：合法错误回包后继续任务/评分可被接受；删除/篡改错误回包或 ID 仍被拒；模型未补救不被包装成成功。尽量复用现有真实 CloudAuditProxy 测试链，不 mock 核心审计门。
- 多调用启用时，批内前置失败不丢其他调用与回包；未知副作用错误仍中止，保留已执行证据，不重放已执行调用。

## 交付

四文件改前后 SHA、完整 diff、原生与桥接正反例、定向工具错误/桥接/RE 审计回归日志（注明 fixture、无 skip 数）、短说明。按变更范围验证，不无由重复全部测试。本轮不部署、不封上传包、不重跑模型。另注明本次代码变化使旧部署包摘要过期，后续新批次部署核摘要；旧真实运行永久保留原 INVALID。
