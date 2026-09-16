# 支付前置错误 r1：摘要见证最小补修

2026-09-13。待用户人工转交，未派发。仅离线补修，保留 r1 和 Codex 核验目录字节，不部署/启动服务/调用模型。

## 已通过部分

Codex 核 r1 交付清单14项及当前四文件摘要全部一致；独立禁外连复跑 test_ae_tool_errors + PaymentPreconditionPairTests 共46项，0skip全过。正常原生缺单错误回传、真实创建后支付、两臂完整输入审计、回包篡改拒绝均已覆盖，不重做核心逻辑。

## 唯一补修点

`ae_task_run.native_state_hash()` 直接调用环境任意 get_db_hash；native_write_precondition 只检查 callable；verified_write_precondition 用 str(before)==str(after) 判断有效。因此方法来源及返回值未验证。

核验探针 `results/ae-payment-precondition-codex-review-20260913-r1/review.py` 和 `witness-probes.json`：
- 原生 getter + 无写入 → 正常 error 回传。
- 原生 getter + WritingOrders 在真实 helper raise 前插入订单 → BridgeBlocked。
- 相同 WritingOrders，但 environment.get_db_hash 返回空串 → db_changed=true 却正常 error 回传。
- 相同 WritingOrders，但 getter 返回固定的先前真实64位摘要 → 同样错误放行。
- getter 返回 None → 正确 BridgeBlocked。

后两种反例的 pay/helper 仍是真实固定函数；只有摘要来源变成替代实现。属于离线故障注入，不宣称服务侧发生过。它反驳的是“缺失或过期见证必拒绝”的当前契约。只校验摘要长度/十六进制不能拒绝固定旧摘要。

## 范围与要求

只允许改 ae_task_run.py 与 tests/test_ae_tool_errors.py，新增 results/ae-payment-precondition-20260913-r2/。先保存改前完整字节/SHA；ae_adapter.py、RE测试及其他文件保持。

1. 使用真实固定来源的状态见证并验证来源，不能信任任意替代/缓存 get_db_hash。覆盖 Environment 到 ToolKitBase 的实际摘要链；也可采用明确独立的真实数据库快照核对，但不得退化为只看订单数量或仅支付字段。返回值类型/有效性严格，不用 str() 把无效返回值转成相同“证明”。
2. 缺失、异常、空串、错误类型、固定旧摘要/替代 getter 的反例均应停止；保持原生有效来源时无写入可恢复、有写入停止。不要改变 READ 恢复、WRITE 分类、其他错误保护、参数/ID/回包协议，不扩大可恢复分支。
3. 文案准确：前后快照相等只证明所覆盖状态前后相等；不单独证明过程中从未写入或不存在外部副作用。无写入结论依赖已核固定执行路径，摘要仅为额外状态核对。修正文档时写在新交付 README，不改旧 r1。
4. 在真实 Vita 环境中加入上述反例，跑现有46项定向回归及新增用例。保留原生 getter 对照，禁止 mock 核验器以求通过。保留两臂完整审计正例，无需重复112项多调用部署回归，除非新改动影响其路径。

交付两文件diff、改前后SHA、反例复跑及定向日志；固定源码/旧 raw/旧报告不改，旧云配对仍INVALID。补修完成后再准备新部署批次，不能用旧包摘要。
