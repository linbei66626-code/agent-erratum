# 9B 下一步：真实 t4 执行能力探针（离线实现后停止）

用户已授权往深处测。上一轮信息完整短案例2请求严格通过；现在先测查询、选择、下单执行，暂不测记忆修正或完整R/E。不要重做短案例、部署模型、改云端入口。

## 场景与现成依据

真实数据 .ae-verify-src/tasks-full-a4553e1.json，任务U000828/sub_U000828_4；现场vendor/vita/source。复用ae_inputs.prepare_sample、ae_capability.build_capability_inputs：当前正确偏好从t3事实及公开t4变更证据构造，不读私有current。只给当前偏好快照、用户资料、原t4任务和必要领域规则；不灌完整历史，明确标记current-state execution diagnostic。不是原封不动benchmark，更不是R/E。

复用真实NativeVita环境/工具、environment_bindings、既有参数验证与执行。全部原生业务工具schema可见，不能预告目标商品ID、缩到正确商家或过滤错误候选。商品规格必须由真实查询回包获得，不把oracle值写进提示。禁止把target_product_ids、evaluation_criteria或后台全库给Agent。

## 最小入口

优先新增scripts/ae_01_local_t4_probe.py与tests/test_ae_local_t4_probe.py；配置可新增一份。复用已有模块，不改旧冻结profile/短案例或Letta，不实现新记忆策略。若复用存在实质阻碍，先交付具体阻碍及所需最小改动，不绕校验。

固定Qwen/Qwen3.5-9B、http://127.0.0.1:8001，所有模型角色均回环、显式非思考、temperature0。默认plan、preflight离线，run新目录；仅一个t4实例。Agent最多12次请求；如需澄清，复用原生用户模拟、最多4次；所有实际模型POST总计最多16、统一计数、无重试/并发；180秒请求超时，输出Agent2048/辅助4096。模拟用户不能读Agent私有评分信息之外的新材料，不要自己写假用户回答。无需模型judge：结束后复用ae_capability.diagnose_orders做确定性订单诊断，明确不是完整原生评分。

计数：每条实际发送前，使用现场同模型vLLM /tokenize按同messages/tools/chat_template_kwargs和生成前缀计数，与本请求输出预留相加核对服务真实max_model_len。当前是8192；不够即capacity_blocked，保留已完成证据，不摘要/裁剪/缩schema/扩大工具上限，不能用bytes/4猜。tokenize不可用或形状不等价则阻止run并如实给缺项。不在本任务里重启服务扩窗口。

复用26214字符工具回包上限，保留完整真实raw及实际入wire的内容/截断标记；如任务因截断信息不足，不混成纯模型失败。工具结果不确定、网络失败、length、预算耗尽分别停止留证；不循环跑到成功。

## 验收与输出

只做少量针对测试：plan/preflight零推理；初始输入无私有答案且有当前偏好/完整工具集；脚本化模型走真实查询→create→pay并由native数据库判；伪造ID/错地址糖度不通过；超上下文零推理；包含用户模拟的总预算、length和非200停止。只跑新套件及实际改动直接相关检查，不跑全仓/45份旧交付。

记录数据/代码/案例摘要，全部请求响应及工具回包，初始/最终数据库、计数、角色与总次数、付款ID来源、确定性订单诊断和停止原因。PASS只说明该次正确当前状态下真实t4执行；不证明从历史识别更新能力或长上下文能力。

交付简短README、diff、定向日志和SHA，给现场plan/preflight/run命令。Codex核对后仅执行一次；若8K容量阻止，先回报测量，不把它当能力不足或自动换模型。
