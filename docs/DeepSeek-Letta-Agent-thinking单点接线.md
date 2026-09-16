# 仅修 Letta Agent 非思考字段接线

现场证据：`transfers/ae-deepseek-re-live-20260916-r1/`。完整 RUN 在 R t4 history 首请求停止；代理收到 deepseek-flash，却没有 thinking，报 declared_transport_field_missing；零生成 POST 发往官方。

任务：定位并修复真实 Letta Agent 请求构造路径，使显式 DeepSeek profile 声明的 `thinking:{"type":"disabled"}` 到达代理。NativeVita 的 extra_body 已接通，但不能当作 Letta Agent 接通的证据。只修此缺项，不取消代理必需字段校验，不默认采用服务商模式，不改提示、R/E、容量、预算或旧 Qwen 路径。

验收：用真实 Letta 请求构造路径捕获到达代理的 body，断言 thinking.disabled；缺失/错误值仍被代理拒绝；旧 Qwen 行为不变。不得手写带 thinking 的 body 冒充真实路径验收。仅定向用例，不跑全仓、不联网、不部署、不调用模型、不扩大范围。列清实际改动和是否需重建 stack/重启/新回执。新交付目录，不覆盖旧字节。
