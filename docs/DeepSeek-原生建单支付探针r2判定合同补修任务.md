# 原生建单支付探针 r2：恢复原成功合同

仅改 scripts/ae_01_native_order_probe.py 与 tests/test_ae_native_order_probe.py，新增独占 r3 交付。全程离线，旧交付与核验字节保留。此前三处修复已经通过，不重做。

唯一问题：新增“成功支付必须是批次最后动作，否则未完成”不是原任务的成功条件。Codex 真实 Vita fixture：请求1创建，请求2以该真实 ID 连续支付两次（不同调用 ID），两次均 success，唯一订单字段正确且 paid、来源已核、整批无丢失，但 r2 因 tail 判 MODEL_TASK_INCOMPLETE。证据 results/ae-native-order-probe-codex-review-20260913-r2/repeated-payment.json。不要修改原生工具使第二次支付变失败。

恢复原定义：整个批次处理完才判断；核最终唯一正确 paid 订单、其支付 ID 来自更早请求创建回包且已进入后续输入，无未处理调用与不确定副作用。不要把非空 tail 本身作为任务失败的充分条件。冗余动作单独记录 batch_tail_after_success/冗余行为，不能等同订单未完成。PLAN decision_rules 同步删掉这条额外限制，不改提示/模型/参数/预算。

保留失败边界：pay+create 产生两订单不通过，应由最终订单条件拒绝；尾部未声明/参数非法等协议问题要完整记录且不冒称干净通过；不确定异常仍立即停。针对有效重复支付、产生第二张订单、非法尾部三者分别验证，不把 tail 一刀切。所有既有来源/规格/目录保护保持。

新增重复支付反例修后为任务完成且保留冗余行为记录；正常链保持通过；原三问题与原有负例继续拒绝。只跑本模块定向测试。完整 before/after 字节、SHA、diff、日志交付；不执行 RUN。验收后沿用已授权最多3请求，不再索要同一预算授权。
