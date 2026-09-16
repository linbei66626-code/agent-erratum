# 本地短筛选运行标记：最后一个小补修

核对结果：12项定向测试复跑通过；交付12项SHA通过；生产入口与after逐字节相同。购买提示、精确oracle、付款批次停止均已符合要求。不要重做这些部分。

只改 scripts/ae_01_local_order_probe.py 的运行记录与 tests/test_ae_local_order_probe.py 的一项针对性回归；不得联网/部署/调用模型/改提示或评分/跑全仓。

两处确定问题：
1. execute_probe初始化model_called=false，此后从未更新，真实收到模型响应仍false。明确其为传输尝试标记（不是执行已确认），在调用前更新并写语义说明；服务执行是否已知以实际响应为准。
2. opener被补成默认build_opener后，finally再用opener is not None生成fixture_used，导致真实运行也恒true。必须在替换默认opener之前捕获是否由调用方显式注入，后面使用该原值。

顺手消除同一记录歧义：requests_sent当前恒等于attempted，甚至连接建立失败也加一；不要说它证明“已发出”。可删除该冗余字段，或保留为明确的legacy attempted alias并说明；不得新增发送审计体系。

一项定向回归足够：调用execute_probe不传opener，由测试替换build_opener工厂返回离线脚本响应，驱动真实native create→pay；断言fixture_used=false、model_called符合明确语义、attempted/responses各2及严格成功。显式传opener的原测试仍fixture_used=true。无应答原测试继续unknown而非服务执行已确认。

只交付diff、测试日志、简短摘要与SHA；不重验45份旧交付、不跑69项，运行本地probe套件即可。此补修不解释模型能力，也不引入新的试验。
