# 支付前置错误 r2：补齐摘要链对象绑定

2026-09-13。待人工转交，未派发；只离线，保留全部旧交付及核验目录。

已确认：Codex复跑55项，0skip全过；r2清单12项及两当前文件SHA通过；原有空串/缓存摘要反例全部修正。唯一新增反例来自上轮未覆盖的对象绑定维度，不重做已通过逻辑。

## 复现

见 `results/ae-payment-precondition-codex-review-20260913-r2/review.py` 和 `witness-probes.json` 中 `other_toolkit_witness`。

真实当前环境 env，真实另一环境 other；设置 `env.tools.get_db_hash = other.tools.get_db_hash`。两个方法的 `__func__` 完全相同且都是固定 ToolKitBase.get_db_hash，但 `__self__` 是另一 toolkit。固定 Environment.get_db_hash 内部继续动态调用 self.tools.get_db_hash，因此取到的是另一数据库的摘要。

沿用 r1/r2 的 WritingOrders，在真实 pay/helper 缺单 raise 前写入当前 db。实测 db_changed=true，桥接仍返回普通 error，未 BridgeBlocked。无网络，无生产源码修改；这是故障注入，不是已观察服务故障。

## 最小范围

只改 ae_task_run.py、tests/test_ae_tool_errors.py；新建 results/ae-payment-precondition-20260913-r3/，冻结改前两文件字节/SHA。

- native_state_hash 必须同时要求环境 getter 的 __self__ is environment、toolkit getter 的 __self__ is environment.tools，并保留 __func__ 与摘要格式校验。不可将另一对象的原生绑定方法当成本对象的合法来源。
- 同文件支付前置证明里对 pay/helper 的绑定身份也核对 __self__ is 当前 toolkit，避免同类方法跨实例借用；不扩分支、不改变正常调用路径。
- 加入真实不同环境/不同toolkit借用方法反例：借用摘要即使无写入也不授权；当前db写入而被借用db不变必须停止；覆盖构建绑定后替换的逐次检查。固定当前对象的控制仍可正常恢复。
- 保留已通过55项并加上述针对性用例；不改adapter、RE测试、审计、上游、配置或旧raw；不部署/启服/调用模型。

交付两文件diff、SHA、反例与定向日志。不要把“函数定义相同”表述为“绑定对象相同”。
