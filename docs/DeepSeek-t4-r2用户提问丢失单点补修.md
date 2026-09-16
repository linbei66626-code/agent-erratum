# t4 r2：只闭合原生用户对话链

Codex离线复跑23 passed / 2 subtests，但真实NativeUser反例失败：依次提问“请问送到哪里？”、“上一条地址需要修改吗？”，捕获两次发送均只有system，问题均不在输入，两请求完全相同。无网络或推理。证据：results/ae-local-t4-r2-codex-review/user-question-counterexample.json。

根因在scripts/ae_01_local_t4_probe.py的NativeUser.next_message：创建message却未使用；每次get_init_state()清空状态；_user_visible_history传入self.messages这个类名字典而非对话，因此返回空。未调用原生generate_next_message/state.flip_roles。README声称沿用原生role flip和后处理与实现不一致。

只修NativeUser及对应定向测试/说明，不动计数门、角色预算、工具、提示/oracle、模型、服务窗口。持有同一次任务的原生UserState，实际走UserSimulator.generate_next_message和原生flip_roles/后处理；仅以受控传输适配替换其generate，并确保退出后恢复，不让真实client绕过send。初始任务可见对话及历次问答要按原生语义进入state；每条提问恰好一次，工具内部轮次不能塞进用户对话。不手工重写一份“相似”原生流程。

只补有区分力的离线用例：真实NativeUser连续两次不同问题，第一条wire含第一问（翻转为user角色）；第二条含前问、前答、当前问，顺序和原生state.flip_roles一致，无重复；证明实际调用原生generate_next_message；有计数且实际用户chat返回200并完成原生响应转换。不再只断言标记为uses_native或请求数。原有empty/length/容量拒绝保留。只跑当前套件。

另外随这一个函数顺手修正：_generate返回非200/无响应后，next_message不能统一标sent=false或被上层归类capacity；只有GateRefused且零chat才是发送前拒绝，非200/无响应保留传输失败原因。无需新增计数体系。

交付新独占目录，先拒绝目录已存在，再写任何文件；保留本轮可信r2快照。不要运行旧staging命令覆盖目录。此次仅离线补修，不SSH/部署/调模型。Codex核对后按现有授权单次运行真实t4。
