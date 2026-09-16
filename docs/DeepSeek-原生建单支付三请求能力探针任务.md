# 原生建单→取真实订单号→支付：三请求能力探针

2026-09-13。用户已授权这个小任务；当前缺真实模型闭环入口。按项目分工待人工转交，尚未派发。DeepSeek仅离线新增实现/测试，Codex验收后沿用当前授权执行一次最多3次模型请求，无需重复确认同一运行预算。不恢复完整R/E，不换模型或修改既有冻结协议。

## 要回答什么

当信息齐全、历史为空且只提供两个必要工具时，当前Qwen模型能否通过真实建单回包取得订单号，再正确支付？这是基础工具链诊断，不是偏好更新实验；一次成功不证明稳定可靠，更不说明Letta或R/E长上下文已通过。

新入口直接使用现有CloudAuditProxy与固定Vita的工具/状态，暂不经过Letta，不启动数据库/Letta/用户模拟器/judge，不引入记忆或防重复提示。此次“订单”仅为Vita内存模拟数据库记录，不是真实外卖交易。

## 允许范围

只新增 `scripts/ae_01_native_order_probe.py`、`tests/test_ae_native_order_probe.py`、`results/ae-native-order-probe-20260913-r1/`。复用现有代码，不改它们：CloudAuditProxy及其严格规范化/密钥加载、environment_bindings及参数校验、固定Vita消息类与format_messages。生产脚本不要导入tests取fixture；可在新脚本中明确声明下面独立合成案例。默认PLAN禁网，显式RUN才调用模型。模型输出不能由scripted响应替代。

## 冻结小案例

沿用已验证可运行的 `tests/test_ae_tool_errors.py` 中 CHAIN_DB / CREATE_ARGS 所描述案例，但新实现独立记录完整案例：
- user_id U1；当前时间2024-06-23 10:00:00；订单为空。
- 门店S1、商品P1/Milk Tea、单价10元，库存/属性与已核CHAIN_DB一致，规格7分糖。
- 地址Addr，登记坐标(1.0,2.0)，与门店同坐标；要求配送2024-06-23 15:00:00。
- 用户一次性要求：为U1在S1购买P1一件，规格7分糖，送Addr，配送时间如上，并完成支付。用户已明确给出身份和授权，不再要求它推断偏好或找商品。
- 系统只作通常工具执行约束：以真实回包为依据，没有成功回包不得声称完成；不要加入针对上一轮循环的特殊提示。完整提示与案例在PLAN冻结可审查。

只向模型暴露固定原生 create_delivery_order / pay_delivery_order 两个完整schema，tool_choice=auto、parallel_tool_calls=false。真实返回订单号不可预先注入用户提示，不在模型返回后替填/修正参数或ID。

## 闭环与预算

最多3次实际chat请求，共用一个max_requests=3代理；模型与原节流配置不变（65秒，temperature0，实际请求输出上限2048），不传代理不支持的seed。不重试失败请求、不续跑同目录。新独占目录、密钥仅既有私密加载接口、记录完整wire及每步原生状态快照。

解析真实provider消息；保留其调用ID/参数/顺序，先按schema校验，再经现有可信environment_bindings执行真实工具，按固定原生消息表示把真实错误/成功回包送入下一次请求。不要伪造成功，不靠测试函数自动接力执行建单或支付。

所有支付请求order_id必须源自本次此前真实成功create回包；无来源ID可记录模型提议错误，并以真实现有错误机制处理，但不可替换为正确ID。若模型同批先建单又猜ID支付，不能因第一条刚创建就由driver代填第二条；按相同来源规则记录，不把它算通过。副作用不确定异常立即停，保留快照，不重放。

成功后立即停止，无需额外请求让模型复述：恰好1张本次新增订单、正确user/store/product/数量/地址/时间/规格、status=paid；支付ID与该创建回包及db记录精确相同，create回包先进入模型下一次输入。其它输入/工具链完整但模型不完成，记录模型任务失败/未完成；网络/捕获/执行不确定为诊断无效。没有工具调用却声称完成也不能算成功，不自动继续无意义对话。

不要调用旧R/E完整审计器并声称本探针通过其门；新报告仅核本探针实际wire、原生工具状态与ID依赖，scientific_result保持null。保留错误、请求数、停止原因及是否使用fixture。

## 离线验收

用真实Vita类、真实工具执行/序列化/参数检查和真实CloudAuditProxy的离线传输fixture。scripted模型按收到的真实创建回包取ID再支付，明确fixture，不冒称能力测试。覆盖：正常链；虚构ID；只口头宣称完成；参数错/工具未声明；回包被改/删；工具异常后无自动重试；请求3次上限；已有输出目录拒绝；显式源码缺失硬失败；无网络PLAN；调用后状态与来源核对。

保持规模，不做整个生产系统重构或全量回归。交付新文件完整字节/SHA、日志、PLAN、用例说明及准确实验室RUN命令。目标Vita路径 `/root/agent-erratum/vendor/vita/source`，Python `/root/agent-erratum/.venv-vita/bin/python`。RUN前校园网无凭据HTTPS检查必须通过；本轮DeepSeek不联网/上传/部署/执行RUN。Codex验收后只运行这个最多3次的诊断。
