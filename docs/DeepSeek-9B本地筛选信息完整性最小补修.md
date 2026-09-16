# DeepSeek：9B 本地短筛选最小补修

目标：修正信息不足与结果标签，不换模型，不扩成新实验平台。仅离线实现并交付，停止；不SSH/部署/调模型/读密钥。

依据：results/ae-qwen35-9b-local-review-20260915-r1/README.md；原始证据transfers/ae-qwen35-9b-local-deploy-20260915-r1/。首请求没有oracle要求的“规格: 7分糖”；模型还填写了未提供的饮食禁忌事实。Codex临时probe-local.py最终NO_TOOL_CALL掩盖了前一轮attribute mismatch。

允许新增：scripts/ae_01_local_order_probe.py、tests/test_ae_local_order_probe.py及一个简短交付目录。复用既有native probe的build_case、schemas、_execute、order_matches、order_id_from；不要改旧probe常量、旧结果、oracle或云profile。以封存probe-local.py为参考，不复制整套云代理。

1. 默认plan、显式run、新目录；固定回环http://127.0.0.1:8001，模型Qwen/Qwen3.5-9B；最多3请求、不重试、不并发、2048输出、temperature0、chat_template_kwargs.enable_thinking=false，保留请求/响应/回包/数据库。不得接入云地址或其他模型。
2. 新案例显式版本local-order-complete-info-v1。保留原购买需求与native数据库；额外可见事实明确“P1目录规格原值是‘规格: 7分糖’，attributes按目录原值填写”及“本次用户明确表示无特殊饮食禁忌”。两项新增事实全部记录，不称与旧输入相同，不改旧版本。此案例只测信息齐备后的执行能力，不能测遇到未知禁忌是否询问。
3. 成功付款所在整个工具批次执行结束后立即记录native字段核对与付款ID前序来源；严格成功或失败均结束，失败写明order_state_check，不能继续一轮后用NO_TOOL_CALL覆盖。首次无工具且无完成数据库状态才记未完成；正常最终文本不能独立证明成功。编造ID/不确定工具结果/非200/length分别如实记录。发送计数不要把尝试等同于服务已收到；最小区分attempted与responses_received，不新增审计体系。
4. 仅定向离线测试：plan零网络；新wire可见精确目录值和禁忌事实；真实create→pay正确ID且规格精确则pass；错误规格在付款后仍false且理由保留；编造ID不pass；length/非200停止。可以脚本化模型响应，但必须真实native工具与评分。没有模型能力通过的声明。

交付：简短README、diff/新文件、测试日志与SHA256SUMS。无需跑全仓、17套件或重验全部旧交付。提供一条现场plan/run命令即可；后续Codex现场只复测一次，不循环调提示至通过。
